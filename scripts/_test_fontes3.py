"""Testa DSP, Raia, Beleza e Cosmos com EANs que sabemos existir neles."""
import sys, time
sys.path.insert(0, '/root/scripts/pedidoeletronico/dns_ecommerce')
from dotenv import load_dotenv
load_dotenv('/root/scripts/pedidoeletronico/.env')

from scripts.revalidar_tarja_vtex import _vtex_fetch, _raia_fetch, _extrair_dados_vtex
from scripts.buscar_imagens_cosmeticos import (
    _beleza_fetch, _cosmos_fetch_rotating, _cosmos_tokens,
)

cosmos_tokens = _cosmos_tokens()
print(f"Cosmos tokens: {len(cosmos_tokens)}\n")

# EANs que o log mostrou que DSP encontrou + outros conhecidos
EANS = [
    ('4005900940759', 'esfoliante nivea'),   # encontrado pelo script existente
    ('4005900945709', 'sabonete nivea gel'), # encontrado pelo script existente
    ('7896014648672', 'dove sabonete'),
    ('7891024130780', 'dove sabonete 2'),
    ('7896098900028', 'nivea hidratante'),
    ('7891150037465', 'pantene shampoo'),
    ('7891058002602', 'dipirona sodica'),    # medicamento que DSP achou antes
]

for ean, nome in EANS:
    print(f"== {ean} ({nome}) ==")

    # Cosmos (single token para testar)
    if cosmos_tokens:
        try:
            import urllib.request, json
            req = urllib.request.Request(
                f'https://api.cosmos.bluesoft.com.br/gtins/{ean}',
                headers={'X-Cosmos-Token': cosmos_tokens[0], 'User-Agent': 'Cosmos-API-Request'},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode('utf-8', 'ignore'))
            thumb = d.get('thumbnail', '')
            desc = d.get('description', d.get('description', ''))[:30]
            print(f"  Cosmos: {thumb[:65] if thumb else 'sem imagem'} ({desc})")
        except Exception as e:
            print(f"  Cosmos: {type(e).__name__}: {str(e)[:60]}")

    # DSP
    try:
        p = _vtex_fetch(ean, verbose=False)
        d = _extrair_dados_vtex(p, verbose=False) if p else None
        img = (d or {}).get('imagem')
        print(f"  DSP:    {img[:65] if img else 'sem imagem'}")
    except Exception as e:
        print(f"  DSP:    {type(e).__name__}: {str(e)[:60]}")

    # Raia
    try:
        d = _raia_fetch(ean, verbose=False)
        img = (d or {}).get('imagem')
        print(f"  Raia:   {img[:65] if img else 'sem imagem'}")
    except Exception as e:
        print(f"  Raia:   {type(e).__name__}: {str(e)[:60]}")

    # Beleza na Web
    try:
        img = _beleza_fetch(ean, verbose=False)
        print(f"  Beleza: {img[:65] if img else 'sem imagem'}")
    except Exception as e:
        print(f"  Beleza: {type(e).__name__}: {str(e)[:60]}")

    print()
    time.sleep(1.0)
