"""Testa o que REALMENTE funciona no servidor: Cosmos, DSP, Raia, Beleza, Serper."""
import sys, json, os, urllib.request, re, time
sys.path.insert(0, '/root/scripts/pedidoeletronico/dns_ecommerce')
from dotenv import load_dotenv
load_dotenv('/root/scripts/pedidoeletronico/.env')

# EANs de cosméticos/higiene que provavelmente existem nos sites
EANS = [
    ('7891150037465', 'pantene shampoo'),
    ('7896098900028', 'nivea hidratante'),
    ('7891024130780', 'dove sabonete'),
    ('7500435141376', 'head shoulders'),
    ('7896014648672', 'dove sabonete 2'),
]

from scripts.revalidar_tarja_vtex import _vtex_fetch, _raia_fetch, _extrair_dados_vtex, _UA
from scripts.buscar_imagens_cosmeticos import (
    _beleza_fetch, _cosmos_fetch_rotating, _serper_fetch_rotating,
    _cosmos_tokens, _serper_keys,
)

cosmos_tokens = _cosmos_tokens()
serper_keys   = _serper_keys()
print(f"Cosmos tokens: {len(cosmos_tokens)}")
print(f"Serper keys:   {len(serper_keys)}")
print()

for ean, nome in EANS:
    print(f"== {ean} ({nome}) ==")

    # Cosmos
    if cosmos_tokens:
        try:
            img = _cosmos_fetch_rotating(ean, cosmos_tokens, verbose=False)
            print(f"  Cosmos:  {img[:70] if img else 'sem imagem'}")
        except Exception as e:
            print(f"  Cosmos:  ERRO {e}")
    else:
        print("  Cosmos:  sem tokens")

    # DSP
    try:
        p = _vtex_fetch(ean, verbose=False)
        d = _extrair_dados_vtex(p, verbose=False) if p else None
        img = (d or {}).get('imagem')
        print(f"  DSP:     {img[:70] if img else 'sem imagem'}")
    except Exception as e:
        print(f"  DSP:     ERRO {e}")

    # Raia
    try:
        d = _raia_fetch(ean, verbose=False)
        img = (d or {}).get('imagem')
        print(f"  Raia:    {img[:70] if img else 'sem imagem'}")
    except Exception as e:
        print(f"  Raia:    ERRO {e}")

    # Beleza na Web
    try:
        img = _beleza_fetch(ean, verbose=False)
        print(f"  Beleza:  {img[:70] if img else 'sem imagem'}")
    except Exception as e:
        print(f"  Beleza:  ERRO {e}")

    # Serper
    if serper_keys:
        try:
            img = _serper_fetch_rotating(ean, nome, serper_keys, verbose=False)
            print(f"  Serper:  {img[:70] if img else 'sem imagem'}")
        except Exception as e:
            print(f"  Serper:  ERRO {e}")
    else:
        print("  Serper:  sem chaves")

    print()
    time.sleep(0.5)
