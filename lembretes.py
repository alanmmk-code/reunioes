"""Lembretes em segundo plano:

- Resumo pré-reunião: alguns minutos antes de cada reunião do Meet na agenda, avisa o que está
  pendente com o cliente e abre o resumo ao clicar na notificação.
- Aviso diário: uma vez por dia, quantas tarefas vencem hoje e quantas estão atrasadas.

Não usa o Claude (não gasta cota): só junta o que já está nas tarefas e atas.
"""

import json
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import config
import db
import google_services as g
import monitor

ESTADO_ARQ = config.BASE_DIR / "lembretes.json"  # o que já foi avisado neste PC


def _estado() -> dict:
    try:
        return json.loads(ESTADO_ARQ.read_text(encoding="utf-8"))
    except Exception:
        return {"aviso_diario": "", "briefings": {}}


def _salvar(estado: dict) -> None:
    # guarda só os avisos dos últimos dias
    limite = (date.today() - timedelta(days=3)).isoformat()
    estado["briefings"] = {k: v for k, v in estado["briefings"].items() if v >= limite}
    ESTADO_ARQ.write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")


def url(caminho: str) -> str:
    return f"http://localhost:{config.PORTA}{caminho}"


# ---------------------------------------------------------------- aviso diário

def aviso_diario(estado: dict, forcar: bool = False) -> bool:
    hoje = date.today().isoformat()
    if not forcar and (estado.get("aviso_diario") == hoje or datetime.now().strftime("%H:%M") < config.AVISO_DIARIO_HORA):
        return False
    minhas = db.tarefas(somente_minhas=True)
    atrasadas = [t for t in minhas if t["atrasada"]]
    de_hoje = [t for t in minhas if t["hoje"]]
    estado["aviso_diario"] = hoje
    if not atrasadas and not de_hoje and not forcar:
        return False  # nada urgente: não incomoda
    partes = []
    if de_hoje:
        partes.append(f"{len(de_hoje)} vence{'m' if len(de_hoje) > 1 else ''} hoje")
    if atrasadas:
        partes.append(f"{len(atrasadas)} atrasada{'s' if len(atrasadas) > 1 else ''}")
    destaque = (atrasadas + de_hoje)[0]["descricao"] if (atrasadas or de_hoje) else ""
    monitor.notificar(
        "Suas tarefas de hoje: " + (" e ".join(partes) if partes else "nada urgente"),
        (f"Ex.: {destaque[:90]}. " if destaque else "") + "Clique para abrir a lista.",
        url("/tarefas"),
    )
    return True


# ---------------------------------------------------------------- resumo pré-reunião

def resumo_cliente(cliente_id) -> dict:
    """Tudo o que importa sobre o cliente antes de uma reunião."""
    tarefas = db.tarefas(cliente_id=cliente_id) if cliente_id else []
    return {
        "minhas": [t for t in tarefas if t["minha"]],
        "do_cliente": [t for t in tarefas if not t["minha"]],
        "atrasadas": [t for t in tarefas if t["atrasada"] and t["minha"]],
    }


def briefings(estado: dict, sugerir_cliente) -> None:
    agora = datetime.now().astimezone()
    for r in g.listar_reunioes(dias_atras=0, dias_frente=1):
        if "T" not in r["inicio"] or r["id"] in estado["briefings"]:
            continue
        faltam = (datetime.fromisoformat(r["inicio"]) - agora).total_seconds() / 60
        if not (0 <= faltam <= config.BRIEFING_MINUTOS_ANTES):
            continue
        estado["briefings"][r["id"]] = date.today().isoformat()
        cli = db.cliente(sugerir_cliente(r))
        texto = f"{r['titulo']} em {max(1, round(faltam))} min"
        if cli:
            res = resumo_cliente(cli["id"])
            partes = [f"{len(res['minhas'])} pendência(s) sua(s)"]
            if res["atrasadas"]:
                partes[0] += f" ({len(res['atrasadas'])} atrasada(s))"
            if res["do_cliente"]:
                partes.append(f"{len(res['do_cliente'])} do cliente para cobrar")
            monitor.notificar(f"Reunião com {cli['nome']}: {texto}", ", ".join(partes) + ". Clique para ver o resumo.",
                              url(f"/resumo/{r['id']}"))
        else:
            monitor.notificar(f"Reunião: {texto}", "Clique para ver a pauta e o histórico.", url(f"/resumo/{r['id']}"))


# ---------------------------------------------------------------- ciclo

def iniciar(sugerir_cliente) -> None:
    def loop():
        time.sleep(20)  # deixa o painel subir
        while True:
            try:
                estado = _estado()
                aviso_diario(estado)
                briefings(estado, sugerir_cliente)
                _salvar(estado)
            except Exception as e:
                print(f"[lembretes] erro: {e}")
                traceback.print_exc()
            time.sleep(60)

    threading.Thread(target=loop, daemon=True, name="lembretes").start()
