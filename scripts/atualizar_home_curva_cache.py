"""Preaquece o snapshot de curva A usado pela home.

Uso:
  python scripts/atualizar_home_curva_cache.py
  python scripts/atualizar_home_curva_cache.py --lat -23.55 --lng -46.63
"""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import (  # noqa: E402
    _home_public_cnpjs,
    _home_curva_cache_key,
    _build_home_curva_payload,
    _home_curva_cache_set,
)


def main():
    parser = argparse.ArgumentParser(description="Atualiza cache da home por curva A.")
    parser.add_argument("--lat", type=float, default=0.0)
    parser.add_argument("--lng", type=float, default=0.0)
    parser.add_argument("--ttl-min", type=int, default=30)
    args = parser.parse_args()

    cnpjs, sem_farmacia = _home_public_cnpjs(args.lat, args.lng)
    if not cnpjs:
        print("Nenhuma loja publica encontrada.")
        return 1
    cache_key = _home_curva_cache_key(cnpjs)
    payload = _build_home_curva_payload(cnpjs, sem_farmacia)
    _home_curva_cache_set(cache_key, payload, ttl_minutes=args.ttl_min)
    print(f"Cache atualizado: {cache_key} | lojas={len(cnpjs)} | produtos={len(payload.get('produtos') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
