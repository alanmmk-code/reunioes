"""Acesso ao Claude por dois caminhos:

- "claude_code": usa o Claude Code instalado no PC, logado com a sua assinatura do Claude
  (Pro/Max). Não gasta créditos de API; consome a cota do plano.
- "api": usa a API da Anthropic com a chave do .env (cobra créditos por uso).
- "auto": tenta a API e, se não houver crédito/chave válida, usa o Claude Code.
"""

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import anthropic
from pydantic import BaseModel

import config

_PASTA_NEUTRA = config.BASE_DIR / ".claude_code"  # pasta vazia: o Claude Code não carrega contexto de projeto
_PASTA_NEUTRA.mkdir(exist_ok=True)


class ErroIA(RuntimeError):
    pass


class Cancelado(Exception):
    """O usuário cancelou o trabalho em andamento."""


_vigia = threading.local()


def cancelavel(evento: threading.Event | None) -> None:
    """As chamadas ao Claude feitas nesta thread param quando `evento` for ligado (None desliga)."""
    _vigia.evento = evento


def conferir_cancelamento() -> None:
    evento = getattr(_vigia, "evento", None)
    if evento is not None and evento.is_set():
        raise Cancelado()


def gerar(system: str, blocos: list[str], instrucao: str, saida: type[BaseModel], effort: str = "high") -> BaseModel:
    """Envia o conteúdo ao Claude e devolve a resposta validada no formato `saida`."""
    modo = config.IA_MODO
    if modo == "api":
        return _via_api(system, blocos, instrucao, saida, effort)
    if modo == "auto" and os.getenv("ANTHROPIC_API_KEY", "").startswith("sk-ant-"):
        try:
            return _via_api(system, blocos, instrucao, saida, effort)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.BadRequestError) as e:
            if isinstance(e, anthropic.BadRequestError) and "credit" not in str(e).lower():
                raise
    return _via_claude_code(system, blocos, instrucao, saida, effort)


# ---------------------------------------------------------------- API

def _via_api(system, blocos, instrucao, saida, effort):
    conteudo = [{"type": "text", "text": b} for b in blocos] + [{"type": "text", "text": instrucao}]
    resp = anthropic.Anthropic().beta.messages.parse(
        model=config.CLAUDE_MODEL,
        max_tokens=16000,
        system=system,
        cache_control={"type": "ephemeral"},
        output_config={"effort": effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": conteudo}],
        output_format=saida,
    )
    if resp.stop_reason == "refusal":
        raise ErroIA("O modelo recusou processar este conteúdo.")
    if resp.parsed_output is None:
        raise ErroIA("A resposta do modelo veio incompleta. Tente novamente.")
    return resp.parsed_output


# ---------------------------------------------------------------- Claude Code (assinatura)

def _executavel_claude() -> str:
    caminho = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
    if not Path(caminho).exists():
        raise ErroIA("Claude Code não encontrado. Instale em https://claude.com/claude-code e faça login.")
    return caminho


def _via_claude_code(system, blocos, instrucao, saida, effort):
    schema = saida.model_json_schema()
    comando = [
        _executavel_claude(), "-p", instrucao,
        "--system-prompt", system,  # substitui as instruções longas do Claude Code (economiza cota)
        "--output-format", "json",
        "--json-schema", json.dumps(schema, ensure_ascii=False),
        "--tools", "",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--effort", effort,
    ]
    if config.CLAUDE_CODE_MODELO:
        comando += ["--model", config.CLAUDE_CODE_MODELO]
    # Sem a chave da API no ambiente, o Claude Code usa o login da assinatura
    ambiente = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    conferir_cancelamento()
    proc = subprocess.Popen(
        comando,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=_PASTA_NEUTRA,
        env=ambiente,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    resultado = {}
    leitor = threading.Thread(target=lambda: resultado.update(
        zip(("out", "err"), proc.communicate("\n\n".join(blocos).encode("utf-8")))), daemon=True)
    leitor.start()
    limite = time.monotonic() + 600
    # Espera em pedaços curtos para poder atender ao Cancelar do painel
    while leitor.is_alive():
        leitor.join(0.5)
        evento = getattr(_vigia, "evento", None)
        cancelado = evento is not None and evento.is_set()
        if leitor.is_alive() and (cancelado or time.monotonic() > limite):
            _matar(proc)
            leitor.join(10)
            if cancelado:
                raise Cancelado()
            raise ErroIA("O Claude Code demorou demais para responder.")
    saida_txt = resultado.get("out", b"").decode("utf-8", errors="replace").strip()
    try:
        dados = json.loads(saida_txt)
    except json.JSONDecodeError:
        erro = resultado.get("err", b"").decode("utf-8", errors="replace").strip() or saida_txt
        raise ErroIA(f"Claude Code falhou: {erro[:300]}")
    if dados.get("is_error") or dados.get("structured_output") is None:
        raise ErroIA(f"Claude Code falhou: {str(dados.get('result') or dados.get('subtype'))[:300]}")
    return saida.model_validate(dados["structured_output"])


def _matar(proc: subprocess.Popen) -> None:
    """Encerra o Claude Code e o que ele tiver aberto."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.poll() is None:
        proc.kill()
