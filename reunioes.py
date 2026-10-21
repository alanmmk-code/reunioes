"""Assistente de reuniões do Google Meet.

Uso:
    python reunioes.py          abre o painel web (http://localhost:5055)
    python reunioes.py login    conecta sua conta Google
    python reunioes.py auto     gera atas das reuniões que já terminaram (para agendar no Windows)
"""

import json
import os
import re
from urllib.parse import quote, urlencode
from html import escape
import socket
import subprocess
import sys
import threading
import time
import traceback
import wave
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, flash, redirect, render_template_string, request, url_for

import analisador
import assistente
import atas
import clientes
import config
import db
import envio
import extras
import google_services as g
import gravador
import ia
import lembretes
import monitor
import service_desk
import sincronia

app = Flask(__name__)
app.secret_key = "reunioes-local"

_HOSTS_DO_PAINEL = {"localhost", "127.0.0.1"}


@app.before_request
def so_o_proprio_painel():
    """O painel só atende quem é dele. Qualquer site aberto no navegador consegue mandar um POST para
    localhost (excluir cliente, enviar e-mail pelo Outlook, reiniciar...): pedido que altera algo só vale
    vindo das páginas do painel, da extensão do Chrome ou de programas locais (janelinha, sem Origin).
    E um site cujo endereço aponte para 127.0.0.1 (DNS rebinding) não consegue nem ler as páginas."""
    host = (request.host or "").rsplit(":", 1)[0] if not (request.host or "").startswith("[") else ""
    if host not in _HOSTS_DO_PAINEL:
        return "Endereço não reconhecido pelo painel.", 403
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origem = request.headers.get("Origin")
    if origem is None:  # navegadores sempre mandam Origin num POST; sem ele, é um programa deste PC
        return None
    if origem.startswith("chrome-extension://"):
        return None
    try:
        from urllib.parse import urlsplit
        o = urlsplit(origem)
        if o.scheme == "http" and o.hostname in _HOSTS_DO_PAINEL and (o.port or 80) == config.PORTA:
            return None
    except ValueError:
        pass
    print(f"[seguranca] recusado {request.method} {request.path} vindo de {origem[:80]}")
    return "Pedido recusado: não veio do painel.", 403


def seletor_remetente(nome: str = "remetente") -> str:
    """<select> com as contas do Outlook, para escolher de qual e-mail enviar."""
    contas, padrao = envio.contas(), envio.remetente_padrao()
    if not contas:  # ainda carregando do Outlook (ou Outlook fechado): deixa digitar
        return (f'<label class="mut remetente">Enviar de <input name="{nome}" value="{escape(padrao)}" '
                'placeholder="seu@email.com" style="width:260px"> <span>(conta do Outlook deste PC)</span></label>')
    opcoes = "".join(f'<option value="{escape(c)}" {"selected" if c.lower() == padrao.lower() else ""}>{escape(c)}</option>'
                     for c in contas)
    return f'<label class="mut remetente">Enviar de <select name="{nome}" style="width:auto">{opcoes}</select></label>'


app.jinja_env.globals.update(seletor_remetente=seletor_remetente)

GRAVADOR = gravador.Gravador()


@app.after_request
def _sincronizar_apos_alteracao(resposta):
    if request.method == "POST" and not request.path.startswith(("/api/", "/sincronizar")):
        sincronia.agendar()
    return resposta
AO_VIVO: assistente.AoVivo | None = None  # assistente da reunião em andamento
# Andamento do processamento depois que a gravação para (transcrição -> ata)
TAREFA = {"ativa": False, "etapa": "", "pct": 0, "erro": None, "event_id": None, "titulo": "", "transcricao_salva": None,
          "cancelavel": False, "cancelada": False, "etapa_desde": 0.0}
CANCELAR_ATA = threading.Event()  # ligado pelo botão Cancelar; conferido na transcrição e na chamada ao Claude
FILA: list[dict] = []  # atas esperando a vez enquanto outra é gerada: {"chave", "titulo", "event_id", "cancelada"}


# ---------------------------------------------------------------- Lógica principal

def processar(reuniao: dict, transcricao: str, origem: str) -> dict:
    """Transcrição -> ata (Claude) -> Google Doc -> salva localmente -> tarefas do cliente."""
    cli = db.cliente(reuniao.get("cliente_id"))
    reuniao = {**reuniao, "cliente_id": cli["id"] if cli else None, "cliente_nome": cli["nome"] if cli else None}
    ata = analisador.gerar_ata(transcricao, reuniao)
    ia.conferir_cancelamento()
    TAREFA["cancelavel"] = False  # daqui em diante cria o Google Doc: não dá mais para voltar atrás pela metade
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
    """Cliente provável: pelos e-mails/domínio cadastrados no cliente; senão, pelas atas anteriores
    com as mesmas pessoas ou o mesmo título. O cliente escolhido na tela de resumo vale sobre tudo isso."""
    fixado = db.cliente_por_uuid(reuniao.get("cliente_uuid")) if reuniao.get("cliente_uuid") else None
    if fixado and not fixado.get("excluido"):
        return fixado["id"]
    emails = {p["email"].lower() for p in reuniao.get("participantes", [])}
    for email in emails:
        cid = db.cliente_por_email(email)
        if cid:
            return cid
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


def enviar_ata(event_id: str, destinatarios: list[str], remetente: str | None = None) -> None:
    reg = atas.carregar(event_id)
    ata = analisador.Ata.model_validate(reg["ata"])
    html = atas.para_html(ata, reg["reuniao"])
    html = html.replace(
        "</h1>", f'</h1><p><a href="{reg["doc_link"]}">Abrir a ata no Google Docs</a></p>', 1
    )
    envio.enviar_email(remetente or envio.remetente_padrao(), destinatarios, f"Ata: {ata.titulo}", html)
    reg["email_enviado_para"] = sorted(set(reg["email_enviado_para"]) | set(destinatarios))
    atas.salvar(event_id, reg)


_fila_processamento = threading.Lock()


def entrar_na_fila(reuniao: dict) -> dict:
    """Chamado pela rota antes de abrir a thread: se nada está sendo gerado, o painel já mostra 'Preparando';
    senão a ata aparece na fila, com o seu próprio Cancelar."""
    item = {"chave": os.urandom(4).hex(), "titulo": reuniao.get("titulo", ""), "event_id": reuniao["id"],
            "cancelada": False}
    FILA.append(item)
    if not TAREFA["ativa"]:
        TAREFA.update(ativa=True, etapa="Preparando", pct=0, erro=None, cancelada=False, cancelavel=False,
                      event_id=reuniao["id"], titulo=item["titulo"], etapa_desde=time.time())
    return item


def tarefa_para_tela() -> dict:
    """TAREFA mais o que a tela precisa: há quanto tempo está na etapa e as atas na fila."""
    minutos = max(0, int((time.time() - TAREFA["etapa_desde"]) // 60)) if TAREFA["etapa_desde"] else 0
    return {**TAREFA, "ha": f"há {minutos} min" if minutos else "há menos de 1 min", "fila": list(FILA)}


def _sair_da_fila(item: dict | None) -> bool:
    """Tira da fila ao chegar a vez. Devolve False se o usuário cancelou enquanto ela esperava."""
    if item is None:
        return True
    if item in FILA:
        FILA.remove(item)
    return not item["cancelada"]


def processar_gravacao(reuniao: dict, arquivos: dict, avisar: bool = False, item: dict | None = None) -> None:
    """Roda em segundo plano: Whisper -> Claude -> Google Doc.
    avisar=True (gravação automática): notifica e abre a ata no navegador ao terminar."""
    with _fila_processamento:  # uma gravação por vez
        if _sair_da_fila(item):
            _processar_gravacao(reuniao, arquivos, avisar)
        else:
            _devolver_para_pendente(reuniao, arquivos)
            _liberar_preparando()


def _liberar_preparando() -> None:
    """Item cancelado na fila: se era ele quem deixou o painel em 'Preparando' e não há mais ninguém esperando, solta a tarefa."""
    if not FILA:
        TAREFA.update(ativa=False, cancelavel=False)


def _etapa(etapa: str, **outros) -> None:
    TAREFA.update(etapa=etapa, etapa_desde=time.time(), **outros)


def _comecar_tarefa(reuniao: dict, etapa: str, pct: int) -> None:
    CANCELAR_ATA.clear()
    ia.cancelavel(CANCELAR_ATA)
    TAREFA.update(ativa=True, pct=pct, erro=None, transcricao_salva=None, cancelavel=True,
                  cancelada=False, event_id=reuniao["id"], titulo=reuniao.get("titulo", ""))
    _etapa(etapa)


def transcricao_ja_feita(arquivos: dict) -> Path | None:
    """Transcrição do Whisper guardada de uma tentativa anterior (cancelada ou com erro no Claude)."""
    if not arquivos:
        return None
    txt = Path(next(iter(arquivos.values()))).with_suffix(".txt")
    try:
        return txt if txt.read_text(encoding="utf-8", errors="replace").strip() else None
    except OSError:
        return None


def _progresso_da_transcricao(pct: int) -> None:
    TAREFA.update(pct=pct)
    ia.conferir_cancelamento()  # o Whisper avisa o andamento a cada trecho: é ali que o Cancelar entra


def _ata_cancelada(reuniao: dict, arquivos: dict, ao_vivo: str = "") -> None:
    """Cancelada pelo usuário: a gravação volta para 'aguardando decisão' (dá para gerar depois ou apagar)."""
    _etapa("Cancelada", cancelada=True)
    _devolver_para_pendente(reuniao, arquivos, ao_vivo)


def _devolver_para_pendente(reuniao: dict, arquivos: dict, ao_vivo: str = "") -> None:
    voce = arquivos.get("voce")
    if voce is None:
        return
    pid = Path(voce).name.removesuffix("-voce.wav")
    arq = config.GRAVACOES_DIR / f"{pid}.json"
    dados = _ler_pendente(arq) if arq.exists() else None
    if dados:
        dados.pop("resolvida", None)
        _gravar_pendente(pid, dados)
        return
    segundos = 0.0
    try:
        with wave.open(str(voce)) as w:
            segundos = w.getnframes() / w.getframerate()
    except (OSError, wave.Error, ZeroDivisionError):
        pass
    salvar_pendente(reuniao, {n: Path(c) for n, c in arquivos.items()}, ao_vivo, segundos)


def _processar_gravacao(reuniao: dict, arquivos: dict, avisar: bool) -> None:
    _comecar_tarefa(reuniao, "Transcrevendo o áudio", 0)
    try:
        txt = transcricao_ja_feita(arquivos)
        if txt:  # o Whisper já rodou numa tentativa anterior: vai direto para o Claude
            texto = txt.read_text(encoding="utf-8")
        else:
            texto = gravador.transcrever(arquivos, progresso=_progresso_da_transcricao, reuniao=reuniao)
            ia.conferir_cancelamento()
            if not texto:
                raise RuntimeError("Nenhuma fala foi reconhecida no áudio gravado.")
            # Guarda a transcrição antes de chamar o Claude, para não perdê-la se algo falhar
            txt = next(iter(arquivos.values())).with_suffix(".txt")
            txt.write_text(texto, encoding="utf-8")
        _etapa("Escrevendo a ata com o Claude", pct=100, transcricao_salva=str(txt))
        processar(reuniao, texto, f"Gravação no PC + Whisper ({config.WHISPER_MODELO})")
        TAREFA.update(etapa="Concluído")
        # Ata pronta e a reunião tem vídeo com áudio: os áudios separados não são mais necessários
        voce = arquivos.get("voce")
        if voce is not None and voce.with_name(voce.name.removesuffix("-voce.wav") + ".webm").exists():
            for c in arquivos.values():
                c.unlink(missing_ok=True)
        if avisar:
            monitor.notificar("Ata pronta", f"{reuniao.get('titulo', '')} — abrindo suas tarefas")
            webbrowser.open(f"http://localhost:{config.PORTA}{tarefas_da_ata(reuniao['id'])}")
    except ia.Cancelado:
        _ata_cancelada(reuniao, arquivos)
    except Exception as e:
        traceback.print_exc()
        TAREFA.update(erro=str(e))
        if not atas.carregar(reuniao["id"]):  # dá para tentar de novo pelo painel
            _devolver_para_pendente(reuniao, arquivos)
        if avisar:
            monitor.notificar("Erro ao gerar a ata", str(e)[:150])
    finally:
        ia.cancelavel(None)
        TAREFA.update(ativa=False, cancelavel=False)


def processar_texto(reuniao: dict, texto: str, arquivos: dict | None = None, item: dict | None = None) -> None:
    """Gera a ata a partir de uma transcrição já pronta (ex.: a feita ao vivo, quando o áudio está em outro PC).
    `arquivos` são os da gravação pendente, para ela voltar a aguardar decisão se o usuário cancelar."""
    with _fila_processamento:
        if not _sair_da_fila(item):
            _devolver_para_pendente(reuniao, arquivos or {}, texto)
            _liberar_preparando()
            return
        _comecar_tarefa(reuniao, "Escrevendo a ata com o Claude", 100)
        try:
            processar(reuniao, texto, "Transcrição feita ao vivo")
            TAREFA.update(etapa="Concluído")
        except ia.Cancelado:
            _ata_cancelada(reuniao, arquivos or {}, texto)
        except Exception as e:
            traceback.print_exc()
            TAREFA.update(erro=str(e))
            if not atas.carregar(reuniao["id"]):  # dá para tentar de novo pelo painel
                _devolver_para_pendente(reuniao, arquivos or {}, texto)
        finally:
            ia.cancelavel(None)
            TAREFA.update(ativa=False, cancelavel=False)


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


def iniciar_gravacao(reuniao: dict, automatica: bool, pela_extensao: bool = False) -> None:
    """Começa a gravar, liga o assistente ao vivo e abre a janelinha de sugestões."""
    global AO_VIVO
    reuniao.setdefault("cliente_id", None)
    reuniao["cliente_sugerido"] = sugerir_cliente(reuniao)
    GRAVADOR.iniciar(reuniao, pela_extensao=pela_extensao)
    GRAVADOR.automatica = automatica
    if config.ASSISTENTE_AO_VIVO:
        AO_VIVO = assistente.AoVivo(GRAVADOR, reuniao)
        AO_VIVO.iniciar()
    JANELA["fechada_pelo_usuario"] = False
    abrir_janela()


# ---------------------------------------------------------------- Janelinha de sugestões

JANELA = {"processo": None, "fechada_pelo_usuario": False}
_janela_lock = threading.Lock()


def janela_aberta() -> bool:
    p = JANELA["processo"]
    return p is not None and p.poll() is None


def abrir_janela() -> None:
    """Abre a janelinha (pergunta o cliente e mostra as sugestões), se ainda não estiver aberta."""
    with _janela_lock:  # o vigia e o início da gravação podem chamar ao mesmo tempo
        _abrir_janela()


def _abrir_janela() -> None:
    if janela_aberta():
        return
    pythonw = config.BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    log = open(config.BASE_DIR / "janela.log", "a", encoding="utf-8")
    log.write(f"\n--- {datetime.now():%d/%m %H:%M:%S} abrindo a janela\n")
    log.flush()
    JANELA["processo"] = subprocess.Popen(
        [str(pythonw if pythonw.exists() else sys.executable), "-X", "faulthandler", str(config.BASE_DIR / "janela.py")],
        cwd=str(config.BASE_DIR), stdout=log, stderr=log,
    )

    def registrar_saida(processo=JANELA["processo"]):
        codigo = processo.wait()
        print(f"[janela] fechou (código {codigo}; fechada pelo usuário: {JANELA['fechada_pelo_usuario']})")
    threading.Thread(target=registrar_saida, daemon=True).start()


def vigiar_janela() -> None:
    """Durante a gravação, reabre a janela se ela fechar sozinha (não se você a fechou)."""
    def loop():
        while True:
            time.sleep(5)
            try:
                if GRAVADOR.ativo and not JANELA["fechada_pelo_usuario"] and not janela_aberta():
                    print("[janela] não estava aberta durante a gravação; reabrindo")
                    abrir_janela()
            except Exception:
                traceback.print_exc()
    threading.Thread(target=loop, daemon=True, name="vigia-janela").start()


@app.post("/envio/<int:envio_id>/<acao>")
def envio_acao(envio_id, acao):
    """Conferir de novo / dispensar um e-mail ou convite que ficou preso no Outlook."""
    e = next((x for x in envio.ENVIOS if x["id"] == envio_id), None)
    if e and acao == "dispensar":
        e["estado"] = "dispensado"
    elif e and acao == "conferir":
        estado = envio.conferir(e)
        flash("Saiu! Não está mais na Caixa de Saída." if estado == "enviado"
              else "Continua na Caixa de Saída do Outlook. Abra o Outlook, entre na conta e aperte F9 (Enviar/Receber).")
    return redirect(request.referrer or url_for("inicio"))


@app.post("/api/janela/fechada")
def api_janela_fechada():
    JANELA["fechada_pelo_usuario"] = True
    return {"ok": True}


@app.post("/api/janela/abrir")
def api_janela_abrir():
    JANELA["fechada_pelo_usuario"] = False
    abrir_janela()
    return redirect(request.referrer or url_for("inicio")) if request.form or not request.is_json else {"ok": True}


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
    try:
        antigo = arquivos["voce"].name.removesuffix("-voce.wav")
        novo = _renomear_arquivos(antigo, nome_da_gravacao(reuniao, _inicio_da_gravacao(antigo)))
        arquivos = {n: c.with_name(novo + c.name[len(antigo):]) for n, c in arquivos.items()}
    except Exception:
        traceback.print_exc()  # fica com o nome técnico; nada se perde
    return reuniao, arquivos, ao_vivo.transcricao() if ao_vivo else ""


# ---------------------------------------------------------------- Nome dos arquivos da gravação

_PROIBIDOS_NO_NOME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# O que vem depois do nome-base nos arquivos de uma gravação
_FIM_ARQUIVO_GRAVACAO = re.compile(r"(\.json|-(voce|outros|comparar)\.(wav|txt)|(-tela)?(-\d+)?\.webm)")


def _eh_da_gravacao(nome_arquivo: str, pid: str) -> bool:
    return nome_arquivo.startswith(pid) and bool(_FIM_ARQUIVO_GRAVACAO.fullmatch(nome_arquivo[len(pid):]))


def _inicio_da_gravacao(pid: str, dados: dict | None = None) -> datetime:
    """Quando a gravação começou: do .json (gravações novas) ou do nome técnico (AAAAMMDD-HHMMSS-...)."""
    try:
        return datetime.fromisoformat((dados or {}).get("gravada_em") or "")
    except ValueError:
        pass
    try:
        return datetime.strptime(pid[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return datetime.now()


def nome_da_gravacao(reuniao: dict, inicio: datetime) -> str:
    """Nome fácil de achar na pasta: "2026-10-07 15h49 - Cliente - com Fulano, Beltrano"."""
    cli = db.cliente(reuniao.get("cliente_id"))
    partes = [f"{inicio:%Y-%m-%d %Hh%M}", cli["nome"] if cli else "sem cliente"]
    if reuniao.get("na_chamada"):
        partes.append("com " + ", ".join(reuniao["na_chamada"]))
    nome = " ".join(_PROIBIDOS_NO_NOME.sub("", " - ".join(partes)).split())
    while ".." in nome:
        nome = nome.replace("..", ".")
    return nome[:120].rstrip(". ")


def _renomear_arquivos(antigo: str, novo: str) -> str:
    """Troca o nome-base de todos os arquivos da gravação (áudios, vídeo, transcrição, .json).
    Devolve o nome usado (com " (2)" se já existir outra gravação com o mesmo nome)."""
    if not novo or novo == antigo:
        return antigo
    base, n = novo, 2
    while any((config.GRAVACOES_DIR / f"{novo}{fim}").exists() for fim in (".json", "-voce.wav")):
        novo, n = f"{base} ({n})", n + 1
    trocados = []
    for arq in list(config.GRAVACOES_DIR.iterdir()):
        if _eh_da_gravacao(arq.name, antigo):
            arq.rename(arq.with_name(novo + arq.name[len(antigo):]))
            trocados.append(f"grav__{arq.name}")
    if trocados:  # os outros PCs apagam a cópia com o nome antigo e baixam a nova
        sincronia.marcar_excluido(*trocados)
        sincronia.agendar()
    return novo


def ao_detectar_meet(titulo_janela: str, pela_extensao: bool = False) -> None:
    if GRAVADOR.ativo:
        return
    reuniao = _reuniao_da_janela(titulo_janela)
    try:
        iniciar_gravacao(reuniao, automatica=True, pela_extensao=pela_extensao)
        print(f"[{'extensao' if pela_extensao else 'monitor'}] gravando: {reuniao['titulo']}")
        monitor.notificar("Gravando reunião", f"{reuniao['titulo']} — o assistente está ouvindo.")
    except Exception as e:
        traceback.print_exc()
        monitor.notificar("Não consegui gravar a reunião", str(e)[:150])


def ao_encerrar_meet() -> str | None:
    """Encerra a gravação automática. Devolve o id da gravação pendente (None se não havia ou foi descartada)."""
    if not (GRAVADOR.ativo and GRAVADOR.automatica):
        return None
    inicio = GRAVADOR.inicio
    segundos = (datetime.now() - inicio).total_seconds()
    reuniao, arquivos, ao_vivo = encerrar_gravacao()
    if segundos < config.DURACAO_MINIMA_SEG:
        for c in arquivos.values():
            c.unlink(missing_ok=True)
        print(f"[monitor] gravação de {segundos:.0f}s descartada (curta demais)")
        _vincular_telas(None)
        return None
    pid = salvar_pendente(reuniao, arquivos, ao_vivo, segundos)
    videos = _vincular_telas(pid)
    if videos:
        threading.Thread(target=_juntar_audio_nos_videos, args=(pid, videos, inicio, arquivos), daemon=True).start()
    print(f"[monitor] chamada encerrada após {segundos / 60:.0f} min; aguardando decisão sobre a ata")
    monitor.notificar("Reunião encerrada", "Quer gerar a ata? Abri a pergunta no navegador.")
    webbrowser.open(f"http://localhost:{config.PORTA}/pendente/{quote(pid)}")
    return pid


# ---------------------------------------------------------------- Extensão do Chrome (áudio só da aba do Meet)

# A extensão (pasta extensao/) avisa a cada segundo se a aba do Meet está numa chamada e manda o áudio
# dos outros participantes. Com ela, a gravação começa e termina pela chamada de verdade, e a trilha
# "outros" tem só o som da reunião (nada de outras abas ou programas).
EXTENSAO = {"sinal_em": 0.0, "aba": None, "aba_sinal_em": 0.0, "fora_desde": None, "log_em": 0.0, "pico": 0.0,
            "telas": {}, "telas_inicio": {}, "som_em": 0.0, "mudo_avisado": False}  # telas: aba -> vídeo da aba (gravação da tela); quando começou
EXTENSAO_ATIVA_SEG = 30  # sem sinal há mais que isso: extensão ausente, vale o detector antigo
EXTENSAO_SEM_SINAL_FIM = 20  # gravando e a aba parou de mandar sinal (fechou, Chrome caiu): encerra
SOM_MINIMO = 0.01  # pico abaixo disso é silêncio
SOM_PC_SEG = 45  # a extensão sem som por esse tempo: a trilha "outros" passa a gravar o som do PC (alto-falante/fone)
SOM_MUDO_SEG = 180  # sem som e sem conseguir usar o som do PC por esse tempo: avisa
_extensao_lock = threading.Lock()


def extensao_ativa() -> bool:
    return time.time() - EXTENSAO["sinal_em"] <= EXTENSAO_ATIVA_SEG


def _gravando_pela_extensao() -> bool:
    return GRAVADOR.ativo and GRAVADOR.automatica and GRAVADOR.pela_extensao


@app.post("/api/extensao/estado")
def api_extensao_estado():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        d = {}
    aba, em_chamada, titulo = d.get("aba"), bool(d.get("em_chamada")), d.get("titulo") or "Meet"
    agora = time.time()
    with _extensao_lock:
        EXTENSAO["sinal_em"] = agora
        if _gravando_pela_extensao():
            if aba != EXTENSAO["aba"]:
                return {"ok": True, "gravando": False}  # outra aba do Meet: ignora
            EXTENSAO["aba_sinal_em"] = agora
            try:
                pico = float(d.get("pico") or 0)
            except (TypeError, ValueError):
                pico = 0.0
            EXTENSAO["pico"] = max(EXTENSAO["pico"], pico)
            if pico > SOM_MINIMO:
                EXTENSAO.update(som_em=agora, mudo_avisado=False)
                if GRAVADOR.usando_som_do_pc:
                    GRAVADOR.usar_som_do_pc(False)
                    print("[extensao] o som da aba voltou: a trilha dos outros volta a vir da extensão")
            elif em_chamada and agora - EXTENSAO["som_em"] >= SOM_PC_SEG and not GRAVADOR.usando_som_do_pc:
                if GRAVADOR.usar_som_do_pc(True):
                    print(f"[extensao] {SOM_PC_SEG}s sem som da aba: gravando os outros pelo som do PC "
                          f"(trilhas={d.get('trilhas')})")
            if (pico <= SOM_MINIMO and em_chamada and not GRAVADOR.usando_som_do_pc
                    and agora - EXTENSAO["som_em"] >= SOM_MUDO_SEG and not EXTENSAO["mudo_avisado"]):
                EXTENSAO["mudo_avisado"] = True
                print(f"[extensao] AVISO: nenhum som da chamada há {SOM_MUDO_SEG // 60} min (trilhas={d.get('trilhas')})")
                threading.Thread(target=monitor.notificar, daemon=True, args=(
                    "Som da chamada não está sendo gravado",
                    f"Há {SOM_MUDO_SEG // 60} min não chega som dos outros participantes. Se eles estão falando, "
                    "use a caixa de som (sem fone) para o microfone captar.",
                    f"http://localhost:{config.PORTA}/gravacao")).start()
            if agora - EXTENSAO["log_em"] >= 30:  # diagnóstico: o áudio da aba está chegando?
                print(f"[extensao] trilhas={d.get('trilhas')} áudio={d.get('audio_ctx')} pico 30s={EXTENSAO['pico']:.3f}"
                      + (" (gravando pelo som do PC)" if GRAVADOR.usando_som_do_pc else "")
                      + (f" diag={d['diag']}" if isinstance(d.get("diag"), dict) else ""))
                EXTENSAO["log_em"], EXTENSAO["pico"] = agora, 0.0
            if em_chamada:
                EXTENSAO["fora_desde"] = None
            else:
                EXTENSAO["fora_desde"] = EXTENSAO["fora_desde"] or agora
                if d.get("fim") or agora - EXTENSAO["fora_desde"] >= monitor.FIM_APOS:
                    EXTENSAO["fora_desde"], EXTENSAO["aba"] = None, None
                    threading.Thread(target=_encerrar_pela_extensao, args=(aba,), daemon=True).start()
                    return {"ok": True, "gravando": False}
            return {"ok": True, "gravando": True}
        if em_chamada and not GRAVADOR.ativo:
            EXTENSAO.update(aba=aba, aba_sinal_em=agora, fora_desde=None, som_em=agora, mudo_avisado=False)
            ao_detectar_meet(titulo, pela_extensao=True)
            return {"ok": True, "gravando": GRAVADOR.ativo and GRAVADOR.pela_extensao}
    return {"ok": True, "gravando": False}


@app.post("/api/extensao/audio")
def api_extensao_audio():
    """PCM 16 kHz mono, int16 little-endian, da aba que está sendo gravada."""
    if _gravando_pela_extensao() and request.args.get("aba", type=int) == EXTENSAO["aba"]:
        import numpy as np

        GRAVADOR.alimentar_outros(np.frombuffer(request.get_data(), dtype="<i2").astype(np.float32) / 32768.0)
    return {"ok": True}


@app.post("/api/extensao/tela")
def api_extensao_tela():
    """Vídeo da aba do Meet: pedaços WebM do MediaRecorder, em ordem. parte=0 começa um arquivo novo."""
    aba, parte = request.args.get("aba", type=int), request.args.get("parte", type=int, default=1)
    with _extensao_lock:
        caminho = EXTENSAO["telas"].get(aba)
        if parte == 0:
            caminho = config.GRAVACOES_DIR / f"tela-{datetime.now():%Y%m%d-%H%M%S}-aba{aba}.webm"
            EXTENSAO["telas"][aba] = caminho
            # Quando o vídeo começou (para alinhar com o áudio); sem a informação: 5 s antes (1º pedaço)
            inicio_ms = request.args.get("inicio", type=float)
            EXTENSAO["telas_inicio"][aba] = inicio_ms / 1000 if inicio_ms else time.time() - 5
        elif caminho is None:
            return {"ok": False}  # sem o começo do arquivo o vídeo não abre
        with open(caminho, "ab") as f:
            f.write(request.get_data())
    return {"ok": True}


@app.post("/api/extensao/erro")
def api_extensao_erro():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        d = {}
    print(f"[extensao] erro em {str(d.get('onde'))[:40]}: {str(d.get('erro'))[:300]}")
    return {"ok": True}


def _vincular_telas(pid: str | None) -> list[tuple]:
    """Vídeos da tela gravados durante a gravação que terminou (ainda com nome provisório tela-*):
    passam a se chamar <pid>-tela.webm, junto dos áudios (e são apagados junto).
    Gravação descartada: apaga o vídeo. Devolve [(aba, vídeo, início do vídeo em epoch)]."""
    vinculados = []
    with _extensao_lock:
        for aba, caminho in list(EXTENSAO["telas"].items()):
            if not caminho.name.startswith("tela-") or not caminho.exists():
                continue
            if pid is None:
                caminho.unlink(missing_ok=True)
                EXTENSAO["telas"].pop(aba, None)
                continue
            novo = config.GRAVACOES_DIR / f"{pid}-tela.webm"
            if novo.exists():  # duas abas gravando: a segunda ganha um sufixo
                novo = config.GRAVACOES_DIR / f"{pid}-tela-{aba}.webm"
            caminho.rename(novo)
            EXTENSAO["telas"][aba] = novo  # pedaços que ainda chegarem vão para o arquivo novo
            vinculados.append((aba, novo, EXTENSAO["telas_inicio"].get(aba)))
            print(f"[extensao] vídeo da tela: {novo.name}")
    return vinculados


def _juntar_audio_nos_videos(pid: str, videos: list[tuple], inicio_audio: datetime, arquivos: dict) -> None:
    """O vídeo da tela vem sem som: quando ele termina de chegar, vira <pid>.webm com o áudio da reunião
    (você + outros). O vídeo mudo é apagado; os .wav ficam até a ata ser gerada."""
    for i, (aba, video, inicio_video) in enumerate(videos):
        try:
            # Espera os últimos pedaços (a extensão para o vídeo ao sair da chamada)
            tamanho, parado_desde, limite = -1, time.time(), time.time() + 90
            while time.time() < limite and time.time() - parado_desde < 8:
                atual = video.stat().st_size if video.exists() else -1
                if atual != tamanho:
                    tamanho, parado_desde = atual, time.time()
                time.sleep(1)
            if not video.exists():
                continue
            saida = config.GRAVACOES_DIR / (f"{pid}.webm" if i == 0 else f"{pid}-{i + 1}.webm")
            audios = [arquivos[n] for n in ("voce", "outros") if n in arquivos and arquivos[n].exists()]
            atraso = (inicio_video or video.stat().st_ctime) - inicio_audio.timestamp()
            gravador.juntar_audio_no_video(video, audios, saida, atraso)
            with _extensao_lock:
                video.unlink(missing_ok=True)
                if EXTENSAO["telas"].get(aba) == video:
                    EXTENSAO["telas"].pop(aba, None)
            print(f"[extensao] vídeo com áudio: {saida.name}")
        except Exception:
            traceback.print_exc()  # fica o vídeo mudo + os áudios separados


def _encerrar_pela_extensao(aba) -> None:
    ao_encerrar_meet()


def vigiar_extensao() -> None:
    """A aba sumiu sem avisar (fechou o Chrome, travou): encerra a gravação."""
    def loop():
        while True:
            time.sleep(5)
            try:
                if _gravando_pela_extensao() and time.time() - EXTENSAO["aba_sinal_em"] > EXTENSAO_SEM_SINAL_FIM:
                    print("[extensao] a aba do Meet parou de responder; encerrando a gravação")
                    aba, EXTENSAO["aba"] = EXTENSAO["aba"], None
                    _encerrar_pela_extensao(aba)
            except Exception:
                traceback.print_exc()
    threading.Thread(target=loop, daemon=True, name="vigia-extensao").start()


# ---------------------------------------------------------------- Gravações aguardando decisão

def salvar_pendente(reuniao: dict, arquivos: dict, transcricao_ao_vivo: str, segundos: float) -> str:
    pid = arquivos["voce"].name.removesuffix("-voce.wav")
    dados = {"reuniao": {k: v for k, v in reuniao.items() if k != "anexos"},
             "gravada_em": (datetime.now() - timedelta(seconds=segundos)).isoformat(timespec="seconds"),
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


def _ler_pendente(arq) -> dict | None:
    """Lê uma gravação pendente; ignora arquivo corrompido ou fora do formato."""
    try:
        dados = json.loads(arq.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not isinstance(dados, dict) or not isinstance(dados.get("reuniao"), dict) or not isinstance(dados.get("arquivos"), dict):
        return None
    dados["reuniao"].setdefault("titulo", "Reunião")
    dados.setdefault("minutos", 0)
    dados.setdefault("encerrada_em", "")
    dados.setdefault("transcricao_ao_vivo", "")
    return dados


def carregar_pendente(pid: str) -> dict | None:
    if not pid or "/" in pid or "\\" in pid or ".." in pid:
        return None
    arq = config.GRAVACOES_DIR / f"{pid}.json"
    if not arq.exists():
        return None
    dados = _ler_pendente(arq)
    return None if not dados or dados.get("resolvida") else dados


def listar_pendentes() -> list[dict]:
    lista = []
    for arq in sorted(config.GRAVACOES_DIR.glob("*.json"), reverse=True):
        dados = _ler_pendente(arq)
        if dados and not dados.get("resolvida"):
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
@media (max-width:600px){.sinc span:not(.dot){display:none}}
{{ css_comum|safe }}details>summary::-webkit-details-marker{display:none}
/* pendências do cliente agrupadas (ficha do cliente) */
.grp{display:flex;justify-content:space-between;gap:10px;padding:8px 16px;background:var(--card2);border-bottom:1px solid var(--bd);font-size:12.5px;font-weight:600;color:var(--tx2)}
.grp span{color:var(--mut);font-weight:500}
.seg{display:inline-flex;border:1px solid var(--bd);border-radius:8px;overflow:hidden}
.seg button{background:var(--card);color:var(--mut);border:0;border-radius:0;padding:3px 10px;font-size:12px}
.seg button.on{background:var(--pri-s);color:var(--pri)}
.seg button:hover{filter:none;color:var(--tx)}
</style></head><body>
<header class="top"><div class="in">
<a class="logo" href="{{ url_for('inicio') }}"><i>R</i>Reuniões</a>
<nav class="menu">
<a href="{{ url_for('inicio') }}" class="{{ 'on' if request.path == '/' }}">Painel</a>
<a href="{{ url_for('lista_clientes') }}" class="{{ 'on' if request.path.startswith('/cliente') }}">Clientes</a>
<a href="{{ url_for('tarefas') }}" class="{{ 'on' if request.path.startswith('/tarefas') }}">Tarefas</a>
<a href="{{ url_for('semana') }}" class="{{ 'on' if request.path.startswith('/semana') }}">Semana</a>
</nav>
{% if sinc_ligada %}<form method="post" action="{{ url_for('sincronizar_agora') }}" class="sinc" title="{{ sinc.erro or sinc.resumo or 'Sincroniza atas, tarefas e gravações com o seu Google Drive' }}">
<span class="dot {{ 'err' if sinc.erro else ('on' if sinc.ultima else '') }}"></span>
<span>{% if sinc.rodando %}Sincronizando…{% elif sinc.erro %}Erro ao sincronizar{% elif sinc.ultima %}Drive: {{ sinc.ultima.strftime('%H:%M') }}{% else %}Drive: aguardando{% endif %}</span>
<button class="sec">Sincronizar</button></form>{% endif %}
</div></header>
<main>
{% if desatualizado %}<form method="post" action="{{ url_for('reiniciar') }}" class="flash" style="display:flex;gap:12px;align-items:center;justify-content:space-between">
<span>Há uma versão nova do painel instalada (depois de um <code>git pull</code>). Reinicie para ela valer.</span>
<button>Reiniciar agora</button></form>{% endif %}
{% if google_caiu %}<form method="post" action="{{ url_for('google_conectar') }}" class="flash" style="display:flex;gap:12px;align-items:center;justify-content:space-between">
<span>A conexão com o Google caiu (a autorização venceu ou foi revogada). Agenda, atas e sincronização param até reconectar.</span>
<button>Reconectar Google</button></form>{% endif %}
{% if drive_cheio %}<div class="flash" style="border-color:var(--err)">O Google Drive está quase cheio:
{{ '%.1f'|format(drive_cheio[0]) }} de {{ '%.0f'|format(drive_cheio[1]) }} GB usados. Quando encher, as cópias de segurança
e os vídeos param de ir para lá. Libere espaço apagando vídeos antigos da pasta "{{ pasta_videos }}" ou outros arquivos
grandes (e esvazie a lixeira do Drive, que também conta).</div>{% endif %}
{% for pc, quando in pcs_parados %}<form method="post" action="{{ url_for('esquecer_pc') }}" class="flash" style="display:flex;gap:12px;align-items:center;justify-content:space-between">
<span>O PC <b>{{ pc }}</b> não sincroniza desde {{ quando.astimezone().strftime('%d/%m às %H:%M') }}. Se ele esteve ligado, a sincronização dele está com problema (veja o painel dele, por exemplo a conexão com o Google).</span>
<input type="hidden" name="pc" value="{{ pc }}"><button class="sec">Não uso mais esse PC</button></form>{% endfor %}
{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}
{{ corpo|safe }}
</main>
<script>document.querySelectorAll('form[data-espera]').forEach(f=>f.addEventListener('submit',()=>{
const b=f.querySelector('button');b.disabled=true;b.textContent=f.dataset.espera}))</script>
</body></html>"""


def pagina(corpo_tpl: str, **ctx):
    corpo = render_template_string(corpo_tpl, **ctx)
    css = (config.BASE_DIR / "telas" / "comum.css").read_text(encoding="utf-8")
    return render_template_string(BASE, corpo=corpo, sinc=sincronia.ESTADO, sinc_ligada=config.SINCRONIZAR, css_comum=css,
                                  desatualizado=codigo_mudou(), google_caiu=bool(g.ESTADO["desconectado"]),
                                  pcs_parados=sincronia.pcs_parados() if config.SINCRONIZAR else [],
                                  drive_cheio=drive_quase_cheio(), pasta_videos=config.PASTA_VIDEOS)


# ---------------------------------------------------------------- painel desatualizado (código novo em disco)

def _versao_do_codigo() -> tuple:
    """Impressão digital do código em disco: muda quando um git pull (ou edição) troca algum arquivo."""
    arquivos = sorted(config.BASE_DIR.glob("*.py")) + sorted((config.BASE_DIR / "telas").glob("*"))
    return tuple((a.name, a.stat().st_mtime_ns, a.stat().st_size) for a in arquivos if a.is_file())


VERSAO_INICIAL = _versao_do_codigo()
_versao_vista = {"em": 0.0, "mudou": False}


def codigo_mudou() -> bool:
    agora = time.time()
    if agora - _versao_vista["em"] >= 30:  # não precisa olhar o disco a cada página
        _versao_vista.update(em=agora, mudou=_versao_do_codigo() != VERSAO_INICIAL)
    return _versao_vista["mudou"]


def _ocupado() -> str | None:
    if GRAVADOR.ativo:
        return "há uma gravação em andamento"
    if TAREFA["ativa"]:
        return "uma ata está sendo gerada"
    return None


ESPACO_DRIVE = {"usado": 0, "limite": 0}  # bytes, medido de hora em hora
DRIVE_QUASE_CHEIO = 0.8


def drive_quase_cheio() -> tuple[float, float] | None:
    """(usado, limite) em GB quando o Drive passou de DRIVE_QUASE_CHEIO; senão None."""
    usado, limite = ESPACO_DRIVE["usado"], ESPACO_DRIVE["limite"]
    if limite and usado >= DRIVE_QUASE_CHEIO * limite:
        return usado / 1e9, limite / 1e9
    return None


def copias_de_seguranca() -> None:
    """Cópia do banco ao abrir (se ainda não houver a do dia) e, com o painel aberto, a cada virada de dia;
    as últimas também vão para o Drive."""
    def loop():
        while True:
            try:
                feita = db.copia_de_seguranca()
                if feita:
                    print(f"[copia] {feita.name}")
            except Exception:
                traceback.print_exc()
            try:
                enviadas = sincronia.enviar_copias()  # cópia fora do PC, contra perda do disco
                if enviadas:
                    print(f"[copia] {enviadas} cópia(s) enviada(s) ao Drive")
            except Exception as e:
                print(f"[copia] não consegui enviar ao Drive (tento de novo em 1 hora): {e}")
            try:
                ESPACO_DRIVE["usado"], ESPACO_DRIVE["limite"] = g.drive_espaco()
            except Exception as e:
                print(f"[copia] não consegui ver o espaço do Drive: {e}")
            try:
                for nome in sincronia.arquivar_videos(ocupado=lambda: bool(_ocupado())):
                    print(f"[videos] {nome} saiu do PC" + (" (está no Drive)" if config.VIDEOS_NO_DRIVE else ""))
            except Exception as e:
                print(f"[videos] não consegui arquivar (tento de novo em 1 hora): {e}")
            time.sleep(3600)
    threading.Thread(target=loop, daemon=True, name="copias").start()


def _encerrar_processo() -> None:
    os._exit(0)


@app.post("/reiniciar")
def reiniciar():
    """Sobe um painel novo (que espera a porta liberar) e encerra este, para valer o código atualizado."""
    motivo = _ocupado()
    if motivo:
        flash(f"Não reiniciei agora porque {motivo}. Tente de novo quando terminar.")
        return redirect(request.referrer or url_for("inicio"))
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "web", "--reiniciado"], cwd=str(config.BASE_DIR),
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                     close_fds=True)
    threading.Timer(0.5, _encerrar_processo).start()  # dá tempo de a página de espera chegar ao navegador
    return """<!doctype html><meta charset="utf-8"><title>Reiniciando…</title>
<body style="font-family:system-ui;padding:40px;color:#333">Reiniciando o painel com a versão nova…
<script>setTimeout(function t(){fetch('/api/versao').then(r=>r.ok?location.replace('/'):setTimeout(t,1000))
.catch(()=>setTimeout(t,1000))},2000)</script></body>"""


@app.get("/api/versao")
def api_versao():
    return {"desatualizado": codigo_mudou()}


# ---------------------------------------------------------------- reconectar o Google pelo painel

def _login_google() -> None:
    login = g.ESTADO["login"]
    try:
        login["conta"] = g.reconectar()
        g.anotar_conexao()
        clientes._conta["email"] = None  # verificações do painel refeitas com a conta nova
        clientes._status["quando"] = 0.0
        sincronia.agendar()
    except Exception as e:
        login["erro"] = str(e)[:300]
    finally:
        login["rodando"] = False


@app.post("/google/conectar")
def google_conectar():
    """Abre o login do Google no navegador (o mesmo do comando login) e espera, sem travar o painel."""
    login = g.ESTADO["login"]
    if not login["rodando"]:
        login.update(rodando=True, erro=None, conta=None)
        threading.Thread(target=_login_google, daemon=True, name="login-google").start()
    return pagina(
        "<h1>Reconectar Google</h1><p id='msg'>Abri a tela de login do Google numa aba nova do navegador. "
        "Escolha a sua conta e autorize; esta página volta sozinha ao painel quando terminar.</p>"
        "<p class='mut'>Se aparecer \"O Google não verificou este app\", clique em <b>Avançado</b> e depois em "
        "<b>Acessar</b> (o app é o seu próprio).</p>"
        "<script>(function t(){fetch('/api/google').then(r=>r.json()).then(j=>{"
        "if(j.rodando)return setTimeout(t,1500);"
        "if(j.erro){document.getElementById('msg').textContent='Não conectou: '+j.erro+'. Tente de novo pelo botão.';return}"
        "location.replace('/')}).catch(()=>setTimeout(t,3000))})()</script>")


@app.get("/api/google")
def api_google():
    login = g.ESTADO["login"]
    return {"rodando": login["rodando"], "erro": login["erro"], "conta": login["conta"],
            "desconectado": bool(g.ESTADO["desconectado"])}


@app.get("/copias")
def copias_tela():
    return pagina((config.BASE_DIR / "telas" / "copias.html").read_text(encoding="utf-8"), copias=db.copias())


@app.post("/copias/voltar")
def copias_voltar():
    nome = request.form.get("nome", "")
    try:
        voltaram = db.restaurar_copia(nome)
    except ValueError as e:
        flash(f"Não voltei: {e}.")
        return redirect(url_for("copias_tela"))
    sincronia.agendar()
    flash(f"Pronto: {voltaram} registro(s) voltaram como estavam na cópia. Para desfazer, volte para a "
          "cópia “antes de voltar” do topo da lista.")
    return redirect(url_for("copias_tela"))


@app.post("/sincronia/esquecer-pc")
def esquecer_pc():
    pc = request.form.get("pc", "")
    try:
        if sincronia.esquecer_pc(pc):
            flash(f"Pronto: o PC {pc} não será mais acompanhado (volta a aparecer se ele sincronizar de novo).")
    except Exception as e:
        flash(f"Não consegui tirar o aviso agora: {e}")
    return redirect(request.referrer or url_for("inicio"))


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
    semana = max(-520, min(520, request.args.get("semana", 0, type=int)))  # até 10 anos para cada lado
    agora = datetime.now().astimezone()
    hoje = agora.date()
    segunda = hoje - timedelta(days=hoje.weekday()) + timedelta(weeks=semana)
    ini_semana = datetime.combine(segunda, datetime.min.time(), agora.tzinfo)
    try:
        eventos = g.listar_eventos(ini_semana, ini_semana + timedelta(days=7))
        ocultos = {n[len("evento__"):] for n in sincronia.excluidos() if n.startswith("evento__")}
        recentes = [r for r in g.listar_reunioes(dias_atras=14, dias_frente=0)
                    if r["ja_terminou"] and r["id"] not in ocultos][::-1]
        # compromissos de hoje: vêm da própria semana exibida; em outra semana, busca só o dia de hoje
        eventos_de_hoje = eventos if semana == 0 else g.listar_eventos(
            datetime.combine(hoje, datetime.min.time(), agora.tzinfo), datetime.combine(hoje + timedelta(days=1), datetime.min.time(), agora.tzinfo))
    except Exception as e:
        g.anotar_conexao(e)
        return pagina(
            "<h1>Conecte sua conta Google</h1><p>{{ erro }}</p>"
            "<form method='post' action='{{ url_for(\"google_conectar\") }}'><button>Conectar Google</button></form>"
            "<p class='mut'>Se o botão não abrir o login, rode na pasta do painel "
            "<code>.venv\\Scripts\\python.exe reunioes.py login</code> e recarregue esta página "
            "(só <code>python</code> pode abrir a Microsoft Store em vez do Python do painel).</p>",
            erro=str(e),
        )
    g.anotar_conexao()

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

    historico = historico_por_cliente()
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
        gravando=GRAVADOR.ativo, titulo_gravacao=(GRAVADOR.reuniao or {}).get("titulo", ""), tarefa=tarefa_para_tela(),
        seletor_cliente=seletor_cliente, seu_nome=config.SEU_NOME, historico=historico, presos=envio.presos(),
        atencao=clientes.precisam_de_atencao(),
        status=clientes.status_sistema(sincronia.ESTADO, config.GRAVACAO_AUTOMATICA),
    )


def _breve(texto: str, limite: int = 220) -> str:
    """As primeiras frases do resumo, para bater o olho e entender a conversa."""
    texto = (texto or "").strip()
    if len(texto) <= limite:
        return texto
    corte = texto.rfind(". ", 0, limite)
    return texto[: corte + 1] if corte > 80 else texto[:limite].rsplit(" ", 1)[0] + "…"


def historico_por_cliente() -> list[dict]:
    """Atas agrupadas por cliente (mais recentes primeiro); clientes com reunião mais recente no topo."""
    nomes = {c["id"]: c["nome"] for c in db.clientes()}
    grupos = {}
    for event_id in atas.existentes():
        reg = atas.carregar(event_id) or {}
        a, r = reg.get("ata"), reg.get("reuniao", {})
        if not a:
            continue
        cid = reg.get("cliente_id") if reg.get("cliente_id") in nomes else None
        acoes = a.get("acoes", [])
        grupos.setdefault(cid, []).append({
            "event_id": event_id,
            "titulo": a.get("titulo") or r.get("titulo", "Reunião"),
            "inicio": r.get("inicio", ""),
            "breve": _breve(a.get("resumo", "")),
            "resumo": a.get("resumo", ""),
            "decisoes": a.get("decisoes", []),
            "acoes_minhas": [x for x in acoes if x.get("do_usuario", True)],
            "acoes_cliente": [x for x in acoes if not x.get("do_usuario", True)],
            "doc_link": reg.get("doc_link"),
            "busca": " ".join([nomes.get(cid, ""), a.get("titulo", ""), r.get("titulo", ""), a.get("resumo", ""),
                               " ".join(a.get("decisoes", []))]).lower(),
        })
    lista = []
    for cid, reunioes_cli in grupos.items():
        reunioes_cli.sort(key=lambda x: x["inicio"], reverse=True)
        lista.append({"cliente_id": cid, "nome": nomes.get(cid, "Sem cliente"), "reunioes": reunioes_cli,
                      "ultima": reunioes_cli[0]["inicio"]})
    lista.sort(key=lambda x: x["ultima"], reverse=True)  # reunião mais recente primeiro
    lista.sort(key=lambda x: x["cliente_id"] is None)  # "Sem cliente" por último
    return lista


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
            item = entrar_na_fila(reuniao)
            threading.Thread(target=processar_gravacao, args=(reuniao, arquivos, False, item), daemon=True).start()
        except Exception as e:
            flash(str(e))
    return redirect(url_for("gravacao"))


@app.post("/gravacao/cancelar-ata")
def cancelar_ata():
    chave = request.form.get("chave")
    na_fila = next((i for i in FILA if chave and i["chave"] == chave), None)
    if na_fila:
        na_fila["cancelada"] = True  # sai quando chegar a vez, sem gerar nada
        FILA.remove(na_fila)
        flash(f'"{na_fila["titulo"] or "Reunião"}" saiu da fila. A gravação ficou aguardando decisão.')
    elif chave:  # já saiu da fila: não mexe na ata que está sendo gerada sem o usuário ver qual é
        flash("Essa ata já começou a ser gerada. Se quiser parar, use o Cancelar do aviso \"Gerando ata\".")
    elif TAREFA["ativa"] and TAREFA["cancelavel"]:
        CANCELAR_ATA.set()
        TAREFA.update(etapa="Cancelando")
    elif TAREFA["ativa"] and TAREFA["etapa"] == "Preparando":
        flash("A geração da ata ainda está começando. Tente cancelar de novo em alguns segundos.")
    elif TAREFA["ativa"]:
        flash("A ata já está sendo salva no Google Docs; não dá mais para cancelar.")
    voltar = request.form.get("voltar", "")
    if not voltar.startswith("/") or voltar.startswith("//") or "\\" in voltar or not voltar.isprintable():
        voltar = url_for("gravacao")
    return redirect(voltar)


# ---------------------------------------------------------------- API da janelinha de sugestões

@app.get("/api/aovivo")
def api_aovivo():
    r = GRAVADOR.reuniao or {}
    cli = db.cliente(r.get("cliente_id"))
    estado = {"gravando": GRAVADOR.ativo, "duracao": GRAVADOR.duracao(), "titulo": r.get("titulo", ""),
              "assistente": bool(AO_VIVO), "cliente_definido": bool(r.get("cliente_definido")),
              "cliente": cli["nome"] if cli else None, "cliente_sugerido": r.get("cliente_sugerido"),
              "clientes": [{"id": c["id"], "nome": c["nome"]} for c in db.clientes()],
              "na_chamada": r.get("na_chamada", []),
              "pessoas_por_cliente": {str(k): v for k, v in db.pessoas_por_cliente().items()}}
    if AO_VIVO:
        estado.update(AO_VIVO.estado())
    return estado


@app.post("/api/cliente")
def api_cliente():
    dados = request.get_json(silent=True)
    dados = dados if isinstance(dados, dict) else {}
    cliente_id = dados.get("cliente_id")
    if isinstance(dados.get("novo"), str) and dados["novo"].strip():
        cliente_id = db.criar_cliente(dados["novo"])
    pessoas = dados.get("pessoas")
    if isinstance(pessoas, list):
        # Quem está na chamada: fica na reunião (ata, assistente) e ligado ao cliente para as próximas
        nomes = list(dict.fromkeys(" ".join(str(n).split())[:80] for n in pessoas if str(n).strip()))
        if GRAVADOR.reuniao is not None:
            GRAVADOR.reuniao["na_chamada"] = nomes
        for nome in nomes:
            db.adicionar_pessoa(cliente_id, nome)
        if nomes and db.cliente(cliente_id):
            sincronia.agendar()
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
        # Cliente escolhido agora: o nome dos arquivos acompanha
        novo = _renomear_arquivos(pid, nome_da_gravacao(p["reuniao"], _inicio_da_gravacao(pid, p)))
        if novo != pid:
            p["arquivos"] = {n: novo + Path(c).name[len(pid):] for n, c in p["arquivos"].items()}
            pid = novo
        arquivos = _audios_da_pendente(p)
        if not arquivos and not p.get("transcricao_ao_vivo"):
            flash("O áudio desta reunião está em outro PC. Gere a ata por lá.")
            return redirect(url_for("pendente", pid=pid))
        _resolver_pendente(pid, p, "ata")
        item = entrar_na_fila(p["reuniao"])
        if arquivos:
            threading.Thread(target=processar_gravacao, args=(p["reuniao"], arquivos, False, item), daemon=True).start()
        else:
            threading.Thread(target=processar_texto, args=(p["reuniao"], p["transcricao_ao_vivo"], p["arquivos"], item),
                             daemon=True).start()
    return redirect(url_for("gravacao"))


@app.post("/pendente/<pid>/descartar")
def pendente_descartar(pid):
    p = carregar_pendente(pid)
    if p:
        excluir_arquivos_da_gravacao(pid)
        _resolver_pendente(pid, p, "descartada")
        flash("Gravação apagada deste PC e do Google Drive.")
    return redirect(url_for("inicio"))


@app.route("/gravacao")
def gravacao():
    if not GRAVADOR.ativo and not TAREFA["ativa"] and not TAREFA["erro"] and not TAREFA["cancelada"] and TAREFA["event_id"]:
        if atas.carregar(TAREFA["event_id"]):
            return redirect(tarefas_da_ata(TAREFA["event_id"]))
    return pagina(
        """{% if gravando %}<meta http-equiv="refresh" content="5">
<h1 style="color:#d93025">● Gravando</h1><p><b>{{ titulo }}</b> · {{ duracao }}</p>
{% if automatica %}<p>Gravação automática: ela para sozinha quando você sair da chamada do Meet.</p>{% endif %}
<p class="mut">Gravando seu microfone e o áudio da chamada. Pode minimizar esta página.
Use <b>fone de ouvido</b> para a transcrição separar melhor quem falou.</p>
{% if som_do_pc %}<p class="mut">O som não estava chegando pela extensão do Chrome: a voz dos outros está sendo
gravada pelo som do PC. Se a extensão voltar, o painel volta a usá-la sozinho.</p>{% endif %}
{% if som_mudo %}<div class="flash" style="border-color:var(--err)"><b>O som da chamada não está chegando.</b>
Há mais de {{ som_mudo_min }} minutos a extensão do Chrome não recebe nenhum som dos outros participantes.
Se eles estão falando, <b>tire o fone e use a caixa de som</b>: assim o microfone capta a conversa e a ata não sai só com o seu lado.</div>{% endif %}
<form method="post" action="{{ url_for('parar') }}" data-espera="Parando…"><button>■ Parar e gerar ata</button></form>
{% elif tarefa.ativa %}<meta http-equiv="refresh" content="3">
<h1>⏳ {{ tarefa.etapa }}…</h1>{% if tarefa.titulo %}<p><b>{{ tarefa.titulo }}</b></p>{% endif %}
{% if tarefa.etapa.startswith('Escrevendo') %}<p>Começou {{ tarefa.ha }}. Costuma levar de 1 a 5 minutos.</p>{% endif %}
{% if tarefa.etapa.startswith('Transcrevendo') %}<p>{{ tarefa.pct }}% concluído</p>
<p class="mut">No primeiro uso o Whisper baixa o modelo de voz (alguns minutos). A transcrição roda no seu PC
e leva aproximadamente de 1/4 a 1/2 da duração da reunião.</p>{% endif %}
{% if tarefa.cancelavel and tarefa.etapa != 'Cancelando' %}<form method="post" action="{{ url_for('cancelar_ata') }}"
onsubmit="return confirm('Cancelar a criação da ata? A gravação fica guardada para você gerar depois.')">
<button class="sec" style="color:var(--err)">Cancelar criação da ata</button></form>{% endif %}
{% for i in tarefa.fila %}<p class="mut">Na fila: {{ i.titulo or 'Reunião' }}</p>{% endfor %}
{% elif tarefa.cancelada %}<h1>Criação da ata cancelada</h1>
<p>A gravação ficou guardada em "Gravação aguardando decisão" no painel: dá para gerar a ata depois ou apagar.</p>
<p><a class="btn" href="{{ url_for('inicio') }}">Voltar ao painel</a></p>
{% elif tarefa.erro %}<h1>Algo deu errado</h1><p>{{ tarefa.erro }}</p>
{% if tarefa.transcricao_salva %}<p class="mut">A transcrição foi salva e a gravação voltou para "Gravação aguardando decisão"
no painel: ao gerar a ata de novo, ela é aproveitada e o áudio não é transcrito outra vez.</p>{% endif %}
{% else %}<h1>Nenhuma gravação em andamento</h1>{% endif %}""",
        gravando=GRAVADOR.ativo,
        automatica=GRAVADOR.automatica,
        titulo=(GRAVADOR.reuniao or {}).get("titulo", ""),
        duracao=GRAVADOR.duracao(),
        tarefa=tarefa_para_tela(),
        som_mudo=_gravando_pela_extensao() and EXTENSAO["mudo_avisado"],
        som_mudo_min=SOM_MUDO_SEG // 60,
        som_do_pc=_gravando_pela_extensao() and GRAVADOR.usando_som_do_pc,
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
        if not reuniao:
            flash("Reunião não encontrada no Google Agenda.")
            return redirect(url_for("inicio"))
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
{% for s in sd %}<p class="ok">Service Desk: <b>{{ s.caso }}</b> {{ s.cliente }} · {{ s.solicitacao }} · {{ s.status }}{% if s.chamado %} · chamado {{ s.chamado }}{% if s.analista %} ({{ s.analista }}){% endif %}{% endif %}</p>{% endfor %}
<p><a class="btn" href="{{ reg.doc_link }}" target="_blank">Abrir no Google Docs</a> <a class="btn sec" href="{{ url_for('ata_editar', event_id=event_id) }}">Editar ata</a> <a class="btn sec" href="{{ url_for('tarefas', ver='todas') }}">Ver tarefas</a>
<a class="btn sec" href="{{ url_for('ata_excluir', event_id=event_id) }}" style="color:var(--err)">Excluir ata</a></p>
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
<p>{{ seletor_remetente()|safe }}</p>
{% if reg.email_enviado_para %}<p class="ok">Já enviada para: {{ reg.email_enviado_para|join(', ') }}</p>{% endif %}
<button>Enviar</button></form>

<h2>Agendar próxima reunião no Meet</h2>
{% if reg.followup %}<p class="ok">Agendada: <a href="{{ reg.followup.link_evento }}" target="_blank">ver evento</a> · <a href="{{ reg.followup.link_meet }}" target="_blank">link do Meet</a></p>{% endif %}
<form class="card" method="post" action="{{ url_for('followup', event_id=event_id) }}" data-espera="Agendando…">
<p><input name="titulo" value="Follow-up: {{ r.titulo or a.titulo }}"></p>
<div class="row"><input type="datetime-local" name="inicio" value="{{ prox.data_hora }}" style="flex:2" required>
<input type="number" name="duracao" value="{{ prox.duracao_min or 30 }}" min="15" step="15" style="flex:1"> min</div>
<p class="mut">Convidados: {{ r.participantes|map(attribute='email')|join(', ') or 'nenhum' }}</p>
<p>{{ seletor_remetente()|safe }}</p>
<button>Criar evento com Meet</button></form>""",
        a=a, r=r, reg=reg, prox=prox, event_id=event_id, seletor_cliente=seletor_cliente,
        sd=service_desk.casos_da_ata(event_id),
    )


def _linhas(campo: str) -> list[str]:
    return [l.strip() for l in request.form.get(campo, "").splitlines() if l.strip()]


def ata_do_formulario(antiga: dict) -> "analisador.Ata":
    f = request.form
    topicos = [analisador.Topico(titulo=t.strip(), discussao=d.strip())
               for t, d in zip(f.getlist("topico_titulo"), f.getlist("topico_texto")) if t.strip() or d.strip()]
    acoes = [analisador.Acao(tarefa=t.strip(), responsavel=r.strip() or "A definir", prazo=p.strip() or "A definir",
                             do_usuario=m == "1")
             for t, r, p, m in zip(f.getlist("acao_tarefa"), f.getlist("acao_resp"), f.getlist("acao_prazo"),
                                   f.getlist("acao_minha")) if t.strip()]
    prox = antiga.get("proxima_reuniao") or {}
    return analisador.Ata(
        titulo=f.get("titulo", "").strip() or antiga["titulo"],
        resumo=f.get("resumo", "").strip(),
        participantes=_linhas("participantes"),
        topicos=topicos,
        decisoes=_linhas("decisoes"),
        acoes=acoes,
        pontos_de_atencao=_linhas("pontos"),
        proxima_reuniao=analisador.ProximaReuniao(
            combinada=f.get("prox_combinada") == "1", data_hora=f.get("prox_data", "").strip(),
            duracao_min=int(prox.get("duracao_min") or 0), pauta=_linhas("prox_pauta")),
    )


@app.route("/ata/<event_id>/editar", methods=["GET", "POST"])
def ata_editar(event_id):
    reg = atas.carregar(event_id)
    if not reg:
        flash("Essa ata já não existe.")
        return redirect(url_for("inicio"))
    if request.method == "POST":
        antiga = analisador.Ata.model_validate(reg["ata"])
        ata = ata_do_formulario(reg["ata"])
        mexido = _doc_mexido(reg)
        if mexido and request.form.get("substituir") != "1":
            # o Google Doc foi alterado fora do painel: não sobrescreve sem confirmar (e não perde o que foi digitado)
            return _tela_editar(event_id, ata.model_dump(), mexido)
        reg["ata"] = ata.model_dump()
        atas.salvar(event_id, reg)  # salva antes do Google: a edição não se perde se o Drive falhar
        saiu, entrou = db.atualizar_acoes_da_ata(reg["reuniao"], antiga.acoes, ata, reg.get("cliente_id"))
        msg = "Ata salva."
        if saiu or entrou:
            msg += f" Tarefas: {entrou} nova(s), {saiu} retirada(s)."
        try:
            data = reg["reuniao"].get("inicio", "")[:10]
            reg["doc_link"] = g.atualizar_google_doc(reg.get("doc_link", ""), f"Ata - {ata.titulo} - {data}",
                                                     atas.para_html(ata, reg["reuniao"]))
            try:  # marca de quando o painel salvou: alteração no Docs depois disso é de alguém, e o painel avisa
                salvo = g.doc_modificado_em(reg["doc_link"])
                reg["doc_salvo_em"] = salvo.isoformat() if salvo else None
            except Exception:
                pass
            atas.salvar(event_id, reg)
            msg += " Google Doc atualizado."
        except Exception as e:
            msg += f" Não consegui atualizar o Google Doc ({e}); tente salvar de novo."
        flash(msg)
        return redirect(url_for("ver_ata", event_id=event_id))
    try:
        mexido = _doc_mexido(reg)
    except Exception:
        mexido = None
    return _tela_editar(event_id, reg["ata"], mexido)


def _doc_mexido(reg: dict) -> "datetime | None":
    """Se o Google Doc da ata foi alterado fora do painel (depois da última vez que o painel o gravou), devolve
    quando; senão None. Sem marca guardada (ata antiga), compara com a hora em que a ata foi gerada."""
    try:
        alterado = g.doc_modificado_em(reg.get("doc_link", ""))
    except Exception:
        return None  # sem Google agora: não dá para saber, não bloqueia
    if not alterado:
        return None
    try:
        base = datetime.fromisoformat(reg.get("doc_salvo_em") or reg.get("gerada_em") or "")
    except ValueError:
        return None
    base = base if base.tzinfo else base.astimezone()
    return alterado if alterado > base + timedelta(minutes=3) else None


def _tela_editar(event_id: str, a: dict, mexido):
    return pagina(
        """<h1>Editar ata</h1>
{% if mexido %}<div class="flash" style="border-color:var(--warn)"><b>Atenção:</b> o Google Doc desta ata foi alterado
direto no Google Docs em {{ mexido.astimezone().strftime('%d/%m às %H:%M') }}, depois da última vez que o painel o gravou.
Salvar daqui <b>substitui o documento inteiro</b> e essas alterações se perdem.
<a href="{{ doc_link }}" target="_blank">Abra o documento</a> e copie para cá o que quiser manter.</div>{% endif %}
<p class="mut">Ao salvar, o Google Doc da ata é refeito com este conteúdo (o link continua o mesmo).
Alterações feitas direto no Google Docs são substituídas. Ações novas viram tarefas; ações apagadas saem das tarefas (as já feitas ficam).</p>
<form method="post" data-espera="Salvando…">
<div class="card"><p><b>Título</b><br><input name="titulo" value="{{ a.titulo }}" required></p>
<p><b>Participantes</b> <span class="mut">(um por linha)</span><br><textarea name="participantes" rows="4">{{ a.participantes|join('\\n') }}</textarea></p>
<p><b>Resumo</b><br><textarea name="resumo" rows="6">{{ a.resumo }}</textarea></p></div>
<div class="card"><h2 style="margin-top:0">Assuntos</h2><p class="mut">Deixe título e texto vazios para apagar um assunto.</p>
{% for t in a.topicos + [{'titulo': '', 'discussao': ''}] * 2 %}
<p><input name="topico_titulo" value="{{ t.titulo }}" placeholder="Título do assunto"><br>
<textarea name="topico_texto" rows="4" placeholder="O que foi discutido">{{ t.discussao }}</textarea></p>{% endfor %}</div>
<div class="card"><h2 style="margin-top:0">Decisões</h2><p class="mut">Uma por linha.</p>
<textarea name="decisoes" rows="6">{{ a.decisoes|join('\\n') }}</textarea></div>
<div class="card"><h2 style="margin-top:0">Ações</h2><p class="mut">Apague o texto da tarefa para tirar a ação.</p>
<table><tr><th>Tarefa</th><th>Responsável</th><th>Prazo</th><th>De quem</th></tr>
{% for x in a.acoes + [{'tarefa': '', 'responsavel': '', 'prazo': '', 'do_usuario': True}] * 3 %}
<tr><td><textarea name="acao_tarefa" rows="2">{{ x.tarefa }}</textarea></td>
<td><input name="acao_resp" value="{{ x.responsavel }}"></td>
<td><input name="acao_prazo" value="{{ x.prazo }}" placeholder="AAAA-MM-DD"></td>
<td><select name="acao_minha"><option value="1"{% if x.do_usuario %} selected{% endif %}>Minha / equipe</option>
<option value="0"{% if not x.do_usuario %} selected{% endif %}>Cliente / terceiros</option></select></td></tr>{% endfor %}</table></div>
<div class="card"><h2 style="margin-top:0">Pontos de atenção</h2><p class="mut">Um por linha.</p>
<textarea name="pontos" rows="5">{{ a.pontos_de_atencao|join('\\n') }}</textarea></div>
<div class="card"><h2 style="margin-top:0">Próxima reunião</h2>
<p><label><input type="checkbox" style="width:auto" name="prox_combinada" value="1"{% if a.proxima_reuniao.combinada %} checked{% endif %}> Foi combinada</label></p>
<p><input type="datetime-local" name="prox_data" value="{{ a.proxima_reuniao.data_hora }}"></p>
<p><b>Pauta</b> <span class="mut">(um item por linha)</span><br><textarea name="prox_pauta" rows="4">{{ a.proxima_reuniao.pauta|join('\\n') }}</textarea></p></div>
{% if mexido %}<p><label><input type="checkbox" style="width:auto" name="substituir" value="1" required>
Entendi: substituir o Google Doc e perder o que foi alterado direto nele</label></p>{% endif %}
<p><button>Salvar e atualizar o Google Doc</button> <a class="btn sec" href="{{ url_for('ver_ata', event_id=event_id) }}">Cancelar</a></p>
</form>""",
        a=a, event_id=event_id, mexido=mexido, doc_link=(atas.carregar(event_id) or {}).get("doc_link", ""),
    )


@app.post("/ata/<event_id>/email")
def email(event_id):
    emails = request.form.getlist("emails")
    emails += [e.strip() for e in request.form.get("extras", "").split(",") if e.strip()]
    if not emails:
        flash("Escolha pelo menos um destinatário.")
    else:
        try:
            remetente = request.form.get("remetente") or envio.remetente_padrao()
            enviar_ata(event_id, emails, remetente)
            flash(f"Ata enviada de {remetente} para {', '.join(emails)}.")
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
        convidados = [p["email"] for p in reg["reuniao"]["participantes"]]
        duracao = int(request.form.get("duracao", 30))
        reg["followup"] = g.agendar_followup(request.form["titulo"], inicio, duracao, convidados, descricao,
                                             enviar_convites=False)
        atas.salvar(event_id, reg)
        if convidados:
            remetente = request.form.get("remetente") or envio.remetente_padrao()
            entry = envio.enviar_convite(remetente, convidados, request.form["titulo"], inicio, duracao,
                                         reg["followup"].get("link_meet"), descricao)
            envio.registrar_convite(reg["followup"]["id"], remetente, entry, request.form["titulo"], inicio)
            flash(f"Reunião agendada e convite enviado de {remetente}.")
        else:
            flash("Reunião agendada (sem convidados).")
    except Exception as e:
        flash(f"Erro ao agendar: {e}")
    return redirect(url_for("ver_ata", event_id=event_id))


# ---------------------------------------------------------------- Exclusões

def excluir_arquivos_da_gravacao(pid: str) -> list[str]:
    """Apaga áudios e transcrição de uma gravação (não o .json, que guarda a decisão para os outros PCs)."""
    nomes = []
    for arq in list(config.GRAVACOES_DIR.iterdir()):  # sem glob: nome com [ ] confundiria o padrão
        if _eh_da_gravacao(arq.name, pid) and arq.suffix in (".wav", ".txt", ".webm"):
            arq.unlink(missing_ok=True)
            nomes.append(f"grav__{arq.name}")
    if nomes:
        sincronia.marcar_excluido(*nomes)
        sincronia.agendar()
    return nomes


def excluir_ata(event_id: str, com_tarefas: bool) -> dict:
    """Apaga a ata daqui, do Drive (cópia de sincronização) e manda o Google Doc para a lixeira."""
    reg = atas.carregar(event_id) or {}
    resultado = {"doc": None, "tarefas": 0}
    doc_id = g.id_do_link(reg.get("doc_link", ""))
    if doc_id:
        try:
            g.drive_lixeira(doc_id)
            resultado["doc"] = "lixeira"
        except Exception as e:
            resultado["doc"] = f"não consegui mandar para a lixeira ({e})"
    atas._arquivo(event_id).unlink(missing_ok=True)
    if com_tarefas:
        resultado["tarefas"] = db.excluir_tarefas_da_ata(event_id)
    sincronia.marcar_excluido(f"ata__{event_id}.json")
    sincronia.agendar()
    return resultado


@app.route("/ata/<event_id>/excluir", methods=["GET", "POST"])
def ata_excluir(event_id):
    reg = atas.carregar(event_id)
    if not reg:
        flash("Essa ata já não existe.")
        return redirect(url_for("inicio"))
    abertas = [t for t in db.tarefas() if t["origem_ata"] == event_id]
    if request.method == "POST":
        res = excluir_ata(event_id, request.form.get("tarefas") == "1")
        partes = ["Ata excluída deste PC e do Google Drive"]
        if res["doc"] == "lixeira":
            partes.append("o Google Doc foi para a lixeira do Drive (dá para recuperar por 30 dias)")
        elif res["doc"]:
            partes.append(f"Google Doc: {res['doc']}")
        if res["tarefas"]:
            partes.append(f"{res['tarefas']} tarefa(s) da reunião excluída(s)")
        flash("; ".join(partes) + ".")
        return redirect(url_for("inicio"))
    return pagina(
        """<h1>Excluir a ata?</h1>
<div class="card"><b>{{ reg.ata.titulo }}</b> <span class="mut">· {{ reg.reuniao.inicio[:16].replace('T',' ') }}</span>
<p class="mut" style="margin:8px 0 0">{{ (reg.ata.resumo or '')[:300] }}</p></div>
<p>Vai ser apagado:</p>
<ul><li>a ata deste PC e a cópia na pasta do Google Drive (e dos outros PCs, na próxima sincronização);</li>
{% if reg.doc_link and reg.doc_link.startswith('http') %}<li>o <b>Google Doc</b> da ata, que vai para a <b>lixeira do Drive</b> (dá para recuperar por 30 dias).</li>{% endif %}</ul>
<form method="post">
{% if abertas %}<label style="display:flex;gap:8px;align-items:flex-start;margin:12px 0">
<input type="checkbox" name="tarefas" value="1" checked style="width:auto;margin-top:4px">
<span>Excluir também as <b>{{ abertas|length }} tarefa(s) em aberto</b> que vieram desta reunião:
<span class="mut">{{ abertas|map(attribute='descricao')|join('; ') }}</span></span></label>{% endif %}
<div class="row" style="justify-content:flex-start">
<button style="background:var(--err)">Excluir</button>
<a class="btn sec" href="{{ url_for('inicio') }}">Cancelar</a></div></form>""",
        reg=reg, abertas=abertas,
    )


@app.post("/evento/<event_id>/ocultar")
def evento_ocultar(event_id):
    """Tira uma reunião sem ata da lista de recentes (o evento continua no Google Agenda)."""
    sincronia.marcar_excluido(f"evento__{event_id}")
    sincronia.agendar()
    flash("Reunião removida da lista. O evento continua no seu Google Agenda.")
    return redirect(request.referrer or url_for("inicio"))


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


@app.post("/resumo/<event_id>/cliente")
def resumo_cliente(event_id):
    """Fixa o cliente da reunião (vale na agenda, nas sugestões de convidados e na ata gerada depois)."""
    cli = db.cliente(request.form.get("cliente", type=int))
    if not g.obter_reuniao(event_id):
        flash("Reunião não encontrada na agenda.")
        return redirect(url_for("inicio"))
    try:
        g.definir_cliente_do_evento(event_id, cli["uuid"] if cli else None)
        clientes._historico["quando"] = 0  # as sugestões de convidados releem a agenda
        flash(f"Cliente da reunião: {cli['nome']}." if cli else "Reunião sem cliente fixado; o painel volta a sugerir.")
    except Exception as e:
        flash(f"Não consegui salvar o cliente: {e}")
    return redirect(url_for("resumo", event_id=event_id))


@app.post("/resumo/<event_id>/consultor")
def resumo_consultor(event_id):
    """Anota quem é o consultor da reunião (aparece na agenda); vazio apaga."""
    consultor = " ".join(request.form.get("consultor", "").split())[:80]
    if not g.obter_reuniao(event_id):
        flash("Reunião não encontrada na agenda.")
        return redirect(url_for("inicio"))
    try:
        g.definir_consultor_do_evento(event_id, consultor)
        clientes._historico["quando"] = 0  # a lista de consultores sugeridos relê a agenda
        flash(f"Consultor da reunião: {consultor}." if consultor else "Consultor apagado desta reunião.")
    except Exception as e:
        flash(f"Não consegui salvar o consultor: {e}")
    return redirect(url_for("resumo", event_id=event_id))


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
<div style="display:flex;gap:8px">{% if r.organizador and not r.ja_terminou %}<a class="btn sec" href="{{ url_for('remarcar_reuniao', event_id=r.id, voltar=request.full_path) }}">Remarcar</a>
<form class="inline" method="post" action="{{ url_for('cancelar_reuniao', event_id=r.id, voltar=request.full_path) }}" onsubmit='return confirm({{ ("Cancelar a reunião “" ~ r.titulo ~ "”? Ela sai da agenda e os convidados recebem o cancelamento.")|tojson }})'><button class="sec" title="Cancelar para todos">Cancelar</button></form>{% endif %}
<a class="btn" href="{{ r.link }}" target="_blank">Entrar no Meet</a></div></div>

<form method="post" action="{{ url_for('resumo_cliente', event_id=r.id) }}" class="row" style="justify-content:flex-start;margin:6px 0 4px">
<span class="mut">Cliente:</span><select name="cliente" onchange="this.form.submit()" style="width:auto">
<option value="">— nenhum —</option>{% for c in clientes %}<option value="{{ c.id }}" {{ 'selected' if cli and c.id == cli.id }}>{{ c.nome }}</option>{% endfor %}</select>
{% if not cli %}<span class="mut">Escolha o cliente para ver as pendências e a última reunião.</span>
{% elif not r.cliente_uuid %}<span class="mut">sugerido pelo painel; escolha na lista para fixar</span>{% endif %}</form>
<form method="post" action="{{ url_for('resumo_consultor', event_id=r.id) }}" class="row" style="justify-content:flex-start;margin:0 0 10px">
<span class="mut">Consultor:</span><input name="consultor" value="{{ r.consultor }}" list="consultores" maxlength="80"
placeholder="quem vai conduzir" style="width:220px" autocomplete="off">
<datalist id="consultores">{% for n in consultores %}<option value="{{ n }}">{% endfor %}</datalist>
<button class="sec">Salvar</button></form>

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

{% for s in sd %}<div class="card rs"><h3>Service Desk · {{ s.caso }}</h3>
<p><b>{{ s.solicitacao }}</b> <span class="qd">· {{ s.status }}{% if s.chamado %} · chamado {{ s.chamado }}{% if s.analista %} ({{ s.analista }}){% endif %}{% endif %}</span></p>
{% if s.horas %}<p class="{{ 'atr' if s.no_limite else 'qd' }}">{{ s.horas }}h orçadas{% if s.usadas %} · {{ s.usadas }}h já usadas{% if s.consumo %} ({{ s.consumo }}%){% endif %}{% endif %}{% if s.no_limite %} · perto do limite: avalie aditivo{% endif %}</p>{% endif %}
{% if s.solucao %}<p class="mut" style="margin-bottom:4px">Proposta enviada:</p><p style="white-space:pre-wrap">{{ s.solucao }}</p>{% endif %}
</div>{% endfor %}
</div>""",
        r=r, cli=cli, res=res, anterior=anterior, inicio=inicio, faltam=faltam, pauta=pauta, clientes=db.clientes(), consultores=clientes.consultores_conhecidos(),
        sd=[s for s in service_desk.casos_do_cliente(cli["nome"])["casos"] if s["aberto"]] if cli else [],
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
    c = db.cliente(valor)  # valida: número normal e cliente existente (senão, sem cliente)
    return c["id"] if c else None


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
    nome = request.form.get("nome", "").strip()
    if not nome:
        flash("Digite o nome do cliente.")
        return redirect(request.referrer or url_for("tarefas"))
    antes = {c["id"]: c for c in db.clientes()}
    cid = db.criar_cliente(nome, request.form.get("emails", ""))
    if cid in antes:
        novos = (db.cliente(cid) or {}).get("emails") != antes[cid].get("emails")
        flash(f"O cliente {antes[cid]['nome']} já existia" + ("; os e-mails novos foram acrescentados." if novos else "."))
    elif cid and (db.tarefas(cliente_id=cid, incluir_feitas=True) or db.notas(cid)):
        flash(f"O cliente {db.cliente(cid)['nome']} tinha sido excluído e voltou, com as tarefas e anotações dele.")
    else:
        flash(f"Cliente {nome} cadastrado. Ele já aparece para escolher quando você abrir o Meet.")
    if request.form.get("depois") == "nova_reuniao":  # cadastrado pela tela de nova reunião: volta com ele escolhido
        return redirect(url_for("nova_reuniao", cliente=cid))
    if request.form.get("abrir"):
        return redirect(url_for("ficha_cliente", cliente_id=cid))
    return redirect(request.referrer or url_for("tarefas"))


@app.post("/clientes/<int:cliente_id>/editar")
def cliente_editar(cliente_id):
    if request.form.get("nome", "").strip():
        try:
            db.atualizar_cliente(cliente_id, request.form["nome"], request.form.get("emails", ""))
            flash("Cliente atualizado.")
        except db.ClienteDuplicado as e:
            flash(str(e))
    return redirect(url_for("ficha_cliente", cliente_id=cliente_id))


@app.post("/clientes/<int:cliente_id>/excluir")
def cliente_excluir(cliente_id):
    c = db.cliente(cliente_id)
    if c and db.excluir_cliente(cliente_id):
        sincronia.agendar()
        flash(f"Cliente {c['nome']} excluído, com as tarefas e anotações dele. Para trazer tudo de volta, "
              f"cadastre de novo um cliente chamado {c['nome']}.")
    return redirect(url_for("lista_clientes"))


@app.post("/clientes/<int:cliente_id>")
def cliente_renomear(cliente_id):
    if request.form.get("nome", "").strip():
        try:
            db.renomear_cliente(cliente_id, request.form["nome"])
        except db.ClienteDuplicado as e:
            flash(str(e))
    return redirect(request.referrer or url_for("tarefas"))


# Telas de clientes, cobrança, perguntas, nova reunião e semana (extras.py)
extras.registrar(app, pagina, seletor_cliente, sugerir_cliente, cliente_do_formulario, _breve)


def _trava_do_painel() -> bool:
    """Trava do Windows (mutex com nome) que só um painel segura: a porta só fica ocupada depois de um ou dois
    minutos carregando, e dois painéis abertos em sequência ficavam os dois na mesma porta (pedidos travavam)."""
    if os.name != "nt":
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _trava_do_painel.alca = kernel32.CreateMutexW(None, False, f"Local\\ReunioesPainel{config.PORTA}")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS: outro painel já está aberto (ou subindo)
        kernel32.CloseHandle(ctypes.c_void_p(_trava_do_painel.alca))
        return False
    return True


if __name__ == "__main__":
    comando = sys.argv[1] if len(sys.argv) > 1 else "web"
    if comando == "login":
        print(f"Conectado como: {g.autenticar()}")
    elif comando == "auto":
        modo_auto()
    else:
        url = f"http://localhost:{config.PORTA}"
        reiniciado = "--reiniciado" in sys.argv
        for _ in range(40 if reiniciado else 1):  # reiniciado: o painel antigo ainda está saindo
            with socket.socket() as s:
                ja_rodando = s.connect_ex(("127.0.0.1", config.PORTA)) == 0
            ja_rodando = ja_rodando or not _trava_do_painel()
            if not ja_rodando:
                break
            time.sleep(0.5)
        if ja_rodando:
            webbrowser.open(url)
            sys.exit(0)
        if sys.stdout is None:  # iniciado sem janela (pythonw, inicialização do Windows)
            log = open(config.BASE_DIR / "reunioes.log", "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = log
        if config.GRAVACAO_AUTOMATICA:
            monitor.iniciar(ao_detectar_meet, ao_encerrar_meet,
                            lambda: GRAVADOR.ativo and GRAVADOR.automatica and not GRAVADOR.pela_extensao,
                            extensao_ativa)
            vigiar_extensao()
            print("[monitor] vigiando chamadas do Meet")
        vigiar_janela()
        copias_de_seguranca()
        sincronia.iniciar_automatico()
        envio.contas()  # já busca as contas do Outlook em segundo plano
        # deixa pronto o status do sistema (verifica Google e Claude em segundo plano)
        threading.Thread(target=lambda: clientes.status_sistema(sincronia.ESTADO, config.GRAVACAO_AUTOMATICA),
                         daemon=True).start()
        if config.LEMBRETES:
            lembretes.iniciar(sugerir_cliente)
        # Deixa os modelos de voz prontos antes da primeira reunião
        threading.Thread(target=lambda: [gravador.carregar_modelo(config.WHISPER_MODELO_AO_VIVO),
                                         gravador.carregar_modelo(config.WHISPER_MODELO_AO_VIVO_OUTROS),
                                         gravador.carregar_modelo()], daemon=True).start()
        if not reiniciado:  # reiniciado: a aba que pediu já recarrega sozinha
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        app.run(port=config.PORTA, debug=False)
