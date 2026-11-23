"""Public national showcase, restricted to this store's registered ML listings."""
import math
import re
import threading
import time
import unicodedata
from urllib.parse import urlencode, urlsplit

STORE = "54185432000143"
SELLER = "3501375634"
TTL = 120
_snapshot = (0, [])
_lock = threading.Lock()


def normalized(value):
    return "".join(c for c in unicodedata.normalize("NFKD", str(value or "").lower())
                   if not unicodedata.combining(c))


def public_item(item):
    """Fail closed for paused/deleted/no-stock/other-seller listings and unsafe URLs."""
    if (str(item.get("seller_id")) != SELLER or item.get("status") != "active"
            or item.get("sub_status") or int(item.get("available_quantity") or 0) <= 0):
        return None
    price = float(item.get("price") or 0)
    if not math.isfinite(price) or price <= 0 or item.get("currency_id") != "BRL":
        return None
    link = urlsplit(item.get("permalink") or "")
    if (link.scheme != "https" or link.hostname not in
            ("produto.mercadolivre.com.br", "www.mercadolivre.com.br", "mercadolivre.com.br")
            or link.username or link.password or link.port not in (None, 443)):
        return None
    attrs = {a["id"]: a.get("value_name") for a in item.get("attributes", [])}
    # The discontinued Kids batch must never return through a related catalog listing.
    if attrs.get("GTIN") == "7897230302454":
        return None
    image = next((p.get("secure_url") for p in item.get("pictures", [])
                  if urlsplit(p.get("secure_url") or "").scheme == "https"
                  and (urlsplit(p.get("secure_url") or "").hostname or "").endswith(".mlstatic.com")), None)
    if not image:
        return None
    return {"id": item["id"], "nome": item.get("title") or "", "ean": attrs.get("GTIN") or "",
            "preco": price, "imagem": image, "url": item["permalink"],
            "marca": attrs.get("BRAND") or "", "categoria": item.get("category_id"),
            "origem": "mercado_livre"}


def matching(products, query):
    terms = re.findall(r"[a-z0-9]+", normalized(query))
    terms = [t for t in terms if t not in {"de", "do", "da", "com", "para", "e", "o", "a"}]
    aliases = {"protetores": "protetor", "solares": "solar", "perfumes": "perfume", "suplementos": "suplemento"}
    terms = [aliases.get(t, t) for t in terms]
    return [p for p in products if all(t in normalized(" ".join([
        p["nome"], p["ean"], p["marca"],
        {"MLB199648": "protetor solar protecao solar", "MLB6284": "perfume perfumaria fragrancia",
         "MLB264201": "suplemento nutricao"}.get(p["categoria"], "")])) for t in terms)]


def catalog(load_ids, get_token, api_get, query=""):
    global _snapshot
    now = time.monotonic()
    if now >= _snapshot[0]:
        # No repeated cold-start requests per worker; never serve expired stock on failure.
        with _lock:
            if time.monotonic() >= _snapshot[0]:
                products = []
                try:
                    ids = list(dict.fromkeys(load_ids()))[:200]
                    token = get_token(cnpjloja=STORE) if ids else None
                    if ids and not token:
                        raise RuntimeError("ML token unavailable")
                    for start in range(0, len(ids), 20):
                        result = api_get("/items?" + urlencode({"ids": ",".join(ids[start:start + 20])}), token)
                        if not isinstance(result, list):
                            raise RuntimeError("ML listing read failed")
                        for row in result:
                            if row.get("code") == 200:
                                p = public_item(row.get("body") or {})
                                if p:
                                    products.append(p)
                    # One customer card per EAN, even if DB later includes catalog twins.
                    unique = {}
                    for p in products:
                        unique.setdefault(p["ean"] or p["id"], p)
                    products = list(unique.values())
                except Exception:
                    products = []
                _snapshot = (time.monotonic() + (TTL if products else 15), products)
    return matching(_snapshot[1], query)
