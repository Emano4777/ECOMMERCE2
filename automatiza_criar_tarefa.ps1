# Registra DUAS tarefas agendadas na maquina da loja de Sao Jose do Rio
# Preto (onde o MySQL do Automatiza roda): uma leve (pedidos+nota fiscal,
# 5 em 5 min, pra farmacia ver o pedido rapido) e uma pesada (catalogo
# completo, 1 em 1 hora, nao precisa ser mais rapido que isso). Mesmo
# padrao ja usado pro PoupaquiTesteDiario (CRIAR_TAREFA.ps1), so que
# recorrente em vez de 1x por dia e dividido em 2 tarefas.
#
# ANTES DE RODAR:
#   1. Copie automatiza_sync.py e o .env (com DATABASE_URL + credenciais
#      do Automatiza preenchidas) pra essa maquina, ex: C:\poupaqui\automatiza_sync\
#   2. Instale as dependencias:  pip install psycopg2-binary pymysql python-dotenv
#   3. Confirme que "python automatiza_sync.py" roda sem erro nessa pasta
#      ANTES de agendar (senao a tarefa so vai falhar silenciosamente)
#   4. Ajuste $workdir abaixo se o caminho for diferente
#   5. Rode esse .ps1 como Administrador

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Host "python.exe nao encontrado no PATH. Instale o Python (marcando 'Add to PATH') antes de continuar." -ForegroundColor Red
    Write-Host "Pressione Enter para fechar..."
    Read-Host
    exit
}
$python  = $pythonCmd.Source
$workdir = "C:\poupaqui\automatiza_sync"
$script  = Join-Path $workdir "automatiza_sync.py"
$logdir  = Join-Path $workdir "logs"

New-Item -ItemType Directory -Force -Path $logdir | Out-Null

function Registrar-Tarefa($nome, $arg, $minutos, $logfile) {
    Unregister-ScheduledTask -TaskName $nome -Confirm:$false -ErrorAction SilentlyContinue

    # cmd /c pra redirecionar a saida do python pro log, sem precisar de wrapper .bat separado
    $action = New-ScheduledTaskAction `
        -Execute "cmd.exe" `
        -Argument "/c `"`"$python`" `"$script`" $arg >> `"$logfile`" 2>&1`"" `
        -WorkingDirectory $workdir

    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes $minutos) `
        -RepetitionDuration ([TimeSpan]::MaxValue)

    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
        -RestartCount 2 `
        -RestartInterval (New-TimeSpan -Minutes 2) `
        -StartWhenAvailable `
        -DontStopOnIdleEnd `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask `
        -TaskName $nome `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -RunLevel Highest `
        -Force | Out-Null

    if ($?) {
        Write-Host "Tarefa '$nome' criada -- roda a cada $minutos minuto(s)." -ForegroundColor Green
    } else {
        Write-Host "Falha ao criar '$nome'." -ForegroundColor Red
    }
}

Registrar-Tarefa "PoupaquiAutomatizaPedidosSJRP"  "pedidos"  5  (Join-Path $logdir "sync_pedidos.log")
Registrar-Tarefa "PoupaquiAutomatizaProdutosSJRP" "produtos" 60 (Join-Path $logdir "sync_produtos.log")

Write-Host ""
Write-Host "Logs em: $logdir"
Write-Host ""
schtasks /Query /TN "PoupaquiAutomatizaPedidosSJRP" /FO LIST
schtasks /Query /TN "PoupaquiAutomatizaProdutosSJRP" /FO LIST

Write-Host ""
Write-Host "Pressione Enter para fechar..."
Read-Host
