@echo off
cd /d "%~dp0"
echo ============================================================
echo  Sincronizacao ANVISA - Produtos DNS/Vitnatu (~200 produtos)
echo ============================================================
echo.
echo Opcoes:
echo   [1] Sincronizar pendentes  (~10 min)
echo   [2] Re-sincronizar tudo    (apaga nao-encontrados e rebusca)
echo   [3] Testar com 5 produtos  (rapido, so para confirmar)
echo   [4] Sair
echo.
set /p opcao="Escolha uma opcao (1-4): "

if "%opcao%"=="1" (
    python anvisa_sync.py
) else if "%opcao%"=="2" (
    python anvisa_sync.py --forcar
) else if "%opcao%"=="3" (
    python anvisa_sync.py --limite 5
) else if "%opcao%"=="4" (
    exit /b 0
) else (
    echo Opcao invalida.
)

echo.
pause
