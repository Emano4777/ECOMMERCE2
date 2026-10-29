#!/bin/bash
# Monitor + watchdog do piloto Ollama no HostGator.
# Amostra a cada 2s: load, MemAvailable, swap, RAM/CPU do cgroup ollama-piloto
# e latencia dos apps locais (8001/8002/8003). PARA o ollama-piloto se:
#   MemAvailable < 3000MB | swap cresceu > 300MB | load1 > 3.6 por 3 amostras
#   | algum app local falhar/demorar > 5s em 2 checagens seguidas.
# Uso: watchdog.sh <segundos> <csv>
DUR=${1:-900}; OUT=${2:-/opt/ollama-piloto/logs/watchdog.csv}
CG=/sys/fs/cgroup/system.slice/ollama-piloto.service
swap0=$(awk '/SwapTotal/{t=$2}/SwapFree/{f=$2}END{print int((t-f)/1024)}' /proc/meminfo)
echo "ts,load1,mem_avail_mb,swap_used_mb,ollama_mem_mb,ollama_cpu_usec,app8001,app8002,app8003" > "$OUT"
hi=0; bad=0; end=$((SECONDS+DUR)); i=0
check() { curl -s -o /dev/null --max-time 5 -w '%{http_code}:%{time_total}' "http://127.0.0.1:$1/" || true; }
stop() { echo "WATCHDOG_STOP $(date +%T) motivo=$1" | tee -a "$OUT.events"; systemctl stop ollama-piloto; exit 2; }
while [ $SECONDS -lt $end ]; do
  l=$(cut -d' ' -f1 /proc/loadavg)
  ma=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
  sw=$(awk '/SwapTotal/{t=$2}/SwapFree/{f=$2}END{print int((t-f)/1024)}' /proc/meminfo)
  om=$(( $(cat $CG/memory.current 2>/dev/null || echo 0) / 1048576 ))
  oc=$(awk '/usage_usec/{print $2}' $CG/cpu.stat 2>/dev/null || echo 0)
  a1=-; a2=-; a3=-
  if [ $((i % 5)) -eq 0 ]; then
    a1=$(check 8001); a2=$(check 8002); a3=$(check 8003)
    if echo "$a1 $a2 $a3" | grep -qE '(^| )(000|5[0-9][0-9]):'; then bad=$((bad+1)); else bad=0; fi
    [ $bad -ge 2 ] && stop "app_local_falhou $a1 $a2 $a3"
  fi
  echo "$(date +%T),$l,$ma,$sw,$om,$oc,$a1,$a2,$a3" >> "$OUT"
  [ "$ma" -lt 3000 ] && stop "mem_avail=${ma}MB"
  [ $((sw - swap0)) -gt 300 ] && stop "swap_cresceu=$((sw - swap0))MB"
  if awk "BEGIN{exit !($l > 3.6)}"; then hi=$((hi+1)); else hi=0; fi
  [ $hi -ge 3 ] && stop "load1=$l"
  i=$((i+1)); sleep 2
done
echo "WATCHDOG_FIM_OK $(date +%T)" >> "$OUT.events"
