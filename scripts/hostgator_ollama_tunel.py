"""LOCAL: tunel SSH 127.0.0.1:11435 (este PC) -> 127.0.0.1:11434 do ollama-piloto na HostGator.

Permite o worker local usar o modelo do piloto sem expor a API: escuta so em loopback
aqui e o destino e o loopback do servidor (a unit continua com sem-rede.conf).
Reconecta sozinho se o SSH cair. Credenciais lidas por AST em _hg_remote (nunca impressas).
Uso: python scripts/hostgator_ollama_tunel.py [porta_local]
Worker: OLLAMA_URL=http://127.0.0.1:11435 POUPAQUI_OLLAMA_MODEL=gemma4:e2b
"""
import select
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hg_remote import _auth  # noqa: E402

LOCAL_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 11435
REMOTE = ('127.0.0.1', 11434)
_state = {'transport': None}
_lock = threading.Lock()


def log(msg):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), msg, flush=True)


def transport():
    import paramiko
    with _lock:
        t = _state['transport']
        if t is not None and t.is_active():
            return t
        a = _auth()
        c = paramiko.SSHClient()
        c.load_system_host_keys()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(a['HOST'], port=a['PORT'], username=a['USER'], password=a['PASS'], timeout=15,
                      banner_timeout=15, allow_agent=False, look_for_keys=False)
        finally:
            a.clear()
        t = c.get_transport()
        t.set_keepalive(30)
        _state['transport'] = t
        log('ssh conectado')
        return t


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            ch = transport().open_channel('direct-tcpip', REMOTE, self.request.getpeername())
        except Exception as exc:  # nunca stringificar (pode conter host/conta)
            log(f'canal falhou: {type(exc).__name__}')
            with _lock:
                _state['transport'] = None
            return
        try:
            while True:
                r, _, _ = select.select([self.request, ch], [], [], 300)
                if not r:
                    break
                if self.request in r:
                    data = self.request.recv(65536)
                    if not data:
                        break
                    ch.sendall(data)
                if ch in r:
                    data = ch.recv(65536)
                    if not data:
                        break
                    self.request.sendall(data)
        except OSError:
            pass
        finally:
            ch.close()


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


if __name__ == '__main__':
    srv = Server(('127.0.0.1', LOCAL_PORT), Handler)
    log(f'tunel 127.0.0.1:{LOCAL_PORT} -> piloto 127.0.0.1:11434')
    srv.serve_forever()
