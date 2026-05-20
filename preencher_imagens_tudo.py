"""
preencher_imagens_tudo.py — Preenche imagens faltantes em loop automático.

Fase 1: medicamentos sem imagem  (usa OCR + checagem de farmácia + banner)
Fase 2: ecommerce sem imagem     (cosméticos, suplementos, etc. não-ANVISA)

Uso:
    python preencher_imagens_tudo.py
    python preencher_imagens_tudo.py --batch 200   # tamanho do lote (padrão: 300)
    python preencher_imagens_tudo.py --so-med       # só fase 1 (medicamentos)
    python preencher_imagens_tudo.py --so-ecom      # só fase 2 (ecommerce)
    python preencher_imagens_tudo.py --dry-run      # simula sem salvar nada
"""

import argparse
import time
import sys
import os
import signal

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

signal.signal(signal.SIGINT,  lambda *_: (print("\n\nInterrompido. Encerrando..."), os._exit(0)))
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))

from app import (
    GENERIC_TARJA_PRETA_IMG,
    GENERIC_TARJA_VERMELHA_IMG,
    _detectar_tarja,
    _download_image_for_storage,
    _fill_one_catalog_image,
    _generic_placeholder_for,
    _image_has_other_pharmacy_text,
    _image_looks_non_product,
    _is_untrusted_scraped_image,
    _looks_like_other_pharmacy_brand,
    _upsert_catalog_image_all_stores,
    db,
    upload_to_supabase_storage,
)
from preencher_medicamentos_imagens_seguras import (
    _anvisa_for_name,
    _candidate_images,
    _external_image,
    _is_clean_image as _is_clean,
)

_COSMOS_THUMB_CACHE = {}


# ── contadores ───────────────────────────────────────────────

def _count_med_missing(conn):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM medicamentos WHERE COALESCE(barra_norm,barra,'') <> '' AND (imagem IS NULL OR TRIM(imagem) = '')")
    n = cur.fetchone()["n"]
    cur.close()
    return n


def _count_ecom_missing(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(DISTINCT ean) AS n FROM (
            SELECT COALESCE(e.barras_norm, e.barras) AS ean,
                   COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem),'')) AS imagem
            FROM estoque e
            LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = e.cnpj AND epi.ean = e.barras
            WHERE e.estoque > 0
            UNION ALL
            SELECT ae.ean,
                   COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem),'')) AS imagem
            FROM automatiza_estoque ae
            LEFT JOIN medicamentos m ON m.barra_norm = ae.ean
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = ae.cnpj_loja AND epi.ean = ae.ean
            WHERE ae.quantidade_estoque > 0
        ) t
        WHERE imagem IS NULL OR TRIM(imagem) = ''
    """)
    n = cur.fetchone()["n"]
    cur.close()
    return n


# ── thumbnail Cosmos ─────────────────────────────────────────

def _cosmos_thumbnail_url(conn, ean):
    """Busca imagem_cosmos da produto_canon, faz upload para storage e retorna URL permanente."""
    if ean in _COSMOS_THUMB_CACHE:
        return _COSMOS_THUMB_CACHE[ean]
    cur = conn.cursor()
    cur.execute(
        "SELECT imagem_cosmos FROM produto_canon WHERE ean = %s AND imagem_cosmos IS NOT NULL LIMIT 1",
        (ean,),
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        _COSMOS_THUMB_CACHE[ean] = None
        return None
    src = (row["imagem_cosmos"] or "").strip()
    if not src.startswith("http"):
        _COSMOS_THUMB_CACHE[ean] = None
        return None
    # faz download e sobe para nosso storage (evita depender do CDN Bluesoft)
    raw, ext, content_type = _download_image_for_storage(src)
    if not raw:
        _COSMOS_THUMB_CACHE[ean] = None
        return None
    uploaded = upload_to_supabase_storage(raw, f"cosmos-ean/{ean}.{ext}", content_type)
    _COSMOS_THUMB_CACHE[ean] = uploaded
    return uploaded


# ── fase 1: medicamentos ──────────────────────────────────────

def _batch_med(conn, batch):
    cur = conn.cursor()
    cur.execute("""
        SELECT id, descricao, barra_norm, barra, classe, imagem
        FROM medicamentos
        WHERE COALESCE(barra_norm, barra, '') <> ''
          AND (imagem IS NULL OR TRIM(imagem) = '')
        ORDER BY id
        LIMIT %s
    """, (batch,))
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


def processar_medicamento(conn, med, dry_run=False):
    cur = conn.cursor()
    ean = (med.get("barra_norm") or med.get("barra") or "").strip()
    nome = med.get("descricao") or ""
    anvisa = _anvisa_for_name(cur, nome)
    tarja = _detectar_tarja(anvisa)
    if tarja:
        anvisa["tarja"] = tarja

    placeholder = _generic_placeholder_for(nome, anvisa=anvisa, med=med)
    chosen = None

    for candidate in _candidate_images(cur, med["id"], ean):
        if candidate == med.get("imagem"):
            continue
        if _is_clean(candidate, check_ocr=True):
            chosen = candidate
            break

    if not chosen:
        chosen = _cosmos_thumbnail_url(conn, ean)
    if not chosen:
        chosen = _external_image(ean, nome)
    if not chosen:
        chosen = placeholder

    if chosen and not dry_run:
        cur.execute("UPDATE medicamentos SET imagem=%s WHERE id=%s", (chosen, med["id"]))
        _upsert_catalog_image_all_stores(cur, ean, chosen)

    cur.close()
    return chosen


# ── fase 2: ecommerce ─────────────────────────────────────────

VISIBLE_FILTER_ALPHA = """
    AND (
      dns.ean_norm IS NOT NULL
      OR mi.cloudinary_url IS NOT NULL
      OR NULLIF(TRIM(m.imagem), '') IS NOT NULL
      OR e.descricao ILIKE ANY(ARRAY[
           '%%anasol%%','%%vit natu%%','%%vitnatu%%',
           '%%pronabol%%','%%ricosol%%','%%unispray%%','%%goodvit%%'
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
           '%%pronabol%%','%%ricosol%%','%%unispray%%','%%goodvit%%'
         ])
    )
"""


def _batch_ecom(conn, batch):
    cur = conn.cursor()
    cur.execute(f"""
        WITH catalogo AS (
            SELECT e.cnpj AS cnpjloja, e.barras AS ean, e.descricao AS nome,
                   COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
            FROM estoque e
            LEFT JOIN omie_estoque_dns dns ON dns.ean_norm = COALESCE(e.barras_norm, e.barras)
            LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = e.cnpj AND epi.ean = e.barras
            WHERE e.estoque > 0
              AND NOT EXISTS (SELECT 1 FROM ecommerce_catalogo_oculto co WHERE co.cnpjloja = e.cnpj AND co.ean = e.barras)
              {VISIBLE_FILTER_ALPHA}
            UNION ALL
            SELECT ae.cnpj_loja, ae.ean, ae.descricao_produto,
                   COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
            FROM automatiza_estoque ae
            LEFT JOIN omie_estoque_dns dns ON dns.ean_norm = ae.ean
            LEFT JOIN medicamentos m ON m.barra_norm = ae.ean
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = ae.cnpj_loja AND epi.ean = ae.ean
            WHERE ae.quantidade_estoque > 0
              AND NOT EXISTS (SELECT 1 FROM ecommerce_catalogo_oculto co WHERE co.cnpjloja = ae.cnpj_loja AND co.ean = ae.ean)
              {VISIBLE_FILTER_AUTO}
        )
        SELECT MIN(cnpjloja) AS cnpjloja, ean, MIN(nome) AS nome
        FROM catalogo
        WHERE imagem IS NULL OR TRIM(imagem) = ''
        GROUP BY ean
        ORDER BY nome
        LIMIT %s
    """, (batch,))
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


# ── loop principal ────────────────────────────────────────────

def _barra(ok, total, width=30):
    filled = int(width * ok / max(total, 1))
    return f"[{'#'*filled}{'-'*(width-filled)}] {ok}/{total}"


def _row_key(row):
    return str(row.get("id") or row.get("ean") or row.get("barra_norm") or row.get("barra") or "")


def run_fase(nome_fase, fetch_fn, process_fn, conn, batch, dry_run, total_inicial, commit_every=25):
    ok = sem_fonte = 0
    rodada = 0
    total_processado = 0
    dirty = 0
    sem_fonte_keys = set()
    t0 = time.monotonic()

    print(f"\n{'='*60}")
    print(f"{nome_fase}  ({total_inicial} pendentes)")
    print('='*60)

    while True:
        fetch_limit = min(max(batch, batch + len(sem_fonte_keys)), 10000)
        rows = fetch_fn(conn, fetch_limit)
        if not rows:
            break
        rows = [r for r in rows if _row_key(r) not in sem_fonte_keys][:batch]
        if not rows:
            break
        rodada += 1
        for i, row in enumerate(rows, 1):
            ean = row.get("ean") or row.get("barra_norm") or row.get("barra") or ""
            nome = row.get("nome") or row.get("descricao") or ""
            resultado = process_fn(conn, row, dry_run=dry_run)
            total_processado += 1
            if resultado:
                ok += 1
                if not dry_run:
                    dirty += 1
                kind = ("TARJA_PRETA" if resultado == GENERIC_TARJA_PRETA_IMG
                        else "TARJA_VERMELHA" if resultado == GENERIC_TARJA_VERMELHA_IMG
                        else "OK")
                print(f"  [{total_processado:5}] {kind:14}  {ean}  {nome[:45]}", flush=True)
            else:
                sem_fonte += 1
                sem_fonte_keys.add(_row_key(row))
                print(f"  [{total_processado:5}] SEM FONTE      {ean}  {nome[:45]}", flush=True)

            if dirty >= commit_every:
                conn.commit()
                dirty = 0

        elapsed = time.monotonic() - t0
        vel = total_processado / elapsed if elapsed > 0 else 0
        restante = (total_inicial - total_processado) / vel if vel > 0 else 0
        print(f"\n  {_barra(total_processado, total_inicial)}  "
              f"{vel:.1f}/s  ETA ~{int(restante//60)}min\n", flush=True)

    print(f"\n{nome_fase} concluída: {ok} preenchidos  {sem_fonte} sem fonte")
    if dirty:
        conn.commit()
    return ok, sem_fonte


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch",    type=int, default=300, help="Itens por lote (padrão: 300)")
    ap.add_argument("--so-med",   action="store_true",   help="Só fase 1 (medicamentos)")
    ap.add_argument("--so-ecom",  action="store_true",   help="Só fase 2 (ecommerce)")
    ap.add_argument("--commit-every", type=int, default=25, help="Commit a cada N imagens salvas (padrao: 25)")
    ap.add_argument("--dry-run",  action="store_true",   help="Simula sem salvar")
    args = ap.parse_args()

    conn = db()

    med_missing  = _count_med_missing(conn)
    ecom_missing = _count_ecom_missing(conn)

    print(f"{'='*60}")
    print(f"Preencher imagens — diagnóstico inicial")
    print(f"{'='*60}")
    print(f"  medicamentos sem imagem : {med_missing:,}")
    print(f"  ecommerce sem imagem    : {ecom_missing:,}")
    if args.dry_run:
        print("  [DRY-RUN] nada será salvo")
    print(f"{'='*60}")

    t_start = time.monotonic()
    total_ok = 0
    total_sem = 0

    if not args.so_ecom and med_missing > 0:
        ok, sem = run_fase(
            "FASE 1 — medicamentos",
            _batch_med,
            processar_medicamento,
            conn, args.batch, args.dry_run, med_missing, args.commit_every,
        )
        total_ok  += ok
        total_sem += sem

    if not args.so_med:
        # recontabiliza ecommerce (fase 1 pode ter propagado imagens via _upsert_catalog_image_all_stores)
        ecom_missing = _count_ecom_missing(conn)
        if ecom_missing > 0:
            def _processar_ecom(conn, row, dry_run=False):
                if dry_run:
                    return None
                ean  = row["ean"]
                cnpj = row["cnpjloja"]
                nome = row.get("nome")
                img  = _cosmos_thumbnail_url(conn, ean)
                if img:
                    c = conn.cursor()
                    _upsert_catalog_image_all_stores(c, ean, img)
                    c.close()
                    return img
                return _fill_one_catalog_image(cnpj, ean, nome)

            ok, sem = run_fase(
                "FASE 2 — ecommerce (não-ANVISA)",
                _batch_ecom,
                _processar_ecom,
                conn, args.batch, args.dry_run, ecom_missing, args.commit_every,
            )
            total_ok  += ok
            total_sem += sem
        else:
            print("\nFASE 2: nada pendente após fase 1.")

    elapsed = time.monotonic() - t_start
    print(f"\n{'='*60}")
    print(f"CONCLUÍDO em {int(elapsed//60)}min {int(elapsed%60)}s")
    print(f"  Preenchidos : {total_ok:,}")
    print(f"  Sem fonte   : {total_sem:,}")
    print(f"{'='*60}\n")
    conn.close()


if __name__ == "__main__":
    main()
