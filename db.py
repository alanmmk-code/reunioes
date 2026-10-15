"""Banco local (SQLite) de clientes e tarefas/demandas.

Cada registro tem um `uuid` (igual em todos os PCs), `atualizado_em` e `excluido` (exclusão marcada,
não apagada), para que a sincronização pelo Google Drive consiga mesclar o que foi feito em cada PC.
"""

import sqlite3
import unicodedata
import uuid as _uuid
from contextlib import closing, contextmanager
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
        # descrição vazia grava como o mesclar normaliza (senão os PCs nunca ficam iguais); sem mexer no
        # atualizado_em, para cada PC corrigir a própria cópia sem disputar versão
        con.execute("UPDATE tarefas SET descricao = ? WHERE trim(coalesce(descricao, '')) = ''", (SEM_DESCRICAO,))
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
        # clientes que só diferem na caixa de letra acentuada ("Ação"/"AÇÃO") passaram pelo NOCASE: une
        chaves = [_chave_nome(r["nome"]) for r in con.execute("SELECT nome FROM clientes")]
        if len(set(chaves)) != len(chaves):
            _mesclar_clientes(con, [])
        _unificar_pessoas(con)


def copia_de_seguranca(manter: int = 14):
    """Uma cópia do banco por dia em copias/ (as últimas `manter`). Protege contra uma sincronia ruim,
    que chegaria também ao Drive. Usa a cópia do próprio SQLite: vale mesmo com o painel gravando."""
    pasta = ARQUIVO.parent / "copias"
    destino = pasta / f"dados-{date.today().isoformat()}.db"
    if destino.exists() or not ARQUIVO.exists():
        return None
    pasta.mkdir(exist_ok=True)
    provisorio = destino.with_suffix(".tmp")
    with closing(sqlite3.connect(ARQUIVO)) as origem, closing(sqlite3.connect(provisorio)) as copia:
        origem.backup(copia)
    provisorio.replace(destino)  # só aparece como cópia do dia depois de completa
    for velha in sorted(pasta.glob("dados-*.db"))[:-manter]:
        velha.unlink(missing_ok=True)
    return destino


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
    """Cliente que tem este e-mail cadastrado; senão, o que tem o domínio (@empresa.com).
    O e-mail exato ganha: a pessoa pode ser de uma empresa e atender o projeto de outro cliente."""
    email = str(email or "").strip().lower()
    if "@" not in email:
        return None
    dominio = "@" + email.split("@", 1)[1]
    todos = [(c["id"], [e.strip() for e in (c.get("emails") or "").split(",") if e.strip()]) for c in clientes()]
    for procurado in (email, dominio):
        for cid, cadastrados in todos:
            if procurado in cadastrados:
                return cid
    return None


def criar_cliente(nome, emails: str = "") -> int | None:
    """Cadastra o cliente. Se o nome já existe, é ele mesmo: volta a valer se estava excluído e
    ganha os e-mails novos (os que já tinha continuam)."""
    nome = str(nome or "").strip()
    if not nome:
        return None
    with conexao() as con:
        existente = _mesmo_nome(con, nome)
        if existente:
            juntos = _limpar_emails(", ".join(x for x in (existente["emails"], _limpar_emails(emails)) if x))
            if existente["excluido"] or juntos != (existente["emails"] or ""):
                con.execute("UPDATE clientes SET excluido = 0, emails = ?, atualizado_em = ? WHERE id = ?",
                            (juntos, agora(), existente["id"]))
            return existente["id"]
        momento = agora()
        return con.execute("INSERT INTO clientes (nome, criado_em, uuid, atualizado_em, emails) VALUES (?, ?, ?, ?, ?)",
                           (nome, momento, _uuid.uuid4().hex, momento, _limpar_emails(emails))).lastrowid


def _mesmo_nome(con, nome: str, exceto: int | None = None):
    """O cliente (excluído ou não) com este nome, sem diferenciar maiúsculas, inclusive acentuadas."""
    chave = _chave_nome(nome)
    for r in con.execute("SELECT id, nome, excluido, emails FROM clientes"):
        if r["id"] != exceto and _chave_nome(r["nome"]) == chave:
            return r
    return None


def excluir_cliente(cliente_id) -> bool:
    """Tira o cliente das telas (nos dois PCs, pela sincronia). Tarefas, anotações e pessoas ficam ligadas a
    ele e somem junto; cadastrar o mesmo nome de novo traz tudo de volta. As atas não são tocadas."""
    cliente_id = _id(cliente_id)
    if not cliente_id:
        return False
    with conexao() as con:
        return con.execute("UPDATE clientes SET excluido = 1, atualizado_em = ? WHERE id = ? AND excluido = 0",
                           (agora(), cliente_id)).rowcount == 1


def _nome_livre(con, nome: str, cliente_id: int) -> None:
    if _mesmo_nome(con, nome, exceto=cliente_id):
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


SEM_DESCRICAO = "(sem descrição)"


def _descricao(descricao) -> str:
    return str(descricao or "").strip() or SEM_DESCRICAO


def _cliente_existente(con, cliente_id) -> int | None:
    cliente_id = _id(cliente_id)
    if cliente_id and con.execute("SELECT 1 FROM clientes WHERE id = ? AND excluido = 0", (cliente_id,)).fetchone():
        return cliente_id
    return None


def criar_tarefa(cliente_id, descricao, responsavel="", minha=True, prazo=None, origem_ata=None, origem_titulo=None) -> int:
    momento = agora()
    with conexao() as con:
        return con.execute(
            "INSERT INTO tarefas (cliente_id, descricao, responsavel, minha, prazo, origem_ata, origem_titulo, "
            "criada_em, uuid, atualizado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_cliente_existente(con, cliente_id), _descricao(descricao), str(responsavel or "").strip(),
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
    if "descricao" in campos:
        campos["descricao"] = _descricao(campos["descricao"])
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
    # tarefa de cliente excluído some junto com ele (e volta se o cliente for cadastrado de novo)
    sql = ("SELECT t.*, c.nome AS cliente FROM tarefas t LEFT JOIN clientes c ON c.id = t.cliente_id "
           "WHERE t.excluido = 0 AND (c.id IS NULL OR c.excluido = 0)")
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


def _pessoa_no_cliente(con, cliente_id, nome: str):
    """A pessoa do cliente com este nome (sem diferenciar maiúsculas, inclusive acentuadas); a viva antes da excluída."""
    chave = _chave_nome(nome)
    achadas = [r for r in con.execute("SELECT id, uuid, nome, excluido FROM pessoas WHERE cliente_id IS ? "
                                      "ORDER BY excluido, uuid", (cliente_id,)) if _chave_nome(r["nome"]) == chave]
    return achadas[0] if achadas else None


def _unificar_pessoas(con) -> int:
    """A mesma pessoa duas vezes no cliente (veio de outro PC com outra caixa, ou dois clientes se uniram):
    fica a de menor uuid — escolha igual em todos os PCs — e as outras são excluídas."""
    grupos = {}
    for r in con.execute("SELECT id, uuid, cliente_id, nome FROM pessoas WHERE excluido = 0 AND cliente_id IS NOT NULL"):
        grupos.setdefault((r["cliente_id"], _chave_nome(r["nome"])), []).append(r)
    sobra = [r["id"] for g in grupos.values() if len(g) > 1 for r in sorted(g, key=lambda x: x["uuid"])[1:]]
    for pid in sobra:
        con.execute("UPDATE pessoas SET excluido = 1, atualizado_em = ? WHERE id = ?", (agora(), pid))
    return len(sobra)


def adicionar_pessoa(cliente_id, nome) -> bool:
    """Liga a pessoa ao cliente (sem duplicar: mesmo nome, sem diferenciar maiúsculas). True se é nova."""
    nome = " ".join(str(nome or "").split())[:80]
    c = cliente(cliente_id)
    if not nome or not c:
        return False
    with conexao() as con:
        existente = _pessoa_no_cliente(con, c["id"], nome)
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


def _chave_nome(nome: str) -> str:
    """Nomes que são o mesmo cliente: sem diferenciar maiúsculas, inclusive acentuadas ("Ação" = "AÇÃO").
    É mais largo que o UNIQUE COLLATE NOCASE do SQLite (só A-Z), então quem é igual para ele também é aqui."""
    return unicodedata.normalize("NFC", str(nome)).casefold()


def _mesclar_clientes(con, remotos: list[dict]) -> int:
    """Junta os clientes daqui com os de outro PC de uma vez só: primeiro a versão mais recente de cada uuid,
    depois une os que terminam com o mesmo nome. Como só o resultado final conta, a ordem não importa."""
    locais = {r["uuid"]: dict(r) for r in con.execute("SELECT * FROM clientes")}
    versoes = dict(locais)
    for c in remotos:
        atual = versoes.get(c["uuid"])
        versoes[c["uuid"]] = c if atual is None else _mais_recente(atual, c)

    grupos: dict[str, list[dict]] = {}
    for v in versoes.values():
        grupos.setdefault(_chave_nome(v["nome"]), []).append(v)

    plano = []  # (id local que fica ou None, valores finais, ids locais que saem)
    for membros in grupos.values():
        canonico = min(m["uuid"] for m in membros)
        dados = membros[0]
        for m in membros[1:]:
            dados = _mais_recente(dados, m)
        com_linha = sorted((m["uuid"] for m in membros if m["uuid"] in locais), key=lambda u: u != canonico)
        fica = locais[com_linha[0]]["id"] if com_linha else None
        sai = [locais[u]["id"] for u in com_linha[1:]]
        final = {"uuid": canonico, "nome": dados["nome"], "emails": dados.get("emails") or "",
                 "excluido": int(dados.get("excluido") or 0), "atualizado_em": dados.get("atualizado_em") or "",
                 "criado_em": dados.get("criado_em") or agora()}
        plano.append((fica, final, sai))
        for m in membros:
            _apelidar(con, m["uuid"], canonico)

    campos = ("uuid", "nome", "emails", "excluido", "atualizado_em")
    por_id = {c["id"]: c for c in locais.values()}
    mudam = [(fica, final) for fica, final, _ in plano
             if fica is not None and tuple(por_id[fica][k] or "" if k != "excluido" else int(por_id[fica][k] or 0)
                                            for k in campos) != tuple(final[k] for k in campos)]
    mudou = 0
    # nomes provisórios antes de renomear, para o UNIQUE não esbarrar no meio da troca
    for fica, final in mudam:
        con.execute("UPDATE clientes SET nome = ?, uuid = ? WHERE id = ?", ("" + str(fica), "" + str(fica), fica))
    for fica, _final, sai in plano:
        for velho in sai:
            for tabela in ("tarefas", "notas", "pessoas"):
                con.execute(f"UPDATE {tabela} SET cliente_id = ? WHERE cliente_id = ?", (fica, velho))
            con.execute("DELETE FROM clientes WHERE id = ?", (velho,))
            mudou += 1
    for fica, final in mudam:
        con.execute("UPDATE clientes SET uuid = ?, nome = ?, emails = ?, excluido = ?, atualizado_em = ? WHERE id = ?",
                    (*(final[k] for k in campos), fica))
        mudou += 1
    for fica, final, _sai in plano:
        if fica is None:
            con.execute("INSERT INTO clientes (uuid, nome, emails, criado_em, atualizado_em, excluido) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (final["uuid"], final["nome"], final["emails"], final["criado_em"],
                                                      final["atualizado_em"], final["excluido"]))
            mudou += 1
    return mudou


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
    v["descricao"] = _descricao(v["descricao"])
    v["responsavel"] = str(v["responsavel"] or "")
    v["minha"] = int(bool(v["minha"]))
    v["prazo"] = _prazo_valido(v["prazo"]) if v["prazo"] else None
    v["status"] = v["status"] if v["status"] in STATUS else "a_fazer"
    v["excluido"] = int(bool(v["excluido"]))
    v["criada_em"] = v["criada_em"] or v["atualizado_em"] or agora()
    v["atualizado_em"] = v["atualizado_em"] or ""
    v["concluida_em"] = (v["concluida_em"] or v["atualizado_em"] or v["criada_em"]) if v["status"] == "feito" else None
    return [v[c] for c in CAMPOS_TAREFA]


def _registros(remoto: dict, chave: str) -> list[dict]:
    """Os itens de uma lista do arquivo de outro PC que são registros com uuid (ignora o resto)."""
    lista = remoto.get(chave)
    if not isinstance(lista, list):
        return []
    return [r for r in lista if isinstance(r, dict) and r.get("uuid") and isinstance(r["uuid"], str)]


def mesclar(remoto: dict) -> int:
    """Junta os dados vindos de outro PC: em cada registro vale a alteração mais recente.
    Devolve quantos registros locais mudaram."""
    if not isinstance(remoto, dict):
        return 0
    mudou = 0
    with conexao() as con:
        # 1) apelidos (uniões feitas no outro PC)
        apelidos_remotos = {ap["uuid"]: ap.get("para") for ap in _registros(remoto, "apelidos")}
        conhecidos = {r["uuid"] for r in con.execute("SELECT uuid FROM clientes")} | {
            c["uuid"] for c in _registros(remoto, "clientes")}

        def termina_em_cliente(u):
            vistos = set()
            while u not in conhecidos and u not in vistos:
                vistos.add(u)
                r = con.execute("SELECT para FROM clientes_alias WHERE uuid = ?", (u,)).fetchone()
                u = r["para"] if r else apelidos_remotos.get(u)
                if not isinstance(u, str):
                    return False
            return u in conhecidos

        for ap in _registros(remoto, "apelidos"):
            # a união sempre aponta para o menor uuid; o contrário só viria de arquivo estragado e faria ciclo
            if not isinstance(ap.get("para"), str) or not ap["para"] or ap["para"] >= ap["uuid"]:
                continue
            if not termina_em_cliente(ap["para"]):  # apelido para um cliente que não existe em lugar nenhum
                continue
            # cada PC pode ter guardado um passo diferente da mesma cadeia: fica o menor (o mais perto do
            # cliente que ficou), senão os dois trocariam de apelido a cada sincronia
            atual = con.execute("SELECT para FROM clientes_alias WHERE uuid = ?", (ap["uuid"],)).fetchone()
            if atual and atual["para"] <= ap["para"]:
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
        remotos = []
        for c in _registros(remoto, "clientes"):
            remotos.append({"uuid": _canonico(con, c["uuid"]), "nome": str(c.get("nome") or "").strip() or "(sem nome)",
                            "emails": _limpar_emails(c.get("emails")), "excluido": int(bool(c.get("excluido"))),
                            "atualizado_em": c.get("atualizado_em") or "", "criado_em": c.get("criado_em") or agora()})
        mudou += _mesclar_clientes(con, remotos)
        # apelido aponta direto para o cliente que ficou (não para um passo do meio da cadeia): assim os
        # dois PCs guardam o mesmo, e mesclar de novo não muda nada
        for ap in con.execute("SELECT uuid, para FROM clientes_alias").fetchall():
            final = _canonico(con, ap["para"])
            if final != ap["para"]:
                con.execute("UPDATE clientes_alias SET para = ? WHERE uuid = ?", (final, ap["uuid"]))
                mudou += 1

        def id_local(cliente_uuid):
            if not cliente_uuid:
                return None
            r = con.execute("SELECT id FROM clientes WHERE uuid = ?", (_canonico(con, cliente_uuid),)).fetchone()
            return r["id"] if r else None

        # 3) tarefas
        for t in _registros(remoto, "tarefas"):
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
        for n in _registros(remoto, "notas"):
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
        for p in _registros(remoto, "pessoas"):
            nome = " ".join(str(p.get("nome") or "").split())[:80]
            if not nome:
                continue
            cid = id_local(p.get("cliente_uuid"))
            local = con.execute("SELECT atualizado_em FROM pessoas WHERE uuid = ?", (p["uuid"],)).fetchone()
            if not local:
                # a mesma pessoa criada nos dois PCs (mesmo cliente e nome) fica uma só
                if _pessoa_no_cliente(con, cid, nome):
                    continue
                con.execute("INSERT INTO pessoas (uuid, cliente_id, nome, criado_em, atualizado_em, excluido) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (p["uuid"], cid, nome, p.get("criado_em") or agora(),
                                                           p.get("atualizado_em") or "", int(bool(p.get("excluido")))))
                mudou += 1
            elif (p.get("atualizado_em") or "") > (local["atualizado_em"] or ""):
                con.execute("UPDATE pessoas SET cliente_id = ?, nome = ?, atualizado_em = ?, excluido = ? WHERE uuid = ?",
                            (cid, nome, p["atualizado_em"], int(bool(p.get("excluido"))), p["uuid"]))
                mudou += 1
        # uniões de clientes e versões de outro PC podem ter juntado a mesma pessoa duas vezes no cliente
        mudou += _unificar_pessoas(con)
    return mudou


criar_tabelas()
