"""
Preenche ecommerce_classificacao_ean direto do campo `classificacao` que o
Alpha ja manda por produto (ex: "PRINCIPAL > HPC > INFANTIL"), sem chamar a
Anthropic. Mais rapido, deterministico e sem falha de parsing de IA.

So cobre produtos cujo 2o nivel da classificacao Alpha e reconhecido (ver
_ALPHA_CLASSIFICACAO_CATEGORIA em app.py) — os "OUTROS"/nao mapeados ficam
de fora e continuam dependendo do classificador por IA (classificar_produtos.py).

Uso:
    python scripts/backfill_categoria_alpha.py --dry-run
    python scripts/backfill_categoria_alpha.py                 # so os sem classificacao
    python scripts/backfill_categoria_alpha.py --force         # reclassifica todos os EANs Alpha reconhecidos
"""
import os
import argparse

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

DATABASE_URL = os.environ["DATABASE_URL"]

# Mesmo mapeamento de app.py::_ALPHA_CLASSIFICACAO_CATEGORIA — manter em sincronia.
_ALPHA_CLASSIFICACAO_CATEGORIA = {
    "HPC": "perfumaria",
    "VAREJO": "varejo",
    "NUTRACEUTICOS": "suplemento",
    "DERMOCOSMETICO": "dermocosmetico",
    "MARCA": "referencia",
    "SIMILAR": "similar",
    "GENERICO": "generico",
    "ETICO": "referencia",
    "REFERENCIA": "referencia",
    "MEDICAMENTOS": "referencia",
}

_TIPO_TO_CATEGORIA = {
    "generico": "medicamento",
    "similar": "medicamento",
    "referencia": "medicamento",
    "suplemento": "suplemento",
    "perfumaria": "perfumaria",
    "varejo": "varejo",
    "dermocosmetico": "dermocosmetico",
}


def categoria_from_classificacao(classificacao):
    if not classificacao:
        return None
    partes = [p.strip().upper() for p in str(classificacao).split(">") if p.strip()]
    if len(partes) < 2:
        return None
    return _ALPHA_CLASSIFICACAO_CATEGORIA.get(partes[1])


def main():
    ap = argparse.ArgumentParser(description="Backfill de categoria direto do Alpha, sem IA")
    ap.add_argument("--dry-run", action="store_true", help="nao grava, so reporta")
    ap.add_argument("--force", action="store_true", help="reclassifica mesmo EANs ja classificados")
    args = ap.parse_args()

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT ON (LTRIM(COALESCE(ap.ean,''),'0'))
               LTRIM(COALESCE(ap.ean,''),'0') AS ean_key, ap.ean, ap.nome, ap.classificacao
        FROM ecommerce_alpha_produtos ap
        WHERE COALESCE(ap.inativo,false)=false AND COALESCE(ap.estoque,0)>0
        ORDER BY LTRIM(COALESCE(ap.ean,''),'0'), ap.estoque DESC
    """)
    produtos = [dict(r) for r in cur.fetchall()]
    print(f"Total EANs ativos: {len(produtos)}")

    if not args.force:
        cur.execute("SELECT ean FROM ecommerce_classificacao_ean")
        ja_classificados = {r["ean"] for r in cur.fetchall()}
        produtos = [p for p in produtos if p["ean"] not in ja_classificados]
        print(f"Sem classificacao ainda: {len(produtos)}")

    candidatos = []
    sem_mapeamento = 0
    for p in produtos:
        tipo = categoria_from_classificacao(p.get("classificacao"))
        if not tipo:
            sem_mapeamento += 1
            continue
        candidatos.append((p["ean"], p["nome"], tipo, _TIPO_TO_CATEGORIA.get(tipo, tipo)))

    print(f"Mapeados via classificacao Alpha: {len(candidatos)}")
    print(f"Sem 2o nivel reconhecido (ficam pro classificador IA): {sem_mapeamento}")

    from collections import Counter
    dist = Counter(tipo for _, _, tipo, _ in candidatos)
    print("Distribuicao do backfill:")
    for tipo, count in dist.most_common():
        print(f"  {tipo}: {count}")

    if args.dry_run:
        print("\n--dry-run: nada foi gravado.")
        cur.close()
        conn.close()
        return

    for ean, nome, tipo, categoria in candidatos:
        cur.execute(
            """
            INSERT INTO ecommerce_classificacao_ean
                (ean, nome_ref, tipo, categoria, confianca, modelo_ia, classificado_em)
            VALUES (%s, %s, %s, %s, 'alta', 'alpha_classificacao_direta', NOW())
            ON CONFLICT (ean) DO UPDATE SET
                tipo = EXCLUDED.tipo,
                categoria = EXCLUDED.categoria,
                nome_ref = EXCLUDED.nome_ref,
                confianca = 'alta',
                modelo_ia = 'alpha_classificacao_direta',
                classificado_em = NOW()
            """,
            (ean, nome, tipo, categoria),
        )
    conn.commit()
    print(f"\nGravado: {len(candidatos)} EANs classificados via Alpha (sem IA).")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
