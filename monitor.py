"""Detecta reuniões do Meet abertas no navegador para gravar automaticamente.

- Início: uma janela com título do Meet ("Meet - abc-defg-hij") E o navegador usando o microfone.
- Fim: o navegador para de usar o microfone (saiu da chamada) por alguns segundos.
O microfone é a referência do fim porque o título da janela muda quando você troca de aba.
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
FIM_APOS = 15  # segundos sem microfone no navegador para considerar que a chamada acabou
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
    """O Windows registra LastUsedTimeStop = 0 enquanto um app está usando o microfone."""
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
    return (codigo.group(0) if codigo else None), nome


def notificar(titulo: str, mensagem: str) -> None:
    """Notificação do Windows (canto da tela), sem abrir janela."""
    titulo, mensagem = (s.replace("'", "’").replace("<", "").replace("&", "e") for s in (titulo, mensagem))
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>{titulo}</text><text>{mensagem}</text></binding></visual></toast>');"
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


def iniciar(ao_detectar, ao_encerrar, gravando_automatico) -> None:
    """Roda em segundo plano.

    ao_detectar(titulo_janela): chamado quando uma chamada do Meet começa
    ao_encerrar(): chamado quando a chamada termina
    gravando_automatico(): True se há uma gravação iniciada por este monitor
    """

    def loop():
        ultimo_titulo, visto_em, sem_mic_desde = None, 0.0, None
        while True:
            try:
                titulo = janela_meet()
                if titulo:
                    ultimo_titulo, visto_em = titulo, time.time()
                mic = navegador_usando_microfone()

                if gravando_automatico():
                    if mic:
                        sem_mic_desde = None
                    else:
                        sem_mic_desde = sem_mic_desde or time.time()
                        if time.time() - sem_mic_desde >= FIM_APOS:
                            sem_mic_desde = None
                            ultimo_titulo = None
                            ao_encerrar()
                elif mic and ultimo_titulo and time.time() - visto_em <= MEMORIA_TITULO:
                    ao_detectar(ultimo_titulo)
                    ultimo_titulo = None  # não dispara de novo para a mesma chamada
            except Exception as e:
                print(f"[monitor] erro: {e}")
            time.sleep(INTERVALO)

    threading.Thread(target=loop, daemon=True, name="monitor-meet").start()
