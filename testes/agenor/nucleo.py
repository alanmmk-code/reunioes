"""O corredor: sorteia, executa, mede, reduz. Nao conhece nenhuma acao pelo nome.

Semente: Random(f"{semente}:{indice}"), uma por passo -> retirar um passo nao embaralha os outros.
"""
import hashlib
import inspect
import random
import traceback

import acoes as A
import acoes2 as A2
import acoes3 as A3
import acoes4 as A4
import acoes5 as A5
import acoes6 as A6
import acoes7 as A7
import acoes8 as A8
import ambiente as amb
import oraculo as o


# Catalogos: "v1" = acoes.py CONGELADO (as sementes antigas sao este passeio); "v2" = v1 + acoes2.py.
CATALOGOS = {"v1": A.ACOES, "v2": A2.ACOES2, "v3": A3.ACOES3, "v4": A4.ACOES4, "v5": A5.ACOES5, "v6": A6.ACOES6, "v7": A7.ACOES7, "v8": A8.ACOES8}
ATIVO = {"nome": "v8"}


def escolher(semente, i, catalogo=None):
    lista = CATALOGOS[catalogo or ATIVO["nome"]]
    r = random.Random(f"{semente}:{i}")
    ac = r.choices(lista, weights=[a.peso for a in lista])[0]
    return ac, r


def _lugar(exc):
    return o.lugar(exc)


def executar(semente, indices, falar=False, parar_em=None, medir_estado=True, catalogo=None):
    """Roda os passos `indices` num mundo novo. Devolve (achados, vistos): achados = {chave: dict}."""
    cli = amb.novo_mundo(semente)
    m = A.Mundo(cli)
    achados, vistos = {}, {}
    for i in indices:
        ac, r = escolher(semente, i, catalogo)
        vistos[ac.nome] = vistos.get(ac.nome, 0) + 1
        try:
            det = ac.fn(m, r)
            if falar:
                print(f"  {i:5d} {ac.nome:34s} {det}")
            if medir_estado:
                o.estado(m)
        except o.Achado as e:
            achados.setdefault(e.chave, {"classe": e.classe, "msg": e.msg, "passo": i, "acao": ac.nome, "hostil": ac.hostil})
        except Exception as e:  # excecao nao prevista dentro de uma acao legitima do banco
            lugar = _lugar(e)
            chave = f"X:{ac.nome}:{type(e).__name__}:{lugar}"
            achados.setdefault(chave, {"classe": "X-acao-estoura", "msg": f"{type(e).__name__}: {e} [{lugar}]", "passo": i,
                                       "acao": ac.nome, "hostil": ac.hostil})
        if parar_em and parar_em in achados:
            break
    return achados, vistos


def reduzir(semente, ate, chave, catalogo=None):
    """ddmin sobre os indices 0..ate: o menor subconjunto que ainda reproduz a mesma chave."""
    def quebra(ind):
        a, _ = executar(semente, ind, parar_em=chave, medir_estado=chave.startswith('E'), catalogo=catalogo)
        return chave in a

    atual = list(range(ate + 1))
    n = 2
    while len(atual) >= 2:
        tam = max(1, len(atual) // n)
        blocos = [atual[k:k + tam] for k in range(0, len(atual), tam)]
        reduziu = False
        for b in blocos:
            resto = [x for x in atual if x not in b]
            if resto and quebra(resto):
                atual, n, reduziu = resto, max(n - 1, 2), True
                break
        if not reduziu:
            if n >= len(atual):
                break
            n = min(len(atual), n * 2)
    return atual


def descrever(semente, indices, catalogo=None):
    cli = amb.novo_mundo(semente)
    m = A.Mundo(cli)
    linhas = []
    for i in indices:
        ac, r = escolher(semente, i, catalogo)
        try:
            det = ac.fn(m, r)
            o.estado(m)
            linhas.append(f"  [{i}] {ac.nome}: {det}")
        except o.Achado as e:
            linhas.append(f"  [{i}] {ac.nome}: >>> {e.classe}: {e.msg}")
            break
        except Exception as e:
            linhas.append(f"  [{i}] {ac.nome}: >>> {type(e).__name__}: {e} [{_lugar(e)}]")
            break
    return linhas


def carimbo(catalogo=None):
    """Hash do catalogo de geradores: se muda, a semente guardada deixou de ser o mesmo passeio."""
    cat = catalogo or ATIVO["nome"]
    ordem = ["v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8"]
    modulos = [A, A2, A3, A4, A5, A6, A7, A8]
    fonte = "".join(inspect.getsource(mod) for mod in modulos[:ordem.index(cat) + 1])
    return hashlib.sha1(fonte.encode("utf-8")).hexdigest()[:12]
