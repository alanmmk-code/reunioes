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

### 2. Acesso ao Claude

Copie `.env.example` para `.env`. Por padrão (`IA_MODO=claude_code`) a ferramenta usa o **Claude Code
logado com a sua assinatura do Claude** (Pro/Max): não precisa de créditos de API, só de ter o Claude Code
instalado e logado (`claude` no terminal → `/login`). O uso conta na cota do seu plano.

Se preferir a API (cobrança por uso), crie uma chave em https://console.anthropic.com/settings/keys,
coloque em `ANTHROPIC_API_KEY` e use `IA_MODO=api` (ou `auto`, que usa a API e cai para o plano se faltar crédito).
Pela assinatura, as sugestões ao vivo demoram ~10 s e só disparam sozinhas quando a fala parece uma pergunta
ou cita o seu nome (o botão "O que eu respondo?" sempre funciona).

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

### Painel: tudo numa tela só

A página inicial do painel (http://localhost:5055) reúne tudo:

- **Resumo do dia**: tarefas atrasadas, que vencem hoje, compromissos de hoje e tarefas em aberto.
- **Alertas**: gravação em andamento, ata sendo gerada, gravações aguardando "Gerar ata?".
- **Agenda da semana** (todos os compromissos do Google Agenda, com ou sem Meet), dia a dia, com as setas
  para outras semanas. Nas reuniões do Meet: **Resumo**, **Entrar e gravar**, **Ver ata** ou **Gerar ata**.
  Os prazos das suas tarefas aparecem no dia em que vencem.
- **Minhas tarefas**: concluir com um clique e adicionar tarefa rápida.
- **Reuniões recentes** e o status das atas.
- **Histórico de reuniões por cliente**: cada cliente com as reuniões em linha do tempo (mais recente
  primeiro) e um resumo curto de cada conversa; clicando, aparecem o resumo completo, as decisões, o que
  ficou com você e com o cliente, e os botões **Ver ata** e **Google Doc**. A busca filtra por cliente,
  assunto ou decisão.

A aba **Tarefas** continua para a gestão completa (filtros, tarefas do cliente, clientes).

### Clientes, cobrança, perguntas e semana

- **Clientes que precisam de atenção** (no Painel e na aba **Clientes**): tarefas suas atrasadas, pendências
  do cliente vencidas e clientes sem reunião há 30 dias ou mais.
- **Ficha do cliente**: suas tarefas, pendências do cliente, próximas reuniões, contatos (tirados dos
  participantes das reuniões), histórico de atas e **anotações** (ligações, WhatsApp — sincronizadas entre PCs).
- **Cobrar pendências**: monta um e-mail com o que ficou do lado do cliente; você revisa e envia pelo Gmail.
  O envio fica registrado nas anotações.
- **Pergunte sobre suas reuniões**: o Claude responde lendo as atas e mostra de qual reunião tirou a resposta
  (usa um pouco da cota do plano por pergunta).
- **Nova reunião**: cria o evento com Meet no Google Agenda, com os contatos e as pendências do cliente na pauta.
- **Semana**: o que foi decidido, o que você concluiu, próxima semana, atrasadas e números do mês. Notificação
  toda sexta às 16:00 (`AVISO_SEMANAL_HORA`).
- **Status do sistema** no rodapé do Painel: Google, Claude, Drive e detector do Meet.

### Lembretes

- **Resumo pré-reunião**: 10 minutos antes de cada reunião do Meet, uma notificação com o que está pendente
  com o cliente; clicando, abre o resumo (suas pendências, o que cobrar do cliente, o que foi decidido na
  última reunião e a pauta do convite). Também pelo botão **Resumo** na agenda.
- **Aviso diário** (08:30, ou ao ligar o PC se for mais tarde): quantas tarefas vencem hoje e quantas estão
  atrasadas; clicando, abre a lista. Só avisa se houver algo para hoje ou atrasado.
- Ajuste em `AVISO_DIARIO_HORA` e `BRIEFING_MINUTOS_ANTES` no `.env`. Os lembretes não usam o Claude.

### Durante a reunião: cliente e sugestões de resposta

Quando uma chamada do Meet começa, a gravação inicia sozinha e abre uma **janelinha no canto da tela**:

1. Ela pergunta **"Qual cliente é esta reunião?"** (sugere o cliente de reuniões anteriores com as mesmas
   pessoas; dá para digitar um cliente novo).
2. Enquanto a conversa acontece, ela **sugere o que responder** sempre que alguém se dirige a você
   (pergunta, pedido de prazo, preço, opinião). O botão **"O que eu respondo?"** pede uma sugestão na hora.
   O assistente usa a pauta do convite, as tarefas em aberto do cliente e as atas anteriores.
3. A janelinha fica **invisível para quem assiste ao seu compartilhamento de tela**.

Ao sair da chamada, o painel pergunta **"Gerar a ata desta reunião?"** (Sim / Não, apagar o áudio / Decidir depois).

### Tarefas por cliente

As ações de cada ata viram tarefas do cliente da reunião. A tela **Tarefas** mostra:

- **O que eu tenho que fazer**: o que ficou com você/sua equipe; **Todas**: inclui o que ficou com o cliente.
- Agrupado por cliente, com prazo (atrasadas em vermelho), status (a fazer / fazendo / feito) e link para a ata.
- Tarefas manuais e cadastro de clientes. Trocar o cliente de uma ata move as tarefas dela junto.

Os dados ficam em `dados.db` (SQLite, local, fora do Git).

### Usar em mais de um PC

O **código** fica no GitHub e os **dados** (atas, tarefas, clientes, gravações) ficam no seu **Google Drive**,
na pasta **"Reuniões - dados do app"**. Os dados nunca vão para o GitHub (o repositório é público).

**Instalar em outro PC:**

```powershell
git clone https://github.com/alanmmk-code/reunioes C:\Orca\Reunioes
cd C:\Orca\Reunioes
powershell -ExecutionPolicy Bypass -File instalar.ps1
```

O instalador configura o Python, pede o seu nome, pega a credencial do Google que ficou no Drive,
faz o login, confere o Claude Code, traz todos os seus dados do Drive e cria os atalhos.

**Atualizar o código** (quando houver mudanças no GitHub): `git pull` e reabra o painel.

**O que conferir no PC novo depois do instalador:**

- **Outlook clássico** instalado e aberto, com as contas de e-mail configuradas: e-mails (ata, cobrança) e
  convites de reunião saem por ele, pela conta escolhida em "Enviar de". Defina a conta padrão em
  `EMAIL_REMETENTE` no `.env`.
- **Microfone**: em `MICROFONE` no `.env`, uma parte do nome do microfone que deve gravar (ex.: `Realtek`).
  Vazio = o padrão do Windows. Microfone de fone Bluetooth em chamada costuma chegar baixo e piorar a
  transcrição; o do notebook ou um fone com fio funcionam melhor. Para comparar dois microfones numa reunião,
  use `MICROFONE_COMPARAR`.
- **Claude Code** logado com a sua assinatura (`claude` no terminal → `/login`).

**Sincronização dos dados** (automática: ao abrir, a cada 5 minutos e logo depois de cada alteração;
ou pelo botão **Sincronizar** no topo do painel):

- Tarefas e clientes são mesclados **registro a registro**: se você concluiu uma tarefa num PC e criou outra
  no outro, as duas mudanças ficam. Se a mesma tarefa foi alterada nos dois, vale a alteração mais recente.
  Um cliente criado com o mesmo nome nos dois PCs vira um só.
- Atas e gravações pendentes: vale a versão mais recente.
- **Áudios não vão para o Drive por padrão** (cerca de 230 MB por hora). Se a ata for gerada em outro PC,
  ele usa a transcrição feita ao vivo. Para enviar os áudios também: `SINCRONIZAR_AUDIO=sim` no `.env`.

### Excluir

- **Ata**: botão **Excluir** no histórico por cliente, **×** em "Reuniões recentes" ou **Excluir ata** na página
  da ata. Abre uma confirmação e apaga a ata deste PC e da pasta do Google Drive; o **Google Doc** da ata vai
  para a **lixeira do Drive** (recuperável por 30 dias). Opcionalmente exclui as tarefas em aberto da reunião.
- **Gravação**: "Não, apagar o áudio" na tela "Gerar ata?" apaga áudios e transcrição daqui e do Drive.
- **Reunião sem ata** em "Reuniões recentes": o **×** só tira da lista (o evento continua no Google Agenda).
- As exclusões valem em **todos os PCs**: ficam registradas em `excluidos.json` no Drive, e cada PC apaga a
  própria cópia na sincronização seguinte (nada excluído volta).

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
