# Assistente de Reuniões (Google Meet + Claude)

Lê sua agenda, busca a transcrição de cada reunião do Meet, usa o Claude para entender a conversa e
gerar a ata (resumo, assuntos, decisões, ações com responsável e prazo), salva a ata como Google Doc,
envia por e-mail aos participantes e agenda a próxima reunião com link do Meet.

## Importante sobre a conta

- A **transcrição do Meet** só existe em contas **Google Workspace** (Business Standard ou superior).
  Use sua conta **comercial** para fazer login nesta ferramenta e para criar as reuniões.
- Em cada reunião, ative **Atividades → Transcrição** (ou configure a transcrição automática no
  evento da agenda). Sem isso não há texto para gerar a ata.
- Participantes com Hotmail/Gmail pessoal podem participar e receber a ata normalmente.
- Reunião sem transcrição? Use o botão **Colar transcrição** (aceita texto ou .txt/.vtt).

## Instalação (uma vez)

### 1. Python e dependências

```powershell
cd C:\Orca\Reunioes
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 2. Chave do Claude

1. Crie uma chave em https://console.anthropic.com/settings/keys
2. Copie `.env.example` para `.env` e cole a chave em `ANTHROPIC_API_KEY`.

### 3. Google Cloud (credentials.json)

Faça isso **logado na conta comercial**:

1. Acesse https://console.cloud.google.com/ e crie um projeto (ex.: "Reunioes").
2. Em **APIs e serviços → Biblioteca**, ative:
   Google Calendar API, Google Drive API, Gmail API e **Google Meet REST API**.
3. Em **APIs e serviços → Tela de permissão OAuth**: tipo **Interno** (se disponível no seu Workspace)
   ou Externo adicionando seu e-mail como usuário de teste.
4. Em **Credenciais → Criar credenciais → ID do cliente OAuth → App para computador**.
5. Baixe o JSON e salve como `C:\Orca\Reunioes\credentials.json`.

### 4. Conectar a conta

```powershell
.venv\Scripts\python reunioes.py login
```

O navegador abre; escolha a conta comercial e autorize.

## Uso

```powershell
.venv\Scripts\python reunioes.py
```

Abre o painel em http://localhost:5055:

- **Já aconteceram** → *Gerar ata* (busca a transcrição, gera a ata e o Google Doc).
- Na ata → *Enviar por e-mail* e *Agendar próxima reunião no Meet* (já com pauta e convidados).
- **Próximas** → atalho para entrar no Meet.

### Gravar a reunião no PC (contas sem transcrição do Meet)

Em contas Google pessoais o Meet não gera transcrição. Nesse caso, use **● Entrar e gravar** (abre o Meet
e começa a gravar) ou **● Gravar agora** para uma reunião fora da agenda. Ao final, clique em
**■ Parar e gerar ata**: o áudio é transcrito no próprio PC com o Whisper e a ata é gerada normalmente.

- São gravadas duas trilhas: seu **microfone** (marcado com `SEU_NOME`) e o **áudio da chamada**
  ("Outros participantes"). Use **fone de ouvido** para separar melhor quem falou; sem fone, as falas
  que vazam do alto-falante para o microfone são filtradas automaticamente.
- A transcrição roda no PC (o áudio não sai da máquina). No primeiro uso o modelo é baixado (~480 MB).
- `WHISPER_MODELO` no `.env`: `small` (padrão) é rápido; `medium` ou `large-v3-turbo` são mais precisos
  e mais lentos.
- Os áudios e transcrições ficam em `gravacoes/`. Apague os antigos quando quiser liberar espaço.

### Abrir junto com o Windows

Já configurado: o atalho `Assistente de Reunioes` na pasta Inicializar do Windows (`Win+R` → `shell:startup`)
abre o painel sem janela de terminal sempre que você liga o PC. Há uma cópia do atalho na Área de Trabalho
para reabrir o painel quando quiser. Se o painel já estiver rodando, o atalho só abre o navegador.
Para desativar, apague o atalho da pasta Inicializar. Erros ficam registrados em `reunioes.log`.

### Modo automático (opcional)

`.venv\Scripts\python reunioes.py auto` gera atas de todas as reuniões terminadas nos últimos 2 dias
que ainda não têm ata. Para rodar sozinho a cada hora, crie uma tarefa no Agendador do Windows:

```powershell
schtasks /Create /SC HOURLY /TN "Atas de Reuniao" /TR "C:\Orca\Reunioes\.venv\Scripts\python.exe C:\Orca\Reunioes\reunioes.py auto"
```

Com `AUTO_ENVIAR_EMAIL=sim` no `.env`, a ata também é enviada aos convidados automaticamente.

## Arquivos

| Arquivo | Função |
|---|---|
| `reunioes.py` | Painel web, linha de comando e fluxo principal |
| `google_services.py` | Agenda, transcrições (API do Meet / anexos do evento), Google Docs, Gmail |
| `analisador.py` | Prompt e formato da ata gerada pelo Claude |
| `atas.py` | Salva as atas em `atas/` e monta o HTML do Doc/e-mail |

`credentials.json`, `token.json`, `.env` e a pasta `atas/` contêm dados privados — não compartilhe.
