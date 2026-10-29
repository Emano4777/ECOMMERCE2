# Registra o tunel SSH 127.0.0.1:11435 -> ollama-piloto (HostGator) como tarefa agendada do
# Windows (inicia no logon, uma unica instancia, reinicia se cair). Usado pelo worker local
# com OLLAMA_URL=http://127.0.0.1:11435 no .env. Escuta so em loopback.
#   Registrar:  powershell -ExecutionPolicy Bypass -File scripts\hostgator_ollama_tunel_tarefa.ps1
#   Remover:    Unregister-ScheduledTask -TaskName "PoupAqui Ollama Tunel HostGator" -Confirm:$false
$repo = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python.exe -ErrorAction Stop).Source
$log = Join-Path $repo "logs\hostgator_ollama_tunel.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

$cmd = "/c cd /d `"$repo`" && `"$python`" scripts\hostgator_ollama_tunel.py >> `"$log`" 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "PoupAqui Ollama Tunel HostGator" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Tunel SSH loopback para o ollama-piloto da HostGator" -Force
