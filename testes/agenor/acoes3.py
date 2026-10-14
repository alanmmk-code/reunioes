"""Catalogo v3 = catalogo v2 (acoes2.py, CONGELADO) + pessoas dos clientes, remarcar, envio e extensao.

Mesmo motivo do v2: acrescentar acao na lista v2 mudaria o passeio das sementes v2 guardadas.
"""
import unicodedata
from datetime import datetime, timedelta

import acoes2 as A2
import ambiente as amb
import db
import envio
import oraculo as o
from acoes import Acao, HUGE, _clientes, chamar
from oraculo import cobrar

# nomes de quem entra na chamada: caixa, acento, espacos, e lixo
PESSOAS = ["Ana", "ana", "ANA ", "João", "JOÃO", "joão", "  Ana   Maria ", "ana maria", "Zoë", "ZOË", "Bob", "Çá"]
PESSOAS_LIXO = ["", "   ", None, 5, "x" * 200, "\n\t"]


def _chave(nome):
    return unicodedata.normalize("NFC", str(nome)).casefold()


def _limpo(nome):
    return " ".join(str(nome or "").split())[:80]


def _vivas(cid):
    return [p for p in o.linhas("pessoas") if p["cliente_id"] == cid and not p["excluido"]]


# ----------------------------------------------------------------------------- pessoas dos clientes

def f_adicionar_pessoa(m, r):
    pc = m.pc(r)
    cl = _clientes()
    cid = r.choice([c["id"] for c in cl] + [999999, None, "abc"]) if cl else 999999
    nome = r.choice(PESSOAS + (PESSOAS_LIXO if r.random() < 0.2 else []))
    antes = {_chave(p["nome"]) for p in _vivas(cid)} if isinstance(cid, int) else set()
    nova = db.adicionar_pessoa(cid, nome)
    limpo = _limpo(nome)
    if not limpo or not db.cliente(cid):
        cobrar(nova is False, "I21-adicionar_pessoa-invalida", f"{pc}: cliente={cid!r} nome={nome!r} devolveu {nova}")
        return f"{pc} pessoa invalida {nome!r} cliente={cid!r}"
    depois = db.pessoas(cid)
    cobrar(_chave(limpo) in {_chave(n) for n in depois}, "I21-adicionar_pessoa-nao-aparece", f"{pc}: {limpo!r} nao esta em pessoas({cid})")
    cobrar(nova == (_chave(limpo) not in antes), "I21-adicionar_pessoa-retorno",
           f"{pc}: {limpo!r} devolveu {nova}, ja havia {sorted(antes)}")
    return f"{pc} pessoa {limpo!r} cliente {cid} nova={nova}"


def f_rota_api_cliente_pessoas(m, r):
    """A janelinha manda quem esta na chamada; as pessoas ficam ligadas ao cliente."""
    m.pc(r)
    cl = _clientes()
    dados = {"cliente_id": r.choice([c["id"] for c in cl] + [None, "x", HUGE]) if cl else None,
             "pessoas": r.choice([[r.choice(PESSOAS) for _ in range(r.randint(0, 4))], "Ana", None, [None, 5, "Ana"], {}])}
    if r.random() < 0.2:
        dados["novo"] = r.choice(["Novo P", "acme", " "])
    pc, resp = chamar(m, r, "post", "/api/cliente", json=dados)
    cid = db.cliente(dados.get("cliente_id")) if not (isinstance(dados.get("novo"), str) and dados["novo"].strip()) else None
    if cid and isinstance(dados["pessoas"], list):
        tem = {_chave(n) for n in db.pessoas(cid["id"])}
        for n in dados["pessoas"]:
            if _limpo(n):
                cobrar(_chave(_limpo(n)) in tem, "I22-api_cliente-pessoa-nao-ligada", f"{pc}: {n!r} nao ficou no cliente {cid['id']}")
    return f"{pc} /api/cliente {str(dados)[:60]}"


def f_pessoas_nos_dois_pcs(m, r):
    """Cada PC liga pessoas ao mesmo cliente (as vezes so a caixa/acento muda), troca, e os dois tem de ver as mesmas."""
    nome_cli = r.choice(["Acme", "Beta Ltda", "Gama"])
    for pc in amb.PCS:
        amb.entrar(pc)
        cid = db.criar_cliente(nome_cli)
        for _ in range(r.randint(1, 3)):
            db.adicionar_pessoa(cid, r.choice(PESSOAS))
    o.trocar(m)
    vistas = {}
    for pc in amb.PCS:
        amb.entrar(pc)
        cli = {c["id"]: _chave(c["nome"]) for c in _clientes() if not c["excluido"]}
        vistas[pc] = sorted((cli[cid], _chave(n)) for cid, ns in db.pessoas_por_cliente().items() if cid in cli for n in ns)
    cobrar(vistas["A"] == vistas["B"], "S10-pessoas-nao-convergem",
           f"cliente {nome_cli!r}: A={[n for c, n in vistas['A'] if c == _chave(nome_cli)]} B={[n for c, n in vistas['B'] if c == _chave(nome_cli)]}")
    return f"pessoas em {nome_cli!r} nos dois PCs + troca"


# ----------------------------------------------------------------------------- remarcar (Google Agenda falso)

def f_remarcar(m, r):
    m.pc(r)
    ev = r.choice(amb.EVENTOS + [{"id": "nao-existe"}])
    if r.random() < 0.7:  # quase sempre um pedido que tem de dar certo; senao o lado que remarca quase nunca roda
        data, hora, dur = r.choice(["2030-02-03", "2026-12-31", "9999-12-31"]), r.choice(["14:30", "08:05", "23:59"]), r.choice(["30", "0", "-5", "100000", HUGE])
    else:
        data, hora, dur = r.choice(["", "03/02/2030", "2030-02-30"]), r.choice(["", "25:00", "9:00"]), r.choice(["abc", "", "30"])
    dados = {"data": data, "hora": hora, "duracao": dur}
    if r.random() < 0.8:
        dados["titulo"] = r.choice(["", "Novo titulo", ev.get("titulo", "")])
    com_convite = r.random() < 0.3
    original = envio.convite_do_evento
    if com_convite:  # convite que saiu pelo Outlook: Google sem aviso, atualizacao pelo Outlook
        envio.convite_do_evento = lambda eid: {"entry_id": "entrada-falsa"}
    amb.REMARCADOS.clear()
    n_envios = len(amb.ENVIOS)
    try:
        pc, resp = chamar(m, r, "post", f"/reuniao/{ev['id']}/remarcar", data=dados)
    finally:
        envio.convite_do_evento = original
    try:
        inicio = datetime.fromisoformat(f"{data}T{hora}")
        inicio + timedelta(minutes=max(5, min(int(dados["duracao"]), 24 * 60)))  # o fim tem de caber no calendario
        valido = True
    except (ValueError, OverflowError):
        valido = False
    pode = bool(ev.get("organizador")) and not ev.get("dia_inteiro") and ev["id"] != "nao-existe"
    feitos = list(amb.REMARCADOS)
    if not (pode and valido):
        cobrar(not feitos, "I23-remarcar-indevido", f"{pc}: {ev['id']} organizador={ev.get('organizador')} dados={dados} remarcou {feitos}")
        return f"{pc} remarcar {ev['id']} recusado"
    cobrar(len(feitos) == 1, "I23-remarcar-nao-remarcou", f"{pc}: {ev['id']} dados={dados} -> {len(feitos)} chamada(s)")
    eid, ini, dur, tit, avisar = feitos[0]
    cobrar(ini == inicio, "I23-remarcar-horario", f"{pc}: pediu {inicio} e foi {ini}")
    cobrar(5 <= dur <= 24 * 60, "I23-remarcar-duracao", f"{pc}: duracao {dados['duracao']!r} -> {dur}")
    cobrar(avisar == (not com_convite), "I23-remarcar-aviso", f"{pc}: convite pelo Outlook={com_convite} e Google avisar={avisar}")
    if com_convite:
        atual = [a for a in amb.ENVIOS[n_envios:] if a[0] == "atualizar_convite"]
        cobrar(len(atual) == 1 and atual[0][1][1] == inicio, "I23-remarcar-outlook", f"{pc}: atualizacao do convite no Outlook: {atual}")
    return f"{pc} remarcou {ev['id']} -> {inicio} ({dur} min)"


# ----------------------------------------------------------------------------- envios presos no Outlook

def f_rota_envio(m, r):
    m.pc(r)
    envio.ENVIOS[:] = [{"id": i, "tipo": "email", "remetente": "teste@exemplo.com", "assunto": f"s{i}", "para": "a@b.com",
                        "momento": 0, "estado": r.choice(["preso", "conferindo", "enviado"])} for i in range(1, 4)]
    antes = [dict(e) for e in envio.ENVIOS]
    alvo = r.choice([1, 2, 3, 99])
    acao = r.choice(["dispensar", "conferir", "apagar", "DISPENSAR"])
    pc, resp = chamar(m, r, "post", f"/envio/{alvo}/{acao}")
    for a, d in zip(antes, envio.ENVIOS):
        if a["id"] == alvo and acao == "dispensar":
            cobrar(d["estado"] == "dispensado", "I24-envio-dispensar", f"{pc}: dispensar {alvo} deixou {d['estado']!r}")
        elif acao != "conferir":
            cobrar(d == a, "I24-envio-mexeu-em-outro", f"{pc}: {acao} {alvo} mudou o envio {a['id']}: {a['estado']} -> {d['estado']}")
    envio.ENVIOS.clear()
    return f"{pc} envio {alvo} {acao}"


# ----------------------------------------------------------------------------- extensao (sem gravacao em andamento)

def f_rota_extensao_sem_gravar(m, r):
    """Sem gravacao: pedaco de video sem o comeco nao vira arquivo; audio e erro nao estouram nem gravam nada."""
    m.pc(r)
    arquivos = lambda: sorted(p.name for p in amb.config.GRAVACOES_DIR.glob("*"))  # noqa: E731
    antes = arquivos()
    esc = r.choice(["tela_sem_comeco", "audio", "erro"])
    if esc == "tela_sem_comeco":
        aba = r.choice([7, 99999, -1])
        pc, resp = chamar(m, r, "post", f"/api/extensao/tela?aba={aba}&parte={r.choice([1, 5])}", data=b"\x1a\x45\xdf\xa3")
        cobrar(arquivos() == antes, "I25-extensao-tela-sem-comeco", f"{pc}: aba {aba} sem parte=0 criou {set(arquivos()) - set(antes)}")
    elif esc == "audio":
        pc, resp = chamar(m, r, "post", f"/api/extensao/audio?aba={r.choice([1, 'x'])}", data=r.choice([b"", b"\x00", b"\x00\x01" * 100]))
        cobrar(arquivos() == antes, "I25-extensao-audio-sem-gravar", f"{pc}: audio sem gravacao criou arquivo")
    else:
        pc, resp = chamar(m, r, "post", "/api/extensao/erro", json=r.choice([{"onde": "x" * 500, "erro": None}, [], "texto", None]))
    return f"{pc} extensao {esc}"


ACOES3 = A2.ACOES2 + [
    Acao("adicionar pessoa", f_adicionar_pessoa, 3, cobre=["db.adicionar_pessoa", "db.pessoas"]),
    Acao("POST /api/cliente com pessoas", f_rota_api_cliente_pessoas, 2, True, ["/api/cliente", "db.adicionar_pessoa"]),
    Acao("pessoas nos dois PCs (+troca)", f_pessoas_nos_dois_pcs, 2, cobre=["db.pessoas_por_cliente", "db.mesclar"]),
    Acao("remarcar reuniao", f_remarcar, 3, True, ["/reuniao/<event_id>/remarcar"]),
    Acao("POST /envio/<id>/<acao>", f_rota_envio, 2, True, ["/envio/<int:envio_id>/<acao>"]),
    Acao("extensao sem gravacao", f_rota_extensao_sem_gravar, 2, True,
         ["/api/extensao/tela", "/api/extensao/audio", "/api/extensao/erro"]),
]
SEM_ACAO = {**A2.SEM_ACAO,
            "/api/extensao/estado e gravacao pela extensao": "dispara gravacao de audio real; sem gravar, o fuzz generico cobre"}
SEM_FUZZ = A2.SEM_FUZZ
