# Instala o Assistente de Reuniões neste PC.
# Uso (na pasta do projeto, depois do git clone):
#     powershell -ExecutionPolicy Bypass -File instalar.ps1

$ErrorActionPreference = "Stop"
$pasta = $PSScriptRoot
Set-Location $pasta
function Passo($texto) { Write-Host "`n==> $texto" -ForegroundColor Cyan }

# 1. Python
Passo "Verificando o Python"
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "Python não encontrado. Instalando o Python 3.12 pelo winget..."
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
}
py -3.12 --version

# 2. Ambiente e bibliotecas
Passo "Instalando as bibliotecas (pode levar alguns minutos)"
if (-not (Test-Path ".venv")) { py -3.12 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -q --upgrade pip
.\.venv\Scripts\python.exe -m pip install -q -r requirements.txt

# 3. Configurações
Passo "Configurações (.env)"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    $nome = Read-Host "Seu nome (como aparece nas reuniões)"
    if ($nome) { (Get-Content ".env" -Encoding UTF8) -replace '^SEU_NOME=.*', "SEU_NOME=$nome" | Set-Content ".env" -Encoding UTF8 }
} else { Write-Host ".env já existe, mantido." }

# 4. Credencial do Google
Passo "Credencial do Google (credentials.json)"
if (-not (Test-Path "credentials.json")) {
    Write-Host "No outro PC ela foi copiada para o seu Google Drive."
    Write-Host "1. Abra a pasta 'Reuniões - dados do app' no Google Drive (vou abrir o Drive agora)."
    Write-Host "2. Baixe o arquivo 'config__credentials.json'."
    Write-Host "3. Salve nesta pasta com o nome credentials.json:  $pasta"
    Start-Process "https://drive.google.com/drive/search?q=config__credentials.json"
    while (-not (Test-Path "credentials.json")) {
        Read-Host "Quando o arquivo estiver na pasta, aperte Enter"
        $baixado = Get-ChildItem "$env:USERPROFILE\Downloads" -Filter "config__credentials*.json" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not (Test-Path "credentials.json") -and $baixado) {
            Copy-Item $baixado.FullName "credentials.json"
            Write-Host "Encontrei em Downloads e copiei: $($baixado.Name)"
        }
    }
}

# 5. Login no Google
Passo "Login no Google (o navegador vai abrir: entre com a mesma conta do outro PC)"
.\.venv\Scripts\python.exe reunioes.py login

# 6. Claude Code (usa sua assinatura do Claude, sem créditos de API)
Passo "Verificando o Claude Code"
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    Write-Host "Claude Code não encontrado. Instalando..."
    Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
    Write-Host "Agora abra um terminal, rode 'claude' e faça o login com a sua conta do Claude." -ForegroundColor Yellow
} else {
    claude auth status
}

# 7. Primeira sincronização: traz atas, tarefas e gravações do Drive
Passo "Trazendo seus dados do Google Drive"
.\.venv\Scripts\python.exe -c "import sincronia; print(sincronia.sincronizar())"

# 8. Atalhos (inicialização do Windows e Área de Trabalho)
Passo "Criando atalhos"
$ws = New-Object -ComObject WScript.Shell
foreach ($destino in @([Environment]::GetFolderPath("Startup"), [Environment]::GetFolderPath("Desktop"))) {
    $lnk = $ws.CreateShortcut("$destino\Assistente de Reunioes.lnk")
    $lnk.TargetPath = "$pasta\.venv\Scripts\pythonw.exe"
    $lnk.Arguments = "`"$pasta\reunioes.py`""
    $lnk.WorkingDirectory = $pasta
    $lnk.Description = "Abre o painel de reuniões do Meet"
    $lnk.Save()
}

Passo "Pronto! Abrindo o painel"
Start-Process "$pasta\.venv\Scripts\pythonw.exe" -ArgumentList "`"$pasta\reunioes.py`"" -WorkingDirectory $pasta
