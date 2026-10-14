"""Frente do Agenor (Reunioes).  Use SEMPRE o venv:  .venv\\Scripts\\python.exe testes\\agenor\\frente.py ...

  --passos N        passos por semente (padrao 600)
  --sementes K      quantas sementes (padrao 10)
  --semente S       repete aquela semente (com --falar imprime passo a passo)
  --reduzir         reduz cada achado ao caso minimo
  --cobertura       o que o catalogo ainda nao sabe testar
  --corpus          roda as sementes guardadas em sementes/ (regressao; tem de passar)
  --guardar-aberto  grava o caso minimo em sementes/abertos/
"""
import argparse
import json
import sys
import time
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))

import ambiente as amb  # noqa: E402
import acoes as A  # noqa: E402
import acoes2 as A2  # noqa: E402
import acoes3 as A3  # noqa: E402
import nucleo as N  # noqa: E402


def cobertura():
    rotas = sorted({str(r.rule) for r in amb.reunioes.app.url_map.iter_rules() if r.endpoint != "static"})
    cobertas = {c for a in A3.ACOES3 for c in a.cobre}
    if "*" in cobertas:  # o fuzz generico sorteia qualquer rota do url_map, menos as de SEM_FUZZ
        cobertas |= {r for r in [str(x.rule) for x in amb.reunioes.app.url_map.iter_rules()] if not r.startswith(A.SEM_FUZZ)}
    funcs = sorted(n for n in dir(amb.db) if not n.startswith("_") and callable(getattr(amb.db, n))
                   and getattr(getattr(amb.db, n), "__module__", "") == "db" and n not in ("conexao", "agora", "criar_tabelas"))
    print(f"catalogo v3: {len(A3.ACOES3)} acoes ({sum(a.hostil for a in A3.ACOES3)} hostis, {sum(not a.hostil for a in A3.ACOES3)} legitimas)")
    sem_rota = [r for r in rotas if r not in cobertas]
    print("rotas sem acao:", sem_rota or "nenhuma")
    print("funcoes de db sem acao:", [f for f in funcs if "db." + f not in cobertas] or "nenhuma")
    print("fora do alcance (por que):")
    for k, v in A3.SEM_ACAO.items():
        print(f"  {k}: {v}")
    ref = AQUI / "cobertura.json"
    atual = {"rotas": rotas, "funcoes": funcs}
    if ref.exists():
        antes = json.loads(ref.read_text(encoding="utf-8"))
        novas = [x for x in rotas + funcs if x not in antes["rotas"] + antes["funcoes"]]
        print("NOVO desde a ultima medicao:", novas or "nada")
    ref.write_text(json.dumps(atual, indent=1), encoding="utf-8")


def corpus():
    pasta = AQUI / "sementes"
    arqs = sorted(pasta.glob("*.json"))
    if not arqs:
        print("corpus vazio (regressao so guarda o que ja foi consertado)")
    ruim = 0
    for a in arqs:
        d = json.loads(a.read_text(encoding="utf-8"))
        cat = d.get("catalogo", "v1")
        if d.get("carimbo") != N.carimbo(cat):
            print(f"AVISO {a.name}: o catalogo {cat} mudou desde a captura; passar aqui nao prova o conserto")
        ach, _ = N.executar(d["semente"], d["passos"], catalogo=cat)
        print(("FALHOU " if ach else "ok     ") + a.name, list(ach)[:3])
        ruim += bool(ach)
    abertos = sorted((pasta / "abertos").glob("*.json"))
    print(f"{len(abertos)} achado(s) aberto(s) em sementes/abertos/ (relatados, nao reprovam)")
    return ruim


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--passos", type=int, default=600)
    ap.add_argument("--sementes", type=int, default=10)
    ap.add_argument("--semente")
    ap.add_argument("--base", type=int, default=1000, help="primeira semente numerica")
    ap.add_argument("--catalogo", default="v3", choices=["v1", "v2", "v3"])
    ap.add_argument("--falar", action="store_true")
    ap.add_argument("--reduzir", action="store_true")
    ap.add_argument("--so", default="", help="so reduz/mostra chaves que contenham este texto")
    ap.add_argument("--cobertura", action="store_true")
    ap.add_argument("--corpus", action="store_true")
    ap.add_argument("--guardar-aberto", action="store_true")
    a = ap.parse_args()
    N.ATIVO["nome"] = a.catalogo
    try:
        if a.cobertura:
            return cobertura()
        if a.corpus:
            return corpus()
        print(f"alvo: {amb.ALVO}")
        sementes = [a.semente] if a.semente else [str(a.base + k) for k in range(a.sementes)]
        todos, usados, t0 = {}, {}, time.time()
        for s in sementes:
            ach, vistos = N.executar(s, range(a.passos), falar=a.falar)
            for k, v in vistos.items():
                usados[k] = usados.get(k, 0) + v
            for k, v in ach.items():
                todos.setdefault(k, {**v, "sementes": [], "semente": s})["sementes"].append(s)
            print(f"semente {s}: {len(ach)} achado(s)")
        print(f"\n{len(sementes)} sementes x {a.passos} passos em {time.time() - t0:.0f}s; {len(todos)} chave(s) distinta(s)")
        sem_uso = [x.nome for x in N.CATALOGOS[a.catalogo] if x.nome not in usados]
        print("acoes nunca sorteadas:", sem_uso or "nenhuma")
        for k, v in sorted(todos.items(), key=lambda kv: kv[1]["classe"]):
            if a.so and a.so not in k:
                continue
            print(f"\n== {v['classe']}  [{k}]  em {len(v['sementes'])} semente(s) ({'hostil' if v['hostil'] else 'legitima'}: {v['acao']})")
            print("   ", v["msg"][:300])
            if a.reduzir:
                ind = N.reduzir(v["semente"], v["passo"], k)
                print(f"   semente {v['semente']}, passo {v['passo']} -> reduzido a {len(ind)} passo(s) {ind}")
                for l in N.descrever(v["semente"], ind):
                    print("   " + l)
                if a.guardar_aberto:
                    nome = "".join(c if c.isalnum() else "_" for c in k)[:60]
                    (AQUI / "sementes" / "abertos" / f"{nome}.json").write_text(json.dumps(
                        {"semente": v["semente"], "passos": ind, "chave": k, "catalogo": a.catalogo, "carimbo": N.carimbo(), "msg": v["msg"]}, indent=1, ensure_ascii=False), encoding="utf-8")
        return 1 if todos else 0
    finally:
        amb.limpar()


if __name__ == "__main__":
    sys.exit(main())
