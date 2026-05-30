"""Teste individual de cada fonte de imagem para cosméticos."""
import sys, json, urllib.request, re
sys.path.insert(0, '/root/scripts/pedidoeletronico/dns_ecommerce')
from dotenv import load_dotenv
load_dotenv('/root/scripts/pedidoeletronico/.env')

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'

# EANs de cosméticos/higiene para teste
eans_teste = [
    ('7891150037465', 'shampoo pantene'),
    ('7896014648672', 'sabonete dove'),
    ('7891010956418', 'condicionador seda'),
    ('7898617540017', 'hidratante nivea'),
    ('7896005401029', 'desodorante rexona'),
]

def vtex_fetch(base, ean, nome_fonte):
    for url in [
        f'{base}/api/catalog_system/pub/products/search?fq=alternateIdValues:{ean}&_from=0&_to=0&sc=1',
        f'{base}/api/catalog_system/pub/products/search?ft={ean}&_from=0&_to=0&sc=1',
    ]:
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': UA, 'Accept': 'application/json',
                'Referer': base + '/',
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read().decode('utf-8', 'ignore'))
            if isinstance(d, list) and d:
                for item in d[0].get('items', []):
                    for img in item.get('images') or []:
                        img_url = (img.get('imageUrl') or '').split('?')[0]
                        if img_url.startswith('http'):
                            return img_url
        except Exception as e:
            print(f'    [{nome_fonte}] {type(e).__name__}: {str(e)[:60]}')
    return None

for ean, nome in eans_teste:
    print(f'\n== {ean} ({nome}) ==')

    # Drogasil
    img = vtex_fetch('https://www.drogasil.com.br', ean, 'drogasil')
    print(f'  Drogasil: {img[:70] if img else "sem imagem"}')

    # Onofre (mesma rede Drogasil/RD)
    img = vtex_fetch('https://www.onofre.com.br', ean, 'onofre')
    print(f'  Onofre: {img[:70] if img else "sem imagem"}')

    # Ultrafarma
    img = vtex_fetch('https://www.ultrafarma.com.br', ean, 'ultrafarma')
    print(f'  Ultrafarma: {img[:70] if img else "sem imagem"}')

    # Panvel
    img = vtex_fetch('https://www.panvel.com', ean, 'panvel')
    print(f'  Panvel: {img[:70] if img else "sem imagem"}')

    # MercadoLivre (API pública)
    try:
        url = f'https://api.mercadolibre.com/products/search?status=active&site_id=MLB&q={ean}'
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode('utf-8', 'ignore'))
        results = d.get('results', [])
        if results:
            pic = (results[0].get('pictures') or [{}])[0].get('url', '')
            print(f'  MercadoLivre: {pic[:70] if pic else "sem imagem"}')
        else:
            print('  MercadoLivre: sem resultado')
    except Exception as e:
        print(f'  MercadoLivre: {type(e).__name__}: {str(e)[:60]}')

    # Open Beauty Facts / Open Food Facts (cobre cosméticos)
    for api_base, nome_api in [
        ('https://world.openbeautyfacts.org/api/v2', 'OpenBeautyFacts'),
        ('https://world.openfoodfacts.org/api/v2', 'OpenFoodFacts'),
    ]:
        try:
            url = f'{api_base}/product/{ean}.json?fields=product_name,image_url,image_front_url'
            req = urllib.request.Request(url, headers={'User-Agent': 'PoupaquiEcommerce/1.0'})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode('utf-8', 'ignore'))
            status = d.get('status', 0)
            img = (d.get('product') or {}).get('image_front_url') or (d.get('product') or {}).get('image_url', '')
            print(f'  {nome_api}: status={status} img={img[:70] if img else "nenhuma"}')
        except Exception as e:
            print(f'  {nome_api}: {type(e).__name__}: {str(e)[:60]}')
