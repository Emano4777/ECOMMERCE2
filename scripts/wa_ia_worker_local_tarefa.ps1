# Registra o worker local do atendimento WhatsApp IA como tarefa agendada do
# Windows (inicia no logon do usuario, uma unica instancia, reinicia se cair).
# NAO muda o modo: o worker so atende com motor 'local' e modo test/live no banco.
#   Registrar:  powershell -ExecutionPolicy Bypass -File scripts\wa_ia_worker_local_tarefa.ps1
#   Remover:    Unregister-ScheduledTask -TaskName "PoupAqui WA IA Worker" -Confirm:$false
$repo = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python.exe -ErrorAction Stop).Source
$log = Join-Path $repo "logs\wa_ia_worker_local.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

$cmd = "/c cd /d `"$repo`" && `"$python`" scripts\wa_ia_worker_local.py --loop 30 --assistente >> `"$log`" 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "PoupAqui WA IA Worker" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Atendimento WhatsApp IA (Ollama local)" -Force


