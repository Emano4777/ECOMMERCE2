import argparse

from app import (
    _anvisa_chave,
    _detectar_tarja,
    _generic_placeholder_for,
    _image_has_other_pharmacy_text,
    _image_looks_non_product,
    _looks_like_other_pharmacy_brand,
    _upsert_catalog_image_all_stores,
    db,
)


def _clean_url(url):
    url = (url or "").strip()
    return url if url.startswith(("http://", "https://")) else None


def _is_clean_image(url):
    if not _clean_url(url):
        return False
    if _looks_like_other_pharmacy_brand(url):
        return False
    if _image_has_other_pharmacy_text(url):
        return False
    if _image_looks_non_product(url):
        return False
    return True


def _catalog_missing_or_suspect(limit):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        WITH catalogo AS (
            SELECT e.cnpj AS cnpjloja, e.barras AS ean, e.descricao AS nome,
                   COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
            FROM estoque e
            LEFT JOIN omie_estoque_dns dns ON dns.ean_norm = COALESCE(e.barras_norm, e.barras)
            LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = e.cnpj AND epi.ean = e.barras
            WHERE e.estoque > 0
              AND (dns.ean_norm IS NOT NULL OR mi.cloudinary_url IS NOT NULL OR NULLIF(TRIM(m.imagem), '') IS NOT NULL
                   OR e.descricao ILIKE ANY(ARRAY['%%anasol%%','%%vit natu%%','%%vitnatu%%','%%pronabol%%','%%ricosol%%','%%unispray%%','%%goodvit%%']))

            UNION ALL

            SELECT ae.cnpj_loja AS cnpjloja, ae.ean, ae.descricao_produto AS nome,
                   COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
            FROM automatiza_estoque ae
            LEFT JOIN omie_estoque_dns dns ON dns.ean_norm = ae.ean
            LEFT JOIN medicamentos m ON m.barra_norm = ae.ean
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = ae.cnpj_loja AND epi.ean = ae.ean
            WHERE ae.quantidade_estoque > 0
              AND (dns.ean_norm IS NOT NULL OR mi.cloudinary_url IS NOT NULL OR NULLIF(TRIM(m.imagem), '') IS NOT NULL
                   OR ae.descricao_produto ILIKE ANY(ARRAY['%%anasol%%','%%vit natu%%','%%vitnatu%%','%%pronabol%%','%%ricosol%%','%%unispray%%','%%goodvit%%']))
        )
        SELECT MIN(cnpjloja) AS cnpjloja, ean, MIN(nome) AS nome, MIN(imagem) AS imagem
        FROM catalogo
        WHERE ean IS NOT NULL AND TRIM(ean) <> ''
        GROUP BY ean
        ORDER BY nome
        LIMIT %s
        """,
        (limit,),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def _med_info(cur, ean):
    cur.execute(
        """
        SELECT id, descricao, classe, laboratorio, imagem
        FROM medicamentos
        WHERE LTRIM(COALESCE(barra_norm, barra, ''), '0') = LTRIM(%s, '0')
        ORDER BY (classe ILIKE '%%gen%%') DESC, id
        LIMIT 1
        """,
        (ean,),
    )
    row = cur.fetchone()
    return dict(row) if row else {}


def _tarja_info(cur, nome):
    chave = _anvisa_chave(nome or "")
    if not chave:
        return {}
    cur.execute(
        "SELECT alertas, como_usar, nome_anvisa, principio_ativo, tarja FROM anvisa_cache WHERE chave=%s AND encontrado=TRUE LIMIT 1",
        (chave,),
    )
    row = cur.fetchone()
    return dict(row) if row else {}


def _candidate_images(cur, ean):
    urls = []
    cur.execute(
        """
        SELECT imagem_url AS url FROM ecommerce_produto_imagens
        WHERE LTRIM(COALESCE(ean,''), '0') = LTRIM(%s, '0')
          AND imagem_url IS NOT NULL AND TRIM(imagem_url) <> ''
        ORDER BY updated_at DESC NULLS LAST
        """,
        (ean,),
    )
    urls.extend([r["url"] for r in cur.fetchall()])

    cur.execute(
        """
        SELECT mi.cloudinary_url AS url
        FROM medicamentos m
        JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        WHERE LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(%s, '0')
          AND mi.cloudinary_url IS NOT NULL AND TRIM(mi.cloudinary_url) <> ''
        ORDER BY mi.created_at DESC NULLS LAST
        """,
        (ean,),
    )
    urls.extend([r["url"] for r in cur.fetchall()])

    cur.execute(
        """
        SELECT imagem AS url FROM medicamentos
        WHERE LTRIM(COALESCE(barra_norm, barra, ''), '0') = LTRIM(%s, '0')
          AND imagem IS NOT NULL AND TRIM(imagem) <> ''
        ORDER BY id
        """,
        (ean,),
    )
    urls.extend([r["url"] for r in cur.fetchall()])

    seen = set()
    clean = []
    for url in urls:
        url = _clean_url(url)
        if url and url not in seen:
            seen.add(url)
            clean.append(url)
    return clean


def main():
    parser = argparse.ArgumentParser(description="Audita imagens com OCR e remove imagens com marca de outras farmacias.")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = _catalog_missing_or_suspect(args.limit)
    conn = db()
    cur = conn.cursor()
    fixed = blocked = kept = no_action = 0

    for idx, row in enumerate(rows, 1):
        ean = row["ean"]
        nome = row["nome"]
        current = _clean_url(row["imagem"])
        med = _med_info(cur, ean)
        anvisa = _tarja_info(cur, nome)
        tarja = _detectar_tarja(anvisa)
        if tarja:
            anvisa["tarja"] = tarja

        current_bad = bool(current) and (
            _looks_like_other_pharmacy_brand(current) or _image_has_other_pharmacy_text(current)
        )
        if current and not current_bad:
            kept += 1
            print(f"[{idx}/{len(rows)}] OK LIMPA {ean} - {nome}")
            continue

        chosen = None
        for candidate in _candidate_images(cur, ean):
            if candidate == current:
                continue
            if _is_clean_image(candidate):
                chosen = candidate
                break

        placeholder = _generic_placeholder_for(nome, anvisa=anvisa, med=med)
        chosen = chosen or placeholder

        if chosen:
            fixed += 1
            if current_bad:
                blocked += 1
            print(f"[{idx}/{len(rows)}] TROCA {ean} - {nome} -> {chosen}")
            if not args.dry_run:
                _upsert_catalog_image_all_stores(cur, ean, chosen)
                conn.commit()
        else:
            no_action += 1
            print(f"[{idx}/{len(rows)}] SEM IMAGEM LIMPA {ean} - {nome}")

    cur.close()
    print(f"Finalizado: limpas={kept}, trocadas={fixed}, bloqueadas_por_ocr={blocked}, sem_acao={no_action}")


if __name__ == "__main__":
    main()
