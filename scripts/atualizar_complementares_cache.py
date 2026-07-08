"""Preaquece complementares por produto/loja para checkout e detalhe.

Uso:
  python scripts/atualizar_complementares_cache.py
  python scripts/atualizar_complementares_cache.py --max-products 300 --max-ai 80
"""
import argparse
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import (  # noqa: E402
    db,
    _build_complement_cache_for_item,
    _ensure_complement_cache_schema,
)


def _load_products(max_products, only_cnpj=None):
    conn = db()
    cur = conn.cursor()
    params = []
    where_cnpj = ""
    if only_cnpj:
        where_cnpj = "AND ap.cnpjloja = %s"
        params.append(only_cnpj)
    params.append(int(max_products))
    cur.execute(
        f"""
        SELECT ap.cnpjloja, ap.ean, ap.nome,
               COALESCE(SUM(v.itens), 0) AS vendas_score,
               COALESCE(ap.estoque, 0) AS estoque
        FROM ecommerce_alpha_produtos ap
        LEFT JOIN vendageral v ON v.cnpj = ap.cnpjloja AND v.ean = ap.ean
        WHERE COALESCE(ap.inativo, false) = false
          AND COALESCE(ap.estoque, 0) > 0
          AND COALESCE(ap.ean, '') <> ''
          AND COALESCE(ap.nome, '') <> ''
          {where_cnpj}
        GROUP BY ap.cnpjloja, ap.ean, ap.nome, ap.estoque
        ORDER BY COALESCE(SUM(v.itens), 0) DESC, COALESCE(ap.estoque, 0) DESC
        LIMIT %s
        """,
        tuple(params),
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def main():
    parser = argparse.ArgumentParser(description="Atualiza cache de produtos complementares.")
    parser.add_argument("--max-products", type=int, default=350)
    parser.add_argument("--max-ai", type=int, default=80, help="Quantidade maxima de produtos sem regra local que podem chamar IA.")
    parser.add_argument("--cnpj", default="")
    args = parser.parse_args()

    _ensure_complement_cache_schema()
    rows = _load_products(args.max_products, args.cnpj.strip() or None)
    ok = 0
    empty = 0
    errors = 0
    ai_left = max(0, args.max_ai)
    started = time.time()
    for idx, row in enumerate(rows, 1):
        nome = row.get("nome") or ""
        use_ai = ai_left > 0
        try:
            eans = _build_complement_cache_for_item(row["cnpjloja"], row["ean"], nome, use_ai=use_ai, limit=16)
            if eans:
                ok += 1
            else:
                empty += 1
            if use_ai:
                ai_left -= 1
        except Exception as exc:
            errors += 1
            print(f"[{idx}/{len(rows)}] ERRO {row.get('ean')} {nome[:60]}: {exc}")
            continue
        if idx % 25 == 0:
            print(f"[{idx}/{len(rows)}] ok={ok} vazio={empty} erros={errors} elapsed={time.time()-started:.1f}s")
    print(f"Complementares atualizados: produtos={len(rows)} ok={ok} vazio={empty} erros={errors} tempo={time.time()-started:.1f}s")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
