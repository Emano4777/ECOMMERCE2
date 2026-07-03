#!/usr/bin/env python3
"""
Classifica todos os produtos do ecommerce Poupaqui usando Claude (Anthropic).
Usa o EAN como chave única — sem ambiguidade por nome duplicado.

Classificações possíveis:
  generico    → Medicamento genérico (bioequivalência, nome genérico, [G] ANVISA)
  similar     → Medicamento similar (nome comercial, mesmo princípio ativo, não é o pioneiro)
  referencia  → Medicamento de referência (marca pioneira/original: Tylenol, Novalgina…)
  suplemento  → Suplemento alimentar/nutricional (vitaminas, proteínas, fitoterápicos)
  cosmetico   → Dermocosmético, higiene pessoal, protetor solar, shampoo, sabonete
  outro       → Dispositivo médico, fraldas, equipamento, não identificado

Uso:
  python scripts/classificar_produtos.py                # apenas não classificados
  python scripts/classificar_produtos.py --force        # reclassifica todos
  python scripts/classificar_produtos.py --limit 200    # limita qtd
  python scripts/classificar_produtos.py --dry-run      # não grava no banco
  python scripts/classificar_produtos.py --ean 7891234  # classifica EAN específico
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

# carrega .env do diretório pai do script (raiz do projeto)
load_dotenv(Path(__file__).parent.parent / ".env")

DATABASE_URL  = os.environ["DATABASE_URL"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL         = "claude-sonnet-5"   # mais preciso para classificação (evita erros do haiku)
BATCH_SIZE    = 30

# ── SCHEMA ──────────────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS ecommerce_classificacao_ean (
    ean                TEXT PRIMARY KEY,
    nome_ref           TEXT,
    tipo               TEXT NOT NULL,
    categoria          TEXT NOT NULL,
    principio_ativo    TEXT,
    classe_terapeutica TEXT,
    confianca          TEXT DEFAULT 'media',
    modelo_ia          TEXT,
    classificado_em    TIMESTAMPTZ DEFAULT NOW()
);
"""


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


# ── BUSCA DE PRODUTOS ────────────────────────────────────────────────────────

FETCH_SQL = """
WITH inv AS (
    -- Catálogo DNS (omie): sempre exibido independente de estoque externo
    SELECT LTRIM(COALESCE(ean_norm, ''), '0') AS ean_key, '' AS descricao
    FROM omie_estoque_dns
    WHERE ean_norm IS NOT NULL AND ean_norm != ''

    UNION

    -- Alpha: só EANs com estoque > 0 que estão no catálogo DNS ou na tabela medicamentos
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

    -- Automatiza: só EANs com estoque > 0 que estão no catálogo DNS ou na tabela medicamentos
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

    UNION

    -- Fluxo novo (Alpha): EANs do catálogo público puxados do Alpha. Aqui NÃO
    -- exigimos vínculo com omie/medicamentos — são exatamente os produtos que
    -- o ecommerce exibe hoje e precisam de categoria (ex: NARIX, REPELENTE).
    SELECT LTRIM(COALESCE(ap.ean, ''), '0') AS ean_key, ap.nome AS descricao
    FROM ecommerce_alpha_produtos ap
    WHERE COALESCE(ap.inativo, false) = false
      AND COALESCE(ap.estoque, 0) > 0
      AND COALESCE(ap.ean, '') != ''
)
SELECT DISTINCT ON (inv.ean_key)
    inv.ean_key                                                      AS ean,
    COALESCE(m.descricao, pc.descricao_canon, inv.descricao)        AS nome,
    COALESCE(m.laboratorio, pc.laboratorio, '')                     AS laboratorio,
    COALESCE(m.classe, '')                                           AS classe_med
FROM inv
LEFT JOIN medicamentos m
       ON m.barra_norm = inv.ean_key
LEFT JOIN produto_canon pc
       ON pc.ean = inv.ean_key
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
            "AND inv.ean_key NOT IN (SELECT ean FROM ecommerce_classificacao_ean)"
        )
    if ean_filter:
        safe = ean_filter.lstrip("0")
        parts.append(f"AND inv.ean_key = '{safe}'")

    sql = FETCH_SQL.format(
        extra_where=" ".join(parts),
        limit_clause=f"LIMIT {limit}" if limit else "",
    )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return cur.fetchall()


# ── SALVAR ───────────────────────────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO ecommerce_classificacao_ean
    (ean, nome_ref, tipo, categoria, principio_ativo, classe_terapeutica, confianca, modelo_ia)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (ean) DO UPDATE SET
    nome_ref           = EXCLUDED.nome_ref,
    tipo               = EXCLUDED.tipo,
    categoria          = EXCLUDED.categoria,
    principio_ativo    = EXCLUDED.principio_ativo,
    classe_terapeutica = EXCLUDED.classe_terapeutica,
    confianca          = EXCLUDED.confianca,
    modelo_ia          = EXCLUDED.modelo_ia,
    classificado_em    = NOW()
"""

TIPOS_VALIDOS = {
    "generico", "similar", "referencia",
    "suplemento", "perfumaria", "dermocosmetico", "nutricao", "varejo",
}
CAT_DE_TIPO = {
    "generico":       "medicamento",
    "similar":        "medicamento",
    "referencia":     "medicamento",
    "suplemento":     "suplemento",
    "perfumaria":     "perfumaria",
    "dermocosmetico": "dermocosmetico",
    "nutricao":       "nutricao",
    "varejo":         "varejo",
}


def save_classifications(conn, results):
    rows = []
    for r in results:
        tipo = (r.get("tipo") or "varejo").lower().strip()
        if tipo not in TIPOS_VALIDOS:
            tipo = "varejo"
        categoria = r.get("categoria") or CAT_DE_TIPO[tipo]
        rows.append((
            r["ean"],
            r.get("nome_ref") or "",
            tipo,
            categoria,
            r.get("principio_ativo"),
            r.get("classe_terapeutica"),
            r.get("confianca", "media"),
            r.get("modelo_ia", MODEL),
        ))
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, rows)
    conn.commit()


# ── CLAUDE ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
Você é especialista em classificação de produtos de farmácia do mercado brasileiro, seguindo o padrão das grandes redes (Drogasil, Droga Raia, Ultrafarma).

━━━ CAMPO tipo — escolha EXATAMENTE UMA das 8 opções abaixo ━━━

"generico"
  Medicamento com nome genérico ANVISA, bioequivalência comprovada, símbolo [G].
  Exemplos: PARACETAMOL 500MG, AMOXICILINA 500MG, OMEPRAZOL 20MG, DIPIRONA SODICA 500MG/ML
  Pista: nome começa pelo princípio ativo, sem nome fantasia comercial.

"similar"
  Medicamento com nome comercial fantasia, mesmo princípio ativo que o referência, mas NÃO é o pioneiro.
  Exemplos: MAXALGINA (dipirona), IBUPRIL (ibuprofeno), POLARADEX (dexametasona), FISIOFORT (diclofenaco tópico)
  Pista: nome fantasia + princípio ativo identificável. Inclui pomadas/cremes analgésicos com nome comercial.

"referencia"
  Medicamento de referência — marca original/pioneira, padrão de comparação ANVISA.
  Exemplos: TYLENOL, NOVALGINA, BUSCOPAN, RIVOTRIL, NEOSALDINA, ALLEGRA, GLIFAGE, VOLTAREN, BEPANTOL medicamento
  Pista: marca conhecida de multinacional ou laboratório pioneiro histórico.

"suplemento"
  Suplemento alimentar ANVISA — sem prescrição, sem tarja, finalidade nutricional ou de saúde natural.
  Exemplos: vitaminas (A, B, C, D, complexo B), minerais (zinco, ferro, cálcio), ômega-3, colágeno, probióticos,
           whey protein, creatina, BCAA, melatonina, fitoterápicos, chás medicinais, própolis, polivitamínicos.
  Produtos Vitnatu são SEMPRE suplemento.

"perfumaria"
  Perfumaria e higiene pessoal — produtos de cuidado pessoal, higiene, beleza e maquiagem SEM ativos dermatológicos funcionais.
  Inclui: shampoo, condicionador, creme/máscara capilar, óleo capilar, anticaspa, tintura capilar
          sabonete (corporal, íntimo, bebê), desodorante, antitranspirante, depilatório, cera depilatória
          espuma/gel/creme de barbear, aparelho de barbear, loção pós-barba
          pasta dental, escova dental, fio dental, enxaguante bucal, colutório, antisséptico bucal
          absorvente feminino, protetor diário, fralda (infantil e geriátrica), absorvente geriátrico, lenço umedecido
          perfume, colônia, eau de parfum/toilette
          maquiagem: batom, blush, primer, base, sombra, delineador, rímel, glitter
          esmalte para unhas, acetona, removedor de esmalte
          óleo corporal, talco, creme para assadura, creme para os pés, lixa de pés
          protetor labial sem ativo medicamentoso (Lipgel, Carmed, Labello/Nivea Labello)
          preservativo, lubrificante íntimo
          algodão, cotonete, hastes flexíveis, papel higiênico
          álcool gel 70% (higiene pessoal)
  NÃO inclui: protetor solar (FPS/SPF), sérum, hidratante/creme facial com ativo, creme anti-age → use dermocosmetico

"dermocosmetico"
  Dermocosmético — produto cosmético com ativo funcional dermatológico; sem prescrição, sem tarja.
  Inclui: protetor solar (qualquer FPS/SPF), bloqueador solar
          sérum facial ou corporal
          hidratante facial (com ou sem FPS), creme facial, loção facial
          creme/tratamento anti-idade, anti-age, antirrugas
          clareador facial, despigmentante
          esfoliante facial
          máscara facial (argila, colágeno etc.)
          tônico facial, água micelar
          tratamento para acne (tópico cosmético, NÃO medicamento com prescrição)
          BB Cream, CC Cream
          firmador, tratamento para manchas
  NÃO inclui: protetor labial simples (→ perfumaria); cremes capilares (→ perfumaria)
  NÃO inclui: medicamentos dermatológicos com prescrição (→ similar/referencia)

"nutricao"
  Nutrição especial — alimentos de uso médico, dietético ou para fases especiais da vida.
  Exemplos: dieta enteral (Fresubin, Ensure, Nutren), fórmula infantil (NAN, Aptamil, Enfamil),
           leite sem lactose, alimento para diabético, papinha/purê para bebê, bolacha de arroz bebê,
           adoçante (Sucralose, Stévia, Frutose), isotônico, energético líquido.
  Distinção: cápsula/pó nutricional = suplemento; alimento pronto para consumo = nutricao.

"varejo"
  Varejo, conveniência e insumos hospitalares — tudo que não se encaixa nas categorias acima.
  Inclui INSUMOS HOSPITALARES/CORRELATOS: seringa, agulha, luva, gaze, atadura, esparadrapo,
         curativo adesivo, band-aid, lanceta, tira reagente de glicemia, glicosímetro,
         termômetro, esfigmomanômetro, nebulizador, inalador, cateter, sonda,
         bolsa de colostomia, muleta, cadeira de rodas,
         soro fisiológico (ampola), água oxigenada, PVPI/Povidine, clorexidina, álcool isopropanol, compressa.
  Inclui CONVENIÊNCIA/ALIMENTOS: bala, chiclete, paçoca, barrinha de cereal, chocolate, biscoito,
         água mineral, suco, energético em lata.
  Inclui ELETRÔNICOS/ACESSÓRIOS: pilha, bateria, carregador, cabo USB, fone de ouvido, brinquedo.
  Inclui OUTROS: produto de limpeza doméstica, papelaria, pastilha não medicamentosa.

━━━ ATENÇÃO — distinções críticas ━━━
  - classe_med do banco PODE estar errado — use como pista, não como verdade
  - "CORRELATOS" na classe_med → varejo (insumos hospitalares vão para varejo; correlato não existe mais)
  - "PERFUMARIA"/"PERFUMARIA/COSMETICO"/"HIGIENE" → perfumaria (higiene) ou dermocosmetico (ativo derm.)
  - "ALIMENTOS" → suplemento (cápsula/pó) | nutricao (alimento especial) | varejo (bala/chiclete)
  - "CONTROLADO" → não muda o tipo — avalie pelo nome (generico/similar/referencia)
  - Pomada/creme com princípio ativo identificável (diclofenaco, ibuprofeno) → similar ou referencia, NÃO perfumaria
  - Protetor solar (FPS/SPF) → SEMPRE dermocosmetico
  - Protetor labial simples (Lipgel, Carmed, Labello) → perfumaria
  - Seringa, agulha, gaze, curativo → varejo

━━━ OUTROS CAMPOS ━━━
principio_ativo    Para generico/similar/referencia: princípio ativo em português. null para os demais.
classe_terapeutica Para generico/similar/referencia: "Analgésico", "Antibiótico", "Anti-hipertensivo", etc. null para os demais.
confianca          "alta" (certeza), "media" (provável), "baixa" (dúvida real)

Retorne SOMENTE um array JSON válido, sem markdown, sem texto extra.
[{"ean":"7891234567890","tipo":"generico","categoria":"medicamento","principio_ativo":"Paracetamol","classe_terapeutica":"Analgésico/Antipirético","confianca":"alta"}]"""


def classify_batch(client, batch):
    items = [
        {
            "ean": str(p["ean"]),
            "nome": (p["nome"] or "").strip(),
            "laboratorio": (p["laboratorio"] or "").strip(),
            "classe_med": (p["classe_med"] or "").strip(),
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
                f"Classifique estes {len(items)} produtos brasileiros:\n"
                + json.dumps(items, ensure_ascii=False, indent=2)
            ),
        }],
    )

    # Sonnet pode retornar blocos de "thinking" antes do texto — pega o bloco de texto.
    raw = "".join(
        getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text"
    ).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    classified = json.loads(raw)

    # garantir que o EAN volta com o nome_ref para o save
    ean_to_info = {str(p["ean"]): p for p in batch}
    for r in classified:
        ean = str(r.get("ean", ""))
        orig = ean_to_info.get(ean, {})
        r["nome_ref"] = orig.get("nome") or ""
        r["modelo_ia"] = MODEL

    return classified


# ── MAIN ─────────────────────────────────────────────────────────────────────

TIPO_COR = {
    "generico":       "\033[36m",   # ciano
    "similar":        "\033[33m",   # amarelo
    "referencia":     "\033[35m",   # magenta
    "suplemento":     "\033[32m",   # verde
    "perfumaria":     "\033[95m",   # magenta claro
    "dermocosmetico": "\033[96m",   # ciano claro
    "nutricao":       "\033[93m",   # amarelo claro
    "varejo":         "\033[37m",   # branco
}
RESET = "\033[0m"


def _cor(tipo):
    return TIPO_COR.get(tipo, "") + tipo + RESET


def main():
    parser = argparse.ArgumentParser(description="Classifica produtos do Poupaqui com Claude")
    parser.add_argument("--force",      action="store_true", help="Reclassifica mesmo os já classificados")
    parser.add_argument("--limit",      type=int, default=None,  help="Máximo de produtos a processar")
    parser.add_argument("--skip",       type=int, default=0,     help="Pula os primeiros N itens da lista (para retomar)")
    parser.add_argument("--dry-run",    action="store_true", help="Não grava no banco de dados")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help=f"Produtos por chamada API (padrão: {BATCH_SIZE})")
    parser.add_argument("--ean",        type=str, default=None, help="Classifica um EAN específico")
    args = parser.parse_args()

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    conn   = psycopg2.connect(DATABASE_URL)

    ensure_schema(conn)
    print("Tabela ecommerce_classificacao_ean pronta.")

    print("Buscando produtos no banco...")
    products = fetch_products(
        conn,
        force=args.force or bool(args.ean),
        limit=args.limit,
        ean_filter=args.ean,
    )

    if args.skip:
        print(f"  Pulando primeiros {args.skip} itens (--skip).")
        products = products[args.skip:]

    total = len(products)
    print(f"  {total} produto(s) para classificar\n")

    if not total:
        print("Nada a fazer. Use --force para reclassificar produtos já processados.")
        conn.close()
        return

    classified_total = 0
    error_batches    = 0
    batch_size       = args.batch_size

    for i in range(0, total, batch_size):
        batch    = products[i : i + batch_size]
        start    = i + 1
        end      = min(i + batch_size, total)
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
            tipo = (r.get("tipo") or "outro").lower()
            pa   = r.get("principio_ativo") or ""
            conf = r.get("confianca", "?")[0].upper()
            nome = (r.get("nome_ref") or "")[:42]
            print(
                f"  {r.get('ean','?'):>14}  {_cor(tipo):<24}  [{conf}]  {nome}"
                + (f"  ({pa})" if pa else "")
            )

        time.sleep(0.4)   # respeita rate-limit
        print()

    conn.close()

    print("=" * 60)
    print(f"Concluído: {classified_total} classificados, {error_batches} batch(es) com falha")
    if args.dry_run:
        print("(modo dry-run: nenhum dado foi gravado no banco)")
    else:
        print("Dados gravados em: ecommerce_classificacao_ean")


if __name__ == "__main__":
    main()
