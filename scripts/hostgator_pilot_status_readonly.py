"""REMOTO read-only: memoria/eventos do cgroup do piloto, modelos carregados e runners."""
import json, os, subprocess, urllib.request
CG = '/sys/fs/cgroup/system.slice/ollama-piloto.service/'
def r(n): return open(CG + n).read().strip()
pid = subprocess.run(['systemctl', 'show', '-p', 'MainPID', '--value', 'ollama-piloto'], capture_output=True, text=True).stdout.strip()
procs = subprocess.run(['ps', '-o', 'pid,rss,etime,args', '--ppid', pid], capture_output=True, text=True).stdout
print(json.dumps({'cur_mib': int(r('memory.current')) >> 20, 'events': r('memory.events').replace('\n', ' '),
    'ps': json.load(urllib.request.urlopen('http://127.0.0.1:11434/api/ps', timeout=5)),
    'avail_mib': next(int(l.split()[1]) >> 10 for l in open('/proc/meminfo') if l.startswith('MemAvailable')),
    'load': os.getloadavg(), 'runners': [l[:110] for l in procs.splitlines()]}, ensure_ascii=False, indent=1))
