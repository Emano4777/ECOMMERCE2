"""
buscar_imagens_v2.py — Busca imagens de cosméticos, perfumaria, dermocosméticos,
higiene e puericultura para produtos sem classificação ANVISA.

IMPORTANTE: Execute localmente (não no servidor), pois Drogasil/Ultrafarma/Panvel
bloqueiam o IP do Hostgator. O banco de dados (Supabase) é acessível de qualquer lugar.

Fontes em ordem de prioridade:
  1. Cosmos/Bluesoft  — base de EANs nacionais (tokens no .env)
  2. DSP (VTEX)       — Drogaria São Paulo
  3. Drogasil (VTEX)  — grande acervo de cosméticos (funciona no local, bloqueado no server)
  4. Ultrafarma       — dermocosméticos, suplementos (funciona no local)
  5. Panvel           — cosméticos, higiene
  6. Beleza na Web    — especialista em cosméticos
  7. Droga Raia       — scraping HTML/JSON-LD
  8. Serper           — Google Images (se chave disponível)

Validações:
  - Dimensões mínimas 150x150 px (Pillow)
  - Proporção não-banner (0.25–4.0)
  - Tamanho mínimo 6 KB
  - URL sem padrão banner/promo/editorial
  - Sem branding de farmácia concorrente na URL
  - OCR anti-branding (se OCR.space key disponível)

Uso (da raiz do projeto dns-ecommerce):
  python scripts/buscar_imagens_v2.py                   # dry-run 200 EANs
  python scripts/buscar_imagens_v2.py --apply           # grava
  python scripts/buscar_imagens_v2.py --limite 500
  python scripts/buscar_imagens_v2.py --ean 7891150037465
  python scripts/buscar_imagens_v2.py --categoria cosmeticos
  python scripts/buscar_imagens_v2.py --sem-serper
  python scripts/buscar_imagens_v2.py -v
"""
from __future__ import annotations
import argparse, io, json, os, re, sys, time, urllib.request, urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from app import (
    db,
    _looks_like_other_pharmacy_brand,
    _image_has_other_pharmacy_text,
    _image_looks_non_product,
)
from scripts.revalidar_tarja_vtex import (
    _vtex_fetch,
    _raia_fetch,
    _extrair_dados_vtex,
    _upload_cloudinary,
    _UA,
)
from scripts.buscar_imagens_cosmeticos import (
    _beleza_fetch,
    _cosmos_fetch_rotating,
    _serper_fetch_rotating,
    _cosmos_tokens,
    _serper_keys,
)

try:
    from PIL import Image
    _PIL_OK = True
except ImportError:
    _PIL_OK = False
    print("[aviso] Pillow nao instalado — instale: pip install Pillow")

# ── Palavras-chave de cosméticos / higiene / puericultura ─────────────────────
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
        "dermocosmetico", "dermocosmético", "protetor solar", "fps ",
        "bb cream", "tônico", "tonico facial", "micellar", "micelar",
        "gel capilar", "mousse", "finalizador", "oleo capilar",
        "tratamento capilar", "tonalizante", "descolorante", "oxigenada",
    ],
    "perfumaria": [
        "perfume ", "eau de", "edt ", "edp ", "colônia", "colonia ",
        "body splash", "splash ", "deo colonia", "deo parfum",
        "bvlgari", "carolina herrera", "chanel ", "dior ", "givenchy",
        "calvin klein", "hugo boss", "armani", "versace", "paco rabanne",
        "azzaro", "jequiti", "boticário", "natura ", "o boticario",
        "dolce ", "antonio banderas", "davidoff", "ferrari ", "mont blanc",
        "thierry mugler", "viktor rolf", "narciso rodriguez",
    ],
    "higiene": [
        "sabonete barra", "sabonete gel", "desodorante", "des rexona",
        "des nivea", "des dove", "des gilette", "absorvente", "absorv ",
        "preserv", "camisinha", "fio dental", "escova dent",
        "creme dent", "pasta dent", "enxaguante", "antisseptico buc",
        "algodao", "curativo", "band-aid", "repelente",
        "protetor labial", "depilatorio", "depilatório",
        "barbeador", "aparelho barb", "lamina barb", "gillette",
        "papel higienico", "papel hig", "lenço descart", "toalha umed",
        "sabao liquido", "detergente", "amaciante", "limpador",
        "multiuso", "desinfetante", "agua sanitaria",
    ],
    "puericultura": [
        "fralda", "lenco umed", "lenço umed", "mamadeira", "chupeta",
        "buba ", "mam ", "huggies", "pampers", "baby ", "bebê ", "bebe ",
        "recem nasc", "recém nasc", "pomada assad", "talco infantil",
        "aspirador nasal", "johnson", "mustela", "infantil ",
    ],
    "varejo_correlatos": [
        # Suplementos (nomes de produto, não princípios ativos de medicamentos)
        "colageno", "colágeno", "suplemento ", "whey ", "creatina ",
        "proteina isolada", "proteína isolada", "omega 3", "ômega 3",
        "melatonina ", "biotin ", "biotina ", "acido hialuronico",
        # Dispositivos e acessórios
        "termômetro", "termometro", "medidor pressao", "esfigmomanom",
        "glicosimetro", "glicosímetro", "lanceta ", "tira glicemia",
        "nebulizador", "inalador ", "luva descart", "mascara descart",
        "micropore", "esparadrapo", "curativo adesivo",
        # Higiene geral (sem palavras que matchem princípios ativos)
        "alcool gel", "alcool 70%", "agua oxigenada 10vol",
        "preservativo", "lubrificante intim",
        # Vitaminas (somente nomes comerciais, não fórmulas)
        "vitamina c efervescente", "vitamina d3 gotas", "vitamina e creme",
        "complexo b comprimido", "suplemento vitamini",
    ],
}

_ALL_KEYWORDS = list({kw for kws in _KEYWORDS.values() for kw in kws})

# Versão com alias (para SELECT com JOIN)
_SEM_IMAGEM_COND = (
    "(m.imagem IS NULL OR TRIM(m.imagem)='' "
    "OR m.imagem LIKE '%%CAIXA_GEN%%' "
    "OR m.imagem LIKE '%%ChatGPT_Image%%' "
    "OR m.imagem LIKE '%%placeholder%%' "
    "OR m.imagem LIKE '%%44356%%')"
)
# Versão sem alias (para UPDATE)
_SEM_IMAGEM_COND_NOALIAS = (
    "(imagem IS NULL OR TRIM(imagem)='' "
    "OR imagem LIKE '%%CAIXA_GEN%%' "
    "OR imagem LIKE '%%ChatGPT_Image%%' "
    "OR imagem LIKE '%%placeholder%%' "
    "OR imagem LIKE '%%44356%%')"
)

_BANNER_RE = re.compile(
    r'banner|editorial|lifestyle|campanha|promo|oferta|desconto',
    re.IGNORECASE,
)

# URLs de CDN de farmácias concorrentes — imagens dali têm branding ou são placeholders
_COMPETITOR_CDN_RE = re.compile(
    r'raiadrogasil\.io|drogariaraia\.|drogasil\.com|panvel\.com'
    r'|ultrafarma\.com|farmacia\.com\.br|onofre\.com',
    re.IGNORECASE,
)

# Texto que indica imagem genérica/placeholder de medicamento — deve ser rejeitada
_PLACEHOLDER_TEXT_RE = re.compile(
    r'imagem meramente ilustrativa|imagem ilustrativa|medicamento gen[eé]rico'
    r'|comprimidos\b|c[aá]psulas\b|venda sob prescri[cç][aã]o'
    r'|genericamente ilustrado|imagem do produto pode variar',
    re.IGNORECASE,
)

# Rastreia IDs de imagem já usados no run — mesmo ID para EANs diferentes = placeholder
_seen_image_ids: set[str] = set()


def _extract_image_id(url: str) -> str | None:
    """Extrai o ID numérico da imagem da URL (ex: /images/14982031.webp → '14982031')."""
    m = re.search(r'/images?/(\d{5,})', url)
    return m.group(1) if m else None


def _is_competitor_cdn(url: str) -> bool:
    """Verifica se a URL é de CDN de concorrente.
    Não bloqueia fontes como Raia/DSP cujas imagens são re-hospedadas no Cloudinary.
    Apenas bloqueia se a URL for usada diretamente (não via fonte dedicada).
    """
    return bool(_COMPETITOR_CDN_RE.search(url))


def _is_placeholder_by_ocr(img_url: str) -> bool:
    """Verifica se a imagem é um placeholder via OCR.space (se chave disponível)."""
    try:
        from app import _ocr_image_text
        text = _ocr_image_text(img_url)
        return bool(_PLACEHOLDER_TEXT_RE.search(text or ""))
    except Exception:
        return False


_VTEX_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer": "https://www.drogasil.com.br/",
}


# ── Validação de imagem ───────────────────────────────────────────────────────

def _validate_image_url(url: str, verbose: bool = False,
                        check_competitor_cdn: bool = False) -> bool:
    # 1. Rejeita URL de CDN de concorrente (só para fontes abertas como Serper/Cosmos)
    if check_competitor_cdn and _is_competitor_cdn(url):
        if verbose:
            print(f"    [val] CDN de concorrente na URL, rejeitando")
        return False
    # 2. Rejeita padrão de banner na URL
    if _BANNER_RE.search(url):
        if verbose:
            print(f"    [val] padrao banner na URL")
        return False
    # 3. Detecta placeholder pelo ID de imagem (mesmo ID = imagem genérica compartilhada)
    img_id = _extract_image_id(url)
    if img_id and img_id in _seen_image_ids:
        if verbose:
            print(f"    [val] ID {img_id} já visto — placeholder genérico")
        return False
    if not _PIL_OK:
        if img_id:
            _seen_image_ids.add(img_id)
        return True
    # 4. Valida dimensões e tamanho
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = r.read(500_000)
        if len(data) < 6_000:
            if verbose:
                print(f"    [val] muito pequena ({len(data)} bytes)")
            return False
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if w < 150 or h < 150:
            if verbose:
                print(f"    [val] resolucao baixa ({w}x{h})")
            return False
        ratio = w / h
        if ratio > 4.0 or ratio < 0.25:
            if verbose:
                print(f"    [val] proporcao suspeita ({w}x{h})")
            return False
        # Registra o ID para detectar futuros duplicados
        if img_id:
            _seen_image_ids.add(img_id)
        return True
    except Exception as exc:
        if verbose:
            print(f"    [val] erro: {exc}")
        return False


# ── Fontes VTEX (padrão) ──────────────────────────────────────────────────────

def _vtex_store_fetch(base_url: str, nome_fonte: str, ean: str,
                      verbose: bool = False) -> str | None:
    ean_digits = re.sub(r"\D", "", ean)
    if not ean_digits:
        return None
    tentativas = [
        f"{base_url}/api/catalog_system/pub/products/search"
        f"?fq=alternateIdValues:{ean_digits}&_from=0&_to=0&sc=1",
        f"{base_url}/api/catalog_system/pub/products/search"
        f"?ft={ean_digits}&_from=0&_to=0&sc=1",
    ]
    for url in tentativas:
        try:
            req = urllib.request.Request(url, headers=_VTEX_HEADERS)
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as exc:
            if verbose:
                print(f"    [{nome_fonte}] {type(exc).__name__}: {str(exc)[:60]}")
            continue
        if not isinstance(data, list) or not data:
            continue
        for item in data[0].get("items", []):
            for img in item.get("images") or []:
                img_url = (img.get("imageUrl") or "").strip().split("?")[0]
                if not img_url.startswith("http"):
                    continue
                fname = img_url.split("/")[-1].lower()
                label = img.get("imageText") or img.get("imageLabel") or ""
                if _looks_like_other_pharmacy_brand(img_url, fname, label):
                    if verbose:
                        print(f"    [{nome_fonte}] branded, pulando")
                    continue
                if verbose:
                    print(f"    [{nome_fonte}] {img_url[:70]}")
                return img_url
    return None


def _drogasil_fetch(ean: str, verbose: bool = False) -> str | None:
    return _vtex_store_fetch("https://www.drogasil.com.br", "drogasil", ean, verbose)


def _ultrafarma_fetch(ean: str, verbose: bool = False) -> str | None:
    return _vtex_store_fetch("https://www.ultrafarma.com.br", "ultrafarma", ean, verbose)


def _panvel_fetch(ean: str, verbose: bool = False) -> str | None:
    return _vtex_store_fetch("https://www.panvel.com", "panvel", ean, verbose)


# ── Busca candidatos no banco (só cosméticos/higiene) ─────────────────────────

def _buscar_candidatos(cur, categoria: str | None, limite: int,
                       ean_filtro: str | None) -> list[dict]:
    if ean_filtro:
        cur.execute(
            "SELECT barra_norm AS ean, descricao_norm AS nome, id "
            "FROM medicamentos WHERE barra_norm=%s LIMIT 1",
            (ean_filtro,),
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows] or [{"ean": ean_filtro, "nome": ean_filtro, "id": None}]

    keywords = _KEYWORDS.get(categoria, _ALL_KEYWORDS) if categoria else _ALL_KEYWORDS

    # Gera filtros ILIKE para nome de produto
    ilike_parts = " OR ".join(["m.descricao_norm ILIKE %s"] * len(keywords))
    params = [f"%{kw}%" for kw in keywords]

    cur.execute(f"""
        SELECT DISTINCT ON (m.barra_norm) m.barra_norm AS ean,
               m.descricao_norm AS nome, m.id
        FROM medicamentos m
        WHERE m.barra_norm IS NOT NULL
          AND m.barra_norm ~ '^[1-9][0-9]{{7,12}}$'
          AND {_SEM_IMAGEM_COND}
          AND ({ilike_parts})
        ORDER BY m.barra_norm
        LIMIT %s
    """, params + [limite])
    return [dict(r) for r in cur.fetchall()]


def _salvar(cur, ean: str, nome: str, imagem_url: str, fonte: str,
            apply: bool, verbose: bool) -> None:
    url_final = imagem_url
    if apply:
        url_cl = _upload_cloudinary(imagem_url, ean, verbose=verbose)
        if url_cl:
            url_final = url_cl
        elif verbose:
            print(f"    [cloudinary FALHOU] usando URL original")
    if not apply:
        return

    cur.execute("""
        INSERT INTO produto_canon (ean, descricao_canon, imagem_cosmos, fonte, atualizado_em)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (ean) DO UPDATE SET
          imagem_cosmos = EXCLUDED.imagem_cosmos,
          fonte         = EXCLUDED.fonte,
          atualizado_em = NOW()
        WHERE produto_canon.imagem_cosmos IS NULL
           OR TRIM(produto_canon.imagem_cosmos) = ''
           OR produto_canon.imagem_cosmos LIKE '%%CAIXA_GEN%%'
           OR produto_canon.imagem_cosmos LIKE '%%ChatGPT_Image%%'
           OR produto_canon.imagem_cosmos LIKE '%%placeholder%%'
           OR produto_canon.imagem_cosmos LIKE '%%44356%%'
    """, (ean, nome, url_final, fonte))

    cur.execute(f"""
        UPDATE medicamentos SET imagem = %s
        WHERE barra_norm = %s AND {_SEM_IMAGEM_COND_NOALIAS}
    """, (url_final, ean))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Busca imagens de cosméticos/perfumaria/dermocosméticos.\n"
            "EXECUTE LOCALMENTE — sites como Drogasil bloqueiam o IP do servidor."
        )
    )
    parser.add_argument("--apply",       action="store_true",
                        help="Grava no banco + upload Cloudinary")
    parser.add_argument("--limite",      type=int, default=200, metavar="N")
    parser.add_argument("--ean",         help="Processa apenas este EAN")
    parser.add_argument("--categoria",   choices=list(_KEYWORDS.keys()),
                        help="Filtra por categoria (padrao: todas)")
    parser.add_argument("--delay",       type=float, default=0.5, metavar="S")
    parser.add_argument("--commit-cada", type=int, default=30, metavar="N")
    parser.add_argument("--sem-serper",  action="store_true",
                        help="Pula Serper (cota esgotada)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    cosmos_tokens = _cosmos_tokens()
    serper_keys   = _serper_keys() if not args.sem_serper else []

    print("=" * 60)
    print(f"  buscar_imagens_v2 — {'GRAVANDO' if args.apply else 'DRY-RUN'}")
    print(f"  Cosmos: {len(cosmos_tokens)} tokens | Serper: {len(serper_keys)} chaves")
    print(f"  Pillow: {'OK' if _PIL_OK else 'INDISPONIVEL — pip install Pillow'}")
    print("=" * 60)
    print()

    import psycopg2

    def _get_conn():
        """Cria conexão com keepalive para evitar SSL timeout do Supabase."""
        c = db()
        try:
            c.set_session(autocommit=False)
        except Exception:
            pass
        return c

    conn = _get_conn()
    cur  = conn.cursor()

    registros = _buscar_candidatos(cur, args.categoria, args.limite, args.ean)
    cat_label = args.categoria or "todas"
    print(f"Candidatos [{cat_label}]: {len(registros)}")
    if not registros:
        print("Nenhum produto de cosmeticos/higiene encontrado sem imagem.")
        cur.close(); conn.close(); return

    n = {s: 0 for s in (
        "cosmos","dsp","drogasil","beleza","ultrafarma","panvel","raia","serper","sem","inv"
    )}
    pendentes = 0

    def _reconectar():
        """Reconecta ao banco quando a conexão SSL cai."""
        nonlocal conn, cur
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        print("  [db] reconectando...")
        conn = _get_conn()
        cur  = conn.cursor()

    for reg in registros:
        ean  = reg["ean"]
        nome = (reg.get("nome") or ean)[:70]
        achou = False

        if args.verbose:
            print(f"\n[{ean}] {nome}")

        fontes = [
            ("cosmos",     lambda e=ean: _cosmos_fetch_rotating(e, cosmos_tokens, args.verbose) if cosmos_tokens else None),
            ("dsp",        lambda e=ean: (_extrair_dados_vtex(_vtex_fetch(e, args.verbose), args.verbose) or {}).get("imagem")),
            ("drogasil",   lambda e=ean: _drogasil_fetch(e, args.verbose)),
            ("beleza",     lambda e=ean: _beleza_fetch(e, args.verbose)),
            ("ultrafarma", lambda e=ean: _ultrafarma_fetch(e, args.verbose)),
            ("panvel",     lambda e=ean: _panvel_fetch(e, args.verbose)),
            ("raia",       lambda e=ean: (_raia_fetch(e, args.verbose) or {}).get("imagem")),
        ]
        if serper_keys:
            fontes.append(
                ("serper", lambda e=ean, nm=nome: _serper_fetch_rotating(e, nm, serper_keys, args.verbose))
            )

        for fonte_label, fn in fontes:
            if achou:
                break
            time.sleep(args.delay * 0.15)
            try:
                img = fn()
            except Exception as exc:
                if args.verbose:
                    print(f"    [{fonte_label}] erro: {exc}")
                continue
            if not img:
                continue
            # Fontes abertas (serper/cosmos): verifica CDN concorrente também
            cdn_check = fonte_label in ("serper", "cosmos")
            if not _validate_image_url(img, args.verbose, check_competitor_cdn=cdn_check):
                n["inv"] += 1
                continue
            # OCR: rejeita imagem com texto de concorrente OU placeholder genérico
            if fonte_label in ("beleza", "raia", "serper", "dsp", "drogasil", "ultrafarma", "panvel"):
                if _image_has_other_pharmacy_text(img):
                    if args.verbose:
                        print(f"    [{fonte_label}] branding concorrente no OCR, rejeitando")
                    n["inv"] += 1
                    continue
                if _is_placeholder_by_ocr(img):
                    if args.verbose:
                        print(f"    [{fonte_label}] placeholder generico no OCR, rejeitando")
                    n["inv"] += 1
                    continue
            print(f"  [{fonte_label}]  {ean}  {nome[:40]}  {img[:65]}...")
            try:
                _salvar(cur, ean, nome, img, fonte_label, args.apply, args.verbose)
            except psycopg2.OperationalError:
                _reconectar()
                _salvar(cur, ean, nome, img, fonte_label, args.apply, args.verbose)
            n[fonte_label] += 1
            achou = True

        if not achou:
            n["sem"] += 1
            if args.verbose:
                print("  [sem imagem] nenhuma fonte encontrou")

        if achou and args.apply:
            pendentes += 1
            if pendentes >= args.commit_cada:
                try:
                    conn.commit()
                except psycopg2.OperationalError:
                    _reconectar()
                    conn.commit()
                print(f"  [commit] {pendentes} gravadas")
                pendentes = 0

        time.sleep(args.delay)

    total = sum(v for k, v in n.items() if k not in ("sem", "inv"))
    if args.apply:
        if pendentes:
            try:
                conn.commit()
            except psycopg2.OperationalError:
                _reconectar()
                conn.commit()
        print(f"\nGravado." if total else "\nNenhuma imagem nova.")
    else:
        try:
            conn.rollback()
        except Exception:
            pass
        if total:
            print(f"\nDry-run: {total} — use --apply para gravar.")
        else:
            print("\nNenhuma imagem nova encontrada.")

    print(
        f"\nTotal: {len(registros)} | Sem: {n['sem']} | Invalidas: {n['inv']}"
        f"\nCosmos: {n['cosmos']} | DSP: {n['dsp']} | Drogasil: {n['drogasil']}"
        f" | Beleza: {n['beleza']} | Ultrafarma: {n['ultrafarma']}"
        f" | Panvel: {n['panvel']} | Raia: {n['raia']} | Serper: {n['serper']}"
    )
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
