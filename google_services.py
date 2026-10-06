"""Integração com Google: Agenda, Meet (transcrições), Drive (Docs) e Gmail."""

import base64
import uuid
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

import config

_services = {}


def _credentials() -> Credentials:
    creds = None
    if config.TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(config.TOKEN_FILE), config.GOOGLE_SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not config.CREDENTIALS_FILE.exists():
            raise RuntimeError(
                "Arquivo credentials.json não encontrado. Veja o passo 'Google Cloud' no README.md."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(config.CREDENTIALS_FILE), config.GOOGLE_SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
    config.TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _svc(name: str, version: str):
    if name not in _services:
        creds = _credentials()
        try:
            _services[name] = build(name, version, credentials=creds, cache_discovery=False)
        except Exception:
            # Bibliotecas antigas podem não ter o documento de descoberta embutido (ex.: Meet)
            _services[name] = build(name, version, credentials=creds, static_discovery=False)
    return _services[name]


def autenticar() -> str:
    """Força o login e devolve o e-mail da conta conectada."""
    _credentials()
    # events.list é coberto pelo escopo calendar.events; o "summary" da agenda principal é o e-mail
    info = _svc("calendar", "v3").events().list(calendarId="primary", maxResults=1).execute()
    return info.get("summary", "")


# ---------------------------------------------------------------- Agenda

def _eventos(inicio: datetime, fim: datetime) -> list[dict]:
    """Todos os eventos da agenda principal no período (com e sem Meet)."""
    agora = datetime.now(timezone.utc)
    itens, token = [], None
    while True:
        resp = (
            _svc("calendar", "v3")
            .events()
            .list(calendarId="primary", timeMin=inicio.isoformat(), timeMax=fim.isoformat(),
                  singleEvents=True, orderBy="startTime", maxResults=250, pageToken=token)
            .execute()
        )
        itens += resp.get("items", [])
        token = resp.get("nextPageToken")
        if not token:
            break
    eventos = []
    for ev in itens:
        if ev.get("status") == "cancelled":
            continue
        eu = next((a for a in ev.get("attendees", []) if a.get("self")), {})
        if eu.get("responseStatus") == "declined":
            continue
        inicio_ev = ev["start"].get("dateTime") or ev["start"].get("date")
        fim_ev = ev["end"].get("dateTime") or ev["end"].get("date")
        link = ev.get("hangoutLink")
        eventos.append(
            {
                "id": ev["id"],
                "titulo": ev.get("summary", "(sem título)"),
                "descricao": ev.get("description", ""),
                "local": ev.get("location", ""),
                "inicio": inicio_ev,
                "fim": fim_ev,
                "dia_inteiro": "T" not in inicio_ev,
                "link": link,
                "link_agenda": ev.get("htmlLink"),
                "codigo_meet": ((ev.get("conferenceData") or {}).get("conferenceId")
                                or (link.rstrip("/").split("/")[-1] if link else None)),
                "participantes": [
                    {"email": a["email"], "nome": a.get("displayName", ""), "resposta": a.get("responseStatus", "")}
                    for a in ev.get("attendees", [])
                    if not a.get("resource")
                ],
                "anexos": ev.get("attachments", []),
                # só quem organizou pode mudar data/hora para todos
                "organizador": bool((ev.get("organizer") or {}).get("self")),
                "ja_terminou": datetime.fromisoformat(fim_ev.replace("Z", "+00:00")).astimezone(timezone.utc) < agora
                if "T" in fim_ev
                else False,
            }
        )
    return eventos


def listar_eventos(inicio: datetime, fim: datetime) -> list[dict]:
    """Agenda completa do período, para a tela de agenda."""
    return _eventos(inicio, fim)


def listar_reunioes(dias_atras: int = 7, dias_frente: int = 7) -> list[dict]:
    """Eventos da agenda principal que têm link do Google Meet."""
    agora = datetime.now(timezone.utc)
    return [e for e in _eventos(agora - timedelta(days=dias_atras), agora + timedelta(days=dias_frente)) if e["link"]]


def obter_reuniao(event_id: str) -> dict | None:
    for r in listar_reunioes(dias_atras=60, dias_frente=60):
        if r["id"] == event_id:
            return r
    return None


def agendar_followup(titulo: str, inicio: datetime, duracao_min: int, emails: list[str], descricao: str,
                     enviar_convites: bool = True) -> dict:
    """Cria um evento com link do Meet. Com enviar_convites=False o Google não manda e-mail
    (o convite sai pelo Outlook — ver envio.py)."""
    body = {
        "summary": titulo,
        "description": descricao,
        "start": {"dateTime": inicio.isoformat(), "timeZone": config.TIMEZONE},
        "end": {"dateTime": (inicio + timedelta(minutes=duracao_min)).isoformat(), "timeZone": config.TIMEZONE},
        "attendees": [{"email": e} for e in emails],
        "conferenceData": {
            "createRequest": {"requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"}}
        },
    }
    ev = (
        _svc("calendar", "v3")
        .events()
        .insert(calendarId="primary", body=body, conferenceDataVersion=1,
                sendUpdates="all" if enviar_convites else "none")
        .execute()
    )
    return {"id": ev["id"], "link_evento": ev.get("htmlLink"), "link_meet": ev.get("hangoutLink")}


def obter_evento(event_id: str) -> dict | None:
    """Um evento da agenda principal pelo id (qualquer data)."""
    try:
        ev = _svc("calendar", "v3").events().get(calendarId="primary", eventId=event_id).execute()
    except HttpError:
        return None
    inicio = ev["start"].get("dateTime") or ev["start"].get("date")
    fim = ev["end"].get("dateTime") or ev["end"].get("date")
    return {"id": ev["id"], "titulo": ev.get("summary", "(sem título)"), "inicio": inicio, "fim": fim,
            "dia_inteiro": "T" not in inicio, "link": ev.get("hangoutLink"), "link_agenda": ev.get("htmlLink"),
            "organizador": bool((ev.get("organizer") or {}).get("self")),
            "participantes": [a["email"] for a in ev.get("attendees", []) if not a.get("resource") and not a.get("self")]}


def remarcar_evento(event_id: str, inicio: datetime, duracao_min: int, titulo: str | None = None,
                    avisar: bool = True) -> dict:
    """Muda data/hora (e o título, se informado) e avisa os convidados."""
    corpo = {
        "start": {"dateTime": inicio.isoformat(), "timeZone": config.TIMEZONE},
        "end": {"dateTime": (inicio + timedelta(minutes=duracao_min)).isoformat(), "timeZone": config.TIMEZONE},
    }
    if titulo:
        corpo["summary"] = titulo
    ev = (_svc("calendar", "v3").events()
          .patch(calendarId="primary", eventId=event_id, body=corpo, sendUpdates="all" if avisar else "none").execute())
    return {"link_evento": ev.get("htmlLink"), "link_meet": ev.get("hangoutLink")}


# ---------------------------------------------------------------- Transcrição

def _nome_participante(meet, nome_recurso: str, cache: dict) -> str:
    if nome_recurso not in cache:
        try:
            p = meet.conferenceRecords().participants().get(name=nome_recurso).execute()
            pessoa = p.get("signedinUser") or p.get("anonymousUser") or p.get("phoneUser") or {}
            cache[nome_recurso] = pessoa.get("displayName", "Participante")
        except HttpError:
            cache[nome_recurso] = "Participante"
    return cache[nome_recurso]


def _transcricao_via_meet_api(reuniao: dict) -> str | None:
    """Busca a transcrição pela API do Meet (com nome de quem falou e horário)."""
    meet = _svc("meet", "v2")
    inicio = datetime.fromisoformat(reuniao["inicio"].replace("Z", "+00:00"))
    filtro = (
        f'space.meeting_code = "{reuniao["codigo_meet"]}" '
        f'AND start_time >= "{(inicio - timedelta(hours=3)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}" '
        f'AND start_time <= "{(inicio + timedelta(hours=6)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}"'
    )
    registros = meet.conferenceRecords().list(filter=filtro).execute().get("conferenceRecords", [])
    linhas = []
    nomes = {}
    for reg in sorted(registros, key=lambda r: r.get("startTime", "")):
        transcricoes = meet.conferenceRecords().transcripts().list(parent=reg["name"]).execute()
        for tr in transcricoes.get("transcripts", []):
            token = None
            while True:
                resp = (
                    meet.conferenceRecords()
                    .transcripts()
                    .entries()
                    .list(parent=tr["name"], pageSize=100, pageToken=token)
                    .execute()
                )
                for e in resp.get("transcriptEntries", []):
                    hora = datetime.fromisoformat(e["startTime"].replace("Z", "+00:00")).astimezone().strftime("%H:%M")
                    quem = _nome_participante(meet, e.get("participant", ""), nomes)
                    linhas.append(f"[{hora}] {quem}: {e.get('text', '')}")
                token = resp.get("nextPageToken")
                if not token:
                    break
    return "\n".join(linhas) or None


def _exportar_doc(file_id: str) -> str:
    dados = _svc("drive", "v3").files().export(fileId=file_id, mimeType="text/plain").execute()
    return dados.decode("utf-8-sig") if isinstance(dados, bytes) else str(dados)


def _transcricao_via_anexos(reuniao: dict) -> str | None:
    """Plano B: o Meet anexa a transcrição (Google Doc) ao evento da agenda."""
    chaves = ("transcri", "transcript", "anotações", "notes by gemini", "anotações do gemini")
    textos = []
    for anexo in reuniao.get("anexos", []):
        if anexo.get("mimeType") != "application/vnd.google-apps.document":
            continue
        if any(k in anexo.get("title", "").lower() for k in chaves):
            textos.append(f"=== {anexo['title']} ===\n{_exportar_doc(anexo['fileId'])}")
    return "\n\n".join(textos) or None


def buscar_transcricao(reuniao: dict) -> tuple[str | None, str]:
    """Devolve (texto, origem). Tenta a API do Meet e depois os anexos do evento."""
    erros = []
    try:
        texto = _transcricao_via_meet_api(reuniao)
        if texto:
            return texto, "API do Google Meet"
    except HttpError as e:
        erros.append(f"Meet API: {e.status_code} {e.reason}")
    try:
        texto = _transcricao_via_anexos(reuniao)
        if texto:
            return texto, "Google Doc anexado ao evento"
    except HttpError as e:
        erros.append(f"Drive: {e.status_code} {e.reason}")
    return None, "; ".join(erros) or "Nenhuma transcrição encontrada (a transcrição foi ativada na reunião?)"


# ---------------------------------------------------------------- Drive / Gmail

def criar_google_doc(titulo: str, html: str) -> str:
    """Cria um Google Doc a partir de HTML e devolve o link."""
    media = MediaInMemoryUpload(html.encode("utf-8"), mimetype="text/html", resumable=False)
    arquivo = (
        _svc("drive", "v3")
        .files()
        .create(
            body={"name": titulo, "mimeType": "application/vnd.google-apps.document"},
            media_body=media,
            fields="id,webViewLink",
        )
        .execute()
    )
    return arquivo["webViewLink"]


PASTA_MIME = "application/vnd.google-apps.folder"


def drive_pasta(nome: str) -> str:
    """Id da pasta do app na raiz do Drive (cria se não existir; havendo duplicadas, usa a mais antiga)."""
    drive = _svc("drive", "v3")
    nome_q = nome.replace("\\", "\\\\").replace("'", "\\'")
    achadas = drive.files().list(
        q=f"name = '{nome_q}' and mimeType = '{PASTA_MIME}' and trashed = false and 'root' in parents",
        fields="files(id, createdTime)", orderBy="createdTime", pageSize=10,
    ).execute().get("files", [])
    if achadas:
        return achadas[0]["id"]
    return drive.files().create(body={"name": nome, "mimeType": PASTA_MIME}, fields="id").execute()["id"]


def drive_listar(pasta_id: str) -> dict[str, dict]:
    """Arquivos da pasta: nome -> {id, modifiedTime, appProperties}."""
    drive, arquivos, token = _svc("drive", "v3"), {}, None
    while True:
        resp = drive.files().list(
            q=f"'{pasta_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, modifiedTime, appProperties)",
            pageSize=1000, pageToken=token,
        ).execute()
        for f in resp.get("files", []):
            arquivos.setdefault(f["name"], f)
        token = resp.get("nextPageToken")
        if not token:
            return arquivos


def drive_enviar(pasta_id: str, nome: str, conteudo: bytes | None = None, caminho=None,
                 mimetype: str = "application/json", file_id: str | None = None, props: dict | None = None) -> str:
    """Cria ou atualiza um arquivo na pasta. Use `conteudo` (bytes) ou `caminho` (arquivo grande)."""
    from googleapiclient.http import MediaFileUpload

    media = (MediaFileUpload(str(caminho), mimetype=mimetype, resumable=True) if caminho
             else MediaInMemoryUpload(conteudo, mimetype=mimetype, resumable=False))
    drive = _svc("drive", "v3")
    if file_id:
        return drive.files().update(fileId=file_id, body={"appProperties": props or {}},
                                    media_body=media, fields="id").execute()["id"]
    corpo = {"name": nome, "parents": [pasta_id], "appProperties": props or {}}
    return drive.files().create(body=corpo, media_body=media, fields="id").execute()["id"]


def drive_baixar(file_id: str, destino=None) -> bytes | None:
    """Baixa um arquivo; com `destino`, grava direto no disco (para arquivos grandes)."""
    from googleapiclient.http import MediaIoBaseDownload

    pedido = _svc("drive", "v3").files().get_media(fileId=file_id)
    if destino is None:
        return pedido.execute()
    from pathlib import Path

    destino = Path(destino)
    temporario = destino.with_name(destino.name + ".baixando")
    with open(temporario, "wb") as f:
        baixador = MediaIoBaseDownload(f, pedido, chunksize=8 * 1024 * 1024)
        terminou = False
        while not terminou:
            _, terminou = baixador.next_chunk()
    temporario.replace(destino)  # só aparece com o nome final quando chegou inteiro
    return None


def drive_apagar(file_id: str) -> None:
    """Apaga de vez um arquivo de dados do app (cópia de sincronização)."""
    _svc("drive", "v3").files().delete(fileId=file_id).execute()


def drive_lixeira(file_id: str) -> None:
    """Manda para a lixeira do Drive (dá para recuperar por 30 dias)."""
    _svc("drive", "v3").files().update(fileId=file_id, body={"trashed": True}).execute()


def id_do_link(link: str) -> str | None:
    """Extrai o id de um link do Google Docs/Drive."""
    import re
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", link or "")
    return m.group(1) if m else None


def enviar_email(destinatarios: list[str], assunto: str, html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["To"] = ", ".join(destinatarios)
    msg["Subject"] = assunto
    msg.attach(MIMEText(html, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    _svc("gmail", "v1").users().messages().send(userId="me", body={"raw": raw}).execute()
