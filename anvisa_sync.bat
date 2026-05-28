@echo off
cd /d "%~dp0"
echo ============================================================
echo  Sincronizacao ANVISA - Catalogo visivel por EAN unico
echo ============================================================
echo.
echo Opcoes:
echo   [1] Sincronizar pendentes  (catalogo visivel)
echo   [2] Re-sincronizar tudo    (apaga nao-encontrados e rebusca)
echo   [3] Testar com 5 produtos  (rapido, so para confirmar)
echo   [4] Sincronizar recorte antigo DNS/Vitnatu
echo   [5] Sincronizar + revisar cache ANVISA com Claude
echo   [6] Sincronizar lote de 500 pendentes
echo   [7] Sair
echo.
set /p opcao="Escolha uma opcao (1-7): "

if "%opcao%"=="1" (
    python anvisa_sync.py
) else if "%opcao%"=="2" (
    python anvisa_sync.py --forcar
) else if "%opcao%"=="3" (
    python anvisa_sync.py --limite 5
) else if "%opcao%"=="4" (
    python anvisa_sync.py --recorte-antigo
) else if "%opcao%"=="5" (
    python anvisa_sync.py --validar-claude
) else if "%opcao%"=="6" (
    set /p lote="Numero do lote (1, 2, 3...): "
    python anvisa_sync.py --lote %lote% --lote-size 500
) else if "%opcao%"=="7" (
    exit /b 0
) else (
    echo Opcao invalida.
)

echo.
pause
