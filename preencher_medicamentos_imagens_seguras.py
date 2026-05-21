import argparse

from app import (
    GENERIC_TARJA_PRETA_IMG,
    GENERIC_TARJA_VERMELHA_IMG,
    _anvisa_chave,
    _detectar_tarja,
    _download_image_for_storage,
    _fetch_cosmos_api_image_url,
    _fetch_exact_barcode_image_url,
    _fetch_serper_image_result_url,
    _fetch_verified_serper_image_url,
    _generic_placeholder_for,
    _image_has_other_pharmacy_text,
    _image_looks_non_product,
    _is_untrusted_scraped_image,
    _looks_like_other_pharmacy_brand,
    _upsert_catalog_image_all_stores,
    db,
    upload_to_supabase_storage,
)


def _clean_url(url):
    url = (url or "").strip()
    return url if url.startswith(("http://", "https://")) else None


def _is_bad_image(url, check_ocr=True):
    url = _clean_url(url)
    if not url:
        return False
    if _looks_like_other_pharmacy_brand(url):
        return True
    if check_ocr and _image_looks_non_product(url):
        return True
    return bool(check_ocr and _image_has_other_pharmacy_text(url))


def _is_clean_image(url, check_ocr=True):
    url = _clean_url(url)
    return bool(url and not _is_bad_image(url, check_ocr=check_ocr))


def _anvisa_for_name(cur, nome):
    chave = _anvisa_chave(nome or "")
    if not chave:
        return {}
    cur.execute(
        """
        SELECT alertas, como_usar, nome_anvisa, principio_ativo, tarja, exibir_imagem_publica
        FROM anvisa_cache
        WHERE chave=%s AND encontrado=TRUE
        LIMIT 1
        """,
        (chave,),
    )
    row = cur.fetchone()
    return dict(row) if row else {}


def _candidate_images(cur, med_id, ean):
    urls = []
    cur.execute(
        """
        SELECT cloudinary_url AS url
        FROM medicamentos_imagens
        WHERE medicamento_id = %s
          AND cloudinary_url IS NOT NULL AND TRIM(cloudinary_url) <> ''
        ORDER BY created_at DESC NULLS LAST
        """,
        (med_id,),
    )
    urls.extend(r["url"] for r in cur.fetchall())

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
    urls.extend(r["url"] for r in cur.fetchall())

    cur.execute(
        """
        SELECT imagem_url AS url
        FROM ecommerce_produto_imagens
        WHERE LTRIM(COALESCE(ean, ''), '0') = LTRIM(%s, '0')
          AND imagem_url IS NOT NULL AND TRIM(imagem_url) <> ''
        ORDER BY updated_at DESC NULLS LAST
        """,
        (ean,),
    )
    urls.extend(r["url"] for r in cur.fetchall())

    cur.execute(
        """
        SELECT imagem AS url
        FROM medicamentos
        WHERE LTRIM(COALESCE(barra_norm, barra, ''), '0') = LTRIM(%s, '0')
          AND imagem IS NOT NULL AND TRIM(imagem) <> ''
        ORDER BY id
        """,
        (ean,),
    )
    urls.extend(r["url"] for r in cur.fetchall())

    seen = set()
    result = []
    for url in urls:
        url = _clean_url(url)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _external_image(ean, nome, upload=True):
    source_fns = (
        lambda: _fetch_exact_barcode_image_url(ean),          # OpenFoodFacts/BeautyFacts (alimentos/beleza)
        lambda: _fetch_cosmos_api_image_url(ean),             # Bluesoft Cosmos (melhor cobertura BR)
        lambda: _fetch_verified_serper_image_url(ean, nome),
        lambda: _fetch_serper_image_result_url(ean, nome),
    )
    tried = set()
    for get_source in source_fns:
        source_url = get_source()
        if not source_url or source_url in tried:
            continue
        tried.add(source_url)
        if _is_bad_image(source_url):
            continue
        raw, ext, content_type = _download_image_for_storage(source_url)
        if not raw:
            continue
        if not upload:
            return source_url
        uploaded = upload_to_supabase_storage(raw, f"medicamentos-auto-ean/{ean}.{ext}", content_type)
        if uploaded:
            return uploaded
    return None


def _rows_to_process(limit, only_missing, include_suspect, only_generics, suspect_only):
    where = ["COALESCE(barra_norm, barra, '') <> ''"]
    if only_generics:
        where.append("classe ILIKE '%%gen%%'")
    if suspect_only:
        where.append("(imagem ILIKE '%%google_auto%%' OR imagem ILIKE '%%drogaria%%' OR imagem ILIKE '%%farmacia%%' OR imagem ILIKE '%%farma%%')")
    elif only_missing:
        where.append("(imagem IS NULL OR TRIM(imagem) = '')")
    elif include_suspect:
        where.append("(imagem IS NULL OR TRIM(imagem) = '' OR imagem ILIKE '%%google_auto%%' OR imagem ILIKE '%%drogaria%%' OR imagem ILIKE '%%farmacia%%' OR imagem ILIKE '%%farma%%')")
    sql = f"""
        SELECT id, descricao, barra_norm, barra, classe, imagem
        FROM medicamentos
        WHERE {' AND '.join(where)}
        ORDER BY (imagem IS NULL OR TRIM(imagem) = '') DESC, id
        LIMIT %s
    """
    conn = db()
    cur = conn.cursor()
    cur.execute(sql, (limit,))
    rows = cur.fetchall()
    cur.close()
    return rows


def main():
    parser = argparse.ArgumentParser(description="Preenche/saneia medicamentos.imagem com OCR e fontes seguras por EAN.")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only-missing", action="store_true")
    parser.add_argument("--include-suspect", action="store_true", default=True)
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--only-generics", action="store_true")
    parser.add_argument("--suspect-only", action="store_true")
    parser.add_argument("--skip-external-if-placeholder", action="store_true")
    args = parser.parse_args()

    rows = _rows_to_process(args.limit, args.only_missing, args.include_suspect, args.only_generics, args.suspect_only)
    conn = db()
    cur = conn.cursor()
    ok = kept = bad = none = 0

    for idx, row in enumerate(rows, 1):
        med = dict(row)
        ean = (med.get("barra_norm") or med.get("barra") or "").strip()
        nome = med.get("descricao") or ""
        current = _clean_url(med.get("imagem"))
        check_ocr = not args.no_ocr
        anvisa = _anvisa_for_name(cur, nome)
        tarja = _detectar_tarja(anvisa)
        if tarja:
            anvisa["tarja"] = tarja

        current_bad = bool(current) and (
            _is_bad_image(current, check_ocr=check_ocr)
            or (_is_untrusted_scraped_image(current) and _image_has_other_pharmacy_text(current))
        )
        if current and not current_bad:
            kept += 1
            print(f"[{idx}/{len(rows)}] MANTEM {ean} - {nome}")
            continue
        if current_bad:
            bad += 1

        chosen = None
        placeholder = _generic_placeholder_for(nome, anvisa=anvisa, med=med)
        for candidate in _candidate_images(cur, med["id"], ean):
            if candidate == current:
                continue
            if placeholder and args.skip_external_if_placeholder and _is_untrusted_scraped_image(candidate):
                continue
            if _is_clean_image(candidate, check_ocr=check_ocr):
                chosen = candidate
                break

        if not chosen and placeholder and args.skip_external_if_placeholder:
            chosen = placeholder
        if not chosen:
            chosen = _external_image(ean, nome)
        if not chosen:
            chosen = placeholder

        if chosen:
            ok += 1
            kind = "TARJA_PRETA" if chosen == GENERIC_TARJA_PRETA_IMG else "TARJA_VERMELHA" if chosen == GENERIC_TARJA_VERMELHA_IMG else "IMAGEM_LIMPA"
            print(f"[{idx}/{len(rows)}] ATUALIZA {ean} - {nome} -> {kind}")
            if not args.dry_run:
                cur.execute("UPDATE medicamentos SET imagem=%s WHERE id=%s", (chosen, med["id"]))
                _upsert_catalog_image_all_stores(cur, ean, chosen)
                conn.commit()
        else:
            none += 1
            print(f"[{idx}/{len(rows)}] SEM FONTE LIMPA {ean} - {nome}")

    cur.close()
    print(f"Finalizado: mantidas={kept}, atualizadas={ok}, suspeitas_detectadas={bad}, sem_fonte={none}")


if __name__ == "__main__":
    main()
