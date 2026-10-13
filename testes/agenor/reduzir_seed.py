"""Uso: python reduzir_seed.py SEMENTE [PASSOS] [filtro,filtro]  -> roda, e reduz cada achado cuja chave contem um filtro."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ambiente as amb  # noqa: E402
import nucleo as N  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sem = sys.argv[1]
passos = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
filtros = (sys.argv[3] if len(sys.argv) > 3 else "").split(",")
try:
    ach, _ = N.executar(sem, range(passos))
    for k, v in ach.items():
        if any(f and f in k for f in filtros):
            ind = N.reduzir(sem, v["passo"], k)
            print(f"== {k}\n   {v['msg'][:300]}\n   semente {sem} passo {v['passo']} -> {len(ind)} passos {ind}")
            for l in N.descrever(sem, ind):
                print("  " + l)
finally:
    amb.limpar()
