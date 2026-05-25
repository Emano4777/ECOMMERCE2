#!/usr/bin/env python3
"""
Classifica os registros da tabela `medicamentos` usando Claude (Anthropic).
Usa barra_norm (EAN) como chave única — sem ambiguidade.

Adiciona 3 colunas novas em `medicamentos` (não sobrescreve `classe`):
  tipo_ia               → generico | similar | referencia | suplemento | cosmetico | outro
  principio_ativo_ia    → princípio ativo principal (medicamentos)
  classe_terapeutica_ia → ex: "Analgésico", "Antibiótico" (medicamentos)

Uso:
  python scripts/classificar_medicamentos.py                # apenas não classificados
  python scripts/classificar_medicamentos.py --force        # reclassifica todos
  python scripts/classificar_medicamentos.py --limit 500    # limita qtd
  python scripts/classificar_medicamentos.py --dry-run      # não grava no banco
  python scripts/classificar_medicamentos.py --ean 7891234  # EAN específico
"""

import os
import sys
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
BATCH_SIZE    = 30

# ── SCHEMA ──────────────────────────────────────────────────────────────────

def ensure_columns(conn):
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE medicamentos ADD COLUMN IF NOT EXISTS tipo_ia TEXT")
        cur.execute("ALTER TABLE medicamentos ADD COLUMN IF NOT EXISTS principio_ativo_ia TEXT")
        cur.execute("ALTER TABLE medicamentos ADD COLUMN IF NOT EXISTS classe_terapeutica_ia TEXT")
        cur.execute("ALTER TABLE medicamentos ADD COLUMN IF NOT EXISTS confianca_ia TEXT")
    conn.commit()
    print("Colunas tipo_ia / principio_ativo_ia / classe_terapeutica_ia / confianca_ia garantidas.")


# ── BUSCA DE PRODUTOS ────────────────────────────────────────────────────────

def fetch_products(conn, force=False, limit=None, ean_filter=None):
    conditions = [
        "barra_norm IS NOT NULL",
        "barra_norm != ''",
    ]
    if not force and ean_filter is None:
        conditions.append("tipo_ia IS NULL")
    if ean_filter:
        safe = ean_filter.lstrip("0")
        conditions.append(f"barra_norm = '{safe}'")

    where = " AND ".join(conditions)
    limit_clause = f"LIMIT {limit}" if limit else ""

    sql = f"""
        SELECT id, barra_norm, descricao, classe, laboratorio, marca
        FROM medicamentos
        WHERE {where}
        ORDER BY id
        {limit_clause}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return cur.fetchall()


# ── SALVAR ───────────────────────────────────────────────────────────────────

TIPOS_VALIDOS = {
    "generico", "similar", "referencia",
    "suplemento", "perfumaria", "correlato", "nutricao", "varejo",
}

UPDATE_SQL = """
UPDATE medicamentos SET
    tipo_ia               = %s,
    principio_ativo_ia    = %s,
    classe_terapeutica_ia = %s,
    confianca_ia          = %s
WHERE barra_norm = %s
"""


def save_classifications(conn, results):
    rows = []
    for r in results:
        tipo = (r.get("tipo") or "outro").lower().strip()
        if tipo not in TIPOS_VALIDOS:
            tipo = "outro"
        rows.append((
            tipo,
            r.get("principio_ativo"),
            r.get("classe_terapeutica"),
            r.get("confianca", "media"),
            r["barra_norm"],
        ))
    with conn.cursor() as cur:
        cur.executemany(UPDATE_SQL, rows)
    conn.commit()


# ── CLAUDE ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
Você é especialista em classificação de produtos de farmácia do mercado brasileiro, seguindo o padrão das grandes redes (Drogasil, Droga Raia, Ultrafarma).

━━━ CAMPO tipo — escolha EXATAMENTE UMA das 10 opções abaixo ━━━

"generico"
  Medicamento com nome genérico ANVISA, bioequivalência comprovada, símbolo [G].
  Exemplos: PARACETAMOL 500MG, AMOXICILINA 500MG, OMEPRAZOL 20MG, DIPIRONA SODICA 500MG/ML
  Pista: nome começa pelo princípio ativo, sem nome fantasia comercial.

"similar"
  Medicamento com nome comercial fantasia, mesmo princípio ativo que o referência, mas NÃO é o pioneiro.
  Exemplos: MAXALGINA (dipirona), IBUPRIL (ibuprofeno), POLARADEX (dexametasona), FISIOFORT (diclofenaco tópico), REPOFLOR
  Pista: nome fantasia + princípio ativo identificável. Inclui "CONTROLADOS" com nome fantasia e pomadas/cremes analgésicos com marca.

"referencia"
  Medicamento de referência — marca original/pioneira, padrão de comparação ANVISA.
  Exemplos: TYLENOL, NOVALGINA, BUSCOPAN, RIVOTRIL, NEOSALDINA, ALLEGRA, GLIFAGE, VOLTAREN, DICLOFENACO EMS referência
  Pista: marca conhecida de multinacional ou laboratório pioneiro histórico.

"suplemento"
  Suplemento alimentar ANVISA — sem prescrição, sem tarja, finalidade nutricional ou de saúde natural.
  Exemplos: vitaminas (A, B, C, D, complexo B), minerais (zinco, ferro, cálcio), ômega-3, colágeno, probióticos,
           whey protein, creatina, BCAA, melatonina, fitoterápicos, chás medicinais, própolis, polivitamínicos.
  Produtos Vitnatu são SEMPRE suplemento.

"perfumaria"
  Perfumaria, higiene pessoal e beleza — tudo que não é medicamento nem suplemento voltado a cuidado pessoal.
  Exemplos: perfume, colônia, desodorante, esmalte para unhas, acetona, removedor de esmalte,
           tintura capilar, batom, blush, maquiagem, bronzeador, protetor solar (FPS/SPF), hidratante facial,
           sérum, creme anti-age, shampoo, condicionador, sabonete, pasta dental, escova dental, fio dental,
           enxaguante bucal, absorvente, fralda, lenço umedecido, algodão, hastes flexíveis (cotonete),
           papel higiênico, preservativo, talco, creme para assadura, álcool gel 70%, antisséptico bucal.
  Use para qualquer produto de cuidado pessoal, higiene ou beleza — não existe distinção entre higiene e cosméticos aqui.

"correlato"
  Correlatos, dispositivos médicos e equipamentos de saúde — regulados pela ANVISA como produtos médicos.
  Exemplos: agulha, seringa, luva, gaze, atadura, esparadrapo, curativo adesivo, band-aid, lanceta,
           tira reagente de glicemia, glicosímetro, termômetro, esfigmomanômetro, nebulizador, inalador,
           cateter, sonda, bolsa de colostomia, muleta, cadeira de rodas,
           soro fisiológico (ampola), água oxigenada, PVPI/Povidine, clorexidina, álcool isopropanol.

"nutricao"
  Nutrição especial — alimentos de uso médico, dietético ou para fases especiais da vida.
  Exemplos: dieta enteral (Fresubin, Ensure, Nutren), fórmula infantil (NAN, Aptamil, Enfamil),
           leite sem lactose, alimento para diabético, papinha/purê para bebê, bolacha de arroz bebê,
           adoçante (Sucralose, Stévia, Frutose), isotônico, energético líquido, barra de cereal.
  Distinção: cápsula/pó nutricional = suplemento; alimento pronto para consumo = nutricao.

"varejo"
  Varejo/conveniência — itens não farmacêuticos vendidos na farmácia por conveniência.
  Exemplos: balas, chicletes, biscoitos, água mineral, suco em embalagem individual, pilhas/baterias,
           acessórios de escritório, produtos de limpeza doméstica, itens de papelaria.
  Use apenas quando o produto claramente não se enquadra em nenhuma das outras categorias.

━━━ ATENÇÃO — distinções críticas ━━━
  - classe_atual do banco PODE estar errado — use como pista, não como verdade
  - "CORRELATOS" na classe → correlato
  - "PERFUMARIA"/"PERFUMARIA/COSMETICO"/"HIGIENE" na classe → perfumaria
  - "ALIMENTOS" → suplemento (cápsula/pó) | nutricao (alimento pronto) | varejo (bala/chiclete)
  - "CONTROLADO" → não muda o tipo — avalie pelo nome (generico/similar/referencia)
  - Pomada/creme com princípio ativo identificável (diclofenaco, ibuprofeno, etc.) → similar ou referencia, NÃO perfumaria

━━━ OUTROS CAMPOS ━━━
principio_ativo    Para generico/similar/referencia: princípio ativo em português. null para os demais.
classe_terapeutica Para generico/similar/referencia: "Analgésico", "Antibiótico", "Anti-hipertensivo", etc. null para os demais.
confianca          "alta" (certeza), "media" (provável), "baixa" (dúvida real)

Retorne SOMENTE um array JSON válido, sem markdown, sem texto extra.
[{"barra_norm":"7891234567890","tipo":"generico","principio_ativo":"Paracetamol","classe_terapeutica":"Analgésico/Antipirético","confianca":"alta"}]"""


def classify_batch(client, batch):
    items = [
        {
            "barra_norm":   str(p["barra_norm"]),
            "nome":         (p["descricao"] or "").strip(),
            "classe_atual": (p["classe"] or "").strip(),
            "laboratorio":  (p["laboratorio"] or "").strip(),
            "marca":        (p["marca"] or "").strip(),
        }
        for p in batch
    ]

    msg = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Classifique estes {len(items)} produtos:\n"
                + json.dumps(items, ensure_ascii=False, indent=2)
            ),
        }],
    )

    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    classified = json.loads(raw)

    bn_to_info = {str(p["barra_norm"]): p for p in batch}
    for r in classified:
        orig = bn_to_info.get(str(r.get("barra_norm", "")), {})
        r["_nome"] = orig.get("descricao") or ""

    return classified


# ── MAIN ─────────────────────────────────────────────────────────────────────

TIPO_COR = {
    "generico":   "\033[36m",   # ciano
    "similar":    "\033[33m",   # amarelo
    "referencia": "\033[35m",   # magenta
    "suplemento": "\033[32m",   # verde
    "perfumaria": "\033[95m",   # magenta claro
    "correlato":  "\033[90m",   # cinza
    "nutricao":   "\033[93m",   # amarelo claro
    "varejo":     "\033[37m",   # branco
}
RESET = "\033[0m"


def _cor(tipo):
    return TIPO_COR.get(tipo, "") + f"{tipo:<11}" + RESET


def print_stats(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT tipo_ia, COUNT(*) AS cnt,
                   ROUND(COUNT(*)*100.0 / SUM(COUNT(*)) OVER(), 1) AS pct
            FROM medicamentos
            WHERE tipo_ia IS NOT NULL
            GROUP BY tipo_ia ORDER BY cnt DESC
        """)
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS total FROM medicamentos WHERE barra_norm IS NOT NULL AND barra_norm != ''")
        total_ean = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) AS classif FROM medicamentos WHERE tipo_ia IS NOT NULL")
        classif = cur.fetchone()["classif"]

    print(f"\n{'='*50}")
    print(f"Classificados: {classif} / {total_ean} com EAN válido ({classif*100//total_ean if total_ean else 0}%)")
    print(f"{'─'*50}")
    for r in rows:
        cor = TIPO_COR.get(r["tipo_ia"], "")
        bar = "█" * int(r["pct"] / 2)
        print(f"  {cor}{r['tipo_ia']:<12}{RESET}  {r['cnt']:>5}  {r['pct']:>5}%  {bar}")
    print(f"{'='*50}\n")


def main():
    parser = argparse.ArgumentParser(description="Classifica medicamentos com Claude")
    parser.add_argument("--force",      action="store_true", help="Reclassifica mesmo os já classificados")
    parser.add_argument("--limit",      type=int, default=None)
    parser.add_argument("--dry-run",    action="store_true", help="Não grava no banco")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--ean",        type=str, default=None, help="Classifica um EAN específico")
    parser.add_argument("--stats",      action="store_true", help="Mostra distribuição atual e sai")
    args = parser.parse_args()

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    conn   = psycopg2.connect(DATABASE_URL)

    ensure_columns(conn)

    if args.stats:
        print_stats(conn)
        conn.close()
        return

    print("Buscando medicamentos...")
    products = fetch_products(
        conn,
        force=args.force or bool(args.ean),
        limit=args.limit,
        ean_filter=args.ean,
    )
    total = len(products)
    print(f"  {total} medicamento(s) para classificar\n")

    if not total:
        print("Nada a fazer. Use --force para reclassificar os já processados.")
        conn.close()
        return

    classified_total = 0
    error_batches    = 0
    batch_size       = args.batch_size

    for i in range(0, total, batch_size):
        batch = products[i : i + batch_size]
        start = i + 1
        end   = min(i + batch_size, total)
        print(f"[{start:>5} – {end:<5} / {total}]  enviando para Claude...")

        attempt = 0
        results = None
        while attempt < 3:
            attempt += 1
            try:
                results = classify_batch(client, batch)
                break
            except Exception as exc:
                wait = attempt * 5
                print(f"  ⚠  Tentativa {attempt} falhou: {exc}")
                if attempt < 3:
                    print(f"     aguardando {wait}s...")
                    time.sleep(wait)

        if results is None:
            error_batches += 1
            print(f"  ✗  Batch ignorado após 3 tentativas.\n")
            continue

        if not args.dry_run:
            save_classifications(conn, results)

        classified_total += len(results)
        for r in results:
            tipo  = (r.get("tipo") or "outro").lower()
            pa    = r.get("principio_ativo") or ""
            conf  = r.get("confianca", "?")[0].upper()
            nome  = (r.get("_nome") or "")[:42]
            print(
                f"  {str(r.get('barra_norm','?')):>14}  {_cor(tipo)}  [{conf}]  {nome}"
                + (f"  ({pa})" if pa else "")
            )

        time.sleep(0.4)
        print()

    if not args.dry_run and classified_total > 0:
        print_stats(conn)

    conn.close()

    print("=" * 60)
    print(f"Concluído: {classified_total} classificados, {error_batches} batch(es) com falha")
    if args.dry_run:
        print("(dry-run: nenhum dado gravado)")
    else:
        print("Colunas atualizadas em: medicamentos.tipo_ia / principio_ativo_ia / classe_terapeutica_ia")


if __name__ == "__main__":
    main()
