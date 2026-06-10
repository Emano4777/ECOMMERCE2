#!/usr/bin/env python3
"""
Corrige medicamentos.laboratorio para produtos com m.marca = distribuidor
(NUCLEO, GOLDEN, etc.) que nao passam pelo fluxo normal de estoque.

Usa o mesmo sistema de IA do corrigir_laboratorios.py, mas busca diretamente
em medicamentos onde marca = nome de distribuidor e laboratorio esta vazio.

Uso:
  python scripts/corrigir_labs_distribuidor.py              # processa ate 500
  python scripts/corrigir_labs_distribuidor.py --limit 200
  python scripts/corrigir_labs_distribuidor.py --dry-run
"""

import os
import json
import time
import argparse
import psycopg2
import psycopg2.extras
from pathlib import Path
from dotenv import load_dotenv
import anthropic

load_dotenv(Path(__file__).parent.parent / ".env")

DATABASE_URL  = os.environ["DATABASE_URL"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL         = "claude-haiku-4-5-20251001"
BATCH_SIZE    = 25

DISTRIBUTOR_NAMES = ("NUCLEO", "GOLDEN", "GOLDEN PHARMA")

SYSTEM_PROMPT = """\
Você é especialista em identificar fabricantes/laboratórios de produtos farmacêuticos \
e de saúde do mercado brasileiro.

Dado um produto com EAN, nome e laboratorio atual (pode estar errado, incompleto ou vazio), \
retorne o nome correto do fabricante/laboratório.

━━━ REGRA 1 — sufixo entre parênteses no nome indica o laboratório ━━━
   (EMS) → EMS            (MED) → MEDLEY          (NEO)/(NC) → NEO QUÍMICA
   (CIM)/(CHR) → CIMED    (ACH)/(AH) → ACHÉ       (EUR) → EUROFARMA
   (GER) → GERMED         (PRA) → PRATI-DONADUZZI  (BST) → BIOSINTÉTICA
   (TEU) → TEUTO          (SAL)/(CRE) → CREMER     (IBI) → IBIS
   (UQ) → UNIÃO QUÍMICA   (BLA) → BLAU             (HYP) → HYPERA
   (ABB) → ABBOTT         (BAY) → BAYER            (NOV) → NOVARTIS
   (SAN) → SANDOZ         (SNF) → SANOFI           (PFI) → PFIZER
   (MSD) → MSD            (TAK) → TAKEDA           (HAR) → HAROFARMA
   UQFAR → UNIÃO QUÍMICA  LEGRAND → LEGRAND        GEOLAB → GEOLAB
   NATULAB → NATULAB      CIMED → CIMED            MULTILAB → MULTILAB
   AIRELA → AIRELA        PHARLAB → PHARLAB        BIOSYN → BIOSINTÉTICA

━━━ REGRA 2 — nome/marca conhecida DENTRO do nome do produto ━━━
Se a marca/fabricante aparece no próprio nome do produto (não só como sufixo), use-a:
   Vitnatu / Vitanatu → VITNATU    Bepantol / Bayer → BAYER
   Curaprox / Curaden → CURADEN   Micropore / 3M → 3M
   Band-Aid / Johnson → JOHNSON & JOHNSON  Neutrogena → JOHNSON & JOHNSON
   Nivea / Eucerin / Beiersdorf → BEIERSDORF
   Dove / Rexona / Seda / Axe → UNILEVER
   Vick / Gillette / Oral-B / Pampers → PROCTER & GAMBLE
   Alivium / Buscopan → BAYER     Bioderma → NAOS
   La Roche-Posay / Vichy / CeraVe → L'ORÉAL  Isdin → ISDIN

━━━ REGRA 3 — última palavra do nome costuma ser o fabricante ━━━
   Para produtos como "FERISEPT SPRAY 45ML UQFAR", a última palavra antes de
   qualquer dose/tamanho pode indicar o fabricante.

━━━ REGRA 4 — quando NÃO identificar ━━━
   Produto genérico sem sufixo E sem marca identificável → laboratorio: null, confianca: "baixa"

━━━ PADRONIZAÇÃO ━━━
   MAIÚSCULAS, sem "S.A.", "LTDA", "DO BRASIL"

━━━ confianca ━━━
"alta"  → certeza (sufixo claro OU marca inconfundível no nome)
"media" → inferência provável
"baixa" → não identificado → retorne laboratorio: null

Retorne SOMENTE array JSON válido, sem markdown.
Exemplo:
[{"ean":"7891800628401","laboratorio":"CREMER","confianca":"alta"},
 {"ean":"7891234000001","laboratorio":null,"confianca":"baixa"}]"""


def fetch_products(conn, limit, skip=0):
    placeholders = ",".join(["%s"] * len(DISTRIBUTOR_NAMES))
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT DISTINCT ON (m.barra)
                LTRIM(COALESCE(m.barra_norm, m.barra::text, ''), '0') AS ean,
                m.descricao AS nome,
                '' AS lab_atual,
                m.marca AS marca
            FROM medicamentos m
            WHERE UPPER(TRIM(m.marca)) IN ({placeholders})
              AND (m.laboratorio IS NULL OR m.laboratorio = '')
              AND COALESCE(m.barra::text, '') != ''
            ORDER BY m.barra, m.id DESC
            LIMIT %s OFFSET %s
        """, list(DISTRIBUTOR_NAMES) + [limit, skip])
        return cur.fetchall()


def correct_batch(client, batch):
    items = [
        {
            "ean":       r["ean"],
            "nome":      (r["nome"] or "").strip(),
            "lab_atual": "",
            "marca":     "",
        }
        for r in batch
    ]
    msg = client.messages.create(
        model=MODEL,
        max_tokens=3000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Identifique o laboratorio correto para estes {len(items)} produtos "
                f"(a marca atual é um distribuidor, não o fabricante):\n"
                + json.dumps(items, ensure_ascii=False, indent=2)
            ),
        }],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(raw)


def save_results(conn, results, dry_run=False, min_confianca="alta"):
    aceitar = {"alta"}
    if min_confianca == "media":
        aceitar = {"alta", "media"}
    validos = [(r["laboratorio"], r["ean"]) for r in results if r.get("confianca") in aceitar and r.get("laboratorio")]
    if not validos:
        return 0
    if dry_run:
        for lab, ean in validos:
            print(f"  [DRY] UPDATE medicamentos SET laboratorio='{lab}' WHERE barra_norm/barra = '{ean}'")
        return len(validos)
    with conn.cursor() as cur:
        for lab, ean in validos:
            cur.execute("""
                UPDATE medicamentos
                SET laboratorio = %s
                WHERE LTRIM(COALESCE(barra_norm, barra::text, ''), '0') = %s
                  AND (laboratorio IS NULL OR laboratorio = '')
            """, (lab, ean))
    conn.commit()
    return len(validos)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",         type=int, default=2000)
    parser.add_argument("--skip",          type=int, default=0)
    parser.add_argument("--batch-size",    type=int, default=BATCH_SIZE)
    parser.add_argument("--dry-run",       action="store_true")
    parser.add_argument("--min-confianca", choices=["alta", "media"], default="alta",
                        help="Confiança mínima para salvar (alta=só certeza, media=aceita inferência provável)")
    args = parser.parse_args()

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    conn   = psycopg2.connect(DATABASE_URL)

    print(f"Buscando produtos com marca = distribuidor sem laboratorio...")
    print(f"  min-confianca: {args.min_confianca}")
    products = fetch_products(conn, args.limit, args.skip)
    total = len(products)
    print(f"  {total} produto(s) para processar\n")

    if not total:
        print("Nenhum produto encontrado.")
        conn.close()
        return

    updated_total = 0
    for i in range(0, total, args.batch_size):
        batch = products[i:i + args.batch_size]
        batch_n = len(batch)
        print(f"Lote {i // args.batch_size + 1}: {batch_n} items ({i+1}–{i+batch_n}/{total})")
        try:
            results = correct_batch(client, batch)
            updated = save_results(conn, results, dry_run=args.dry_run, min_confianca=args.min_confianca)
            updated_total += updated
            alta  = sum(1 for r in results if r.get("confianca") == "alta"  and r.get("laboratorio"))
            media = sum(1 for r in results if r.get("confianca") == "media" and r.get("laboratorio"))
            print(f"  alta={alta}  media={media}  salvo={updated}")
        except Exception as e:
            print(f"  ERRO no lote: {e}")
        time.sleep(0.5)

    print(f"\nConcluido: {updated_total} laboratorios atualizados em medicamentos.")
    conn.close()


if __name__ == "__main__":
    main()
