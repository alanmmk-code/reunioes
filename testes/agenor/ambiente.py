"""O ambiente do Agenor: isola TUDO numa pasta temporaria e bloqueia rede/servicos.

Ordem importa: a pasta de dados e trocada ANTES de importar db (db.ARQUIVO sai de config.DADOS_DIR e
db.criar_tabelas() roda no import). Nada aqui toca dados.db, atas/, gravacoes/, token.json, .env.
"""
import json
import os
import random
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
# AGENOR_ALVO: testa outra copia do codigo (ex.: `git archive HEAD` numa pasta) em vez da arvore de trabalho
ALVO = Path(os.environ.get("AGENOR_ALVO") or RAIZ)
sys.path.insert(0, str(ALVO))

TMP = Path(tempfile.mkdtemp(prefix="agenor_reunioes_"))

# --- BLOQUEIO DO OUTLOOK (antes de importar qualquer modulo do sistema): nada pode abrir o Outlook nem enviar e-mail real.
import types as _types  # noqa: E402


class _OutlookBloqueado(Exception):
    pass


def _dispatch_bloqueado(*a, **k):
    raise _OutlookBloqueado("win32com.client.Dispatch bloqueado no teste")


_w32 = _types.ModuleType("win32com")
_w32c = _types.ModuleType("win32com.client")
_w32c.Dispatch = _dispatch_bloqueado
_w32c.DispatchEx = _dispatch_bloqueado
_w32c.GetActiveObject = _dispatch_bloqueado
_w32.client = _w32c
_pyc = _types.ModuleType("pythoncom")
_pyc.CoInitialize = _dispatch_bloqueado
_pyc.CoInitializeEx = _dispatch_bloqueado
_pyc.CoUninitialize = lambda: None
sys.modules.update({"win32com": _w32, "win32com.client": _w32c, "pythoncom": _pyc})
ENVIOS = []  # tudo que o sistema tentou enviar por envio.* (registrado, nunca enviado)


def _bloquear_envio():
    try:
        import envio as _e
    except ImportError:
        return
    retornos = {"contas": ["teste@exemplo.com"], "remetente_padrao": "teste@exemplo.com", "enviar_convite": "entrada-falsa",
                "convite_do_evento": None, "enviar_email": lambda: ["teste@exemplo.com"], "presos": lambda: [],
                "outlook_aberto": False, "atualizar_contas": lambda: ["teste@exemplo.com"], "conferir": "enviado"}
    for _n in dir(_e):
        _f = getattr(_e, _n)
        if not _n.startswith("_") and callable(_f) and getattr(_f, "__module__", "") == _e.__name__:
            ret = retornos.get(_n)
            setattr(_e, _n, (lambda nome, ret: lambda *a, **k: (ENVIOS.append((nome, a, k)), ret() if callable(ret) else ret)[1])(_n, ret))


import config  # noqa: E402

config.DADOS_DIR = TMP
config.ATAS_DIR = TMP / "atas"
config.GRAVACOES_DIR = TMP / "gravacoes"
config.SINCRONIZAR = False
config.LEMBRETES = False
config.GRAVACAO_AUTOMATICA = False
config.ASSISTENTE_AO_VIVO = False

import db  # noqa: E402

assert TMP in db.ARQUIVO.parents, "db.ARQUIVO nao esta na pasta temporaria"

_bloquear_envio()  # envio.py (Outlook) vira falso ANTES de reunioes/extras o importarem
import analisador  # noqa: E402
import atas  # noqa: E402
import google_services as g  # noqa: E402
import lembretes  # noqa: E402
import monitor  # noqa: E402
import sincronia  # noqa: E402
import reunioes  # noqa: E402
import ia  # noqa: E402
import subprocess  # noqa: E402


class RedeBloqueada(Exception):
    pass


FALTANDO = set()  # servicos externos que uma rota tentou usar e o ambiente nao tem falso para (cobertura, nao defeito)


def _bloqueio(nome):
    def f(*a, **k):
        FALTANDO.add(f"google_services.{nome}")
        raise RedeBloqueada(f"google_services.{nome} chamado no teste")
    return f


def _bloqueio_proc(*a, **k):
    FALTANDO.add("subprocess")
    raise RedeBloqueada("subprocess bloqueado no teste")


subprocess.Popen = _bloqueio_proc
subprocess.run = _bloqueio_proc
subprocess.check_output = _bloqueio_proc
ia.gerar = lambda *a, **k: (FALTANDO.add("ia.gerar"), (_ for _ in ()).throw(RedeBloqueada("ia.gerar bloqueado")))[1]


# tudo o que e publico em google_services vira bloqueio (menos as funcoes puras, sem rede); a agenda ganha um falso
for _n in dir(g):
    if not _n.startswith("_") and _n not in ("id_do_link", "anotar_conexao", "autorizacao_vencida") and callable(getattr(g, _n)) and getattr(getattr(g, _n), "__module__", "") == g.__name__:
        setattr(g, _n, _bloqueio(_n))

CLOCK = {"n": 0}
RNG = {"r": random.Random(0)}
BASE_T = datetime(2030, 1, 1, tzinfo=timezone.utc)


def _agora():
    CLOCK["n"] += 1  # estritamente crescente: sem empate de milissegundo entre os "PCs"
    return (BASE_T + timedelta(milliseconds=CLOCK["n"])).isoformat(timespec="milliseconds")


class _U:
    def __init__(self):
        self.hex = "%032x" % RNG["r"].getrandbits(128)


class _UUIDStub:
    @staticmethod
    def uuid4():
        return _U()


db.agora = _agora
db._uuid = _UUIDStub

EVENTOS = []
EMAILS = []


def _evento(i, dias, meet=True, dia_inteiro=False):
    ini = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=dias)
    if dia_inteiro:
        return {"id": f"ev{i}", "titulo": f"Dia {i}", "descricao": "", "local": "", "inicio": ini.date().isoformat(),
                "fim": (ini.date() + timedelta(days=1)).isoformat(), "dia_inteiro": True, "link": None,
                "link_agenda": None, "codigo_meet": None, "participantes": [], "anexos": [], "ja_terminou": False}
    return {"id": f"ev{i}", "titulo": f"Reuniao {i}", "descricao": "pauta<br>x", "local": "", "inicio": ini.isoformat(),
            "fim": (ini + timedelta(hours=1)).isoformat(), "dia_inteiro": False,
            "link": "https://meet.example/abc" if meet else None, "link_agenda": None, "codigo_meet": "abc",
            "participantes": [{"email": "a@x.com", "nome": "A", "resposta": ""}], "anexos": [],
            "ja_terminou": dias < 0, "organizador": i % 2 == 1}


def _listar_eventos(ini, fim):
    return [e for e in EVENTOS if True]


g.listar_eventos = _listar_eventos
g.listar_reunioes = lambda dias_atras=7, dias_frente=7: [e for e in EVENTOS if e["link"]]
g.obter_reuniao = lambda eid: next((e for e in EVENTOS if e["id"] == eid and e["link"]), None)
REMARCADOS = []  # (event_id, inicio, duracao, titulo, avisar) de cada remarcar_evento


def _obter_evento(eid):
    e = next((x for x in EVENTOS if x["id"] == eid), None)
    return e and {**e, "organizador": e.get("organizador", False), "participantes": [p["email"] for p in e["participantes"]]}


def _remarcar_evento(event_id, inicio, duracao_min, titulo=None, avisar=True):
    inicio + timedelta(minutes=duracao_min)  # como o real: o fim tem de caber no calendario
    REMARCADOS.append((event_id, inicio, duracao_min, titulo, avisar))
    return {"link_evento": "https://x", "link_meet": "https://y"}


CANCELADOS = []  # (event_id, avisar) de cada cancelar_evento


def _cancelar_evento(event_id, avisar=True):
    if not any(e["id"] == event_id for e in EVENTOS):
        raise RuntimeError("evento nao existe")  # o Google responderia 404/410
    CANCELADOS.append((event_id, avisar))  # so registra: a agenda falsa fica igual (testes antigos contam com ev1..ev5)


g.obter_evento = _obter_evento
g.cancelar_evento = _cancelar_evento
g.remarcar_evento = _remarcar_evento
g.criar_google_doc = lambda titulo, html: "https://docs.google.com/document/d/" + "A" * 25 + "/edit"
DOCS_ATUALIZADOS = []  # (link, titulo) de cada atualizar_google_doc
g.atualizar_google_doc = lambda link, titulo, html: (DOCS_ATUALIZADOS.append((link, titulo)),
                                                     link if g.id_do_link(link) else g.criar_google_doc(titulo, html))[1]
g.doc_modificado_em = lambda link: None  # o doc falso nunca e alterado fora do painel
LIXEIRA = []
g.drive_lixeira = lambda fid: LIXEIRA.append(fid)
g.buscar_transcricao = lambda r: ("texto da transcricao", "falso")
g.enviar_email = lambda dest, assunto, html: EMAILS.append(dest)
AGENDADOS = []  # (args, kwargs) de cada agendar_followup
g.agendar_followup = lambda *a, **k: (AGENDADOS.append((a, k)), {"link_evento": "https://x", "link_meet": "https://y"})[1]
INCLUIDOS = []  # (event_id, emails, avisar) de cada incluir_convidados


def _incluir_convidados(event_id, emails, avisar=True):
    e = next((x for x in EVENTOS if x["id"] == event_id), None)
    if not e:
        raise RuntimeError("evento nao existe")
    INCLUIDOS.append((event_id, list(emails), avisar))
    ja = {p["email"].lower() for p in e["participantes"]}
    return [x for x in dict.fromkeys(emails) if x.lower() not in ja]  # como o real; a agenda falsa fica igual


g.incluir_convidados = _incluir_convidados


GANCHO_ATA = {"fn": None}  # chamado dentro do gerar_ata falso (a acao de cancelar aperta o botao ali, como no meio do Claude)


def _gerar_ata(transcricao, reuniao):
    r = RNG["r"]
    if GANCHO_ATA["fn"]:
        GANCHO_ATA["fn"]()
        ia.conferir_cancelamento()  # o _via_claude_code de verdade confere ao entrar e mata o processo quando o evento liga
    prazos = ["A definir", "", "2030-01-15", "amanha", (datetime.now().date() + timedelta(days=r.randint(-5, 5))).isoformat()]
    return analisador.Ata(
        titulo="Ata falsa", resumo="Resumo. " * r.randint(1, 30), participantes=["A"],
        topicos=[analisador.Topico(titulo="t", discussao="d")], decisoes=["d1"],
        acoes=[analisador.Acao(tarefa=f"acao {k}", responsavel=r.choice(["A definir", "Joao"]), prazo=r.choice(prazos),
                               do_usuario=r.random() < 0.6) for k in range(r.randint(0, 4))],
        pontos_de_atencao=[], proxima_reuniao=analisador.ProximaReuniao(combinada=False, data_hora="", duracao_min=30, pauta=[]))


analisador.gerar_ata = _gerar_ata
monitor.notificar = lambda *a, **k: None
sincronia.agendar = lambda *a, **k: None


def _sinc_falsa():
    raise RuntimeError("sem rede no teste")


sincronia.sincronizar = _sinc_falsa
LOGINS = []  # cada "Reconectar Google" (o login de verdade abriria o navegador)


def _reconectar(espera_s=300):
    LOGINS.append(espera_s)
    return "teste@exemplo.com"


g.reconectar = _reconectar
sincronia.enviar_copias = lambda *a, **k: 0
sincronia.PC = "PC-TESTE"  # o nome de verdade do PC nao entra em semente (o repositorio e publico)
reunioes.sincronia = sincronia
PROCESSAR_GRAVACAO_REAL = reunioes._processar_gravacao  # a acao "cancelar a criacao da ata" roda o de verdade, em linha
PROCESSAR_TEXTO_REAL = reunioes.processar_texto
PROCESSAR_GRAVACAO_FILA_REAL = reunioes.processar_gravacao  # com `item` e a trava: a acao da fila roda os de verdade, em linha
PROCESSAR_TEXTO_FILA_REAL = reunioes.processar_texto
WORKERS = []  # o que as rotas teriam posto em threads: ("gravacao"|"texto", args); a acao da fila roda estes em sequencia
reunioes.processar_gravacao = lambda *a, **k: WORKERS.append(("gravacao", a))
reunioes.processar_texto = lambda *a, **k: WORKERS.append(("texto", a))
reunioes._encerrar_processo = lambda: None  # /reiniciar nunca derruba o teste (e o Popen ja e bloqueado)
reunioes.app.testing = True
reunioes.app.config["PROPAGATE_EXCEPTIONS"] = True

PCS = ("A", "B")
SUJOS = set()  # PCs tocados desde a ultima medicao de estado (otimizacao: so mede quem pode ter mudado)
RUN = {"dir": None}


def entrar(pc):
    SUJOS.add(pc)
    d = RUN["dir"] / pc
    db.ARQUIVO = d / "dados.db"
    config.ATAS_DIR = d / "atas"
    config.GRAVACOES_DIR = d / "gravacoes"
    assert TMP in db.ARQUIVO.parents and TMP in config.ATAS_DIR.parents and TMP in config.GRAVACOES_DIR.parents
    return d


def novo_mundo(semente):
    if RUN["dir"]:
        shutil.rmtree(RUN["dir"], ignore_errors=True)
    RUN["dir"] = TMP / "run"
    for pc in PCS:
        d = RUN["dir"] / pc
        (d / "atas").mkdir(parents=True)
        (d / "gravacoes").mkdir(parents=True)
        entrar(pc)
        db.criar_tabelas()
    CLOCK["n"] = 0
    RNG["r"] = random.Random(f"uuid:{semente}")
    EVENTOS[:] = [_evento(1, 0), _evento(2, -1), _evento(3, 2, meet=False), _evento(4, 1, dia_inteiro=True), _evento(5, 3)]
    EMAILS.clear()
    ENVIOS.clear()
    REMARCADOS.clear()
    CANCELADOS.clear()
    DOCS_ATUALIZADOS.clear()
    LIXEIRA.clear()
    AGENDADOS.clear()
    INCLUIDOS.clear()
    LOGINS.clear()
    g.ESTADO["desconectado"] = None
    g.ESTADO["login"].update(rodando=False, erro=None, conta=None)
    sincronia.ESTADO["outros_pcs"] = {}
    reunioes.TAREFA.update(ativa=False, etapa="", pct=0, erro=None, event_id=None, cancelavel=False, cancelada=False,
                           etapa_desde=0.0)
    reunioes.FILA.clear()
    WORKERS.clear()
    reunioes.CANCELAR_ATA.clear()
    ia.cancelavel(None)
    GANCHO_ATA["fn"] = None
    entrar("A")
    SUJOS.update(PCS)
    return reunioes.app.test_client()


def limpar():
    shutil.rmtree(TMP, ignore_errors=True)
