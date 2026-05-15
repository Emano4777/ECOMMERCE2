import argparse

from app import _fill_one_catalog_image, db


VISIBLE_FILTER_ALPHA = """
    AND (
      dns.ean_norm IS NOT NULL
      OR mi.cloudinary_url IS NOT NULL
      OR NULLIF(TRIM(m.imagem), '') IS NOT NULL
      OR e.descricao ILIKE ANY(ARRAY[
           '%%anasol%%','%%vit natu%%','%%vitnatu%%',
           '%%pronabol%%','%%ricosol%%','%%unispray%%',
           '%%goodvit%%'
         ])
    )
"""

VISIBLE_FILTER_AUTO = """
    AND (
      dns.ean_norm IS NOT NULL
      OR mi.cloudinary_url IS NOT NULL
      OR NULLIF(TRIM(m.imagem), '') IS NOT NULL
      OR ae.descricao_produto ILIKE ANY(ARRAY[
           '%%anasol%%','%%vit natu%%','%%vitnatu%%',
           '%%pronabol%%','%%ricosol%%','%%unispray%%',
           '%%goodvit%%'
         ])
    )
"""


def find_missing(limit):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        f"""
        WITH catalogo AS (
            SELECT e.cnpj AS cnpjloja, e.barras AS ean, e.descricao AS nome,
                   COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
            FROM estoque e
            LEFT JOIN omie_estoque_dns dns ON dns.ean_norm = COALESCE(e.barras_norm, e.barras)
            LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = e.cnpj AND epi.ean = e.barras
            WHERE e.estoque > 0
              AND NOT EXISTS (
                  SELECT 1 FROM ecommerce_catalogo_oculto co
                  WHERE co.cnpjloja = e.cnpj AND co.ean = e.barras
              )
              {VISIBLE_FILTER_ALPHA}

            UNION ALL

            SELECT ae.cnpj_loja AS cnpjloja, ae.ean, ae.descricao_produto AS nome,
                   COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
            FROM automatiza_estoque ae
            LEFT JOIN omie_estoque_dns dns ON dns.ean_norm = ae.ean
            LEFT JOIN medicamentos m ON m.barra_norm = ae.ean
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = ae.cnpj_loja AND epi.ean = ae.ean
            WHERE ae.quantidade_estoque > 0
              AND NOT EXISTS (
                  SELECT 1 FROM ecommerce_catalogo_oculto co
                  WHERE co.cnpjloja = ae.cnpj_loja AND co.ean = ae.ean
              )
              {VISIBLE_FILTER_AUTO}
        )
        SELECT MIN(cnpjloja) AS cnpjloja, ean, MIN(nome) AS nome
        FROM catalogo
        WHERE imagem IS NULL OR TRIM(imagem) = ''
        GROUP BY ean
        ORDER BY nome
        LIMIT %s
        """,
        (limit,),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def main():
    parser = argparse.ArgumentParser(description="Preenche imagens faltantes do catalogo por EAN exato.")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    rows = find_missing(args.limit)
    print(f"Encontrados {len(rows)} item(ns) sem imagem para tentar preencher.")
    ok = 0
    for i, row in enumerate(rows, 1):
        cnpj = row["cnpjloja"]
        ean = row["ean"]
        nome = row["nome"]
        image_url = _fill_one_catalog_image(cnpj, ean)
        if image_url:
            ok += 1
            print(f"[{i}/{len(rows)}] OK {ean} - {nome}")
        else:
            print(f"[{i}/{len(rows)}] SEM FONTE SEGURA {ean} - {nome}")
    print(f"Finalizado: {ok}/{len(rows)} preenchidos.")


if __name__ == "__main__":
    main()
