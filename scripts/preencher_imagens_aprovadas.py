"""
preencher_imagens_aprovadas.py
Preenche imagens de EANs que o revalidar_tarja_vtex JÁ aprovou
(anvisa_cache.exibir_imagem_publica = TRUE) mas que ainda ficaram sem imagem.

Foco: cosméticos, higiene, perfumaria, puericultura — categorias sem risco de
conflito com tarja. Se um EAN tiver exibir=FALSE no anvisa_cache, a imagem
simplesmente não aparece no ecommerce de qualquer forma.

Fontes de imagem (em ordem de prioridade):
  1. medicamentos5.imagem  — catálogo interno Poupaqui (sem OCR necessário)
  2. DSP (VTEX)            — com OCR
  3. Droga Raia            — com OCR
  4. Ultrafarma            — scraping simples, com OCR

Atualiza apenas produto_canon.imagem_cosmos (não toca tarja nem exibir).
Como produto_canon é por EAN, uma imagem encontrada vale para TODAS as lojas.

Uso:
    py scripts/preencher_imagens_aprovadas.py             # dry-run 100 chaves
    py scripts/preencher_imagens_aprovadas.py --apply
    py scripts/preencher_imagens_aprovadas.py --limite 200
    py scripts/preencher_imagens_aprovadas.py --chave "SHAMPOO SEDA"
    py scripts/preencher_imagens_aprovadas.py --categoria higiene
    py scripts/preencher_imagens_aprovadas.py -v
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from app import db, _anvisa_chave
from scripts.revalidar_tarja_vtex import (
    _vtex_fetch,
    _raia_fetch,
    _extrair_dados_vtex,
    _upload_cloudinary,
    _analisar_imagem_branded,
    _PLACEHOLDERS_POUPAQUI,
    _UA,
)

# ── Categorias-foco (sem risco de tarja) ────────────────────────────────────

_KEYWORDS: dict[str, list[str]] = {
    "cosmeticos": [
        "shampoo", "condicionador", "creme pent", "leave-in", "leave in",
        "mascara capilar", "mascara trat", "serum", "sérum", "elseve", "seda ",
        "pantene", "garnier", "siage", "tresemme", "loreal", "wella",
        "hidratante", "creme facial", "creme corp", "loção", "locao",
        "sabonete liq", "cicatricure", "nivea", "dove ", "giovanna",
        "natura ", "boticario", "avon ", "esmalte", "maquiagem",
        "batom", "blush", "rimel", "perfume", "colonia", "agua de colonia",
    ],
    "higiene": [
        "sabonete", "desodorante", "absorvente", "preserv", "fio dental",
        "escova dent", "creme dent", "pasta dent", "enxaguante", "algodao",
        "curativo", "band-aid", "repelente", "protetor solar", "protetor labial",
        "depilatorio", "depilatório", "barbeador", "lamina barb",
    ],
    "puericultura": [
        "fralda", "lenco umed", "lenço umed", "mamadeira", "chupeta",
        "buba ", "mam ", "huggies", "pampers", "baby ", "bebê ", "bebe ",
        "pomada assad", "talco infantil", "aspirador nasal",
    ],
}
_ALL_KEYWORDS = [kw for kws in _KEYWORDS.values() for kw in kws]

_PLACEHOLDERS = list(_PLACEHOLDERS_POUPAQUI)


# ── Ultrafarma ───────────────────────────────────────────────────────────────

_ULTRA_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, */*",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer": "https://www.ultrafarma.com.br/",
}
_BRANDED_ULTRA_RE = re.compile(
    r"ultrafarma|drogaria|farmacia\s+pop|pague\s*menos|de[\s_-]referencia",
    re.IGNORECASE,
)


def _ultrafarma_fetch(ean: str, verbose: bool = False) -> str | None:
    """Busca imagem real na Ultrafarma pelo EAN. Retorna URL ou None."""
    try:
        from curl_cffi import requests as cr
    except ImportError:
        return None

    url = f"https://www.ultrafarma.com.br/busca?q={ean}"
    if verbose:
        print(f"    [ultra] GET {url}")
    try:
        r = cr.get(url, headers=_ULTRA_HEADERS, impersonate="chrome124",
                   timeout=15, allow_redirects=True)
        if r.status_code != 200 or not r.text:
            return None
        html = r.text
    except Exception as exc:
        if verbose:
            print(f"    [ultra] ERRO: {exc}")
        return None

    # JSON-LD product
    for raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            ld = json.loads(raw)
            items = ld if isinstance(ld, list) else [ld]
            for item in items:
                if str(item.get("@type", "")).lower() != "product":
                    continue
                img = item.get("image") or ""
                if isinstance(img, list):
                    img = img[0] if img else ""
                if not img or not img.startswith("http"):
                    continue
                fname = img.split("?")[0].split("/")[-1]
                if _BRANDED_ULTRA_RE.search(fname) or _BRANDED_ULTRA_RE.search(img):
                    if verbose:
                        print(f"    [ultra] branded, pulando")
                    return None
                if verbose:
                    print(f"    [ultra] imagem: {img[:70]}")
                return img
        except Exception:
            pass

    # og:image fallback
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                  html, re.IGNORECASE)
    if m:
        img = m.group(1)
        if img.startswith("http") and not _BRANDED_ULTRA_RE.search(img):
            return img

    return None


# ── medicamentos5 lookup ─────────────────────────────────────────────────────

def _med5_lookup(cur, eans: list[str]) -> str | None:
    """Busca imagem em medicamentos5 pelo EAN (barra). Retorna URL ou None."""
    if not eans:
        return None
    cur.execute(
        """
        SELECT imagem FROM medicamentos5
        WHERE barra = ANY(%s)
          AND imagem IS NOT NULL AND TRIM(imagem) <> ''
        LIMIT 1
        """,
        (eans,),
    )
    row = cur.fetchone()
    return row["imagem"] if row else None


# ── Busca de EANs aprovados sem imagem ───────────────────────────────────────

_SEM_IMAGEM_SQL = """NOT EXISTS (
        SELECT 1 FROM produto_canon pc
        WHERE pc.ean = ac.chave
          AND pc.imagem_cosmos IS NOT NULL
          AND TRIM(pc.imagem_cosmos) <> ''
          AND pc.imagem_cosmos NOT LIKE '%%CAIXA_GEN%%'
          AND pc.imagem_cosmos NOT LIKE '%%ChatGPT_Image%%'
          AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss')
    )"""


def _buscar_chaves_aprovadas(cur, categoria: str | None, limite: int,
                              chave_filtro: str | None,
                              modo: str = "cosmeticos") -> list[dict]:
    """
    Retorna chaves do anvisa_cache onde exibir=TRUE sem imagem no produto_canon.

    modo='tarja_null'  -> exibir=TRUE + tarja IS NULL (OTC/perfumaria, 2318 registros)
    modo='cosmeticos'  -> filtra por keywords de categoria (padrao)
    """
    if chave_filtro:
        cur.execute("""
            SELECT chave, tarja FROM anvisa_cache
            WHERE encontrado = TRUE
              AND exibir_imagem_publica = TRUE
              AND chave ILIKE %s
            LIMIT 50
        """, (f"%{chave_filtro.upper()}%",))
        return [dict(r) for r in cur.fetchall()]

    if modo == "tarja_null":
        cur.execute(f"""
            SELECT ac.chave, ac.tarja
            FROM anvisa_cache ac
            WHERE ac.encontrado = TRUE
              AND ac.exibir_imagem_publica = TRUE
              AND ac.tarja IS NULL
              AND {_SEM_IMAGEM_SQL}
            ORDER BY ac.chave
            LIMIT %s
        """, (limite,))
        return [dict(r) for r in cur.fetchall()]

    # modo='cosmeticos' — filtra por keywords
    keywords = _KEYWORDS.get(categoria, _ALL_KEYWORDS) if categoria else _ALL_KEYWORDS
    ilike_clauses = " OR ".join(["ac.chave ILIKE %s"] * len(keywords))
    params_kw = [f"%{kw.upper()}%" for kw in keywords]
    cur.execute(f"""
        SELECT ac.chave, ac.tarja
        FROM anvisa_cache ac
        WHERE ac.encontrado = TRUE
          AND ac.exibir_imagem_publica = TRUE
          AND ({ilike_clauses})
          AND {_SEM_IMAGEM_SQL}
        ORDER BY ac.chave
        LIMIT %s
    """, params_kw + [limite])
    return [dict(r) for r in cur.fetchall()]


def _resolver_eans(cur, chaves: list[str]) -> dict[str, list[str]]:
    """
    Para cada chave, encontra EANs no catálogo de estoque usando _anvisa_chave.
    Retorna {chave: [ean1, ean2, ...]}.
    """
    if not chaves:
        return {}

    cur.execute("""
        SELECT DISTINCT
            COALESCE(m.barra_norm, e.barras) AS ean,
            COALESCE(m.descricao, e.descricao) AS nome
        FROM estoque e
        LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
        WHERE COALESCE(e.barras, '') <> ''
          AND e.estoque > 0
          AND COALESCE(e.barras_norm, e.barras) ~ '^[1-9][0-9]{6,12}$'

        UNION

        SELECT DISTINCT
            COALESCE(m.barra_norm, ae.ean) AS ean,
            COALESCE(m.descricao, ae.descricao_produto) AS nome
        FROM automatiza_estoque ae
        LEFT JOIN medicamentos m ON m.barra_norm = ae.ean
        WHERE COALESCE(ae.ean, '') <> ''
          AND ae.quantidade_estoque > 0
          AND ae.ean ~ '^[1-9][0-9]{6,12}$'
    """)

    chaves_set = set(chaves)
    resultado: dict[str, list[str]] = {c: [] for c in chaves}
    for row in cur.fetchall():
        ean  = (row.get("ean") or "").strip()
        nome = (row.get("nome") or "").strip()
        if not ean or not nome:
            continue
        chave = _anvisa_chave(nome)
        if chave in chaves_set and ean not in resultado[chave]:
            resultado[chave].append(ean)
    return resultado


def _salvar_imagem(cur, eans: list[str], url: str, fonte: str,
                   apply: bool, verbose: bool) -> bool:
    """Faz upload para Cloudinary e salva em produto_canon. Retorna True se gravou."""
    url_final = url
    if apply:
        url_cl = _upload_cloudinary(url, eans[0], verbose=verbose)
        if url_cl:
            url_final = url_cl
            if verbose:
                print(f"    [cloudinary OK] {url_cl[:70]}")
        else:
            if verbose:
                print(f"    [cloudinary FALHOU] usando URL original")

    for ean in eans:
        if apply:
            cur.execute("""
                UPDATE produto_canon
                   SET imagem_cosmos = %s, fonte = %s, atualizado_em = NOW()
                WHERE ean = %s
                  AND (
                    imagem_cosmos IS NULL OR TRIM(imagem_cosmos) = ''
                    OR imagem_cosmos LIKE '%%CAIXA_GEN%%'
                    OR imagem_cosmos LIKE '%%ChatGPT_Image%%'
                    OR fonte IN ('cosmos_miss', 'ia_miss')
                  )
            """, (url_final, fonte, ean))

            if cur.rowcount == 0:
                cur.execute("""
                    INSERT INTO produto_canon (ean, descricao_canon, imagem_cosmos, fonte, atualizado_em)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (ean) DO UPDATE
                      SET imagem_cosmos = EXCLUDED.imagem_cosmos,
                          fonte = EXCLUDED.fonte,
                          atualizado_em = NOW()
                    WHERE produto_canon.imagem_cosmos IS NULL
                       OR TRIM(produto_canon.imagem_cosmos) = ''
                       OR produto_canon.imagem_cosmos LIKE '%%CAIXA_GEN%%'
                       OR produto_canon.imagem_cosmos LIKE '%%ChatGPT_Image%%'
                       OR produto_canon.fonte IN ('cosmos_miss', 'ia_miss')
                """, (ean, ean, url_final, fonte))
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Preenche imagens de EANs aprovados (exibir=TRUE) sem imagem."
    )
    parser.add_argument("--apply",     action="store_true")
    parser.add_argument("--limite",    type=int, default=100, metavar="N",
                        help="Número de chaves anvisa_cache a processar")
    parser.add_argument("--chave",     help="Filtra por chave anvisa especifica")
    parser.add_argument("--modo",      choices=["cosmeticos", "tarja_null"],
                        default="cosmeticos",
                        help="tarja_null=exibir=TRUE+sem tarja (2318 OTC/perfumaria); cosmeticos=por keyword")
    parser.add_argument("--categoria", choices=list(_KEYWORDS.keys()),
                        help="Filtra por categoria (so no modo cosmeticos)")
    parser.add_argument("--delay",     type=float, default=0.7, metavar="S")
    parser.add_argument("--commit-cada", type=int, default=30, metavar="N")
    parser.add_argument("--sem-ocr",   action="store_true",
                        help="Pula verificação OCR (mais rápido, menos seguro)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    ocr_key = os.getenv("OCR_SPACE_API_KEY", "").strip()
    if ocr_key and not args.sem_ocr:
        print(f"OCR ativo (chave: {ocr_key[:8]}...)")
    else:
        print("OCR inativo" + (" (--sem-ocr)" if args.sem_ocr else " (sem chave)"))

    conn = db()
    cur  = conn.cursor()

    # 1. Busca chaves aprovadas sem imagem
    chaves_rows = _buscar_chaves_aprovadas(cur, args.categoria, args.limite, args.chave, args.modo)
    cat_label = f"{args.modo}/{args.categoria or 'todas'}"
    print(f"\nChaves aprovadas sem imagem [{cat_label}]: {len(chaves_rows)}")
    if not chaves_rows:
        print("Nenhuma chave encontrada.")
        cur.close(); conn.close(); return

    # 2. Resolve chaves → EANs
    todas_chaves = [r["chave"] for r in chaves_rows]
    print(f"Resolvendo EANs para {len(todas_chaves)} chaves...")
    chave_para_eans = _resolver_eans(cur, todas_chaves)
    sem_ean = sum(1 for c in todas_chaves if not chave_para_eans.get(c))
    print(f"  Com EAN: {len(todas_chaves)-sem_ean} | Sem EAN: {sem_ean}\n")

    n_med5 = n_dsp = n_raia = n_ultra = n_sem = 0
    pendentes = 0

    for reg in chaves_rows:
        chave = reg["chave"]
        eans  = chave_para_eans.get(chave) or []
        if not eans:
            if args.verbose:
                print(f"[{chave}] sem EAN, pulando")
            continue

        if args.verbose:
            print(f"\n[{chave}] EANs: {eans}")

        achou = False

        # ── Fonte 1: medicamentos5 ────────────────────────────────────────
        url_med5 = _med5_lookup(cur, eans)
        if url_med5:
            print(f"  [med5]  {chave}  {url_med5[:70]}...")
            _salvar_imagem(cur, eans, url_med5, "med5", args.apply, args.verbose)
            n_med5 += 1
            achou = True

        if not achou:
            # ── Fonte 2: DSP ──────────────────────────────────────────────
            produto_vtex = None
            for ean in eans:
                produto_vtex = _vtex_fetch(ean, verbose=args.verbose)
                if produto_vtex:
                    break
                time.sleep(args.delay * 0.4)

            dados_dsp  = _extrair_dados_vtex(produto_vtex, verbose=args.verbose) if produto_vtex else None
            imagem_dsp = (dados_dsp or {}).get("imagem")
            exibir_dsp = (dados_dsp or {}).get("exibir_imagem")

            if imagem_dsp:
                print(f"  [dsp]   {chave}  {imagem_dsp[:70]}...")
                _salvar_imagem(cur, eans, imagem_dsp, "vtex_dsp", args.apply, args.verbose)
                n_dsp += 1
                achou = True

        if not achou:
            time.sleep(args.delay * 0.3)
            # ── Fonte 3: Droga Raia ───────────────────────────────────────
            for ean in eans:
                dados_raia = _raia_fetch(ean, verbose=args.verbose)
                imagem_raia = (dados_raia or {}).get("imagem")
                if imagem_raia:
                    print(f"  [raia]  {chave}  {imagem_raia[:70]}...")
                    _salvar_imagem(cur, eans, imagem_raia, "vtex_raia", args.apply, args.verbose)
                    n_raia += 1
                    achou = True
                    break
                time.sleep(args.delay * 0.3)

        if not achou:
            # ── Fonte 4: Ultrafarma ───────────────────────────────────────
            for ean in eans:
                imagem_ultra = _ultrafarma_fetch(ean, verbose=args.verbose)
                if imagem_ultra:
                    # OCR verify se disponível
                    usar = True
                    if ocr_key and not args.sem_ocr:
                        branded = _analisar_imagem_branded(imagem_ultra, verbose=args.verbose)
                        if branded is True:
                            if args.verbose:
                                print(f"    [ocr] branded na Ultrafarma, pulando")
                            usar = False
                    if usar:
                        print(f"  [ultra] {chave}  {imagem_ultra[:70]}...")
                        _salvar_imagem(cur, eans, imagem_ultra, "ultrafarma", args.apply, args.verbose)
                        n_ultra += 1
                        achou = True
                        break
                time.sleep(args.delay * 0.3)

        if not achou:
            n_sem += 1
            if args.verbose:
                print(f"  [sem imagem] {chave}")

        # Commit parcial
        if achou and args.apply:
            pendentes += 1
            if pendentes >= args.commit_cada:
                conn.commit()
                print(f"  [commit parcial] {pendentes} gravadas")
                pendentes = 0

        time.sleep(args.delay)

    # Commit final
    if args.apply:
        if pendentes > 0:
            conn.commit()
        total = n_med5 + n_dsp + n_raia + n_ultra
        print(f"\nGravado." if total else "\nNenhuma imagem nova encontrada.")
    else:
        conn.rollback()
        total = n_med5 + n_dsp + n_raia + n_ultra
        if total:
            print(f"\nDry-run: {total} imagem(ns) encontrada(s) — use --apply para gravar.")
        else:
            print("\nNenhuma imagem nova encontrada.")

    print(
        f"\nTotal chaves: {len(chaves_rows)} | Sem EAN: {sem_ean}"
        f"\nMed5: {n_med5} | DSP: {n_dsp} | Raia: {n_raia} | Ultrafarma: {n_ultra} | Sem imagem: {n_sem}"
    )
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
