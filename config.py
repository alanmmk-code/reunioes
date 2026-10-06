import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
# Como falar com o Claude: claude_code (sua assinatura, sem créditos), api (créditos) ou auto
IA_MODO = os.getenv("IA_MODO", "claude_code").lower()
# Modelo usado pelo Claude Code (vazio = o padrão do seu plano)
CLAUDE_CODE_MODELO = os.getenv("CLAUDE_CODE_MODELO", "")
TIMEZONE = os.getenv("TIMEZONE", "America/Sao_Paulo")
AUTO_ENVIAR_EMAIL = os.getenv("AUTO_ENVIAR_EMAIL", "nao").lower() in ("sim", "s", "true", "1")
PORTA = int(os.getenv("PORTA", "5055"))

# Arquivos de autenticação do Google
CREDENTIALS_FILE = BASE_DIR / "credentials.json"  # baixado do Google Cloud Console
TOKEN_FILE = BASE_DIR / "token.json"  # gerado no primeiro login

# Dados locais (atas, gravações, banco de tarefas). São sincronizados pelo Google Drive, não pelo Git.
DADOS_DIR = BASE_DIR

# Onde as atas geradas ficam salvas localmente
ATAS_DIR = DADOS_DIR / "atas"
ATAS_DIR.mkdir(exist_ok=True)

# Gravação local + transcrição com Whisper (para contas sem transcrição do Meet)
GRAVACOES_DIR = DADOS_DIR / "gravacoes"
GRAVACOES_DIR.mkdir(exist_ok=True)
WHISPER_MODELO = os.getenv("WHISPER_MODELO", "small")  # tiny, base, small, medium, large-v3-turbo
WHISPER_MODELO_AO_VIVO = os.getenv("WHISPER_MODELO_AO_VIVO", "base")  # seu microfone, ao vivo (rápido)
WHISPER_MODELO_AO_VIVO_OUTROS = os.getenv("WHISPER_MODELO_AO_VIVO_OUTROS", "small")  # os outros, ao vivo
SEU_NOME = os.getenv("SEU_NOME", "Eu")
# Gravar sozinho quando uma chamada do Meet for detectada no navegador
GRAVACAO_AUTOMATICA = os.getenv("GRAVACAO_AUTOMATICA", "sim").lower() in ("sim", "s", "true", "1")
# Transcrever durante a reunião e sugerir o que responder (janelinha de sugestões)
ASSISTENTE_AO_VIVO = os.getenv("ASSISTENTE_AO_VIVO", "sim").lower() in ("sim", "s", "true", "1")
# Gravações automáticas mais curtas que isso são descartadas (ex.: teste de câmera)
DURACAO_MINIMA_SEG = int(os.getenv("DURACAO_MINIMA_SEG", "60"))

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",  # ler agenda e agendar follow-ups
    "https://www.googleapis.com/auth/drive.readonly",  # ler transcrições do Meet
    "https://www.googleapis.com/auth/drive.file",  # criar o Google Doc da ata
    "https://www.googleapis.com/auth/gmail.send",  # enviar a ata por e-mail
    "https://www.googleapis.com/auth/meetings.space.readonly",  # transcrições via API do Meet
]

# Lembretes: aviso diário das tarefas (HH:MM) e resumo antes de cada reunião
AVISO_DIARIO_HORA = os.getenv("AVISO_DIARIO_HORA", "08:30")
BRIEFING_MINUTOS_ANTES = int(os.getenv("BRIEFING_MINUTOS_ANTES", "10"))
AVISO_SEMANAL_HORA = os.getenv("AVISO_SEMANAL_HORA", "16:00")  # sexta-feira
LEMBRETES = os.getenv("LEMBRETES", "sim").lower() in ("sim", "s", "true", "1")

# Sincronização dos dados entre PCs pelo Google Drive
SINCRONIZAR = os.getenv("SINCRONIZAR", "sim").lower() in ("sim", "s", "true", "1")
# Enviar também os áudios (grandes: ~230 MB por hora de reunião). Sem isso, só textos e atas vão para o Drive.
SINCRONIZAR_AUDIO = os.getenv("SINCRONIZAR_AUDIO", "nao").lower() in ("sim", "s", "true", "1")
PASTA_DRIVE = os.getenv("PASTA_DRIVE", "Reuniões - dados do app")
