"""Helper: roda codigo Python no HostGator via Paramiko sem imprimir credenciais.

Uso: python scripts/_hg_remote.py arquivo_remoto.py [args...]
Le HOST/PORT/USER/PASS de _ssh_run.py via AST (nunca exibe). Saida = stdout remoto.
"""
import ast
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _auth():
    a = {}
    for n in ast.parse((ROOT / '_ssh_run.py').read_text(encoding='utf-8-sig')).body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id in ('HOST', 'PORT', 'USER', 'PASS'):
                    a[t.id] = ast.literal_eval(n.value)
    return a


def run_remote(code, args='', timeout=600, stream=True):
    import paramiko
    a = _auth()
    c = paramiko.SSHClient()
    c.load_system_host_keys()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    out = b''
    try:
        c.connect(a['HOST'], port=a['PORT'], username=a['USER'], password=a['PASS'], timeout=15,
                  banner_timeout=15, allow_agent=False, look_for_keys=False)
        i, o, e = c.exec_command('python3 -u - ' + args, timeout=timeout)
        i.write(code)
        i.flush()
        i.channel.shutdown_write()
        ch = o.channel
        deadline = time.monotonic() + timeout
        while True:
            if ch.recv_ready():
                b = ch.recv(32768)
                out += b
                if stream:
                    sys.stdout.write(b.decode('utf-8', 'replace'))
                    sys.stdout.flush()
            if ch.recv_stderr_ready():
                err = ch.recv_stderr(32768).decode('utf-8', 'replace')
                if stream:
                    sys.stdout.write('[stderr] ' + err[-1500:])
            if ch.exit_status_ready() and not ch.recv_ready() and not ch.recv_stderr_ready():
                code_ = ch.recv_exit_status()
                break
            if time.monotonic() > deadline:
                code_ = -1
                print('[local_deadline]')
                break
            time.sleep(0.1)
        return code_, out.decode('utf-8', 'replace')
    except Exception as exc:  # nunca stringificar (pode conter host/conta)
        print('[local_error]', type(exc).__name__)
        return -2, out.decode('utf-8', 'replace')
    finally:
        c.close()
        a.clear()


if __name__ == '__main__':
    src = Path(sys.argv[1]).read_text(encoding='utf-8')
    rc, _ = run_remote(src, ' '.join(sys.argv[2:]))
    print('\n[remote_exit]', rc)
