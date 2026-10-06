"""Assistente de reuniões do Google Meet.

Uso:
    python reunioes.py          abre o painel web (http://localhost:5055)
    python reunioes.py login    conecta sua conta Google
    python reunioes.py auto     gera atas das reuniões que já terminaram (para agendar no Windows)
"""

import json
from urllib.parse import urlencode
from html import escape
import socket
import subprocess
import sys
import threading
import traceback
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, flash, redirect, render_template_string, request, url_for

import analisador
import assistente
import atas
import config
import db
import google_services as g
import gravador
import monitor

app = Flask(__name__)
app.secret_key = "reunioes-local"

GRAVADOR = gravador.Gravador()
AO_VIVO: assistente.AoVivo | None = None  # assistente da reunião em andamento
# Andamento do processamento depois que a gravação para (transcrição -> ata)
TAREFA = {"ativa": False, "etapa": "", "pct": 0, "erro": None, "event_id": None, "titulo": "", "transcricao_salva": None}


# ---------------------------------------------------------------- Lógica principal

def processar(reuniao: dict, transcricao: str, origem: str) -> dict:
    """Transcrição -> ata (Claude) -> Google Doc -> salva localmente -> tarefas do cliente."""
    cli = db.cliente(reuniao.get("cliente_id"))
    reuniao = {**reuniao, "cliente_id": cli["id"] if cli else None, "cliente_nome": cli["nome"] if cli else None}
    ata = analisador.gerar_ata(transcricao, reuniao)
    html = atas.para_html(ata, reuniao)
    data = reuniao.get("inicio", "")[:10]
    doc_link = g.criar_google_doc(f"Ata - {ata.titulo} - {data}", html)
    registro = {
        "reuniao": {k: v for k, v in reuniao.items() if k != "anexos"},
        "ata": ata.model_dump(),
        "origem_transcricao": origem,
        "transcricao": transcricao,
        "doc_link": doc_link,
        "gerada_em": datetime.now().isoformat(timespec="seconds"),
        "email_enviado_para": [],
        "followup": None,
        "cliente_id": reuniao["cliente_id"],
    }
    atas.salvar(reuniao["id"], registro)
    db.importar_acoes_da_ata(reuniao, ata, reuniao["cliente_id"])
    return registro


def tarefas_da_ata(event_id: str) -> str:
    """Endereço da tela de Tarefas filtrada no cliente da reunião, com o aviso da ata nova."""
    reg = atas.carregar(event_id) or {}
    params = {"ver": "minhas", "nova_ata": event_id}
    if reg.get("cliente_id"):
        params["cliente"] = reg["cliente_id"]
    return "/tarefas?" + urlencode(params)


def sugerir_cliente(reuniao: dict) -> int | None:
    """Cliente provável: o de atas anteriores com as mesmas pessoas ou o mesmo título."""
    emails = {p["email"].lower() for p in reuniao.get("participantes", [])}
    votos = {}
    for event_id in atas.existentes():
        reg = atas.carregar(event_id) or {}
        cid, r = reg.get("cliente_id"), reg.get("reuniao", {})
        if not cid:
            continue
        outros = {p["email"].lower() for p in r.get("participantes", [])}
        peso = len(emails & outros) + (2 if r.get("titulo") and r.get("titulo") == reuniao.get("titulo") else 0)
        if peso:
            votos[cid] = votos.get(cid, 0) + peso
    return max(votos, key=votos.get) if votos else None


def enviar_ata(event_id: str, destinatarios: list[str]) -> None:
    reg = atas.carregar(event_id)
    ata = analisador.Ata.model_validate(reg["ata"])
    html = atas.para_html(ata, reg["reuniao"])
    html = html.replace(
        "</h1>", f'</h1><p><a href="{reg["doc_link"]}">Abrir a ata no Google Docs</a></p>', 1
    )
    g.enviar_email(destinatarios, f"Ata: {ata.titulo}", html)
    reg["email_enviado_para"] = sorted(set(reg["email_enviado_para"]) | set(destinatarios))
    atas.salvar(event_id, reg)


_fila_processamento = threading.Lock()


def processar_gravacao(reuniao: dict, arquivos: dict, avisar: bool = False) -> None:
    """Roda em segundo plano: Whisper -> Claude -> Google Doc.
    avisar=True (gravação automática): notifica e abre a ata no navegador ao terminar."""
    with _fila_processamento:  # uma gravação por vez
        _processar_gravacao(reuniao, arquivos, avisar)


def _processar_gravacao(reuniao: dict, arquivos: dict, avisar: bool) -> None:
    TAREFA.update(ativa=True, etapa="Transcrevendo o áudio", pct=0, erro=None, transcricao_salva=None,
                  event_id=reuniao["id"], titulo=reuniao.get("titulo", ""))
    try:
        texto = gravador.transcrever(arquivos, progresso=lambda p: TAREFA.update(pct=p))
        if not texto:
            raise RuntimeError("Nenhuma fala foi reconhecida no áudio gravado.")
        # Guarda a transcrição antes de chamar o Claude, para não perdê-la se algo falhar
        txt = next(iter(arquivos.values())).with_suffix(".txt")
        txt.write_text(texto, encoding="utf-8")
        TAREFA.update(etapa="Escrevendo a ata com o Claude", pct=100, transcricao_salva=str(txt))
        processar(reuniao, texto, f"Gravação no PC + Whisper ({config.WHISPER_MODELO})")
        TAREFA.update(etapa="Concluído")
        if avisar:
            monitor.notificar("Ata pronta", f"{reuniao.get('titulo', '')} — abrindo suas tarefas")
            webbrowser.open(f"http://localhost:{config.PORTA}{tarefas_da_ata(reuniao['id'])}")
    except Exception as e:
        traceback.print_exc()
        TAREFA.update(erro=str(e))
        if avisar:
            monitor.notificar("Erro ao gerar a ata", str(e)[:150])
    finally:
        TAREFA["ativa"] = False


# ---------------------------------------------------------------- Gravação automática

def _reuniao_da_janela(titulo_janela: str) -> dict:
    """Associa a chamada detectada a um evento da agenda (pelo código do Meet)."""
    codigo, nome = monitor.info_da_janela(titulo_janela)
    try:
        agora = datetime.now().astimezone()
        candidatos = g.listar_reunioes(dias_atras=1, dias_frente=1)
        for r in candidatos:
            if codigo and r["codigo_meet"] == codigo:
                return r
        # Sem código no título: usa o evento da agenda que está acontecendo agora, se houver só um
        em_andamento = [
            r for r in candidatos
            if "T" in r["inicio"]
            and datetime.fromisoformat(r["inicio"]) - timedelta(minutes=15) <= agora <= datetime.fromisoformat(r["fim"])
        ]
        if not codigo and len(em_andamento) == 1:
            return em_andamento[0]
    except Exception:
        traceback.print_exc()
    return {"id": f"avulsa-{datetime.now():%Y%m%d%H%M%S}", "titulo": nome if nome != codigo else "Reunião do Meet",
            "inicio": datetime.now().isoformat(timespec="minutes"), "descricao": "", "participantes": []}


def iniciar_gravacao(reuniao: dict, automatica: bool) -> None:
    """Começa a gravar, liga o assistente ao vivo e abre a janelinha de sugestões."""
    global AO_VIVO
    reuniao.setdefault("cliente_id", None)
    reuniao["cliente_sugerido"] = sugerir_cliente(reuniao)
    GRAVADOR.iniciar(reuniao)
    GRAVADOR.automatica = automatica
    if config.ASSISTENTE_AO_VIVO:
        AO_VIVO = assistente.AoVivo(GRAVADOR, reuniao)
        AO_VIVO.iniciar()
    # A janelinha pergunta o cliente e mostra as sugestões
    pythonw = config.BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable), str(config.BASE_DIR / "janela.py")],
                     cwd=str(config.BASE_DIR))


def definir_cliente_da_gravacao(cliente_id) -> None:
    cli = db.cliente(cliente_id)
    if GRAVADOR.reuniao is not None:
        GRAVADOR.reuniao["cliente_id"] = cli["id"] if cli else None
        GRAVADOR.reuniao["cliente_definido"] = True
        if AO_VIVO:
            AO_VIVO.atualizar_contexto()


def encerrar_gravacao() -> tuple[dict, dict, str]:
    """Para gravação e assistente. Devolve (reuniao, arquivos, transcrição feita ao vivo)."""
    global AO_VIVO
    ao_vivo, AO_VIVO = AO_VIVO, None
    if ao_vivo:
        ao_vivo.parar()
    reuniao, arquivos = GRAVADOR.parar()
    GRAVADOR.automatica = False
    return reuniao, arquivos, ao_vivo.transcricao() if ao_vivo else ""


def ao_detectar_meet(titulo_janela: str) -> None:
    if GRAVADOR.ativo:
        return
    reuniao = _reuniao_da_janela(titulo_janela)
    try:
        iniciar_gravacao(reuniao, automatica=True)
        print(f"[monitor] gravando: {reuniao['titulo']}")
        monitor.notificar("Gravando reunião", f"{reuniao['titulo']} — o assistente está ouvindo.")
    except Exception as e:
        traceback.print_exc()
        monitor.notificar("Não consegui gravar a reunião", str(e)[:150])


def ao_encerrar_meet() -> None:
    if not (GRAVADOR.ativo and GRAVADOR.automatica):
        return
    segundos = (datetime.now() - GRAVADOR.inicio).total_seconds()
    reuniao, arquivos, ao_vivo = encerrar_gravacao()
    if segundos < config.DURACAO_MINIMA_SEG:
        for c in arquivos.values():
            c.unlink(missing_ok=True)
        print(f"[monitor] gravação de {segundos:.0f}s descartada (curta demais)")
        return
    pid = salvar_pendente(reuniao, arquivos, ao_vivo, segundos)
    print(f"[monitor] chamada encerrada após {segundos / 60:.0f} min; aguardando decisão sobre a ata")
    monitor.notificar("Reunião encerrada", "Quer gerar a ata? Abri a pergunta no navegador.")
    webbrowser.open(f"http://localhost:{config.PORTA}/pendente/{pid}")


# ---------------------------------------------------------------- Gravações aguardando decisão

def salvar_pendente(reuniao: dict, arquivos: dict, transcricao_ao_vivo: str, segundos: float) -> str:
    pid = arquivos["voce"].name.removesuffix("-voce.wav")
    dados = {"reuniao": {k: v for k, v in reuniao.items() if k != "anexos"},
             "arquivos": {n: str(c) for n, c in arquivos.items()},
             "transcricao_ao_vivo": transcricao_ao_vivo, "minutos": round(segundos / 60),
             "encerrada_em": datetime.now().isoformat(timespec="minutes")}
    (config.GRAVACOES_DIR / f"{pid}.json").write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    return pid


def carregar_pendente(pid: str) -> dict | None:
    arq = config.GRAVACOES_DIR / f"{pid}.json"
    if not arq.exists() or "/" in pid or "\\" in pid:
        return None
    return json.loads(arq.read_text(encoding="utf-8"))


def listar_pendentes() -> list[dict]:
    return [{"pid": p.stem, **json.loads(p.read_text(encoding="utf-8"))}
            for p in sorted(config.GRAVACOES_DIR.glob("*.json"), reverse=True)]


def modo_auto() -> None:
    """Processa reuniões terminadas nos últimos 2 dias que ainda não têm ata."""
    feitas = atas.existentes()
    for r in g.listar_reunioes(dias_atras=2, dias_frente=0):
        if not r["ja_terminou"] or r["id"] in feitas:
            continue
        try:
            texto, origem = g.buscar_transcricao(r)
            if not texto:
                print(f"- {r['titulo']}: sem transcrição ({origem})")
                continue
            reg = processar(r, texto, origem)
            print(f"+ {r['titulo']}: ata criada {reg['doc_link']}")
            if config.AUTO_ENVIAR_EMAIL:
                emails = [p["email"] for p in r["participantes"]]
                if emails:
                    enviar_ata(r["id"], emails)
                    print(f"  enviada para {', '.join(emails)}")
        except Exception as e:
            print(f"! {r['titulo']}: erro {e}")


# ---------------------------------------------------------------- Painel web

BASE = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Reuniões</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--tx:#1d2330;--mut:#667085;--bd:#e3e6eb;--pri:#1a73e8;--ok:#1e8e3e}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--tx:#e8eaed;--mut:#9aa0a6;--bd:#30353d;--pri:#8ab4f8;--ok:#81c995}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 system-ui,Segoe UI,Arial}
main{max-width:960px;margin:0 auto;padding:24px 16px}
a{color:var(--pri)}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;margin:10px 0}
.row{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.mut{color:var(--mut);font-size:13px}.ok{color:var(--ok);font-weight:600}
button,.btn{background:var(--pri);color:#fff;border:0;border-radius:6px;padding:7px 14px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}
@media (prefers-color-scheme:dark){button,.btn{color:#14171c}}
button.sec,.btn.sec{background:transparent;color:var(--pri);border:1px solid var(--pri)}
textarea,input{width:100%;padding:8px;border:1px solid var(--bd);border-radius:6px;background:var(--card);color:var(--tx);font:inherit}
table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid var(--bd);padding:6px;text-align:left;vertical-align:top}
.flash{background:#fff4e5;color:#7a4a00;border-radius:8px;padding:10px 14px;margin:10px 0}
form.inline{display:inline}
</style></head><body><main>
<nav style="display:flex;gap:16px;margin-bottom:8px"><a href="{{ url_for('inicio') }}"><b>Reuniões</b></a><a href="{{ url_for('tarefas') }}"><b>Tarefas</b></a></nav>
{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}
{{ corpo|safe }}
</main>
<script>document.querySelectorAll('form[data-espera]').forEach(f=>f.addEventListener('submit',()=>{
const b=f.querySelector('button');b.disabled=true;b.textContent=f.dataset.espera}))</script>
</body></html>"""


def pagina(corpo_tpl: str, **ctx):
    corpo = render_template_string(corpo_tpl, **ctx)
    return render_template_string(BASE, corpo=corpo)


@app.route("/")
def inicio():
    try:
        reunioes = g.listar_reunioes()
    except Exception as e:
        return pagina(
            "<h1>Conecte sua conta Google</h1><p>{{ erro }}</p>"
            "<p>Rode <code>python reunioes.py login</code> no terminal e recarregue esta página.</p>",
            erro=str(e),
        )
    feitas = atas.existentes()
    passadas = [r for r in reunioes if r["ja_terminou"]][::-1]
    futuras = [r for r in reunioes if not r["ja_terminou"]]
    return pagina(
        """<h1>Minhas reuniões do Meet</h1>
<p class="mut">Últimos 7 dias e próximos 7 dias da sua agenda.</p>
{% if minhas %}<div class="card row"><div><b>{{ minhas|length }} tarefas suas em aberto</b>
{% set atr = minhas|selectattr('atrasada')|list|length %}{% if atr %} · <b style="color:#d93025">{{ atr }} atrasadas</b>{% endif %}</div>
<a class="btn" href="{{ url_for('tarefas') }}">Ver tarefas</a></div>{% endif %}
{% if gravando or tarefa.ativa %}<div class="card row" style="border-color:#d93025"><div>
<b style="color:#d93025">{% if gravando %}● Gravando{% else %}⏳ Processando gravação{% endif %}</b></div>
<a class="btn" href="{{ url_for('gravacao') }}">Acompanhar</a></div>{% endif %}
<h2>Já aconteceram</h2>
{% for r in passadas %}<div class="card row"><div><b>{{ r.titulo }}</b><br>
<span class="mut">{{ r.inicio[:16].replace('T',' ') }} · {{ r.participantes|length }} convidados</span></div><div>
{% if r.id in feitas %}<span class="ok">✓ Ata pronta</span> <a class="btn sec" href="{{ url_for('ver_ata', event_id=r.id) }}">Ver ata</a>
{% else %}<form class="inline" method="post" action="{{ url_for('gerar', event_id=r.id) }}" data-espera="Gerando ata…">
<button>Gerar ata</button></form>
<a class="btn sec" href="{{ url_for('manual', event_id=r.id) }}">Colar transcrição</a>{% endif %}
</div></div>{% else %}<p class="mut">Nenhuma reunião do Meet nos últimos 7 dias.</p>{% endfor %}
<h2>Próximas</h2>
{% for r in futuras %}<div class="card row"><div><b>{{ r.titulo }}</b><br>
<span class="mut">{{ r.inicio[:16].replace('T',' ') }}</span></div>
<div>{% if not gravando %}<form class="inline" method="post" action="{{ url_for('gravar', event_id=r.id) }}"
onsubmit="window.open('{{ r.link }}','_blank')"><button>● Entrar e gravar</button></form>{% endif %}
<a class="btn sec" href="{{ r.link }}" target="_blank">Só entrar</a></div></div>
{% else %}<p class="mut">Nada agendado.</p>{% endfor %}
{% if pendentes %}<h2>Gravações aguardando decisão</h2>
{% for p in pendentes %}<div class="card row"><div><b>{{ p.reuniao.titulo }}</b><br>
<span class="mut">{{ p.encerrada_em.replace('T',' ') }} · {{ p.minutos }} min</span></div>
<a class="btn" href="{{ url_for('pendente', pid=p.pid) }}">Gerar ata?</a></div>{% endfor %}{% endif %}
<h2>Reunião fora da agenda</h2>
<p>{% if not gravando %}<form class="inline" method="post" action="{{ url_for('gravar', event_id='avulsa') }}">
<button class="sec">● Gravar agora</button></form> ·{% endif %}
<a href="{{ url_for('manual', event_id='avulsa') }}">Colar uma transcrição avulsa</a></p>""",
        passadas=passadas,
        futuras=futuras,
        feitas=feitas,
        gravando=GRAVADOR.ativo,
        tarefa=TAREFA,
        pendentes=listar_pendentes(),
        minhas=db.tarefas(somente_minhas=True),
    )


@app.post("/gravar/<event_id>")
def gravar(event_id):
    if GRAVADOR.ativo:
        return redirect(url_for("gravacao"))
    if event_id == "avulsa":
        reuniao = {"id": f"avulsa-{datetime.now():%Y%m%d%H%M%S}", "titulo": "Reunião gravada",
                   "inicio": datetime.now().isoformat(timespec="minutes"), "descricao": "", "participantes": []}
    else:
        reuniao = g.obter_reuniao(event_id)
    try:
        iniciar_gravacao(reuniao, automatica=False)
    except Exception as e:
        traceback.print_exc()
        flash(f"Não consegui iniciar a gravação: {e}")
        return redirect(url_for("inicio"))
    return redirect(url_for("gravacao"))


@app.post("/parar")
def parar():
    if GRAVADOR.ativo:
        try:
            reuniao, arquivos, _ = encerrar_gravacao()
            TAREFA.update(ativa=True, etapa="Preparando", pct=0, erro=None, event_id=reuniao["id"])
            threading.Thread(target=processar_gravacao, args=(reuniao, arquivos), daemon=True).start()
        except Exception as e:
            flash(str(e))
    return redirect(url_for("gravacao"))


# ---------------------------------------------------------------- API da janelinha de sugestões

@app.get("/api/aovivo")
def api_aovivo():
    r = GRAVADOR.reuniao or {}
    cli = db.cliente(r.get("cliente_id"))
    estado = {"gravando": GRAVADOR.ativo, "duracao": GRAVADOR.duracao(), "titulo": r.get("titulo", ""),
              "assistente": bool(AO_VIVO), "cliente_definido": bool(r.get("cliente_definido")),
              "cliente": cli["nome"] if cli else None, "cliente_sugerido": r.get("cliente_sugerido"),
              "clientes": [{"id": c["id"], "nome": c["nome"]} for c in db.clientes()]}
    if AO_VIVO:
        estado.update(AO_VIVO.estado())
    return estado


@app.post("/api/cliente")
def api_cliente():
    dados = request.get_json(silent=True) or {}
    cliente_id = dados.get("cliente_id")
    if dados.get("novo"):
        cliente_id = db.criar_cliente(dados["novo"])
    definir_cliente_da_gravacao(cliente_id)
    return {"ok": True}


@app.post("/api/sugerir")
def api_sugerir():
    if AO_VIVO:
        AO_VIVO.pedir_sugestao()
    return {"ok": bool(AO_VIVO)}


# ---------------------------------------------------------------- Gerar ata? (fim da gravação automática)

@app.route("/pendente/<pid>")
def pendente(pid):
    p = carregar_pendente(pid)
    if not p:
        flash("Essa gravação não está mais aguardando decisão.")
        return redirect(url_for("inicio"))
    return pagina(
        """<h1>Gerar a ata desta reunião?</h1>
<p><b>{{ p.reuniao.titulo }}</b> · {{ p.minutos }} min · encerrada em {{ p.encerrada_em.replace('T',' ') }}</p>
<div class="row" style="justify-content:flex-start">
<form class="inline" method="post" action="{{ url_for('pendente_gerar', pid=pid) }}">{{ seletor_cliente(atual)|safe }} <button>Sim, gerar ata</button></form>
<form class="inline" method="post" action="{{ url_for('pendente_descartar', pid=pid) }}"
onsubmit="return confirm('Apagar o áudio desta reunião? Não dá para desfazer.')"><button class="sec">Não, apagar o áudio</button></form>
<a class="btn sec" href="{{ url_for('inicio') }}">Decidir depois</a></div>
{% if p.transcricao_ao_vivo %}<h2>O que foi captado ao vivo</h2>
<div class="card" style="white-space:pre-wrap;font-size:13px;max-height:400px;overflow:auto">{{ p.transcricao_ao_vivo }}</div>{% endif %}""",
        p=p, pid=pid, seletor_cliente=seletor_cliente,
        atual=p["reuniao"].get("cliente_id") or p["reuniao"].get("cliente_sugerido"),
    )


@app.post("/pendente/<pid>/gerar")
def pendente_gerar(pid):
    p = carregar_pendente(pid)
    if p:
        p["reuniao"]["cliente_id"] = cliente_do_formulario()
        arquivos = {n: Path(c) for n, c in p["arquivos"].items()}
        (config.GRAVACOES_DIR / f"{pid}.json").unlink(missing_ok=True)
        TAREFA.update(ativa=True, etapa="Preparando", pct=0, erro=None, event_id=p["reuniao"]["id"])
        threading.Thread(target=processar_gravacao, args=(p["reuniao"], arquivos), daemon=True).start()
    return redirect(url_for("gravacao"))


@app.post("/pendente/<pid>/descartar")
def pendente_descartar(pid):
    p = carregar_pendente(pid)
    if p:
        for c in p["arquivos"].values():
            Path(c).unlink(missing_ok=True)
        (config.GRAVACOES_DIR / f"{pid}.json").unlink(missing_ok=True)
        flash("Áudio apagado.")
    return redirect(url_for("inicio"))


@app.route("/gravacao")
def gravacao():
    if not GRAVADOR.ativo and not TAREFA["ativa"] and not TAREFA["erro"] and TAREFA["event_id"]:
        if atas.carregar(TAREFA["event_id"]):
            return redirect(tarefas_da_ata(TAREFA["event_id"]))
    return pagina(
        """{% if gravando %}<meta http-equiv="refresh" content="5">
<h1 style="color:#d93025">● Gravando</h1><p><b>{{ titulo }}</b> · {{ duracao }}</p>
{% if automatica %}<p>Gravação automática: ela para sozinha quando você sair da chamada do Meet.</p>{% endif %}
<p class="mut">Gravando seu microfone e o áudio da chamada. Pode minimizar esta página.
Use <b>fone de ouvido</b> para a transcrição separar melhor quem falou.</p>
<form method="post" action="{{ url_for('parar') }}" data-espera="Parando…"><button>■ Parar e gerar ata</button></form>
{% elif tarefa.ativa %}<meta http-equiv="refresh" content="3">
<h1>⏳ {{ tarefa.etapa }}…</h1>
{% if tarefa.etapa.startswith('Transcrevendo') %}<p>{{ tarefa.pct }}% concluído</p>
<p class="mut">No primeiro uso o Whisper baixa o modelo de voz (alguns minutos). A transcrição roda no seu PC
e leva aproximadamente de 1/4 a 1/2 da duração da reunião.</p>{% endif %}
{% elif tarefa.erro %}<h1>Algo deu errado</h1><p>{{ tarefa.erro }}</p>
{% if tarefa.transcricao_salva %}<p class="mut">A transcrição foi salva em {{ tarefa.transcricao_salva }}.
Você pode colá-la em "Colar transcrição".</p>{% endif %}
{% else %}<h1>Nenhuma gravação em andamento</h1>{% endif %}""",
        gravando=GRAVADOR.ativo,
        automatica=GRAVADOR.automatica,
        titulo=(GRAVADOR.reuniao or {}).get("titulo", ""),
        duracao=GRAVADOR.duracao(),
        tarefa=TAREFA,
    )


@app.post("/gerar/<event_id>")
def gerar(event_id):
    reuniao = g.obter_reuniao(event_id)
    if not reuniao:
        flash("Reunião não encontrada na agenda.")
        return redirect(url_for("inicio"))
    try:
        texto, origem = g.buscar_transcricao(reuniao)
        if not texto:
            flash(f"Não encontrei a transcrição automaticamente: {origem}. Cole o texto abaixo.")
            return redirect(url_for("manual", event_id=event_id))
        processar(reuniao, texto, origem)
    except Exception as e:
        traceback.print_exc()
        flash(f"Erro ao gerar a ata: {e}")
        return redirect(url_for("inicio"))
    return redirect(tarefas_da_ata(event_id))


@app.route("/manual/<event_id>", methods=["GET", "POST"])
def manual(event_id):
    if event_id == "avulsa":
        reuniao = {"id": f"avulsa-{datetime.now():%Y%m%d%H%M%S}", "titulo": "", "inicio": datetime.now().isoformat(),
                   "descricao": "", "participantes": []}
    else:
        reuniao = g.obter_reuniao(event_id)
    if request.method == "POST":
        texto = request.form.get("texto", "").strip()
        arquivo = request.files.get("arquivo")
        if arquivo and arquivo.filename:
            texto = arquivo.read().decode("utf-8", errors="replace")
        if not texto:
            flash("Cole a transcrição ou envie um arquivo .txt.")
        else:
            if event_id == "avulsa":
                reuniao["titulo"] = request.form.get("titulo", "") or "Reunião"
            try:
                processar(reuniao, texto, "Texto colado manualmente")
                return redirect(tarefas_da_ata(reuniao["id"]))
            except Exception as e:
                traceback.print_exc()
                flash(f"Erro ao gerar a ata: {e}")
    return pagina(
        """<h1>Colar transcrição</h1><p class="mut">{{ r.titulo or 'Reunião avulsa' }}</p>
<form method="post" enctype="multipart/form-data" data-espera="Gerando ata…">
{% if avulsa %}<p><input name="titulo" placeholder="Título da reunião"></p>{% endif %}
<p><textarea name="texto" rows="16" placeholder="Cole aqui a transcrição…"></textarea></p>
<p class="mut">ou envie um arquivo .txt / .vtt: <input type="file" name="arquivo" accept=".txt,.vtt,.srt"></p>
<button>Gerar ata</button></form>""",
        r=reuniao,
        avulsa=event_id == "avulsa",
    )


@app.route("/ata/<event_id>")
def ver_ata(event_id):
    reg = atas.carregar(event_id)
    if not reg:
        return redirect(url_for("inicio"))
    a, r = reg["ata"], reg["reuniao"]
    prox = a["proxima_reuniao"]
    return pagina(
        """<h1>{{ a.titulo }}</h1>
<p class="mut">{{ r.inicio[:16].replace('T',' ') }} · transcrição: {{ reg.origem_transcricao }}</p>
<form method="post" action="{{ url_for('ata_cliente', event_id=event_id) }}" class="row" style="justify-content:flex-start"><span>Cliente:</span> {{ seletor_cliente(reg.cliente_id)|safe }} <button class="sec">Salvar</button>{% if not reg.cliente_id %}<span class="mut">defina o cliente para as tarefas aparecerem no lugar certo</span>{% endif %}</form>
<p><a class="btn" href="{{ reg.doc_link }}" target="_blank">Abrir no Google Docs</a> <a class="btn sec" href="{{ url_for('tarefas', ver='todas') }}">Ver tarefas</a></p>
<div class="card"><h2 style="margin-top:0">Resumo</h2><p>{{ a.resumo }}</p>
<p class="mut">Participantes: {{ a.participantes|join(', ') }}</p></div>
<div class="card"><h2 style="margin-top:0">Decisões</h2><ul>{% for d in a.decisoes %}<li>{{ d }}</li>{% else %}<li class="mut">Nenhuma</li>{% endfor %}</ul></div>
<div class="card"><h2 style="margin-top:0">Ações</h2><table><tr><th>Tarefa</th><th>Responsável</th><th>Prazo</th></tr>
{% for x in a.acoes %}<tr><td>{{ x.tarefa }}</td><td>{{ x.responsavel }}</td><td>{{ x.prazo }}</td></tr>{% endfor %}</table></div>
<div class="card"><h2 style="margin-top:0">Assuntos</h2>{% for t in a.topicos %}<p><b>{{ t.titulo }}</b><br>{{ t.discussao }}</p>{% endfor %}</div>
{% if a.pontos_de_atencao %}<div class="card"><h2 style="margin-top:0">Pontos de atenção</h2><ul>{% for p in a.pontos_de_atencao %}<li>{{ p }}</li>{% endfor %}</ul></div>{% endif %}

<h2>Enviar ata por e-mail</h2>
<form class="card" method="post" action="{{ url_for('email', event_id=event_id) }}" data-espera="Enviando…">
{% for p in r.participantes %}<label><input type="checkbox" style="width:auto" name="emails" value="{{ p.email }}" checked> {{ p.nome or p.email }} &lt;{{ p.email }}&gt;</label><br>{% endfor %}
<p><input name="extras" placeholder="Outros e-mails, separados por vírgula"></p>
{% if reg.email_enviado_para %}<p class="ok">Já enviada para: {{ reg.email_enviado_para|join(', ') }}</p>{% endif %}
<button>Enviar</button></form>

<h2>Agendar próxima reunião no Meet</h2>
{% if reg.followup %}<p class="ok">Agendada: <a href="{{ reg.followup.link_evento }}" target="_blank">ver evento</a> · <a href="{{ reg.followup.link_meet }}" target="_blank">link do Meet</a></p>{% endif %}
<form class="card" method="post" action="{{ url_for('followup', event_id=event_id) }}" data-espera="Agendando…">
<p><input name="titulo" value="Follow-up: {{ r.titulo or a.titulo }}"></p>
<div class="row"><input type="datetime-local" name="inicio" value="{{ prox.data_hora }}" style="flex:2" required>
<input type="number" name="duracao" value="{{ prox.duracao_min or 30 }}" min="15" step="15" style="flex:1"> min</div>
<p class="mut">Convidados: {{ r.participantes|map(attribute='email')|join(', ') or 'nenhum' }}</p>
<button>Criar evento com Meet</button></form>""",
        a=a, r=r, reg=reg, prox=prox, event_id=event_id, seletor_cliente=seletor_cliente,
    )


@app.post("/ata/<event_id>/email")
def email(event_id):
    emails = request.form.getlist("emails")
    emails += [e.strip() for e in request.form.get("extras", "").split(",") if e.strip()]
    if not emails:
        flash("Escolha pelo menos um destinatário.")
    else:
        try:
            enviar_ata(event_id, emails)
            flash(f"Ata enviada para {', '.join(emails)}.")
        except Exception as e:
            flash(f"Erro ao enviar: {e}")
    return redirect(url_for("ver_ata", event_id=event_id))


@app.post("/ata/<event_id>/followup")
def followup(event_id):
    reg = atas.carregar(event_id)
    try:
        inicio = datetime.fromisoformat(request.form["inicio"])
        pauta = reg["ata"]["proxima_reuniao"]["pauta"] or [x["tarefa"] for x in reg["ata"]["acoes"]]
        descricao = "Pauta:\n" + "\n".join(f"- {p}" for p in pauta) + f"\n\nAta anterior: {reg['doc_link']}"
        reg["followup"] = g.agendar_followup(
            request.form["titulo"], inicio, int(request.form.get("duracao", 30)),
            [p["email"] for p in reg["reuniao"]["participantes"]], descricao,
        )
        atas.salvar(event_id, reg)
        flash("Reunião agendada e convites enviados.")
    except Exception as e:
        flash(f"Erro ao agendar: {e}")
    return redirect(url_for("ver_ata", event_id=event_id))


# ---------------------------------------------------------------- Clientes e tarefas

def seletor_cliente(atual=None, nome="cliente_id") -> str:
    """<select> de clientes com opção de cadastrar um novo na hora."""
    opcoes = ['<option value="">— sem cliente —</option>']
    for c in db.clientes():
        marcado = " selected" if atual and int(atual) == c["id"] else ""
        opcoes.append(f'<option value="{c["id"]}"{marcado}>{escape(c["nome"])}</option>')
    opcoes.append('<option value="novo">+ Novo cliente…</option>')
    return (f'<select name="{nome}" onchange="this.nextElementSibling.style.display=this.value==\'novo\'?\'inline-block\':\'none\'"'
            f' style="width:auto;padding:6px">{"".join(opcoes)}</select>'
            '<input name="cliente_novo" placeholder="Nome do cliente" style="display:none;width:200px">')


def cliente_do_formulario():
    valor = request.form.get("cliente_id", "")
    if valor == "novo":
        nome = request.form.get("cliente_novo", "").strip()
        return db.criar_cliente(nome) if nome else None
    return int(valor) if valor.isdigit() else None


@app.post("/ata/<event_id>/cliente")
def ata_cliente(event_id):
    reg = atas.carregar(event_id)
    if reg:
        reg["cliente_id"] = cliente_do_formulario()
        atas.salvar(event_id, reg)
        db.mover_tarefas_da_ata(event_id, reg["cliente_id"])
        flash("Cliente atualizado; as tarefas desta reunião foram movidas junto.")
    return redirect(url_for("ver_ata", event_id=event_id))


@app.route("/tarefas")
def tarefas():
    ver = request.args.get("ver", "minhas")
    feitas = request.args.get("feitas") == "1"
    cliente_id = request.args.get("cliente", type=int)
    lista = db.tarefas(somente_minhas=ver == "minhas", incluir_feitas=feitas, cliente_id=cliente_id)
    grupos = {}
    for t in lista:
        grupos.setdefault(t["cliente"] or "Sem cliente", []).append(t)
    abertas = [t for t in lista if t["status"] != "feito"]
    nova = None
    reg = atas.carregar(request.args.get("nova_ata", "")) if request.args.get("nova_ata") else None
    if reg:
        da_ata = [t for t in db.tarefas(incluir_feitas=True) if t["origem_ata"] == request.args["nova_ata"]]
        cli = db.cliente(reg.get("cliente_id"))
        nova = {"id": request.args["nova_ata"], "titulo": reg["ata"]["titulo"], "cliente": cli["nome"] if cli else None,
                "minhas": sum(1 for t in da_ata if t["minha"]), "cliente_qtd": sum(1 for t in da_ata if not t["minha"])}
    return pagina(
        """{% if nova %}<div class="card" style="border-color:#1e8e3e">
<b class="ok">✓ Ata pronta: {{ nova.titulo }}</b>{% if nova.cliente %} · cliente {{ nova.cliente }}{% endif %}<br>
{% if nova.minhas %}{{ nova.minhas }} tarefa{{ 's' if nova.minhas > 1 }} nova{{ 's' if nova.minhas > 1 }} para você{% else %}Nenhuma tarefa nova para você{% endif %}{% if nova.cliente_qtd %} e {{ nova.cliente_qtd }} com o cliente/terceiros (veja em "Todas"){% endif %}.
<div class="row" style="justify-content:flex-start;margin-top:8px">
<a class="btn" href="{{ url_for('ver_ata', event_id=nova.id) }}">Ver a ata</a>
{% if not nova.cliente %}<span class="mut">Esta ata está sem cliente: defina na página da ata.</span>{% endif %}</div></div>{% endif %}
<h1>Tarefas e demandas</h1>
<div class="row" style="justify-content:flex-start;gap:8px;margin:8px 0 16px">
<a class="btn {{ '' if ver=='minhas' else 'sec' }}" href="{{ url_for('tarefas', ver='minhas', feitas=feitas and 1 or None, cliente=cliente_id) }}">O que eu tenho que fazer</a>
<a class="btn {{ '' if ver=='todas' else 'sec' }}" href="{{ url_for('tarefas', ver='todas', feitas=feitas and 1 or None, cliente=cliente_id) }}">Todas (inclui as do cliente)</a>
<form method="get" class="inline"><input type="hidden" name="ver" value="{{ ver }}">{% if feitas %}<input type="hidden" name="feitas" value="1">{% endif %}
<select name="cliente" onchange="this.form.submit()" style="width:auto;padding:6px"><option value="">Todos os clientes</option>
{% for c in clientes %}<option value="{{ c.id }}" {{ 'selected' if c.id == cliente_id }}>{{ c.nome }}</option>{% endfor %}</select></form>
<a href="{{ url_for('tarefas', ver=ver, feitas=None if feitas else 1, cliente=cliente_id) }}">{{ 'Esconder concluídas' if feitas else 'Mostrar concluídas' }}</a>
</div>
<p><b style="color:#d93025">{{ abertas|selectattr('atrasada')|list|length }} atrasadas</b> ·
<b>{{ abertas|selectattr('hoje')|list|length }} para hoje</b> · {{ abertas|length }} em aberto</p>

{% for nome, itens in grupos.items() %}<h2>{{ nome }}</h2><div class="card" style="padding:4px 8px"><table>
{% for t in itens %}<tr style="{{ 'opacity:.55' if t.status=='feito' }}">
<td style="width:28px"><form method="post" action="{{ url_for('tarefa_atualizar', tarefa_id=t.id) }}">
<input type="hidden" name="status" value="{{ 'a_fazer' if t.status=='feito' else 'feito' }}">
<input type="checkbox" style="width:auto" onchange="this.form.submit()" {{ 'checked' if t.status=='feito' }} title="Concluir"></form></td>
<td>{% if t.status=='feito' %}<s>{{ t.descricao }}</s>{% else %}{{ t.descricao }}{% endif %}
<br><span class="mut">{{ t.responsavel or '—' }}{% if not t.minha %} · do cliente/terceiros{% endif %}
{% if t.origem_ata %} · <a href="{{ url_for('ver_ata', event_id=t.origem_ata) }}">{{ t.origem_titulo or 'reunião' }}</a>{% endif %}</span></td>
<td style="width:150px"><form method="post" action="{{ url_for('tarefa_atualizar', tarefa_id=t.id) }}">
<input type="date" name="prazo" value="{{ t.prazo or '' }}" onchange="this.form.submit()"
style="{{ 'border-color:#d93025;color:#d93025;font-weight:600' if t.atrasada }}"></form></td>
<td style="width:120px"><form method="post" action="{{ url_for('tarefa_atualizar', tarefa_id=t.id) }}">
<select name="status" onchange="this.form.submit()" style="padding:6px">
{% for s, rot in [('a_fazer','A fazer'),('fazendo','Fazendo'),('feito','Feito')] %}<option value="{{ s }}" {{ 'selected' if t.status==s }}>{{ rot }}</option>{% endfor %}
</select></form></td>
<td style="width:30px"><form method="post" action="{{ url_for('tarefa_atualizar', tarefa_id=t.id) }}" onsubmit="return confirm('Excluir esta tarefa?')">
<input type="hidden" name="excluir" value="1"><button class="sec" style="padding:2px 8px" title="Excluir">×</button></form></td>
</tr>{% endfor %}</table></div>
{% else %}<p class="mut">Nenhuma tarefa aqui. As ações das atas entram sozinhas nesta lista.</p>{% endfor %}

<h2>Nova tarefa</h2>
<form method="post" action="{{ url_for('tarefa_nova') }}" class="card">
<p><input name="descricao" placeholder="O que precisa ser feito" required></p>
<div class="row" style="justify-content:flex-start">{{ seletor_cliente(cliente_id)|safe }}
<input name="responsavel" value="{{ seu_nome }}" placeholder="Responsável" style="width:160px">
<input type="date" name="prazo" style="width:160px">
<label><input type="checkbox" name="minha" value="1" checked style="width:auto"> é minha</label>
<button>Adicionar</button></div></form>

<h2>Clientes</h2>
<div class="card">{% for c in clientes %}<form method="post" action="{{ url_for('cliente_renomear', cliente_id=c.id) }}" class="row" style="justify-content:flex-start;margin:4px 0">
<input name="nome" value="{{ c.nome }}" style="width:260px"><button class="sec">Renomear</button>
<a href="{{ url_for('tarefas', ver='todas', cliente=c.id) }}">ver tarefas</a></form>{% else %}<p class="mut">Nenhum cliente ainda.</p>{% endfor %}
<form method="post" action="{{ url_for('cliente_novo') }}" class="row" style="justify-content:flex-start;margin-top:10px">
<input name="nome" placeholder="Novo cliente" required style="width:260px"><button>Cadastrar</button></form></div>""",
        nova=nova, grupos=grupos, abertas=abertas, ver=ver, feitas=feitas, cliente_id=cliente_id, clientes=db.clientes(),
        seletor_cliente=seletor_cliente, seu_nome=config.SEU_NOME,
    )


@app.post("/tarefas/nova")
def tarefa_nova():
    db.criar_tarefa(cliente_do_formulario(), request.form["descricao"], request.form.get("responsavel", ""),
                    request.form.get("minha") == "1", request.form.get("prazo"))
    return redirect(request.referrer or url_for("tarefas"))


@app.post("/tarefas/<int:tarefa_id>")
def tarefa_atualizar(tarefa_id):
    if request.form.get("excluir"):
        db.excluir_tarefa(tarefa_id)
    else:
        db.atualizar_tarefa(tarefa_id, **{k: v for k, v in request.form.items() if k in ("status", "prazo")})
    return redirect(request.referrer or url_for("tarefas"))


@app.post("/clientes/novo")
def cliente_novo():
    db.criar_cliente(request.form["nome"])
    return redirect(request.referrer or url_for("tarefas"))


@app.post("/clientes/<int:cliente_id>")
def cliente_renomear(cliente_id):
    if request.form.get("nome", "").strip():
        db.renomear_cliente(cliente_id, request.form["nome"])
    return redirect(request.referrer or url_for("tarefas"))


if __name__ == "__main__":
    comando = sys.argv[1] if len(sys.argv) > 1 else "web"
    if comando == "login":
        print(f"Conectado como: {g.autenticar()}")
    elif comando == "auto":
        modo_auto()
    else:
        url = f"http://localhost:{config.PORTA}"
        with socket.socket() as s:
            ja_rodando = s.connect_ex(("127.0.0.1", config.PORTA)) == 0
        if ja_rodando:
            webbrowser.open(url)
            sys.exit(0)
        if sys.stdout is None:  # iniciado sem janela (pythonw, inicialização do Windows)
            log = open(config.BASE_DIR / "reunioes.log", "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = log
        if config.GRAVACAO_AUTOMATICA:
            monitor.iniciar(ao_detectar_meet, ao_encerrar_meet, lambda: GRAVADOR.ativo and GRAVADOR.automatica)
            print("[monitor] vigiando chamadas do Meet")
        # Deixa os modelos de voz prontos antes da primeira reunião
        threading.Thread(target=lambda: [gravador.carregar_modelo(config.WHISPER_MODELO_AO_VIVO),
                                         gravador.carregar_modelo()], daemon=True).start()
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        app.run(port=config.PORTA, debug=False)
