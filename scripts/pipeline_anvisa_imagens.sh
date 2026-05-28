#!/bin/bash
# Pipeline: anvisa_sync (+ validar-claude) em paralelo com limpar_imagens_erradas
# Envia email ao terminar.
# Cron sugerido: 0 1 * * 0  (todo domingo 01:00) ou conforme necessidade.

SCRIPT_DIR="/root/scripts/pedidoeletronico/dns_ecommerce"
VENV="/root/scripts/pedidoeletronico/venv/bin/python"
LOG_DIR="/root/scripts/pedidoeletronico/logs"
TS=$(date +%Y%m%d_%H%M%S)
LOG_ANVISA="$LOG_DIR/anvisa_sync_$TS.log"
LOG_IMAGENS="$LOG_DIR/limpar_imagens_$TS.log"
EMAIL_TO="tencologia@Licencefarma.com.br"

mkdir -p "$LOG_DIR"

# Carregar .env (suporte a CRLF do Windows)
if [ -f "/root/scripts/pedidoeletronico/.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${line// }" ]] && continue
        [[ "$line" == *=* ]] && export "$line" 2>/dev/null || true
    done < "/root/scripts/pedidoeletronico/.env"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Pipeline anvisa + imagens iniciado ===" | tee -a "$LOG_ANVISA"

# ---- FASE 1: anvisa_sync + validar-claude ----
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Iniciando anvisa_sync.py --validar-claude ..." | tee -a "$LOG_ANVISA"
"$VENV" "$SCRIPT_DIR/anvisa_sync.py" --validar-claude >> "$LOG_ANVISA" 2>&1
STATUS_ANVISA=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] anvisa_sync finalizado (exit=$STATUS_ANVISA)" | tee -a "$LOG_ANVISA"

# ---- FASE 2: ambos em paralelo ----
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Iniciando limpar_imagens_erradas.py ..." | tee -a "$LOG_IMAGENS"
"$VENV" "$SCRIPT_DIR/scripts/limpar_imagens_erradas.py" >> "$LOG_IMAGENS" 2>&1 &
PID_IMAGENS=$!

wait $PID_IMAGENS
STATUS_IMAGENS=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] limpar_imagens finalizado (exit=$STATUS_IMAGENS)" | tee -a "$LOG_IMAGENS"

# ---- EMAIL ----
ANVISA_OK="OK"
IMAGENS_OK="OK"
[ $STATUS_ANVISA -ne 0 ] && ANVISA_OK="ERRO (exit=$STATUS_ANVISA)"
[ $STATUS_IMAGENS -ne 0 ] && IMAGENS_OK="ERRO (exit=$STATUS_IMAGENS)"

LAST_ANVISA=$(tail -20 "$LOG_ANVISA" 2>/dev/null)
LAST_IMAGENS=$(tail -10 "$LOG_IMAGENS" 2>/dev/null)

/usr/sbin/sendmail -t << EOF
To: $EMAIL_TO
From: servidor@licencefarma.com.br
Subject: [Servidor] Pipeline ANVISA + Imagens concluido - $(date '+%d/%m/%Y %H:%M')
Content-Type: text/plain; charset=UTF-8

Pipeline finalizado em $(date '+%d/%m/%Y às %H:%M:%S')

STATUS
------
anvisa_sync.py --validar-claude : $ANVISA_OK
limpar_imagens_erradas.py       : $IMAGENS_OK

--- Últimas linhas do log ANVISA ---
$LAST_ANVISA

--- Últimas linhas do log IMAGENS ---
$LAST_IMAGENS

Logs completos em:
  $LOG_ANVISA
  $LOG_IMAGENS
EOF

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Email enviado para $EMAIL_TO"

# Manter só os 10 logs mais recentes de cada tipo
ls -t "$LOG_DIR"/anvisa_sync_*.log  2>/dev/null | tail -n +11 | xargs rm -f
ls -t "$LOG_DIR"/limpar_imagens_*.log 2>/dev/null | tail -n +11 | xargs rm -f

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Pipeline concluido ==="
