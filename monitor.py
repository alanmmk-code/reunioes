"""Detecta reuniões do Meet abertas no navegador para gravar automaticamente.

- Início: uma janela com título do Meet ("Meet - abc-defg-hij") E o navegador em chamada.
- Fim: o navegador sai da chamada por alguns segundos.
"Em chamada" = navegador com o microfone aberto OU tocando áudio. Quem entra com o microfone desligado
não abre o microfone, mas o áudio da chamada fica aberto até sair (no mudo o Meet mantém o microfone).
O áudio é a referência do fim porque o título da janela muda quando você troca de aba.
"""

import ctypes
import re
import subprocess
import threading
import time
import winreg
from ctypes import wintypes

NAVEGADORES = ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe")
CHAVE_MIC = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged"
# Durante a chamada a aba se chama "Meet - abc-defg-hij" ou "Meet - Nome da reunião"
# (o navegador acrescenta " - Google Chrome" etc. no fim)
TITULO_MEET = re.compile(r"^Meet\s*[-–:]\s*(.+)$")
CODIGO_MEET = re.compile(r"\b[a-z]{3}-[a-z]{4}-[a-z]{3}\b")

INTERVALO = 3  # segundos entre verificações
FIM_APOS = 15  # segundos com o navegador fora de chamada (sem microfone e sem áudio) para considerar o fim
MEMORIA_TITULO = 120  # segundos que um título do Meet visto continua valendo


def titulos_janelas() -> list[str]:
    user32 = ctypes.windll.user32
    titulos = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                titulos.append(buf.value)
        return True

    user32.EnumWindows(callback, 0)
    return titulos


def janela_meet() -> str | None:
    """Título da janela do Meet, se houver uma visível."""
    for t in titulos_janelas():
        if TITULO_MEET.search(t.strip()):
            return t.strip()
    return None


def navegador_usando_microfone() -> bool:
    """True se algum navegador está captando o microfone agora."""
    try:
        return _microfone_por_sessao_de_audio()
    except Exception:
        return _microfone_pelo_registro()


def navegador_tocando_audio() -> bool:
    """True se algum navegador está com o áudio de saída aberto (numa chamada, mesmo em silêncio)."""
    try:
        return _navegador_em_sessao_de_audio(0)
    except Exception:
        return False


def navegador_em_chamada() -> bool:
    return navegador_usando_microfone() or navegador_tocando_audio()


def _microfone_por_sessao_de_audio() -> bool:
    return _navegador_em_sessao_de_audio(1)


def _navegador_em_sessao_de_audio(fluxo: int) -> bool:
    """Pergunta ao sistema de áudio do Windows quais programas estão com o áudio aberto
    (a mesma informação do mixer de volume). fluxo: 0 = saída, 1 = microfone."""
    import comtypes
    import psutil
    from comtypes import CLSCTX_ALL
    from pycaw.constants import CLSID_MMDeviceEnumerator
    from pycaw.pycaw import IAudioSessionControl2, IAudioSessionManager2, IMMDeviceEnumerator

    comtypes.CoInitialize()
    try:
        enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
        dispositivos = enum.EnumAudioEndpoints(fluxo, 1)  # só os ativos
        for i in range(dispositivos.GetCount()):
            mgr = dispositivos.Item(i).Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
            sessoes = mgr.QueryInterface(IAudioSessionManager2).GetSessionEnumerator()
            for j in range(sessoes.GetCount()):
                s = sessoes.GetSession(j).QueryInterface(IAudioSessionControl2)
                if s.GetState() != 1:  # 1 = ativa
                    continue
                try:
                    if psutil.Process(s.GetProcessId()).name().lower() in NAVEGADORES:
                        return True
                except psutil.Error:
                    continue
        return False
    finally:
        comtypes.CoUninitialize()


def _microfone_pelo_registro() -> bool:
    """Plano B: o Windows registra LastUsedTimeStop = 0 enquanto um app está usando o microfone
    (em algumas versões do Windows esse registro não é atualizado)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_MIC) as chave:
            i = 0
            while True:
                try:
                    nome = winreg.EnumKey(chave, i)
                except OSError:
                    return False
                i += 1
                if not nome.lower().endswith(NAVEGADORES):
                    continue
                with winreg.OpenKey(chave, nome) as app:
                    try:
                        parou, _ = winreg.QueryValueEx(app, "LastUsedTimeStop")
                    except OSError:
                        continue
                    if parou == 0:
                        return True
    except OSError:
        return False


def info_da_janela(titulo: str) -> tuple[str | None, str]:
    """Extrai (código do Meet, nome da reunião) do título da janela."""
    codigo = CODIGO_MEET.search(titulo)
    m = TITULO_MEET.search(titulo)
    nome = m.group(1).split(" - ")[0].strip() if m else "Reunião do Meet"
    # Sem nome de reunião no título, sobra só o nome do navegador ("Meet - Google Chrome")
    navegadores = ("google chrome", "microsoft edge", "microsoft​ edge", "mozilla firefox", "brave", "opera", "vivaldi")
    if not nome or nome.lower().replace("​", "") in navegadores:
        nome = "Reunião do Meet"
    return (codigo.group(0) if codigo else None), nome


def notificar(titulo: str, mensagem: str, url: str | None = None) -> None:
    """Notificação do Windows (canto da tela), sem abrir janela."""
    titulo, mensagem = (s.replace("'", "’").replace("<", "").replace("&", "e") for s in (titulo, mensagem))
    # Com url, clicar na notificação abre a página no navegador
    abrir = (f' activationType="protocol" launch="{url.replace("&", "&amp;")}"' if url else "")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml('<toast{abrir}><visual><binding template=\"ToastGeneric\"><text>{titulo}</text><text>{mensagem}</text></binding></visual></toast>');"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def iniciar(ao_detectar, ao_encerrar, gravando_automatico, extensao_ativa=lambda: False) -> None:
    """Roda em segundo plano.

    ao_detectar(titulo_janela): chamado quando uma chamada do Meet começa
    ao_encerrar(): chamado quando a chamada termina
    gravando_automatico(): True se há uma gravação iniciada por este monitor
    extensao_ativa(): True se a extensão do Chrome está numa aba do Meet (aí é ela quem inicia a gravação)
    """

    def loop():
        ultimo_titulo, visto_em, fora_desde = None, 0.0, None
        while True:
            try:
                titulo = janela_meet()
                if titulo:
                    ultimo_titulo, visto_em = titulo, time.time()
                em_chamada = navegador_em_chamada()

                if gravando_automatico():
                    if em_chamada:
                        fora_desde = None
                    else:
                        fora_desde = fora_desde or time.time()
                        if time.time() - fora_desde >= FIM_APOS:
                            fora_desde = None
                            ultimo_titulo = None
                            ao_encerrar()
                elif (em_chamada and ultimo_titulo and time.time() - visto_em <= MEMORIA_TITULO
                      and not extensao_ativa()):
                    ao_detectar(ultimo_titulo)
                    ultimo_titulo = None  # não dispara de novo para a mesma chamada
            except Exception as e:
                print(f"[monitor] erro: {e}")
            time.sleep(INTERVALO)

    threading.Thread(target=loop, daemon=True, name="monitor-meet").start()
