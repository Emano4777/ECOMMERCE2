"""LOCAL: dispara hostgator_bench_v3_remote.py no HostGator e salva HOSTGATOR_BENCH_V3_<tag>.json.
argv: modelos (virgula) repeticoes tag
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hg_remote import run_remote, ROOT

models, reps, tag = sys.argv[1], sys.argv[2], sys.argv[3]
src = (ROOT / 'scripts' / 'hostgator_bench_v3_remote.py').read_text(encoding='utf-8')
rc, out = run_remote(src, f'{models} {reps}', timeout=1800, stream=True)
rows = []
for line in out.splitlines():
    try: rows.append(json.loads(line))
    except ValueError: pass
(ROOT / f'HOSTGATOR_BENCH_V3_{tag}.json').write_text(json.dumps({'remote_exit': rc, 'rows': rows},
    ensure_ascii=False, indent=1), encoding='utf-8')
print('\nSALVO', tag, 'rc', rc, 'linhas', len(rows))
