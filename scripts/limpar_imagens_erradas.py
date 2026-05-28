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


def _env_tokens(*names: str) -> list[str]:
    tokens = []
    seen = set()
    for name in names:
        raw = os.getenv(name, "")
        for token in re.split(r"[\s,;]+", raw):
            token = token.strip()
            if token and token not in seen:
                tokens.append(token)
                seen.add(token)
    return tokens


COSMOS_TOKENS = _env_tokens("COSMOS_TOKEN", "COSMOS_TOKENS")
SERPER_KEYS = _env_tokens("SERPER_API_KEY", "SERPER_API_KEYS")


def _digits(s):
    return re.sub(r"\D", "", s or "")


def _ean_variants(ean: str) -> list[str]:
    """
    Gera variantes úteis para busca.
    Muitos cadastros vêm como DUN-14 (1/2 + GTIN-13), com zeros à esquerda
    ou até código interno. Buscar apenas o valor bruto perde muitas imagens.
    """
    raw = _digits(ean)
    if not raw:
        return []
    variants = []

    def add(value: str):
        value = _digits(value).lstrip("0")
        if len(value) >= 8 and value not in variants:
            variants.append(value)

    add(raw)
    if len(raw) == 14:
        add(raw[1:])       # indicador logístico + EAN-13
        add(raw[-13:])
        add(raw[-12:])
    elif len(raw) == 13:
        add(raw[-12:])
    elif len(raw) > 14:
        add(raw[-14:])
        add(raw[-13:])
        add(raw[-12:])
    return variants


_NAME_CLEAN_REPLACEMENTS = [
    (r"\bc[/\s]*(\d+)\b", r"com \1"),
    (r"\bcpd?s?\b|\bcpr?s?\b|\bcomp\.?\b", "comprimidos"),
    (r"\bfr\b", "frasco"),
    (r"\bund?\b|\bunid?\b", "unidades"),
]


def _clean_product_query(nome: str, laboratorio: str = "") -> str:
    text = (nome or "").strip()
    text = re.sub(r"\s+", " ", text)
    for pat, repl in _NAME_CLEAN_REPLACEMENTS:
        text = re.sub(pat, repl, text, flags=re.IGNORECASE)
    # Remove lixo operacional comum, mas preserva medida/sabor/tamanho.
    text = re.sub(r"\b(display|pote\s+c\s+\d+|sortid[ao]s?)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    if laboratorio and laboratorio.lower() not in text.lower():
        text = f"{text} {laboratorio}".strip()
    return text[:110]


def _image_search_queries(ean: str, nome: str, laboratorio: str = "") -> list[str]:
    variants = _ean_variants(ean)
    clean_name = _clean_product_query(nome, laboratorio)
    queries = []
    if clean_name:
        # Nome primeiro costuma recuperar itens com EAN/DUN mal cadastrado.
        queries.extend([
            f"{clean_name} embalagem",
            f"{clean_name} produto",
            f"{clean_name} foto",
        ])
    for v in variants[:4]:
        queries.append(f'"{v}"')
        if clean_name:
            queries.append(f"{v} {clean_name[:55]}")
    if nome:
        try:
            q = _claude_suggest_query(variants[0] if variants else ean, nome, laboratorio)
            if q:
                queries.insert(0, q)
        except Exception:
            pass
    return list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))


def _post_json(url, payload, headers=None, timeout=12):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={**(headers or {}), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _post_json_with_api_keys(url, payload, header_name: str, api_keys: list[str], timeout=12):
    if not api_keys:
        return None
    last_error = None
    for api_key in api_keys:
        try:
            return _post_json(
                url,
                payload,
                headers={header_name: api_key},
                timeout=timeout,
            )
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 400 and isinstance(payload, dict) and "q" in payload:
                try:
                    return _post_json(
                        url,
                        {"q": payload["q"], "num": payload.get("num", 10)},
                        headers={header_name: api_key},
                        timeout=timeout,
                    )
                except urllib.error.HTTPError as exc2:
                    last_error = exc2
                except Exception as exc2:
                    last_error = exc2
                continue
            if exc.code in (401, 403, 429):
                continue
            raise
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return None


# ── PADRÕES PARA IDENTIFICAR URLs QUE PRECISAM DE VALIDAÇÃO VISUAL ───────────

# URLs que provavelmente têm conteúdo ruim (logo de farmácia, placeholder)
# e precisam ser validadas pelo Claude Vision
NEEDS_VALIDATION_RE = re.compile(
    r"supabase\.co/storage.*/auto-ean/"
    r"|supabase\.co/storage.*/medicamentos-auto"
    r"|/google_auto/|/pedidoeletronico/google_auto/",
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
    r"|drogarias?[\-_]s[aã]o[\-_]jo[aã]o|saojoao|heroos"
    r"|promofarma|drogariacatarinense|drogaria[\-_]?catarinense"
    r"|drogariavenancio|venancio|farmaponte|farmacia[\-_]?ponte"
    r"|farmaciabrito|farmacia[\-_]?brito|drogariasp|drogaria[\-_]?sp"
    r"|drogarias?[\-_]?pacheco|drogarias?[\-_]?tamoio|drogarias?[\-_]?globo"
    r"|drogarias?[\-_]?ultra[\-_]?popular|drogarias?[\-_]?campe[aã]"
    r"|drogaria|droga[a-z0-9_-]*|farmacia|pharma|farma\d+|comprar[\-_]?na[\-_]?farma"
    r"|[\./_-]farma[a-z0-9_-]*",
    re.IGNORECASE,
)


def url_needs_vision(url: str) -> bool:
    return bool(NEEDS_VALIDATION_RE.search(url or ""))


EXTRA_BAD_IMAGE_REF_RE = re.compile(
    r"avante[\s\-_]*farm|pague[\s\-_]*menos|droga[\s\-_]*raia|drogaria[\s\-_]*araujo"
    r"|imagem\s+indispon[iÃ­]vel|sem\s+imagem|sem\s+imagem\s+de\s+divulga[cÃ§][aÃ£]o"
    r"|para\s+que\s+serve|banner|promo[cÃ§][aÃ£]o|oferta|delivery|entrega|frete"
    r"|placeholder|default\s+product",
    re.IGNORECASE,
)


def image_ref_is_bad_by_pattern(*values: str) -> bool:
    u = " ".join(v or "" for v in values)
    return bool(
        GENERIC_PLACEHOLDER_RE.search(u)
        or COMPETITOR_URL_RE.search(u)
        or EXTRA_BAD_IMAGE_REF_RE.search(u)
    )


def url_is_bad_by_pattern(url: str) -> bool:
    return image_ref_is_bad_by_pattern(url)


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

# Query focada: EANs ÚNICOS com epi que precisa validação visual (qualquer URL, não só supabase)
# Deduplicado por EAN — valida a imagem uma só vez, deleta para todas as lojas se ruim.
_CATALOG_VALIDATE_EPI = """
SELECT DISTINCT ON (LTRIM(epi.ean, '0'))
       epi.ean                                                       AS ean_raw,
       LTRIM(epi.ean, '0')                                          AS ean_key,
       epi.imagem_url                                               AS epi_url,
       COALESCE(pc.descricao_canon, m.descricao)                   AS nome,
       COALESCE(elab.laboratorio, pc.laboratorio, m.laboratorio)   AS laboratorio,
       m.id                                                         AS med_id,
       m.imagem                                                     AS med_imagem,
       pc.imagem_cosmos,
       mi.cloudinary_url
FROM ecommerce_produto_imagens epi
LEFT JOIN medicamentos m    ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(epi.ean, '0')
LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
LEFT JOIN produto_canon pc  ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(epi.ean, '0')
                            AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss')
LEFT JOIN ecommerce_lab_ean elab ON LTRIM(COALESCE(elab.ean, ''), '0') = LTRIM(epi.ean, '0')
WHERE epi.imagem_url IS NOT NULL
  AND TRIM(epi.imagem_url) != ''
  -- Só valida se NÃO há fonte confiável sobrepondo (cosmos/cloudinary já tapa o problema)
  AND NULLIF(TRIM(COALESCE(pc.imagem_cosmos, '')), '') IS NULL
  AND NULLIF(TRIM(COALESCE(mi.cloudinary_url, '')), '') IS NULL
  -- Pula EANs já validados como OK nos últimos 30 dias
  AND (epi.validado_em IS NULL OR epi.validado_em < NOW() - INTERVAL '30 days')
{ean_filter}
ORDER BY LTRIM(epi.ean, '0'), epi.ean
{limit}
"""

_CATALOG_VALIDATE_DISPLAY = """
WITH catalog AS MATERIALIZED (
    SELECT LTRIM(COALESCE(barras_norm, barras, ''), '0') AS ean_key,
           MIN(COALESCE(barras_norm, barras)) AS ean_raw,
           MAX(descricao) AS nome_estoque
    FROM estoque
    WHERE estoque > 0 AND COALESCE(barras_norm, barras, '') != ''
    GROUP BY LTRIM(COALESCE(barras_norm, barras, ''), '0')
    UNION ALL
    SELECT LTRIM(COALESCE(ean, ''), '0') AS ean_key,
           MIN(ean) AS ean_raw,
           MAX(descricao_produto) AS nome_estoque
    FROM automatiza_estoque
    WHERE quantidade_estoque > 0 AND COALESCE(ean, '') != ''
    GROUP BY LTRIM(COALESCE(ean, ''), '0')
),
dedup AS MATERIALIZED (
    SELECT ean_key, MIN(ean_raw) AS ean_raw, MAX(nome_estoque) AS nome_estoque
    FROM catalog
    GROUP BY ean_key
)
SELECT
    c.ean_raw,
    c.ean_key,
    COALESCE(pc.descricao_canon, m.descricao, c.nome_estoque) AS nome,
    COALESCE(elab.laboratorio, pc.laboratorio, m.laboratorio) AS laboratorio,
    m.id AS med_id,
    m.imagem AS med_imagem,
    pc.imagem_cosmos,
    mi.cloudinary_url,
    epi.imagem_url AS epi_url,
    COALESCE(mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), epi.imagem_url) AS displayed_url,
    CASE
        WHEN NULLIF(TRIM(COALESCE(mi.cloudinary_url, '')), '') IS NOT NULL THEN 'medicamentos_imagens'
        WHEN NULLIF(TRIM(COALESCE(pc.imagem_cosmos, '')), '') IS NOT NULL THEN 'produto_canon'
        WHEN NULLIF(TRIM(COALESCE(m.imagem, '')), '') IS NOT NULL THEN 'medicamentos'
        WHEN NULLIF(TRIM(COALESCE(epi.imagem_url, '')), '') IS NOT NULL THEN 'ecommerce_produto_imagens'
        ELSE ''
    END AS image_source
FROM dedup c
LEFT JOIN LATERAL (
    SELECT id, descricao, laboratorio, imagem
    FROM medicamentos
    WHERE LTRIM(COALESCE(barra_norm, barra, ''), '0') = c.ean_key
    ORDER BY id
    LIMIT 1
) m ON TRUE
LEFT JOIN LATERAL (
    SELECT cloudinary_url
    FROM medicamentos_imagens
    WHERE medicamento_id = m.id
      AND NULLIF(TRIM(COALESCE(cloudinary_url, '')), '') IS NOT NULL
    ORDER BY created_at DESC NULLS LAST
    LIMIT 1
) mi ON TRUE
LEFT JOIN LATERAL (
    SELECT descricao_canon, laboratorio, imagem_cosmos
    FROM produto_canon
    WHERE LTRIM(COALESCE(ean, ''), '0') = c.ean_key
      AND fonte NOT IN ('cosmos_miss', 'ia_miss')
    ORDER BY atualizado_em DESC NULLS LAST
    LIMIT 1
) pc ON TRUE
LEFT JOIN LATERAL (
    SELECT imagem_url
    FROM ecommerce_produto_imagens
    WHERE LTRIM(COALESCE(ean, ''), '0') = c.ean_key
      AND NULLIF(TRIM(COALESCE(imagem_url, '')), '') IS NOT NULL
    ORDER BY updated_at DESC NULLS LAST
    LIMIT 1
) epi ON TRUE
LEFT JOIN ecommerce_lab_ean elab ON LTRIM(COALESCE(elab.ean, ''), '0') = c.ean_key
WHERE NULLIF(TRIM(COALESCE(mi.cloudinary_url, pc.imagem_cosmos, m.imagem, epi.imagem_url, '')), '') IS NOT NULL
{ean_filter}
ORDER BY c.ean_key
{limit}
"""


def fetch_epi_to_validate(conn, ean_filter=None, limit=None) -> list[dict]:
    """EANs únicos com epi sem cobertura de cosmos/cloudinary — precisam validação visual."""
    ean_clause = ""
    params = []
    if ean_filter:
        ean_clause = "AND LTRIM(COALESCE(epi.ean, ''), '0') = %s"
        params.append(_digits(ean_filter).lstrip("0"))
    lim = f"LIMIT {int(limit)}" if limit else ""
    sql = _CATALOG_VALIDATE_EPI.format(ean_filter=ean_clause, limit=lim)
    print("  [DB] Executando query de validacao...", flush=True)
    t0 = time.time()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SET LOCAL statement_timeout = 0")
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    print(f"  [DB] {len(rows)} linhas retornadas em {time.time()-t0:.1f}s", flush=True)
    return rows


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
    print("  [DB] Executando query sem-imagem (pode demorar)...", flush=True)
    t0 = time.time()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    print(f"  [DB] {len(rows)} linhas retornadas em {time.time()-t0:.1f}s", flush=True)
    return rows


# ── VALIDAÇÃO CLAUDE VISION ───────────────────────────────────────────────────

def dedupe_rows_by_ean(rows: list[dict]) -> list[dict]:
    deduped = []
    seen = set()
    for row in rows:
        key = (row.get("ean_key") or _digits(row.get("ean_raw"))).lstrip("0")
        if not key or key in seen:
            continue
        deduped.append(row)
        seen.add(key)
    return deduped


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
    if image_ref_is_bad_by_pattern(image_url) or not _url_is_loadable_image(image_url):
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
                            f"Produto esperado: {product_name}\n"
                            "Esta imagem mostra a embalagem/foto deste produto especifico, "
                            "compativel com o nome esperado? "
                            "Responda apenas 'sim' ou 'nao'. "
                            "Diga 'nao' se for outro produto, outra marca/laboratorio, logo de farmacia, "
                            "banner informativo, imagem com texto 'para que serve', caixa generica sem marca "
                            "do produto, imagem indisponivel, ou placeholder."
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
    "saudedireta.com.br",
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
    if not COSMOS_TOKENS:
        return None
    variants = _ean_variants(ean)
    if not variants:
        return None
    for ean_d in variants:
        for token in COSMOS_TOKENS:
            try:
                req = urllib.request.Request(
                    f"https://api.cosmos.bluesoft.com.br/gtins/{ean_d}",
                    headers={
                        "X-Cosmos-Token": token,
                        "User-Agent": "Cosmos-API-Request",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read().decode("utf-8", "ignore"))
                thumb = (data.get("thumbnail") or "").strip()
                if thumb.startswith("http"):
                    return thumb
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 429):
                    continue
                break
            except Exception:
                continue
    return None


def fetch_from_trusted_sources(ean: str, nome: str, laboratorio: str = "") -> str | None:
    """
    Busca imagem em fontes confiáveis (banco de dados farmacêuticos + fabricante)
    via Serper, com raspa de página para extrair foto estruturada.
    Não depende de domínio concorrente; valida o conteúdo HTML.
    """
    if not SERPER_KEYS:
        return None
    variants = _ean_variants(ean)
    if not variants:
        return None

    # 1. Tenta sites de banco de dados farmacêutico (query por EAN)
    trusted_site_str = " OR ".join(f"site:{d}" for d in TRUSTED_PHARMA_DOMAINS[:4])
    clean_name = _clean_product_query(nome, laboratorio)
    queries_text = []
    for ean_d in variants[:4]:
        queries_text.extend([
            f'"{ean_d}" ({trusted_site_str})',
            f'"{ean_d}" produto farmaceutico embalagem',
        ])
    if clean_name:
        queries_text.append(f"{clean_name} ({trusted_site_str})")
    queries_text = list(dict.fromkeys(queries_text))

    # 2. Tenta site do fabricante se soubermos o lab
    if laboratorio:
        for dom in MANUFACTURER_DOMAINS:
            if any(part in dom for part in laboratorio.lower().split()):
                for ean_d in variants[:3]:
                    queries_text.insert(0, f'"{ean_d}" site:{dom}')
                if clean_name:
                    queries_text.insert(0, f"{clean_name} site:{dom}")
                break

    for q in queries_text:
        try:
            data = _post_json_with_api_keys(
                "https://google.serper.dev/search",
                {"q": q, "num": 5, "gl": "br", "hl": "pt-br"},
                "X-API-KEY",
                SERPER_KEYS,
            )
        except Exception:
            continue
        for item in (data.get("organic") or [])[:5]:
            page_url = (item.get("link") or "").strip()
            title = (item.get("title") or "").strip()
            snippet = (item.get("snippet") or "").strip()
            if not page_url or image_ref_is_bad_by_pattern(page_url, title, snippet):
                continue
            img = _scrape_image_from_page(page_url, variants[0])
            if img and _url_is_loadable_image(img):
                return img

    return None


def fetch_serper_images_multi(ean: str, nome: str, laboratorio: str = "") -> list[str]:
    """
    Retorna até 5 candidatas de imagem via Serper Google Images,
    usando query sugerida pelo Claude + query por EAN como fallback.
    Filtra concorrentes, mas não valida conteúdo — deixa para Claude Vision.
    """
    if not SERPER_KEYS:
        return []
    variants = _ean_variants(ean)
    if not variants:
        return []

    queries = _image_search_queries(ean, nome, laboratorio)

    candidates = []
    for q in queries:
        try:
            data = _post_json_with_api_keys(
                "https://google.serper.dev/images",
                {"q": q, "num": 20, "gl": "br", "hl": "pt-br"},
                "X-API-KEY",
                SERPER_KEYS,
            )
        except Exception:
            continue
        for item in (data.get("images") or [])[:20]:
            url  = (item.get("imageUrl") or "").strip()
            page = item.get("link") or ""
            title = item.get("title") or ""
            if not url or url in candidates:
                continue
            if image_ref_is_bad_by_pattern(url, page, title):
                continue
            candidates.append(url)
            if len(candidates) >= 10:
                return candidates

    # Fallback: busca textual normal. Alguns produtos não aparecem no endpoint
    # /images, mas páginas orgânicas trazem imageUrl/thumbnail ou og:image.
    for q in queries:
        try:
            data = _post_json_with_api_keys(
                "https://google.serper.dev/search",
                {"q": q, "num": 10, "gl": "br", "hl": "pt-br"},
                "X-API-KEY",
                SERPER_KEYS,
            )
        except Exception:
            continue
        blocks = []
        kg = data.get("knowledgeGraph") or {}
        if kg:
            blocks.append(kg)
        blocks.extend(data.get("organic") or [])
        blocks.extend(data.get("places") or [])
        for item in blocks[:12]:
            title = item.get("title") or ""
            page = item.get("link") or item.get("website") or ""
            snippet = item.get("snippet") or item.get("description") or ""
            for key in ("imageUrl", "thumbnailUrl", "thumbnail", "image"):
                url = (item.get(key) or "").strip()
                if url and url.startswith("http") and url not in candidates:
                    if not image_ref_is_bad_by_pattern(url, page, title, snippet):
                        candidates.append(url)
                        if len(candidates) >= 10:
                            return candidates
            if page and not image_ref_is_bad_by_pattern(page, title, snippet):
                img = _scrape_image_from_page(page, variants[0])
                if img and img not in candidates and _url_is_loadable_image(img):
                    candidates.append(img)
                    if len(candidates) >= 10:
                        return candidates

    return candidates


_STOP_NAME_TOKENS = {
    "com", "sem", "para", "por", "das", "dos", "fr", "frasco", "caixa", "unidade",
    "unidades", "c", "cp", "cpr", "comprimido", "comprimidos", "ml", "mg", "g",
    "solucao", "xpe", "xarope", "gotas", "gts",
}


def _norm_hint_text(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def product_hint_matches_url(image_url: str, nome: str) -> bool:
    text = _norm_hint_text(urllib.parse.unquote(image_url or ""))
    name_text = _norm_hint_text(nome or "")
    if not text or not name_text:
        return False
    name_tokens = [
        t for t in name_text.split()
        if len(t) >= 4 and t not in _STOP_NAME_TOKENS and not t.isdigit()
    ]
    if not name_tokens or name_tokens[0] not in text:
        return False
    measures = re.findall(r"\d+\s*(?:ml|mg|g|cp|cpr)", name_text)
    if measures:
        compact_text = text.replace(" ", "")
        return any(m.replace(" ", "") in compact_text for m in measures)
    return sum(1 for t in name_tokens[:4] if t in text) >= min(2, len(name_tokens))


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
        if product_hint_matches_url(url, nome):
            return url, "serper-hint"

    return None, ""


# ── AÇÕES NO BANCO ────────────────────────────────────────────────────────────

def delete_epi_for_ean(conn, ean_raw: str, dry_run=False):
    if dry_run:
        return
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM ecommerce_produto_imagens WHERE LTRIM(COALESCE(ean, ''), '0') = LTRIM(%s, '0')",
            (ean_raw,),
        )
    conn.commit()


def clear_med_imagem(conn, med_id: int, dry_run=False):
    if dry_run:
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE medicamentos SET imagem = NULL WHERE id = %s", (med_id,))
    conn.commit()


def mark_epi_validated(conn, ean_key: str, dry_run=False):
    """Marca todas as linhas de ecommerce_produto_imagens deste EAN como validadas agora."""
    if dry_run:
        return
    ean_norm = _digits(ean_key).lstrip("0")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ecommerce_produto_imagens SET validado_em = NOW()"
            " WHERE LTRIM(COALESCE(ean, ''), '0') = %s",
            (ean_norm,),
        )
    conn.commit()


def clear_bad_sources_for_ean(conn, ean_key: str, bad_url: str, dry_run=False) -> dict:
    """Remove a mesma URL ruim de todas as fontes que podem sobrepor a imagem nova."""
    counts = {"epi": 0, "med": 0, "canon": 0, "cloudinary": 0}
    if dry_run:
        return counts
    ean_norm = _digits(ean_key).lstrip("0")
    bad_url = (bad_url or "").strip()
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM ecommerce_produto_imagens
             WHERE LTRIM(COALESCE(ean, ''), '0') = %s
            """,
            (ean_norm,),
        )
        counts["epi"] = cur.rowcount

        cur.execute(
            """
            UPDATE medicamentos
               SET imagem = NULL
             WHERE LTRIM(COALESCE(barra_norm, barra, ''), '0') = %s
               AND (%s = '' OR imagem = %s OR imagem ILIKE '%%farmalan%%' OR imagem ILIKE '%%avante%%')
            """,
            (ean_norm, bad_url, bad_url),
        )
        counts["med"] = cur.rowcount

        cur.execute(
            """
            UPDATE produto_canon
               SET imagem_cosmos = NULL, atualizado_em = NOW()
             WHERE LTRIM(COALESCE(ean, ''), '0') = %s
               AND (%s = '' OR imagem_cosmos = %s OR imagem_cosmos ILIKE '%%farmalan%%' OR imagem_cosmos ILIKE '%%avante%%')
            """,
            (ean_norm, bad_url, bad_url),
        )
        counts["canon"] = cur.rowcount

        cur.execute(
            """
            DELETE FROM medicamentos_imagens mi
             USING medicamentos m
             WHERE mi.medicamento_id = m.id
               AND LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = %s
               AND (%s = '' OR mi.cloudinary_url = %s OR mi.cloudinary_url ILIKE '%%farmalan%%' OR mi.cloudinary_url ILIKE '%%avante%%')
            """,
            (ean_norm, bad_url, bad_url),
        )
        counts["cloudinary"] = cur.rowcount
    conn.commit()
    return counts


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

def save_med_imagem_for_ean(conn, ean_key: str, image_url: str, dry_run=False) -> int:
    if dry_run:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE medicamentos
               SET imagem = %s
             WHERE LTRIM(COALESCE(barra_norm, barra, ''), '0') = LTRIM(%s, '0')
            """,
            (image_url, ean_key),
        )
        saved = cur.rowcount
    conn.commit()
    return saved


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

    # ── FASE 1: validar EANs com epi sem cobertura de cosmos/cloudinary ────────
    # Deduplicado por EAN — Claude Vision chamado 1x por EAN, não 1x por loja
    print("FASE 1 - Validando imagem exibida por EAN unico...")
    epi_rows = fetch_epi_to_validate(conn, ean_filter=args.ean, limit=args.limit)
    print(f"  {len(epi_rows)} EANs unicos para validar\n")

    stats = {"epi_eans_removidos": 0, "med_limpas": 0, "salvas_epi": 0, "salvas_med": 0,
             "fontes_limpas": 0, "sem_imagem": 0, "ja_ok": 0}

    # EANs que perderam imagem na fase 1 e precisam de substituicao
    needs_replacement: list[dict] = []

    for i, row in enumerate(epi_rows, 1):
        ean_key  = (row["ean_key"]  or "").lstrip("0")
        ean_raw  = row["ean_raw"]   or ean_key
        nome     = (row["nome"]     or "").strip()
        lab      = (row["laboratorio"] or "").strip()
        med_id   = row["med_id"]
        img_url  = row.get("displayed_url") or row.get("epi_url") or ""
        img_src  = row.get("image_source") or ""

        prefix = f"[F1 {i}/{len(epi_rows)}] {ean_raw} | {nome[:45]}"

        # Validar com Claude Vision
        if args.skip_validate:
            valid = True
        elif image_ref_is_bad_by_pattern(img_url):
            valid = False
            print(f"{prefix}\n  padrao=RUIM ({img_src}): {img_url[:75]}")
        else:
            valid = validate_image_claude(img_url, nome)
            print(f"{prefix}\n  vision={'OK' if valid else 'RUIM'} ({img_src}): {img_url[:75]}")

        if not valid:
            # Remove TODAS as lojas deste EAN de uma vez (não só uma)
            cleared = clear_bad_sources_for_ean(conn, ean_key or ean_raw, img_url, dry_run=args.dry_run)
            stats["fontes_limpas"] += sum(cleared.values())
            stats["epi_eans_removidos"] += 1
            if nome:
                needs_replacement.append(row)
        else:
            mark_epi_validated(conn, ean_key or ean_raw, dry_run=args.dry_run)
            stats["ja_ok"] += 1

        time.sleep(0.2)

    # ── FASE 2: EANs sem nenhuma imagem boa (inclui os que perderam na fase 1) ─
    print(f"\nFASE 2 — Buscando imagens para EANs sem fonte valida...")
    no_image_rows = dedupe_rows_by_ean(fetch_catalog_no_image(conn, ean_filter=args.ean, limit=args.limit))

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
        if med_id and med_img and (url_needs_validation(med_img) or image_ref_is_bad_by_pattern(med_img)):
            valid_med = args.skip_validate or (not image_ref_is_bad_by_pattern(med_img) and validate_image_claude(med_img, nome))
            print(f"{prefix}\n  MED vision={'OK' if valid_med else 'RUIM'}: {med_img[:70]}")
            if not valid_med:
                cleared = clear_bad_sources_for_ean(conn, ean_key or ean_raw, med_img, dry_run=args.dry_run)
                stats["med_limpas"] += cleared.get("med", 0)
                stats["fontes_limpas"] += sum(cleared.values())
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

        # Salvar em epi para todas as lojas e marcar como validada
        saved = save_image_for_all_stores(conn, ean_key, best_url, dry_run=args.dry_run)
        stats["salvas_epi"] += saved
        print(f"    epi: {saved} lojas {'(dry-run)' if args.dry_run else 'salvos'}")
        mark_epi_validated(conn, ean_key, dry_run=args.dry_run)

        # Salvar em medicamentos.imagem para todos os cadastros duplicados do EAN
        med_saved = save_med_imagem_for_ean(conn, ean_key, best_url, dry_run=args.dry_run)
        stats["salvas_med"] += med_saved
        if med_saved:
            print(f"    med: {med_saved} cadastros atualizados {'(dry-run)' if args.dry_run else ''}")

        time.sleep(0.25)

    print(f"\n=== CONCLUÍDO {'(DRY-RUN)' if args.dry_run else ''} ===")
    print(f"  EPI EANs removidos (todas lojas)  : {stats['epi_eans_removidos']}")
    print(f"  Fontes ruins limpas               : {stats['fontes_limpas']}")
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
