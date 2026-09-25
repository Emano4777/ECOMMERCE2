# Registra a tarefa agendada que roda automatiza_sync.py de 30 em 30 min,
# na maquina da loja de Sao Jose do Rio Preto (onde o MySQL do Automatiza
# roda). Mesmo padrao ja usado pro PoupaquiTesteDiario (CRIAR_TAREFA.ps1),
# so que recorrente em vez de 1x por dia.
#
# ANTES DE RODAR:
#   1. Copie automatiza_sync.py, automatiza_sync.env.example e requirements
#      pra essa maquina (ex: C:\poupaqui\automatiza_sync\)
#   2. Copie automatiza_sync.env.example para ".env" na mesma pasta e
#      preencha DATABASE_URL de verdade
#   3. Instale as dependencias:  pip install psycopg2-binary pymysql python-dotenv
#   4. Ajuste as variaveis $python/$script/$workdir abaixo se o caminho for diferente
#   5. Rode esse .ps1 como Administrador

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

$python   = "C:\poupaqui\automatiza_sync\venv\Scripts\python.exe"
$script   = "C:\poupaqui\automatiza_sync\automatiza_sync.py"
$workdir  = "C:\poupaqui\automatiza_sync"
$logfile  = "C:\poupaqui\automatiza_sync\logs\sync.log"
$taskName = "PoupaquiAutomatizaSyncSJRP"

New-Item -ItemType Directory -Force -Path (Split-Path $logfile) | Out-Null

# Remove tarefa anterior se existir
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# cmd /c pra redirecionar a saida do python pro log, sem precisar de wrapper .bat separado
$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"`"$python`" `"$script`" >> `"$logfile`" 2>&1`"" `
    -WorkingDirectory $workdir

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration ([TimeSpan]::MaxValue)

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -Force

if ($?) {
    Write-Host ""
    Write-Host "Tarefa '$taskName' criada com sucesso -- roda a cada 30 minutos." -ForegroundColor Green
    Write-Host "Log em: $logfile"
    Write-Host ""
    schtasks /Query /TN $taskName /FO LIST
} else {
    Write-Host "Falha ao criar tarefa." -ForegroundColor Red
}

Write-Host ""
Write-Host "Pressione Enter para fechar..."
Read-Host
