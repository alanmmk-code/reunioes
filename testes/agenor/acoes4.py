"""Catalogo v4 = catalogo v3 (acoes3.py, CONGELADO) + excluir cliente pela tela (e trazer de volta recadastrando)."""
import acoes3 as A3
import db
import oraculo as o
from acoes import Acao, _clientes, chamar
from oraculo import cobrar


def f_excluir_cliente(m, r):
    m.pc(r)
    vivos = [c for c in _clientes() if not c["excluido"]]
    if not vivos:
        return "sem cliente"
    c = r.choice(vivos)
    pc, resp = chamar(m, r, "post", f"/clientes/{c['id']}/excluir")
    cobrar(db.cliente(c["id"])["excluido"] == 1, "I26-excluir_cliente", f"{pc}: {c['nome']!r} nao ficou excluido")
    cobrar(all(x["id"] != c["id"] for x in db.clientes()), "I26-excluir_cliente-listado", f"{pc}: {c['nome']!r} segue em clientes()")
    cobrar(chamar(m, r, "get", f"/cliente/{c['id']}")[1].status_code == 302, "I26-excluir_cliente-ficha", f"{pc}: ficha do excluido abre")
    return f"{pc} exclui {c['nome']!r}"


def f_recadastrar_excluido(m, r):
    """Cadastrar de novo o nome de um cliente excluido traz ele (e as tarefas abertas dele) de volta."""
    m.pc(r)
    mortos = [c for c in _clientes() if c["excluido"]]
    if not mortos:
        return "sem excluido"
    c = r.choice(mortos)
    dele = {t["id"] for t in o.linhas("tarefas") if t["cliente_id"] == c["id"] and not t["excluido"] and t["status"] != "feito"}
    pc, resp = chamar(m, r, "post", "/clientes/novo", data={"nome": r.choice([c["nome"], c["nome"].upper(), c["nome"].lower()])})
    cobrar(db.cliente(c["id"])["excluido"] == 0, "I27-recadastrar-nao-voltou", f"{pc}: {c['nome']!r} segue excluido")
    cobrar(dele <= {t["id"] for t in db.tarefas()}, "I27-recadastrar-tarefas", f"{pc}: tarefas de {c['nome']!r} nao voltaram")
    return f"{pc} recadastra {c['nome']!r}"


ACOES4 = A3.ACOES3 + [
    Acao("excluir cliente", f_excluir_cliente, 2, True, ["/clientes/<int:cliente_id>/excluir", "db.excluir_cliente"]),
    Acao("recadastrar cliente excluido", f_recadastrar_excluido, 2, True, ["/clientes/novo", "db.criar_cliente"]),
]
SEM_ACAO = A3.SEM_ACAO
SEM_FUZZ = A3.SEM_FUZZ
