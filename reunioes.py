"""Assistente de reuniões do Google Meet.

Uso:
    python reunioes.py          abre o painel web (http://localhost:5055)
    python reunioes.py login    conecta sua conta Google
    python reunioes.py auto     gera atas das reuniões que já terminaram (para agendar no Windows)
"""

import json
import re
from urllib.parse import urlencode
from html import escape
import socket
import subprocess
import sys
import threading
import traceback
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, flash, redirect, render_template_string, request, url_for

import analisador
import assistente
import atas
import config
import db
import google_services as g
import gravador
import lembretes
import monitor
import sincronia

app = Flask(__name__)
app.secret_key = "reunioes-local"

GRAVADOR = gravador.Gravador()


@app.after_request
def _sincronizar_apos_alteracao(resposta):
    if request.method == "POST" and not request.path.startswith(("/api/", "/sincronizar")):
        sincronia.agendar()
    return resposta
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
    sincronia.agendar()
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


def processar_texto(reuniao: dict, texto: str) -> None:
    """Gera a ata a partir de uma transcrição já pronta (ex.: a feita ao vivo, quando o áudio está em outro PC)."""
    with _fila_processamento:
        TAREFA.update(ativa=True, etapa="Escrevendo a ata com o Claude", pct=100, erro=None, transcricao_salva=None,
                      event_id=reuniao["id"], titulo=reuniao.get("titulo", ""))
        try:
            processar(reuniao, texto, "Transcrição feita ao vivo")
            TAREFA.update(etapa="Concluído")
        except Exception as e:
            traceback.print_exc()
            TAREFA.update(erro=str(e))
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
             # só o nome: a pasta pode ser diferente em outro PC
             "arquivos": {n: c.name for n, c in arquivos.items()},
             "transcricao_ao_vivo": transcricao_ao_vivo, "minutos": round(segundos / 60),
             "encerrada_em": datetime.now().isoformat(timespec="minutes")}
    _gravar_pendente(pid, dados)
    return pid


def _gravar_pendente(pid: str, dados: dict) -> None:
    dados["atualizado_em"] = db.agora()  # usado para mesclar entre PCs
    (config.GRAVACOES_DIR / f"{pid}.json").write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    sincronia.agendar()


def _resolver_pendente(pid: str, dados: dict, decisao: str) -> None:
    """Marca como decidida (em vez de apagar), para a decisão chegar aos outros PCs."""
    dados["resolvida"] = decisao
    _gravar_pendente(pid, dados)


def _audios_da_pendente(p: dict) -> dict:
    """Caminhos dos áudios neste PC (só os que existem aqui)."""
    caminhos = {n: config.GRAVACOES_DIR / Path(c).name for n, c in p["arquivos"].items()}
    return {n: c for n, c in caminhos.items() if c.exists()}


def carregar_pendente(pid: str) -> dict | None:
    arq = config.GRAVACOES_DIR / f"{pid}.json"
    if not arq.exists() or "/" in pid or "\\" in pid:
        return None
    dados = json.loads(arq.read_text(encoding="utf-8"))
    return None if dados.get("resolvida") else dados


def listar_pendentes() -> list[dict]:
    lista = []
    for arq in sorted(config.GRAVACOES_DIR.glob("*.json"), reverse=True):
        dados = json.loads(arq.read_text(encoding="utf-8"))
        if not dados.get("resolvida"):
            lista.append({"pid": arq.stem, **dados})
    return lista


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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#f4f5f7;--card:#fff;--card2:#f9fafb;--tx:#111827;--tx2:#374151;--mut:#6b7280;--bd:#e5e7eb;
--pri:#4f46e5;--pri-s:#eef2ff;--ok:#059669;--ok-s:#ecfdf5;--warn:#b45309;--warn-s:#fffbeb;--err:#dc2626;--err-s:#fef2f2;
--sh:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.06);--r:12px}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--card2:#1d212a;--tx:#f3f4f6;--tx2:#d1d5db;--mut:#9ca3af;--bd:#2a2f3a;
--pri:#818cf8;--pri-s:#1e1b4b;--ok:#34d399;--ok-s:#052e22;--warn:#fbbf24;--warn-s:#2a1f05;--err:#f87171;--err-s:#2d0f0f;--sh:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:14.5px/1.55 Inter,system-ui,"Segoe UI",Arial,sans-serif;-webkit-font-smoothing:antialiased}
header.top{background:var(--card);border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:5}
header.top .in{max-width:1040px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;gap:20px}
.logo{font-weight:700;font-size:15px;color:var(--tx);text-decoration:none;display:flex;align-items:center;gap:8px}
.logo i{width:26px;height:26px;border-radius:8px;background:var(--pri);color:#fff;display:grid;place-items:center;font-style:normal;font-size:13px}
nav.menu{display:flex;gap:4px}
nav.menu a{padding:6px 12px;border-radius:8px;color:var(--mut);text-decoration:none;font-weight:500}
nav.menu a:hover{background:var(--card2);color:var(--tx)}
nav.menu a.on{background:var(--pri-s);color:var(--pri)}
main{max-width:1040px;margin:0 auto;padding:24px 16px 64px}
a{color:var(--pri)}
h1{font-size:24px;font-weight:700;letter-spacing:-.01em;margin:0 0 4px}
h2{font-size:16px;font-weight:600;margin:28px 0 10px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:14px 16px;margin:10px 0;box-shadow:var(--sh)}
.row{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.mut{color:var(--mut);font-size:13px}.ok{color:var(--ok);font-weight:600}
button,.btn{background:var(--pri);color:#fff;border:1px solid transparent;border-radius:8px;padding:7px 14px;font:inherit;font-weight:500;
cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:6px;transition:filter .15s}
button:hover,.btn:hover{filter:brightness(1.08)}
@media (prefers-color-scheme:dark){button,.btn{color:#0f1115}}
button.sec,.btn.sec{background:var(--card);color:var(--tx2);border-color:var(--bd)}
button.sec:hover,.btn.sec:hover{background:var(--card2);filter:none}
textarea,input,select{width:100%;padding:8px 10px;border:1px solid var(--bd);border-radius:8px;background:var(--card);color:var(--tx);font:inherit}
input:focus,select:focus,textarea:focus{outline:2px solid var(--pri-s);border-color:var(--pri)}
table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid var(--bd);padding:8px 6px;text-align:left;vertical-align:top}
.flash{background:var(--warn-s);color:var(--warn);border:1px solid var(--bd);border-radius:10px;padding:10px 14px;margin:10px 0}
form.inline{display:inline}
details>summary{cursor:pointer;list-style:none}
.sinc{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--mut);font-size:12.5px}
.sinc button{padding:4px 10px;font-size:12.5px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--bd)}.dot.on{background:var(--ok)}.dot.err{background:var(--err)}
@media (max-width:600px){.sinc span:not(.dot){display:none}}details>summary::-webkit-details-marker{display:none}
</style></head><body>
<header class="top"><div class="in">
<a class="logo" href="{{ url_for('inicio') }}"><i>R</i>Reuniões</a>
<nav class="menu">
<a href="{{ url_for('inicio') }}" class="{{ 'on' if not request.path.startswith('/tarefas') }}">Painel</a>
<a href="{{ url_for('tarefas') }}" class="{{ 'on' if request.path.startswith('/tarefas') }}">Tarefas</a>
</nav>
{% if sinc_ligada %}<form method="post" action="{{ url_for('sincronizar_agora') }}" class="sinc" title="{{ sinc.erro or sinc.resumo or 'Sincroniza atas, tarefas e gravações com o seu Google Drive' }}">
<span class="dot {{ 'err' if sinc.erro else ('on' if sinc.ultima else '') }}"></span>
<span>{% if sinc.rodando %}Sincronizando…{% elif sinc.erro %}Erro ao sincronizar{% elif sinc.ultima %}Drive: {{ sinc.ultima.strftime('%H:%M') }}{% else %}Drive: aguardando{% endif %}</span>
<button class="sec">Sincronizar</button></form>{% endif %}
</div></header>
<main>
{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}
{{ corpo|safe }}
</main>
<script>document.querySelectorAll('form[data-espera]').forEach(f=>f.addEventListener('submit',()=>{
const b=f.querySelector('button');b.disabled=true;b.textContent=f.dataset.espera}))</script>
</body></html>"""


def pagina(corpo_tpl: str, **ctx):
    corpo = render_template_string(corpo_tpl, **ctx)
    return render_template_string(BASE, corpo=corpo, sinc=sincronia.ESTADO, sinc_ligada=config.SINCRONIZAR)


@app.post("/sincronizar")
def sincronizar_agora():
    try:
        flash(f"Sincronizado com o Google Drive: {sincronia.sincronizar()}.")
    except Exception as e:
        flash(f"Não consegui sincronizar: {e}")
    return redirect(request.referrer or url_for("inicio"))


DIAS_SEMANA = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
         "outubro", "novembro", "dezembro"]
TELA_PAINEL = config.BASE_DIR / "telas" / "painel.html"


@app.route("/")
def inicio():
    """Tela única: agenda da semana, tarefas, reuniões recentes e alertas."""
    semana = request.args.get("semana", 0, type=int)
    agora = datetime.now().astimezone()
    hoje = agora.date()
    segunda = hoje - timedelta(days=hoje.weekday()) + timedelta(weeks=semana)
    ini_semana = datetime.combine(segunda, datetime.min.time(), agora.tzinfo)
    try:
        eventos = g.listar_eventos(ini_semana, ini_semana + timedelta(days=7))
        recentes = [r for r in g.listar_reunioes(dias_atras=14, dias_frente=0) if r["ja_terminou"]][::-1]
        # compromissos de hoje: vêm da própria semana exibida; em outra semana, busca só o dia de hoje
        eventos_de_hoje = eventos if semana == 0 else g.listar_eventos(
            datetime.combine(hoje, datetime.min.time(), agora.tzinfo), datetime.combine(hoje + timedelta(days=1), datetime.min.time(), agora.tzinfo))
    except Exception as e:
        return pagina(
            "<h1>Conecte sua conta Google</h1><p>{{ erro }}</p>"
            "<p>Rode <code>python reunioes.py login</code> no terminal e recarregue esta página.</p>",
            erro=str(e),
        )

    minhas = db.tarefas(somente_minhas=True)
    clientes_por_id = {c["id"]: c["nome"] for c in db.clientes()}
    dias = []
    for i in range(7):
        d = segunda + timedelta(days=i)
        do_dia = []
        for e in eventos:
            if e["dia_inteiro"]:
                if date.fromisoformat(e["inicio"]) <= d < date.fromisoformat(e["fim"]):
                    do_dia.append({**e, "hora": "Dia todo", "hora_fim": "", "agora": False})
                continue
            ini_e = datetime.fromisoformat(e["inicio"]).astimezone()
            fim_e = datetime.fromisoformat(e["fim"]).astimezone()
            if ini_e.date() == d:
                cliente = clientes_por_id.get(sugerir_cliente(e)) if e["link"] else None
                do_dia.append({**e, "hora": ini_e.strftime("%H:%M"), "hora_fim": fim_e.strftime("%H:%M"),
                               "agora": ini_e <= agora <= fim_e, "cliente": cliente})
        dias.append({"data": d, "semana": DIAS_SEMANA[i], "hoje": d == hoje, "passado": d < hoje, "eventos": do_dia,
                     "tarefas": [t for t in minhas if t["prazo"] == d.isoformat()]})

    eventos_hoje = sum(
        1 for e in eventos_de_hoje
        if (date.fromisoformat(e["inicio"]) <= hoje < date.fromisoformat(e["fim"]) if e["dia_inteiro"]
            else datetime.fromisoformat(e["inicio"]).astimezone().date() == hoje)
    )
    domingo = segunda + timedelta(days=6)
    semana_txt = (f"{segunda.day} a {domingo.day} de {MESES[domingo.month - 1]}" if segunda.month == domingo.month
                  else f"{segunda.day} de {MESES[segunda.month - 1]} a {domingo.day} de {MESES[domingo.month - 1]}")
    saudacao = "Bom dia" if agora.hour < 12 else ("Boa tarde" if agora.hour < 18 else "Boa noite")
    if config.SEU_NOME and config.SEU_NOME != "Eu":
        saudacao += f", {config.SEU_NOME}"
    return pagina(
        TELA_PAINEL.read_text(encoding="utf-8"),
        saudacao=saudacao,
        hoje_txt=f"{DIAS_SEMANA[hoje.weekday()]}, {hoje.day} de {MESES[hoje.month - 1]}",
        semana=semana, semana_txt=semana_txt, dias=dias, eventos_hoje=eventos_hoje,
        minhas=minhas, recentes=recentes, feitas=atas.existentes(), pendentes=listar_pendentes(),
        gravando=GRAVADOR.ativo, titulo_gravacao=(GRAVADOR.reuniao or {}).get("titulo", ""), tarefa=TAREFA,
        seletor_cliente=seletor_cliente, seu_nome=config.SEU_NOME,
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
{% if not tem_audio %}<div class="flash">O áudio desta reunião ficou no PC onde ela foi gravada.
{% if p.transcricao_ao_vivo %}Dá para gerar a ata aqui com a transcrição feita ao vivo (um pouco menos precisa), ou gerar no outro PC.
{% else %}Gere a ata naquele PC.{% endif %}</div>{% endif %}
<div class="row" style="justify-content:flex-start">
<form class="inline" method="post" action="{{ url_for('pendente_gerar', pid=pid) }}">{{ seletor_cliente(atual)|safe }} <button>Sim, gerar ata</button></form>
<form class="inline" method="post" action="{{ url_for('pendente_descartar', pid=pid) }}"
onsubmit="return confirm('Apagar o áudio desta reunião? Não dá para desfazer.')"><button class="sec">Não, apagar o áudio</button></form>
<a class="btn sec" href="{{ url_for('inicio') }}">Decidir depois</a></div>
{% if p.transcricao_ao_vivo %}<h2>O que foi captado ao vivo</h2>
<div class="card" style="white-space:pre-wrap;font-size:13px;max-height:400px;overflow:auto">{{ p.transcricao_ao_vivo }}</div>{% endif %}""",
        p=p, pid=pid, seletor_cliente=seletor_cliente, tem_audio=bool(_audios_da_pendente(p)),
        atual=p["reuniao"].get("cliente_id") or p["reuniao"].get("cliente_sugerido"),
    )


@app.post("/pendente/<pid>/gerar")
def pendente_gerar(pid):
    p = carregar_pendente(pid)
    if p:
        p["reuniao"]["cliente_id"] = cliente_do_formulario()
        arquivos = _audios_da_pendente(p)
        if not arquivos and not p.get("transcricao_ao_vivo"):
            flash("O áudio desta reunião está em outro PC. Gere a ata por lá.")
            return redirect(url_for("pendente", pid=pid))
        _resolver_pendente(pid, p, "ata")
        TAREFA.update(ativa=True, etapa="Preparando", pct=0, erro=None, event_id=p["reuniao"]["id"])
        if arquivos:
            threading.Thread(target=processar_gravacao, args=(p["reuniao"], arquivos), daemon=True).start()
        else:
            threading.Thread(target=processar_texto, args=(p["reuniao"], p["transcricao_ao_vivo"]), daemon=True).start()
    return redirect(url_for("gravacao"))


@app.post("/pendente/<pid>/descartar")
def pendente_descartar(pid):
    p = carregar_pendente(pid)
    if p:
        for c in _audios_da_pendente(p).values():
            c.unlink(missing_ok=True)
        _resolver_pendente(pid, p, "descartada")
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


# ---------------------------------------------------------------- Resumo pré-reunião

def ultima_ata_do_cliente(cliente_id) -> dict | None:
    candidatas = []
    for event_id in atas.existentes():
        reg = atas.carregar(event_id) or {}
        if cliente_id and reg.get("cliente_id") == cliente_id and reg.get("ata"):
            candidatas.append((reg["reuniao"].get("inicio", ""), event_id, reg))
    if not candidatas:
        return None
    _, event_id, reg = max(candidatas, key=lambda c: c[0])
    return {"event_id": event_id, **reg}


@app.route("/resumo/<event_id>")
def resumo(event_id):
    r = g.obter_reuniao(event_id)
    if not r:
        flash("Reunião não encontrada na agenda.")
        return redirect(url_for("inicio"))
    cliente_id = request.args.get("cliente", type=int) or sugerir_cliente(r)
    cli = db.cliente(cliente_id)
    res = lembretes.resumo_cliente(cliente_id)
    anterior = ultima_ata_do_cliente(cliente_id)
    inicio = datetime.fromisoformat(r["inicio"]) if "T" in r["inicio"] else None
    faltam = round((inicio - datetime.now().astimezone()).total_seconds() / 60) if inicio else None
    pauta = re.sub(r"<[^>]+>", " ", (r.get("descricao") or "").replace("<br>", "\n")).strip()
    return pagina(
        """<style>
.rs-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media (max-width:760px){.rs-grid{grid-template-columns:1fr}}
.rs h3{margin:0 0 10px;font-size:14px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.rs ul{margin:0;padding-left:18px}.rs li{margin:4px 0}
.atr{color:var(--err);font-weight:600}.qd{color:var(--mut);font-size:12.5px}
</style>
<div class="row"><div><h1>{{ r.titulo }}</h1>
<p class="mut">{% if inicio %}{{ inicio.strftime('%d/%m às %H:%M') }}{% if faltam is not none and faltam >= 0 %} · começa em {{ faltam }} min{% endif %}{% endif %}
{% if r.participantes %} · {{ r.participantes|map(attribute='email')|join(', ') }}{% endif %}</p></div>
<a class="btn" href="{{ r.link }}" target="_blank">Entrar no Meet</a></div>

<form method="get" class="row" style="justify-content:flex-start;margin:6px 0 4px">
<span class="mut">Cliente:</span><select name="cliente" onchange="this.form.submit()" style="width:auto">
<option value="">— nenhum —</option>{% for c in clientes %}<option value="{{ c.id }}" {{ 'selected' if cli and c.id == cli.id }}>{{ c.nome }}</option>{% endfor %}</select>
{% if not cli %}<span class="mut">Escolha o cliente para ver as pendências e a última reunião.</span>{% endif %}</form>

<div class="rs-grid">
<div class="card rs"><h3>Suas pendências{% if cli %} com {{ cli.nome }}{% endif %}</h3>
{% if res.minhas %}<ul>{% for t in res.minhas %}<li>{{ t.descricao }}
<span class="{{ 'atr' if t.atrasada else 'qd' }}">{% if t.prazo %}· {{ 'atrasada desde' if t.atrasada else 'até' }} {{ t.prazo[8:10] }}/{{ t.prazo[5:7] }}{% endif %}{% if t.status == 'fazendo' %} · em andamento{% endif %}</span></li>{% endfor %}</ul>
{% else %}<p class="mut">Nada pendente do seu lado.</p>{% endif %}</div>

<div class="card rs"><h3>Para cobrar do cliente</h3>
{% if res.do_cliente %}<ul>{% for t in res.do_cliente %}<li>{{ t.descricao }} <span class="qd">· {{ t.responsavel }}{% if t.prazo %} · até {{ t.prazo[8:10] }}/{{ t.prazo[5:7] }}{% endif %}</span></li>{% endfor %}</ul>
{% else %}<p class="mut">Nada pendente do lado do cliente.</p>{% endif %}</div>

<div class="card rs"><h3>Última reunião</h3>
{% if anterior %}<p><b>{{ anterior.ata.titulo }}</b> <span class="qd">· {{ anterior.reuniao.inicio[8:10] }}/{{ anterior.reuniao.inicio[5:7] }}</span></p>
<p>{{ anterior.ata.resumo }}</p>
{% if anterior.ata.decisoes %}<p class="mut" style="margin-bottom:4px">Decisões:</p><ul>{% for d in anterior.ata.decisoes %}<li>{{ d }}</li>{% endfor %}</ul>{% endif %}
{% if anterior.ata.proxima_reuniao.pauta %}<p class="mut" style="margin:10px 0 4px">Ficou para esta reunião:</p><ul>{% for p in anterior.ata.proxima_reuniao.pauta %}<li>{{ p }}</li>{% endfor %}</ul>{% endif %}
<p><a href="{{ url_for('ver_ata', event_id=anterior.event_id) }}">Ver a ata completa</a></p>
{% else %}<p class="mut">Nenhuma ata anterior{% if cli %} com {{ cli.nome }}{% endif %}.</p>{% endif %}</div>

<div class="card rs"><h3>Pauta do convite</h3>
{% if pauta %}<p style="white-space:pre-wrap">{{ pauta }}</p>{% else %}<p class="mut">O convite não tem descrição.</p>{% endif %}</div>
</div>""",
        r=r, cli=cli, res=res, anterior=anterior, inicio=inicio, faltam=faltam, pauta=pauta, clientes=db.clientes(),
    )


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
        """<style>
.tk-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:18px}
.tk-sub{color:var(--mut);margin:0}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:0 0 20px}
@media (max-width:720px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
.stat{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:14px 16px;box-shadow:var(--sh);text-decoration:none;color:var(--tx)}
.stat b{display:block;font-size:26px;font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums}
.stat span{color:var(--mut);font-size:13px;font-weight:500}
.stat.err b{color:var(--err)}.stat.warn b{color:var(--warn)}.stat.pri b{color:var(--pri)}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
.seg{display:inline-flex;background:var(--card2);border:1px solid var(--bd);border-radius:10px;padding:3px}
.seg a{padding:6px 12px;border-radius:7px;color:var(--mut);text-decoration:none;font-weight:500;font-size:13.5px}
.seg a.on{background:var(--card);color:var(--tx);box-shadow:var(--sh)}
.bar select{width:auto;padding:7px 10px}
.bar .lnk{color:var(--mut);font-size:13px;text-decoration:none;margin-left:auto}
.bar .lnk:hover{color:var(--tx)}
.grupo{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);box-shadow:var(--sh);margin:14px 0;overflow:hidden}
.grupo-h{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--bd);background:var(--card2)}
.av{width:28px;height:28px;border-radius:8px;background:var(--pri-s);color:var(--pri);display:grid;place-items:center;font-weight:700;font-size:13px;flex:none}
.grupo-h h3{margin:0;font-size:15px;font-weight:600}
.grupo-h .cnt{margin-left:auto;color:var(--mut);font-size:12.5px}
.task{display:grid;grid-template-columns:28px 1fr auto auto 28px;gap:12px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--bd)}
.task:last-child{border-bottom:0}
.task:hover{background:var(--card2)}
.task.feito .t-txt{color:var(--mut);text-decoration:line-through}
.chk{appearance:none;-webkit-appearance:none;width:20px;height:20px;border:2px solid var(--bd);border-radius:50%;cursor:pointer;margin:0;padding:0;display:grid;place-items:center;background:var(--card)}
.chk:hover{border-color:var(--ok)}
.chk:checked{background:var(--ok);border-color:var(--ok)}
.chk:checked::after{content:"";width:5px;height:9px;border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg) translate(-1px,-1px)}
.t-txt{font-weight:500;color:var(--tx)}
.t-meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px;align-items:center}
.pill{display:inline-flex;align-items:center;gap:4px;font-size:12px;padding:2px 8px;border-radius:999px;background:var(--card2);border:1px solid var(--bd);color:var(--tx2);text-decoration:none;white-space:nowrap}
.pill.cli{background:var(--warn-s);color:var(--warn);border-color:transparent}
a.pill:hover{border-color:var(--pri);color:var(--pri)}
.due{position:relative;display:inline-flex}
.due span{font-size:12.5px;font-weight:600;padding:4px 10px;border-radius:8px;background:var(--card2);color:var(--tx2);white-space:nowrap;cursor:pointer}
.due span.atr{background:var(--err-s);color:var(--err)}
.due span.hj{background:var(--warn-s);color:var(--warn)}
.due span.sem{color:var(--mut);font-weight:500}
.due input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;padding:0}
.st{width:auto;padding:4px 8px;font-size:12.5px;font-weight:600;border-radius:8px;border:1px solid transparent;cursor:pointer}
.st.a_fazer{background:var(--card2);color:var(--tx2);border-color:var(--bd)}
.st.fazendo{background:var(--pri-s);color:var(--pri)}
.st.feito{background:var(--ok-s);color:var(--ok)}
.del{background:none;border:0;color:var(--mut);font-size:18px;line-height:1;padding:4px;opacity:0;cursor:pointer}
.task:hover .del{opacity:1}.del:hover{color:var(--err);filter:none}
@media (max-width:720px){.task{grid-template-columns:28px 1fr 28px}.task .due,.task .stw{grid-column:2}.del{opacity:1}}
.vazio{text-align:center;padding:40px 16px;color:var(--mut)}
.vazio b{display:block;color:var(--tx);font-size:16px;margin-bottom:4px}
.novo{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);box-shadow:var(--sh);margin:20px 0}
.novo summary{padding:14px 16px;font-weight:600;color:var(--pri)}
.novo form{padding:0 16px 16px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
.novo form .full{grid-column:1/-1}
@media (max-width:720px){.novo form{grid-template-columns:1fr}}
.aviso{border:1px solid var(--ok);background:var(--ok-s);border-radius:var(--r);padding:14px 16px;margin-bottom:18px}
.aviso b{color:var(--ok)}
</style>

{% if nova %}<div class="aviso">
<b>✓ Ata pronta: {{ nova.titulo }}</b>{% if nova.cliente %} · {{ nova.cliente }}{% endif %}<br>
{% if nova.minhas %}{{ nova.minhas }} tarefa{{ 's' if nova.minhas > 1 }} nova{{ 's' if nova.minhas > 1 }} para você{% else %}Nenhuma tarefa nova para você{% endif %}{% if nova.cliente_qtd %} e {{ nova.cliente_qtd }} com o cliente/terceiros (veja em "Todas"){% endif %}.
<div class="row" style="justify-content:flex-start;margin-top:10px">
<a class="btn" href="{{ url_for('ver_ata', event_id=nova.id) }}">Ver a ata</a>
{% if not nova.cliente %}<span class="mut">Esta ata está sem cliente: defina na página da ata.</span>{% endif %}</div></div>{% endif %}

<div class="tk-head"><div><h1>Tarefas</h1>
<p class="tk-sub">{{ 'O que ficou com você nas reuniões' if ver=='minhas' else 'Tudo o que foi combinado, inclusive o que ficou com os clientes' }}</p></div></div>

{% set atr = abertas|selectattr('atrasada')|list|length %}
{% set hj = abertas|selectattr('hoje')|list|length %}
{% set sem = abertas|selectattr('dias','ne',None)|selectattr('dias','ge',1)|selectattr('dias','le',7)|list|length %}
<div class="stats">
<div class="stat err"><b>{{ atr }}</b><span>Atrasadas</span></div>
<div class="stat warn"><b>{{ hj }}</b><span>Vencem hoje</span></div>
<div class="stat pri"><b>{{ sem }}</b><span>Próximos 7 dias</span></div>
<div class="stat"><b>{{ abertas|length }}</b><span>Em aberto</span></div>
</div>

<div class="bar">
<div class="seg">
<a class="{{ 'on' if ver=='minhas' }}" href="{{ url_for('tarefas', ver='minhas', feitas=feitas and 1 or None, cliente=cliente_id) }}">Minhas</a>
<a class="{{ 'on' if ver=='todas' }}" href="{{ url_for('tarefas', ver='todas', feitas=feitas and 1 or None, cliente=cliente_id) }}">Todas</a>
</div>
<form method="get" class="inline"><input type="hidden" name="ver" value="{{ ver }}">{% if feitas %}<input type="hidden" name="feitas" value="1">{% endif %}
<select name="cliente" onchange="this.form.submit()"><option value="">Todos os clientes</option>
{% for c in clientes %}<option value="{{ c.id }}" {{ 'selected' if c.id == cliente_id }}>{{ c.nome }}</option>{% endfor %}</select></form>
<a class="lnk" href="{{ url_for('tarefas', ver=ver, feitas=None if feitas else 1, cliente=cliente_id) }}">{{ 'Esconder concluídas' if feitas else 'Mostrar concluídas' }}</a>
</div>

{% for nome, itens in grupos.items() %}
<section class="grupo">
<div class="grupo-h"><div class="av">{{ nome[:1]|upper }}</div><h3>{{ nome }}</h3>
<span class="cnt">{{ itens|rejectattr('status','eq','feito')|list|length }} em aberto</span></div>
{% for t in itens %}
<div class="task {{ t.status }}">
<form method="post" action="{{ url_for('tarefa_atualizar', tarefa_id=t.id) }}">
<input type="hidden" name="status" value="{{ 'a_fazer' if t.status=='feito' else 'feito' }}">
<input type="checkbox" class="chk" onchange="this.form.submit()" {{ 'checked' if t.status=='feito' }} title="{{ 'Reabrir' if t.status=='feito' else 'Concluir' }}"></form>
<div><div class="t-txt">{{ t.descricao }}</div>
<div class="t-meta">
<span class="pill">{{ t.responsavel or 'Sem responsável' }}</span>
{% if not t.minha %}<span class="pill cli">Com o cliente</span>{% endif %}
{% if t.origem_ata %}<a class="pill" href="{{ url_for('ver_ata', event_id=t.origem_ata) }}" title="Ver a ata">↗ {{ t.origem_titulo or 'reunião' }}</a>{% endif %}
</div></div>
<form method="post" action="{{ url_for('tarefa_atualizar', tarefa_id=t.id) }}" class="due" title="Mudar o prazo">
{% if t.prazo %}{% set d = t.dias %}
<span class="{{ 'atr' if t.atrasada else ('hj' if d == 0 else '') }}">
{% if t.status == 'feito' %}{{ t.prazo[8:10] }}/{{ t.prazo[5:7] }}
{% elif d < -1 %}Atrasada {{ -d }} dias{% elif d == -1 %}Venceu ontem{% elif d == 0 %}Hoje{% elif d == 1 %}Amanhã
{% elif d <= 6 %}Em {{ d }} dias{% else %}{{ t.prazo[8:10] }}/{{ t.prazo[5:7] }}{% endif %}</span>
{% else %}<span class="sem">+ prazo</span>{% endif %}
<input type="date" name="prazo" value="{{ t.prazo or '' }}" onchange="this.form.submit()"></form>
<form method="post" action="{{ url_for('tarefa_atualizar', tarefa_id=t.id) }}" class="stw">
<select name="status" class="st {{ t.status }}" onchange="this.form.submit()">
{% for s, rot in [('a_fazer','A fazer'),('fazendo','Fazendo'),('feito','Feito')] %}<option value="{{ s }}" {{ 'selected' if t.status==s }}>{{ rot }}</option>{% endfor %}
</select></form>
<form method="post" action="{{ url_for('tarefa_atualizar', tarefa_id=t.id) }}" onsubmit="return confirm('Excluir esta tarefa?')">
<input type="hidden" name="excluir" value="1"><button class="del" title="Excluir">×</button></form>
</div>
{% endfor %}
</section>
{% else %}
<div class="grupo vazio"><b>{{ 'Nada pendente por aqui' if not feitas else 'Nenhuma tarefa' }}</b>
As ações combinadas nas reuniões entram sozinhas nesta lista quando a ata é gerada.</div>
{% endfor %}

<details class="novo"><summary>+ Nova tarefa</summary>
<form method="post" action="{{ url_for('tarefa_nova') }}">
<input class="full" name="descricao" placeholder="O que precisa ser feito" required>
<div>{{ seletor_cliente(cliente_id)|safe }}</div>
<input name="responsavel" value="{{ seu_nome }}" placeholder="Responsável">
<input type="date" name="prazo">
<label class="mut" style="display:flex;align-items:center;gap:8px"><input type="checkbox" name="minha" value="1" checked style="width:auto"> É minha (eu ou minha equipe)</label>
<div class="full"><button>Adicionar tarefa</button></div>
</form></details>

<details class="novo"><summary>Clientes ({{ clientes|length }})</summary>
<div style="padding:0 16px 16px">
{% for c in clientes %}<form method="post" action="{{ url_for('cliente_renomear', cliente_id=c.id) }}" class="row" style="justify-content:flex-start;margin:6px 0;flex-wrap:nowrap">
<div class="av">{{ c.nome[:1]|upper }}</div><input name="nome" value="{{ c.nome }}"><button class="sec">Renomear</button>
<a class="btn sec" href="{{ url_for('tarefas', ver='todas', cliente=c.id) }}">Tarefas</a></form>{% else %}<p class="mut">Nenhum cliente ainda.</p>{% endfor %}
<form method="post" action="{{ url_for('cliente_novo') }}" class="row" style="justify-content:flex-start;margin-top:12px;flex-wrap:nowrap">
<input name="nome" placeholder="Nome do novo cliente" required><button>Cadastrar</button></form></div></details>""",
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
        sincronia.iniciar_automatico()
        if config.LEMBRETES:
            lembretes.iniciar(sugerir_cliente)
        # Deixa os modelos de voz prontos antes da primeira reunião
        threading.Thread(target=lambda: [gravador.carregar_modelo(config.WHISPER_MODELO_AO_VIVO),
                                         gravador.carregar_modelo()], daemon=True).start()
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        app.run(port=config.PORTA, debug=False)
