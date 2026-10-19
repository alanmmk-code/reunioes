"""Casos de Service Desk (Linx) do cliente, para a ficha do cliente no painel.

Os dados vêm de Meu Drive/MSX/orcamentos_sd.json, que o projeto "Service Desk Linx" (planilha de controle dos
orçamentos) exporta a cada hora. Ficam só no Google Drive — nada de cliente entra neste repositório, que é público.
Outro caminho: SD_ORCAMENTOS no .env. Sem o arquivo, a seção simplesmente não aparece.
"""

import json
import os
import re
import unicodedata
from pathlib import Path

ORDEM = ["Em execução", "Agendado", "Aprovado", "Pendente", "Rascunho", "Enviado", "Concluído", "Reprovado",
         "Cancelado"]
ABERTOS = {"Em execução", "Agendado", "Aprovado", "Pendente", "Rascunho", "Enviado"}
_IGNORAR = {"LTDA", "MATRIZ", "FILIAL", "LOJA", "COMERCIO", "SERVICOS", "INDUSTRIA", "SA", "ME", "EPP"}


def _arquivo() -> Path | None:
    if os.getenv("SD_ORCAMENTOS"):
        return Path(os.getenv("SD_ORCAMENTOS"))
    for letra in "DEFGHIJKLMNOPQRSTUVWXYZ":
        for nome in ("Meu Drive", "My Drive"):
            base = Path(f"{letra}:/{nome}")
            if base.is_dir():
                return base / "MSX" / "orcamentos_sd.json"
    return None


def _palavras(texto: str) -> set[str]:
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper()
    return {p for p in re.sub(r"[^A-Z0-9 ]", " ", t).split() if len(p) >= 3 and p not in _IGNORAR}


def mesmo_cliente(a: str, b: str) -> bool:
    """'LOJA EXEMPLO - (MATRIZ)' ~ 'Loja Exemplo', 'EXEMPLO MODAS LTDA' ~ 'Exemplo Modas'."""
    return bool(_palavras(a) & _palavras(b))


def _brl(v) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def ler() -> dict:
    arq = _arquivo()
    try:
        return json.loads(arq.read_text(encoding="utf-8")) if arq else {}
    except (OSError, ValueError):
        return {}


def casos_do_cliente(nome: str, dados: dict | None = None) -> dict:
    """{'casos': [...], 'atualizado': 'AAAA-MM-DDTHH:MM'} com os casos do cliente, os em andamento primeiro."""
    dados = ler() if dados is None else dados
    itens = [i for i in dados.get("itens", []) if mesmo_cliente(i.get("cliente"), nome)]
    casos = []
    for i in sorted(itens, key=lambda i: (ORDEM.index(i["status"]) if i.get("status") in ORDEM else 99,
                                           -(int(re.sub(r"\D", "", i.get("pedido") or "0") or 0)))):
        valor = (_brl(i["repasse"]) if i.get("repasse")
                 else f"≈ {_brl(i['estimado'])} estimado" if i.get("estimado") else "")
        casos.append({
            "caso": i.get("caso") or "projeto direto", "solicitacao": i.get("solicitacao", ""),
            "status": i.get("status", ""), "aberto": i.get("status") in ABERTOS,
            "horas": f"{i['horas']:g}".replace(".", ",") if i.get("horas") else None,
            "analista": i.get("analista"), "chamado": i.get("chamado"),
            "proxima": i.get("proximaReuniao"), "valor": valor,
        })
    return {"casos": casos, "atualizado": dados.get("atualizado")}
