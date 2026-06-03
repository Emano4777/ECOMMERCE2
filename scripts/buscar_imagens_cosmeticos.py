"""
buscar_imagens_cosmeticos.py — Busca imagens de produtos sem imagem via múltiplas fontes.

Fontes consultadas em ordem de prioridade:
  1. Cosmos/Bluesoft API  — base de códigos de barras, cobre marcas nacionais
  2. DSP (VTEX)           — Drogaria São Paulo (bom para Nivea, L'Oreal, etc.)
  3. Droga Raia           — scraping HTML/JSON-LD
  4. Serper Google Images — fallback mais amplo com rotação de chaves e limite

Regras de aceitação (herdadas de app.py):
  - Sem branding de farmácia concorrente na URL ou no OCR da imagem
  - Sem banners, promoções ou imagens não-produto (verificado via OCR)
  - Imagem de site confiável (não YouTube, Vecteezy, Flickr, Getty, etc.)
  - EAN presente na URL da imagem OU no domínio é de e-commerce conhecido

Como produto_canon é por EAN (não por loja), uma imagem encontrada aqui
beneficia automaticamente TODAS as lojas que têm aquele EAN em estoque.

Uso:
    py scripts/buscar_imagens_cosmeticos.py               # dry-run, 100 EANs cosméticos
    py scripts/buscar_imagens_cosmeticos.py --apply       # grava
    py scripts/buscar_imagens_cosmeticos.py --limite 200
    py scripts/buscar_imagens_cosmeticos.py --ean 7891150037465
    py scripts/buscar_imagens_cosmeticos.py --categoria higiene
    py scripts/buscar_imagens_cosmeticos.py --todos       # TODOS os EANs sem imagem
    py scripts/buscar_imagens_cosmeticos.py --sem-serper  # pula Serper (economia de cota)
    py scripts/buscar_imagens_cosmeticos.py -v
"""
from __future__ import annotations
import argparse
import datetime
import itertools
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from app import (
    db,
    _fetch_cosmos_api_image_url,
    _fetch_serper_image_result_url,
    _looks_like_other_pharmacy_brand,
    _image_has_other_pharmacy_text,
    _image_looks_non_product,
)
from scripts.revalidar_tarja_vtex import (
    _vtex_fetch,
    _raia_fetch,
    _extrair_dados_vtex,
    _upload_cloudinary,
    _PLACEHOLDERS_POUPAQUI,
    _UA,
)

# ── Palavras-chave por categoria ──────────────────────────────────────────────

_KEYWORDS: dict[str, list[str]] = {
    "cosmeticos": [
        "shampoo", " sh ", "condicionador", " cond ", "creme pent",
        "leave-in", "leave in", "mascara trat", "mascara capilar",
        "serum", "sérum", "elseve", "seda ", "pantene", "garnier",
        "siage", "tresemme", "loreal", "l oreal", "wella", "keune",
        "hidratante", "creme facial", "creme corp", "loção", "locao",
        "sabonete liq", "cicatricure", "nivea", "dove ", "giovanna",
        "natura ", "boticario", "avon ", "esmalte", "maquiagem",
        "batom", "blush", "rimel", "mascara olho", "perfume",
        "colonia", "agua de colonia", "oil body", "skala", "salon line",
        "phytoervas", "monange", "palmolive", "lux ", "koleston",
        "coloracao", "tintura", "creme alisante", "bothanico", "btox",
    ],
    "higiene": [
        "sabonete barra", "sabonete gel", "desodorante", "des rexona",
        "des nivea", "des dove", "des gilette", "absorvente", "absorv ",
        "preserv", "camisinha", "fio dental", "escova dent",
        "creme dent", "pasta dent", "enxaguante", "antisseptico buc",
        "algodao", "curativo", "band-aid", "repelente", "protetor solar",
        "fps", "protetor labial", "depilatorio", "depilatório",
        "barbeador", "aparelho barb", "lamina barb", "gillette",
    ],
    "puericultura": [
        "fralda", "lenco umed", "lenço umed", "mamadeira", "chupeta",
        "buba ", "mam ", "huggies", "pampers", "baby ", "bebê ", "bebe ",
        "recem nasc", "recém nasc", "pomada assad", "talco infantil",
        "aspirador nasal", "termometro", "termômetro",
    ],
}

_ALL_KEYWORDS = [kw for kws in _KEYWORDS.values() for kw in kws]

_SEM_IMAGEM_COND = """(
    pc.ean IS NULL
    OR pc.imagem_cosmos IS NULL
    OR TRIM(pc.imagem_cosmos) = ''
    OR pc.imagem_cosmos LIKE '%%CAIXA_GEN%%'
    OR pc.imagem_cosmos LIKE '%%ChatGPT_Image%%'
    OR pc.imagem_cosmos LIKE '%%44356%%'
    OR pc.imagem_cosmos LIKE '%%12466%%'
    OR pc.fonte = 'placeholder_broken'
)"""


# ── Validação de EAN comercial (dígito verificador GS1) ──────────────────────

def _ean_comercial(ean: str) -> bool:
    """True se o EAN é um código comercial válido: EAN-13 ou UPC-12 com dígito verificador correto.

    Elimina PLUs internos de farmácia (ex.: 1000000035520, 10000342, etc.)
    que passam na regex mas não têm check digit GS1 válido ou têm comprimento errado.
    """
    d = re.sub(r"\D", "", ean)
    # Padrão de PLU interno: começa com 1000 ou 2000 seguido de zeros (padding)
    if len(d) in (12, 13) and re.match(r"^[12]0{4,}", d):
        return False
    if len(d) == 13:
        pesos = [1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3]
        soma = sum(int(d[i]) * pesos[i] for i in range(12))
        return (10 - soma % 10) % 10 == int(d[12])
    if len(d) == 12:  # UPC-A
        pesos = [3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3]
        soma = sum(int(d[i]) * pesos[i] for i in range(11))
        return (10 - soma % 10) % 10 == int(d[11])
    return False


# ── Rotação de chaves Serper e Cosmos ─────────────────────────────────────────

def _serper_keys() -> list[str]:
    multi = [k.strip() for k in os.getenv("SERPER_API_KEYS", "").split(",") if k.strip()]
    single = os.getenv("SERPER_API_KEY", "").strip()
    keys = multi or ([single] if single else [])
    return keys


def _cosmos_tokens() -> list[str]:
    multi = [t.strip() for t in os.getenv("COSMOS_TOKENS", "").split(",") if t.strip()]
    single = os.getenv("COSMOS_TOKEN", "").strip()
    return multi or ([single] if single else [])


# ── Beleza na Web (VTEX) ──────────────────────────────────────────────────────

_BELEZA_BASE = "https://www.belezanaweb.com.br"
_VTEX_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

def _beleza_fetch(ean: str, verbose: bool = False) -> str | None:
    """Busca imagem real na Beleza na Web pelo EAN via VTEX Catalog API."""
    ean_digits = re.sub(r"\D", "", ean)
    if not ean_digits:
        return None
    urls_tentativas = [
        f"{_BELEZA_BASE}/api/catalog_system/pub/products/search?fq=alternateIdValues:{ean_digits}&_from=0&_to=0&sc=1",
        f"{_BELEZA_BASE}/api/catalog_system/pub/products/search?ft={ean_digits}&_from=0&_to=0&sc=1",
    ]
    for url in urls_tentativas:
        try:
            req = urllib.request.Request(url, headers=_VTEX_HEADERS)
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as exc:
            if verbose:
                print(f"    [beleza] {type(exc).__name__}: {exc}")
            continue
        if not isinstance(data, list) or not data:
            continue
        produto = data[0]
        for item in produto.get("items", []):
            for img in item.get("images") or []:
                img_url = (img.get("imageUrl") or "").strip()
                if not img_url.startswith("http"):
                    continue
                fname = img_url.split("?")[0].split("/")[-1].lower()
                img_text = img.get("imageText") or img.get("imageLabel") or ""
                if _looks_like_other_pharmacy_brand(img_url, fname, img_text):
                    if verbose:
                        print(f"    [beleza] branded, pulando: {img_url[:60]}")
                    continue
                if verbose:
                    print(f"    [beleza] imagem: {img_url[:70]}")
                return img_url
    return None


# ── Cosmos com rotação de tokens ──────────────────────────────────────────────

def _cosmos_fetch_rotating(ean: str, tokens: list[str], verbose: bool = False) -> str | None:
    """Tenta Cosmos com rotação de tokens — usa próximo se o atual estiver sem cota.

    Para EANs com zeros à esquerda (ex: 0000042277217 → EAN-8 42277217),
    tenta também a versão sem zeros para cobrir ambas as indexações.
    """
    ean_digits = re.sub(r"\D", "", ean)
    if not ean_digits or len(ean_digits) < 8:
        return None
    # Candidatos: EAN completo e sem zeros à esquerda (EAN-8 padded)
    ean_stripped = ean_digits.lstrip("0") or ean_digits
    candidatos = list(dict.fromkeys([ean_digits, ean_stripped]))

    for ean_q in candidatos:
        if len(ean_q) < 7:
            continue
        for token in tokens:
            try:
                req = urllib.request.Request(
                    f"https://api.cosmos.bluesoft.com.br/gtins/{ean_q}",
                    headers={
                        "X-Cosmos-Token": token,
                        "User-Agent": "Cosmos-API-Request",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    if r.status == 429:
                        if verbose:
                            print(f"    [cosmos] token {token[:8]} sem cota, trocando...")
                        continue
                    data = json.loads(r.read().decode("utf-8", "ignore"))
            except Exception as exc:
                if "429" in str(exc) or "quota" in str(exc).lower():
                    continue
                if verbose:
                    print(f"    [cosmos] {type(exc).__name__}: {exc}")
                break  # EAN não encontrado, tenta próximo candidato
            thumb = (data.get("thumbnail") or "").strip()
            if thumb.startswith("http"):
                if verbose:
                    print(f"    [cosmos] imagem: {thumb[:70]}")
                return thumb
    return None


# ── Serper com rotação de chaves ──────────────────────────────────────────────

# Sites de mídia/banco de imagem que nunca têm fotos reais de produto correlacionadas ao EAN
_SERPER_BLOCKED_DOMAINS = re.compile(
    r"youtube\.com|youtu\.be|vimeo\.com"
    r"|vecteezy\.com|freepik\.com|shutterstock\.com|gettyimages"
    r"|flickr\.com|instagram\.com|pinterest\."
    r"|wikimedia\.org|wikipedia\.org"
    r"|staticflickr\.com|pimg\.jp"
    r"|blogger\.com|blogspot\.com",
    re.IGNORECASE,
)

# Extensões de arquivo que não são imagens de produto
_SERPER_BLOCKED_EXT = re.compile(
    r"\.(gif|svg|webp\.gif|mp4|mov|avi)(\?|$)", re.IGNORECASE
)

# Domínios de e-commerce confiáveis para produtos brasileiros
_ECOMMERCE_CONFIAVEL_RE = re.compile(
    r"mercadolivre|meli\.com|mlstatic"
    r"|americanas\.|shoptime\.|submarino\."
    r"|magazineluiza|magalu\."
    r"|carrefour\.|extra\.|pontofrio\."
    r"|casasbahia\."
    r"|belezanaweb\.|sephora\.|netfarma\."
    r"|drogarmarys|drogasil|farmaponto"
    r"|paguemenos\."
    r"|zacaris\.|perfumaria\."
    r"|cdiscount\.|amazon\.",
    re.IGNORECASE,
)


def _serper_fetch_rotating(ean: str, nome: str, keys: list[str],
                            chaves_esgotadas: set[str],
                            verbose: bool = False) -> str | None:
    """Serper Google Images com rotação de chaves, queries progressivas e validação rigorosa.

    chaves_esgotadas é um set mutável compartilhado entre todos os EANs da sessão.
    Quando todas as chaves estiverem esgotadas, retorna None imediatamente para
    economizar chamadas de API.
    """
    chaves_disponiveis = [k for k in keys if k not in chaves_esgotadas]
    if not chaves_disponiveis:
        if verbose:
            print("    [serper] todas as chaves esgotadas, pulando")
        return None

    ean_digits = re.sub(r"\D", "", ean)
    if len(ean_digits) < 8:
        return None

    # EANs internos/PLU (< 8 dígitos padrão ou não EAN-8/13) são ignorados
    if len(ean_digits) not in (8, 12, 13) and len(ean_digits) < 12:
        if verbose:
            print(f"    [serper] EAN {ean_digits} ({len(ean_digits)} dígitos) parece interno, pulando")
        return None

    nome_curto = " ".join((nome or "").split()[:5])
    ean_stripped = ean_digits.lstrip("0") or ean_digits
    padded = ean_digits != ean_stripped  # EAN com zeros à esquerda

    queries = list(dict.fromkeys([
        # EAN original + nome (busca principal)
        f"{ean_digits} {nome_curto}".strip() if nome_curto else ean_digits,
        # Para EAN padded: busca por nome do produto (mais eficaz)
        f"{nome_curto} produto farmacia" if (padded and nome_curto) else None,
        # EAN sem zeros + nome
        f"{ean_stripped} {nome_curto}".strip() if (padded and ean_stripped != ean_digits) else None,
        f'"{ean_digits}"',
    ]))
    queries = [q for q in queries if q]  # remove None

    key_iter = iter(chaves_disponiveis)
    api_key = next(key_iter, None)

    for q in queries:
        # Avança para chave não esgotada
        while api_key and api_key in chaves_esgotadas:
            api_key = next(key_iter, None)
        if not api_key:
            break

        try:
            payload = json.dumps({"q": q, "num": 10, "gl": "br", "hl": "pt-br"}).encode()
            req = urllib.request.Request(
                "https://google.serper.dev/images",
                data=payload,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as exc:
            err_str = str(exc)
            if "429" in err_str or "quota" in err_str.lower():
                if verbose:
                    print(f"    [serper] chave {api_key[:8]} esgotada (429), trocando...")
                chaves_esgotadas.add(api_key)
                api_key = next(key_iter, None)
            else:
                if verbose:
                    print(f"    [serper] {type(exc).__name__}: {exc}")
            continue

        for item in (data.get("images") or [])[:10]:
            img_url  = item.get("imageUrl") or item.get("thumbnailUrl") or ""
            page_url = item.get("link") or ""
            title    = item.get("title") or ""
            if not img_url.startswith("http"):
                continue
            # Rejeita mídia/bancos de imagem
            if _SERPER_BLOCKED_DOMAINS.search(img_url) or _SERPER_BLOCKED_DOMAINS.search(page_url):
                continue
            if _SERPER_BLOCKED_EXT.search(img_url):
                continue
            # Rejeita farmácias concorrentes
            if _looks_like_other_pharmacy_brand(img_url, page_url, title):
                continue
            # Para EAN padded (zeros à esquerda), aceita também o EAN sem zeros na URL
            ean_na_url = (ean_digits in img_url.replace("-", "").replace("_", "")
                          or (padded and ean_stripped in img_url.replace("-", "").replace("_", "")))
            site_ok = bool(_ECOMMERCE_CONFIAVEL_RE.search(img_url) or _ECOMMERCE_CONFIAVEL_RE.search(page_url))
            # Para queries por nome (EAN padded), aceita de qualquer site de produto
            nome_query = padded and nome_curto and q.startswith(nome_curto[:6])
            if not ean_na_url and not site_ok and not nome_query:
                if verbose:
                    print(f"    [serper] rejeitada (sem EAN na URL e site desconhecido): {img_url[:70]}")
                continue
            if verbose:
                print(f"    [serper] candidata: {img_url[:70]}")
            return img_url

    remaining = len([k for k in keys if k not in chaves_esgotadas])
    if verbose and chaves_esgotadas:
        print(f"    [serper] {len(chaves_esgotadas)} chave(s) esgotada(s), {remaining} restante(s)")
    return None


# ── Busca de EANs sem imagem ──────────────────────────────────────────────────

def _buscar_eans(cur, categoria: str | None, limite: int, ean_filtro: str | None,
                 todos: bool = False) -> list[dict]:
    if ean_filtro:
        cur.execute("""
            SELECT ean, nome FROM (
                SELECT DISTINCT COALESCE(e.barras_norm, e.barras) AS ean,
                       e.descricao AS nome
                FROM estoque e
                WHERE COALESCE(e.barras_norm, e.barras) = %s AND e.estoque > 0
                UNION
                SELECT DISTINCT ae.ean, ae.descricao_produto AS nome
                FROM automatiza_estoque ae
                WHERE ae.ean = %s AND ae.quantidade_estoque > 0
            ) t LIMIT 1
        """, (ean_filtro, ean_filtro))
        rows = cur.fetchall()
        return [dict(r) for r in rows] if rows else [{"ean": ean_filtro, "nome": ean_filtro}]

    if todos:
        # Todos os EANs sem imagem — exclui medicamentos tarjados e genéricos
        # e exige 12-13 dígitos (EAN comercial; PLUs internos têm dígito verificador inválido)
        _EXCLUIR_MED = """
            AND NOT EXISTS (
                SELECT 1 FROM medicamentos m2
                WHERE LTRIM(COALESCE(m2.barra_norm, m2.barra, ''), '0') =
                      LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0')
                  AND (
                    m2.classe ILIKE '%%genéric%%'
                    OR m2.classe ILIKE '%%generic%%'
                    OR m2.descricao ILIKE '%%genérico%%'
                    OR m2.descricao ILIKE '%%generico%%'
                    OR m2.classe ILIKE '%%tarja%%'
                    OR m2.classe ILIKE '%%controla%%'
                  )
            )
        """
        _EXCLUIR_MED_AE = _EXCLUIR_MED.replace(
            "COALESCE(e.barras_norm, e.barras, '')", "ae.ean"
        )
        # SQL restringe a 12-13 dígitos; Python filtra dígito verificador GS1
        cur.execute(f"""
            SELECT DISTINCT ON (ean) ean, nome
            FROM (
                SELECT
                    COALESCE(e.barras_norm, e.barras) AS ean,
                    e.descricao AS nome
                FROM estoque e
                LEFT JOIN produto_canon pc ON pc.ean = COALESCE(e.barras_norm, e.barras)
                WHERE COALESCE(e.barras_norm, e.barras) IS NOT NULL
                  AND COALESCE(e.barras_norm, e.barras) ~ '^[0-9]{{12,13}}$'
                  AND e.estoque > 0
                  AND {_SEM_IMAGEM_COND}
                  {_EXCLUIR_MED}

                UNION

                SELECT
                    ae.ean,
                    ae.descricao_produto AS nome
                FROM automatiza_estoque ae
                LEFT JOIN produto_canon pc ON pc.ean = ae.ean
                WHERE ae.ean IS NOT NULL
                  AND ae.ean ~ '^[0-9]{{12,13}}$'
                  AND ae.quantidade_estoque > 0
                  AND {_SEM_IMAGEM_COND}
                  {_EXCLUIR_MED_AE}
            ) t
            ORDER BY ean
            LIMIT %s
        """, [limite * 3])  # busca 3x para compensar o filtro Python
        rows = cur.fetchall()
        # Filtra pelo dígito verificador GS1 — elimina PLUs internos
        validos = [dict(r) for r in rows if _ean_comercial(r["ean"])]
        return validos[:limite]

    keywords = _KEYWORDS.get(categoria, _ALL_KEYWORDS) if categoria else _ALL_KEYWORDS
    ilike_e  = " OR ".join(["e.descricao ILIKE %s"]          * len(keywords))
    ilike_ae = " OR ".join(["ae.descricao_produto ILIKE %s"] * len(keywords))
    params   = [f"%{kw}%" for kw in keywords]

    cur.execute(f"""
        SELECT DISTINCT ON (ean) ean, nome
        FROM (
            SELECT
                COALESCE(e.barras_norm, e.barras) AS ean,
                e.descricao AS nome
            FROM estoque e
            LEFT JOIN produto_canon pc ON pc.ean = COALESCE(e.barras_norm, e.barras)
            WHERE COALESCE(e.barras_norm, e.barras) IS NOT NULL
              AND COALESCE(e.barras_norm, e.barras) ~ '^[1-9][0-9]{{7,12}}$'
              AND e.estoque > 0
              AND ({ilike_e})
              AND {_SEM_IMAGEM_COND}

            UNION

            SELECT
                ae.ean,
                ae.descricao_produto AS nome
            FROM automatiza_estoque ae
            LEFT JOIN produto_canon pc ON pc.ean = ae.ean
            WHERE ae.ean IS NOT NULL
              AND ae.ean ~ '^[1-9][0-9]{{7,12}}$'
              AND ae.quantidade_estoque > 0
              AND ({ilike_ae})
              AND {_SEM_IMAGEM_COND}
        ) t
        ORDER BY ean
        LIMIT %s
    """, params + params + [limite])

    return [dict(r) for r in cur.fetchall()]


def _med_imagens_fetch(ean: str, cur) -> str | None:
    """Busca imagem já indexada em medicamentos_imagens pelo EAN (ex: Vitnatu, Principia)."""
    ean_stripped = re.sub(r"\D", "", ean).lstrip("0")
    if not ean_stripped:
        return None
    try:
        cur.execute("""
            SELECT mi.cloudinary_url
            FROM medicamentos m
            JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            WHERE LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = %s
              AND mi.cloudinary_url IS NOT NULL
              AND TRIM(mi.cloudinary_url) != ''
            LIMIT 1
        """, (ean_stripped,))
        r = cur.fetchone()
        return r["cloudinary_url"] if r else None
    except Exception:
        return None


def _salvar(cur, ean: str, nome: str, imagem: str, fonte: str, apply: bool,
            verbose: bool) -> bool:
    """Faz upload para Cloudinary e salva em produto_canon. Retorna True se gravou."""
    url_final = imagem
    if apply:
        url_cl = _upload_cloudinary(imagem, ean, verbose=verbose)
        if url_cl:
            url_final = url_cl
            if verbose:
                print(f"    [cloudinary OK] {url_cl[:70]}")
        else:
            # None = Cloudinary rejeitou (pessoa detectada, branding, erro de upload)
            # Não salva a URL externa de farmácia concorrente como fallback
            if verbose:
                print(f"    [cloudinary REJEITOU] imagem descartada, não salva")
            return False

    if apply:
        cur.execute("""
            UPDATE produto_canon
               SET imagem_cosmos = %s, fonte = %s, atualizado_em = NOW()
            WHERE ean = %s
              AND (
                imagem_cosmos IS NULL OR TRIM(imagem_cosmos) = ''
                OR imagem_cosmos LIKE '%%CAIXA_GEN%%'
                OR imagem_cosmos LIKE '%%ChatGPT_Image%%'
                OR imagem_cosmos LIKE '%%44356%%'
                OR imagem_cosmos LIKE '%%12466%%'
                OR fonte = 'placeholder_broken'
              )
        """, (url_final, fonte, ean))

        if cur.rowcount == 0:
            cur.execute("""
                INSERT INTO produto_canon (ean, descricao_canon, imagem_cosmos, fonte, atualizado_em)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (ean) DO UPDATE
                  SET imagem_cosmos = EXCLUDED.imagem_cosmos,
                      fonte         = EXCLUDED.fonte,
                      atualizado_em = NOW()
                WHERE produto_canon.imagem_cosmos IS NULL
                   OR TRIM(produto_canon.imagem_cosmos) = ''
                   OR produto_canon.imagem_cosmos LIKE '%%CAIXA_GEN%%'
                   OR produto_canon.imagem_cosmos LIKE '%%ChatGPT_Image%%'
                   OR produto_canon.imagem_cosmos LIKE '%%44356%%'
                   OR produto_canon.imagem_cosmos LIKE '%%12466%%'
                   OR produto_canon.fonte = 'placeholder_broken'
            """, (ean, nome, url_final, fonte))
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Busca imagens de cosméticos/higiene/puericultura via múltiplas fontes."
    )
    parser.add_argument("--apply",       action="store_true", help="Grava no banco + upload Cloudinary")
    parser.add_argument("--limite",      type=int, default=100, metavar="N")
    parser.add_argument("--ean",         help="Processa apenas este EAN")
    parser.add_argument("--categoria",   choices=list(_KEYWORDS.keys()),
                        help="Filtra por categoria (padrão: todas)")
    parser.add_argument("--todos",       action="store_true",
                        help="Busca TODOS os EANs do ecommerce sem imagem (sem filtro de categoria)")
    parser.add_argument("--delay",       type=float, default=0.5, metavar="S")
    parser.add_argument("--commit-cada", type=int, default=30, metavar="N")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.todos:
        print("Modo: TODOS os EANs sem imagem (--todos)")
    print("Fontes: medicamentos_imagens → DSP (VTEX) → Droga Raia (VTEX) | OCR ativo")
    print()

    def _nova_conn():
        """Abre conexão com keepalives para evitar queda em sessões longas."""
        import psycopg2, psycopg2.extras
        database_url = os.getenv("DATABASE_URL", "")
        c = psycopg2.connect(
            database_url,
            keepalives=1,
            keepalives_idle=60,
            keepalives_interval=10,
            keepalives_count=5,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        c.autocommit = False
        return c

    def _reconectar(conn_ref, cur_ref):
        _log("  [reconexao] SSL caiu, reconectando...")
        try: conn_ref[0].close()
        except Exception: pass
        conn_ref[0] = _nova_conn()
        cur_ref[0] = conn_ref[0].cursor()

    def _commit_seguro(conn_ref, cur_ref):
        try:
            conn_ref[0].commit()
        except Exception as exc:
            if "SSL" in str(exc) or "connection" in str(exc).lower() or "abort" in str(exc).lower():
                _reconectar(conn_ref, cur_ref)
                conn_ref[0].commit()
            else:
                raise

    def _salvar_retry(conn_ref, cur_ref, ean, nome, img, fonte, apply, verbose):
        """Chama _salvar com reconexão automática em caso de queda SSL."""
        for tentativa in range(2):
            try:
                _salvar(cur_ref[0], ean, nome, img, fonte, apply, verbose)
                return
            except Exception as exc:
                if tentativa == 0 and (
                    "SSL" in str(exc) or "connection" in str(exc).lower() or "abort" in str(exc).lower()
                ):
                    _reconectar(conn_ref, cur_ref)
                else:
                    raise

    conn_ref = [_nova_conn()]
    cur_ref  = [conn_ref[0].cursor()]

    # Usa referências para _buscar_eans (leitura inicial — conn normal basta)
    conn = conn_ref[0]
    cur  = cur_ref[0]

    registros = _buscar_eans(cur, args.categoria, args.limite, args.ean, todos=args.todos)
    cat_label = "todos" if args.todos else (args.categoria or "cosméticos+higiene+puericultura")
    print(f"EANs a processar [{cat_label}]: {len(registros)}")
    if not registros:
        print("Nenhum EAN encontrado sem imagem.")
        cur.close(); conn.close(); return

    # Log em arquivo para acompanhar EAN por EAN em tempo real
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"buscar_imagens_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_file = open(log_path, "w", encoding="utf-8", buffering=1)
    total_r = len(registros)

    def _log(msg: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        log_file.write(line + "\n")

    _log(f"Inicio — {total_r} EANs | apply={args.apply} | limite={args.limite}")
    _log(f"Log salvo em: {log_path}")

    n_med = n_dsp = n_raia = n_sem = n_branded = 0
    pendentes = 0

    for i, reg in enumerate(registros, 1):
        ean  = reg["ean"]
        nome = (reg.get("nome") or ean)[:70]
        achou = False
        fonte_label = ""

        _log(f"[{i}/{total_r}] EAN {ean}  {nome}")
        if args.verbose:
            print(f"\n[{ean}] {nome}")

        # ── Fonte 1: medicamentos_imagens (Vitnatu, Principia, etc.) ─────
        if not achou:
            img = _med_imagens_fetch(ean, cur_ref[0])
            if img:
                _log(f"  -> MED_IMG  {img[:80]}")
                _salvar_retry(conn_ref, cur_ref, ean, nome, img, "medicamentos_imagens", args.apply, args.verbose)
                n_med += 1
                achou = True

        # ── Fonte 2: DSP (Drogaria São Paulo) via VTEX ───────────────────
        if not achou:
            p = _vtex_fetch(ean, verbose=args.verbose)
            d = _extrair_dados_vtex(p, verbose=args.verbose) if p else None
            img = (d or {}).get("imagem")
            if img:
                ocr_text = _ocr_image_text(img)
                # OCR vazio = falhou ou imagem não tem texto legível (foto de pessoa,
                # embalagem encoberta, etc.) → rejeita sem tentar salvar
                if not ocr_text:
                    if args.verbose:
                        print(f"    [ocr] DSP imagem rejeitada (OCR vazio)")
                    n_branded += 1
                elif _image_has_other_pharmacy_text(img) or _image_looks_non_product(img):
                    if args.verbose:
                        print(f"    [ocr] DSP imagem rejeitada (farmácia/banner/pessoa)")
                    n_branded += 1
                else:
                    _log(f"  -> DSP      {img[:80]}")
                    _salvar_retry(conn_ref, cur_ref, ean, nome, img, "vtex_dsp", args.apply, args.verbose)
                    n_dsp += 1
                    achou = True
            elif (d or {}).get("exibir_imagem") is False:
                n_branded += 1

        # ── Fonte 3: Droga Raia via VTEX ──────────────────────────────────
        if not achou:
            time.sleep(args.delay * 0.3)
            d = _raia_fetch(ean, verbose=args.verbose)
            img = (d or {}).get("imagem")
            if img:
                ocr_text = _ocr_image_text(img)
                if not ocr_text:
                    if args.verbose:
                        print(f"    [ocr] Raia imagem rejeitada (OCR vazio)")
                    n_branded += 1
                elif _image_has_other_pharmacy_text(img) or _image_looks_non_product(img):
                    if args.verbose:
                        print(f"    [ocr] Raia imagem rejeitada (farmácia/banner/pessoa)")
                    n_branded += 1
                else:
                    _log(f"  -> RAIA     {img[:80]}")
                    _salvar_retry(conn_ref, cur_ref, ean, nome, img, "vtex_raia", args.apply, args.verbose)
                    n_raia += 1
                    achou = True

        if not achou:
            n_sem += 1
            _log(f"  -> SEM IMAGEM")

        # Commit parcial com reconexão automática
        if achou and args.apply:
            pendentes += 1
            if pendentes >= args.commit_cada:
                _commit_seguro(conn_ref, cur_ref)
                _log(f"  [commit parcial] {pendentes} gravadas | med={n_med} dsp={n_dsp} raia={n_raia} sem={n_sem}")
                pendentes = 0

        time.sleep(args.delay)

    # Commit final com reconexão automática
    total = n_med + n_dsp + n_raia
    if args.apply:
        if pendentes > 0:
            _commit_seguro(conn_ref, cur_ref)
        _log(f"\nGravado: {total} imagem(ns)." if total else "\nNenhuma imagem nova.")
    else:
        try: conn_ref[0].rollback()
        except Exception: pass
        if total:
            _log(f"\nDry-run: {total} imagem(ns) — use --apply para gravar.")
        else:
            _log("\nNenhuma imagem nova encontrada.")

    resumo = (
        f"\nTotal: {len(registros)} | Sem imagem: {n_sem} | Rejeitadas (OCR): {n_branded}"
        f"\nMed_Imagens: {n_med} | DSP: {n_dsp} | Raia: {n_raia}"
        f"\nLog completo: {log_path}"
    )
    _log(resumo)
    log_file.close()
    try: cur_ref[0].close()
    except Exception: pass
    try: conn_ref[0].close()
    except Exception: pass


if __name__ == "__main__":
    main()
