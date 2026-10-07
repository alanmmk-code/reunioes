"""Banco local (SQLite) de clientes e tarefas/demandas.

Cada registro tem um `uuid` (igual em todos os PCs), `atualizado_em` e `excluido` (exclusão marcada,
não apagada), para que a sincronização pelo Google Drive consiga mesclar o que foi feito em cada PC.
"""

import sqlite3
import uuid as _uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone

import config

ARQUIVO = config.DADOS_DIR / "dados.db"
STATUS = ("a_fazer", "fazendo", "feito")
CAMPOS_TAREFA = ("descricao", "responsavel", "minha", "prazo", "status", "origem_ata", "origem_titulo",
                 "criada_em", "concluida_em", "atualizado_em", "excluido")


def agora() -> str:
    """Momento atual em UTC, com milissegundos (ordena corretamente entre PCs)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def conexao():
    con = sqlite3.connect(ARQUIVO)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def criar_tabelas() -> None:
    with conexao() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS clientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL UNIQUE COLLATE NOCASE,
                criado_em TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tarefas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente_id INTEGER REFERENCES clientes(id),
                descricao TEXT NOT NULL,
                responsavel TEXT NOT NULL DEFAULT '',
                minha INTEGER NOT NULL DEFAULT 1,   -- 1 = é sua (ou da sua equipe)
                prazo TEXT,                          -- AAAA-MM-DD ou NULL
                status TEXT NOT NULL DEFAULT 'a_fazer',
                origem_ata TEXT,                     -- id da reunião que gerou a tarefa
                origem_titulo TEXT,
                criada_em TEXT NOT NULL,
                concluida_em TEXT
            );
            """
        )
        # Migração: campos de sincronização
        for tabela in ("clientes", "tarefas"):
            colunas = {r["name"] for r in con.execute(f"PRAGMA table_info({tabela})")}
            if "uuid" not in colunas:
                con.execute(f"ALTER TABLE {tabela} ADD COLUMN uuid TEXT")
            if "atualizado_em" not in colunas:
                con.execute(f"ALTER TABLE {tabela} ADD COLUMN atualizado_em TEXT")
            if "excluido" not in colunas:
                con.execute(f"ALTER TABLE {tabela} ADD COLUMN excluido INTEGER NOT NULL DEFAULT 0")
            for r in con.execute(f"SELECT id FROM {tabela} WHERE uuid IS NULL").fetchall():
                con.execute(f"UPDATE {tabela} SET uuid = ?, atualizado_em = ? WHERE id = ?",
                            (_uuid.uuid4().hex, agora(), r["id"]))
        if "emails" not in {r["name"] for r in con.execute("PRAGMA table_info(clientes)")}:
            con.execute("ALTER TABLE clientes ADD COLUMN emails TEXT NOT NULL DEFAULT ''")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_clientes_uuid ON clientes(uuid)")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_tarefas_uuid ON tarefas(uuid)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS notas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT NOT NULL UNIQUE,
                cliente_id INTEGER REFERENCES clientes(id),
                texto TEXT NOT NULL,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL,
                excluido INTEGER NOT NULL DEFAULT 0
            )"""
        )
        # Pessoas de cada cliente (quem entra nas chamadas), escolhidas ou digitadas no assistente
        con.execute(
            """CREATE TABLE IF NOT EXISTS pessoas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT NOT NULL UNIQUE,
                cliente_id INTEGER REFERENCES clientes(id),
                nome TEXT NOT NULL,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL,
                excluido INTEGER NOT NULL DEFAULT 0
            )"""
        )
        # uuid de cliente que foi unido a outro -> uuid que ficou (sincronizado entre PCs)
        con.execute("CREATE TABLE IF NOT EXISTS clientes_alias (uuid TEXT PRIMARY KEY, para TEXT NOT NULL)")


# ---------------------------------------------------------------- validação

class ClienteDuplicado(ValueError):
    """Já existe outro cliente com esse nome."""


def _id(valor) -> int | None:
    """Id válido do SQLite, ou None (texto, vazio, dígitos estranhos, número gigante)."""
    if isinstance(valor, bool):
        return None
    try:
        numero = int(str(valor).strip()) if not isinstance(valor, int) else valor
    except (TypeError, ValueError):
        return None
    return numero if 0 < numero < 2 ** 63 else None


# ---------------------------------------------------------------- clientes

def clientes() -> list[dict]:
    with conexao() as con:
        return [dict(r) for r in con.execute("SELECT * FROM clientes WHERE excluido = 0 ORDER BY nome")]


def cliente(cliente_id) -> dict | None:
    cliente_id = _id(cliente_id)
    if not cliente_id:
        return None
    with conexao() as con:
        r = con.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
        return dict(r) if r else None


def cliente_por_uuid(cliente_uuid) -> dict | None:
    if not cliente_uuid:
        return None
    with conexao() as con:
        r = con.execute("SELECT * FROM clientes WHERE uuid = ?", (_canonico(con, cliente_uuid),)).fetchone()
        return dict(r) if r else None


def _limpar_emails(emails) -> str:
    """Normaliza 'joao@ivone.com; @ivone.com' -> 'joao@ivone.com, @ivone.com'."""
    itens = [e.strip().lower() for e in str(emails or "").replace(";", ",").replace("\n", ",").split(",")]
    vistos = []
    for e in itens:
        if "@" in e and e not in vistos:
            vistos.append(e)
    return ", ".join(vistos)


def cliente_por_email(email: str) -> int | None:
    """Cliente cujo e-mail ou domínio (@empresa.com) cadastrado bate com este e-mail."""
    email = str(email or "").strip().lower()
    if "@" not in email:
        return None
    dominio = "@" + email.split("@", 1)[1]
    for c in clientes():
        cadastrados = [e.strip() for e in (c.get("emails") or "").split(",") if e.strip()]
        if email in cadastrados or dominio in cadastrados:
            return c["id"]
    return None


def criar_cliente(nome, emails: str = "") -> int | None:
    nome = str(nome or "").strip()
    if not nome:
        return None
    with conexao() as con:
        existente = con.execute("SELECT id, excluido FROM clientes WHERE nome = ?", (nome,)).fetchone()
        if existente:
            if existente["excluido"]:
                con.execute("UPDATE clientes SET excluido = 0, atualizado_em = ? WHERE id = ?", (agora(), existente["id"]))
            return existente["id"]
        momento = agora()
        return con.execute("INSERT INTO clientes (nome, criado_em, uuid, atualizado_em, emails) VALUES (?, ?, ?, ?, ?)",
                           (nome, momento, _uuid.uuid4().hex, momento, _limpar_emails(emails))).lastrowid


def _nome_livre(con, nome: str, cliente_id: int) -> None:
    outro = con.execute("SELECT id FROM clientes WHERE nome = ? AND id != ?", (nome, cliente_id)).fetchone()
    if outro:
        raise ClienteDuplicado(f"Já existe um cliente chamado \"{nome}\".")


def atualizar_cliente(cliente_id, nome, emails) -> None:
    cliente_id, nome = _id(cliente_id), str(nome or "").strip()
    if not cliente_id or not nome:
        return
    with conexao() as con:
        _nome_livre(con, nome, cliente_id)
        con.execute("UPDATE clientes SET nome = ?, emails = ?, atualizado_em = ? WHERE id = ?",
                    (nome, _limpar_emails(emails), agora(), cliente_id))


def renomear_cliente(cliente_id, nome) -> None:
    cliente_id, nome = _id(cliente_id), str(nome or "").strip()
    if not cliente_id or not nome:
        return
    with conexao() as con:
        _nome_livre(con, nome, cliente_id)
        con.execute("UPDATE clientes SET nome = ?, atualizado_em = ? WHERE id = ?", (nome, agora(), cliente_id))


# ---------------------------------------------------------------- tarefas

def _prazo_valido(prazo) -> str | None:
    try:
        return date.fromisoformat(str(prazo or "").strip()[:10]).isoformat()
    except ValueError:
        return None  # "A definir" e afins


def _cliente_existente(con, cliente_id) -> int | None:
    cliente_id = _id(cliente_id)
    if cliente_id and con.execute("SELECT 1 FROM clientes WHERE id = ?", (cliente_id,)).fetchone():
        return cliente_id
    return None


def criar_tarefa(cliente_id, descricao, responsavel="", minha=True, prazo=None, origem_ata=None, origem_titulo=None) -> int:
    momento = agora()
    with conexao() as con:
        return con.execute(
            "INSERT INTO tarefas (cliente_id, descricao, responsavel, minha, prazo, origem_ata, origem_titulo, "
            "criada_em, uuid, atualizado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_cliente_existente(con, cliente_id), str(descricao or "").strip(), str(responsavel or "").strip(),
             int(bool(minha)), _prazo_valido(prazo), origem_ata, origem_titulo, momento, _uuid.uuid4().hex, momento),
        ).lastrowid


def importar_acoes_da_ata(reuniao: dict, ata, cliente_id) -> int:
    """Cria as tarefas a partir das ações da ata (substitui as de uma importação anterior da mesma ata)."""
    with conexao() as con:
        con.execute("UPDATE tarefas SET excluido = 1, atualizado_em = ? "
                    "WHERE origem_ata = ? AND status != 'feito' AND excluido = 0", (agora(), reuniao["id"]))
    for a in ata.acoes:
        criar_tarefa(cliente_id, a.tarefa, a.responsavel, a.do_usuario, a.prazo, reuniao["id"], ata.titulo)
    return len(ata.acoes)


def mover_tarefas_da_ata(event_id: str, cliente_id) -> None:
    with conexao() as con:
        con.execute("UPDATE tarefas SET cliente_id = ?, atualizado_em = ? WHERE origem_ata = ?",
                    (_cliente_existente(con, cliente_id), agora(), event_id))


def atualizar_tarefa(tarefa_id, **campos) -> None:
    tarefa_id = _id(tarefa_id)
    if not tarefa_id:
        return
    permitidos = {"descricao", "responsavel", "minha", "prazo", "status", "cliente_id"}
    campos = {k: v for k, v in campos.items() if k in permitidos}
    if "prazo" in campos:
        campos["prazo"] = _prazo_valido(campos["prazo"])
    if "status" in campos:
        if campos["status"] not in STATUS:
            return
        campos["concluida_em"] = agora() if campos["status"] == "feito" else None
    if not campos:
        return
    campos["atualizado_em"] = agora()
    with conexao() as con:
        if "cliente_id" in campos:
            campos["cliente_id"] = _cliente_existente(con, campos["cliente_id"])
        con.execute(f"UPDATE tarefas SET {', '.join(f'{k} = ?' for k in campos)} WHERE id = ?",
                    (*campos.values(), tarefa_id))


def excluir_tarefas_da_ata(event_id: str) -> int:
    """Exclui (marcando) as tarefas em aberto que vieram de uma reunião. Devolve quantas."""
    with conexao() as con:
        return con.execute("UPDATE tarefas SET excluido = 1, atualizado_em = ? "
                           "WHERE origem_ata = ? AND status != 'feito' AND excluido = 0", (agora(), event_id)).rowcount


def excluir_tarefa(tarefa_id) -> None:
    tarefa_id = _id(tarefa_id)
    if not tarefa_id:
        return
    with conexao() as con:
        con.execute("UPDATE tarefas SET excluido = 1, atualizado_em = ? WHERE id = ?", (agora(), tarefa_id))


def tarefas(somente_minhas=False, incluir_feitas=False, cliente_id=None) -> list[dict]:
    sql = ("SELECT t.*, c.nome AS cliente FROM tarefas t LEFT JOIN clientes c ON c.id = t.cliente_id "
           "WHERE t.excluido = 0")
    args = []
    if somente_minhas:
        sql += " AND t.minha = 1"
    if not incluir_feitas:
        sql += " AND t.status != 'feito'"
    if cliente_id is not None and cliente_id != "":
        sql += " AND t.cliente_id = ?"
        args.append(_id(cliente_id) or -1)  # id inválido: nenhuma tarefa
    sql += " ORDER BY t.status = 'feito', t.prazo IS NULL, t.prazo, t.id"
    hoje = date.today()
    with conexao() as con:
        linhas = [dict(r) for r in con.execute(sql, args)]
    for t in linhas:
        prazo = _prazo_valido(t["prazo"]) if t["prazo"] else None  # prazo estranho vindo de fora não derruba a tela
        t["atrasada"] = bool(prazo and prazo < hoje.isoformat() and t["status"] != "feito")
        t["hoje"] = prazo == hoje.isoformat()
        t["dias"] = (date.fromisoformat(prazo) - hoje).days if prazo else None
    return linhas


# ---------------------------------------------------------------- anotações por cliente

def notas(cliente_id) -> list[dict]:
    cliente_id = _id(cliente_id)
    if not cliente_id:
        return []
    with conexao() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM notas WHERE cliente_id = ? AND excluido = 0 ORDER BY criado_em DESC", (cliente_id,))]


def criar_nota(cliente_id, texto) -> int | None:
    texto = str(texto or "").strip()
    c = cliente(cliente_id)
    if not texto or not c:
        return None
    momento = agora()
    with conexao() as con:
        return con.execute("INSERT INTO notas (uuid, cliente_id, texto, criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?)",
                           (_uuid.uuid4().hex, c["id"], texto, momento, momento)).lastrowid


def excluir_nota(nota_id) -> None:
    nota_id = _id(nota_id)
    if not nota_id:
        return
    with conexao() as con:
        con.execute("UPDATE notas SET excluido = 1, atualizado_em = ? WHERE id = ?", (agora(), nota_id))


# ---------------------------------------------------------------- pessoas por cliente

def pessoas(cliente_id) -> list[str]:
    """Nomes das pessoas do cliente (quem já entrou em chamada), em ordem alfabética."""
    cliente_id = _id(cliente_id)
    if not cliente_id:
        return []
    with conexao() as con:
        return [r["nome"] for r in con.execute(
            "SELECT nome FROM pessoas WHERE cliente_id = ? AND excluido = 0 ORDER BY nome COLLATE NOCASE", (cliente_id,))]


def pessoas_por_cliente() -> dict[int, list[str]]:
    with conexao() as con:
        lista = {}
        for r in con.execute("SELECT cliente_id, nome FROM pessoas WHERE excluido = 0 AND cliente_id IS NOT NULL "
                             "ORDER BY nome COLLATE NOCASE"):
            lista.setdefault(r["cliente_id"], []).append(r["nome"])
        return lista


def adicionar_pessoa(cliente_id, nome) -> bool:
    """Liga a pessoa ao cliente (sem duplicar: mesmo nome, sem diferenciar maiúsculas). True se é nova."""
    nome = " ".join(str(nome or "").split())[:80]
    c = cliente(cliente_id)
    if not nome or not c:
        return False
    with conexao() as con:
        existente = con.execute("SELECT id, excluido FROM pessoas WHERE cliente_id = ? AND nome = ? COLLATE NOCASE",
                                (c["id"], nome)).fetchone()
        if existente:
            if existente["excluido"]:
                con.execute("UPDATE pessoas SET excluido = 0, atualizado_em = ? WHERE id = ?", (agora(), existente["id"]))
            return bool(existente["excluido"])
        momento = agora()
        con.execute("INSERT INTO pessoas (uuid, cliente_id, nome, criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?)",
                    (_uuid.uuid4().hex, c["id"], nome, momento, momento))
        return True


# ---------------------------------------------------------------- sincronização
#
# Clientes com o mesmo nome (sem diferenciar maiúsculas) são o MESMO cliente, venham de onde vierem
# (criados nos dois PCs, ou um renomeado para o nome do outro). A união é determinística — os dois
# PCs chegam ao mesmo resultado em qualquer ordem: fica o MENOR uuid, valem os dados da versão mais
# recente, tarefas e anotações passam para o registro que fica, e o uuid que saiu vira um apelido
# (clientes_alias) que também é sincronizado.

def _canonico(con, cliente_uuid):
    vistos = set()
    while cliente_uuid and cliente_uuid not in vistos:
        vistos.add(cliente_uuid)
        r = con.execute("SELECT para FROM clientes_alias WHERE uuid = ?", (cliente_uuid,)).fetchone()
        if not r:
            break
        cliente_uuid = r["para"]
    return cliente_uuid


def _mais_recente(a: dict, b: dict) -> dict:
    return a if ((a.get("atualizado_em") or ""), a["uuid"]) >= ((b.get("atualizado_em") or ""), b["uuid"]) else b


def _apelidar(con, uuid_antigo: str, canonico: str) -> None:
    if uuid_antigo != canonico:
        con.execute("INSERT OR REPLACE INTO clientes_alias (uuid, para) VALUES (?, ?)", (uuid_antigo, canonico))


def _unir_registros(con, a: dict, b: dict) -> None:
    """Une dois registros locais (ids diferentes) do mesmo cliente."""
    novo = _mais_recente(a, b)
    canonico = min(a["uuid"], b["uuid"])
    fica, sai = (a, b) if a["uuid"] == canonico else (b, a)
    con.execute("UPDATE tarefas SET cliente_id = ? WHERE cliente_id = ?", (fica["id"], sai["id"]))
    con.execute("UPDATE notas SET cliente_id = ? WHERE cliente_id = ?", (fica["id"], sai["id"]))
    con.execute("UPDATE pessoas SET cliente_id = ? WHERE cliente_id = ?", (fica["id"], sai["id"]))
    con.execute("DELETE FROM clientes WHERE id = ?", (sai["id"],))
    con.execute("UPDATE clientes SET nome = ?, emails = ?, excluido = ?, atualizado_em = ? WHERE id = ?",
                (novo["nome"], novo.get("emails") or "", int(novo.get("excluido") or 0), novo["atualizado_em"], fica["id"]))
    _apelidar(con, sai["uuid"], canonico)


def _aplicar(con, local: dict, remoto: dict) -> bool:
    """Leva para o registro local a versão remota, se for mais recente. Devolve se mudou algo."""
    if _mais_recente(local, remoto) is local:
        return False
    novos = (remoto["nome"], remoto.get("emails") or "", int(remoto.get("excluido") or 0), remoto["atualizado_em"])
    if novos == (local["nome"], local.get("emails") or "", int(local.get("excluido") or 0), local["atualizado_em"]):
        return False
    outro = con.execute("SELECT * FROM clientes WHERE nome = ? AND id != ?", (remoto["nome"], local["id"])).fetchone()
    if outro:  # renomeado para o nome de outro cliente: são o mesmo
        _unir_registros(con, {**local, "nome": remoto["nome"], "emails": novos[1], "excluido": novos[2],
                              "atualizado_em": novos[3]}, dict(outro))
    else:
        con.execute("UPDATE clientes SET nome = ?, emails = ?, excluido = ?, atualizado_em = ? WHERE id = ?",
                    (*novos, local["id"]))
    return True


def exportar() -> dict:
    """Tudo (inclusive excluídos), com o cliente referenciado pelo uuid — igual em todos os PCs."""
    with conexao() as con:
        cli = [dict(r) for r in con.execute(
            "SELECT uuid, nome, emails, criado_em, atualizado_em, excluido FROM clientes ORDER BY uuid")]
        tar = [dict(r) for r in con.execute(
            f"SELECT t.uuid, c.uuid AS cliente_uuid, {', '.join('t.' + c for c in CAMPOS_TAREFA)} "
            "FROM tarefas t LEFT JOIN clientes c ON c.id = t.cliente_id ORDER BY t.uuid")]
        nts = [dict(r) for r in con.execute(
            "SELECT n.uuid, c.uuid AS cliente_uuid, n.texto, n.criado_em, n.atualizado_em, n.excluido "
            "FROM notas n LEFT JOIN clientes c ON c.id = n.cliente_id ORDER BY n.uuid")]
        pes = [dict(r) for r in con.execute(
            "SELECT p.uuid, c.uuid AS cliente_uuid, p.nome, p.criado_em, p.atualizado_em, p.excluido "
            "FROM pessoas p LEFT JOIN clientes c ON c.id = p.cliente_id ORDER BY p.uuid")]
        apelidos = [dict(r) for r in con.execute("SELECT uuid, para FROM clientes_alias ORDER BY uuid")]
    return {"clientes": cli, "tarefas": tar, "notas": nts, "pessoas": pes, "apelidos": apelidos}


def _tarefa_limpa(t: dict) -> list:
    """Valores de uma tarefa vinda de outro PC, normalizados como o próprio sistema grava."""
    v = {c: t.get(c) for c in CAMPOS_TAREFA}
    v["descricao"] = str(v["descricao"] or "").strip() or "(sem descrição)"
    v["responsavel"] = str(v["responsavel"] or "")
    v["minha"] = int(bool(v["minha"]))
    v["prazo"] = _prazo_valido(v["prazo"]) if v["prazo"] else None
    v["status"] = v["status"] if v["status"] in STATUS else "a_fazer"
    v["excluido"] = int(bool(v["excluido"]))
    v["criada_em"] = v["criada_em"] or v["atualizado_em"] or agora()
    v["atualizado_em"] = v["atualizado_em"] or ""
    return [v[c] for c in CAMPOS_TAREFA]


def mesclar(remoto: dict) -> int:
    """Junta os dados vindos de outro PC: em cada registro vale a alteração mais recente.
    Devolve quantos registros locais mudaram."""
    if not isinstance(remoto, dict):
        return 0
    mudou = 0
    with conexao() as con:
        # 1) apelidos (uniões feitas no outro PC)
        for ap in remoto.get("apelidos", []) or []:
            if not ap.get("uuid") or not ap.get("para") or ap["uuid"] == ap["para"]:
                continue
            if con.execute("SELECT 1 FROM clientes_alias WHERE uuid = ? AND para = ?", (ap["uuid"], ap["para"])).fetchone():
                continue
            _apelidar(con, ap["uuid"], ap["para"])
            mudou += 1
            antigo = con.execute("SELECT * FROM clientes WHERE uuid = ?", (ap["uuid"],)).fetchone()
            if antigo:
                destino = con.execute("SELECT * FROM clientes WHERE uuid = ?", (_canonico(con, ap["para"]),)).fetchone()
                if destino:
                    _unir_registros(con, dict(antigo), dict(destino))
                else:
                    con.execute("UPDATE clientes SET uuid = ? WHERE id = ?", (_canonico(con, ap["para"]), antigo["id"]))

        # 2) clientes
        for c in sorted(remoto.get("clientes", []) or [], key=lambda x: str(x.get("uuid"))):
            if not c.get("uuid"):
                continue
            c = {**c, "uuid": _canonico(con, c["uuid"]), "nome": str(c.get("nome") or "").strip() or "(sem nome)",
                 "emails": _limpar_emails(c.get("emails")), "excluido": int(bool(c.get("excluido"))),
                 "atualizado_em": c.get("atualizado_em") or "", "criado_em": c.get("criado_em") or agora()}
            local = con.execute("SELECT * FROM clientes WHERE uuid = ?", (c["uuid"],)).fetchone()
            if local:
                mudou += _aplicar(con, dict(local), c)
                continue
            mesmo_nome = con.execute("SELECT * FROM clientes WHERE nome = ?", (c["nome"],)).fetchone()
            if not mesmo_nome:
                con.execute("INSERT INTO clientes (uuid, nome, emails, criado_em, atualizado_em, excluido) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (c["uuid"], c["nome"], c["emails"], c["criado_em"], c["atualizado_em"], c["excluido"]))
                mudou += 1
                continue
            # mesmo cliente com outro uuid: fica o menor, valem os dados mais recentes
            local = dict(mesmo_nome)
            canonico = min(local["uuid"], c["uuid"])
            novo = _mais_recente(local, c)
            con.execute("UPDATE clientes SET uuid = ?, nome = ?, emails = ?, excluido = ?, atualizado_em = ? WHERE id = ?",
                        (canonico, novo["nome"], novo.get("emails") or "", int(novo.get("excluido") or 0),
                         novo["atualizado_em"], local["id"]))
            _apelidar(con, c["uuid"] if canonico == local["uuid"] else local["uuid"], canonico)
            mudou += 1

        def id_local(cliente_uuid):
            if not cliente_uuid:
                return None
            r = con.execute("SELECT id FROM clientes WHERE uuid = ?", (_canonico(con, cliente_uuid),)).fetchone()
            return r["id"] if r else None

        # 3) tarefas
        for t in remoto.get("tarefas", []) or []:
            if not t.get("uuid"):
                continue
            local = con.execute("SELECT atualizado_em FROM tarefas WHERE uuid = ?", (t["uuid"],)).fetchone()
            valores = _tarefa_limpa(t)
            if not local:
                con.execute(f"INSERT INTO tarefas (uuid, cliente_id, {', '.join(CAMPOS_TAREFA)}) "
                            f"VALUES (?, ?, {', '.join('?' * len(CAMPOS_TAREFA))})",
                            (t["uuid"], id_local(t.get("cliente_uuid")), *valores))
                mudou += 1
            elif (t.get("atualizado_em") or "") > (local["atualizado_em"] or ""):
                con.execute(f"UPDATE tarefas SET cliente_id = ?, {', '.join(c + ' = ?' for c in CAMPOS_TAREFA)} "
                            "WHERE uuid = ?", (id_local(t.get("cliente_uuid")), *valores, t["uuid"]))
                mudou += 1

        # 4) anotações
        for n in remoto.get("notas", []) or []:
            if not n.get("uuid"):
                continue
            local = con.execute("SELECT atualizado_em FROM notas WHERE uuid = ?", (n["uuid"],)).fetchone()
            texto = str(n.get("texto") or "")
            if not local:
                con.execute("INSERT INTO notas (uuid, cliente_id, texto, criado_em, atualizado_em, excluido) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (n["uuid"], id_local(n.get("cliente_uuid")), texto,
                                                           n.get("criado_em") or agora(), n.get("atualizado_em") or "",
                                                           int(bool(n.get("excluido")))))
                mudou += 1
            elif (n.get("atualizado_em") or "") > (local["atualizado_em"] or ""):
                con.execute("UPDATE notas SET cliente_id = ?, texto = ?, atualizado_em = ?, excluido = ? WHERE uuid = ?",
                            (id_local(n.get("cliente_uuid")), texto, n["atualizado_em"], int(bool(n.get("excluido"))),
                             n["uuid"]))
                mudou += 1

        # 5) pessoas dos clientes
        for p in remoto.get("pessoas", []) or []:
            nome = " ".join(str(p.get("nome") or "").split())[:80]
            if not p.get("uuid") or not nome:
                continue
            cid = id_local(p.get("cliente_uuid"))
            local = con.execute("SELECT atualizado_em FROM pessoas WHERE uuid = ?", (p["uuid"],)).fetchone()
            if not local:
                # a mesma pessoa criada nos dois PCs (mesmo cliente e nome) fica uma só
                if con.execute("SELECT 1 FROM pessoas WHERE cliente_id IS ? AND nome = ? COLLATE NOCASE",
                               (cid, nome)).fetchone():
                    continue
                con.execute("INSERT INTO pessoas (uuid, cliente_id, nome, criado_em, atualizado_em, excluido) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (p["uuid"], cid, nome, p.get("criado_em") or agora(),
                                                           p.get("atualizado_em") or "", int(bool(p.get("excluido")))))
                mudou += 1
            elif (p.get("atualizado_em") or "") > (local["atualizado_em"] or ""):
                con.execute("UPDATE pessoas SET cliente_id = ?, nome = ?, atualizado_em = ?, excluido = ? WHERE uuid = ?",
                            (cid, nome, p["atualizado_em"], int(bool(p.get("excluido"))), p["uuid"]))
                mudou += 1
    return mudou


criar_tabelas()
