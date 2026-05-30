"""Testa fontes alternativas ao Serper e verifica Cosmos."""
import sys, json, os, urllib.request, urllib.parse, re, time
sys.path.insert(0, '/root/scripts/pedidoeletronico/dns_ecommerce')
from dotenv import load_dotenv
load_dotenv('/root/scripts/pedidoeletronico/.env')

from scripts.buscar_imagens_cosmeticos import _cosmos_tokens
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36'

# EANs de cosméticos brasileiros muito comuns
EANS = [
    ('7896098900028', 'nivea'),
    ('7891150028180', 'pantene condicionador'),
    ('4005900374448', 'nivea body'),
    ('7891035551971', 'johnson baby'),
    ('7896014648672', 'dove sabonete'),
]

cosmos_tokens = _cosmos_tokens()
print(f"Cosmos tokens: {len(cosmos_tokens)}")
print()

for ean, nome in EANS:
    print(f"== {ean} ({nome}) ==")

    # 1. Cosmos direto (sem rotação)
    if cosmos_tokens:
        try:
            req = urllib.request.Request(
                f'https://api.cosmos.bluesoft.com.br/gtins/{ean}',
                headers={
                    'X-Cosmos-Token': cosmos_tokens[0],
                    'User-Agent': 'Cosmos-API-Request',
                    'Content-Type': 'application/json',
                }
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                status = r.status
                d = json.loads(r.read().decode('utf-8', 'ignore'))
            thumb = d.get('thumbnail', '')
            desc = d.get('description', '')
            print(f"  Cosmos: status={status} thumb={thumb[:60] if thumb else 'sem'} desc={desc[:30]}")
        except Exception as e:
            print(f"  Cosmos: ERRO {type(e).__name__}: {e}")

    # 2. Open Beauty Facts (cosméticos)
    try:
        url = f'https://world.openbeautyfacts.org/api/v3/product/{ean}?fields=product_name,image_front_url,image_url'
        req = urllib.request.Request(url, headers={'User-Agent': 'PoupaquiEcommerce/1.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode('utf-8', 'ignore'))
        status = d.get('status', '')
        prod = d.get('product') or {}
        img = prod.get('image_front_url') or prod.get('image_url', '')
        nome_p = prod.get('product_name', '')
        print(f"  OpenBeauty: status={status} nome={nome_p[:30]} img={img[:60] if img else 'sem'}")
    except Exception as e:
        print(f"  OpenBeauty: {type(e).__name__}: {str(e)[:60]}")

    # 3. UPC Item DB (API pública)
    try:
        url = f'https://api.upcitemdb.com/prod/trial/lookup?upc={ean}'
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode('utf-8', 'ignore'))
        items = d.get('items', [])
        if items:
            imgs = items[0].get('images', [])
            title = items[0].get('title', '')
            print(f"  UPCItemDB: titulo={title[:40]} imgs={len(imgs)} first={imgs[0][:60] if imgs else 'sem'}")
        else:
            print(f"  UPCItemDB: sem resultado (code={d.get('code','?')})")
    except Exception as e:
        print(f"  UPCItemDB: {type(e).__name__}: {str(e)[:60]}")

    # 4. Go UPC (outra base de EAN)
    try:
        url = f'https://go-upc.com/api/v1/code/{ean}'
        go_key = os.getenv('GO_UPC_KEY', '')
        headers = {'User-Agent': UA}
        if go_key:
            headers['Authorization'] = f'Bearer {go_key}'
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode('utf-8', 'ignore'))
        prod = d.get('product') or {}
        img = prod.get('imageUrl', '')
        name = prod.get('name', '')
        print(f"  GoUPC: nome={name[:40]} img={img[:60] if img else 'sem'}")
    except Exception as e:
        print(f"  GoUPC: {type(e).__name__}: {str(e)[:60]}")

    # 5. DuckDuckGo Images (scraping HTML)
    try:
        query = urllib.parse.quote(f'{ean} embalagem')
        url = f'https://duckduckgo.com/?q={query}&iax=images&ia=images&format=json'
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            txt = r.read().decode('utf-8', 'ignore')
        # DuckDuckGo não tem JSON diretamente, tenta VQD
        vqd = re.search(r'vqd=([^&"]+)', txt)
        if vqd:
            vqd_val = vqd.group(1)
            url2 = f'https://duckduckgo.com/i.js?q={query}&vqd={vqd_val}&p=1'
            req2 = urllib.request.Request(url2, headers={'User-Agent': UA, 'Referer': 'https://duckduckgo.com/'})
            with urllib.request.urlopen(req2, timeout=8) as r2:
                d2 = json.loads(r2.read().decode('utf-8', 'ignore'))
            results = d2.get('results', [])
            img = results[0].get('image', '') if results else ''
            print(f"  DuckDuckGo: {len(results)} imgs first={img[:60] if img else 'sem'}")
        else:
            print("  DuckDuckGo: sem vqd")
    except Exception as e:
        print(f"  DuckDuckGo: {type(e).__name__}: {str(e)[:60]}")

    print()
    time.sleep(0.5)
