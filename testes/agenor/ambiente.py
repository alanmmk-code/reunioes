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


# tudo o que e publico em google_services vira bloqueio; so as funcoes de leitura da agenda ganham um falso
for _n in dir(g):
    if not _n.startswith("_") and _n not in ("id_do_link",) and callable(getattr(g, _n)) and getattr(getattr(g, _n), "__module__", "") == g.__name__:
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
            "ja_terminou": dias < 0}


def _listar_eventos(ini, fim):
    return [e for e in EVENTOS if True]


g.listar_eventos = _listar_eventos
g.listar_reunioes = lambda dias_atras=7, dias_frente=7: [e for e in EVENTOS if e["link"]]
g.obter_reuniao = lambda eid: next((e for e in EVENTOS if e["id"] == eid and e["link"]), None)
g.criar_google_doc = lambda titulo, html: "https://docs.google.com/document/d/" + "A" * 25 + "/edit"
LIXEIRA = []
g.drive_lixeira = lambda fid: LIXEIRA.append(fid)
g.buscar_transcricao = lambda r: ("texto da transcricao", "falso")
g.enviar_email = lambda dest, assunto, html: EMAILS.append(dest)
g.agendar_followup = lambda *a, **k: {"link_evento": "https://x", "link_meet": "https://y"}


def _gerar_ata(transcricao, reuniao):
    r = RNG["r"]
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
reunioes.sincronia = sincronia
reunioes.processar_gravacao = lambda *a, **k: None
reunioes.processar_texto = lambda *a, **k: None
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
    LIXEIRA.clear()
    reunioes.TAREFA.update(ativa=False, etapa="", pct=0, erro=None, event_id=None)
    entrar("A")
    SUJOS.update(PCS)
    return reunioes.app.test_client()


def limpar():
    shutil.rmtree(TMP, ignore_errors=True)
