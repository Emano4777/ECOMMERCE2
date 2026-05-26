#!/usr/bin/env python3
"""
Identifica e corrige o laboratorio/fabricante de todos os produtos do ecommerce
usando Claude (Anthropic). Varre o mesmo inventário do classificar_produtos.py
(estoque + automatiza_estoque + omie_estoque_dns) por EAN em lote.

Salva em:
  ecommerce_lab_ean          → tabela canônica (lab por EAN, todos os produtos)
  medicamentos.laboratorio   → atualizado quando confiança = alta
  produto_canon.laboratorio  → atualizado quando confiança = alta

Uso (rodar da raiz do projeto):
  python scripts/corrigir_laboratorios.py               # só EANs sem lab
  python scripts/corrigir_laboratorios.py --force       # reprocessa todos
  python scripts/corrigir_laboratorios.py --limit 200
  python scripts/corrigir_laboratorios.py --dry-run
  python scripts/corrigir_laboratorios.py --ean 7891800628401
  python scripts/corrigir_laboratorios.py --stats
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

# ── SCHEMA ──────────────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS ecommerce_lab_ean (
    ean           TEXT PRIMARY KEY,
    nome_ref      TEXT,
    laboratorio   TEXT,
    confianca     TEXT DEFAULT 'media',
    modelo_ia     TEXT,
    processado_em TIMESTAMPTZ DEFAULT NOW()
);
"""


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()
    print("Tabela ecommerce_lab_ean garantida.")


# ── BUSCA (mesmo inventário do classificar_produtos.py) ──────────────────────

FETCH_SQL = """
WITH inv AS (
    SELECT LTRIM(COALESCE(ean_norm, ''), '0') AS ean_key, '' AS descricao
    FROM omie_estoque_dns
    WHERE ean_norm IS NOT NULL AND ean_norm != ''

    UNION

    SELECT LTRIM(COALESCE(barras_norm, barras, ''), '0') AS ean_key, descricao
    FROM estoque
    WHERE estoque > 0
      AND COALESCE(barras_norm, barras, '') != ''
      AND (
          COALESCE(barras_norm, barras) IN (SELECT ean_norm FROM omie_estoque_dns)
          OR COALESCE(barras_norm, barras) IN (
              SELECT barra_norm FROM medicamentos
              WHERE barra_norm IS NOT NULL AND barra_norm != ''
          )
      )

    UNION

    SELECT LTRIM(COALESCE(ean::text, ''), '0') AS ean_key, '' AS descricao
    FROM automatiza_estoque
    WHERE quantidade_estoque > 0
      AND ean IS NOT NULL AND ean::text != ''
      AND (
          ean IN (SELECT ean_norm FROM omie_estoque_dns)
          OR ean IN (
              SELECT barra_norm FROM medicamentos
              WHERE barra_norm IS NOT NULL AND barra_norm != ''
          )
      )
)
SELECT DISTINCT ON (inv.ean_key)
    inv.ean_key                                                      AS ean,
    COALESCE(m.descricao, pc.descricao_canon, inv.descricao)        AS nome,
    COALESCE(pc.laboratorio, m.laboratorio, '')                     AS lab_atual,
    COALESCE(m.marca, '')                                            AS marca,
    COALESCE(m.classe, pc.categoria, '')                             AS categoria
FROM inv
LEFT JOIN medicamentos m
       ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = inv.ean_key
LEFT JOIN produto_canon pc
       ON LTRIM(COALESCE(pc.ean, ''), '0') = inv.ean_key
      AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss')
WHERE inv.ean_key != '' AND inv.ean_key IS NOT NULL
{extra_where}
ORDER BY inv.ean_key, (m.id IS NOT NULL) DESC
{limit_clause}
"""


def fetch_products(conn, force=False, limit=None, ean_filter=None):
    parts = []
    if not force and ean_filter is None:
        parts.append(
            "AND inv.ean_key NOT IN (SELECT ean FROM ecommerce_lab_ean WHERE laboratorio IS NOT NULL)"
        )
    if ean_filter:
        safe = ean_filter.lstrip("0").replace("'", "")
        parts.append(f"AND inv.ean_key = '{safe}'")

    sql = FETCH_SQL.format(
        extra_where=" ".join(parts),
        limit_clause=f"LIMIT {limit}" if limit else "",
    )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return cur.fetchall()


# ── SALVAR ───────────────────────────────────────────────────────────────────

UPSERT_LAB = """
INSERT INTO ecommerce_lab_ean (ean, nome_ref, laboratorio, confianca, modelo_ia)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (ean) DO UPDATE SET
    nome_ref      = EXCLUDED.nome_ref,
    laboratorio   = EXCLUDED.laboratorio,
    confianca     = EXCLUDED.confianca,
    modelo_ia     = EXCLUDED.modelo_ia,
    processado_em = NOW()
"""

UPDATE_MED = """
UPDATE medicamentos
SET laboratorio = %s
WHERE LTRIM(COALESCE(barra_norm, barra, ''), '0') = %s
  AND (%s = 'alta')
"""

UPDATE_PC = """
UPDATE produto_canon
SET laboratorio = %s
WHERE LTRIM(COALESCE(ean, ''), '0') = %s
  AND (%s = 'alta')
"""


def save_corrections(conn, results):
    lab_rows = []
    med_rows = []
    pc_rows  = []

    for r in results:
        ean  = (r.get("ean") or "").lstrip("0")
        lab  = r.get("laboratorio")          # pode ser None (não identificado)
        conf = (r.get("confianca") or "media").lower()
        nome = (r.get("_nome") or "")

        if not ean:
            continue

        lab_rows.append((ean, nome, lab, conf, MODEL))

        if lab and conf == "alta":
            med_rows.append((lab, ean, conf))
            pc_rows.append((lab, ean, conf))

    with conn.cursor() as cur:
        if lab_rows:
            cur.executemany(UPSERT_LAB, lab_rows)
        if med_rows:
            cur.executemany(UPDATE_MED, med_rows)
        if pc_rows:
            cur.executemany(UPDATE_PC, pc_rows)
    conn.commit()


# ── CLAUDE ───────────────────────────────────────────────────────────────────

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

━━━ REGRA 2 — nome/marca conhecida DENTRO do nome do produto ━━━
Se a marca/fabricante aparece no próprio nome do produto (não só como sufixo), use-a:
   Vitnatu / Vitanatu              → VITNATU
   Curaprox / Curaden              → CURADEN
   Condor                          → CONDOR
   Strepsils / Reckitt             → RECKITT BENCKISER
   Micropore / Cavilon / 3M        → 3M
   Nexcare                         → 3M
   Band-Aid / Curativo Johnson     → JOHNSON & JOHNSON
   Bepantol / Bayer                → BAYER
   Salvelox                        → CREMER
   Neutrogena                      → JOHNSON & JOHNSON
   Nivea / Eucerin / Beiersdorf    → BEIERSDORF
   Dove / Rexona / Seda / Axe      → UNILEVER
   Vick / Gillette / Oral-B / Always / Pampers → PROCTER & GAMBLE
   Koleston / Pantene / Head & Shoulders       → PROCTER & GAMBLE
   Sempre Livre / Whisper          → JOHNSON & JOHNSON (Whisper/Procter & Gamble)
   Mantecorp / Farmasa             → MANTECORP FARMASA
   Laboratoires Thea / Thealoz / Hyabak        → LABORATOIRES THEA
   Alivium / Buscopan / Bayer      → BAYER
   Predsim / Cosme                 → COSME
   Bioderma                        → NAOS
   La Roche-Posay / Vichy / CeraVe → L'ORÉAL
   Isdin                           → ISDIN
   Darrow / Cimed                  → CIMED
   Hypera / Neo Química            → HYPERA / NEO QUÍMICA

━━━ REGRA 3 — lab_atual como pista ━━━
   Se lab_atual está preenchido e parece correto para o produto, retorne-o normalizado
   (sem "S.A.", "LTDA", "DO BRASIL", "INDUSTRIA E COMERCIO") com confianca "media".

━━━ REGRA 4 — quando NÃO identificar ━━━
   Produto com nome vazio OU genérico sem sufixo E sem marca identificável no nome
   (ex: "PARACETAMOL 500MG" sem sufixo, sem lab_atual) → laboratorio: null, confianca: "baixa"

━━━ PADRONIZAÇÃO ━━━
   MAIÚSCULAS, sem "S.A.", "LTDA", "DO BRASIL" → "EMS" não "E.M.S. S/A FARMACÊUTICA"

━━━ confianca ━━━
"alta"  → certeza (sufixo claro OU marca inconfundível no nome)
"media" → inferência provável (lab_atual parece correto, ou nome sugere o fab. sem certeza total)
"baixa" → não identificado → retorne laboratorio: null

Retorne SOMENTE array JSON válido, sem markdown.
Exemplo:
[{"ean":"7891800628401","laboratorio":"CREMER","confianca":"alta"},
 {"ean":"7891234000001","laboratorio":null,"confianca":"baixa"}]"""


def correct_batch(client, batch):
    items = [
        {
            "ean":       r["ean"],
            "nome":      (r["nome"] or "").strip(),
            "lab_atual": (r["lab_atual"] or "").strip(),
            "marca":     (r["marca"] or "").strip(),
            "categoria": (r["categoria"] or "").strip(),
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
                f"Identifique o laboratorio correto para estes {len(items)} produtos:\n"
                + json.dumps(items, ensure_ascii=False, indent=2)
            ),
        }],
    )

    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    corrected = json.loads(raw)

    ean_to_nome = {r["ean"]: r["nome"] for r in batch}
    for r in corrected:
        r["_nome"] = ean_to_nome.get(r.get("ean", ""), "")

    return corrected


# ── STATS ────────────────────────────────────────────────────────────────────

def print_stats(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT laboratorio AS lab, COUNT(*) AS cnt
            FROM ecommerce_lab_ean
            WHERE laboratorio IS NOT NULL AND laboratorio != ''
            GROUP BY lab ORDER BY cnt DESC LIMIT 25
        """)
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS total FROM ecommerce_lab_ean")
        total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) AS c FROM ecommerce_lab_ean WHERE laboratorio IS NOT NULL")
        done = cur.fetchone()["c"]

    print(f"\n{'='*58}")
    print(f"ecommerce_lab_ean: {done} com lab identificado / {total} total")
    print(f"{'─'*58}")
    for r in rows:
        bar = "█" * min(int(r["cnt"] / 2), 32)
        print(f"  {(r['lab'] or 'NULL'):<30}  {r['cnt']:>5}  {bar}")
    print(f"{'='*58}\n")


# ── MAIN ─────────────────────────────────────────────────────────────────────

CONF_COR = {"alta": "\033[32m", "media": "\033[33m", "baixa": "\033[31m"}
RESET    = "\033[0m"


def main():
    parser = argparse.ArgumentParser(description="Corrige laboratorios do ecommerce com Claude")
    parser.add_argument("--force",      action="store_true", help="Reprocessa todos os EANs")
    parser.add_argument("--limit",      type=int,  default=None)
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--batch-size", type=int,  default=BATCH_SIZE)
    parser.add_argument("--ean",        type=str,  default=None)
    parser.add_argument("--skip",       type=int,  default=0)
    parser.add_argument("--stats",      action="store_true")
    args = parser.parse_args()

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    conn   = psycopg2.connect(DATABASE_URL)

    ensure_schema(conn)

    if args.stats:
        print_stats(conn)
        conn.close()
        return

    print("Buscando produtos do catálogo ativo do ecommerce...")
    products = fetch_products(
        conn,
        force=args.force or bool(args.ean),
        limit=args.limit,
        ean_filter=args.ean,
    )

    if args.skip:
        products = products[args.skip:]
        print(f"  Pulando primeiros {args.skip} (--skip).")

    total = len(products)
    print(f"  {total} produto(s) para processar\n")

    if not total:
        print("Nada a fazer. Use --force para reprocessar todos.")
        conn.close()
        return

    corrected_total = 0
    error_batches   = 0
    batch_size      = args.batch_size

    for i in range(0, total, batch_size):
        batch   = products[i : i + batch_size]
        start   = i + 1
        end     = min(i + batch_size, total)
        print(f"[{start:>5} – {end:<5} / {total}]  enviando para Claude...")

        attempt = 0
        results = None
        while attempt < 3:
            attempt += 1
            try:
                results = correct_batch(client, batch)
                break
            except Exception as exc:
                wait = attempt * 6
                print(f"  ⚠  Tentativa {attempt} falhou: {exc}")
                if attempt < 3:
                    time.sleep(wait)

        if results is None:
            error_batches += 1
            print("  ✗  Batch ignorado após 3 tentativas.\n")
            continue

        if not args.dry_run:
            save_corrections(conn, results)

        corrected_total += len(results)
        for r in results:
            lab  = r.get("laboratorio") or "(não identificado)"
            conf = (r.get("confianca") or "?").lower()
            cor  = CONF_COR.get(conf, "")
            nome = (r.get("_nome") or "")[:44]
            print(f"  {r.get('ean','?'):>14}  {cor}{lab:<26}{RESET}  [{conf[0].upper()}]  {nome}")

        time.sleep(0.4)
        print()

    if not args.dry_run and corrected_total > 0:
        print_stats(conn)

    conn.close()
    print("=" * 60)
    print(f"Concluído: {corrected_total} processados, {error_batches} batch(es) com falha")
    if args.dry_run:
        print("(dry-run: nada gravado)")
    else:
        print("Gravado em: ecommerce_lab_ean (todos os EANs do catálogo ativo)")
        print("Atualizado: medicamentos.laboratorio + produto_canon.laboratorio (confiança alta)")


if __name__ == "__main__":
    main()
