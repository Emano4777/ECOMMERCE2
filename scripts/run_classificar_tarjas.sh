#!/bin/bash
set -euo pipefail
umask 077
BASE=/root/scripts/dns_ecommerce_web
STATE="$BASE/tarja_state"
mkdir -p "$STATE"
# O PostgreSQL tambem possui lock global: execucoes manuais nao se sobrepoem.
exec /usr/bin/flock -n "$STATE/cron.lock" /usr/bin/timeout 45m \
  "$BASE/venv/bin/python3.11" -u "$BASE/app/scripts/classificar_tarjas_ecommerce.py" \
  --env "$BASE/app/.env" --state-dir "$STATE" --search-provider tavily-serper \
  --online-limit 20 --online-daily-limit 20 --apply \
  >> "$STATE/cron.log" 2>&1
