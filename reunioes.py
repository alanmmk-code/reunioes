"""Assistente de reuniões do Google Meet.

Uso:
    python reunioes.py          abre o painel web (http://localhost:5055)
    python reunioes.py login    conecta sua conta Google
    python reunioes.py auto     gera atas das reuniões que já terminaram (para agendar no Windows)
"""

import socket
import sys
import threading
import traceback
import webbrowser
from datetime import datetime

from flask import Flask, flash, redirect, render_template_string, request, url_for

import analisador
import atas
import config
import google_services as g
import gravador

app = Flask(__name__)
app.secret_key = "reunioes-local"

GRAVADOR = gravador.Gravador()
# Andamento do processamento depois que a gravação para (transcrição -> ata)
TAREFA = {"ativa": False, "etapa": "", "pct": 0, "erro": None, "event_id": None, "titulo": "", "transcricao_salva": None}


# ---------------------------------------------------------------- Lógica principal

def processar(reuniao: dict, transcricao: str, origem: str) -> dict:
    """Transcrição -> ata (Claude) -> Google Doc -> salva localmente."""
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
    }
    atas.salvar(reuniao["id"], registro)
    return registro


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


def processar_gravacao(reuniao: dict, arquivos: dict) -> None:
    """Roda em segundo plano: Whisper -> Claude -> Google Doc."""
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
    except Exception as e:
        traceback.print_exc()
        TAREFA.update(erro=str(e))
    finally:
        TAREFA["ativa"] = False


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
<p><a href="{{ url_for('inicio') }}">← Reuniões</a></p>
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
<h2>Reunião fora da agenda</h2>
<p>{% if not gravando %}<form class="inline" method="post" action="{{ url_for('gravar', event_id='avulsa') }}">
<button class="sec">● Gravar agora</button></form> ·{% endif %}
<a href="{{ url_for('manual', event_id='avulsa') }}">Colar uma transcrição avulsa</a></p>""",
        passadas=passadas,
        futuras=futuras,
        feitas=feitas,
        gravando=GRAVADOR.ativo,
        tarefa=TAREFA,
    )


@app.post("/gravar/<event_id>")
def gravar(event_id):
    if TAREFA["ativa"]:
        flash("Aguarde terminar o processamento da gravação anterior.")
        return redirect(url_for("gravacao"))
    if event_id == "avulsa":
        reuniao = {"id": f"avulsa-{datetime.now():%Y%m%d%H%M%S}", "titulo": "Reunião gravada",
                   "inicio": datetime.now().isoformat(timespec="minutes"), "descricao": "", "participantes": []}
    else:
        reuniao = g.obter_reuniao(event_id)
    try:
        GRAVADOR.iniciar(reuniao)
    except Exception as e:
        traceback.print_exc()
        flash(f"Não consegui iniciar a gravação: {e}")
        return redirect(url_for("inicio"))
    return redirect(url_for("gravacao"))


@app.post("/parar")
def parar():
    if GRAVADOR.ativo:
        try:
            reuniao, arquivos = GRAVADOR.parar()
            TAREFA.update(ativa=True, etapa="Preparando", pct=0, erro=None, event_id=reuniao["id"])
            threading.Thread(target=processar_gravacao, args=(reuniao, arquivos), daemon=True).start()
        except Exception as e:
            flash(str(e))
    return redirect(url_for("gravacao"))


@app.route("/gravacao")
def gravacao():
    if not GRAVADOR.ativo and not TAREFA["ativa"] and not TAREFA["erro"] and TAREFA["event_id"]:
        if atas.carregar(TAREFA["event_id"]):
            return redirect(url_for("ver_ata", event_id=TAREFA["event_id"]))
    return pagina(
        """{% if gravando %}<meta http-equiv="refresh" content="5">
<h1 style="color:#d93025">● Gravando</h1><p><b>{{ titulo }}</b> · {{ duracao }}</p>
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
    return redirect(url_for("ver_ata", event_id=event_id))


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
                return redirect(url_for("ver_ata", event_id=reuniao["id"]))
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
<p><a class="btn" href="{{ reg.doc_link }}" target="_blank">Abrir no Google Docs</a></p>
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
        a=a, r=r, reg=reg, prox=prox, event_id=event_id,
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
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        app.run(port=config.PORTA, debug=False)
