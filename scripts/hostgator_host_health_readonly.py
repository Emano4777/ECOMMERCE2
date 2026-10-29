"""REMOTO read-only: saude do host apos testes (load, memoria, units falhas, top CPU fora do piloto)."""
import json, os, subprocess as sp
def sh(c): return sp.run(c, shell=True, capture_output=True, text=True, timeout=15).stdout.strip()
print(json.dumps({
 'load': os.getloadavg(),
 'mem': sh('free -m | sed -n 2p'),
 'failed_units': sh('systemctl --failed --no-legend --plain | cut -d" " -f1'),
 'pilot_cg_mib': int(open('/sys/fs/cgroup/system.slice/ollama-piloto.service/memory.current').read()) >> 20,
 'pilot_events': open('/sys/fs/cgroup/system.slice/ollama-piloto.service/memory.events').read().replace('\n', ' '),
 'pilot_ps': sh('curl -s 127.0.0.1:11434/api/ps'),
 'top_cpu': sh('ps -eo pcpu,rss,comm --sort=-pcpu | head -8'),
 'host_oom_dmesg_last': sh('dmesg -T 2>/dev/null | grep -iE "out of memory|oom-kill" | tail -3'),
}, ensure_ascii=False, indent=1))
