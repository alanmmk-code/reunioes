"""Envio de e-mails e convites de reunião pelo Outlook deste PC, pela conta que você escolher.

A conta Google do painel (alanmmk@hotmail.com) não tem Gmail, e convites enviados pelo Google em nome
de um endereço Hotmail costumam cair no spam das empresas. O Outlook já tem as suas contas configuradas
(Hotmail, corporativas, IMAP) e envia pela conta escolhida, como se fosse você.

Proteção: fora do banco real (testes, Agenor) nada é enviado.
"""

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import config
import db

OL_MAIL, OL_APPOINTMENT = 0, 1
OL_MEETING = 1  # MeetingStatus: reunião com convidados
OL_FOLDER_CALENDAR = 9
_cache = {"contas": None, "quando": 0.0}
_lock = threading.Lock()


class EnvioBloqueado(RuntimeError):
    pass


def _liberado() -> None:
    """Só envia de verdade quando o painel está usando o banco real (nunca em teste)."""
    real = (config.DADOS_DIR / "dados.db").resolve()
    if Path(db.ARQUIVO).resolve() != real:
        raise EnvioBloqueado("Envio bloqueado: o sistema está rodando com dados de teste.")


_executor = {"pool": None}
TEMPO_LIMITE = 45  # segundos


def _no_outlook(funcao, *args):
    """Roda `funcao(outlook, *args)` numa thread só do Outlook, em modo MTA (sem depender de quem chama
    — chamadas STA de threads do servidor web travavam o Outlook) e com tempo limite."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    def rodar():
        import pythoncom
        import win32com.client

        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        try:
            return funcao(win32com.client.Dispatch("Outlook.Application"), *args)
        finally:
            pythoncom.CoUninitialize()

    with _lock:
        if _executor["pool"] is None:
            _executor["pool"] = ThreadPoolExecutor(max_workers=1, thread_name_prefix="outlook")
        pool = _executor["pool"]
    tarefa = pool.submit(rodar)
    try:
        return tarefa.result(timeout=TEMPO_LIMITE)
    except TimeoutError:
        with _lock:  # abandona a thread travada; a próxima chamada usa uma nova
            if _executor["pool"] is pool:
                _executor["pool"] = None
        raise RuntimeError(f"O Outlook não respondeu em {TEMPO_LIMITE} s. Abra o Outlook neste PC e tente de novo.")


def _ler_contas(ol) -> list[str]:
    ns = ol.GetNamespace("MAPI")
    return [ns.Accounts.Item(i).SmtpAddress for i in range(1, ns.Accounts.Count + 1)]


def atualizar_contas() -> list[str]:
    """Lê as contas do Outlook agora (pode demorar se o Outlook estiver abrindo)."""
    try:
        lista = _no_outlook(_ler_contas)
    except Exception:
        lista = _cache["contas"] or []
    _cache.update(contas=lista, quando=time.time())
    return list(lista)


def contas() -> list[str]:
    """Contas do Outlook, sem fazer a tela esperar: devolve o que já está guardado e atualiza em segundo plano."""
    if _cache["contas"] is None or time.time() - _cache["quando"] > 300:
        _cache["quando"] = time.time()  # evita disparar várias atualizações seguidas
        threading.Thread(target=atualizar_contas, daemon=True, name="contas-outlook").start()
    return list(_cache["contas"] or [])


def remetente_padrao() -> str:
    lista = contas()
    if config.EMAIL_REMETENTE and (not lista or config.EMAIL_REMETENTE.lower() in [c.lower() for c in lista]):
        return config.EMAIL_REMETENTE
    return lista[0] if lista else ""


def _conta(ol, remetente: str):
    ns = ol.GetNamespace("MAPI")
    for i in range(1, ns.Accounts.Count + 1):
        conta = ns.Accounts.Item(i)
        if conta.SmtpAddress.lower() == (remetente or "").lower():
            return conta
    raise ValueError(f"A conta {remetente} não está configurada no Outlook deste PC.")


OL_FOLDER_DRAFTS = 16


def _usar_conta(item, conta) -> None:
    """Define a conta de envio. No pywin32, `item.SendUsingAccount = conta` é ignorado em silêncio
    (o item sai pela conta padrão do Outlook); é preciso atribuir por referência (DISPATCH_PROPERTYPUTREF)."""
    item._oleobj_.Invoke(64209, 0, 8, 0, conta)  # 64209 = SendUsingAccount, 8 = PROPERTYPUTREF


def _novo_email(ol, conta):
    """E-mail criado nos Rascunhos da própria conta (assim a cópia vai para os Enviados dela)."""
    try:
        return conta.DeliveryStore.GetDefaultFolder(OL_FOLDER_DRAFTS).Items.Add(OL_MAIL)
    except Exception:
        return ol.CreateItem(OL_MAIL)


def _destinos(lista) -> list[str]:
    if isinstance(lista, str):
        lista = lista.replace(";", ",").split(",")
    return [e.strip() for e in lista if e and "@" in e]


def enviar_email(remetente: str, destinatarios, assunto: str, html: str) -> list[str]:
    """Envia um e-mail pelo Outlook, pela conta `remetente`. Devolve os destinatários."""
    _liberado()
    para = _destinos(destinatarios)
    if not para:
        raise ValueError("Informe pelo menos um e-mail válido.")
    remetente = remetente or remetente_padrao()

    def enviar(ol):
        conta = _conta(ol, remetente)
        msg = _novo_email(ol, conta)
        msg.To = "; ".join(para)
        msg.Subject = assunto
        msg.HTMLBody = html
        _usar_conta(msg, conta)
        if msg.SendUsingAccount.SmtpAddress.lower() != remetente.lower():
            raise RuntimeError(f"O Outlook não aceitou enviar pela conta {remetente}.")
        msg.Send()

    _no_outlook(enviar)
    _vigiar("e-mail", remetente, assunto, para)
    return para


def _calendario_da_conta(ol, conta):
    try:
        return conta.DeliveryStore.GetDefaultFolder(OL_FOLDER_CALENDAR)
    except Exception:
        return ol.GetNamespace("MAPI").GetDefaultFolder(OL_FOLDER_CALENDAR)


def enviar_convite(remetente: str, convidados, titulo: str, inicio: datetime, duracao_min: int,
                   link_meet: str, pauta: str = "") -> str:
    """Envia um convite de reunião (aceitar/recusar) pelo Outlook, com o link do Meet.
    Devolve o id do item no Outlook, para remarcar depois."""
    _liberado()
    para = _destinos(convidados)
    if not para:
        raise ValueError("A reunião não tem convidados.")
    remetente = remetente or remetente_padrao()

    def criar(ol):
        conta = _conta(ol, remetente)
        item = _calendario_da_conta(ol, conta).Items.Add(OL_APPOINTMENT)
        item.MeetingStatus = OL_MEETING
        item.Subject = titulo
        item.Start = inicio.replace(tzinfo=None, second=0, microsecond=0)
        item.Duration = int(duracao_min)
        item.Location = link_meet or ""
        item.Body = (f"Entrar no Google Meet: {link_meet}\n\n" if link_meet else "") + (pauta or "")
        for email in para:
            item.Recipients.Add(email)
        item.Recipients.ResolveAll()
        _usar_conta(item, conta)
        if item.SendUsingAccount.SmtpAddress.lower() != remetente.lower():
            raise RuntimeError(f"O Outlook não aceitou enviar o convite pela conta {remetente}.")
        item.Save()
        item.Send()
        return item.EntryID

    entry_id = _no_outlook(criar)
    _vigiar("convite", remetente, titulo, para)
    return entry_id


def atualizar_convite(entry_id: str, inicio: datetime, duracao_min: int, titulo: str | None = None) -> None:
    """Remarca um convite enviado pelo Outlook e manda a atualização aos convidados."""
    _liberado()

    def atualizar(ol):
        item = ol.GetNamespace("MAPI").GetItemFromID(entry_id)
        item.Start = inicio.replace(tzinfo=None, second=0, microsecond=0)
        item.Duration = int(duracao_min)
        if titulo:
            item.Subject = titulo
        item.Save()
        item.Send()
        conta = item.SendUsingAccount
        return (conta.SmtpAddress if conta else remetente_padrao()), item.Subject, [r.Address for r in item.Recipients]

    remetente, assunto, para = _no_outlook(atualizar)
    _vigiar("atualização de convite", remetente, assunto, para)


# ---------------------------------------------------------------- conferência: a mensagem saiu da Caixa de Saída?

ENVIOS = []  # [{id, tipo, remetente, assunto, para, momento, estado}] — estado: conferindo | enviado | preso | dispensado
_seq = {"n": 0}
OL_FOLDER_OUTBOX = 4


def _na_caixa_de_saida(ol, envio: dict) -> bool:
    conta = _conta(ol, envio["remetente"])
    for item in conta.DeliveryStore.GetDefaultFolder(OL_FOLDER_OUTBOX).Items:
        try:
            if (item.Subject or "") == envio["assunto"]:
                return True
        except Exception:
            continue
    return False


def conferir(envio: dict) -> str:
    """Confere agora; devolve o novo estado."""
    try:
        preso = _no_outlook(_na_caixa_de_saida, envio)
    except Exception as erro:
        envio["detalhe"] = f"não consegui conferir: {erro}"
        return envio["estado"]
    envio["estado"] = "preso" if preso else "enviado"
    envio["conferido_em"] = datetime.now().strftime("%H:%M")
    return envio["estado"]


def _vigiar(tipo: str, remetente: str, assunto: str, para: list[str]) -> None:
    _seq["n"] += 1
    envio = {"id": _seq["n"], "tipo": tipo, "remetente": remetente, "assunto": assunto, "para": para,
             "momento": datetime.now().strftime("%H:%M"), "estado": "conferindo"}
    ENVIOS.append(envio)
    del ENVIOS[:-50]  # guarda só os últimos

    def depois():
        for espera in (60, 120):  # confere com 1 e com 3 minutos
            time.sleep(espera)
            if envio["estado"] == "dispensado" or conferir(envio) == "enviado":
                return
        if envio["estado"] == "preso":
            try:
                import monitor

                monitor.notificar(f"{tipo.capitalize()} preso no Outlook — {remetente}",
                                  f"\"{assunto[:60]}\" não saiu da Caixa de Saída. Abra o Outlook e aperte F9 (Enviar/Receber).",
                                  f"http://localhost:{config.PORTA}/")
            except Exception:
                pass

    threading.Thread(target=depois, daemon=True, name="confere-envio").start()


def presos() -> list[dict]:
    return [e for e in ENVIOS if e["estado"] == "preso"]


def outlook_aberto() -> bool:
    """Outlook rodando com janela (sem janela ele pode não enviar as contas IMAP/Gmail)."""
    try:
        import psutil

        import monitor

        rodando = any((p.info["name"] or "").lower() == "outlook.exe" for p in psutil.process_iter(["name"]))
        return rodando and any(t.endswith("- Outlook") for t in monitor.titulos_janelas())
    except Exception:
        return False


# ---------------------------------------------------------------- convites enviados (evento do Google -> Outlook)

_CONVITES = config.DADOS_DIR / "convites.json"


def _ler() -> dict:
    import json

    try:
        dados = json.loads(_CONVITES.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except Exception:
        return {}


def registrar_convite(event_id: str, remetente: str, entry_id: str) -> None:
    import json

    dados = _ler()
    dados[event_id] = {"remetente": remetente, "entry_id": entry_id, "enviado_em": datetime.now().isoformat(timespec="seconds")}
    _CONVITES.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")


def convite_do_evento(event_id: str) -> dict | None:
    return _ler().get(event_id)
