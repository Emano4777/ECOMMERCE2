"""
revalidar_tarja_vtex.py — Revalida tarja/imagem consultando DSP (VTEX) + Droga Raia por EAN.

Fontes consultadas em ordem:
  1. Drogaria SP (VTEX Catalog API) — principal, responde com HTTP 206
  2. Droga Raia (scraping HTML/JSON-LD) — complementa quando DSP nao tem

Logica de imagem:
  - Concorrente exibe imagem BRANDED (logo da loja, "de-referencia-tarja-*")
      -> exibir_imagem_publica=false, mantemos nosso placeholder Poupaqui
  - Concorrente exibe caixa real do fabricante
      -> faz upload para Cloudinary e salva URL em produto_canon

Uso:
    py scripts/revalidar_tarja_vtex.py              # dry-run, 200 registros
    py scripts/revalidar_tarja_vtex.py --apply      # grava no banco + upload Cloudinary
    py scripts/revalidar_tarja_vtex.py --limite 50
    py scripts/revalidar_tarja_vtex.py --ean 7898040321970
    py scripts/revalidar_tarja_vtex.py --chave ATENOLOL
    py scripts/revalidar_tarja_vtex.py --modo tudo
    py scripts/revalidar_tarja_vtex.py --modo so_imagens
    py scripts/revalidar_tarja_vtex.py -v
"""
from __future__ import annotations  # permite str|None e X|Y em Python 3.9
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

from app import db, _anvisa_schema  # noqa: E402

_PLACEHOLDERS_POUPAQUI = frozenset({
    "https://res.cloudinary.com/dizfq460q/image/upload/v1778783063/CAIXA_GEN%C3%89RICO_-_POUPAQUI_itiyth.jpg",
    "https://res.cloudinary.com/dizfq460q/image/upload/v1778783450/ChatGPT_Image_14_de_mai._de_2026_15_30_35_wuovpb.png",
    # imagem genérica de tarja vermelha subida com EAN interno 44356 e distribuída erroneamente
    "https://res.cloudinary.com/dizfq460q/image/upload/v1779979170/catalogo_produtos/catalogo/44356.webp",
})

# Detecta imagem branded/template de qualquer concorrente pela URL/metadados.
_BRANDED_IMAGE_RE = re.compile(
    # Lojas específicas por nome (URL ou alt-text)
    r"drogaria[\s_-]?s[aã]o[\s_-]?paulo|drogaria[\s_-]?sp\b"
    r"|droga[\s_-]?raia|drogaraia"
    r"|davó?\s*farma|d[\'\-]?avo[\s_-]?farma"
    r"|ultrafarma|pague[\s_-]?menos|farmacia[\s_-]?popular"
    # Padrões de template genérico (qualquer loja)
    r"|de[\s_-]referencia[\s_-]tarja"
    r"|[\-_/]tarja[\-_](vermelha|preta)"
    r"|tarja[\-_]gen[eé]rica"
    r"|imagem[\s_-]?ilustrativa?"
    r"|meramente[\s_-]?ilustrativa?"
    r"|geramente[\s_-]?ilustrativa?",
    re.IGNORECASE,
)
# Para imageText/label que descrevem conteúdo de prescrição no campo de texto
_BRANDED_TEXT_RE = re.compile(
    r"venda\s+sob\s+prescri[cç][aã]o|uso\s+sob\s+prescri[cç][aã]o"
    r"|somente\s+com\s+receita|tarja\s+vermelha|tarja\s+preta"
    r"|reten[cç][aã]o\s+da\s+receita",
    re.IGNORECASE,
)

# Regex para detectar texto de branding de farmácia em OCR da imagem
_BRANDED_OCR_RE = re.compile(
    r"drogaria|droga\s+raia|ultrafarma|pague\s+menos"
    r"|venda\s+sob\s+prescri[cç][aã]o"
    r"|uso\s+sob\s+prescri[cç][aã]o"
    r"|somente\s+com\s+receita"
    r"|medicamento\s+sujeito\s+a\s+prescri"
    r"|controle\s+especial"
    r"|©\s*dsp|drogaria\s*sp",
    re.IGNORECASE,
)
# Extrai a tarja do nome do arquivo branded
_TARJA_DA_URL_RE = re.compile(r"tarja[\-_](vermelha|preta)", re.IGNORECASE)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _analisar_imagem_branded(url: str, tarja: str | None = None,
                              verbose: bool = False) -> bool:
    """Detecta se uma imagem é branded (template de farmácia) pelo conteúdo via OCR.

    Estratégia:
      1. OCR.space: detecta texto "Drogaria" na imagem (logo DSP/Raia/etc.)
         Diferencia template branded (tem logo da loja) de foto real do produto.
         NÃO usa análise de cor — produtos legítimos (Advil, etc.) têm embalagens
         coloridas que seriam falsos positivos.

    Retorna True se o OCR detectar branding de farmácia, False caso contrário.
    Retorna False também quando OCR não está disponível (sem chave ou erro).
    """
    if not url or not url.startswith("http"):
        return False

    ocr_key = os.getenv("OCR_SPACE_API_KEY", "").strip()
    if not ocr_key:
        return False  # sem OCR disponível, usa apenas URL patterns

    _CDN_FARMACIA_RE_LOCAL = re.compile(
        r"\.vteximg\.com\.br|\.vtexassets\.com"
        r"|drogaraia\.|drogariasaopaulo\.|drogariasp\."
        r"|ultrafarma\.|paguemenos\.",
        re.IGNORECASE,
    )

    try:
        import urllib.request as _ur
        import urllib.parse as _up
        # Endpoint correto: parse/image com o URL como campo POST
        payload = _up.urlencode({
            "apikey": ocr_key,
            "url": url,
            "language": "por",
            "isOverlayRequired": "false",
            "detectOrientation": "false",
            "scale": "true",
            "OCREngine": "2",
        }).encode()
        req = _ur.Request(
            "https://api.ocr.space/parse/image",
            data=payload,
            method="POST",
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("User-Agent", "PoupaquiEcommerce/1.0")
        with _ur.urlopen(req, timeout=20) as resp:
            ocr_data = json.loads(resp.read().decode("utf-8", errors="replace"))
        texto_ocr = " ".join(
            (r.get("ParsedText") or "")
            for r in (ocr_data.get("ParsedResults") or [])
        )
        if verbose:
            print(f"    [ocr] texto: {texto_ocr[:150]!r}")
        if _BRANDED_OCR_RE.search(texto_ocr):
            if verbose:
                print("    [ocr] BRANDED detectado por texto")
            return True
        # OCR rodou mas nao detectou branding -> imagem real
        return False
    except Exception as exc:
        if verbose:
            print(f"    [ocr] erro: {exc}")
        # OCR falhou: usa regra conservadora — CDN de farmácia = incerto
        if _CDN_FARMACIA_RE_LOCAL.search(url):
            if verbose:
                print("    [ocr-falhou] CDN farmacia -> conservador (incerto)")
            return None  # sinaliza "não sabe" ao chamador
        return False
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Cache-Control": "no-cache",
}
_VTEX_BASE = "https://www.drogariasaopaulo.com.br"

_TARJA_MAP = {
    "vermelha": "vermelha", "tarja vermelha": "vermelha", "vermelho": "vermelha",
    "preta": "preta",       "tarja preta": "preta",       "preto": "preta",
}
_TARJA_VERMELHA_RE = re.compile(
    r"venda\s+sob\s+prescri[cç][aã]o|tarja\s+vermelha"
    r"|uso\s+sob\s+prescri[cç][aã]o|controle\s+especial"
    r"|medicamento\s+sujeito\s+a\s+prescri[cç][aã]o",
    re.IGNORECASE,
)
_TARJA_PRETA_RE = re.compile(
    r"tarja\s+preta|notifica[cç][aã]o\s+de\s+receita\s+[ab]|\blista\s+[AB]",
    re.IGNORECASE,
)

# ─── Droga Raia ───────────────────────────────────────────────────────────────
_RAIA_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer": "https://www.drogaraia.com.br/",
}


def _raia_fetch(ean: str, tarja_bd: str | None = None, verbose: bool = False) -> dict | None:
    """
    Raspa a pagina de busca da Droga Raia e extrai tarja, imagem e info de exibicao.
    Retorna dict com mesma estrutura de _extrair_dados_vtex ou None.
    """
    try:
        from curl_cffi import requests as cr
    except ImportError:
        return None

    url = f"https://www.drogaraia.com.br/search?w={ean}"
    if verbose:
        print(f"    [raia] GET {url}")
    try:
        r = cr.get(url, headers=_RAIA_HEADERS, impersonate="chrome124",
                   timeout=18, allow_redirects=True)
        if verbose:
            print(f"    [raia] -> HTTP {r.status_code}, {len(r.text)} chars")
        if r.status_code != 200 or not r.text:
            return None
        html = r.text
    except Exception as exc:
        if verbose:
            print(f"    [raia] -> ERRO: {exc}")
        return None

    result = {"tarja": None, "imagem": None, "exibir_imagem": None}

    # ── JSON-LD structured data (mais confiável) ──────────────────────────────
    for raw_ld in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            ld = json.loads(raw_ld)
            items = ld if isinstance(ld, list) else [ld]
            for item in items:
                if str(item.get("@type", "")).lower() != "product":
                    continue
                # Imagem
                img = item.get("image") or ""
                if isinstance(img, list):
                    img = img[0] if img else ""
                if img and img.startswith("http"):
                    fname = img.split("?")[0].split("/")[-1]
                    m_tarja = _TARJA_DA_URL_RE.search(fname) or _TARJA_DA_URL_RE.search(img)
                    is_branded = bool(
                        _BRANDED_IMAGE_RE.search(fname)
                        or _BRANDED_IMAGE_RE.search(img)
                        or m_tarja
                    )
                    if not is_branded:
                        is_branded = _analisar_imagem_branded(img, tarja=tarja_bd, verbose=verbose)
                    if is_branded:
                        result["exibir_imagem"] = False
                        if m_tarja:
                            result["tarja"] = m_tarja.group(1).lower()
                    else:
                        result["imagem"] = img
                        result["exibir_imagem"] = True
                # Tarja via additionalProperty / description
                for prop in (item.get("additionalProperty") or []):
                    pname = (prop.get("name") or "").lower()
                    pval  = str(prop.get("value") or "").lower()
                    if "tarja" in pname or "receita" in pname or "prescri" in pname:
                        mapped = _TARJA_MAP.get(pval)
                        if mapped:
                            result["tarja"] = mapped
                            break
                        if _TARJA_PRETA_RE.search(pval):
                            result["tarja"] = "preta"
                            break
                        if _TARJA_VERMELHA_RE.search(pval):
                            result["tarja"] = "vermelha"
                            break
                if result["imagem"] or result["exibir_imagem"] is not None:
                    break
        except Exception:
            pass

    # ── __NEXT_DATA__ (FastStore SSR) ─────────────────────────────────────────
    if result["exibir_imagem"] is None:
        m = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE,
        )
        if m:
            try:
                nd = json.loads(m.group(1))
                # navega pela estrutura do Next.js para chegar nos produtos
                products = (
                    nd.get("props", {})
                      .get("pageProps", {})
                      .get("pageProps", {})
                      .get("results", {})
                )
                if isinstance(products, dict):
                    products = products.get("products") or []
                if isinstance(products, list) and products:
                    p = products[0]
                    # Imagem — novo formato: image.src (raiadrogasil.io CDN, sem branding)
                    img = None
                    img_src = (p.get("image") or {}).get("src") if isinstance(p.get("image"), dict) else None
                    if img_src and img_src.startswith("http"):
                        img = img_src
                    else:
                        # fallback formato antigo
                        imgs = p.get("images") or p.get("imageUrls") or []
                        if isinstance(imgs, list) and imgs:
                            raw = imgs[0]
                            img = (raw.get("imageUrl") or raw) if isinstance(raw, dict) else raw
                            if not isinstance(img, str) or not img.startswith("http"):
                                img = None

                    if img:
                        fname = img.split("?")[0].split("/")[-1]
                        # CDN da Raia (raiadrogasil.io) nunca tem branding — aceita direto
                        is_raia_cdn = "raiadrogasil.io" in img
                        is_branded = not is_raia_cdn and bool(
                            _BRANDED_IMAGE_RE.search(fname)
                            or _BRANDED_IMAGE_RE.search(img)
                            or _TARJA_DA_URL_RE.search(fname)
                        )
                        m_tarja = _TARJA_DA_URL_RE.search(fname) or _TARJA_DA_URL_RE.search(img)
                        if is_branded or m_tarja:
                            result["exibir_imagem"] = False
                            if m_tarja:
                                result["tarja"] = m_tarja.group(1).lower()
                        else:
                            result["imagem"] = img
                            result["exibir_imagem"] = True
                        if verbose:
                            print(f"    [raia-next] img={img[:70]} raia_cdn={is_raia_cdn} branded={is_branded}")

                    # Tarja via stripeCode (0=isento, 1=vermelha, 2=preta)
                    stripe = p.get("stripeCode")
                    if stripe == 2:
                        result["tarja"] = "preta"
                    elif stripe == 1 and not result["tarja"]:
                        result["tarja"] = "vermelha"
                    # Tarja via especificacoes (formato antigo)
                    for spec in (p.get("Tarja") or p.get("tarja") or []):
                        val = str(spec).lower()
                        mapped = _TARJA_MAP.get(val)
                        if mapped:
                            result["tarja"] = mapped
                            break
                        if _TARJA_PRETA_RE.search(val):
                            result["tarja"] = "preta"; break
                        if _TARJA_VERMELHA_RE.search(val):
                            result["tarja"] = "vermelha"; break
            except Exception:
                pass

    # ── Fallback: busca "receita" no HTML bruto ───────────────────────────────
    if result["tarja"] is None and result["exibir_imagem"] is None:
        if _TARJA_PRETA_RE.search(html):
            result["tarja"] = "preta"
            result["exibir_imagem"] = False
        elif _TARJA_VERMELHA_RE.search(html):
            result["tarja"] = "vermelha"
            result["exibir_imagem"] = False

    if verbose:
        print(f"    [raia] tarja={result['tarja']} exibir={result['exibir_imagem']} imagem={bool(result['imagem'])}")
    return result if (result["tarja"] or result["imagem"] or result["exibir_imagem"] is not None) else None


# ─── Cloudinary ───────────────────────────────────────────────────────────────

def _upload_cloudinary(imagem_url: str, ean: str, verbose: bool = False) -> str | None:
    """
    Faz download da imagem e upload para Cloudinary.
    Retorna a secure_url do Cloudinary ou None em caso de falha.
    """
    try:
        import cloudinary
        import cloudinary.uploader
        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME", ""),
            api_key=os.getenv("CLOUDINARY_API_KEY", ""),
            api_secret=os.getenv("CLOUDINARY_API_SECRET", ""),
        )
        if not os.getenv("CLOUDINARY_CLOUD_NAME"):
            if verbose:
                print(f"    [cloudinary] nao configurado, pulando upload")
            return None
    except ImportError:
        if verbose:
            print(f"    [cloudinary] modulo nao instalado")
        return None

    try:
        from curl_cffi import requests as cr
        r = cr.get(imagem_url, headers={"User-Agent": _UA}, impersonate="chrome124", timeout=20)
        img_bytes = r.content if r.status_code in (200, 206) else None
    except Exception:
        try:
            import urllib.request
            with urllib.request.urlopen(imagem_url, timeout=20) as resp:
                img_bytes = resp.read()
        except Exception as exc:
            if verbose:
                print(f"    [cloudinary] download falhou: {exc}")
            return None

    if not img_bytes:
        return None

    try:
        result = cloudinary.uploader.upload(
            img_bytes,
            public_id=f"catalogo/{ean}",
            folder="catalogo_produtos",
            resource_type="image",
            overwrite=False,        # nao re-faz upload se ja existe
            unique_filename=False,
            transformation=[
                {"width": 800, "height": 800, "crop": "pad", "background": "white"},
                {"format": "jpg", "quality": "auto:good"},
            ],
        )
        url = result.get("secure_url")
        if verbose:
            print(f"    [cloudinary] upload OK: {url}")
        return url
    except Exception as exc:
        if verbose:
            print(f"    [cloudinary] upload erro: {exc}")
        return None


def _vtex_get(url: str, verbose: bool = False) -> object:
    """GET JSON via curl_cffi (impersonate Chrome) ou urllib como fallback."""
    try:
        from curl_cffi import requests as cr
        r = cr.get(url, headers=_HEADERS, impersonate="chrome124", timeout=12)
        if verbose:
            print(f"    -> HTTP {r.status_code}, {len(r.content)} bytes")
        if r.status_code not in (200, 206) or not r.content:
            return None
        return r.json()
    except ImportError:
        pass
    except Exception as exc:
        if verbose:
            print(f"    -> ERRO cffi: {exc}")
        return None
    try:
        import ssl, urllib.request
        req = urllib.request.Request(url)
        for k, v in _HEADERS.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=12) as resp:
            raw = resp.read()
            if verbose:
                print(f"    -> HTTP 200, {len(raw)} bytes")
            return json.loads(raw) if raw else None
    except Exception as exc:
        if verbose:
            print(f"    -> ERRO urllib: {exc}")
        return None


# channel encoded: {"salesChannel":"1"}
_CH = "%7B%22salesChannel%22%3A%221%22%7D"


def _vtex_fetch(ean: str, verbose: bool = False) -> dict | None:
    """Busca produto na DSP pelo EAN. Tenta Intelligent Search (VTEX IO) e Catalog API."""
    tentativas = [
        # Intelligent Search — principal para lojas VTEX IO como DSP
        (
            f"{_VTEX_BASE}/_v/api/intelligent-search/product_search"
            f"?query={ean}&page=1&count=1&sort=&operator=and&fuzzy=0",
            "is",
        ),
        (
            f"{_VTEX_BASE}/_v/api/intelligent-search/product_search"
            f"?query={ean}&page=1&count=1&sort=&operator=and&fuzzy=0&channel={_CH}",
            "is",
        ),
        # Catalog API legada (fallback)
        (
            f"{_VTEX_BASE}/api/catalog_system/pub/products/search"
            f"?fq=alternateIdValues:{ean}&_from=0&_to=0&sc=1",
            "catalog",
        ),
        (
            f"{_VTEX_BASE}/api/catalog_system/pub/products/search"
            f"?ft={ean}&_from=0&_to=0&sc=1",
            "catalog",
        ),
    ]

    for url, fmt in tentativas:
        if verbose:
            print(f"    [{fmt}] GET {url}")
        data = _vtex_get(url, verbose=verbose)
        if data is None:
            continue

        if fmt == "is":
            # Intelligent Search retorna {"products": [...]}
            products = (data if isinstance(data, dict) else {}).get("products") or []
            if verbose:
                print(f"    -> IS: {len(products)} produto(s)")
            if products:
                # Converte para formato compatível com _extrair_dados_vtex
                p = products[0]
                return _normalizar_is(p)

        elif fmt == "catalog":
            if isinstance(data, list):
                if verbose:
                    print(f"    -> Catalog: {len(data)} produto(s)")
                if data:
                    return data[0]

    return None


def _normalizar_is(p: dict) -> dict:
    """Converte produto do Intelligent Search para o formato do Catalog API."""
    items = []
    for sku in (p.get("items") or p.get("skus") or []):
        images = []
        for img in (sku.get("images") or []):
            images.append({
                "imageUrl": img.get("imageUrl") or img.get("imageText") or "",
                "imageText": img.get("imageLabel") or img.get("imageText") or "",
            })
        items.append({"images": images, "sellers": []})

    # Especificações: IS usa allSpecifications ou specificationGroups
    properties = []
    for spec_name in (p.get("allSpecifications") or []):
        val_list = p.get(spec_name) or []
        properties.append({
            "name": spec_name,
            "values": val_list if isinstance(val_list, list) else [val_list],
        })
    for grp in (p.get("specificationGroups") or []):
        for spec in (grp.get("specifications") or []):
            properties.append({
                "name": spec.get("name") or "",
                "values": spec.get("values") or [],
            })

    return {
        "productName": p.get("productName") or p.get("name") or "",
        "items": items,
        "properties": properties,
    }


def _extrair_dados_vtex(produto: dict, tarja_bd: str | None = None,
                        verbose: bool = False) -> dict:
    result = {"tarja": None, "imagem": None, "exibir_imagem": None}

    # Imagem
    for item in produto.get("items", []):
        for img in (item.get("images") or []):
            url = (img.get("imageUrl") or "").strip()
            if not url.startswith("http"):
                continue
            img_text = " ".join(filter(None, [
                img.get("imageText") or "",
                img.get("imageLabel") or "",
            ]))
            url_fname = url.split("?")[0].split("/")[-1]
            # Verificação 1: padrões no NOME DO ARQUIVO e nos metadados da imagem.
            # A URL completa NÃO é usada aqui — o hostname "drogariasp.vteximg.com.br"
            # hospedam tanto imagens reais quanto templates, portanto não é discriminativo.
            is_branded = bool(
                _BRANDED_IMAGE_RE.search(url_fname)
                or _BRANDED_TEXT_RE.search(img_text)
            )
            if verbose:
                print(f"    imagem: {url[:80]}  text={img_text!r}  branded_fname={is_branded}")
            # Verificação 2: OCR — detecta texto "Drogaria" na imagem
            # Retorna: True=branded, False=real, None=incerto (OCR falhou)
            if not is_branded:
                ocr_result = _analisar_imagem_branded(url, tarja=tarja_bd, verbose=verbose)
                if ocr_result is True:
                    is_branded = True
                elif ocr_result is None:
                    # OCR falhou e URL e de CDN de farmacia -> nao altera exibir
                    if verbose:
                        print("    [cdn-incerto] OCR falhou + CDN -> exibir=None")
                    result["exibir_imagem"] = None
                    result["imagem"] = None
                    break
                # ocr_result is False -> imagem real, nao branded
            if is_branded:
                result["exibir_imagem"] = False
                m = _TARJA_DA_URL_RE.search(url_fname) or _TARJA_DA_URL_RE.search(url)
                if m:
                    result["tarja_da_url"] = m.group(1).lower()
            else:
                result["exibir_imagem"] = True
                result["imagem"] = url
            break
        if result["exibir_imagem"] is not None:
            break

    # Propriedades
    props = {}
    for p in produto.get("properties", []):
        nome = (p.get("name") or "").strip().lower()
        vals = p.get("values") or []
        props[nome] = " ".join(str(v) for v in vals).strip()

    if verbose and props:
        print(f"    props: { {k: v for k, v in list(props.items())[:8]} }")

    # exibir_imagem_publica nas props
    for campo in ("exibir imagem", "exibir imagem publica", "imagem publica"):
        val = props.get(campo, "").lower()
        if val in ("nao", "no", "false", "0"):
            result["exibir_imagem"] = False
            result["imagem"] = None
            break
        if val in ("sim", "yes", "true", "1"):
            if result["exibir_imagem"] is None:
                result["exibir_imagem"] = True
            break

    # Tarja
    for campo in ("tarja", "tipo de tarja", "classificacao", "classificacao terapeutica"):
        val = props.get(campo, "").lower()
        mapped = _TARJA_MAP.get(val)
        if mapped:
            result["tarja"] = mapped
            break
        for k, v in _TARJA_MAP.items():
            if k in val:
                result["tarja"] = v
                break
        if result["tarja"]:
            break

    if result["tarja"] is None:
        blob = " ".join(props.values())
        if _TARJA_PRETA_RE.search(blob):
            result["tarja"] = "preta"
        elif _TARJA_VERMELHA_RE.search(blob):
            result["tarja"] = "vermelha"

    # Tarja do nome do arquivo branded (mais confiável que inferência genérica)
    if result["tarja"] is None and result.get("tarja_da_url"):
        result["tarja"] = result["tarja_da_url"]
    # Se DSP usa imagem branded e ainda sem tarja -> inferimos vermelha (minimo seguro)
    elif result["exibir_imagem"] is False and result["tarja"] is None:
        result["tarja"] = "vermelha"

    result.pop("tarja_da_url", None)

    return result


def _resolver_eans_por_chave(cur, chaves: list[str], so_catalogo_ativo: bool = False) -> dict[str, list[str]]:
    """
    Dado uma lista de chaves anvisa (ex: ['ATENOLOL', 'LEVONORGESTREL']),
    retorna {chave: [ean1, ean2, ...]} buscando nos EANs do catalogo.

    so_catalogo_ativo=True: restringe a lojas com catalogo_publico = TRUE.
    """
    if not chaves:
        return {}

    from app import _anvisa_chave

    filtro_ativo_e = ""
    filtro_ativo_ae = ""
    if so_catalogo_ativo:
        filtro_ativo_e = """
          AND EXISTS (
            SELECT 1 FROM ecommerce_config_loja ecl
            WHERE ecl.cnpjloja = e.cnpj AND ecl.catalogo_publico = TRUE
          )"""
        filtro_ativo_ae = """
          AND EXISTS (
            SELECT 1 FROM ecommerce_config_loja ecl
            WHERE ecl.cnpjloja = ae.cnpj_loja AND ecl.catalogo_publico = TRUE
          )"""

    cur.execute(f"""
        SELECT DISTINCT
            COALESCE(m.barra_norm, e.barras) AS ean,
            COALESCE(m.descricao, e.descricao) AS nome
        FROM estoque e
        LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
        WHERE COALESCE(e.barras, '') <> ''
          AND e.estoque > 0
          AND COALESCE(e.barras_norm, e.barras) ~ '^[1-9][0-9]{{6,12}}$'
          {filtro_ativo_e}

        UNION

        SELECT DISTINCT
            COALESCE(m.barra_norm, ae.ean) AS ean,
            COALESCE(m.descricao, ae.descricao_produto) AS nome
        FROM automatiza_estoque ae
        LEFT JOIN medicamentos m ON m.barra_norm = ae.ean
        WHERE COALESCE(ae.ean, '') <> ''
          AND ae.quantidade_estoque > 0
          AND ae.ean ~ '^[1-9][0-9]{{6,12}}$'
          {filtro_ativo_ae}
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


def _buscar_registros(cur, modo: str, limite: int, chave_filtro: str | None, ean_filtro: str | None) -> list[dict]:
    limit_sql = f" LIMIT {limite}" if limite else ""

    if ean_filtro:
        # Busca pela chave derivada do EAN via medicamentos
        from app import _anvisa_chave
        cur.execute(
            "SELECT descricao FROM medicamentos WHERE barra_norm = %s LIMIT 1",
            (ean_filtro,),
        )
        row = cur.fetchone()
        chave_derivada = _anvisa_chave(row["descricao"]) if row else ean_filtro
        cur.execute(
            "SELECT chave, tarja, exibir_imagem_publica FROM anvisa_cache "
            "WHERE encontrado = TRUE AND (chave = %s OR chave ILIKE %s)",
            (chave_derivada, f"%{chave_derivada}%"),
        )
        return [dict(r) for r in cur.fetchall()]

    if chave_filtro:
        cur.execute(
            "SELECT chave, tarja, exibir_imagem_publica FROM anvisa_cache "
            "WHERE encontrado = TRUE AND (chave = %s OR chave ILIKE %s)",
            (chave_filtro.upper(), f"%{chave_filtro.upper()}%"),
        )
        return [dict(r) for r in cur.fetchall()]

    if modo == "nulos":
        # Processa registros que ainda precisam de trabalho:
        #   1. tarja NULL              -> precisa detectar tarja
        #   2. exibir_imagem NULL      -> precisa decidir se exibe
        #   3. exibir=TRUE sem imagem  -> precisa buscar imagem real
        #   4. exibir=FALSE sem imagem real confirmada -> pode ter sido classificado
        #      incorretamente; revalida contra DSP/Raia para confirmar ou corrigir
        cur.execute(f"""
            SELECT chave, tarja, exibir_imagem_publica
            FROM anvisa_cache
            WHERE encontrado = TRUE
              AND NOT (
                -- ja totalmente processado: exibir=TRUE + tarja + imagem real
                exibir_imagem_publica = TRUE
                AND tarja IS NOT NULL AND TRIM(tarja) <> ''
                AND EXISTS (
                  SELECT 1 FROM produto_canon pc
                  WHERE pc.ean = chave
                    AND pc.imagem_cosmos IS NOT NULL
                    AND TRIM(pc.imagem_cosmos) <> ''
                    AND pc.imagem_cosmos NOT LIKE '%CAIXA_GEN%'
                    AND pc.imagem_cosmos NOT LIKE '%ChatGPT_Image%'
                )
              )
            ORDER BY chave
            {limit_sql}
        """)
    elif modo == "so_imagens":
        cur.execute(f"""
            SELECT DISTINCT ac.chave, ac.tarja, ac.exibir_imagem_publica
            FROM anvisa_cache ac
            JOIN ecommerce_produto_imagens epi ON epi.imagem_url = ANY(%s)
            WHERE ac.encontrado = TRUE
            ORDER BY ac.chave
            {limit_sql}
        """, (list(_PLACEHOLDERS_POUPAQUI),))
    else:
        cur.execute(f"""
            SELECT chave, tarja, exibir_imagem_publica
            FROM anvisa_cache WHERE encontrado = TRUE
            ORDER BY chave {limit_sql}
        """)

    return [dict(r) for r in cur.fetchall()]


# IDs de imagens placeholder da Raia — genéricas, não representam o produto real
_RAIA_PLACEHOLDER_IDS: set[str] = {"14982031", "14982032"}


def _is_placeholder_image(image_url: str) -> bool:
    """Retorna True se a URL for de imagem placeholder conhecida."""
    import re as _re
    m = _re.search(r'/images/(\d+)', image_url or "")
    return bool(m and m.group(1) in _RAIA_PLACEHOLDER_IDS)


def _tarja_from_image_ocr(image_url: str, verbose: bool = False) -> str | None:
    """Determina tarja pelo texto OCR da imagem do medicamento.

    - Faixa preta:    "RETENÇÃO DE RECEITA" no texto
    - Faixa vermelha: "PRESCRIÇÃO MÉDICA" sem "RETENÇÃO"
    - None:           imagem placeholder, OCR vazio ou chave não configurada
    """
    # Rejeita placeholders conhecidos — seu texto genérico não representa o produto
    if _is_placeholder_image(image_url):
        if verbose:
            print(f"    [ocr-tarja] placeholder conhecido, ignorando")
        return None
    try:
        from app import _ocr_image_text
        text = (_ocr_image_text(image_url) or "").upper()
        if not text:
            return None
        if verbose:
            print(f"    [ocr-tarja] {text[:120]!r}")
        # Imagem genérica/ilustrativa — não usar para determinar tarja
        if "ILUSTRATIVA" in text or "MERAMENTE" in text:
            if verbose:
                print(f"    [ocr-tarja] texto indica imagem ilustrativa, ignorando")
            return None
        if "RETEN" in text:
            return "preta"
        if "PRESCRI" in text or "VENDA SOB" in text:
            return "vermelha"
    except Exception as exc:
        if verbose:
            print(f"    [ocr-tarja] erro: {exc}")
    return None


def main():
    parser = argparse.ArgumentParser(description="Revalida tarja e imagem via VTEX Drogaria SP.")
    parser.add_argument("--apply",  action="store_true", help="Grava correcoes no banco")
    parser.add_argument("--limite", type=int, default=200, metavar="N")
    parser.add_argument("--ean",    help="Filtra por EAN numerico (ex: 7898039562551)")
    parser.add_argument("--chave",  help="Filtra por chave anvisa (ex: ATENOLOL)")
    parser.add_argument("--modo",   choices=["nulos", "tudo", "so_imagens"], default="nulos")
    parser.add_argument("--delay",  type=float, default=0.6, metavar="S")
    parser.add_argument("--so-catalogo-ativo", action="store_true",
                        help="Restringe EANs apenas a lojas com catalogo_publico=TRUE")
    parser.add_argument("--commit-cada", type=int, default=100, metavar="N",
                        help="Commit a cada N mudancas gravadas (evita timeout no pooler)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log detalhado de cada requisicao")
    args = parser.parse_args()

    _anvisa_schema()
    conn = db()
    cur  = conn.cursor()

    registros = _buscar_registros(cur, args.modo, args.limite, args.chave, args.ean)
    print(f"Registros a verificar: {len(registros)}")

    if not registros:
        print("Nenhum registro encontrado.")
        cur.close(); conn.close(); return

    # Resolve chaves -> EANs em batch
    todas_chaves = [r["chave"] for r in registros]
    so_ativo = getattr(args, "so_catalogo_ativo", False)
    label_ativo = " (só lojas com catálogo ativo)" if so_ativo else ""
    print(f"Resolvendo EANs para {len(todas_chaves)} chaves{label_ativo}...")
    chave_para_eans = _resolver_eans_por_chave(cur, todas_chaves, so_catalogo_ativo=so_ativo)

    sem_ean = sum(1 for c in todas_chaves if not chave_para_eans.get(c))
    print(f"  Chaves com EAN: {len(todas_chaves) - sem_ean} | Sem EAN no catalogo: {sem_ean}\n")

    sem_resposta = n_tarja = n_exibir = n_imagem = 0
    pendentes_commit = 0

    for reg in registros:
        chave     = reg["chave"]
        tarja_bd  = (reg.get("tarja") or "").strip().lower() or None
        exibir_bd = reg.get("exibir_imagem_publica")

        eans = chave_para_eans.get(chave) or []
        if not eans:
            if args.verbose:
                print(f"[{chave}] sem EAN no catalogo, pulando")
            continue

        if args.verbose:
            print(f"[{chave}] EANs: {eans}")

        precisa_tarja = not tarja_bd

        # ── Consulta DSP ──────────────────────────────────────────────────────
        produto_vtex = None
        for ean in eans:
            produto_vtex = _vtex_fetch(ean, verbose=args.verbose)
            if produto_vtex:
                if args.verbose:
                    print(f"  -> DSP: {produto_vtex.get('productName', '')}")
                break
            time.sleep(args.delay)

        dados_dsp  = _extrair_dados_vtex(produto_vtex, tarja_bd=tarja_bd, verbose=args.verbose) if produto_vtex else None
        exibir_dsp = (dados_dsp or {}).get("exibir_imagem")

        # nao_pode_exibir é determinado APENAS pela fonte externa (DSP/Raia).
        # O valor atual do banco é o que estamos tentando validar — nunca bloqueia
        # a busca por imagem nem a consulta Raia.
        nao_pode_exibir_dsp = exibir_dsp is False

        # ── Consulta Raia (quando DSP nao trouxe tudo que precisamos) ─────────
        precisa_raia = (
            not produto_vtex                                    # DSP nao encontrou
            or (
                not nao_pode_exibir_dsp                         # DSP nao bloqueou
                and (dados_dsp or {}).get("imagem") is None     # DSP nao trouxe imagem
            )
            or (
                precisa_tarja
                and not (dados_dsp or {}).get("tarja")          # DSP nao deu tarja
                and not nao_pode_exibir_dsp
            )
        )

        dados_raia = None
        if precisa_raia:
            for ean in eans:
                dados_raia = _raia_fetch(ean, tarja_bd=tarja_bd, verbose=args.verbose)
                if dados_raia:
                    if args.verbose:
                        print(f"  -> Raia: tarja={dados_raia['tarja']} imagem={bool(dados_raia['imagem'])}")
                    break
                time.sleep(args.delay * 0.5)

        if not dados_dsp and not dados_raia:
            print(f"  [sem resposta DSP+Raia] {chave}  (EANs: {eans})")
            sem_resposta += 1
            continue

        time.sleep(args.delay)

        # ── Mescla: DSP tem prioridade, Raia complementa ─────────────────────
        tarja_vtex  = (dados_dsp or {}).get("tarja") or (dados_raia or {}).get("tarja")
        exibir_vtex = exibir_dsp
        if exibir_vtex is None:
            exibir_vtex = (dados_raia or {}).get("exibir_imagem")

        # nao_pode_exibir final: fonte externa determina; se externa nao opinou,
        # mantém o valor do banco apenas como fallback conservador.
        nao_pode_exibir = exibir_vtex is False or (exibir_vtex is None and exibir_bd is False)

        # Imagem: busca se fonte externa diz que pode (ou nao opinou e banco nao bloqueou)
        imagem_vtex = None
        if not nao_pode_exibir:
            imagem_vtex = (dados_dsp or {}).get("imagem") or (dados_raia or {}).get("imagem")

        nova_tarja  = tarja_bd
        novo_exibir = exibir_bd
        nova_imagem = None

        # Tarja — valida via OCR da imagem antes de aceitar mudança
        if tarja_vtex and tarja_vtex != tarja_bd:
            # Se há imagem, confirma pelo texto visual da embalagem:
            # faixa preta = "RETENÇÃO DE RECEITA"; faixa vermelha = só "PRESCRIÇÃO MÉDICA"
            if imagem_vtex:
                tarja_ocr = _tarja_from_image_ocr(imagem_vtex, verbose=args.verbose)
                if tarja_ocr and tarja_ocr != tarja_vtex:
                    # OCR contradiz catálogo → usa OCR (imagem real do produto)
                    print(f"  [ocr-tarja]  {chave}: catalogo={tarja_vtex!r} imagem={tarja_ocr!r} — usando imagem")
                    tarja_vtex = tarja_ocr
                elif tarja_ocr is None and tarja_bd and _is_placeholder_image(imagem_vtex):
                    # Placeholder detectado + tarja já existe no banco
                    # → não há confirmação visual real, mantém tarja atual
                    print(f"  [ocr-tarja]  {chave}: imagem placeholder, mantendo tarja atual {tarja_bd!r}")
                    tarja_vtex = tarja_bd
            if tarja_vtex != tarja_bd:
                label = "nova" if not tarja_bd else "corrigida"
                print(f"  [tarja {label}]  {chave}: {tarja_bd!r} -> {tarja_vtex!r}")
                nova_tarja = tarja_vtex
                n_tarja += 1

        # exibir_imagem_publica: segue decisão da fonte externa (DSP/Raia).
        # A detecção de imagem branded já foi feita em _extrair_dados_vtex/_raia_fetch
        # via URL patterns + OCR (se disponível). Não aplica regra blanket por tarja.
        if exibir_vtex is not None and exibir_vtex != exibir_bd:
            print(f"  [exibir]         {chave}: {exibir_bd} -> {exibir_vtex}")
            novo_exibir = exibir_vtex
            n_exibir += 1

        # Imagem real do fabricante
        if imagem_vtex:
            cur.execute(
                "SELECT imagem_cosmos FROM produto_canon WHERE ean = ANY(%s) LIMIT 1",
                (eans,),
            )
            row_pc = cur.fetchone()
            imagem_atual = (row_pc.get("imagem_cosmos") or "").strip() if row_pc else ""
            sem_imagem_nossa = not imagem_atual or imagem_atual in _PLACEHOLDERS_POUPAQUI
            if sem_imagem_nossa:
                fonte = "DSP" if (dados_dsp and dados_dsp.get("imagem")) else "Raia"
                print(f"  [imagem {fonte}]    {chave}  EAN={eans[0]}  {imagem_vtex[:70]}...")
                nova_imagem = imagem_vtex
                n_imagem += 1

        # Grava
        _n_antes = n_tarja + n_exibir + n_imagem
        if args.apply:
            if nova_tarja != tarja_bd or novo_exibir != exibir_bd:
                cur.execute(
                    "UPDATE anvisa_cache SET tarja=%s, exibir_imagem_publica=%s WHERE chave=%s",
                    (nova_tarja, novo_exibir, chave),
                )
            if nova_imagem:
                # Faz upload para Cloudinary e usa a URL permanente
                url_final = nova_imagem
                if args.apply:
                    url_cloudinary = _upload_cloudinary(nova_imagem, eans[0], verbose=args.verbose)
                    if url_cloudinary:
                        url_final = url_cloudinary
                        print(f"  [cloudinary OK] {chave}: {url_cloudinary[:70]}...")
                    else:
                        print(f"  [cloudinary FALHOU] {chave}: usando URL original")

                fonte = "vtex_dsp" if (dados_dsp and dados_dsp.get("imagem")) else "vtex_raia"
                for ean in eans:
                    # Tenta atualizar linha existente primeiro
                    cur.execute("""
                        UPDATE produto_canon
                           SET imagem_cosmos = %s,
                               fonte         = %s,
                               atualizado_em = NOW()
                        WHERE ean = %s
                          AND (
                            imagem_cosmos IS NULL
                            OR TRIM(imagem_cosmos) = ''
                            OR imagem_cosmos = ANY(%s)
                          )
                    """, (url_final, fonte, ean, list(_PLACEHOLDERS_POUPAQUI)))

                    if cur.rowcount == 0:
                        # Linha nao existe — busca nome para satisfazer NOT NULL
                        cur.execute("""
                            SELECT COALESCE(m.descricao, e.descricao, ae.descricao_produto) AS nome
                            FROM (SELECT %s::text AS ean) x
                            LEFT JOIN medicamentos m     ON m.barra_norm = x.ean
                            LEFT JOIN estoque e          ON COALESCE(e.barras_norm, e.barras) = x.ean
                            LEFT JOIN automatiza_estoque ae ON ae.ean = x.ean
                            LIMIT 1
                        """, (ean,))
                        row_nome = cur.fetchone()
                        descricao = (row_nome.get("nome") or ean) if row_nome else ean
                        cur.execute("""
                            INSERT INTO produto_canon
                                (ean, descricao_canon, imagem_cosmos, fonte, atualizado_em)
                            VALUES (%s, %s, %s, %s, NOW())
                            ON CONFLICT (ean) DO UPDATE
                              SET imagem_cosmos = EXCLUDED.imagem_cosmos,
                                  fonte         = EXCLUDED.fonte,
                                  atualizado_em = NOW()
                            WHERE produto_canon.imagem_cosmos IS NULL
                               OR TRIM(produto_canon.imagem_cosmos) = ''
                               OR produto_canon.imagem_cosmos = ANY(%s)
                        """, (ean, descricao, url_final, fonte, list(_PLACEHOLDERS_POUPAQUI)))

        if args.apply and (n_tarja + n_exibir + n_imagem > _n_antes):
            pendentes_commit += 1
            if pendentes_commit >= args.commit_cada:
                conn.commit()
                print(f"  [commit parcial] {pendentes_commit} mudancas gravadas")
                pendentes_commit = 0

    if args.apply:
        if pendentes_commit > 0:
            conn.commit()
        if n_tarja or n_exibir or n_imagem:
            print(f"\nGravado.")
        else:
            print("\nNenhuma divergencia encontrada.")
    else:
        conn.rollback()
        if n_tarja or n_exibir or n_imagem:
            print(f"\nDry-run (use --apply para gravar)")
        else:
            print("\nNenhuma divergencia encontrada.")

    print(
        f"\nTotal: {len(registros)} | Sem EAN: {sem_ean} | Sem resposta: {sem_resposta}"
        f"\nTarja: {n_tarja} | Exibir: {n_exibir} | Imagem: {n_imagem}"
    )
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
