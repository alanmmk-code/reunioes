"""Armazenamento local das atas e renderização em HTML (Google Doc e e-mail)."""

import json
from html import escape

import config
from analisador import Ata


def _arquivo(event_id: str):
    return config.ATAS_DIR / f"{event_id}.json"


def salvar(event_id: str, registro: dict) -> None:
    import db

    registro["atualizado_em"] = db.agora()  # usado para mesclar entre PCs
    cli = db.cliente(registro.get("cliente_id"))
    registro["cliente_uuid"] = cli["uuid"] if cli else None
    salvar_bruto(event_id, registro)


def salvar_bruto(event_id: str, registro: dict) -> None:
    _arquivo(event_id).write_text(json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8")


def carregar(event_id: str) -> dict | None:
    arq = _arquivo(event_id)
    return json.loads(arq.read_text(encoding="utf-8")) if arq.exists() else None


def existentes() -> set[str]:
    return {p.stem for p in config.ATAS_DIR.glob("*.json")}


def _lista(itens: list[str]) -> str:
    if not itens:
        return "<p><i>Nenhum.</i></p>"
    return "<ul>" + "".join(f"<li>{escape(i)}</li>" for i in itens) + "</ul>"


def para_html(ata: Ata, reuniao: dict) -> str:
    acoes = "".join(
        f"<tr><td>{escape(a.tarefa)}</td><td>{escape(a.responsavel)}</td><td>{escape(a.prazo)}</td></tr>"
        for a in ata.acoes
    ) or '<tr><td colspan="3"><i>Nenhuma ação registrada.</i></td></tr>'
    topicos = "".join(f"<h3>{escape(t.titulo)}</h3><p>{escape(t.discussao)}</p>" for t in ata.topicos)
    prox = ata.proxima_reuniao
    proxima = (
        f"<p>Combinada para <b>{escape(prox.data_hora or 'data a definir')}</b>.</p>" + _lista(prox.pauta)
        if prox.combinada
        else "<p><i>Não foi combinada.</i></p>"
    )
    th = 'style="border:1px solid #999;padding:6px;background:#eee;text-align:left"'
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:11pt">
<h1>Ata — {escape(ata.titulo)}</h1>
<p><b>Data:</b> {escape(reuniao.get('inicio', '')[:16].replace('T', ' '))}<br>
<b>Participantes:</b> {escape(', '.join(ata.participantes))}</p>
<h2>Resumo</h2><p>{escape(ata.resumo)}</p>
<h2>Assuntos discutidos</h2>{topicos}
<h2>Decisões</h2>{_lista(ata.decisoes)}
<h2>Ações e responsáveis</h2>
<table style="border-collapse:collapse" border="1" cellpadding="6">
<tr><th {th}>Tarefa</th><th {th}>Responsável</th><th {th}>Prazo</th></tr>{acoes}</table>
<h2>Pontos de atenção</h2>{_lista(ata.pontos_de_atencao)}
<h2>Próxima reunião</h2>{proxima}
<p style="color:#888;font-size:9pt">Ata gerada automaticamente a partir da transcrição do Google Meet.</p>
</body></html>"""
