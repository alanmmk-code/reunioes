"""Grava o áudio da reunião no PC e transcreve localmente com Whisper.

Duas trilhas separadas:
- microfone  -> suas falas
- loopback   -> o que sai no alto-falante/fone (os outros participantes da chamada)
Gravar separado permite marcar quem falou na transcrição.
"""

import threading
import time
import wave
from datetime import datetime

import numpy as np

import config

TAXA = 16000  # Whisper trabalha em 16 kHz

_modelos = {}
_modelo_lock = threading.Lock()


def carregar_modelo(nome: str | None = None):
    """Carrega um modelo do Whisper uma vez (na primeira vez baixa da internet).
    Padrão: o modelo da ata (mais preciso). O assistente ao vivo usa um menor e mais rápido."""
    nome = nome or config.WHISPER_MODELO
    with _modelo_lock:
        if nome not in _modelos:
            from faster_whisper import WhisperModel

            _modelos[nome] = WhisperModel(nome, device="cpu", compute_type="int8")
    return _modelos[nome]


def _microfone(parte_do_nome: str):
    """Microfone cujo nome contém o texto (ex.: "Realtek"), ou None."""
    import soundcard as sc

    if not parte_do_nome:
        return None
    for m in sc.all_microphones():
        if parte_do_nome.lower() in m.name.lower():
            return m
    return None


class _Trilha(threading.Thread):
    def __init__(self, dispositivo, caminho, parar: threading.Event):
        super().__init__(daemon=True)
        self.dispositivo, self.caminho, self.parar = dispositivo, caminho, parar
        self.erro = None
        # Áudio novo ainda não lido pelo assistente ao vivo
        self._novos, self._lock = [], threading.Lock()

    def run(self):
        try:
            with wave.open(str(self.caminho), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(TAXA)
                with self.dispositivo.recorder(samplerate=TAXA, channels=1) as rec:
                    while not self.parar.is_set():
                        dados = rec.record(numframes=None)  # lê o que estiver disponível, sem atrasar
                        if len(dados):
                            mono = np.clip(dados[:, 0], -1, 1).astype(np.float32)
                            wav.writeframes((mono * 32767).astype("<i2").tobytes())
                            with self._lock:
                                self._novos.append(mono)
                        else:
                            time.sleep(0.01)
        except Exception as e:
            self.erro = e

    def consumir(self) -> np.ndarray:
        with self._lock:
            novos, self._novos = self._novos, []
        return np.concatenate(novos) if novos else np.zeros(0, dtype=np.float32)


class Gravador:
    def __init__(self):
        self.ativo = False
        self.automatica = False  # iniciada pelo monitor do Meet
        self.reuniao = None
        self.inicio = None
        self.arquivos = {}
        self._parar = threading.Event()
        self._trilhas = []
        self.trilhas = {}

    def iniciar(self, reuniao: dict) -> None:
        import soundcard as sc

        if self.ativo:
            raise RuntimeError("Já existe uma gravação em andamento.")
        base = config.GRAVACOES_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{reuniao['id'][:20]}"
        alto_falante = sc.default_speaker()
        microfone = _microfone(config.MICROFONE) or sc.default_microphone()
        fontes = {
            "voce": microfone,
            "outros": sc.get_microphone(id=str(alto_falante.name), include_loopback=True),
        }
        # Trilha extra só para comparar microfones (não entra na transcrição)
        comparar = _microfone(config.MICROFONE_COMPARAR)
        if comparar is not None and comparar.name != microfone.name:
            fontes["comparar"] = comparar
        self._parar.clear()
        self.arquivos = {nome: base.with_name(f"{base.name}-{nome}.wav") for nome in fontes}
        self._trilhas = [_Trilha(dev, self.arquivos[nome], self._parar) for nome, dev in fontes.items()]
        self.trilhas = dict(zip(fontes, self._trilhas))  # nome -> trilha, para o assistente ao vivo
        for t in self._trilhas:
            t.start()
        self.ativo, self.reuniao, self.inicio = True, reuniao, datetime.now()



    def parar(self) -> tuple[dict, dict]:
        """Encerra a gravação e devolve (reuniao, arquivos)."""
        self._parar.set()
        for t in self._trilhas:
            t.join(timeout=10)
        erros = [str(t.erro) for t in self._trilhas if t.erro]
        reuniao, arquivos = self.reuniao, self.arquivos
        self.ativo, self.reuniao, self._trilhas, self.trilhas = False, None, [], {}
        if erros and len(erros) == len(arquivos):
            raise RuntimeError("Falha ao gravar o áudio: " + "; ".join(erros))
        return reuniao, arquivos

    def duracao(self) -> str:
        if not self.inicio:
            return "00:00"
        s = int((datetime.now() - self.inicio).total_seconds())
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _ler_wav(caminho) -> np.ndarray:
    """Lê o WAV 16 kHz mono gravado acima como float32 (formato que o Whisper aceita direto)."""
    with wave.open(str(caminho), "rb") as wav:
        bruto = wav.readframes(wav.getnframes())
    return np.frombuffer(bruto, dtype="<i2").astype(np.float32) / 32768.0


def ajustar_volume(audio: np.ndarray, janela_seg: int = 30) -> np.ndarray:
    """Deixa a fala num volume bom para o Whisper. Microfone baixo faz o modelo "inventar" frases.
    Ajusta por janelas (um barulho alto num ponto não abafa o resto) e não amplifica silêncio."""
    audio = np.asarray(audio, dtype=np.float32)
    saida = audio.copy()
    passo = TAXA * janela_seg
    for i in range(0, len(audio), passo):
        trecho = audio[i:i + passo]
        if not len(trecho):
            continue
        pico = float(np.percentile(np.abs(trecho), 99.5))  # ignora estalos isolados
        if pico < 0.03:  # só ruído de fundo: amplificar faria o Whisper "ouvir" frases no chiado
            continue
        saida[i:i + passo] = np.clip(trecho * min(0.9 / pico, 6.0), -1.0, 1.0)
    return saida


def prompt_contexto(reuniao: dict | None) -> str:
    """Só uma lista de nomes (pessoas e clientes) para o Whisper acertar a grafia.
    Frases completas no contexto fazem o modelo repeti-las quando há ruído."""
    import db

    reuniao = reuniao or {}
    nomes = [config.SEU_NOME] if config.SEU_NOME and config.SEU_NOME != "Eu" else []
    nomes += [p.get("nome") for p in reuniao.get("participantes", []) if p.get("nome")]
    cli = db.cliente(reuniao.get("cliente_id"))
    if cli:
        nomes.append(cli["nome"])
    nomes += [c["nome"] for c in db.clientes()][:25]
    nomes = list(dict.fromkeys(n.strip() for n in nomes if n and n.strip()))
    return (", ".join(nomes) + ".")[:500] if nomes else ""


def segmento_confiavel(seg) -> bool:
    """Descarta o que o próprio Whisper indica como provável invenção (alucinação)."""
    if getattr(seg, "compression_ratio", 0) > 2.4:  # frase repetida em looping
        return False
    if getattr(seg, "no_speech_prob", 0) > 0.6 and getattr(seg, "avg_logprob", 0) < -1.0:
        return False
    return getattr(seg, "avg_logprob", 0) > -1.5


def transcrever_audio(modelo, audio: np.ndarray, prompt: str = "", beam_size: int = 5):
    """Transcreve com volume ajustado, contexto e filtro de alucinação. Gera (inicio, fim, texto)."""
    segmentos, info = modelo.transcribe(
        ajustar_volume(audio), language="pt", vad_filter=True, beam_size=beam_size,
        condition_on_previous_text=False, initial_prompt=prompt or None,
    )
    palavras_prompt = _palavras(prompt or "")
    anterior = None
    for seg in segmentos:
        texto = seg.text.strip()
        palavras = _palavras(texto)
        copia_do_prompt = palavras_prompt and len(palavras) >= 2 and len(palavras & palavras_prompt) / len(palavras) >= 0.8
        if texto and segmento_confiavel(seg) and texto != anterior and not copia_do_prompt:
            anterior = texto
            yield seg.start, seg.end, texto, info


def _palavras(texto: str) -> set[str]:
    return {p.strip(".,;:!?\"'()").lower() for p in texto.split()} - {""}


def _eco(seg_mic, segs_chamada, folga=3.0, limite=0.6) -> bool:
    """Sem fone, o microfone capta o alto-falante. Uma fala do microfone é considerada eco
    se a maioria das palavras dela aparece na chamada no mesmo intervalo de tempo."""
    inicio, fim, texto = seg_mic
    palavras = _palavras(texto)
    if not palavras:
        return True
    proximas = set()
    for i2, f2, t2 in segs_chamada:
        if i2 <= fim + folga and f2 >= inicio - folga:
            proximas |= _palavras(t2)
    return len(palavras & proximas) / len(palavras) >= limite


def transcrever(arquivos: dict, progresso=lambda pct: None, reuniao: dict | None = None) -> str:
    """Transcreve as trilhas e intercala as falas em ordem de tempo."""
    modelo = carregar_modelo()
    rotulos = {"voce": config.SEU_NOME, "outros": "Outros participantes"}
    prompt = prompt_contexto(reuniao)
    por_trilha = {}
    trilhas = [(n, c) for n, c in arquivos.items() if n in rotulos and c.exists() and c.stat().st_size > 44]
    for i, (nome, caminho) in enumerate(trilhas):
        por_trilha[nome] = []
        for inicio, fim, texto, info in transcrever_audio(modelo, _ler_wav(caminho), prompt):
            por_trilha[nome].append((inicio, fim, texto))
            if info.duration:
                progresso(int(100 * (i + min(fim / info.duration, 1)) / len(trilhas)))

    outros = por_trilha.get("outros", [])
    voce = [s for s in por_trilha.get("voce", []) if not _eco(s, outros)]
    falas = [(s[0], rotulos["voce"], s[2]) for s in voce] + [(s[0], rotulos["outros"], s[2]) for s in outros]
    falas.sort(key=lambda f: f[0])
    linhas = [
        "(Transcrição automática de áudio gravado no PC. "
        f"'{config.SEU_NOME}' = microfone deste computador; 'Outros participantes' = áudio da chamada. "
        "Sem fone de ouvido, falas dos outros podem aparecer repetidas na trilha do microfone.)"
    ]
    linhas += [f"[{int(t // 60):02d}:{int(t % 60):02d}] {quem}: {texto}" for t, quem, texto in falas]
    return "\n".join(linhas) if falas else ""
