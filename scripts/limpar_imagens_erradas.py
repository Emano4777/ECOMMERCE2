#!/usr/bin/env python3
"""
Valida e corrige imagens incorretas no ecommerce (itens do catálogo ativo).

Problema detectado:
  - ecommerce_produto_imagens pode conter logos de farmácias ou placeholders
    genéricos (conteúdo da imagem é ruim, mesmo que a URL pareça OK)
  - medicamentos.imagem pode ter imagens auto-raspadas de baixa qualidade

O script faz:
  1. Coleta os EANs do catálogo ativo (estoque + automatiza_estoque)
  2. Para cada EAN que usa auto-ean/supabase como única fonte de imagem:
     - Valida com Claude Vision se é foto de produto real
     - Se inválida: remove de ecommerce_produto_imagens
  3. Para medicamentos.imagem com URL auto-raspada (supabase):
     - Valida com Claude Vision
     - Se inválida: limpa o campo (NULL)
  4. Para EANs que ficaram sem nenhuma imagem:
     - Tenta Cosmos API (mais confiável)
     - Tenta Serper (Google Images), se SERPER_API_KEY configurada
     - Valida candidatas com Claude Vision
     - Salva em ecommerce_produto_imagens para todas as lojas
        E em medicamentos.imagem se o medicamento existir

Uso (raiz do projeto):
  python scripts/limpar_imagens_erradas.py               # detecta e corrige
  python scripts/limpar_imagens_erradas.py --dry-run     # simula, sem alterar
  python scripts/limpar_imagens_erradas.py --stats       # estatísticas
  python scripts/limpar_imagens_erradas.py --skip-validate  # pula Claude Vision
  python scripts/limpar_imagens_erradas.py --ean EAN     # processa 1 EAN
  python scripts/limpar_imagens_erradas.py --limit N     # processa N EANs
"""

import os
import re
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
import urllib.parse
import psycopg2
import psycopg2.extras
from pathlib import Path
from dotenv import load_dotenv
import anthropic

# Fix encoding no Windows (PowerShell/cp1252)
sys.stdout.reconfigure(encoding="utf-8", errors="replace") if hasattr(sys.stdout, "reconfigure") else None

load_dotenv(Path(__file__).parent.parent / ".env")

DATABASE_URL  = os.environ["DATABASE_URL"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
COSMOS_TOKEN  = os.getenv("COSMOS_TOKEN", "").strip()
SERPER_KEY    = os.getenv("SERPER_API_KEY", "").strip()
MODEL         = "claude-haiku-4-5-20251001"

_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


def _digits(s):
    return re.sub(r"\D", "", s or "")


def _post_json(url, payload, headers=None, timeout=12):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={**(headers or {}), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


# ── PADRÕES PARA IDENTIFICAR URLs QUE PRECISAM DE VALIDAÇÃO VISUAL ───────────

# URLs que provavelmente têm conteúdo ruim (logo de farmácia, placeholder)
# e precisam ser validadas pelo Claude Vision
NEEDS_VALIDATION_RE = re.compile(
    r"supabase\.co/storage.*/auto-ean/"
    r"|supabase\.co/storage.*/medicamentos-auto",
    re.IGNORECASE,
)

# URLs que claramente são placeholders genéricos — deletar sem precisar de Vision
GENERIC_PLACEHOLDER_RE = re.compile(
    r"CAIXA_GEN[EÉ%C3%89]RICO.*POUPAQUI"
    r"|CAIXA_GEN.*POUPAQUI"
    r"|sem[_-]imagem|no[_-]image|placeholder|default[_-]product",
    re.IGNORECASE,
)

# URLs de farmácias concorrentes no próprio endereço — deletar sem Vision
COMPETITOR_URL_RE = re.compile(
    r"farmalan|avante[\-_]farm|drogasil|drogaraia|droga[\s_-]raia|paguemenos"
    r"|panvel|nissei|ultrafarma|drogaria[\-_]araujo|farmais|farmasesi"
    r"|drogarias?[\-_]s[aã]o[\-_]jo[aã]o|saojoao|heroos",
    re.IGNORECASE,
)


def url_needs_vision(url: str) -> bool:
    return bool(NEEDS_VALIDATION_RE.search(url or ""))


def url_is_bad_by_pattern(url: str) -> bool:
    u = url or ""
    return bool(GENERIC_PLACEHOLDER_RE.search(u) or COMPETITOR_URL_RE.search(u))


# ── BUSCA CATÁLOGO ATIVO ─────────────────────────────────────────────────────

# Query base: coleta todos os campos por EAN. Filtros aplicados em Python.
_CATALOG_BASE = """
WITH catalog AS (
    SELECT DISTINCT
        LTRIM(COALESCE(barras_norm, barras, ''), '0') AS ean_key,
        COALESCE(barras_norm, barras)                 AS ean_raw
    FROM estoque
    WHERE estoque > 0 AND COALESCE(barras_norm, barras, '') != ''

    UNION

    SELECT DISTINCT
        LTRIM(COALESCE(ean, ''), '0') AS ean_key,
        ean                           AS ean_raw
    FROM automatiza_estoque
    WHERE quantidade_estoque > 0 AND COALESCE(ean, '') != ''
)
SELECT
    c.ean_key,
    c.ean_raw,
    COALESCE(pc.descricao_canon, m.descricao, ae.descricao_produto) AS nome,
    COALESCE(elab.laboratorio, pc.laboratorio, m.laboratorio)       AS laboratorio,
    m.id            AS med_id,
    m.imagem        AS med_imagem,
    pc.imagem_cosmos,
    mi.cloudinary_url,
    epi.imagem_url  AS epi_url
FROM catalog c
LEFT JOIN medicamentos m
       ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = c.ean_key
LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
LEFT JOIN produto_canon pc
       ON LTRIM(COALESCE(pc.ean, ''), '0') = c.ean_key
      AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss')
LEFT JOIN ecommerce_lab_ean elab
       ON LTRIM(COALESCE(elab.ean, ''), '0') = c.ean_key
LEFT JOIN automatiza_estoque ae
       ON LTRIM(COALESCE(ae.ean, ''), '0') = c.ean_key
      AND ae.quantidade_estoque > 0
LEFT JOIN LATERAL (
    SELECT imagem_url
    FROM ecommerce_produto_imagens
    WHERE ean = c.ean_raw
    LIMIT 1
) epi ON TRUE
{where}
GROUP BY c.ean_key, c.ean_raw, pc.descricao_canon, m.descricao, ae.descricao_produto,
         elab.laboratorio, pc.laboratorio, m.laboratorio,
         m.id, m.imagem, pc.imagem_cosmos, mi.cloudinary_url, epi.imagem_url
ORDER BY c.ean_key
{limit}
"""

# Query focada: só EANs com supabase auto-ean (precisa validação visual urgente)
_CATALOG_SUPABASE = """
SELECT epi.ean AS ean_raw,
       LTRIM(epi.ean, '0') AS ean_key,
       epi.imagem_url AS epi_url,
       COALESCE(pc.descricao_canon, m.descricao) AS nome,
       COALESCE(elab.laboratorio, pc.laboratorio, m.laboratorio) AS laboratorio,
       m.id AS med_id,
       m.imagem AS med_imagem,
       pc.imagem_cosmos,
       mi.cloudinary_url
FROM ecommerce_produto_imagens epi
LEFT JOIN medicamentos m ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(epi.ean, '0')
LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
LEFT JOIN produto_canon pc ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(epi.ean, '0')
         AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss')
LEFT JOIN ecommerce_lab_ean elab ON LTRIM(COALESCE(elab.ean, ''), '0') = LTRIM(epi.ean, '0')
WHERE epi.imagem_url ILIKE '%%supabase%%auto-ean%%'
  AND epi.ean IN (
      SELECT COALESCE(barras_norm, barras) FROM estoque WHERE estoque > 0
      UNION
      SELECT ean FROM automatiza_estoque WHERE quantidade_estoque > 0
  )
{ean_filter}
ORDER BY epi.ean
{limit}
"""


def fetch_catalog_supabase(conn, ean_filter=None, limit=None) -> list[dict]:
    """EANs com supabase auto-ean em epi — precisam de validação visual."""
    ean_clause = ""
    params = []
    if ean_filter:
        ean_clause = "AND LTRIM(epi.ean, '0') = %s"
        params.append(_digits(ean_filter).lstrip("0"))
    lim = f"LIMIT {int(limit)}" if limit else ""
    sql = _CATALOG_SUPABASE.format(ean_filter=ean_clause, limit=lim)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def fetch_catalog_no_image(conn, ean_filter=None, limit=None) -> list[dict]:
    """EANs sem nenhuma fonte boa de imagem e com nome preenchido."""
    where_parts = [
        "NULLIF(TRIM(COALESCE(mi.cloudinary_url,'')), '') IS NULL",
        "NULLIF(TRIM(COALESCE(pc.imagem_cosmos,'')), '') IS NULL",
        "NULLIF(TRIM(COALESCE(m.imagem,'')), '') IS NULL",
        # epi nula ou é apenas CAIXA_GENÉRICO (placeholder, não conta como imagem)
        # %% = % literal no psycopg2
        "(epi.imagem_url IS NULL OR epi.imagem_url ILIKE '%%CAIXA_GEN%%POUPAQUI%%')",
        "LENGTH(TRIM(COALESCE(pc.descricao_canon, m.descricao, ae.descricao_produto, ''))) > 5",
    ]
    params = []
    if ean_filter:
        where_parts.append("c.ean_key = %s")
        params.append(_digits(ean_filter).lstrip("0"))

    where = "WHERE " + " AND ".join(where_parts)
    lim   = f"LIMIT {int(limit)}" if limit else ""
    sql = _CATALOG_BASE.format(where=where, limit=lim)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# ── VALIDAÇÃO CLAUDE VISION ───────────────────────────────────────────────────

def _url_is_loadable_image(url: str) -> bool:
    """Verifica via HEAD/GET se a URL retorna um content-type de imagem."""
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            ct = (r.headers.get("Content-Type") or "").lower()
            return ct.startswith("image/")
    except Exception:
        try:
            req2 = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req2, timeout=6) as r:
                raw = r.read(16)
                ct  = (r.headers.get("Content-Type") or "").lower()
                return ct.startswith("image/") or raw[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1")
        except Exception:
            return False


def validate_image_claude(image_url: str, product_name: str) -> bool:
    """
    Retorna True se a imagem for uma foto de produto real (embalagem/frasco/caixa).
    Retorna False para logos de farmácia, banners, placeholders ou imagens genéricas.
    Retorna False se a URL não carregar como imagem válida.
    """
    if not _url_is_loadable_image(image_url):
        return False
    try:
        msg = _client.messages.create(
            model=MODEL,
            max_tokens=30,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "url", "url": image_url}},
                    {
                        "type": "text",
                        "text": (
                            f"Produto: {product_name}\n"
                            "Esta imagem e a embalagem/foto de um produto farmaceutico ou de saude? "
                            "Responda apenas 'sim' ou 'nao'. "
                            "Diga 'nao' se for: logo de farmacia, banner, caixa generica sem marca "
                            "do produto, imagem com nome de rede de farmacia, ou placeholder."
                        ),
                    },
                ],
            }],
        )
        return (msg.content[0].text or "").strip().lower().startswith("sim")
    except Exception:
        return False


# ── FONTES CONFIÁVEIS PARA IMAGENS FARMACÊUTICAS ─────────────────────────────
# Bancos de dados de produtos sem branding de farmácia concorrente
TRUSTED_PHARMA_DOMAINS = [
    "consultaremedios.com.br",
    "precoremedio.com.br",
    "eanfacil.com.br",
    "bulas.med.br",
    "efarma.com.br",
    "saudedireta.com.br",
    "farmacenter.com.br",
    "remediobarato.com.br",
]

# Domínios de fabricantes conhecidos (imagens oficiais)
MANUFACTURER_DOMAINS = [
    "ems.com.br", "hypera.com.br", "biolab.com.br", "aché.com.br",
    "ache.com.br", "eurofarma.com.br", "zodiac.com.br", "cremer.com.br",
    "johnson.com.br", "reckittbenckiser.com.br", "bayer.com.br",
    "pfizer.com.br", "novartis.com.br", "sanofi.com.br", "neo.com.br",
    "neofarma.com.br", "cimed.com.br", "medley.com.br", "germed.com.br",
    "teuto.com.br", "vitamedic.com.br", "100nutrição.com.br",
]


def _scrape_image_from_page(page_url: str, ean: str) -> str | None:
    """Raspa a imagem de produto de uma página confiável usando dados estruturados."""
    try:
        req = urllib.request.Request(
            page_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PoupaquiBot/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "text/html" not in ctype:
                return None
            html = r.read(600_000).decode("utf-8", "ignore")
    except Exception:
        return None

    # 1. Procura og:image (Open Graph — muito confiável)
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        url = m.group(1).strip()
        if url.startswith("http") and not url_is_bad_by_pattern(url):
            return url

    # 2. Procura JSON-LD com campo "image"
    for ld in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE):
        try:
            obj = json.loads(ld)
            items = obj if isinstance(obj, list) else [obj]
            for item in items:
                for key in ("image", "logo"):
                    val = item.get(key)
                    if isinstance(val, str) and val.startswith("http") and not url_is_bad_by_pattern(val):
                        return val
                    if isinstance(val, dict):
                        u = val.get("url", "")
                        if u.startswith("http") and not url_is_bad_by_pattern(u):
                            return u
        except Exception:
            pass

    return None


def _claude_suggest_query(ean: str, nome: str, laboratorio: str = "") -> str:
    """
    Usa Claude (texto) para sugerir a query de busca mais precisa para a imagem
    do produto, com base no nome e EAN. Retorna só a query.
    """
    lab_hint = f" | Fabricante: {laboratorio}" if laboratorio else ""
    try:
        msg = _client.messages.create(
            model=MODEL,
            max_tokens=60,
            messages=[{
                "role": "user",
                "content": (
                    f"Produto farmaceutico: {nome}{lab_hint} | EAN: {ean}\n"
                    "Escreva APENAS a query Google Images ideal para encontrar a foto da "
                    "embalagem deste produto especifico (sem nome de farmacia). "
                    "Maximo 8 palavras, sem aspas, sem explicacoes."
                ),
            }],
        )
        return (msg.content[0].text or "").strip()
    except Exception:
        return f"{_digits(ean)} {nome}"[:60]


def fetch_cosmos_image(ean: str) -> str | None:
    if not COSMOS_TOKEN:
        return None
    ean_d = _digits(ean)
    if len(ean_d) < 8:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.cosmos.bluesoft.com.br/gtins/{ean_d}",
            headers={
                "X-Cosmos-Token": COSMOS_TOKEN,
                "User-Agent": "Cosmos-API-Request",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        thumb = (data.get("thumbnail") or "").strip()
        return thumb if thumb.startswith("http") else None
    except Exception:
        return None


def fetch_from_trusted_sources(ean: str, nome: str, laboratorio: str = "") -> str | None:
    """
    Busca imagem em fontes confiáveis (banco de dados farmacêuticos + fabricante)
    via Serper, com raspa de página para extrair foto estruturada.
    Não depende de domínio concorrente; valida o conteúdo HTML.
    """
    if not SERPER_KEY:
        return None
    ean_d = _digits(ean)
    if len(ean_d) < 8:
        return None

    # 1. Tenta sites de banco de dados farmacêutico (query por EAN)
    trusted_site_str = " OR ".join(f"site:{d}" for d in TRUSTED_PHARMA_DOMAINS[:4])
    queries_text = [
        f'"{ean_d}" ({trusted_site_str})',
        f'"{ean_d}" produto farmaceutico embalagem',
    ]

    # 2. Tenta site do fabricante se soubermos o lab
    if laboratorio:
        for dom in MANUFACTURER_DOMAINS:
            if any(part in dom for part in laboratorio.lower().split()):
                queries_text.insert(0, f'"{ean_d}" site:{dom}')
                break

    for q in queries_text:
        try:
            data = _post_json(
                "https://google.serper.dev/search",
                {"q": q, "num": 5, "gl": "br", "hl": "pt-br"},
                headers={"X-API-KEY": SERPER_KEY},
            )
        except Exception:
            continue
        for item in (data.get("organic") or [])[:5]:
            page_url = (item.get("link") or "").strip()
            if not page_url or url_is_bad_by_pattern(page_url):
                continue
            img = _scrape_image_from_page(page_url, ean_d)
            if img and _url_is_loadable_image(img):
                return img

    return None


def fetch_serper_images_multi(ean: str, nome: str, laboratorio: str = "") -> list[str]:
    """
    Retorna até 5 candidatas de imagem via Serper Google Images,
    usando query sugerida pelo Claude + query por EAN como fallback.
    Filtra concorrentes, mas não valida conteúdo — deixa para Claude Vision.
    """
    if not SERPER_KEY:
        return []
    ean_d = _digits(ean)
    if len(ean_d) < 8:
        return []

    # Claude sugere a query mais precisa
    claude_query = _claude_suggest_query(ean_d, nome, laboratorio) if nome else ean_d
    queries = list(dict.fromkeys([
        claude_query,
        f"{ean_d} {nome[:30]}".strip() if nome else ean_d,
        f'"{ean_d}"',
    ]))

    candidates = []
    for q in queries:
        try:
            data = _post_json(
                "https://google.serper.dev/images",
                {"q": q, "num": 10, "gl": "br", "hl": "pt-br"},
                headers={"X-API-KEY": SERPER_KEY},
            )
        except Exception:
            continue
        for item in (data.get("images") or [])[:10]:
            url  = (item.get("imageUrl") or "").strip()
            page = item.get("link") or ""
            if not url or url in candidates:
                continue
            if url_is_bad_by_pattern(url) or url_is_bad_by_pattern(page):
                continue
            candidates.append(url)
            if len(candidates) >= 5:
                return candidates

    return candidates


def find_best_image(ean: str, nome: str, laboratorio: str = "",
                    skip_validate: bool = False) -> tuple[str | None, str]:
    """
    Pipeline completo: Cosmos → fontes confiáveis → Serper multi-candidatas com Claude Vision.
    Retorna (url, fonte) ou (None, '').
    """
    # 1. Cosmos (mais confiável — imagens oficiais de produto)
    url = fetch_cosmos_image(ean)
    if url and _url_is_loadable_image(url):
        if skip_validate or validate_image_claude(url, nome):
            return url, "cosmos"

    # 2. Banco de dados farmacêutico confiável (raspa página)
    url = fetch_from_trusted_sources(ean, nome, laboratorio)
    if url:
        if skip_validate or validate_image_claude(url, nome):
            return url, "trusted-db"

    # 3. Serper com múltiplas candidatas — Claude Vision escolhe a primeira boa
    candidates = fetch_serper_images_multi(ean, nome, laboratorio)
    for url in candidates:
        if not _url_is_loadable_image(url):
            continue
        if skip_validate or validate_image_claude(url, nome):
            return url, "serper"

    return None, ""


# ── AÇÕES NO BANCO ────────────────────────────────────────────────────────────

def delete_epi_for_ean(conn, ean_raw: str, dry_run=False):
    if dry_run:
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ecommerce_produto_imagens WHERE ean = %s", (ean_raw,))
    conn.commit()


def clear_med_imagem(conn, med_id: int, dry_run=False):
    if dry_run:
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE medicamentos SET imagem = NULL WHERE id = %s", (med_id,))
    conn.commit()


def save_image_for_all_stores(conn, ean_key: str, image_url: str, dry_run=False) -> int:
    """Salva imagem em ecommerce_produto_imagens para todas as lojas com o EAN em estoque."""
    if dry_run:
        return 0
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ecommerce_produto_imagens (cnpjloja, ean, imagem_url)
            SELECT DISTINCT cnpjloja, ean, %s
            FROM (
                SELECT e.cnpj AS cnpjloja, COALESCE(e.barras_norm, e.barras) AS ean
                FROM estoque e
                WHERE e.estoque > 0
                  AND LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0') = LTRIM(%s, '0')
                UNION ALL
                SELECT ae.cnpj_loja, ae.ean
                FROM automatiza_estoque ae
                WHERE ae.quantidade_estoque > 0
                  AND LTRIM(COALESCE(ae.ean, ''), '0') = LTRIM(%s, '0')
            ) x
            WHERE cnpjloja IS NOT NULL AND ean IS NOT NULL AND TRIM(ean) <> ''
            ON CONFLICT (cnpjloja, ean) DO UPDATE
              SET imagem_url = EXCLUDED.imagem_url, updated_at = NOW()
        """, (image_url, ean_key, ean_key))
        saved = cur.rowcount
    conn.commit()
    return saved


def save_med_imagem(conn, med_id: int, image_url: str, dry_run=False):
    if dry_run or not med_id:
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE medicamentos SET imagem = %s WHERE id = %s", (image_url, med_id))
    conn.commit()


# ── STATS ─────────────────────────────────────────────────────────────────────

def show_stats(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM ecommerce_produto_imagens")
        total_epi = cur.fetchone()["n"]

        cur.execute(
            "SELECT COUNT(*) AS n FROM ecommerce_produto_imagens"
            " WHERE imagem_url ILIKE '%supabase%auto-ean%'"
        )
        supabase_epi = cur.fetchone()["n"]

        cur.execute(
            "SELECT COUNT(*) AS n FROM ecommerce_produto_imagens"
            " WHERE imagem_url ILIKE '%CAIXA_GEN%POUPAQUI%'"
        )
        generic_epi = cur.fetchone()["n"]

        cur.execute(
            "SELECT COUNT(DISTINCT m.id) AS n FROM medicamentos m"
            " WHERE m.imagem ILIKE '%supabase%auto%'"
            " AND m.barra_norm IN ("
            "   SELECT COALESCE(barras_norm, barras) FROM estoque WHERE estoque > 0"
            "   UNION SELECT ean FROM automatiza_estoque WHERE quantidade_estoque > 0"
            ")"
        )
        supabase_med = cur.fetchone()["n"]

    print(f"\n=== ESTATÍSTICAS ===")
    print(f"  ecommerce_produto_imagens total          : {total_epi}")
    print(f"  epi com supabase auto-ean (validar)      : {supabase_epi}")
    print(f"  epi com CAIXA_GENÉRICO (placeholder)     : {generic_epi}")
    print(f"  medicamentos.imagem auto-supabase (cat.) : {supabase_med}")


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run",       action="store_true")
    ap.add_argument("--stats",         action="store_true")
    ap.add_argument("--skip-validate", action="store_true", help="Pula Claude Vision")
    ap.add_argument("--ean",           type=str)
    ap.add_argument("--limit",         type=int)
    args = ap.parse_args()

    conn = psycopg2.connect(DATABASE_URL)

    if args.stats:
        show_stats(conn)
        conn.close()
        return

    # ── FASE 1: validar EANs com imagem supabase auto-ean ────────────────────
    print("FASE 1 — Validando imagens supabase auto-ean (podem conter logos de farmacia)...")
    supabase_rows = fetch_catalog_supabase(conn, ean_filter=args.ean, limit=args.limit)
    print(f"  {len(supabase_rows)} EANs com supabase auto-ean\n")

    stats = {"epi_removidas": 0, "med_limpas": 0, "salvas_epi": 0, "salvas_med": 0,
             "sem_imagem": 0, "ja_ok": 0}

    # EANs que perderam imagem na fase 1 e precisam de substituicao
    needs_replacement: list[dict] = []

    for i, row in enumerate(supabase_rows, 1):
        ean_key  = (row["ean_key"]  or "").lstrip("0")
        ean_raw  = row["ean_raw"]   or ean_key
        nome     = (row["nome"]     or "").strip()
        lab      = (row["laboratorio"] or "").strip()
        med_id   = row["med_id"]
        epi_url  = row["epi_url"]   or ""
        cosmos   = row["imagem_cosmos"]
        cloudify = row["cloudinary_url"]

        prefix = f"[F1 {i}/{len(supabase_rows)}] {ean_raw} | {nome[:45]}"

        # Se ja tem fonte boa, a imagem epi ficara invisivel pelo COALESCE
        if (cloudify and cloudify.strip()) or (cosmos and cosmos.strip()):
            print(f"{prefix}\n  ja tem cosmos/cloudinary — epi sera ignorada")
            stats["ja_ok"] += 1
            continue

        # Validar com Claude Vision
        if args.skip_validate:
            valid = True
        else:
            valid = validate_image_claude(epi_url, nome)
            print(f"{prefix}\n  vision={'OK' if valid else 'RUIM'}: {epi_url[:75]}")

        if not valid:
            delete_epi_for_ean(conn, ean_raw, dry_run=args.dry_run)
            stats["epi_removidas"] += 1
            if nome:
                needs_replacement.append(row)
        else:
            stats["ja_ok"] += 1

        time.sleep(0.2)

    # ── FASE 2: EANs sem nenhuma imagem boa (inclui os que perderam na fase 1) ─
    print(f"\nFASE 2 — Buscando imagens para EANs sem fonte valida...")
    no_image_rows = fetch_catalog_no_image(conn, ean_filter=args.ean, limit=args.limit)

    # Unir com os que perderam imagem na fase 1 (sem duplicar)
    seen_keys = {r["ean_key"] for r in no_image_rows}
    for r in needs_replacement:
        if r["ean_key"] not in seen_keys:
            no_image_rows.append(r)
            seen_keys.add(r["ean_key"])

    print(f"  {len(no_image_rows)} EANs precisando de imagem\n")

    for i, row in enumerate(no_image_rows, 1):
        ean_key  = (row["ean_key"] or "").lstrip("0")
        ean_raw  = row["ean_raw"]  or ean_key
        nome     = (row["nome"]    or "").strip()
        lab      = (row["laboratorio"] or "").strip()
        med_id   = row["med_id"]
        med_img  = row["med_imagem"]

        if not nome:
            continue  # sem nome nao ha como validar

        prefix = f"[F2 {i}/{len(no_image_rows)}] {ean_raw} | {nome[:45]}"

        # Verificar e limpar medicamentos.imagem supabase se existir
        if med_id and med_img and url_needs_validation(med_img):
            valid_med = args.skip_validate or validate_image_claude(med_img, nome)
            print(f"{prefix}\n  MED vision={'OK' if valid_med else 'RUIM'}: {med_img[:70]}")
            if not valid_med:
                clear_med_imagem(conn, med_id, dry_run=args.dry_run)
                stats["med_limpas"] += 1
                med_img = None

        # Buscar melhor imagem: Cosmos → trusted-db → Serper multi-candidata
        best_url, source = find_best_image(
            ean_raw or ean_key, nome, lab,
            skip_validate=args.skip_validate,
        )

        if not best_url:
            print(f"{prefix}\n  [X] nenhuma imagem encontrada")
            stats["sem_imagem"] += 1
            continue

        print(f"{prefix}\n  [{source}] OK: {best_url[:75]}")

        # Salvar em epi para todas as lojas
        saved = save_image_for_all_stores(conn, ean_key, best_url, dry_run=args.dry_run)
        stats["salvas_epi"] += saved
        print(f"    epi: {saved} lojas {'(dry-run)' if args.dry_run else 'salvos'}")

        # Salvar em medicamentos.imagem se nao tinha
        if med_id and not med_img:
            save_med_imagem(conn, med_id, best_url, dry_run=args.dry_run)
            stats["salvas_med"] += 1
            print(f"    med: atualizado {'(dry-run)' if args.dry_run else ''}")

        time.sleep(0.25)

    print(f"\n=== CONCLUÍDO {'(DRY-RUN)' if args.dry_run else ''} ===")
    print(f"  EPI removidas (logo/placeholder)  : {stats['epi_removidas']}")
    print(f"  medicamentos.imagem limpas        : {stats['med_limpas']}")
    print(f"  EPI salvas (novas imagens)        : {stats['salvas_epi']}")
    print(f"  medicamentos.imagem atualizadas   : {stats['salvas_med']}")
    print(f"  Sem imagem disponível             : {stats['sem_imagem']}")
    print(f"  EPI/MED ja com fonte boa          : {stats['ja_ok']}")
    conn.close()


def url_needs_validation(url: str) -> bool:
    return bool(NEEDS_VALIDATION_RE.search(url or ""))


if __name__ == "__main__":
    main()
