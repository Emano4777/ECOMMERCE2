"""REMOTO (root, FORA do cgroup do piloto): importa um modelo da registry oficial do Ollama
para o models dir do ollama-piloto, sem abrir rede da unit (sem-rede.conf permanece).

Uso via helper: scripts/_hg_remote.py scripts/hostgator_import_model_blobs.py <repo> <tag> [--apply]
Sem --apply: so mostra manifesto/tamanhos. Com --apply: baixa, verifica SHA256 e tamanho de cada
blob, grava atomico, chown para o dono do models dir, grava manifesto por ultimo.
Guardas: MemAvailable >= 8 GiB, load1 <= 2.5, disco livre >= 20 GiB, banda limitada a 25 MB/s.
"""
import hashlib, json, os, shutil, sys, time, urllib.request
from pathlib import Path

REPO, TAG = sys.argv[1], sys.argv[2]
APPLY = '--apply' in sys.argv
REG = 'https://registry.ollama.ai'
MODELS = Path('/var/lib/ollama-piloto/models').resolve()
RATE = 25 * 1024 * 1024
G = 1024 ** 3
ACCEPT = 'application/vnd.docker.distribution.manifest.v2+json'

def emit(ev, **k): print(json.dumps(dict(event=ev, **k)), flush=True)
def guard():
    mem = {l.split(':')[0]: int(l.split()[1]) * 1024 for l in open('/proc/meminfo')}
    load = os.getloadavg()[0]
    free = shutil.disk_usage(MODELS).free
    if mem['MemAvailable'] < 8 * G or load > 2.5 or free < 20 * G:
        emit('guard_stop', mem_avail=mem['MemAvailable'], load=load, disk_free=free); sys.exit(2)
    return dict(mem_avail_mib=mem['MemAvailable'] >> 20, load=round(load, 2))

st = MODELS.stat(); UID, GID = st.st_uid, st.st_gid
req = urllib.request.Request(f'{REG}/v2/library/{REPO}/manifests/{TAG}', headers={'Accept': ACCEPT})
raw = urllib.request.urlopen(req, timeout=20).read()
man = json.loads(raw)
layers = [man['config']] + man['layers']
total = sum(l['size'] for l in layers)
emit('manifest', repo=REPO, tag=TAG, total_bytes=total,
     layers=[dict(mediaType=l['mediaType'].split('.')[-1], size=l['size'], digest=l['digest'][:19]) for l in layers])
if not APPLY:
    emit('dry_run_only', resources=guard()); sys.exit(0)

os.nice(10)
blobs = MODELS / 'blobs'
for l in layers:
    algo, hexd = l['digest'].split(':'); assert algo == 'sha256' and len(hexd) == 64
    dest = blobs / f'sha256-{hexd}'
    if dest.exists() and dest.stat().st_size == l['size']:
        h = hashlib.sha256()
        with open(dest, 'rb') as f:
            for c in iter(lambda: f.read(1 << 20), b''): h.update(c)
        if h.hexdigest() == hexd: emit('blob_present_verified', digest=hexd[:12]); continue
    tmp = blobs / f'.import-{hexd}.partial'
    guard(); t0 = time.monotonic(); n = 0; h = hashlib.sha256(); last = 0
    r = urllib.request.urlopen(urllib.request.Request(f'{REG}/v2/library/{REPO}/blobs/{l["digest"]}'), timeout=30)
    with open(tmp, 'wb') as f:
        while True:
            c = r.read(1 << 20)
            if not c: break
            f.write(c); h.update(c); n += len(c)
            el = time.monotonic() - t0
            if n / RATE > el: time.sleep(n / RATE - el)
            if time.monotonic() - last > 15:
                last = time.monotonic(); emit('progress', digest=hexd[:12], done=n, size=l['size'], res=guard())
        f.flush(); os.fsync(f.fileno())
    if n != l['size'] or h.hexdigest() != hexd:
        tmp.unlink(); emit('integrity_fail', digest=hexd[:12], got=n); sys.exit(3)
    os.chmod(tmp, 0o644); os.chown(tmp, UID, GID); os.replace(tmp, dest)
    emit('blob_ok', digest=hexd[:12], bytes=n, seconds=round(time.monotonic() - t0, 1))

mdir = MODELS / 'manifests' / 'registry.ollama.ai' / 'library' / REPO
mdir.mkdir(parents=True, exist_ok=True)
for p in [mdir, mdir.parent, mdir.parent.parent]:
    if p.stat().st_uid == 0: os.chown(p, UID, GID)
tmpm = mdir / f'.{TAG}.partial'
tmpm.write_bytes(raw); os.chmod(tmpm, 0o644); os.chown(tmpm, UID, GID); os.replace(tmpm, mdir / TAG)
emit('manifest_written', sha256=hashlib.sha256(raw).hexdigest()[:12])
tags = json.load(urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=10))
emit('tags', names=[m['name'] for m in tags['models']], resources=guard())
