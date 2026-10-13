"""Catalogo v2 = catalogo v1 (acoes.py, CONGELADO) + o que cobra as mudancas de clientes/sincronizacao.

Por que um arquivo a parte: a semente NAO atravessa mudanca de gerador, e o carimbo de v1 e o hash de acoes.py.
Acrescentar acao la mudaria o passeio de todas as sementes guardadas. Aqui a lista e outra (ACOES2), entao as
sementes antigas continuam sendo o mesmo passeio (catalogo "v1" no JSON) e as novas rodam em "v2".
"""
import shutil
import types

import acoes as A1
import ambiente as amb
import db
import oraculo as o
from acoes import Acao, HUGE, _clientes, _cliente_form, chamar
from oraculo import cobrar

# e-mails: o que o usuario digita (lista/virgula/ponto e virgula/maiuscula/duplicado/dominio) e lixo
EMAILS_OK = ["", "ana@acme.com", "Ana@Acme.COM", "@acme.com", "bob@beta.com, @BETA.com", "a@b.com; c@d.com\ne@f.com",
             "ana@acme.com, ana@acme.com", "  x@y.com  ", "z@zeta.com", "@gama.com, d@delta.com"]
EMAILS_LIXO = [None, 5, "sem-arroba", ",,;;", "a@", "@", ["a@b.com"], "ç@ã.com", "a@b.com,", HUGE]
CONSULTAS = ["ana@acme.com", "ANA@ACME.COM", " ana@acme.com ", "x@acme.com", "@acme.com", "bob@beta.com", "q@beta.com", "z@zeta.com",
             "y@gama.com", "d@delta.com", "nada@nada.com", "", None, 5, "sem-arroba", "@", "a@b.com", "c@d.com", "e@f.com", "x@y.com"]


def _vivos():
    return [c for c in _clientes() if not c["excluido"]]


def _lista(em):
    return [e for e in (em or "").split(", ") if e]


# ----------------------------------------------------------------------------- emails e clientes (legitimas)

def f_atualizar_cliente(m, r):
    pc = m.pc(r)
    cl = _clientes()
    if not cl:
        return "sem cliente"
    c = r.choice(cl)
    outros = {x["nome"].lower() for x in cl if x["id"] != c["id"]}
    livres = [n for n in A1.NOMES + ["Novo1", "Novo2"] if n.strip().lower() not in outros]
    nome = r.choice([c["nome"]] + livres)
    em = r.choice(EMAILS_OK + (EMAILS_LIXO if r.random() < 0.3 else []))
    db.atualizar_cliente(c["id"], nome, em)
    n = db.cliente(c["id"])
    cobrar(n["nome"] == nome.strip(), "I15-atualizar_cliente-nome", f"{pc}: nome {nome!r} -> {n['nome']!r}")
    cobrar(n["atualizado_em"] > c["atualizado_em"], "I15-atualizar_cliente-relogio", f"{pc}: atualizado_em nao avancou (sincronia nao leva a mudanca)")
    if em in EMAILS_OK:  # e-mail digitado direito tem de ser achado depois
        for e in [x.strip().lower() for x in em.replace(";", ",").replace("\n", ",").split(",") if "@" in x]:
            got = db.cliente_por_email(e)
            cobrar(got is not None, "I15-atualizar_cliente-email-nao-achado", f"{pc}: {e!r} gravado e cliente_por_email -> None")
    return f"{pc} atualiza {c['id']} {nome!r} emails={em!r}"


def f_criar_cliente_emails(m, r):
    pc = m.pc(r)
    nome = r.choice(A1.NOMES)
    em = r.choice(EMAILS_OK)
    existia = any(c["nome"].lower() == nome.strip().lower() for c in _clientes())
    cid = db.criar_cliente(nome, em)
    c = db.cliente(cid)
    cobrar(c is not None and c["nome"].lower() == nome.strip().lower(), "I16-criar_cliente-emails", f"{pc}: nome")
    if not existia:
        cobrar(c["emails"] == db._limpar_emails(em) if hasattr(db, "_limpar_emails") else True, "I16-criar_cliente-emails", f"{pc}: emails {em!r} -> {c['emails']!r}")
    return f"{pc} cria {nome!r} emails={em!r} existia={existia}"


def f_cliente_por_email(m, r):
    """Contra a forca bruta: o resultado e um cliente vivo que tem o e-mail ou o dominio; e so e None se nenhum tem."""
    pc = m.pc(r)
    cl = _vivos()
    for c in _clientes():  # cliente_por_uuid: o uuid de um cliente acha o cliente; o de um apelido acha quem ficou
        cobrar((db.cliente_por_uuid(c["uuid"]) or {}).get("id") == c["id"], "I20-cliente_por_uuid", f"{pc}: uuid {c['uuid'][:6]}")
    for a in o.linhas("clientes_alias"):
        dest = db.cliente_por_uuid(a["uuid"])
        cobrar(dest is not None and dest["uuid"] == o.canonico_local(a["uuid"]), "I20-cliente_por_uuid-apelido", f"{pc}: apelido {a['uuid'][:6]} -> {dest and dest['uuid'][:6]}")
    q = r.choice(CONSULTAS + [e for c in cl for e in _lista(c["emails"])])
    got = db.cliente_por_email(q)
    s = str(q or "").strip().lower()
    if "@" not in s:
        cobrar(got is None, "I17-cliente_por_email-sem-arroba", f"{pc}: {q!r} -> {got}")
        return f"{pc} {q!r}"
    dom = "@" + s.split("@", 1)[1]
    donos = [c["id"] for c in cl if s in _lista(c["emails"]) or dom in _lista(c["emails"])]
    cobrar((got in donos) if donos else got is None, "I17-cliente_por_email-errado", f"{pc}: {q!r} -> {got}, donos={donos}")
    return f"{pc} {q!r} -> {got}"


def f_renomear_para_nome_do_outro(m, r):
    """Renomear para um nome que SO existe no outro PC: legitimo aqui, e e o que obriga o mesclar a unir."""
    pc = m.pc(r)
    outro = "B" if pc == "A" else "A"
    amb.entrar(outro)
    nomes_outro = [c["nome"] for c in _clientes()]
    amb.entrar(pc)
    cl = _clientes()
    aqui = {c["nome"].lower() for c in cl}
    alvos = [n for n in nomes_outro if n.lower() not in aqui]
    if not cl or not alvos:
        return "nada a fazer"
    c = r.choice(cl)
    nome = r.choice(alvos)
    db.renomear_cliente(c["id"], nome)
    cobrar(db.cliente(c["id"])["nome"] == nome.strip(), "I6-renomear", "nome nao mudou")
    return f"{pc} renomeia {c['nome']!r} -> {nome!r} (nome do outro PC)"


def f_cenario_uniao(m, r):
    """Cada PC cria/renomeia clientes (nomes que colidem entre os PCs, as vezes so na caixa), com tarefas, notas e e-mails;
    troca; muda de novo; troca de novo -> uniao, apelidos encadeados, tarefas/notas no cliente que fica."""
    pool = ["P", "p", "Q", "R", "r"]
    for _rodada in range(r.randint(1, 3)):
        for pc in r.sample(amb.PCS, 2):
            amb.entrar(pc)
            for _ in range(r.randint(0, 3)):
                cid = db.criar_cliente(r.choice(pool), r.choice(EMAILS_OK))
                if r.random() < 0.7:
                    db.criar_tarefa(cid, f"cen {m.prox()}", "", True, None)
                if r.random() < 0.7:
                    db.criar_nota(cid, f"nota {m.prox()}")
            for _ in range(r.randint(0, 2)):
                cl = _clientes()
                if cl:
                    c = r.choice(cl)
                    outros = {x["nome"].lower() for x in cl if x["id"] != c["id"]}
                    livres = [n for n in pool if n.lower() not in outros]
                    if livres:
                        db.renomear_cliente(c["id"], r.choice(livres))
            for _ in range(r.randint(0, 2)):
                cl = _clientes()
                if cl:
                    c = r.choice(cl)
                    db.atualizar_cliente(c["id"], c["nome"], r.choice(EMAILS_OK))
        o.trocar(m)
    return "cenario de uniao"


# ----------------------------------------------------------------------------- sincronizacao (propriedades de insercao)

def _pc_tmp(nome, copiar_de=None):
    d = amb.RUN["dir"] / nome
    shutil.rmtree(d, ignore_errors=True)
    (d / "atas").mkdir(parents=True)
    (d / "gravacoes").mkdir(parents=True)
    amb.entrar(nome)
    if copiar_de:
        shutil.copy(amb.RUN["dir"] / copiar_de / "dados.db", d / "dados.db")
    db.criar_tabelas()


def _apagar_tmp(*nomes):
    amb.entrar("A")
    for n in nomes:
        shutil.rmtree(amb.RUN["dir"] / n, ignore_errors=True)
        amb.SUJOS.discard(n)


def f_ordem_do_mesclar(m, r):
    """Comutatividade: a partir de um banco vazio, mesclar(A) depois mesclar(B) tem de dar o mesmo que o contrario."""
    ea, eb = o._exp("A"), o._exp("B")
    res = {}
    try:
        for nome, ordem in (("T1", (ea, eb)), ("T2", (eb, ea))):
            _pc_tmp(nome)
            for e in ordem:
                o._mesclar(nome, e, "x")
            res[nome] = o._exp(nome)
        n1, n2 = o._norm(res["T1"]), o._norm(res["T2"])
        cobrar(n1 == n2, "S16-mesclar-depende-da-ordem", "mesclar(A);mesclar(B) != mesclar(B);mesclar(A): " + o._dif(n1, n2),
               "S16-mesclar-depende-da-ordem:" + o._dif_tipo(n1, n2))
    finally:
        _apagar_tmp("T1", "T2")
        amb.entrar("A")
    return "T1/T2"


def f_exportacao_antiga(m, r):
    """Foto do export de um PC; mais tarde, reaplicar a foto antiga NELE MESMO nao pode mudar nada (nada la e mais novo)."""
    pc = m.pc(r)
    fotos = m.__dict__.setdefault("fotos", {})
    if pc not in fotos or r.random() < 0.3:
        fotos[pc] = db.exportar()
        return f"{pc} foto"
    antes = db.exportar()
    n = o._mesclar(pc, fotos[pc], pc)
    cobrar(n == 0 and db.exportar() == antes, "S15-export-antigo-mexe", f"{pc}: reaplicar export antigo do proprio PC mudou {n} registro(s): " + o._dif(o._norm(antes), o._norm(db.exportar())),
           "S15-export-antigo-mexe")
    return f"{pc} reaplica foto antiga"


def _hostil_remoto(m, r, base):
    """Variacoes de um export que um PC desatualizado ou editado a mao poderia mandar."""
    e = {k: [dict(x) for x in v] for k, v in base.items()}
    cl, tr, nt = e["clientes"], e["tarefas"], e["notas"]
    ap = e.setdefault("apelidos", [])
    uu = [c["uuid"] for c in cl] or ["u" * 32]
    soltos = f"s{m.prox():031d}"

    def novo_cliente(**k):
        base_c = {"uuid": soltos, "nome": "Hostil", "emails": "", "criado_em": "2030-01-01", "atualizado_em": db.agora(), "excluido": 0}
        base_c.update(k)
        cl.append(base_c)

    muts = {
        "nome None": lambda: novo_cliente(nome=None), "nome vazio": lambda: novo_cliente(nome="  "), "nome numero": lambda: novo_cliente(nome=5),
        "sem uuid": lambda: novo_cliente(uuid=None), "emails lista": lambda: novo_cliente(emails=["a@b.com"]), "emails None": lambda: novo_cliente(emails=None),
        "emails sujo": lambda: novo_cliente(emails="A@B.com;;;a@b.com\n@C.com"), "sem atualizado_em": lambda: novo_cliente(atualizado_em=None),
        "excluido texto": lambda: novo_cliente(excluido="x"), "sem criado_em": lambda: novo_cliente(criado_em=None),
        "mesmo nome outra caixa": lambda: novo_cliente(nome=(cl[0]["nome"].upper() if cl else "HOSTIL")),
        "dois com o mesmo nome": lambda: (novo_cliente(), novo_cliente(uuid=soltos[:-1] + "z", atualizado_em=db.agora())),
        "uuid duplicado": lambda: cl.append(dict(cl[0])) if cl else None,
        "apelido para si": lambda: ap.append({"uuid": uu[0], "para": uu[0]}),
        "apelido ciclo": lambda: ap.extend([{"uuid": soltos, "para": uu[0]}, {"uuid": uu[0], "para": soltos}]),
        "apelido de cliente vivo": lambda: ap.append({"uuid": uu[0], "para": uu[-1]}),
        "apelido para o nada": lambda: ap.append({"uuid": uu[0], "para": "nada" * 8}),
        "apelido do nada para cliente": lambda: ap.append({"uuid": soltos, "para": uu[0]}),
        "apelidos None": lambda: e.__setitem__("apelidos", None), "apelidos dict": lambda: e.__setitem__("apelidos", {"a": 1}),
        "apelido sem para": lambda: ap.append({"uuid": soltos}), "apelido string": lambda: ap.append("x"),
        "tarefa cliente desconhecido": lambda: tr.append({**(tr[0] if tr else {}), "uuid": soltos + "t", "cliente_uuid": "nada" * 8}) if tr else None,
        "tarefa feita sem concluida_em": lambda: tr.append({**(tr[0] if tr else {}), "uuid": soltos + "t", "status": "feito", "concluida_em": None, "atualizado_em": db.agora()}) if tr else None,
        "tarefa status invalido com concluida_em": lambda: tr.append({**(tr[0] if tr else {}), "uuid": soltos + "t", "status": "xx", "concluida_em": "2030-01-02", "atualizado_em": db.agora()}) if tr else None,
        "tarefa prazo lixo": lambda: tr.append({**(tr[0] if tr else {}), "uuid": soltos + "t", "prazo": "lixo", "atualizado_em": db.agora()}) if tr else None,
        "tarefa descricao None": lambda: tr.append({**(tr[0] if tr else {}), "uuid": soltos + "t", "descricao": None, "atualizado_em": db.agora()}) if tr else None,
        "tarefa sem campos": lambda: tr.append({"uuid": soltos + "t"}),
        "tarefa string": lambda: tr.append("x"),
        "nota cliente desconhecido": lambda: nt.append({"uuid": soltos + "n", "cliente_uuid": "nada" * 8, "texto": "n", "criado_em": "x", "atualizado_em": db.agora(), "excluido": 0}),
        "nota texto None": lambda: nt.append({"uuid": soltos + "n", "cliente_uuid": uu[0], "texto": None, "criado_em": None, "atualizado_em": None, "excluido": None}),
        "export lista": lambda: None,
    }
    nomes = sorted(muts)
    escolhidas = r.sample(nomes, r.randint(1, 3))
    for nme in escolhidas:
        muts[nme]()
    return (["x"] if "export lista" in escolhidas else e), escolhidas


def f_remoto_hostil_clientes(m, r):
    """Um export estranho num banco DESCARTAVEL (copia do PC atual): mesclar nao pode estourar nem deixar o banco incoerente."""
    pc = m.pc(r)
    outro = "B" if pc == "A" else "A"
    rem, quais = _hostil_remoto(m, r, o._exp(outro))
    chave = "+".join(quais)
    try:
        _pc_tmp("T3", copiar_de=pc)
        try:
            amb.entrar("T3")
            db.mesclar(rem)
        except Exception as ex:
            raise o.Achado("S1b-mesclar-estoura-com-export-estranho", f"mesclar({chave}): {type(ex).__name__}: {ex} [{o.lugar(ex)}]",
                           f"S1b:{type(ex).__name__}:{o.lugar(ex)}") from ex
        try:
            db.tarefas(incluir_feitas=True)
            db.clientes()
            db.exportar()
        except Exception as ex:
            raise o.Achado("H4-export-estranho-quebra-leitura", f"depois de mesclar({chave}): {type(ex).__name__}: {ex} [{o.lugar(ex)}]",
                           f"H4c:{type(ex).__name__}:{o.lugar(ex)}") from ex
        antes = db.exportar()
        n = db.mesclar(rem)
        cobrar(n == 0 and db.exportar() == antes, "S9b-mesclar-estranho-nao-idempotente", f"mesclar({chave}) 2x mudou {n}",
               "S9b:" + chave)
        try:
            o.estado(m)  # invariantes de estado tambem no banco descartavel
        except o.Achado as a:
            raise o.Achado(a.classe, f"(apos mesclar {chave}) {a.msg}", a.chave + ":estranho") from a
    finally:
        _apagar_tmp("T3")
        amb.entrar("A")
    return f"{pc} <- hostil {chave}"


# ----------------------------------------------------------------------------- hostis / rotas

def _flashes(m):
    with m.cli.session_transaction() as s:
        return [x[1] if isinstance(x, (list, tuple)) else x for x in s.get("_flashes", [])]


def f_renomear_duplicado(m, r):
    """Nome que ja existe aqui (qualquer caixa/espacos): tem de levantar ClienteDuplicado e NAO mudar nada."""
    pc = m.pc(r)
    cl = _clientes()
    if len(cl) < 2:
        return "poucos clientes"
    c, d = r.sample(cl, 2)
    nome = r.choice([d["nome"], d["nome"].upper(), d["nome"].lower(), " " + d["nome"] + " "])
    antes = o.linhas("clientes")
    via = r.choice(["renomear", "atualizar", "rota", "rota editar"])
    if via in ("renomear", "atualizar"):
        try:
            (db.renomear_cliente(c["id"], nome) if via == "renomear" else db.atualizar_cliente(c["id"], nome, "a@b.com"))
        except db.ClienteDuplicado:
            pass
        except Exception as e:
            raise o.Achado("H6-duplicado-estoura-outra-excecao", f"{via}: {type(e).__name__}: {e}", f"H6:{via}:{type(e).__name__}")
        else:
            cobrar(False, "H6-duplicado-aceito", f"{pc}: {via}({c['id']}, {nome!r}) devolveu normalmente, ja existe o cliente {d['id']}",
                   "H6:" + via + ("" if nome.isascii() else ":nao-ascii"))
    else:
        m.cli.get("/clientes")  # abre sessao limpa
        with m.cli.session_transaction() as s:
            s.pop("_flashes", None)
        rota = f"/clientes/{c['id']}" + ("/editar" if via == "rota editar" else "")
        pc, resp = chamar(m, r, "post", rota, data={"nome": nome, "emails": "a@b.com"})
        cobrar(any("existe" in str(x) for x in _flashes(m)), "I18-rota-duplicado-sem-mensagem", f"{pc}: POST {rota} nome={nome!r} sem mensagem ao usuario",
               "I18:" + via + ("" if nome.isascii() else ":nao-ascii"))
    cobrar(o.linhas("clientes") == antes, "H6-duplicado-mudou-banco", f"{pc}: {via} para nome existente alterou clientes", "H6b:" + via)
    return f"{pc} {via} {c['id']} -> {nome!r}"


def f_rota_cliente_novo_emails(m, r):
    m.pc(r)
    nome = r.choice(["Acme", "acme", "Novo W", "", "  ", "Zeta"])
    em = r.choice(EMAILS_OK + ["", " ", "x" * 3000, "<b>@x.com"])
    dados = {"nome": nome, "emails": em}
    if r.random() < 0.3:
        dados["abrir"] = "1"
    antes = {c["nome"].lower() for c in _clientes()}
    pc, resp = chamar(m, r, "post", "/clientes/novo", data=dados)
    if nome.strip():
        cobrar(nome.strip().lower() in {c["nome"].lower() for c in _clientes()}, "I19-clientes-novo", f"{pc}: cliente {nome!r} nao foi criado")
    else:
        cobrar({c["nome"].lower() for c in _clientes()} == antes, "I19-clientes-novo", f"{pc}: nome vazio criou cliente")
    return f"{pc} /clientes/novo {nome!r} {em[:30]!r}"


def f_rota_cliente_editar(m, r):
    m.pc(r)
    cl = _clientes()
    cid = r.choice([str(c["id"]) for c in cl] + ["999999", "0", HUGE]) if cl else "999999"
    dados = {"nome": r.choice([c["nome"] for c in cl] + ["Novo E", "", "  ", "N" * 500]) if cl else "Novo E", "emails": r.choice(EMAILS_OK + ["x" * 2000])}
    if r.random() < 0.1:
        dados.pop("emails")
    pc, resp = chamar(m, r, "post", f"/clientes/{cid}/editar", data=dados)
    return f"{pc} editar {cid} {dados.get('nome', '')[:10]!r}"


def f_rota_extras(m, r):
    """/perguntar, /reuniao/nova, /cliente/<id>/nota|cobrar, /nota/<id>/excluir, /clientes, /cliente/<id>, /semana"""
    m.pc(r)
    cf = _cliente_form(r)
    nid = str(r.choice([n["id"] for n in o.linhas("notas")])) if o.linhas("notas") and r.random() < 0.7 else r.choice(["0", "999999", HUGE])
    cid = r.choice([cf, "1", "2", "999999", HUGE])
    esc = r.choice(["perguntar_post", "perguntar_get", "nova_post", "nova_get", "nota", "nota_excluir", "cobrar_get", "cobrar_post", "ficha", "lista", "semana"])
    if esc == "perguntar_post":
        pc, resp = chamar(m, r, "post", "/perguntar", data={"pergunta": r.choice(["o que ficou combinado?", "", "  "]), "cliente_id": cf})
    elif esc == "perguntar_get":
        pc, resp = chamar(m, r, "get", "/perguntar", query_string={"cliente_id": cf, "pergunta": "x"})
    elif esc == "nova_post":
        pc, resp = chamar(m, r, "post", "/reuniao/nova", data={"data": r.choice(["2030-01-02", "lixo", ""]), "hora": r.choice(["10:00", "25:00", ""]),
                                                              "titulo": r.choice(["T", ""]), "duracao": r.choice(["30", "x", HUGE]), "cliente_id": cf,
                                                              "convidados": r.choice(["", "a@b.com, c", ";;"])})
    elif esc == "nova_get":
        pc, resp = chamar(m, r, "get", "/reuniao/nova", query_string={"cliente": cf})
    elif esc == "nota":
        pc, resp = chamar(m, r, "post", f"/cliente/{cid}/nota", data={"texto": r.choice(["nota", "", "  ", "z" * 3000])})
    elif esc == "nota_excluir":
        pc, resp = chamar(m, r, "post", f"/nota/{nid}/excluir")
    elif esc == "cobrar_get":
        pc, resp = chamar(m, r, "get", f"/cliente/{cid}/cobrar")
    elif esc == "cobrar_post":
        pc, resp = chamar(m, r, "post", f"/cliente/{cid}/cobrar", data={"para": r.choice(["a@b.com", "", "x", "a@b.com, c@d.com"]), "assunto": "s", "corpo": "c"})
    elif esc == "ficha":
        pc, resp = chamar(m, r, "get", f"/cliente/{cid}")
    elif esc == "lista":
        pc, resp = chamar(m, r, "get", "/clientes")
    else:
        pc, resp = chamar(m, r, "get", "/semana")
    return f"{pc} {esc} {cid}"


ACOES2 = A1.ACOES + [
    Acao("atualizar cliente (nome+emails)", f_atualizar_cliente, 3, cobre=["db.atualizar_cliente", "db.cliente", "db.clientes"]),
    Acao("criar cliente com emails", f_criar_cliente_emails, 2, cobre=["db.criar_cliente"]),
    Acao("cliente_por_email", f_cliente_por_email, 3, cobre=["db.cliente_por_email"]),
    Acao("renomear para nome do outro PC", f_renomear_para_nome_do_outro, 3, cobre=["db.renomear_cliente"]),
    Acao("cenario de uniao (+trocas)", f_cenario_uniao, 3, cobre=["db.mesclar", "db.exportar", "db.cliente_por_uuid"]),
    Acao("ordem do mesclar (T1/T2)", f_ordem_do_mesclar, 2, cobre=["db.mesclar"]),
    Acao("export antigo reaplicado", f_exportacao_antiga, 3, cobre=["db.mesclar", "db.exportar"]),
    Acao("remoto hostil (clientes/apelidos)", f_remoto_hostil_clientes, 2, True, ["db.mesclar"]),
    Acao("renomear/editar para nome existente", f_renomear_duplicado, 3, True, ["db.renomear_cliente", "db.atualizar_cliente", "db.ClienteDuplicado",
                                                                                   "/clientes/<int:cliente_id>", "/clientes/<int:cliente_id>/editar"]),
    Acao("POST /clientes/novo com emails", f_rota_cliente_novo_emails, 2, True, ["/clientes/novo"]),
    Acao("POST /clientes/<id>/editar", f_rota_cliente_editar, 2, True, ["/clientes/<int:cliente_id>/editar"]),
    Acao("rotas extras (perguntar, reuniao nova, notas, cobrar, ficha)", f_rota_extras, 4, True,
         ["/perguntar", "/reuniao/nova", "/cliente/<int:cliente_id>/nota", "/nota/<int:nota_id>/excluir", "/cliente/<int:cliente_id>/cobrar",
          "/clientes", "/cliente/<int:cliente_id>", "/semana"]),
]
SEM_ACAO = A1.SEM_ACAO
SEM_FUZZ = A1.SEM_FUZZ
