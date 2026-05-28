#!/bin/bash
# Wrapper para cron — limpa automatiza_estoque e automatiza_entradas antigos
# Rodado 3x/semana: segunda, quarta, sexta às 02:30
# Cron: 30 2 * * 1,3,5 /root/scripts/pedidoeletronico/run_limpar_automatiza.sh

SCRIPT_DIR="/root/scripts/pedidoeletronico"
VENV="$SCRIPT_DIR/venv/bin/python"
SCRIPT="$SCRIPT_DIR/limpar_automatiza_antigos.py"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/limpar_automatiza_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOG_DIR"

cd "$SCRIPT_DIR" || exit 1

# Carregar variáveis de ambiente do .env (suporta CRLF do Windows)
if [ -f "$SCRIPT_DIR/.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"          # remove CR se existir
        [[ "$line" =~ ^[[:space:]]*# ]] && continue  # pula comentários
        [[ -z "${line// }" ]] && continue             # pula linhas vazias
        [[ "$line" == *=* ]] && export "$line" 2>/dev/null || true
    done < "$SCRIPT_DIR/.env"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Iniciando limpeza automatiza..." >> "$LOG_FILE"
"$VENV" "$SCRIPT" >> "$LOG_FILE" 2>&1
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Concluído com sucesso." >> "$LOG_FILE"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERRO — exit code $STATUS" >> "$LOG_FILE"
fi

# Manter apenas os 10 logs mais recentes
ls -t "$LOG_DIR"/limpar_automatiza_*.log 2>/dev/null | tail -n +11 | xargs rm -f

exit $STATUS
