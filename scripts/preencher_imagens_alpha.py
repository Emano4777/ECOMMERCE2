#!/usr/bin/env python3
"""Preenche imagens faltantes dos produtos do catalogo Alpha (fluxo novo).

Reaproveita exatamente a mesma logica que ja roda no Precificador:
  - get_dns_products(...)          -> monta a lista de produtos da loja
  - _split_catalog_image_status()  -> separa quem esta "sem imagem / nao publicado"
  - _fill_one_catalog_image(...)   -> busca imagem segura (ignora imagem de
                                       farmacia concorrente, banner/promocao,
                                       imagem generica nao confiavel etc.)

Ou seja: processa exatamente os EANs que aparecem no card "Produtos nao
publicados por falta de imagem" do Precificador.

Uso:
    python scripts/preencher_imagens_alpha.py --cnpjloja 54185432000143
    python scripts/preencher_imagens_alpha.py --cnpjloja 54185432000143 --limit 50
    python scripts/preencher_imagens_alpha.py --todas-lojas   # roda para todas as lojas com catalogo Alpha
    python scripts/preencher_imagens_alpha.py --cnpjloja 54185432000143 --dry-run
"""

import argparse
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

signal.signal(signal.SIGINT, lambda *_: (print("\n\nInterrompido. Encerrando..."), os._exit(0)))

from app import (  # noqa: E402
    _fill_one_catalog_image,
    _split_catalog_image_status,
    db,
    get_dns_products,
)


def _lojas_alpha(cur):
    cur.execute(
        """
        SELECT DISTINCT ap.cnpjloja
        FROM ecommerce_alpha_produtos ap
        JOIN users u ON u.cnpjloja = ap.cnpjloja
        WHERE COALESCE(ap.inativo, false) = false
          AND COALESCE(ap.estoque, 0) > 0
          AND u.is_admin = FALSE
        ORDER BY 1
        """
    )
    return [r["cnpjloja"] for r in cur.fetchall()]


def preencher_loja(cnpjloja, limit, dry_run):
    produtos = get_dns_products(
        cnpjloja,
        skip_image_filter=True,
        dedupe_display=False,
        schedule_fill=False,
        persist_image_updates=False,
    )
    _publicados, faltando = _split_catalog_image_status(produtos)

    if limit:
        faltando = faltando[:limit]

    print(f"\n=== Loja {cnpjloja}: {len(faltando)} produto(s) sem imagem publicavel ===")

    ok = 0
    sem_fonte = 0
    for i, p in enumerate(faltando, 1):
        ean = (p.get("ean") or "").strip()
        nome = p.get("nome") or ""
        if not ean:
            continue
        if dry_run:
            print(f"[{i}/{len(faltando)}] (dry-run) {ean} - {nome[:60]}")
            continue
        image_url = _fill_one_catalog_image(cnpjloja, ean, nome)
        if image_url:
            ok += 1
            print(f"[{i}/{len(faltando)}] OK  {ean} - {nome[:60]}")
        else:
            sem_fonte += 1
            print(f"[{i}/{len(faltando)}] SEM FONTE SEGURA  {ean} - {nome[:60]}")
        time.sleep(0.2)  # respeita rate-limit das fontes externas (serper/etc)

    print(f"--- Loja {cnpjloja}: {ok} preenchidos, {sem_fonte} sem fonte segura ---")
    return ok, sem_fonte


def main():
    parser = argparse.ArgumentParser(description="Preenche imagens faltantes do catalogo Alpha")
    parser.add_argument("--cnpjloja", type=str, default=None, help="CNPJ da loja (ver users.cnpjloja)")
    parser.add_argument("--todas-lojas", action="store_true", help="Roda para todas as lojas com catalogo Alpha ativo")
    parser.add_argument("--limit", type=int, default=None, help="Maximo de produtos a tentar por loja")
    parser.add_argument("--dry-run", action="store_true", help="So lista, nao busca/grava imagem")
    args = parser.parse_args()

    if not args.cnpjloja and not args.todas_lojas:
        parser.error("informe --cnpjloja <cnpj> ou --todas-lojas")

    conn = db()
    cur = conn.cursor()

    if args.todas_lojas:
        lojas = _lojas_alpha(cur)
        print(f"{len(lojas)} loja(s) com catalogo Alpha ativo.")
    else:
        lojas = [args.cnpjloja]
    cur.close()

    total_ok = total_sem_fonte = 0
    for cnpjloja in lojas:
        ok, sem_fonte = preencher_loja(cnpjloja, args.limit, args.dry_run)
        total_ok += ok
        total_sem_fonte += sem_fonte

    print(f"\n===== TOTAL: {total_ok} preenchidos, {total_sem_fonte} sem fonte segura =====")


if __name__ == "__main__":
    main()
