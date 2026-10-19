"""Propriedades. As de ESTADO valem depois de cada passo; as de INSERCAO sao cobradas dentro da acao.

Cada violacao e um Achado(classe, mensagem, chave). A chave identifica a causa (para deduplicar entre sementes).
"""
import json
from datetime import date

import ambiente as amb
import db


class Achado(Exception):
    def __init__(self, classe, msg, chave=None):
        super().__init__(msg)
        self.classe, self.msg, self.chave = classe, msg, chave or classe


def cobrar(cond, classe, msg, chave=None):
    if not cond:
        raise Achado(classe, msg, chave)


def linhas(tabela):
    with db.conexao() as con:
        return [dict(r) for r in con.execute(f"SELECT * FROM {tabela} ORDER BY rowid")]


def estado(m):
    """Propriedades de estado, nos dois PCs."""
    hoje = date.today()
    alvo = sorted(amb.SUJOS)
    for pc in alvo:
        amb.entrar(pc)
        tars, clis = linhas("tarefas"), linhas("clientes")
        exc = {t["id"] for t in tars if t["excluido"]}
        ids_cli = {c["id"] for c in clis}
        cli_exc = {c["id"] for c in clis if c["excluido"]}
        for t in tars:
            cobrar(t["status"] in db.STATUS, "E1-status-invalido", f"{pc}: tarefa {t['id']} com status {t['status']!r}")
            if t["prazo"] is not None:
                try:
                    ok = date.fromisoformat(t["prazo"]).isoformat() == t["prazo"]
                except (ValueError, TypeError):
                    ok = False
                cobrar(ok, "E2-prazo-invalido-gravado", f"{pc}: tarefa {t['id']} com prazo {t['prazo']!r} (devia ser AAAA-MM-DD ou NULL)")
            cobrar((t["status"] == "feito") == (t["concluida_em"] is not None), "E3-concluida_em-incoerente",
                   f"{pc}: tarefa {t['id']} status={t['status']} concluida_em={t['concluida_em']}")
            cobrar(t["cliente_id"] is None or t["cliente_id"] in ids_cli, "E4-tarefa-orfa",
                   f"{pc}: tarefa {t['id']} aponta para o cliente {t['cliente_id']}, que nao existe")
        for args in ({}, {"somente_minhas": True}, {"incluir_feitas": True}, {"incluir_feitas": True, "somente_minhas": True}):
            for t in db.tarefas(**args):
                cobrar(t["id"] not in exc, "E5-tarefas()-devolve-excluida", f"{pc}: tarefas({args}) devolveu a excluida {t['id']}")
                cobrar(t["cliente_id"] not in cli_exc, "E28-tarefa-de-cliente-excluido", f"{pc}: tarefas({args}) devolveu {t['id']} do cliente excluido {t['cliente_id']}")
                if not args.get("incluir_feitas"):
                    cobrar(t["status"] != "feito", "E6-feita-sem-pedir", f"tarefas({args}) devolveu feita")
                if args.get("somente_minhas"):
                    cobrar(t["minha"] == 1, "E7-minha", "tarefas(somente_minhas) devolveu tarefa do cliente")
                p = t["prazo"]
                cobrar(t["atrasada"] == bool(p and p < hoje.isoformat() and t["status"] != "feito"), "E8-atrasada-incoerente",
                       f"tarefa {t['id']} prazo={p} status={t['status']} atrasada={t['atrasada']}")
                cobrar(t["hoje"] == (p == hoje.isoformat()), "E9-hoje-incoerente", f"tarefa {t['id']} prazo={p} hoje={t['hoje']}")
                cobrar(t["dias"] == ((date.fromisoformat(p) - hoje).days if p else None), "E10-dias-incoerente", f"tarefa {t['id']}")
        if hasattr(db, "notas"):
            nts = linhas("notas")
            for n in nts:
                cobrar(n["cliente_id"] is None or n["cliente_id"] in ids_cli, "E17-nota-orfa", f"{pc}: nota {n['id']} aponta para cliente {n['cliente_id']} inexistente")
            for c in clis:
                vis = {n["id"] for n in db.notas(c["id"])}
                cobrar(vis == {n["id"] for n in nts if n["cliente_id"] == c["id"] and not n["excluido"]}, "E18-notas()-incoerente", f"{pc}: cliente {c['id']}")
        _estado_clientes(pc, clis)
        _estado_pessoas(pc)
        cobrar(len({c["uuid"] for c in clis}) == len(clis), "E11-uuid-cliente-repetido", pc)
        cobrar(len({t["uuid"] for t in tars}) == len(tars), "E12-uuid-tarefa-repetido", pc)
        nomes = [c["nome"].lower() for c in clis]
        rep = [n for n in set(nomes) if nomes.count(n) > 1]
        cobrar(not rep, "E13-cliente-nome-repetido", f"{pc}: {rep[:2]}", "E13-cliente-nome-repetido" + ("" if all(n.isascii() for n in rep) else ":nao-ascii"))
        exp = db.exportar()
        cobrar(len(exp["tarefas"]) == len(tars) and len(exp["clientes"]) == len(clis), "E14-exportar-perde-registro", pc)
        for arq in amb.config.ATAS_DIR.glob("*.json"):
            try:
                reg = json.loads(arq.read_text(encoding="utf-8"))
            except Exception as e:
                raise Achado("E15-ata-ilegivel", f"{pc}: {arq.name}: {e}")
            cobrar("ata" in reg and "reuniao" in reg, "E16-ata-sem-campos", f"{pc}: {arq.name}")
    amb.entrar("A")
    amb.SUJOS.clear()


def _estado_pessoas(pc):
    """E26/E27: uma pessoa so uma vez por cliente (sem diferenciar maiusculas, inclusive acentuadas); listas coerentes."""
    if not _tem_tabela("pessoas"):
        return
    import unicodedata
    vivas = [p for p in linhas("pessoas") if not p["excluido"] and p["cliente_id"] is not None]
    chaves = [(p["cliente_id"], unicodedata.normalize("NFC", p["nome"]).casefold()) for p in vivas]
    rep = sorted({k for k in chaves if chaves.count(k) > 1})
    cobrar(not rep, "E26-pessoa-repetida-no-cliente", f"{pc}: {rep[:2]}",
           "E26-pessoa-repetida-no-cliente" + ("" if all(n.isascii() for _, n in rep) else ":nao-ascii"))
    por = db.pessoas_por_cliente()
    for cid in {p["cliente_id"] for p in vivas} | set(por):
        cobrar(sorted(db.pessoas(cid)) == sorted(p["nome"] for p in vivas if p["cliente_id"] == cid) == sorted(por.get(cid, [])),
               "E27-pessoas-listas-incoerentes", f"{pc}: cliente {cid}")


def _estado_clientes(pc, clis):
    """E19..E25: apelidos (clientes_alias) e e-mails dos clientes. Estado: valem depois de qualquer passo."""
    if not hasattr(db, "cliente_por_email"):
        return
    uuids = {c["uuid"] for c in clis}
    ap = {a["uuid"]: a["para"] for a in linhas("clientes_alias")} if _tem_tabela("clientes_alias") else {}
    vivo = sorted(set(ap) & uuids)
    cobrar(not vivo, "E19-apelido-de-cliente-vivo", f"{pc}: uuid {vivo[0][:6] if vivo else ''} e apelido E cliente ao mesmo tempo")
    for u in ap:
        v, x = set(), u
        while x in ap:
            cobrar(x not in v, "E20-apelidos-em-ciclo", f"{pc}: ciclo de apelidos em {u[:6]}")
            v.add(x)
            x = ap[x]
        cobrar(x in uuids, "E21-apelido-aponta-para-nada", f"{pc}: apelido {u[:6]} termina em {x[:6]}, que nao e cliente")
    exp = db.exportar()
    cobrar(sorted((a["uuid"], a["para"]) for a in exp.get("apelidos", [])) == sorted(ap.items()), "E22-exportar-apelidos", f"{pc}: exportar()['apelidos'] difere da tabela")
    for c in clis:
        em = c.get("emails") or ""
        itens = [e for e in em.split(", ") if e]
        ok = em == "" or (em == em.lower().strip() and len(set(itens)) == len(itens) and all("@" in e and " " not in e and "," not in e for e in itens))
        cobrar(ok, "E23-emails-fora-do-padrao", f"{pc}: cliente {c['id']} emails={em!r}")
        for e in (itens if not c["excluido"] else []):  # cliente excluido nao e sugerido pelo e-mail (de proposito)
            got = db.cliente_por_email(e)
            dono = next((x for x in clis if x["id"] == got), None)
            cobrar(got is not None and dono is not None and not dono["excluido"], "E24-cliente_por_email-nao-acha",
                   f"{pc}: {e!r} cadastrado no cliente {c['id']} (excluido={c['excluido']}) -> {got}",
                   "E24:" + ("excluido" if c["excluido"] else "vivo"))
            cad = (dono["emails"] or "").split(", ")
            cobrar(e in cad or ("@" + e.split("@", 1)[-1]) in cad, "E25-cliente_por_email-errado", f"{pc}: {e!r} -> cliente {got} sem esse e-mail/dominio")


def canonico_local(u):
    ap = {a["uuid"]: a["para"] for a in linhas("clientes_alias")}
    vistos = set()
    while u in ap and u not in vistos:
        vistos.add(u)
        u = ap[u]
    return u


def _tem_tabela(nome):
    with db.conexao() as con:
        return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone())


# ------------------------------------------------------------------ convergencia (insercao: vale no momento da troca)

def _exp(pc):
    amb.entrar(pc)
    return db.exportar()


def _norm(exp):
    nomes = {c["uuid"]: c["nome"].lower() for c in exp["clientes"]}
    cl = {c["uuid"]: (c["nome"].lower(), c["excluido"], c["atualizado_em"], c.get("emails", "")) for c in exp["clientes"]}
    tr = {t["uuid"]: tuple((k, t[k]) for k in sorted(t) if k != "cliente_uuid") + (("cliente", nomes.get(t["cliente_uuid"])),)
          for t in exp["tarefas"]}
    tr.update({"nota:" + n["uuid"]: tuple((k, n[k]) for k in sorted(n) if k != "cliente_uuid") + (("cliente", nomes.get(n["cliente_uuid"])),)
               for n in exp.get("notas", [])})
    return cl, tr


def _dif(na, nb):
    (ca, ta), (cb, tb) = na, nb
    partes = []
    for u in sorted(set(ca) | set(cb)):
        if ca.get(u) != cb.get(u):
            partes.append(f"cliente {u[:6]}: A={ca.get(u)} B={cb.get(u)}")
    for u in sorted(set(ta) | set(tb)):
        if ta.get(u) != tb.get(u):
            da, dbb = dict(ta.get(u, ())), dict(tb.get(u, ()))
            partes.append(f"tarefa {u[:6]}: campos {sorted(k for k in set(da) | set(dbb) if da.get(k) != dbb.get(k))}")
    return "; ".join(partes[:4]) or "?"


def _dif_tipo(na, nb):
    (ca, ta), (cb, tb) = na, nb
    t = []
    if set(ca) != set(cb):
        t.append("uuids-de-cliente")
    elif ca != cb:
        t.append("campos-de-cliente")
    if ta != tb:
        t.append("tarefas")
    return "+".join(t)


def _mesclar(pc, remoto, de):
    amb.entrar(pc)
    try:
        return db.mesclar(remoto)
    except Exception as e:
        raise Achado("S1-mesclar-estoura", f"{pc}.mesclar(export de {de}): {type(e).__name__}: {e}",
                     f"S1-mesclar-estoura:{type(e).__name__}") from e


def trocar(m):
    """Os dois PCs trocam exportar() nos dois sentidos. Cobra convergencia, nao-perda, nao-ressurreicao, idempotencia."""
    ea, eb = _exp("A"), _exp("B")
    _mesclar("B", ea, "A")
    _mesclar("A", _exp("B"), "B")
    xa, xb = _exp("A"), _exp("B")
    na, nb = _norm(xa), _norm(xb)
    if na != nb:
        d1 = _dif(na, nb)
        _mesclar("B", _exp("A"), "A")
        _mesclar("A", _exp("B"), "B")
        xa, xb = _exp("A"), _exp("B")
        na, nb = _norm(xa), _norm(xb)
        cobrar(na == nb, "S2-nao-converge", "apos 2 rodadas A e B seguem diferentes: " + _dif(na, nb),
               "S2-nao-converge:" + _dif_tipo(na, nb))
        raise Achado("S3-converge-so-na-2a-rodada", "a 1a troca nos dois sentidos nao igualou os bancos: " + d1,
                     "S3-converge-so-na-2a-rodada")
    ta = {t["uuid"]: t for t in ea["tarefas"]}
    tb = {t["uuid"]: t for t in eb["tarefas"]}
    final = {t["uuid"]: t for t in xa["tarefas"]}
    for u in set(ta) | set(tb):
        cobrar(u in final, "S4-tarefa-perdida", f"tarefa {u[:6]} sumiu na troca")
        melhor = max((x[u] for x in (ta, tb) if u in x), key=lambda t: t["atualizado_em"])
        f = final[u]
        for k in ("descricao", "status", "prazo", "excluido", "minha", "responsavel", "atualizado_em"):
            cl = "S6-exclusao-desfeita" if k == "excluido" else "S5-alteracao-mais-recente-perdida"
            cobrar(f[k] == melhor[k], cl, f"tarefa {u[:6]} campo {k}: final={f[k]!r}, mais recente={melhor[k]!r}", cl + ":" + k)
    na_, nb_ = {n["uuid"]: n for n in ea.get("notas", [])}, {n["uuid"]: n for n in eb.get("notas", [])}
    nfin = {n["uuid"]: n for n in xa.get("notas", [])}
    for u in set(na_) | set(nb_):
        cobrar(u in nfin, "S10-nota-perdida", f"nota {u[:6]} sumiu na troca")
        melhor = max((x[u] for x in (na_, nb_) if u in x), key=lambda n: n["atualizado_em"])
        cobrar((nfin[u]["texto"], nfin[u]["excluido"]) == (melhor["texto"], melhor["excluido"]), "S11-nota-mais-recente-perdida", f"nota {u[:6]}")
    # clientes por GRUPO: cada uuid de entrada vai para o uuid que ficou (cadeia de apelidos)
    ap = {a["uuid"]: a["para"] for x in (ea, eb, xa) for a in x.get("apelidos", [])}

    def canon(u):
        vistos = set()
        while u in ap and u not in vistos:
            vistos.add(u)
            u = ap[u]
        return u
    cfin = {c["uuid"]: c for c in xa["clientes"]}
    grupos = {}
    for c in ea["clientes"] + eb["clientes"]:
        grupos.setdefault(canon(c["uuid"]), []).append(c)
    for u, membros in grupos.items():
        cobrar(u in cfin, "S8-cliente-sumiu", f"cliente {membros[0]['nome']!r} ({u[:6]}) sumiu na troca")
        todos = {m["uuid"] for m in membros} | {k for k in ap if canon(k) == u} | {u}
        cobrar(u == min(todos), "S12-uuid-que-fica-nao-e-o-menor", f"grupo {sorted(x[:6] for x in todos)} ficou com {u[:6]}")
        # mesmo desempate de db._mais_recente: data, uuid e, empatando os dois, o proprio conteudo
        melhor = max(membros, key=lambda c: (c["atualizado_em"] or "", c["uuid"], str(c["nome"] or ""),
                                             str(c.get("emails") or ""), int(c["excluido"] or 0)))
        f = cfin[u]
        cobrar((f["nome"], f["excluido"], f.get("emails", "")) == (melhor["nome"], melhor["excluido"], melhor.get("emails", "")),
               "S7-cliente-mais-recente-perdido",
               f"cliente {u[:6]}: final={f['nome']!r}/{f['excluido']}/{f.get('emails')!r} mais recente={melhor['nome']!r}/{melhor['excluido']}/{melhor.get('emails')!r} (grupo de {len(membros)})")
    cobrar(len(cfin) == len(grupos), "S14-clientes-a-mais", f"final tem {len(cfin)} clientes, os grupos de entrada sao {len(grupos)}")
    # a tarefa/nota vai para o cliente (canonico) da versao que venceu
    for tipo, la, lb, fin in (("tarefa", ea["tarefas"], eb["tarefas"], xa["tarefas"]), ("nota", ea.get("notas", []), eb.get("notas", []), xa.get("notas", []))):
        ia_, ib_, fi_ = ({t["uuid"]: t for t in l} for l in (la, lb, fin))
        for u in set(ia_) | set(ib_):
            melhor = max((x[u] for x in (ia_, ib_) if u in x), key=lambda t: t["atualizado_em"])
            esperado = canon(melhor["cliente_uuid"]) if melhor["cliente_uuid"] else None
            real = fi_[u]["cliente_uuid"] if u in fi_ else "?"
            cobrar(real == esperado, "S13-" + tipo + "-no-cliente-errado", f"{tipo} {u[:6]}: final no cliente {str(real)[:6]}, esperado {str(esperado)[:6]}")
    antes = _exp("A"), _exp("B")
    n1 = _mesclar("A", antes[1], "B")
    n2 = _mesclar("B", antes[0], "A")
    cobrar(n1 == 0 and n2 == 0, "S9-mesclar-nao-idempotente", f"re-mesclar o mesmo export mudou {n1}/{n2} registros")
    cobrar((_exp("A"), _exp("B")) == antes, "S9-mesclar-nao-idempotente", "re-mesclar alterou o export")
    amb.entrar("A")


def lugar(exc):
    """arquivo:linha do ultimo quadro que pertence ao sistema testado (nao ao tester, nem ao venv)."""
    raiz = str(amb.ALVO).replace("\\", "/").lower()
    tb, achado = exc.__traceback__, "?"
    while tb:
        f = tb.tb_frame.f_code.co_filename.replace("\\", "/")
        if f.lower().startswith(raiz) and "/testes/" not in f and "/.venv/" not in f:
            achado = f"{f.rsplit('/', 1)[1]}:{tb.tb_lineno}"
        tb = tb.tb_next
    return achado
