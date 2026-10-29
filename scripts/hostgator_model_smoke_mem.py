"""REMOTO: carrega 1 modelo no piloto com 1 pergunta curta e mede RAM/CPU do cgroup. argv: modelo"""
import json, os, sys, threading, time, urllib.request
from pathlib import Path
CG = Path('/sys/fs/cgroup/system.slice/ollama-piloto.service'); M = sys.argv[1]
def rd(n): return (CG / n).read_text().strip()
def avail():
    return next(int(l.split()[1]) >> 10 for l in open('/proc/meminfo') if l.startswith('MemAvailable'))
ev0 = rd('memory.events'); peak = [0, 10 ** 9]; stop = []
def watch():
    while not stop:
        peak[0] = max(peak[0], int(rd('memory.current')) >> 20); peak[1] = min(peak[1], avail()); time.sleep(0.5)
threading.Thread(target=watch, daemon=True).start()
body = {'model': M, 'stream': False, 'think': False, 'keep_alive': '5m',
        'messages': [{'role': 'user', 'content': 'Responda em uma frase: tudo bem?'}],
        'options': {'num_ctx': 2048, 'num_predict': 40, 'num_thread': 3}}
t = time.monotonic()
try:
    o = json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:11434/api/chat',
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}), timeout=180)); err = None
except Exception as e: o = {}; err = type(e).__name__
stop.append(1); time.sleep(0.6)
ps = json.load(urllib.request.urlopen('http://127.0.0.1:11434/api/ps', timeout=5))
print(json.dumps({'model': M, 'error': err, 'wall_s': round(time.monotonic() - t, 1),
    'load_s': round((o.get('load_duration') or 0) / 1e9, 1), 'answer': (o.get('message') or {}).get('content', '')[:200],
    'cg_peak_mib': peak[0], 'host_min_avail_mib': peak[1], 'ps': [(m['name'], m.get('size') >> 20) for m in ps['models']],
    'events_before': ev0.replace('\n', ' '), 'events_after': rd('memory.events').replace('\n', ' '),
    'active': os.popen('systemctl is-active ollama-piloto').read().strip()}, ensure_ascii=False))
