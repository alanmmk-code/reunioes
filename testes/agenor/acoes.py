"""O catalogo: DADO, nao logica. Cada Acao tem nome, peso, se e hostil, e o que cobre (rotas / funcoes).

Legitima: tem de passar. Hostil: tem de ser recusada ou ignorada SEM estourar e SEM mudar o estado indevidamente.
Para acrescentar uma acao: escreva `def f_xxx(m, r)` e uma linha em ACOES. O corredor nao conhece nenhuma pelo nome.
"""
import json
import re
import types
from dataclasses import dataclass, field
from datetime import date, timedelta

import ambiente as amb
import atas
import db
import oraculo as o
from oraculo import cobrar


@dataclass
class Acao:
    nome: str
    fn: object
    peso: int = 1
    hostil: bool = False
    cobre: list = field(default_factory=list)


class Mundo:
    def __init__(self, cli):
        self.cli = cli
        self.seq = 0

    def pc(self, r):
        pc = r.choice(amb.PCS)
        amb.entrar(pc)
        self.atual = pc
        return pc

    def prox(self):
        self.seq += 1
        return self.seq


NOMES = ["Acme", "acme", "ACME ", "Beta Ltda", "beta ltda", "Gama", "Delta", "  Epsilon", "Zeta"]


def _tarefas(incl_excl=True):
    return [t for t in o.linhas("tarefas") if incl_excl or not t["excluido"]]


def _clientes():
    return o.linhas("clientes")


def prazo_legit(r):
    hoje = date.today()
    return r.choice([None, "", "A definir", "garbage", "2025-02-30", "2030-13-01", (hoje + timedelta(days=r.randint(-8, 8))).isoformat(),
                     (hoje + timedelta(days=r.randint(-8, 8))).isoformat() + "T10:00", hoje.isoformat(), " " + hoje.isoformat() + " ",
                     "20260101", "2026-W05-3", "0000-00-00", "9999-12-31"])


def esperado_prazo(p):
    try:
        return date.fromisoformat((p or "").strip()[:10]).isoformat()
    except ValueError:
        return None


# ----------------------------------------------------------------------------- legitimas (banco)

def f_criar_cliente(m, r):
    pc = m.pc(r)
    nome = r.choice(NOMES) + (str(r.randint(0, 3)) if r.random() < 0.3 else "")
    antes = {c["nome"].lower() for c in _clientes()}
    cid = db.criar_cliente(nome)
    cobrar(db.cliente(cid)["nome"].lower() == nome.strip().lower(), "I1-criar_cliente", f"{pc}: nome gravado difere")
    cobrar(len(_clientes()) == len(antes | {nome.strip().lower()}), "I1-criar_cliente", f"{pc}: criar duplicou cliente")
    return f"{pc} cliente {nome!r}"


def f_criar_tarefa(m, r):
    pc = m.pc(r)
    cl = [c for c in _clientes() if not c["excluido"]]
    cid = r.choice(cl)["id"] if cl and r.random() < 0.8 else None
    p = prazo_legit(r)
    tid = db.criar_tarefa(cid, f"tarefa {m.prox()}", r.choice(["", "Joao"]), r.random() < 0.6, p)
    t = next(t for t in _tarefas() if t["id"] == tid)
    cobrar(t["prazo"] == esperado_prazo(p), "I2-criar_tarefa-prazo", f"prazo {p!r} -> {t['prazo']!r}")
    cobrar(any(x["id"] == tid for x in db.tarefas()), "I2-criar_tarefa-nao-aparece", "tarefa nova nao aparece em tarefas()")
    return f"{pc} tarefa prazo={p!r}"


def _escolhe(r, lista):
    return r.choice(lista) if lista else None


def f_status_valido(m, r):
    pc = m.pc(r)
    t = _escolhe(r, _tarefas())
    if not t:
        return "sem tarefa"
    st = r.choice(db.STATUS)
    db.atualizar_tarefa(t["id"], status=st)
    n = next(x for x in _tarefas() if x["id"] == t["id"])
    cobrar(n["status"] == st and (n["concluida_em"] is not None) == (st == "feito"), "I3-status", f"{pc}: status {st}")
    cobrar(n["atualizado_em"] > t["atualizado_em"], "I3-status", "atualizado_em nao avancou")
    return f"{pc} {t['id']}->{st}"


def f_prazo(m, r):
    pc = m.pc(r)
    t = _escolhe(r, _tarefas())
    if not t:
        return "sem tarefa"
    p = prazo_legit(r)
    db.atualizar_tarefa(t["id"], prazo=p)
    n = next(x for x in _tarefas() if x["id"] == t["id"])
    cobrar(n["prazo"] == esperado_prazo(p), "I4-atualizar-prazo", f"{pc}: prazo {p!r} -> {n['prazo']!r}")
    return f"{pc} prazo {p!r}"


def f_excluir_tarefa(m, r):
    pc = m.pc(r)
    t = _escolhe(r, _tarefas(False))
    if not t:
        return "sem tarefa"
    db.excluir_tarefa(t["id"])
    cobrar(all(x["id"] != t["id"] for x in db.tarefas(incluir_feitas=True)), "I5-excluir", "excluida ainda listada")
    return f"{pc} exclui {t['id']}"


def f_renomear(m, r):
    pc = m.pc(r)
    c = _escolhe(r, _clientes())
    if not c:
        return "sem cliente"
    livres = [n for n in NOMES + ["Novo1", "Novo2"] if n.strip().lower() not in {x["nome"].lower() for x in _clientes() if x["id"] != c["id"]}]
    if not livres:
        return "sem nome livre"
    nome = r.choice(livres)
    db.renomear_cliente(c["id"], nome)
    cobrar(db.cliente(c["id"])["nome"] == nome.strip(), "I6-renomear", "nome nao mudou")
    return f"{pc} renomeia {c['nome']!r} -> {nome!r}"


def _ata_falsa(r):
    return amb.analisador.gerar_ata("x", {})


def f_importar_ata(m, r):
    pc = m.pc(r)
    cl = [c for c in _clientes() if not c["excluido"]]
    cid = r.choice(cl)["id"] if cl else None
    ev = f"ev{r.randint(1, 3)}"
    ata = _ata_falsa(r)
    db.importar_acoes_da_ata({"id": ev}, ata, cid)
    novas = [t for t in _tarefas(False) if t["origem_ata"] == ev]
    cobrar(len([t for t in novas if t["status"] != "feito"]) >= len(ata.acoes) or True, "I7", "")
    cobrar(sum(1 for t in novas if t["status"] == "a_fazer" and t["cliente_id"] == cid) >= len(ata.acoes), "I7-importar-ata",
           f"{pc}: nem todas as acoes da ata viraram tarefa")
    return f"{pc} importa {len(ata.acoes)} acoes de {ev}"


def f_mover_ata(m, r):
    pc = m.pc(r)
    cl = [c for c in _clientes() if not c["excluido"]]
    cid = r.choice(cl)["id"] if cl else None
    ev = f"ev{r.randint(1, 3)}"
    db.mover_tarefas_da_ata(ev, cid)
    cobrar(all(t["cliente_id"] == cid for t in _tarefas() if t["origem_ata"] == ev), "I8-mover", "nem tudo moveu")
    return f"{pc} move {ev}"


def f_trocar(m, r):
    o.trocar(m)
    return "A<->B"


def f_remesclar(m, r):
    """Mescla o export do outro PC duas vezes: a segunda nao pode mudar nada."""
    pc = m.pc(r)
    outro = "B" if pc == "A" else "A"
    ex = o._exp(outro)
    o._mesclar(pc, ex, outro)
    antes = o._exp(pc)
    n = o._mesclar(pc, ex, outro)
    cobrar(n == 0 and o._exp(pc) == antes, "S9-mesclar-nao-idempotente", f"{pc}: 2a mesclagem do mesmo export mudou {n}")
    return f"{pc}<-{outro} x2"


# ----------------------------------------------------------------------------- hostis (banco)

def f_status_invalido(m, r):
    pc = m.pc(r)
    t = _escolhe(r, _tarefas())
    if not t:
        return "sem tarefa"
    st = r.choice(["", "FEITO", "done", "a fazer", None, "feito ", "'; drop table tarefas;--", "a_fazer\x00"])
    try:
        db.atualizar_tarefa(t["id"], status=st)
    except Exception as e:
        raise o.Achado("H1-status-invalido-estoura", f"{type(e).__name__}: {e}", f"H1:{type(e).__name__}")
    cobrar(next(x for x in _tarefas() if x["id"] == t["id"]) == t, "H1-status-invalido-mudou-algo", f"status {st!r} alterou a linha")
    return f"{pc} status {st!r}"


def f_atualizar_inexistente(m, r):
    pc = m.pc(r)
    antes = o.linhas("tarefas")
    db.atualizar_tarefa(r.choice([0, -1, 10**6, 2**40]), status="feito", prazo="2030-01-01", descricao="x")
    db.excluir_tarefa(r.choice([0, -1, 10**6]))
    cobrar(o.linhas("tarefas") == antes, "H2-id-inexistente-mudou", "mexer em id inexistente alterou o banco")
    return pc


def f_campos_proibidos(m, r):
    pc = m.pc(r)
    t = _escolhe(r, _tarefas())
    if not t:
        return "sem tarefa"
    db.atualizar_tarefa(t["id"], uuid="x", excluido=r.choice([0, 1]), id=999, criada_em="x", origem_ata="zzz", atualizado_em="0")
    n = next(x for x in _tarefas() if x["id"] == t["id"])
    cobrar(n == t, "H3-campo-proibido", f"campos fora da lista permitida alteraram a linha: {[k for k in n if n[k] != t[k]]}")
    return pc


# ----------------------------------------------------------------------------- rotas

def chamar(m, r, metodo, path, **kw):
    pc = m.atual  # quem chama escolhe o PC ANTES de sortear ids (ids de um PC nao valem no outro)
    try:
        resp = getattr(m.cli, metodo)(path, **kw)
    except amb.RedeBloqueada:
        return pc, types.SimpleNamespace(status_code=0)  # a rota quis um servico externo que o ambiente nao simula
    except Exception as e:
        if any(isinstance(x, amb.RedeBloqueada) for x in (e.__cause__, e.__context__)):
            return pc, types.SimpleNamespace(status_code=0)
        lugar = o.lugar(e)
        rota = path.split("?")[0]
        raise o.Achado("R1-rota-estoura", f"{metodo.upper()} {rota!r}: {type(e).__name__}: {e} [{lugar}]",
                       f"R1:{metodo}:{m_rota(path)}:{type(e).__name__}:{lugar}")
    cobrar(resp.status_code < 500, "R2-rota-5xx", f"{metodo.upper()} {path} -> {resp.status_code}", f"R2:{m_rota(path)}")
    return pc, resp


def m_rota(path):
    partes = path.split("?")[0].split("/")
    return "/".join(("<x>" if (i >= 2 or p.isdigit()) and i > 0 and p else p) for i, p in enumerate(partes))


HUGE = str(2 ** 70)
CLIENTE_FORM = ["", "novo", "abc", "-1", "0", "999999", HUGE, "²", "٣", " 1", "1.5", "1;2"]


def _cliente_form(r):
    cl = _clientes()
    return r.choice([str(c["id"]) for c in cl] + CLIENTE_FORM) if cl else r.choice(CLIENTE_FORM)


def f_rota_tarefa_nova(m, r):
    m.pc(r)
    dados = {"descricao": r.choice(["tarefa", "", "  ", "x" * 5000, "<script>", "ação ✓"]), "responsavel": r.choice(["", "Joao", "y" * 500]),
             "minha": r.choice(["1", "0", "", "talvez"]), "prazo": prazo_legit(r) or "", "cliente_id": r.choice(["", "novo"] + [str(c["id"]) for c in _clientes()]),
             "cliente_novo": r.choice(["", "Acme", "Novo X"])}
    if r.random() < 0.1:
        dados.pop("descricao")
    pc, resp = chamar(m, r, "post", "/tarefas/nova", data=dados)
    return f"{pc} nova {dados.get('descricao', '<ausente>')[:10]!r}"


def f_rota_tarefa_nova_cliente_hostil(m, r):
    m.pc(r)
    dados = {"descricao": "t", "cliente_id": _cliente_form(r)}
    pc, resp = chamar(m, r, "post", "/tarefas/nova", data=dados)
    return f"{pc} cliente_id={dados['cliente_id']!r}"


def f_rota_tarefa_atualizar(m, r):
    m.pc(r)
    ids = [str(t["id"]) for t in _tarefas()]
    tid = r.choice(ids + ["0", "999999", HUGE]) if ids else r.choice(["0", "999999", HUGE])
    dados = r.choice([{"status": r.choice(["feito", "fazendo", "a_fazer", "x", ""])}, {"prazo": prazo_legit(r) or ""}, {"excluir": "1"},
                      {}, {"status": "feito", "prazo": "lixo"}, {"excluir": ""}])
    pc, resp = chamar(m, r, "post", f"/tarefas/{tid}", data=dados)
    return f"{pc} {tid} {dados}"


def f_rota_cliente_novo(m, r):
    m.pc(r)
    nome = r.choice(["Acme", "acme", "", "   ", "N" * 3000, "Novo Y", "ção", "a'b", "<b>"])
    pc, resp = chamar(m, r, "post", "/clientes/novo", data={"nome": nome})
    return f"{pc} cliente {nome[:10]!r}"


def f_rota_cliente_renomear(m, r):
    m.pc(r)
    cl = _clientes()
    cid = r.choice([str(c["id"]) for c in cl] + ["999999", HUGE]) if cl else "999999"
    nome = r.choice([c["nome"] for c in cl] + ["Acme", "ACME", "Outro", "", "  "]) if cl else "Outro"
    pc, resp = chamar(m, r, "post", f"/clientes/{cid}", data={"nome": nome})
    return f"{pc} {cid} -> {nome!r}"


def f_rota_tarefas_get(m, r):
    m.pc(r)
    q = {}
    for k, vals in (("ver", ["minhas", "todas", "x", ""]), ("feitas", ["1", "0", "x"]),
                    ("cliente", ["", "abc", "1", "999999", HUGE, "-5"]), ("nova_ata", ["", "ev1", "../x", "nul", "con", "ev999", "a" * 300])):
        if r.random() < 0.5:
            q[k] = r.choice(vals)
    pc, resp = chamar(m, r, "get", "/tarefas", query_string=q)
    return f"{pc} GET /tarefas {q}"


def f_rota_inicio(m, r):
    m.pc(r)
    sem = r.choice(["0", "1", "-1", "-52", "x", "", "99999", "-99999", "10000000", HUGE])
    pc, resp = chamar(m, r, "get", "/", query_string={"semana": sem})
    return f"{pc} GET / semana={sem}"


def f_rota_get_simples(m, r):
    m.pc(r)
    path = r.choice(["/gravacao", "/api/aovivo", "/ata/inexistente", "/ata/ev1", "/resumo/ev1", "/resumo/ev3", "/resumo/xx", "/manual/ev1",
                     "/manual/avulsa", "/manual/xx", "/resumo/ev1?cliente=abc", "/resumo/ev1?cliente=999999", f"/resumo/ev1?cliente={HUGE}"])
    cl = _clientes()
    if path == "/resumo/ev1" and cl:
        path += f"?cliente={r.choice(cl)['id']}"
    pc, resp = chamar(m, r, "get", path)
    return f"{pc} GET {path}"


def f_rota_manual_gerar(m, r):
    m.pc(r)
    cl = _clientes()
    ev = r.choice(["ev1", "ev2", "avulsa", "ev1"])
    texto = r.choice(["texto da reuniao", "", "  ", "ç" * 100])
    dados = {"texto": texto, "titulo": r.choice(["", "T"])}
    pc, resp = chamar(m, r, "post", f"/manual/{ev}", data=dados)
    if texto.strip() and resp.status_code == 302:
        cobrar(any(amb.config.ATAS_DIR.glob("*.json")), "I9-ata-nao-gravada", f"{pc}: gerar ata nao gravou arquivo")
    return f"{pc} manual {ev} {texto[:8]!r}"


def f_rota_ata_cliente(m, r):
    m.pc(r)
    ids = [p.stem for p in amb.config.ATAS_DIR.glob("*.json")]
    ev = r.choice(ids + ["nao-existe"]) if ids else "nao-existe"
    dados = {"cliente_id": _cliente_form(r), "cliente_novo": r.choice(["", "Zeta", "Cliente Z"])}
    pc, resp = chamar(m, r, "post", f"/ata/{ev}/cliente", data=dados)
    return f"{pc} {ev} {dados['cliente_id']!r}"


def f_rota_ata_email_followup(m, r):
    m.pc(r)
    ids = [p.stem for p in amb.config.ATAS_DIR.glob("*.json")]
    ev = r.choice(ids + ["nao-existe"]) if ids else "nao-existe"
    if r.random() < 0.5:
        dados = {"emails": r.choice([[], ["a@x.com"], [""]]), "extras": r.choice(["", " , ,", "b@x.com, c@x.com", "sem-arroba"])}
        pc, resp = chamar(m, r, "post", f"/ata/{ev}/email", data=dados)
    else:
        dados = {"titulo": r.choice(["F", ""]), "inicio": r.choice(["2030-01-01T10:00", "", "lixo", "2030-02-30T10:00"]),
                 "duracao": r.choice(["30", "x", "", "-1", HUGE])}
        if r.random() < 0.15:
            dados.pop("inicio")
        pc, resp = chamar(m, r, "post", f"/ata/{ev}/followup", data=dados)
    return f"{pc} {ev} {dados}"


def f_rota_api_cliente(m, r):
    m.pc(r)
    corpo = r.choice([{}, {"cliente_id": 1}, {"novo": "Cli API"}, {"novo": ""}, {"novo": 5}, {"novo": None}, [], [1], "x", 5, {"cliente_id": "abc"},
                      {"cliente_id": HUGE}, {"cliente_id": 2 ** 70}, {"novo": "  "}])
    pc, resp = chamar(m, r, "post", "/api/cliente", json=corpo)
    return f"{pc} /api/cliente {corpo!r}"


def f_rota_sincronizar_etc(m, r):
    m.pc(r)
    path = r.choice(["/sincronizar", "/parar", "/api/sugerir"])
    pc, resp = chamar(m, r, "post", path)
    return f"{pc} POST {path}"


# --- gravacoes pendentes

PIDS_HOSTIS = ["..", "...", "..\\..\\dados", "%2e%2e", "x%2Fy", "x%5Cy", "con", "nul", "aux", "a" * 400, "%00", "x y", "ç", "?", "%3F", "..%2F..%2Fdados.db"]


def f_pendente_chega(m, r):
    pc = m.pc(r)
    pid = f"reuniao-2030{m.prox():04d}"
    dados = {"reuniao": {"id": r.choice(["ev1", "ev2", f"avulsa-{m.prox()}"]), "titulo": "Reuniao", "inicio": "2030-01-01T10:00",
                         "cliente_id": r.choice([None, 1, 999])},
             "arquivos": {"voce": pid + "-voce.wav", "outros": pid + "-outros.wav"},
             "transcricao_ao_vivo": r.choice(["", "texto ao vivo"]), "minutos": 5, "encerrada_em": "2030-01-01T10:05",
             "atualizado_em": db.agora()}
    (amb.config.GRAVACOES_DIR / f"{pid}.json").write_text(json.dumps(dados), encoding="utf-8")
    return f"{pc} pendente {pid}"


def _pids():
    return [p.stem for p in amb.config.GRAVACOES_DIR.glob("*.json")]


def f_rota_pendente(m, r):
    m.pc(r)
    pids = _pids()
    pid = r.choice(pids + PIDS_HOSTIS) if pids else r.choice(PIDS_HOSTIS)
    acao = r.choice(["", "/gerar", "/descartar"])
    dados = {"cliente_id": _cliente_form(r)} if acao == "/gerar" else None
    if acao:
        pc, resp = chamar(m, r, "post", f"/pendente/{pid}{acao}", data=dados)
    else:
        pc, resp = chamar(m, r, "get", f"/pendente/{pid}")
    if pid in pids and acao == "/descartar":
        cobrar(amb.reunioes.carregar_pendente(pid) is None, "I10-descartar", "pendente descartada continua pendente")
    return f"{pc} {acao or 'GET'} /pendente/{pid[:20]}"


def f_arquivo_corrompido(m, r):
    """Download interrompido: um .json cortado cai em gravacoes/ ou atas/. A tela inicial tem de sobreviver."""
    pc = m.pc(r)
    onde = r.choice([amb.config.GRAVACOES_DIR, amb.config.ATAS_DIR])
    arq = onde / f"quebrado-{m.prox()}.json"
    arq.write_text(r.choice(['{"reuniao": {"id": "e', "", "[]", "null"]), encoding="utf-8")
    try:
        chamar(m, r, "get", "/")
        chamar(m, r, "get", "/tarefas")
    finally:
        arq.unlink(missing_ok=True)
    return f"{pc} {onde.name}/{arq.name}"


def f_dados_remotos_hostis(m, r):
    """mesclar() recebe um dados.json de outro PC com valores fora do padrao (versao antiga, edicao manual)."""
    pc = m.pc(r)
    rem = o._exp("B" if pc == "A" else "A")
    cl = rem["clientes"]
    base = {"uuid": f"x{m.prox():031d}", "cliente_uuid": None, "descricao": "d", "responsavel": "", "minha": 1, "prazo": r.choice(["A definir", "", None, "2030-01-01"]),
            "status": r.choice(["a_fazer", "feito"]), "origem_ata": None, "origem_titulo": None, "criada_em": "2030-01-01", "concluida_em": None,
            "atualizado_em": db.agora(), "excluido": 0}
    if base["status"] == "feito":
        base["concluida_em"] = "2030-01-02"
    rem = {"clientes": cl, "tarefas": rem["tarefas"] + [base]}
    o._mesclar(pc, rem, "remoto")
    try:
        try:
            db.tarefas()
        except Exception as e:
            raise o.Achado("H4-prazo-remoto-quebra-tarefas", f"tarefas() depois de mesclar prazo {base['prazo']!r}: {type(e).__name__}: {e}", "H4")
        gravado = next(t for t in o.linhas("tarefas") if t["uuid"] == base["uuid"])["prazo"]
        cobrar(gravado == esperado_prazo(base["prazo"]), "H4b-mesclar-grava-prazo-cru",
               f"mesclar gravou prazo {gravado!r} vindo de fora (criar_tarefa/atualizar_tarefa normalizam para NULL)", "H4b")
    finally:  # nao deixa o dado hostil contaminar o resto do passeio
        with db.conexao() as con:
            con.execute("DELETE FROM tarefas WHERE uuid = ?", (base["uuid"],))
    return f"{pc} <- remoto hostil prazo={base['prazo']!r}"


# --- exclusoes (commit 36d64f5: excluir ata, ocultar evento, descartar gravação, excluidos.json)

def f_excluir_tarefas_da_ata(m, r):
    pc = m.pc(r)
    ev = f"ev{r.randint(1, 3)}"
    ts = _tarefas()
    abertas = [t for t in ts if t["origem_ata"] == ev and t["status"] != "feito" and not t["excluido"]]
    outras = {t["id"]: t for t in ts if t not in abertas}
    n = db.excluir_tarefas_da_ata(ev)
    cobrar(n == len(abertas), "I11-excluir_tarefas_da_ata", f"{pc}: devolveu {n}, havia {len(abertas)} abertas")
    depois = {t["id"]: t for t in _tarefas()}
    cobrar(all(depois[i] == t for i, t in outras.items()), "I11-excluir_tarefas_da_ata", "mexeu em tarefa que nao era aberta desta ata")
    cobrar(all(depois[t["id"]]["excluido"] == 1 for t in abertas), "I11-excluir_tarefas_da_ata", "aberta nao ficou excluida")
    return f"{pc} {ev}: {n}"


def f_rota_ata_excluir(m, r):
    pc = m.pc(r)
    ids = [p.stem for p in amb.config.ATAS_DIR.glob("*.json")]
    ev = r.choice(ids + ["nao-existe", "nul", "..", "x.y"]) if ids else r.choice(["nao-existe", "nul", ".."])
    if r.random() < 0.3:
        pc, resp = chamar(m, r, "get", f"/ata/{ev}/excluir")
        return f"{pc} GET excluir {ev}"
    com = r.random() < 0.5
    existia = ev in ids
    abertas = [t["id"] for t in _tarefas(False) if t["origem_ata"] == ev and t["status"] != "feito"]
    feitas = [t["id"] for t in _tarefas(False) if t["origem_ata"] == ev and t["status"] == "feito"]
    pc, resp = chamar(m, r, "post", f"/ata/{ev}/excluir", data={"tarefas": "1"} if com else {})
    if existia:
        cobrar(amb.atas.carregar(ev) is None, "I12-ata-nao-excluida", f"{pc}: ata {ev} continua depois de excluir")
        cobrar(f"ata__{ev}.json" in amb.sincronia.excluidos(), "I12-excluidos.json", "a exclusao nao foi registrada para os outros PCs")
        vivos = {t["id"] for t in _tarefas(False)}
        cobrar(all(i not in vivos for i in abertas) if com else all(i in vivos for i in abertas), "I12-tarefas-da-ata", f"{pc}: com_tarefas={com}")
        cobrar(all(i in vivos for i in feitas), "I12-tarefas-da-ata", "tarefa feita foi excluida junto")
    return f"{pc} POST excluir {ev} com_tarefas={com}"


def f_rota_evento_ocultar(m, r):
    pc = m.pc(r)
    ev = r.choice(["ev1", "ev2", "nao-existe", "x y", "a" * 300, "..", "ç", "%2e%2e"])
    pc, resp = chamar(m, r, "post", f"/evento/{ev}/ocultar")
    return f"{pc} ocultar {ev[:20]}"


def f_apagar_local_hostil(m, r):
    """excluidos.json vem do Drive. Nome com '..' nao pode apagar fora de atas/ e gravacoes/."""
    pc = m.pc(r)
    base = amb.RUN["dir"] / pc
    alvo = base / "fora.json"
    alvo.write_text("{}", encoding="utf-8")
    nome = r.choice(["ata__../fora.json", "grav__../fora.json", "ata__..\fora.json", "grav__..\fora.json"])
    try:
        amb.sincronia._apagar_local(nome)
        cobrar(alvo.exists(), "H5-apagar_local-fora-da-pasta", f"_apagar_local({nome!r}) apagou um arquivo FORA de atas/gravacoes", "H5")
    finally:
        alvo.unlink(missing_ok=True)
    return f"{pc} {nome}"


# --- fuzz generico: sorteia QUALQUER rota do url_map (acompanha o sistema quando ele ganha rota)

SEM_FUZZ = ("/gravar/", "/api/janela", "/static")
VALORES = ["", " ", "x", "1", "2", "3", "0", "-1", "999999", HUGE, "²", "lixo", "2030-01-01", "2030-02-30", "a@b.com", "novo",
           "a" * 3000, "<b>", "ev1", "..", "feito", "a_fazer", "sim", "talvez"]
CAMPOS = ["nome", "texto", "descricao", "responsavel", "cliente_id", "cliente_novo", "prazo", "status", "pergunta", "para", "assunto",
          "corpo", "cliente", "excluir", "tarefas", "emails", "extras", "titulo", "inicio", "duracao", "minha", "nota", "semana", "ver"]


def _valor_url(r, conv):
    if conv in ("int", "integer"):
        ids = [str(c["id"]) for c in _clientes()] + [str(t["id"]) for t in _tarefas()]
        return r.choice(ids + ["0", "1", "999999", HUGE])
    return r.choice(["ev1", "ev2", "nao-existe", "x y", "nul", "ç", "a" * 100, "avulsa"] + [p.stem for p in amb.config.ATAS_DIR.glob("*.json")])


def f_rota_generica(m, r):
    m.pc(r)
    regras = sorted((x for x in amb.reunioes.app.url_map.iter_rules()
                     if x.endpoint != "static" and not any(x.rule.startswith(p) for p in SEM_FUZZ)), key=lambda x: x.rule)
    regra = r.choice(regras)
    path = re.sub(r"<(?:(\w+)(?:\([^>]*\))?:)?(\w+)>", lambda mo: _valor_url(r, mo.group(1) or "string"), regra.rule)
    metodo = r.choice(sorted(regra.methods - {"HEAD", "OPTIONS"})).lower()
    dados = {k: r.choice(VALORES) for k in r.sample(CAMPOS, r.randint(0, 5))}
    if metodo == "get":
        pc, resp = chamar(m, r, "get", path, query_string=dados)
    elif r.random() < 0.15:
        pc, resp = chamar(m, r, metodo, path, json=r.choice([{}, [], [1], "x", dados, {"cliente_id": HUGE}]))
    else:
        pc, resp = chamar(m, r, metodo, path, data=dados)
    return f"{pc} {metodo.upper()} {regra.rule} -> {path[:40]} {sorted(dados)}"


# --- anotacoes por cliente (so existem na arvore de trabalho mais nova)

def f_criar_nota(m, r):
    pc = m.pc(r)
    if not hasattr(db, "criar_nota"):
        return "sem notas neste alvo"
    cl = [c for c in _clientes() if not c["excluido"]]
    texto = r.choice(["nota", "  nota  ", "", "   ", "x" * 500])
    cid = r.choice([c["id"] for c in cl]) if cl else None
    nid = db.criar_nota(cid, texto)
    if cid and texto.strip():
        cobrar(nid and any(n["id"] == nid for n in db.notas(cid)), "I13-criar_nota", f"{pc}: nota nao aparece em notas()")
    else:
        cobrar(nid is None, "I13-criar_nota", f"{pc}: aceitou nota invalida (cliente={cid}, texto={texto!r})")
    return f"{pc} nota {texto[:8]!r}"


def f_excluir_nota(m, r):
    pc = m.pc(r)
    if not hasattr(db, "excluir_nota"):
        return "sem notas neste alvo"
    ns = o.linhas("notas")
    if not ns:
        return "sem nota"
    n = r.choice(ns)
    db.excluir_nota(n["id"])
    cobrar(all(x["id"] != n["id"] for x in db.notas(n["cliente_id"])), "I14-excluir_nota", "nota excluida continua listada")
    return f"{pc} exclui nota {n['id']}"


ACOES = [
    Acao("criar cliente", f_criar_cliente, 3, cobre=["db.criar_cliente"]),
    Acao("criar tarefa", f_criar_tarefa, 6, cobre=["db.criar_tarefa", "db.tarefas"]),
    Acao("mudar status", f_status_valido, 4, cobre=["db.atualizar_tarefa"]),
    Acao("mudar prazo", f_prazo, 3, cobre=["db.atualizar_tarefa"]),
    Acao("excluir tarefa", f_excluir_tarefa, 3, cobre=["db.excluir_tarefa"]),
    Acao("renomear cliente", f_renomear, 2, cobre=["db.renomear_cliente"]),
    Acao("importar acoes da ata", f_importar_ata, 2, cobre=["db.importar_acoes_da_ata"]),
    Acao("mover tarefas da ata", f_mover_ata, 1, cobre=["db.mover_tarefas_da_ata"]),
    Acao("trocar A<->B", f_trocar, 5, cobre=["db.mesclar", "db.exportar"]),
    Acao("remesclar", f_remesclar, 2, cobre=["db.mesclar"]),
    Acao("status invalido", f_status_invalido, 2, True, ["db.atualizar_tarefa"]),
    Acao("id inexistente", f_atualizar_inexistente, 1, True, ["db.atualizar_tarefa", "db.excluir_tarefa"]),
    Acao("campos proibidos", f_campos_proibidos, 1, True, ["db.atualizar_tarefa"]),
    Acao("dados remotos hostis", f_dados_remotos_hostis, 1, True, ["db.mesclar"]),
    Acao("POST /tarefas/nova", f_rota_tarefa_nova, 3, True, ["/tarefas/nova"]),
    Acao("POST /tarefas/nova cliente hostil", f_rota_tarefa_nova_cliente_hostil, 3, True, ["/tarefas/nova", "cliente_do_formulario"]),
    Acao("POST /tarefas/<id>", f_rota_tarefa_atualizar, 4, True, ["/tarefas/<int:tarefa_id>"]),
    Acao("POST /clientes/novo", f_rota_cliente_novo, 2, True, ["/clientes/novo"]),
    Acao("POST /clientes/<id>", f_rota_cliente_renomear, 3, True, ["/clientes/<int:cliente_id>"]),
    Acao("GET /tarefas", f_rota_tarefas_get, 3, True, ["/tarefas"]),
    Acao("GET /", f_rota_inicio, 3, True, ["/"]),
    Acao("GETs simples", f_rota_get_simples, 3, True, ["/gravacao", "/api/aovivo", "/ata/<event_id>", "/resumo/<event_id>", "/manual/<event_id>"]),
    Acao("POST /manual (gerar ata)", f_rota_manual_gerar, 3, False, ["/manual/<event_id>", "reunioes.processar"]),
    Acao("POST /ata/<id>/cliente", f_rota_ata_cliente, 2, True, ["/ata/<event_id>/cliente"]),
    Acao("POST /ata/<id>/email|followup", f_rota_ata_email_followup, 2, True, ["/ata/<event_id>/email", "/ata/<event_id>/followup"]),
    Acao("POST /api/cliente", f_rota_api_cliente, 2, True, ["/api/cliente"]),
    Acao("POST /sincronizar|/parar|/api/sugerir", f_rota_sincronizar_etc, 1, True, ["/sincronizar", "/parar", "/api/sugerir"]),
    Acao("pendente chega", f_pendente_chega, 2, False, ["gravacoes/*.json"]),
    Acao("rotas /pendente", f_rota_pendente, 4, True, ["/pendente/<pid>", "/pendente/<pid>/gerar", "/pendente/<pid>/descartar", "reunioes.excluir_arquivos_da_gravacao"]),
    Acao("arquivo corrompido chega", f_arquivo_corrompido, 1, True, ["/", "/tarefas"]),
    Acao("excluir tarefas da ata", f_excluir_tarefas_da_ata, 2, False, ["db.excluir_tarefas_da_ata"]),
    Acao("POST/GET /ata/<id>/excluir", f_rota_ata_excluir, 3, True, ["/ata/<event_id>/excluir", "reunioes.excluir_ata"]),
    Acao("POST /evento/<id>/ocultar", f_rota_evento_ocultar, 1, True, ["/evento/<event_id>/ocultar"]),
    Acao("excluidos.json hostil", f_apagar_local_hostil, 1, True, ["sincronia._apagar_local"]),
    Acao("fuzz de rota qualquer", f_rota_generica, 6, True, ["*"]),
    Acao("criar nota", f_criar_nota, 2, False, ["db.criar_nota", "db.notas"]),
    Acao("excluir nota", f_excluir_nota, 1, False, ["db.excluir_nota"]),
]

# o que existe no sistema e este catalogo AINDA NAO sabe testar (e por que)
SEM_ACAO = {
    "/gravar/<event_id>": "dispara gravacao de audio real",
    "/api/janela/abrir": "abre janela tkinter (subprocess)",
    "/api/janela/fechada": "so troca uma flag; sem estado de negocio",
    "/gerar/<event_id>": "so envolve Google/transcricao (falsos); coberto indiretamente por /manual",
    "sincronia._sincronizar": "depende do Drive; o lado testavel (db.mesclar) e coberto por 'trocar A<->B'",
    "lembretes.*": "notificacoes/relogio; resumo_cliente e coberto por /resumo",
    "gravador.*, monitor.*, assistente.*, janela.*": "audio, janelas, Whisper",
    "ia.*, analisador.gerar_ata": "chama o Claude (substituido por um falso)",
    "google_services.*": "rede (bloqueado no ambiente)",
    "excluir cliente": "nao existe funcao; so chega via mesclar()",
}
