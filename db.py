"""Banco local (SQLite) de clientes e tarefas/demandas."""

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime

import config

ARQUIVO = config.BASE_DIR / "dados.db"
STATUS = ("a_fazer", "fazendo", "feito")


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


# ---------------------------------------------------------------- clientes

def clientes() -> list[dict]:
    with conexao() as con:
        return [dict(r) for r in con.execute("SELECT * FROM clientes ORDER BY nome")]


def cliente(cliente_id) -> dict | None:
    if not cliente_id:
        return None
    with conexao() as con:
        r = con.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
        return dict(r) if r else None


def criar_cliente(nome: str) -> int:
    nome = nome.strip()
    with conexao() as con:
        existente = con.execute("SELECT id FROM clientes WHERE nome = ?", (nome,)).fetchone()
        if existente:
            return existente["id"]
        return con.execute("INSERT INTO clientes (nome, criado_em) VALUES (?, ?)",
                           (nome, datetime.now().isoformat(timespec="seconds"))).lastrowid


def renomear_cliente(cliente_id: int, nome: str) -> None:
    with conexao() as con:
        con.execute("UPDATE clientes SET nome = ? WHERE id = ?", (nome.strip(), cliente_id))


# ---------------------------------------------------------------- tarefas

def _prazo_valido(prazo: str | None) -> str | None:
    try:
        return date.fromisoformat((prazo or "").strip()[:10]).isoformat()
    except ValueError:
        return None  # "A definir" e afins


def criar_tarefa(cliente_id, descricao, responsavel="", minha=True, prazo=None, origem_ata=None, origem_titulo=None) -> int:
    with conexao() as con:
        return con.execute(
            "INSERT INTO tarefas (cliente_id, descricao, responsavel, minha, prazo, origem_ata, origem_titulo, criada_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cliente_id or None, descricao.strip(), responsavel.strip(), int(bool(minha)), _prazo_valido(prazo),
             origem_ata, origem_titulo, datetime.now().isoformat(timespec="seconds")),
        ).lastrowid


def importar_acoes_da_ata(reuniao: dict, ata, cliente_id) -> int:
    """Cria as tarefas a partir das ações da ata (substitui as de uma importação anterior da mesma ata)."""
    with conexao() as con:
        con.execute("DELETE FROM tarefas WHERE origem_ata = ? AND status != 'feito'", (reuniao["id"],))
    for a in ata.acoes:
        criar_tarefa(cliente_id, a.tarefa, a.responsavel, a.do_usuario, a.prazo, reuniao["id"], ata.titulo)
    return len(ata.acoes)


def mover_tarefas_da_ata(event_id: str, cliente_id) -> None:
    with conexao() as con:
        con.execute("UPDATE tarefas SET cliente_id = ? WHERE origem_ata = ?", (cliente_id or None, event_id))


def atualizar_tarefa(tarefa_id: int, **campos) -> None:
    permitidos = {"descricao", "responsavel", "minha", "prazo", "status", "cliente_id"}
    campos = {k: v for k, v in campos.items() if k in permitidos}
    if "prazo" in campos:
        campos["prazo"] = _prazo_valido(campos["prazo"])
    if "status" in campos:
        if campos["status"] not in STATUS:
            return
        campos["concluida_em"] = datetime.now().isoformat(timespec="seconds") if campos["status"] == "feito" else None
    if not campos:
        return
    with conexao() as con:
        con.execute(f"UPDATE tarefas SET {', '.join(f'{k} = ?' for k in campos)} WHERE id = ?",
                    (*campos.values(), tarefa_id))


def excluir_tarefa(tarefa_id: int) -> None:
    with conexao() as con:
        con.execute("DELETE FROM tarefas WHERE id = ?", (tarefa_id,))


def tarefas(somente_minhas=False, incluir_feitas=False, cliente_id=None) -> list[dict]:
    sql = ("SELECT t.*, c.nome AS cliente FROM tarefas t LEFT JOIN clientes c ON c.id = t.cliente_id WHERE 1=1")
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
    return linhas


criar_tabelas()
