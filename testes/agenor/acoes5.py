"""Catalogo v5 = catalogo v4 (acoes4.py, CONGELADO) + pedidos vindos de outro site (CSRF e DNS rebinding)."""
import acoes4 as A4
import ambiente as amb
import oraculo as o
from acoes import Acao, _clientes, chamar
from oraculo import cobrar

ORIGENS_HOSTIS = ["https://evil.example", "null", "http://localhost:6000", "https://localhost:5055", "http://127.0.0.1.evil.example"]


def _foto():
    return ({t: o.linhas(t) for t in ("clientes", "tarefas", "notas", "pessoas")},
            len(amb.ENVIOS), len(amb.EMAILS), len(amb.REMARCADOS))


def f_post_de_outro_site(m, r):
    """Qualquer POST do painel vindo de outro site e recusado e nao muda nada."""
    m.pc(r)
    regras = sorted(str(x.rule) for x in amb.reunioes.app.url_map.iter_rules() if "POST" in x.methods)
    cl = _clientes()
    ids = [str(c["id"]) for c in cl] + ["1", "2"]
    rota = r.choice(regras)
    caminho = rota
    for marca in ("<int:cliente_id>", "<int:nota_id>", "<int:tarefa_id>", "<int:envio_id>", "<event_id>", "<pid>", "<acao>", "<int:id>"):
        caminho = caminho.replace(marca, r.choice(ids + ["ev1", "dispensar"]))
    antes = _foto()
    origem = r.choice(ORIGENS_HOSTIS)
    pc, resp = chamar(m, r, "post", caminho, data={"nome": "Invasor", "texto": "x", "emails": "a@b.com"}, headers={"Origin": origem})
    cobrar(resp.status_code == 403, "I28-post-de-outro-site-aceito", f"{pc}: POST {caminho} com Origin {origem} -> {resp.status_code}")
    cobrar(_foto() == antes, "I28-post-de-outro-site-mudou-algo", f"{pc}: POST {caminho} com Origin {origem} alterou dados")
    return f"{pc} POST {caminho} de {origem}"


def f_get_dns_rebinding(m, r):
    m.pc(r)
    caminho = r.choice(["/", "/clientes", "/tarefas", "/semana", "/api/aovivo"])
    host = r.choice(["evil.example:5055", "127.0.0.1.nip.io:5055", "localhost.evil.example"])
    pc, resp = chamar(m, r, "get", caminho, headers={"Host": host})
    cobrar(resp.status_code == 403, "I29-dns-rebinding-le-pagina", f"{pc}: GET {caminho} com Host {host} -> {resp.status_code}")
    return f"{pc} GET {caminho} Host {host}"


ACOES5 = A4.ACOES4 + [
    Acao("POST vindo de outro site", f_post_de_outro_site, 3, True, ["*"]),
    Acao("GET com Host estranho (DNS rebinding)", f_get_dns_rebinding, 1, True, ["*"]),
]
SEM_ACAO = A4.SEM_ACAO
SEM_FUZZ = A4.SEM_FUZZ
