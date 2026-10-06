import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
TIMEZONE = os.getenv("TIMEZONE", "America/Sao_Paulo")
AUTO_ENVIAR_EMAIL = os.getenv("AUTO_ENVIAR_EMAIL", "nao").lower() in ("sim", "s", "true", "1")
PORTA = int(os.getenv("PORTA", "5055"))

# Arquivos de autenticação do Google
CREDENTIALS_FILE = BASE_DIR / "credentials.json"  # baixado do Google Cloud Console
TOKEN_FILE = BASE_DIR / "token.json"  # gerado no primeiro login

# Onde as atas geradas ficam salvas localmente
ATAS_DIR = BASE_DIR / "atas"
ATAS_DIR.mkdir(exist_ok=True)

# Gravação local + transcrição com Whisper (para contas sem transcrição do Meet)
GRAVACOES_DIR = BASE_DIR / "gravacoes"
GRAVACOES_DIR.mkdir(exist_ok=True)
WHISPER_MODELO = os.getenv("WHISPER_MODELO", "small")  # tiny, base, small, medium, large-v3-turbo
SEU_NOME = os.getenv("SEU_NOME", "Eu")

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",  # ler agenda e agendar follow-ups
    "https://www.googleapis.com/auth/drive.readonly",  # ler transcrições do Meet
    "https://www.googleapis.com/auth/drive.file",  # criar o Google Doc da ata
    "https://www.googleapis.com/auth/gmail.send",  # enviar a ata por e-mail
    "https://www.googleapis.com/auth/meetings.space.readonly",  # transcrições via API do Meet
]
