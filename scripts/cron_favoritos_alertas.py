"""Aciona com segurança a checagem de alertas de favoritos (preço caiu / voltou ao estoque) no e-commerce."""
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
        f"{BASE_URL}/api/cron/favoritos-alertas",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {CRON_SECRET}",
            "Content-Type": "application/json",
            "User-Agent": "Poupaqui-Favoritos-Alertas-Cron/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
    print(json.dumps(data, ensure_ascii=False))
    return 0 if data.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
