"""Assistente ao vivo: transcreve a chamada enquanto ela acontece e sugere o que responder."""

import threading
import time
import traceback


import numpy as np
from pydantic import BaseModel, Field

import atas
import config
import db
import gravador
import ia

MIN_TRECHO = 5  # segundos de áudio antes de transcrever um trecho
MAX_TRECHO = 15  # transcreve mesmo sem pausa ao chegar nisso
SILENCIO = 0.004  # volume (RMS) abaixo disso = silêncio
INTERVALO_AUTO = 12  # segundos mínimos entre análises automáticas (via API)
INTERVALO_AUTO_PLANO = 45  # idem, via assinatura (Claude Code)


class Sugestao(BaseModel):
    deve_sugerir: bool = Field(description="True se agora é um momento em que o usuário deve responder ou se posicionar")
    pergunta: str = Field(description="O que foi perguntado/pedido ao usuário e por quem, em uma frase curta")
    respostas: list[str] = Field(description="2 ou 3 opções de resposta, prontas para falar, em 1ª pessoa")
    lembrar: list[str] = Field(description="Fatos úteis do contexto (atas anteriores, pauta) para mencionar; pode ser vazio")


SYSTEM = """Você é um assistente discreto que ajuda {nome} ao vivo, durante uma reunião no Google Meet.

Você recebe a transcrição automática da reunião em andamento. Ela tem erros de reconhecimento e frases \
cortadas. '{nome}' é o que o próprio usuário falou (microfone dele); 'Outros participantes' é o áudio da \
chamada (todas as outras pessoas juntas).

Sua tarefa: perceber quando alguém se dirige a {nome} — uma pergunta, um pedido de opinião, de prazo, \
de número, de decisão, uma objeção — e sugerir o que responder.

Regras:
- deve_sugerir=false quando ninguém está esperando uma fala de {nome} no trecho mais recente, ou quando \
{nome} já respondeu.
- Respostas curtas (1 a 3 frases), naturais para falar em voz alta, em português do Brasil, na 1ª pessoa.
- Ofereça opções com posturas diferentes quando fizer sentido (ex.: aceitar, negociar, pedir tempo).
- Nunca invente números, datas ou fatos. Se a resposta depender de um dado que você não tem, escreva \
"[confirmar ...]" no lugar.
- Use o contexto (pauta, atas anteriores) para lembrar compromissos e decisões já tomadas."""


def _contexto(reuniao: dict) -> str:
    cli = db.cliente(reuniao.get("cliente_id"))
    partes = [f"Reunião: {reuniao.get('titulo', '')}", f"Cliente: {cli['nome'] if cli else 'não informado'}"]
    if cli:
        pendentes = db.tarefas(cliente_id=cli["id"])
        if pendentes:
            partes.append("Tarefas em aberto com este cliente:\n" + "\n".join(
                f"- {t['descricao']} (resp.: {t['responsavel'] or '?'}; prazo: {t['prazo'] or 'a definir'}"
                f"{'; ATRASADA' if t['atrasada'] else ''})" for t in pendentes[:30]))
    if reuniao.get("descricao"):
        partes.append(f"Descrição/pauta do convite:\n{reuniao['descricao']}")
    if reuniao.get("participantes"):
        partes.append("Convidados: " + ", ".join(p.get("nome") or p["email"] for p in reuniao["participantes"]))

    # Atas anteriores com as mesmas pessoas (ou mesmo título)
    emails = {p["email"].lower() for p in reuniao.get("participantes", [])}
    anteriores = []
    for event_id in atas.existentes():
        reg = atas.carregar(event_id) or {}
        r, a = reg.get("reuniao", {}), reg.get("ata")
        if not a or event_id == reuniao.get("id"):
            continue
        outros = {p["email"].lower() for p in r.get("participantes", [])}
        mesmo_cliente = cli and reg.get("cliente_id") == cli["id"]
        if mesmo_cliente or (emails and emails & outros) or (r.get("titulo") and r.get("titulo") == reuniao.get("titulo")):
            anteriores.append((r.get("inicio", ""), r.get("titulo", ""), a))
    for inicio, titulo, a in sorted(anteriores, reverse=True)[:3]:
        acoes = "; ".join(f"{x['tarefa']} ({x['responsavel']}, {x['prazo']})" for x in a.get("acoes", []))
        partes.append(
            f"Ata anterior — {titulo} ({inicio[:10]}):\nResumo: {a.get('resumo', '')}\n"
            f"Decisões: {'; '.join(a.get('decisoes', [])) or 'nenhuma'}\nAções: {acoes or 'nenhuma'}"
        )
    return "\n\n".join(partes)


class AoVivo:
    def __init__(self, grav: gravador.Gravador, reuniao: dict):
        self.grav, self.reuniao = grav, reuniao
        self.falas = []  # (segundos desde o início, quem, texto)
        self.sugestao = None
        self.pensando = False
        self.erro = None
        self._parar = threading.Event()
        self._pendente = {n: np.zeros(0, dtype=np.float32) for n in ("voce", "outros")}
        self._offset = {"voce": 0.0, "outros": 0.0}  # segundos já transcritos/descartados por trilha
        self._segs_outros = []  # (inicio, fim, texto) para o filtro de eco
        self._lotes = []  # transcrição enviada ao Claude em blocos (só cresce, para aproveitar o cache)
        self._enviadas = 0
        self._ultima_auto = 0.0
        self._novidade_outros = False
        self._contexto = _contexto(reuniao)
        # Pela assinatura (Claude Code) cada consulta é mais lenta e gasta cota do plano:
        # sugere sozinho só quando parece haver pergunta ou o seu nome, e com mais espaço entre consultas
        self._so_com_gatilho = config.IA_MODO != "api"
        self._intervalo_auto = INTERVALO_AUTO_PLANO if self._so_com_gatilho else INTERVALO_AUTO

    @staticmethod
    def _parece_dirigido(texto: str) -> bool:
        t = texto.lower()
        nome = config.SEU_NOME.lower().strip()
        # o Whisper às vezes erra o final do nome ("Alam" em vez de "Alan")
        return "?" in t or (len(nome) >= 3 and nome[:3] in t) or any(
            p in t for p in ("você acha", "o que acha", "consegue", "pode me", "qual o", "qual a", "quanto", "quando")
        )

    # ---------------------------------------------------------- ciclo

    def iniciar(self):
        threading.Thread(target=self._loop, daemon=True, name="ao-vivo").start()

    def parar(self):
        self._parar.set()

    def atualizar_contexto(self):
        """Chamado quando o cliente é escolhido: inclui as tarefas e atas desse cliente."""
        self._contexto = _contexto(self.reuniao)

    def _loop(self):
        gravador.carregar_modelo(config.WHISPER_MODELO_AO_VIVO)
        while not self._parar.is_set():
            try:
                for nome in ("outros", "voce"):
                    self._ouvir(nome)
                if (self._novidade_outros and not self.pensando
                        and time.time() - self._ultima_auto >= self._intervalo_auto):
                    self._novidade_outros = False
                    self._ultima_auto = time.time()
                    threading.Thread(target=self._sugerir, args=(False,), daemon=True).start()
            except Exception as e:
                traceback.print_exc()
                self.erro = str(e)
            time.sleep(1.5)

    def _ouvir(self, nome: str):
        trilha = self.grav.trilhas.get(nome)
        if not trilha:
            return
        buf = np.concatenate([self._pendente[nome], trilha.consumir()])
        dur = len(buf) / gravador.TAXA
        if dur < MIN_TRECHO:
            self._pendente[nome] = buf
            return
        fim_em_pausa = np.sqrt(np.mean(buf[-gravador.TAXA // 2:] ** 2)) < SILENCIO
        if not fim_em_pausa and dur < MAX_TRECHO:
            self._pendente[nome] = buf
            return
        self._pendente[nome] = np.zeros(0, dtype=np.float32)
        inicio = self._offset[nome]
        self._offset[nome] += dur
        if np.sqrt(np.mean(buf ** 2)) < SILENCIO:
            return  # trecho todo em silêncio
        segs, _ = gravador.carregar_modelo(config.WHISPER_MODELO_AO_VIVO).transcribe(
            buf, language="pt", vad_filter=True, beam_size=1, condition_on_previous_text=False
        )
        for s in segs:
            texto = s.text.strip()
            if not texto:
                continue
            seg = (inicio + s.start, inicio + s.end, texto)
            if nome == "outros":
                self._segs_outros.append(seg)
                if not self._so_com_gatilho or self._parece_dirigido(texto):
                    self._novidade_outros = True
            elif gravador._eco(seg, self._segs_outros[-20:]):
                continue
            quem = "Outros participantes" if nome == "outros" else config.SEU_NOME
            self.falas.append((seg[0], quem, texto))
        self.falas.sort(key=lambda f: f[0])

    # ---------------------------------------------------------- sugestões

    def pedir_sugestao(self):
        if not self.pensando:
            threading.Thread(target=self._sugerir, args=(True,), daemon=True).start()

    def _sugerir(self, pedido_pelo_usuario: bool):
        novas = self.falas[self._enviadas:]
        if not self.falas:
            if pedido_pelo_usuario:
                self.sugestao = {"pergunta": "Ainda não ouvi nada da conversa.", "respostas": [], "lembrar": []}
            return
        self.pensando = True
        try:
            if novas:
                self._lotes.append("\n".join(f"[{int(t // 60):02d}:{int(t % 60):02d}] {q}: {x}" for t, q, x in novas))
                self._enviadas = len(self.falas)
            instrucao = (
                f"{config.SEU_NOME} pediu ajuda agora: sugira o que ele pode dizer neste momento, "
                "com base no trecho mais recente (deve_sugerir=true)."
                if pedido_pelo_usuario
                else "Analise o trecho mais recente. Há algo que alguém espera que o usuário responda agora?"
            )
            if self.sugestao and self.sugestao.get("pergunta"):
                instrucao += f"\nÚltima sugestão já mostrada (não repita se nada mudou): {self.sugestao['pergunta']}"
            blocos = [f"<contexto>\n{self._contexto}\n</contexto>"]
            blocos += [f"<transcricao_parte>\n{lote}\n</transcricao_parte>" for lote in self._lotes]
            s = ia.gerar(SYSTEM.format(nome=config.SEU_NOME), blocos, instrucao, Sugestao,
                         effort="low")  # rapidez importa mais que profundidade aqui
            if s.deve_sugerir or pedido_pelo_usuario:
                self.sugestao = {"pergunta": s.pergunta, "respostas": s.respostas, "lembrar": s.lembrar,
                                 "hora": time.strftime("%H:%M:%S")}
            self.erro = None
        except Exception as e:
            traceback.print_exc()
            self.erro = f"Erro ao consultar o Claude: {e}"
        finally:
            self.pensando = False

    # ---------------------------------------------------------- estado para a janela

    def estado(self) -> dict:
        return {
            "falas": [{"t": f"{int(t // 60):02d}:{int(t % 60):02d}", "quem": q, "texto": x} for t, q, x in self.falas[-8:]],
            "sugestao": self.sugestao,
            "pensando": self.pensando,
            "erro": self.erro,
        }

    def transcricao(self) -> str:
        return "\n".join(f"[{int(t // 60):02d}:{int(t % 60):02d}] {q}: {x}" for t, q, x in self.falas)
