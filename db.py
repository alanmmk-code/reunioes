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
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_clientes_uuid ON clientes(uuid)")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_tarefas_uuid ON tarefas(uuid)")


# ---------------------------------------------------------------- clientes

def clientes() -> list[dict]:
    with conexao() as con:
        return [dict(r) for r in con.execute("SELECT * FROM clientes WHERE excluido = 0 ORDER BY nome")]


def cliente(cliente_id) -> dict | None:
    if not cliente_id:
        return None
    with conexao() as con:
        r = con.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
        return dict(r) if r else None


def cliente_por_uuid(cliente_uuid) -> dict | None:
    if not cliente_uuid:
        return None
    with conexao() as con:
        r = con.execute("SELECT * FROM clientes WHERE uuid = ?", (cliente_uuid,)).fetchone()
        return dict(r) if r else None


def criar_cliente(nome: str) -> int:
    nome = nome.strip()
    with conexao() as con:
        existente = con.execute("SELECT id, excluido FROM clientes WHERE nome = ?", (nome,)).fetchone()
        if existente:
            if existente["excluido"]:
                con.execute("UPDATE clientes SET excluido = 0, atualizado_em = ? WHERE id = ?", (agora(), existente["id"]))
            return existente["id"]
        momento = agora()
        return con.execute("INSERT INTO clientes (nome, criado_em, uuid, atualizado_em) VALUES (?, ?, ?, ?)",
                           (nome, momento, _uuid.uuid4().hex, momento)).lastrowid


def renomear_cliente(cliente_id: int, nome: str) -> None:
    with conexao() as con:
        con.execute("UPDATE clientes SET nome = ?, atualizado_em = ? WHERE id = ?", (nome.strip(), agora(), cliente_id))


# ---------------------------------------------------------------- tarefas

def _prazo_valido(prazo: str | None) -> str | None:
    try:
        return date.fromisoformat((prazo or "").strip()[:10]).isoformat()
    except ValueError:
        return None  # "A definir" e afins


def criar_tarefa(cliente_id, descricao, responsavel="", minha=True, prazo=None, origem_ata=None, origem_titulo=None) -> int:
    momento = agora()
    with conexao() as con:
        return con.execute(
            "INSERT INTO tarefas (cliente_id, descricao, responsavel, minha, prazo, origem_ata, origem_titulo, "
            "criada_em, uuid, atualizado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cliente_id or None, descricao.strip(), responsavel.strip(), int(bool(minha)), _prazo_valido(prazo),
             origem_ata, origem_titulo, momento, _uuid.uuid4().hex, momento),
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
                    (cliente_id or None, agora(), event_id))


def atualizar_tarefa(tarefa_id: int, **campos) -> None:
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
        con.execute(f"UPDATE tarefas SET {', '.join(f'{k} = ?' for k in campos)} WHERE id = ?",
                    (*campos.values(), tarefa_id))


def excluir_tarefas_da_ata(event_id: str) -> int:
    """Exclui (marcando) as tarefas em aberto que vieram de uma reunião. Devolve quantas."""
    with conexao() as con:
        return con.execute("UPDATE tarefas SET excluido = 1, atualizado_em = ? "
                           "WHERE origem_ata = ? AND status != 'feito' AND excluido = 0", (agora(), event_id)).rowcount


def excluir_tarefa(tarefa_id: int) -> None:
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
    if cliente_id:
        sql += " AND t.cliente_id = ?"
        args.append(cliente_id)
    sql += " ORDER BY t.status = 'feito', t.prazo IS NULL, t.prazo, t.id"
    hoje = date.today().isoformat()
    with conexao() as con:
        linhas = [dict(r) for r in con.execute(sql, args)]
    for t in linhas:
        t["atrasada"] = bool(t["prazo"] and t["prazo"] < hoje and t["status"] != "feito")
        t["hoje"] = t["prazo"] == hoje
        t["dias"] = (date.fromisoformat(t["prazo"]) - date.today()).days if t["prazo"] else None
    return linhas


# ---------------------------------------------------------------- sincronização

def exportar() -> dict:
    """Tudo (inclusive excluídos), com o cliente referenciado pelo uuid — igual em todos os PCs."""
    with conexao() as con:
        cli = [dict(r) for r in con.execute("SELECT uuid, nome, criado_em, atualizado_em, excluido FROM clientes ORDER BY uuid")]
        tar = [dict(r) for r in con.execute(
            f"SELECT t.uuid, c.uuid AS cliente_uuid, {', '.join('t.' + c for c in CAMPOS_TAREFA)} "
            "FROM tarefas t LEFT JOIN clientes c ON c.id = t.cliente_id ORDER BY t.uuid")]
    return {"clientes": cli, "tarefas": tar}


def mesclar(remoto: dict) -> int:
    """Junta os dados vindos de outro PC: em cada registro vale a alteração mais recente.
    Devolve quantos registros locais mudaram."""
    mudou = 0
    with conexao() as con:
        for c in remoto.get("clientes", []):
            local = con.execute("SELECT * FROM clientes WHERE uuid = ?", (c["uuid"],)).fetchone()
            if not local:
                # Mesmo cliente criado separadamente nos dois PCs: une pelo nome e fica com um uuid só
                local = con.execute("SELECT * FROM clientes WHERE nome = ?", (c["nome"],)).fetchone()
                if local:
                    if c["uuid"] < local["uuid"]:
                        con.execute("UPDATE clientes SET uuid = ? WHERE id = ?", (c["uuid"], local["id"]))
                        mudou += 1
                    continue
                con.execute("INSERT INTO clientes (uuid, nome, criado_em, atualizado_em, excluido) VALUES (?, ?, ?, ?, ?)",
                            (c["uuid"], c["nome"], c["criado_em"], c["atualizado_em"], c["excluido"]))
                mudou += 1
            elif (c["atualizado_em"] or "") > (local["atualizado_em"] or ""):
                con.execute("UPDATE clientes SET nome = ?, atualizado_em = ?, excluido = ? WHERE id = ?",
                            (c["nome"], c["atualizado_em"], c["excluido"], local["id"]))
                mudou += 1

        ids = {r["uuid"]: r["id"] for r in con.execute("SELECT id, uuid FROM clientes")}
        nomes_remotos = {c["uuid"]: c["nome"] for c in remoto.get("clientes", [])}
        ids_por_nome = {r["nome"].lower(): r["id"] for r in con.execute("SELECT id, nome FROM clientes")}

        def id_local(cliente_uuid):
            if not cliente_uuid:
                return None
            return ids.get(cliente_uuid) or ids_por_nome.get((nomes_remotos.get(cliente_uuid) or "").lower())

        for t in remoto.get("tarefas", []):
            local = con.execute("SELECT atualizado_em FROM tarefas WHERE uuid = ?", (t["uuid"],)).fetchone()
            valores = [t[c] for c in CAMPOS_TAREFA]
            if not local:
                con.execute(f"INSERT INTO tarefas (uuid, cliente_id, {', '.join(CAMPOS_TAREFA)}) "
                            f"VALUES (?, ?, {', '.join('?' * len(CAMPOS_TAREFA))})",
                            (t["uuid"], id_local(t["cliente_uuid"]), *valores))
                mudou += 1
            elif (t["atualizado_em"] or "") > (local["atualizado_em"] or ""):
                con.execute(f"UPDATE tarefas SET cliente_id = ?, {', '.join(c + ' = ?' for c in CAMPOS_TAREFA)} "
                            "WHERE uuid = ?", (id_local(t["cliente_uuid"]), *valores, t["uuid"]))
                mudou += 1
    return mudou


criar_tabelas()
