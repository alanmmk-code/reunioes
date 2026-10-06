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

_modelo = None
_modelo_lock = threading.Lock()


def carregar_modelo():
    """Carrega o Whisper uma vez (na primeira vez baixa o modelo da internet)."""
    global _modelo
    with _modelo_lock:
        if _modelo is None:
            from faster_whisper import WhisperModel

            _modelo = WhisperModel(config.WHISPER_MODELO, device="cpu", compute_type="int8")
    return _modelo


class _Trilha(threading.Thread):
    def __init__(self, dispositivo, caminho, parar: threading.Event):
        super().__init__(daemon=True)
        self.dispositivo, self.caminho, self.parar = dispositivo, caminho, parar
        self.erro = None

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
                            wav.writeframes((np.clip(dados[:, 0], -1, 1) * 32767).astype("<i2").tobytes())
                        else:
                            time.sleep(0.01)
        except Exception as e:
            self.erro = e


class Gravador:
    def __init__(self):
        self.ativo = False
        self.automatica = False  # iniciada pelo monitor do Meet
        self.reuniao = None
        self.inicio = None
        self.arquivos = {}
        self._parar = threading.Event()
        self._trilhas = []

    def iniciar(self, reuniao: dict) -> None:
        import soundcard as sc

        if self.ativo:
            raise RuntimeError("Já existe uma gravação em andamento.")
        base = config.GRAVACOES_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{reuniao['id'][:20]}"
        alto_falante = sc.default_speaker()
        fontes = {
            "voce": sc.default_microphone(),
            "outros": sc.get_microphone(id=str(alto_falante.name), include_loopback=True),
        }
        self._parar.clear()
        self.arquivos = {nome: base.with_name(f"{base.name}-{nome}.wav") for nome in fontes}
        self._trilhas = [_Trilha(dev, self.arquivos[nome], self._parar) for nome, dev in fontes.items()]
        for t in self._trilhas:
            t.start()
        self.ativo, self.reuniao, self.inicio = True, reuniao, datetime.now()
        # Já vai carregando o Whisper enquanto a reunião acontece
        threading.Thread(target=carregar_modelo, daemon=True).start()

    def parar(self) -> tuple[dict, dict]:
        """Encerra a gravação e devolve (reuniao, arquivos)."""
        self._parar.set()
        for t in self._trilhas:
            t.join(timeout=10)
        erros = [str(t.erro) for t in self._trilhas if t.erro]
        reuniao, arquivos = self.reuniao, self.arquivos
        self.ativo, self.reuniao, self._trilhas = False, None, []
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


def transcrever(arquivos: dict, progresso=lambda pct: None) -> str:
    """Transcreve as trilhas e intercala as falas em ordem de tempo."""
    modelo = carregar_modelo()
    rotulos = {"voce": config.SEU_NOME, "outros": "Outros participantes"}
    por_trilha = {}
    trilhas = [(n, c) for n, c in arquivos.items() if c.exists() and c.stat().st_size > 44]
    for i, (nome, caminho) in enumerate(trilhas):
        segmentos, info = modelo.transcribe(
            _ler_wav(caminho), language="pt", vad_filter=True, beam_size=5, condition_on_previous_text=False
        )
        por_trilha[nome] = []
        for seg in segmentos:
            texto = seg.text.strip()
            if texto:
                por_trilha[nome].append((seg.start, seg.end, texto))
            if info.duration:
                progresso(int(100 * (i + min(seg.end / info.duration, 1)) / len(trilhas)))

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
