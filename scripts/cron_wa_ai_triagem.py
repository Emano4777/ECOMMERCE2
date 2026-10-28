"""Aciona o worker do atendimento WhatsApp IA (rodar a cada minuto).

O worker so envia mensagem se o modo efetivo permitir (off/shadow/test/live,
ver poupaqui_wa_atendimento.py). Crontab sugerido no servidor:
  * * * * * cd /root/scripts/dns_ecommerce_web/app && CRON_SECRET=... python3 scripts/cron_wa_ai_triagem.py >> /var/log/wa_ia.log 2>&1
"""
import json
import os
import sys
import urllib.request


BASE_URL = os.getenv("ECOMMERCE_URL", "https://drogariaspoupaqui.com.br").rstrip("/")
CRON_SECRET = os.getenv("CRON_SECRET", "")


def main():
    if not CRON_SECRET:
        raise SystemExit("CRON_SECRET não configurado")
    req = urllib.request.Request(
        f"{BASE_URL}/api/cron/wa-ia-atendimento",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {CRON_SECRET}",
            "Content-Type": "application/json",
            "User-Agent": "Poupaqui-WA-IA-Cron/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=110) as response:
        data = json.loads(response.read().decode("utf-8"))
    print(json.dumps(data, ensure_ascii=False))
    return 0 if data.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
