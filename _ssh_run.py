"""Sobe script para o servidor e roda dry-run com verbose para ver resultado."""
import paramiko, select, time, io

HOST = "162.215.13.255"
PORT = 22
USER = "root"
PASS = "Poupaqui@13"

LOCAL  = r"C:\Users\Public\dns-ecommerce\scripts\buscar_imagens_cosmeticos.py"
REMOTE = "/root/scripts/pedidoeletronico/dns_ecommerce/scripts/buscar_imagens_cosmeticos.py"
WRAPPER = "/root/scripts/pedidoeletronico/_run_revalidar.sh"
ENV_DIR = "/root/scripts/pedidoeletronico"
VENV_PY = f"{ENV_DIR}/venv/bin/python"

def run(client, cmd, timeout=120, live=False):
    chan = client.get_transport().open_session()
    chan.settimeout(5)
    chan.exec_command(cmd)
    out = b""; err = b""
    deadline = time.time() + timeout
    while True:
        select.select([chan], [], [], 0.5)
        try:
            while chan.recv_ready(): out += chan.recv(8192)
        except: pass
        try:
            while chan.recv_stderr_ready(): err += chan.recv_stderr(4096)
        except: pass
        if chan.exit_status_ready() and not chan.recv_ready(): break
        if time.time() > deadline: break
    chan.close()
    def s(b): return b.decode("utf-8",errors="replace").encode("cp1252",errors="replace").decode("cp1252")
    if out:
        if live: print(s(out), end="", flush=True)
        else: print(s(out[:6000]))
    if err and err.strip(): print("[STDERR]", s(err[:600]))
    return s(out)

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, port=PORT, username=USER, password=PASS, timeout=15, banner_timeout=15)

# Upload
sftp = client.open_sftp()
sftp.put(LOCAL, REMOTE)
sftp.close()
print("[UPLOAD] OK\n")

# Dry-run com 10 EANs e verbose para ver as 5 fontes em ação
wrapper_script = f"""#!/bin/bash
set -a
while IFS= read -r line || [ -n "$line" ]; do
  line="${{line%$'\\r'}}";
  [[ "$line" =~ ^[[:space:]]*# ]] && continue
  [[ -z "${{line// }}" ]] && continue
  [[ "$line" == *=* ]] && export "$line" 2>/dev/null || true
done < {ENV_DIR}/.env
set +a
cd {ENV_DIR}/dns_ecommerce
export PYTHONIOENCODING=utf-8
exec {VENV_PY} scripts/buscar_imagens_cosmeticos.py "$@"
"""
sftp = client.open_sftp()
sftp.putfo(io.BytesIO(wrapper_script.encode()), "/root/scripts/pedidoeletronico/_run_cosmeticos.sh")
sftp.close()
run(client, "chmod +x /root/scripts/pedidoeletronico/_run_cosmeticos.sh")

print("="*60)
print("DRY-RUN 15 EANs de cosméticos com verbose")
print("="*60)
run(client,
    "/root/scripts/pedidoeletronico/_run_cosmeticos.sh --limite 15 -v 2>&1",
    timeout=300, live=True)

client.close()
print("\nOK.")
