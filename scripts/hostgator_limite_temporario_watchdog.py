"""REMOTO (root): aumenta SOMENTE memoria do ollama-piloto em runtime, por tempo limitado, com vigia.
argv: start <MemoryHigh> <MemoryMax> <minutos> | stop | status
start: checa folga do host (MemAvailable >= 11 GiB, load1 <= 1.5), aplica limites --runtime e deixa
  um vigia em segundo plano (setsid) que, se MemAvailable < 4.5 GiB ou load1 > 6, descarrega os modelos
  e reverte. Ao fim do prazo, ou com `stop`, sempre reverte para MemoryHigh=7G MemoryMax=7680M (os persistidos).
CPU (CPUQuota=250%, AllowedCPUs=1-3, CPUWeight=20) nao e alterada. Sem restart nem daemon-reload.
"""
import json, os, subprocess as sp, sys, time, urllib.request
UNIT = 'ollama-piloto.service'
STOP = '/run/ollama-piloto-limite.stop'
LOG = '/run/ollama-piloto-limite.log'
BASE = ('7G', '7680M')   # mesmos valores persistidos em system.control
G = 1024 ** 3

def avail():
    return next(int(l.split()[1]) * 1024 for l in open('/proc/meminfo') if l.startswith('MemAvailable'))
def setmem(high, mx):
    sp.run(['systemctl', 'set-property', '--runtime', UNIT, f'MemoryHigh={high}', f'MemoryMax={mx}'], check=True)
def show():
    return sp.run(['systemctl', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryHigh', '-p', 'MemoryMax',
                   '-p', 'CPUQuotaPerSecUSec', '-p', 'AllowedCPUs', '-p', 'CPUWeight'],
                  capture_output=True, text=True).stdout.split()
def unload_all():
    try:
        ps = json.load(urllib.request.urlopen('http://127.0.0.1:11434/api/ps', timeout=5))
        for m in ps['models']:
            urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:11434/api/generate',
                data=json.dumps({'model': m['name'], 'keep_alive': 0}).encode(),
                headers={'Content-Type': 'application/json'}), timeout=30).read()
    except Exception as e:
        return type(e).__name__
def log(**k):
    with open(LOG, 'a') as f: f.write(json.dumps(dict(t=time.strftime('%H:%M:%S'), **k)) + '\n')

def watchdog(minutes):
    end = time.monotonic() + minutes * 60
    min_av, max_ld = 10 ** 15, 0.0
    try:
        while time.monotonic() < end and not os.path.exists(STOP):
            av, ld = avail(), os.getloadavg()[0]
            min_av, max_ld = min(min_av, av), max(max_ld, ld)
            if av < 4.5 * G or ld > 6:
                log(ev='abort', mem_avail_mib=av >> 20, load=ld, unload=unload_all())
                break
            time.sleep(0.5)
    finally:
        # descarregar ANTES de baixar o teto: reduzir MemoryMax abaixo do uso atual dispara
        # o OOM do cgroup e derruba o servico (ocorreu em 29/10/2026 09:43 com gemma4:e4b)
        unload_all()
        for _ in range(60):
            try:
                cur = int(open('/sys/fs/cgroup/system.slice/ollama-piloto.service/memory.current').read())
            except OSError:
                break
            if cur < 5 * G: break
            time.sleep(0.5)
        setmem(*BASE)
        log(ev='reverted', min_mem_avail_mib=min_av >> 20, max_load=round(max_ld, 2), state=show())
        try: os.remove(STOP)
        except FileNotFoundError: pass

cmd = sys.argv[1]
if cmd == 'start':
    high, mx, minutes = sys.argv[2], sys.argv[3], float(sys.argv[4])
    av, ld = avail(), os.getloadavg()[0]
    if av < 11 * G or ld > 1.5:
        print(json.dumps({'refused': True, 'mem_avail_mib': av >> 20, 'load': ld})); sys.exit(2)
    open(LOG, 'w').close()
    setmem(high, mx)
    log(ev='applied', high=high, max=mx, minutes=minutes, mem_avail_mib=av >> 20, load=ld)
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            for fd in (0, 1, 2): os.close(fd)
            watchdog(minutes); os._exit(0)
        os._exit(0)
    time.sleep(0.5)
    print(json.dumps({'applied': show(), 'mem_avail_mib': av >> 20, 'load': ld}))
elif cmd == 'stop':
    open(STOP, 'w').close()
    for _ in range(20):
        if not os.path.exists(STOP): break
        time.sleep(0.5)
    print(open(LOG).read())
else:
    print(open(LOG).read() if os.path.exists(LOG) else 'sem log', show())
