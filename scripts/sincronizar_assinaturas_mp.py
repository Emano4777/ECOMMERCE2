#!/usr/bin/env python3
"""Varre assinaturas recorrentes ativas e resincroniza o status real no
Mercado Pago (conta central do admin).

Detecta automaticamente quando alguem "parou de pagar": o MP pausa/cancela
o preapproval depois de cobrancas recorrentes recusadas, e essa sincronizacao
propaga isso pro nosso banco — cortando os beneficios de assinante na hora
seguinte em que o consumidor (ou o carrinho) checar a assinatura.

Roda por webhook em tempo real tambem (ver mercado_pago_webhook em app.py),
mas esse script serve de rede de seguranca caso algum webhook seja perdido —
pensado pra rodar sozinho (sem Flask, sem o resto do app.py) via cron.

Uso:
    python scripts/sincronizar_assinaturas_mp.py
    python scripts/sincronizar_assinaturas_mp.py --dry-run
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DATABASE_URL = os.environ["DATABASE_URL"]


def _mp_request(access_token, path, method="GET", timeout=20):
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    req = urllib.request.Request(f"https://api.mercadopago.com{path}", headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _admin_mp_token(cur):
    cur.execute("SELECT mp_access_token FROM ecommerce_config_admin WHERE id=1")
    row = cur.fetchone()
    return (row or {}).get("mp_access_token") or ""


def _sincronizar_preapproval(cur, assinatura_id, preapproval_id, token):
    """Espelha app.py:_sincronizar_assinatura_preapproval, sem depender do Flask."""
    try:
        data = _mp_request(token, f"/preapproval/{preapproval_id}")
    except Exception as exc:
        return None, str(exc)
    status = (data.get("status") or "").lower()
    if status in ("authorized", "active"):
        cur.execute(
            """UPDATE ecommerce_assinantes
               SET status='ativo', pagamento_status='aprovado',
                   data_inicio=COALESCE(data_inicio, NOW()),
                   data_fim=NULL, assinatura_recorrente=TRUE
               WHERE id=%s""",
            (assinatura_id,),
        )
    elif status in ("cancelled", "paused"):
        cur.execute(
            "UPDATE ecommerce_assinantes SET status='cancelado', data_fim=NOW() WHERE id=%s",
            (assinatura_id,),
        )
    return status, None


def main():
    parser = argparse.ArgumentParser(description="Resincroniza assinaturas recorrentes com o Mercado Pago")
    parser.add_argument("--dry-run", action="store_true", help="So lista, nao chama o MP nem grava nada")
    args = parser.parse_args()

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()

    token = _admin_mp_token(cur)
    if not token and not args.dry_run:
        print("ERRO: token Mercado Pago do admin nao configurado (/painel/admin/config).")
        return 1

    cur.execute("""
        SELECT a.id, a.mp_preapproval_id, a.cnpjloja, u.razao
        FROM ecommerce_assinantes a
        JOIN users u ON u.cnpjloja = a.cnpjloja
        WHERE a.assinatura_recorrente = TRUE
          AND a.status = 'ativo'
          AND a.mp_preapproval_id IS NOT NULL
        ORDER BY a.id
    """)
    rows = cur.fetchall()

    print(f"{len(rows)} assinatura(s) recorrente(s) ativa(s) para checar.")
    cancelados = 0
    for r in rows:
        if args.dry_run:
            print(f"  [dry-run] assinatura {r['id']} - {r['razao']} (preapproval {r['mp_preapproval_id']})")
            continue
        status, erro = _sincronizar_preapproval(cur, r["id"], r["mp_preapproval_id"], token)
        if erro:
            print(f"  assinatura {r['id']} - {r['razao']}: erro ao consultar MP ({erro})")
            continue
        marcador = ""
        if status in ("cancelled", "paused"):
            cancelados += 1
            marcador = "  -> CANCELADO (pagamento recorrente falhou)"
        print(f"  assinatura {r['id']} - {r['razao']}: status MP = {status}{marcador}")

    if not args.dry_run:
        conn.commit()
        print(f"\nConcluido: {cancelados} assinatura(s) cancelada(s) por falha de pagamento.")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
