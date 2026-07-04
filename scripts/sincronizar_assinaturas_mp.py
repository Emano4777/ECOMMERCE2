#!/usr/bin/env python3
"""Varre assinaturas recorrentes ativas e resincroniza o status real no
Mercado Pago (conta central do admin).

Detecta automaticamente quando alguem "parou de pagar": o MP pausa/cancela
o preapproval depois de cobrancas recorrentes recusadas, e essa sincronizacao
propaga isso pro nosso banco — cortando os beneficios de assinante na hora
seguinte em que o consumidor (ou o carrinho) checar a assinatura.

Roda por webhook em tempo real tambem (ver mercado_pago_webhook em app.py),
mas esse script serve de rede de seguranca caso algum webhook seja perdido —
rodar periodicamente (ex: a cada poucas horas) via agendador do servidor.

Uso:
    python scripts/sincronizar_assinaturas_mp.py
    python scripts/sincronizar_assinaturas_mp.py --dry-run
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import _admin_mp_config, _sincronizar_assinatura_preapproval, db  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Resincroniza assinaturas recorrentes com o Mercado Pago")
    parser.add_argument("--dry-run", action="store_true", help="So lista, nao chama o MP nem grava nada")
    args = parser.parse_args()

    token = _admin_mp_config().get("mp_access_token") or ""
    if not token and not args.dry_run:
        print("ERRO: token Mercado Pago do admin nao configurado (/painel/admin/config).")
        return 1

    conn = db()
    cur = conn.cursor()
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
    cur.close()

    print(f"{len(rows)} assinatura(s) recorrente(s) ativa(s) para checar.")
    cancelados = 0
    for r in rows:
        if args.dry_run:
            print(f"  [dry-run] assinatura {r['id']} — {r['razao']} (preapproval {r['mp_preapproval_id']})")
            continue
        status = _sincronizar_assinatura_preapproval(r["mp_preapproval_id"])
        marcador = ""
        if status in {"cancelled", "paused"}:
            cancelados += 1
            marcador = "  -> CANCELADO (pagamento recorrente falhou)"
        print(f"  assinatura {r['id']} — {r['razao']}: status MP = {status}{marcador}")

    if not args.dry_run:
        print(f"\nConcluido: {cancelados} assinatura(s) cancelada(s) por falha de pagamento.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
