#!/usr/bin/env python3
"""
Alimenta sintomas/termos de busca para produtos disponiveis no ecommerce.

Uso:
  python scripts/alimentar_sintomas_catalogo.py --limit 500
  python scripts/alimentar_sintomas_catalogo.py --force --anthropic
  python scripts/alimentar_sintomas_catalogo.py --ean 7891234567890 --anthropic

Por padrao usa regras locais, sem custo. Com --anthropic, enriquece em lote
usando ANTHROPIC_API_KEY do .env.
"""

import argparse
import html
import json
import os
import re
import ssl
import time
import urllib.request
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATABASE_URL = os.environ["DATABASE_URL"]
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 30

DDL = """
CREATE TABLE IF NOT EXISTS ecommerce_produto_sintomas (
    ean TEXT PRIMARY KEY,
    nome_ref TEXT,
    sintomas TEXT,
    termos_busca TEXT,
    principio_ativo TEXT,
    classe_terapeutica TEXT,
    fonte TEXT DEFAULT 'regras',
    confianca TEXT DEFAULT 'media',
    atualizado_em TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_produto_sintomas_termos
ON ecommerce_produto_sintomas
USING gin (to_tsvector('portuguese', COALESCE(sintomas,'') || ' ' || COALESCE(termos_busca,'')));
"""

FETCH_SQL = """
WITH inv AS (
    SELECT e.cnpj AS cnpjloja,
           LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0') AS ean,
           e.descricao AS nome
    FROM estoque e
    LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = e.cnpj
    WHERE e.estoque > 0
      AND COALESCE(c.catalogo_publico, TRUE) = TRUE
      AND COALESCE(e.barras_norm, e.barras, '') <> ''

    UNION ALL

    SELECT ae.cnpj_loja AS cnpjloja,
           LTRIM(COALESCE(ae.ean, ''), '0') AS ean,
           ae.descricao_produto AS nome
    FROM automatiza_estoque ae
    LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = ae.cnpj_loja
    WHERE ae.quantidade_estoque > 0
      AND COALESCE(c.catalogo_publico, TRUE) = TRUE
      AND COALESCE(ae.ean, '') <> ''
)
SELECT DISTINCT ON (inv.ean)
       inv.ean,
       COALESCE(m.descricao, pc.descricao_canon, inv.nome) AS nome,
       COALESCE(m.laboratorio, pc.laboratorio, '') AS laboratorio,
       COALESCE(m.marca, '') AS marca,
       COALESCE(m.classe, '') AS classe,
       COALESCE(m.tipo_ia, pc.categoria, '') AS categoria,
       COALESCE(m.principio_ativo_ia, '') AS principio_ativo_ia,
       COALESCE(m.classe_terapeutica_ia, '') AS classe_terapeutica_ia
FROM inv
LEFT JOIN medicamentos m ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = inv.ean
LEFT JOIN produto_canon pc ON LTRIM(COALESCE(pc.ean, ''), '0') = inv.ean
WHERE inv.ean <> ''
{extra_where}
ORDER BY inv.ean, (m.id IS NOT NULL) DESC
{limit_clause}
"""

UPSERT_SQL = """
INSERT INTO ecommerce_produto_sintomas
  (ean, nome_ref, sintomas, termos_busca, principio_ativo, classe_terapeutica, fonte, confianca, atualizado_em)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
ON CONFLICT (ean) DO UPDATE SET
  nome_ref=EXCLUDED.nome_ref,
  sintomas=EXCLUDED.sintomas,
  termos_busca=EXCLUDED.termos_busca,
  principio_ativo=EXCLUDED.principio_ativo,
  classe_terapeutica=EXCLUDED.classe_terapeutica,
  fonte=EXCLUDED.fonte,
  confianca=EXCLUDED.confianca,
  atualizado_em=NOW()
"""

UPSERT_VALUES_SQL = """
INSERT INTO ecommerce_produto_sintomas
  (ean, nome_ref, sintomas, termos_busca, principio_ativo, classe_terapeutica, fonte, confianca, atualizado_em)
VALUES %s
ON CONFLICT (ean) DO UPDATE SET
  nome_ref=EXCLUDED.nome_ref,
  sintomas=EXCLUDED.sintomas,
  termos_busca=EXCLUDED.termos_busca,
  principio_ativo=EXCLUDED.principio_ativo,
  classe_terapeutica=EXCLUDED.classe_terapeutica,
  fonte=EXCLUDED.fonte,
  confianca=EXCLUDED.confianca,
  atualizado_em=NOW()
"""

RULES = [
    (r"\b(dipirona|paracetamol|ibuprofeno|maxalgina|novalgina|tylenol|termometro)\b",
     ["febre", "dor"], ["antitermico", "analgesico", "temperatura alta", "mal estar"]),
    (r"\b(antigripal|benegrip|cimegripe|multigrip|resfenol|coristina|vitamina c|soro nasal|maresis|rinossoro)\b",
     ["gripe", "resfriado", "nariz entupido"], ["antigripal", "congestao nasal", "coriza", "espirro"]),
    (r"\b(xarope|tosse|acetilcisteina|ambroxol|guaco|expectorante|antitussigeno)\b",
     ["tosse", "catarro"], ["expectorante", "xarope", "bronquite", "secrecao"]),
    (r"\b(loratadina|cetirizina|fexofenadina|desloratadina|allegra|claritin|antialergico)\b",
     ["alergia", "rinite", "coceira"], ["antialergico", "espirro", "coriza", "urticaria"]),
    (r"\b(omeprazol|pantoprazol|esomeprazol|antiacido|hidroxido|estomazil|epocler|engov)\b",
     ["azia", "queimacao", "ma digestao"], ["estomago", "refluxo", "gastrite", "ressaca"]),
    (r"\b(dramin|dimenidrinato|meclizina|vonau|ondansetrona)\b",
     ["enjoo", "nausea", "vomito"], ["antiemetico", "labirintite", "viagem"]),
    (r"\b(loperamida|floratil|probio|probiotico|soro reidratacao|pedialyte)\b",
     ["diarreia"], ["intestino", "reidratacao", "flora intestinal"]),
    (r"\b(lactulose|laxante|supositorio|fleet|fibra)\b",
     ["prisao de ventre", "constipacao"], ["intestino preso", "laxativo"]),
    (r"\b(buscopan|escopolamina|simeticona|luftal|dimeticona)\b",
     ["colica", "gases", "dor abdominal"], ["antiespasmodico", "estufamento"]),
    (r"\b(pastilha|propolis|mel|garganta|spray bucal)\b",
     ["dor de garganta", "rouquidao"], ["pastilha", "irritacao na garganta"]),
    (r"\b(gaze|curativo|band.?aid|esparadrapo|clorexidina|agua oxigenada|antisseptico|povidine)\b",
     ["machucado", "ferimento", "corte"], ["curativo", "higienizar ferida", "primeiros socorros"]),
    (r"\b(assadura|hipoglos|bepantol|dexpantenol|nistatina|pomada)\b",
     ["assadura", "irritacao na pele"], ["pomada", "pele irritada", "bebe"]),
    (r"\b(acne|antiacne|peroxido|sabonete facial|gel limpeza)\b",
     ["acne", "oleosidade"], ["espinha", "rosto", "pele acneica"]),
    (r"\b(protetor solar|fps|spf|pos sol|aloe|hidratante|pele seca)\b",
     ["pele seca", "queimadura solar", "protecao solar"], ["dermocosmetico", "ressecamento", "sol"]),
    (r"\b(melatonina|sono|insonia)\b",
     ["insonia", "dificuldade para dormir"], ["sono", "dormir", "relaxamento"]),
    (r"\b(vitamina|zinco|omega|colageno|creatina|whey|suplemento|probiotico)\b",
     ["imunidade", "cansaco", "suplementacao"], ["vitaminas", "energia", "nutricao"]),
]


def norm(value):
    value = html.unescape(value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def uniq(items):
    out, seen = [], set()
    for item in items:
        item = norm(item)
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


def fetch_products(conn, force=False, limit=None, ean=None, missing_sintomas=False):
    where = []
    if not force and not ean and not missing_sintomas:
        where.append("AND inv.ean NOT IN (SELECT LTRIM(COALESCE(ean,''), '0') FROM ecommerce_produto_sintomas)")
    if missing_sintomas:
        where.append(
            "AND inv.ean IN (SELECT LTRIM(COALESCE(ean,''), '0') FROM ecommerce_produto_sintomas WHERE COALESCE(TRIM(sintomas),'') = '')"
        )
    if ean:
        where.append("AND inv.ean = %s")
    sql = FETCH_SQL.format(
        extra_where=" ".join(where),
        limit_clause=f"LIMIT {int(limit)}" if limit else "",
    )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (ean.lstrip("0"),) if ean else None)
        return list(cur.fetchall())


def classify_rules(product):
    blob = norm(" ".join([
        product.get("nome") or "",
        product.get("marca") or "",
        product.get("classe") or "",
        product.get("categoria") or "",
        product.get("principio_ativo_ia") or "",
        product.get("classe_terapeutica_ia") or "",
    ]))
    sintomas, termos = [], []
    for pattern, sints, search_terms in RULES:
        if re.search(pattern, blob, re.I):
            sintomas.extend(sints)
            termos.extend(search_terms)
    principio = product.get("principio_ativo_ia") or ""
    classe = product.get("classe_terapeutica_ia") or ""
    if principio:
        termos.append(principio)
    if classe:
        termos.append(classe)
    if not sintomas and (product.get("categoria") or "") in ("generico", "similar", "referencia", "medicamento"):
        sintomas.extend(["medicamento"])
        termos.extend(["farmacia", "remedio"])
    return {
        "ean": product["ean"],
        "nome_ref": product.get("nome") or "",
        "sintomas": uniq(sintomas),
        "termos_busca": uniq(termos + [product.get("nome") or ""]),
        "principio_ativo": principio,
        "classe_terapeutica": classe,
        "fonte": "regras",
        "confianca": "media" if sintomas else "baixa",
    }


def anthropic_batch(products):
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY nao configurada")
    prompt_items = [
        {
            "ean": p["ean"],
            "nome": p.get("nome") or "",
            "classe": p.get("classe") or "",
            "categoria": p.get("categoria") or "",
            "principio_ativo": p.get("principio_ativo_ia") or "",
            "classe_terapeutica": p.get("classe_terapeutica_ia") or "",
        }
        for p in products
    ]
    body = {
        "model": MODEL,
        "max_tokens": 5000,
        "system": (
            "Voce e especialista em farmacia brasileira. Para cada produto, gere sintomas e termos de busca "
            "que um consumidor leigo usaria. Nao invente indicacoes perigosas. Para medicamentos controlados, "
            "use termos terapeuticos gerais e mantenha cautela. Retorne somente JSON valido."
        ),
        "messages": [{
            "role": "user",
            "content": (
                "Para cada item retorne: ean, sintomas(array), termos_busca(array), "
                "principio_ativo, classe_terapeutica, confianca(alta/media/baixa). "
                "Use portugues BR sem acentos nos termos.\n"
                + json.dumps(prompt_items, ensure_ascii=False)
            ),
        }],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8", "ignore"))
    raw = (data.get("content") or [{}])[0].get("text", "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    rows = json.loads(raw)
    if isinstance(rows, dict):
        rows = rows.get("produtos") or rows.get("items") or rows.get("resultados") or rows.get("results") or []
    if not isinstance(rows, list):
        rows = []
    rows = [r for r in rows if isinstance(r, dict)]
    by_ean = {str(r.get("ean", "")).lstrip("0"): r for r in rows if r.get("ean")}
    result = []
    for p in products:
        ean = str(p["ean"]).lstrip("0")
        r = by_ean.get(ean) or {}
        if not r:
            result.append(classify_rules(p))
            continue
        local = classify_rules(p)
        result.append({
            "ean": ean,
            "nome_ref": p.get("nome") or "",
            "sintomas": uniq((r.get("sintomas") or []) + local["sintomas"]),
            "termos_busca": uniq((r.get("termos_busca") or []) + local["termos_busca"]),
            "principio_ativo": r.get("principio_ativo") or local["principio_ativo"],
            "classe_terapeutica": r.get("classe_terapeutica") or local["classe_terapeutica"],
            "fonte": "anthropic",
            "confianca": r.get("confianca") or "media",
        })
    return result


def save_rows(conn, rows, dry_run=False):
    payload = [
        (
            r["ean"],
            r.get("nome_ref") or "",
            ", ".join(uniq(r.get("sintomas") or [])),
            ", ".join(uniq(r.get("termos_busca") or [])),
            norm(r.get("principio_ativo") or ""),
            norm(r.get("classe_terapeutica") or ""),
            r.get("fonte") or "regras",
            r.get("confianca") or "media",
        )
        for r in rows
    ]
    if dry_run:
        for row in payload[:10]:
            print(row)
        print(f"dry-run: {len(payload)} linhas prontas")
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            UPSERT_VALUES_SQL,
            payload,
            template="(%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
            page_size=1000,
        )
    conn.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--ean")
    parser.add_argument("--anthropic", action="store_true")
    parser.add_argument("--missing-sintomas", action="store_true", help="Processa apenas produtos ja indexados que ficaram sem sintomas")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--chunk-size", type=int, default=2000, help="Quantidade buscada por vez no modo regras")
    args = parser.parse_args()
    if not args.anthropic and args.batch_size == BATCH_SIZE:
        args.batch_size = 1000

    conn = psycopg2.connect(DATABASE_URL)
    ensure_schema(conn)

    total = 0
    remaining_limit = args.limit
    while True:
        fetch_limit = remaining_limit if remaining_limit and remaining_limit < args.chunk_size else args.chunk_size
        products = fetch_products(
            conn,
            force=args.force,
            limit=fetch_limit,
            ean=args.ean,
            missing_sintomas=args.missing_sintomas,
        )
        if not products:
            if total == 0:
                print("0 produto(s) para alimentar")
            break
        print(f"{len(products)} produto(s) carregados neste chunk")
        for start in range(0, len(products), args.batch_size):
            batch = products[start:start + args.batch_size]
            if args.anthropic:
                print(f"[{total + 1}-{total + len(batch)}] Anthropic...")
                try:
                    rows = anthropic_batch(batch)
                except Exception as exc:
                    print(f"  falha Anthropic no lote: {exc}; usando regras locais")
                    rows = [classify_rules(p) for p in batch]
                time.sleep(0.4)
            else:
                rows = [classify_rules(p) for p in batch]
            save_rows(conn, rows, dry_run=args.dry_run)
            total += len(rows)
            print(f"salvos: {total}")
            if args.dry_run:
                conn.close()
                return
            if remaining_limit:
                remaining_limit -= len(rows)
                if remaining_limit <= 0:
                    conn.close()
                    return

        if args.ean or args.force or args.missing_sintomas or args.anthropic:
            break
    conn.close()


if __name__ == "__main__":
    main()
