#!/usr/bin/env python3
"""
Audita o catalogo visivel de uma loja por EAN usando fontes oficiais dos fabricantes.

O script usa o web search da API Anthropic somente para localizar evidencias dentro
de dominios oficiais permitidos. Por padrao nao altera o catalogo; use --apply depois
de revisar o relatorio.

Exemplos:
  python scripts/auditar_fontes_fabricantes.py --cidade "Sao Pedro" --limit 20
  python scripts/auditar_fontes_fabricantes.py --cnpj 00000000000000 --limit 100
  python scripts/auditar_fontes_fabricantes.py --cidade "Sao Pedro" --apply
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("ANTHROPIC_RESEARCH_MODEL", "claude-sonnet-4-5-20250929")


class AnthropicBillingError(RuntimeError):
    pass

GENERIC_TARJA_VERMELHA_IMG = (
    "https://res.cloudinary.com/dizfq460q/image/upload/"
    "v1778783063/CAIXA_GEN%C3%89RICO_-_POUPAQUI_itiyth.jpg"
)
GENERIC_TARJA_PRETA_IMG = (
    "https://res.cloudinary.com/dizfq460q/image/upload/"
    "v1778783450/ChatGPT_Image_14_de_mai._de_2026_15_30_35_wuovpb.png"
)

# O dominio e a evidencia encontrada precisam concordar com o laboratorio informado.
# Novos laboratorios podem ser adicionados sem mudar o restante do pipeline.
LABORATORIOS = {
    "CIMED": ("cimedremedios.com.br",),
    "EMS": ("ems.com.br",),
    "GERMED": ("germedpharma.com.br",),
    "LEGRAND": ("legrandpharma.com.br",),
    "MULTILAB": ("multilab.com.br",),
    "MEDLEY": ("medley.com.br",),
    "EUROFARMA": ("eurofarma.com.br",),
    "NEO QUIMICA": ("neoquimica.com.br",),
    "HYPERA": ("hypera.com.br", "neoquimica.com.br"),
    "ACHE": ("ache.com.br",),
    "BIOLAB": ("biolabfarma.com.br",),
    "GEOLAB": ("geolab.com.br",),
    "PRATI DONADUZZI": ("pratidonaduzzi.com.br",),
    "TEUTO": ("teuto.com.br",),
    "NATULAB": ("natulab.com.br",),
    "PHARLAB": ("pharlab.com.br",),
    "UNIAO QUIMICA": ("uniaoquimica.com.br",),
    "BAYER": ("bayer.com.br",),
    "SANOFI": ("sanofi.com.br",),
    "PFIZER": ("pfizer.com.br",),
    "NOVARTIS": ("novartis.com.br",),
    "SANDOZ": ("sandoz.com.br",),
    "ABBOTT": ("abbottbrasil.com.br",),
    "TAKEDA": ("takeda.com",),
    "BOEHRINGER": ("boehringer-ingelheim.com",),
    "ASTRAZENECA": ("astrazeneca.com.br",),
    "GSK": ("gsk.com",),
    "LABORATOIRES THEA": ("theapharma.com.br", "laboratoires-thea.com"),
    "RECKITT BENCKISER": ("reckitt.com", "strepsils.com.br"),
}

LAB_HINTS = (
    (r"\(\s*(?:CIM|CHR)\s*\)|\bCIMED\b", "CIMED"),
    (r"\bEMS\b", "EMS"),
    (r"\(\s*EUR\s*\)|\bEUROFARMA\b|\bEUR\s*$", "EUROFARMA"),
    (r"\(\s*MED\s*\)|\bMEDLEY\b|\bMED\s*$", "MEDLEY"),
    (r"\(\s*(?:NEO|NC)\s*\)|\bNEO QUIMICA\b", "NEO QUIMICA"),
    (r"\(\s*(?:ACH|AH)\s*\)|\bACHE\b", "ACHE"),
    (r"\(\s*GER\s*\)|\bGERMED\b", "GERMED"),
    (r"\(\s*PRA\s*\)|\bPRATI(?: DONADUZZI)?\b", "PRATI DONADUZZI"),
    (r"\(\s*TEU\s*\)|\bTEUTO\b", "TEUTO"),
    (r"\(\s*UQ\s*\)|\bUQFAR\b|\bUNIAO QUIMICA\b", "UNIAO QUIMICA"),
    (r"\(\s*BAY\s*\)|\bBAYER\b|\bBEPANTOL\b", "BAYER"),
    (r"\(\s*SNF\s*\)|\bSANOFI\b", "SANOFI"),
    (r"\(\s*PFI\s*\)|\bPFIZER\b", "PFIZER"),
    (r"\(\s*NOV\s*\)|\bNOVARTIS\b", "NOVARTIS"),
    (r"\(\s*SAN\s*\)|\bSANDOZ\b", "SANDOZ"),
    (r"\bGEOLAB\b", "GEOLAB"),
    (r"\bNATULAB\b", "NATULAB"),
    (r"\bPHARLAB\b", "PHARLAB"),
    (r"\bBIOLAB\b", "BIOLAB"),
    (r"\bMULTILAB\b", "MULTILAB"),
    (r"\bLEGRAND\b", "LEGRAND"),
)

NON_MED_TYPES = {
    "suplemento",
    "perfumaria",
    "dermocosmetico",
    "nutricao",
    "varejo",
    "cosmetico",
    "higiene",
    "outro",
}
MED_TYPES = {"generico", "similar", "referencia"}

DDL = """
CREATE TABLE IF NOT EXISTS ecommerce_fontes_fabricantes (
    laboratorio_chave TEXT PRIMARY KEY,
    laboratorio_nome TEXT NOT NULL,
    dominios JSONB NOT NULL DEFAULT '[]'::jsonb,
    fonte_descoberta_url TEXT,
    evidencia TEXT,
    confianca TEXT NOT NULL DEFAULT 'pendente',
    resposta_ia JSONB,
    descoberto_em TIMESTAMPTZ DEFAULT NOW(),
    confirmado_em TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ecommerce_regulatorio_ean (
    ean TEXT PRIMARY KEY,
    chave_anvisa TEXT,
    nome_estoque TEXT,
    laboratorio_informado TEXT,
    laboratorio_confirmado TEXT,
    tipo_produto TEXT,
    tarja TEXT,
    classificacao_tarja_fabricante TEXT,
    receita_retida BOOLEAN,
    venda_online_permitida BOOLEAN,
    exibir_imagem_publica BOOLEAN,
    registro_anvisa TEXT,
    principio_ativo TEXT,
    apresentacao TEXT,
    dizeres_receita TEXT,
    dizeres_imagem TEXT,
    produto_url TEXT,
    bula_url TEXT,
    imagem_url TEXT,
    fonte_dominio TEXT,
    confianca TEXT,
    evidencia TEXT,
    evidencias_campos JSONB,
    confiancas_campos JSONB,
    cache_antes JSONB,
    divergencias JSONB,
    citacoes JSONB,
    resposta_ia JSONB,
    status TEXT NOT NULL DEFAULT 'pendente',
    modelo_ia TEXT,
    consultado_em TIMESTAMPTZ DEFAULT NOW(),
    aplicado_em TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ecommerce_produto_imagens (
    cnpjloja TEXT NOT NULL,
    ean TEXT NOT NULL,
    imagem_url TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (cnpjloja, ean)
);

ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS url_bula_fabricante TEXT;
ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS fonte_fabricante_url TEXT;
ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS fonte_fabricante_dominio TEXT;
ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS fonte_fabricante_confianca TEXT;
ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS fonte_fabricante_consultada_em TIMESTAMPTZ;
ALTER TABLE ecommerce_regulatorio_ean ADD COLUMN IF NOT EXISTS venda_online_permitida BOOLEAN;
ALTER TABLE ecommerce_regulatorio_ean ADD COLUMN IF NOT EXISTS classificacao_tarja_fabricante TEXT;
ALTER TABLE ecommerce_regulatorio_ean ADD COLUMN IF NOT EXISTS registro_anvisa TEXT;
ALTER TABLE ecommerce_regulatorio_ean ADD COLUMN IF NOT EXISTS principio_ativo TEXT;
ALTER TABLE ecommerce_regulatorio_ean ADD COLUMN IF NOT EXISTS apresentacao TEXT;
ALTER TABLE ecommerce_regulatorio_ean ADD COLUMN IF NOT EXISTS dizeres_receita TEXT;
ALTER TABLE ecommerce_regulatorio_ean ADD COLUMN IF NOT EXISTS dizeres_imagem TEXT;
ALTER TABLE ecommerce_regulatorio_ean ADD COLUMN IF NOT EXISTS evidencias_campos JSONB;
ALTER TABLE ecommerce_regulatorio_ean ADD COLUMN IF NOT EXISTS confiancas_campos JSONB;
ALTER TABLE ecommerce_regulatorio_ean ADD COLUMN IF NOT EXISTS cache_antes JSONB;
ALTER TABLE ecommerce_regulatorio_ean ADD COLUMN IF NOT EXISTS divergencias JSONB;
"""

BLOCKED_DISCOVERY_DOMAINS = {
    "amazon.com.br", "mercadolivre.com.br", "shopee.com.br", "magazineluiza.com.br",
    "drogasil.com.br", "drogaraia.com.br", "drogariasaopaulo.com.br",
    "paguemenos.com.br", "panvel.com", "ultrafarma.com.br", "cliquefarma.com.br",
    "consultaremedios.com.br", "bulas.med.br", "drconsulta.com", "wikipedia.org",
    "sara.com.br", "gov.br", "anvisa.gov.br",
}


def _ascii(value):
    return unicodedata.normalize("NFD", value or "").encode("ascii", "ignore").decode("ascii")


def _norm(value):
    return re.sub(r"[^A-Z0-9]+", " ", _ascii(value).upper()).strip()


def _digits(value):
    return re.sub(r"\D", "", value or "").lstrip("0")


def _domain(url):
    try:
        if not isinstance(url, str):
            return ""
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    except (TypeError, ValueError):
        return ""


def _allowed_url(url, domains):
    host = _domain(url)
    return bool(host and any(host == d or host.endswith("." + d) for d in domains))


def _lab_domains(lab):
    normalized = _norm(lab)
    if not normalized:
        return None, ()
    for alias, domains in LABORATORIOS.items():
        if alias in normalized or normalized in alias:
            return alias, domains
    return None, ()


def _infer_lab(nome, laboratorio):
    if _lab_domains(laboratorio)[1]:
        return laboratorio, False
    normalized_name = re.sub(r"\s+", " ", _ascii(nome).upper()).strip()
    for pattern, lab in LAB_HINTS:
        if re.search(pattern, normalized_name):
            return lab, True
    return laboratorio, False


def _chave(nome):
    stop = {
        "COM", "DE", "DO", "DA", "DOS", "DAS", "PARA", "POR", "EM", "GENERICO",
        "MG", "MCG", "ML", "UI", "CP", "CAPS", "COMP", "COMPRIMIDO", "COMPRIMIDOS",
        "CAPSULA", "CAPSULAS", "GOTAS", "XAROPE", "CREME", "POMADA", "GEL", "SPRAY",
    }
    words = [
        word for word in _norm(nome).split()
        if word not in stop and not any(char.isdigit() for char in word) and len(word) >= 4
    ]
    return " ".join(words[:2])


def _connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ausente")
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(DDL)
        for laboratorio, domains in LABORATORIOS.items():
            cur.execute(
                """
                INSERT INTO ecommerce_fontes_fabricantes
                    (laboratorio_chave, laboratorio_nome, dominios, confianca,
                     evidencia, confirmado_em)
                VALUES (%s,%s,%s::jsonb,'alta','dominio inicial configurado',NOW())
                ON CONFLICT (laboratorio_chave) DO UPDATE SET
                    laboratorio_nome=EXCLUDED.laboratorio_nome,
                    dominios=EXCLUDED.dominios,
                    confianca='alta',
                    evidencia='dominio inicial configurado',
                    confirmado_em=NOW()
                """,
                (_norm(laboratorio), laboratorio, json.dumps(domains)),
            )
    conn.commit()


def load_source_registry(conn):
    registry = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT laboratorio_chave, laboratorio_nome, dominios
            FROM ecommerce_fontes_fabricantes
            WHERE confianca='alta'
            """
        )
        for row in cur.fetchall():
            domains = tuple(
                domain for domain in (row.get("dominios") or [])
                if isinstance(domain, str) and domain
            )
            if domains:
                registry[row["laboratorio_chave"]] = (
                    row["laboratorio_nome"],
                    domains,
                )
    return registry


def registry_lookup(lab, registry):
    normalized = _norm(lab)
    if not normalized:
        return None, ()
    for key, (name, domains) in registry.items():
        if key in normalized or normalized in key:
            return name, domains
    return _lab_domains(lab)


def save_discovered_source(conn, discovery):
    lab = (discovery.get("laboratorio") or "").strip()
    domain = (discovery.get("dominio_oficial") or "").lower().removeprefix("www.")
    if not lab or not domain:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ecommerce_fontes_fabricantes
                (laboratorio_chave, laboratorio_nome, dominios,
                 fonte_descoberta_url, evidencia, confianca, resposta_ia,
                 descoberto_em, confirmado_em)
            VALUES (%s,%s,%s::jsonb,%s,%s,'alta',%s::jsonb,NOW(),NOW())
            ON CONFLICT (laboratorio_chave) DO UPDATE SET
                laboratorio_nome=EXCLUDED.laboratorio_nome,
                dominios=(
                    SELECT jsonb_agg(DISTINCT value)
                    FROM jsonb_array_elements(
                        ecommerce_fontes_fabricantes.dominios || EXCLUDED.dominios
                    )
                ),
                fonte_descoberta_url=EXCLUDED.fonte_descoberta_url,
                evidencia=EXCLUDED.evidencia,
                confianca='alta',
                resposta_ia=EXCLUDED.resposta_ia,
                confirmado_em=NOW()
            """,
            (
                _norm(lab), lab, json.dumps([domain]),
                discovery.get("fonte_url"), discovery.get("evidencia"),
                json.dumps(discovery, ensure_ascii=False),
            ),
        )
    conn.commit()


def resolve_store(conn, cnpj, cidade):
    if cnpj:
        return cnpj
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cnpjloja, razao, endereco
            FROM users
            WHERE is_admin=FALSE
              AND (
                    translate(lower(COALESCE(razao, '')),
                              'áàâãäéèêëíìîïóòôõöúùûüç',
                              'aaaaaeeeeiiiiooooouuuuc') LIKE %s
                 OR translate(lower(COALESCE(endereco, '')),
                              'áàâãäéèêëíìîïóòôõöúùûüç',
                              'aaaaaeeeeiiiiooooouuuuc') LIKE %s
              )
            ORDER BY razao
            LIMIT 2
            """,
            (f"%{_ascii(cidade).lower()}%", f"%{_ascii(cidade).lower()}%"),
        )
        rows = cur.fetchall()
    if not rows:
        raise RuntimeError(f"nenhuma loja encontrada para cidade/termo: {cidade}")
    if len(rows) > 1:
        nomes = ", ".join(f"{r['razao']} ({r['cnpjloja']})" for r in rows)
        raise RuntimeError(f"mais de uma loja encontrada; informe --cnpj: {nomes}")
    row = rows[0]
    print(f"Loja: {row['razao']} | CNPJ: {row['cnpjloja']}")
    return row["cnpjloja"]


def fetch_products(conn, cnpj, limit, ean=None, eans=None, force=False):
    selected_eans = [_digits(value) for value in (eans or []) if _digits(value)]
    params = {
        "cnpj": cnpj,
        "limit": limit,
        "ean": _digits(ean) if ean else None,
        "eans": selected_eans,
    }
    force_filter = "" if force else """
        AND NOT EXISTS (
            SELECT 1 FROM ecommerce_regulatorio_ean er
            WHERE er.ean = b.ean
              AND er.consultado_em >= NOW() - INTERVAL '180 days'
              AND er.status IN ('aplicado', 'confirmado', 'nao_medicamento')
        )
    """
    if ean:
        ean_filter = "AND b.ean = %(ean)s"
    elif selected_eans:
        ean_filter = "AND b.ean = ANY(%(eans)s)"
    else:
        ean_filter = ""
    sql = f"""
        WITH base AS (
            SELECT
                LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0') AS ean,
                COALESCE(m.descricao, e.descricao) AS nome,
                COALESCE(m.laboratorio, m.marca, pc.laboratorio, '') AS laboratorio,
                COALESCE(cls.tipo, m.tipo_ia, '') AS tipo_atual,
                e.estoque AS quantidade
            FROM estoque e
            LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
            LEFT JOIN produto_canon pc ON pc.ean = LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0')
            LEFT JOIN ecommerce_classificacao_ean cls
                   ON cls.ean = LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0')
            WHERE e.cnpj=%(cnpj)s AND e.estoque > 0

            UNION ALL

            SELECT
                LTRIM(COALESCE(ae.ean, ''), '0') AS ean,
                COALESCE(m.descricao, ae.descricao_produto) AS nome,
                COALESCE(m.laboratorio, m.marca, pc.laboratorio, '') AS laboratorio,
                COALESCE(cls.tipo, m.tipo_ia, '') AS tipo_atual,
                ae.quantidade_estoque AS quantidade
            FROM automatiza_estoque ae
            LEFT JOIN medicamentos m ON m.barra_norm = ae.ean
            LEFT JOIN produto_canon pc ON pc.ean = LTRIM(COALESCE(ae.ean, ''), '0')
            LEFT JOIN ecommerce_classificacao_ean cls
                   ON cls.ean = LTRIM(COALESCE(ae.ean, ''), '0')
            WHERE ae.cnpj_loja=%(cnpj)s AND ae.quantidade_estoque > 0
        )
        SELECT DISTINCT ON (b.ean)
               b.ean, b.nome, b.laboratorio, b.tipo_atual, b.quantidade
        FROM base b
        WHERE b.ean <> '' AND b.nome IS NOT NULL
          AND b.ean ~ '^[0-9]+$'
          AND LENGTH(b.ean) BETWEEN 7 AND 14
          {ean_filter}
          {force_filter}
        ORDER BY b.ean, b.quantidade DESC
        LIMIT %(limit)s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _extract_text_and_citations(response):
    texts = []
    citations = []
    for block in response.get("content", []):
        if block.get("type") != "text":
            continue
        texts.append(block.get("text", ""))
        for citation in block.get("citations") or []:
            url = citation.get("url")
            if url and url not in citations:
                citations.append(url)
    return "\n".join(texts).strip(), citations


def _parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("resposta sem objeto JSON")
    return json.loads(text[start:end + 1])


def _http_error_detail(exc):
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    return f"{exc}; resposta={body[:1200]}" if body else str(exc)


def research_product(product, lab_alias, domains):
    prompt = f"""
Pesquise EXCLUSIVAMENTE nos dominios oficiais permitidos o produto brasileiro abaixo.
A identidade principal deve ser comprovada pela combinacao:
laboratorio + nome comercial ou principio ativo + concentracao + forma farmaceutica +
quantidade/volume/apresentacao. O EAN e auxiliar: se aparecer, confirme; se nao aparecer,
NAO reduza a confianca quando nome, dose, forma e apresentacao forem correspondencia exata.
Nao use paginas de farmacias, marketplaces, blogs, agregadores de bula ou snippets sem
pagina oficial verificavel.

EAN: {product['ean']}
Nome no estoque: {product['nome']}
Laboratorio informado: {product['laboratorio']}
Tipo atual: {product['tipo_atual']}
Laboratorio esperado: {lab_alias}

Classifique tipo_produto como um de:
generico, similar, referencia, suplemento, perfumaria, dermocosmetico, nutricao, varejo.

Para medicamento, extraia tarja somente se a pagina/bula oficial trouxer evidencia:
"preta", "vermelha", "sem_tarja" ou null. Nao deduza tarja pela classe terapeutica.
Guarde em classificacao_tarja_fabricante o texto literal da fonte. Se a fonte disser
"Branca Comum", normalize tarja como "sem_tarja", mesmo que exija prescricao, e mantenha
receita_retida=false salvo quando houver mencao explicita a retencao/notificacao.
Extraia tambem numero de registro ANVISA, principio ativo, apresentacao, retencao de
receita, permissao de venda online, regra de exibicao de imagem e dizeres obrigatorios.
Nao confunda "venda sob prescricao" com retencao da receita.
receita_retida, venda_online_permitida e exibir_imagem_publica devem ser true, false
ou null se nao houver base explicita.
Use a URL direta da bula oficial, preferencialmente PDF. imagem_url deve ser uma imagem
oficial do produto, nunca logo/banner. Se dose, forma ou apresentacao divergirem, use
confianca baixa e nao misture dados de outra apresentacao.

Para CADA campo regulatorio, informe evidencia literal resumida e confianca propria.
Um campo sem evidencia deve ficar null e com confianca baixa.

Responda SOMENTE JSON:
{{
  "encontrado": true,
  "laboratorio_confirmado": "texto ou null",
  "tipo_produto": "valor",
  "registro_anvisa": "numero ou null",
  "principio_ativo": "texto ou null",
  "apresentacao": "texto ou null",
  "tarja": "preta|vermelha|sem_tarja|null",
  "classificacao_tarja_fabricante": "texto literal, ex: Branca Comum, Tarja Vermelha sob restricao",
  "receita_retida": true,
  "venda_online_permitida": true,
  "exibir_imagem_publica": true,
  "dizeres_receita": "texto obrigatorio ou null",
  "dizeres_imagem": "texto/restricao de publicidade ou null",
  "produto_url": "https://... ou null",
  "bula_url": "https://... ou null",
  "imagem_url": "https://... ou null",
  "fonte_dominio": "dominio",
  "identidade_confirmada": true,
  "criterios_identidade": {{
    "nome_ou_principio": true,
    "concentracao": true,
    "forma_farmaceutica": true,
    "apresentacao": true,
    "ean": true
  }},
  "confianca": "alta|media|baixa",
  "evidencia": "resumo objetivo do que comprova identidade do produto",
  "evidencias_campos": {{
    "tipo_produto": "evidencia ou null",
    "registro_anvisa": "evidencia ou null",
    "tarja": "evidencia ou null",
    "receita_retida": "evidencia ou null",
    "venda_online_permitida": "evidencia ou null",
    "exibir_imagem_publica": "evidencia ou null",
    "dizeres_receita": "evidencia ou null",
    "dizeres_imagem": "evidencia ou null"
  }},
  "confiancas_campos": {{
    "tipo_produto": "alta|media|baixa",
    "registro_anvisa": "alta|media|baixa",
    "tarja": "alta|media|baixa",
    "receita_retida": "alta|media|baixa",
    "venda_online_permitida": "alta|media|baixa",
    "exibir_imagem_publica": "alta|media|baixa",
    "dizeres_receita": "alta|media|baixa",
    "dizeres_imagem": "alta|media|baixa"
  }}
}}
"""
    body = {
        "model": MODEL,
        "max_tokens": 1400,
        "tools": [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
            "allowed_domains": list(domains),
        }],
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
            text, citations = _extract_text_and_citations(payload)
            result = _parse_json(text)
            result["citacoes"] = citations
            return result
        except urllib.error.HTTPError as exc:
            last_error = _http_error_detail(exc)
            if "credit balance is too low" in str(last_error).lower():
                raise AnthropicBillingError(str(last_error)) from exc
            if exc.code == 400:
                break
            if attempt < 2:
                time.sleep(2 ** attempt)
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"falha na pesquisa Anthropic: {last_error}")


def inspect_official_image(image_url, domains):
    if not image_url or not _allowed_url(image_url, domains):
        return None
    request = urllib.request.Request(
        image_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; PoupaquiCatalogAudit/1.0)"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        content_type = (response.headers.get_content_type() or "").lower()
        image_bytes = response.read(5 * 1024 * 1024 + 1)
    if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        return None
    if not image_bytes or len(image_bytes) > 5 * 1024 * 1024:
        return None
    body = {
        "model": MODEL,
        "max_tokens": 700,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": content_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Leia somente o que esta visivel nesta embalagem oficial brasileira. "
                        "Nao use conhecimento externo. Retorne JSON com: "
                        "produto_corresponde (true/false/null), ean_visivel, tarja "
                        "(preta/vermelha/sem_tarja/null), dizeres_receita, dizeres_imagem, "
                        "registro_anvisa, evidencia_visual e confianca (alta/media/baixa)."
                    ),
                },
            ],
        }],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        if "credit balance is too low" in detail.lower():
            raise AnthropicBillingError(detail) from exc
        raise RuntimeError(detail) from exc
    text, _ = _extract_text_and_citations(payload)
    return _parse_json(text)


def discover_lab(product):
    prompt = f"""
Identifique o fabricante/laboratorio e seu dominio oficial. Use primeiro o laboratorio
informado e o nome do produto; o EAN e auxiliar e pode nao estar publicado pelo fabricante.
Aceite confianca alta quando a pagina oficial comprovar o laboratorio pela marca/produto
ou quando o nome empresarial informado corresponder claramente ao dominio institucional.
Nao aceite distribuidor, farmacia, marketplace ou agregador de bula.

EAN: {product['ean']}
Nome no estoque: {product['nome']}
Laboratorio informado: {product.get('laboratorio') or 'nao informado'}

Responda SOMENTE JSON:
{{
  "laboratorio": "nome oficial ou null",
  "dominio_oficial": "dominio sem www ou null",
  "fonte_url": "pagina institucional oficial ou null",
  "produto_ou_laboratorio_confirmado": true,
  "ean_confirmado": true,
  "confianca": "alta|media|baixa",
  "evidencia": "resumo curto"
}}
"""
    body = {
        "model": MODEL,
        "max_tokens": 600,
        "tools": [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 3,
        }],
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        if "credit balance is too low" in detail.lower():
            raise AnthropicBillingError(detail) from exc
        raise RuntimeError(detail) from exc
    text, citations = _extract_text_and_citations(payload)
    result = _parse_json(text)
    result["citacoes"] = citations
    if (
        result.get("produto_ou_laboratorio_confirmado") is not True
        or result.get("confianca") != "alta"
    ):
        return None
    claimed_domain = (result.get("dominio_oficial") or "").lower().removeprefix("www.")
    if not claimed_domain or any(
        claimed_domain == blocked or claimed_domain.endswith("." + blocked)
        for blocked in BLOCKED_DISCOVERY_DOMAINS
    ):
        return None
    official_urls = [
        result.get("fonte_url"),
        *citations,
    ]
    if not any(_allowed_url(url, (claimed_domain,)) for url in official_urls if url):
        return None
    if not result.get("laboratorio"):
        return None
    result["dominio_oficial"] = claimed_domain
    return result


def _clean_text(value, limit=1000):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def merge_visual_evidence(result, visual):
    if not visual or visual.get("confianca") not in {"alta", "media"}:
        return result
    result["evidencia_visual"] = visual
    field_evidence = result.setdefault("evidencias_campos", {})
    field_confidence = result.setdefault("confiancas_campos", {})
    visual_evidence = _clean_text(visual.get("evidencia_visual"), 1000)
    for field in ("registro_anvisa", "dizeres_receita", "dizeres_imagem"):
        if not result.get(field) and visual.get(field):
            result[field] = visual[field]
            field_evidence[field] = visual_evidence
            field_confidence[field] = visual.get("confianca")
    if not result.get("tarja") and visual.get("tarja") in {
        "preta", "vermelha", "sem_tarja"
    }:
        result["tarja"] = visual["tarja"]
        field_evidence["tarja"] = visual_evidence
        field_confidence["tarja"] = visual.get("confianca")
    return result


def normalize_result(result, domains):
    tipo = (result.get("tipo_produto") or "").strip().lower()
    if tipo not in MED_TYPES | NON_MED_TYPES:
        tipo = ""
    tarja = (result.get("tarja") or "").strip().lower()
    if tarja not in {"preta", "vermelha", "sem_tarja"}:
        tarja = None
    normalized = {
        "encontrado": result.get("encontrado") is True,
        "laboratorio_confirmado": (result.get("laboratorio_confirmado") or "").strip() or None,
        "tipo_produto": tipo or None,
        "registro_anvisa": _clean_text(result.get("registro_anvisa"), 100),
        "principio_ativo": _clean_text(result.get("principio_ativo"), 500),
        "apresentacao": _clean_text(result.get("apresentacao"), 500),
        "tarja": tarja,
        "classificacao_tarja_fabricante": _clean_text(
            result.get("classificacao_tarja_fabricante"), 300
        ),
        "receita_retida": result.get("receita_retida")
        if isinstance(result.get("receita_retida"), bool) else None,
        "venda_online_permitida": result.get("venda_online_permitida")
        if isinstance(result.get("venda_online_permitida"), bool) else None,
        "exibir_imagem_publica": result.get("exibir_imagem_publica")
        if isinstance(result.get("exibir_imagem_publica"), bool) else None,
        "dizeres_receita": _clean_text(result.get("dizeres_receita"), 1000),
        "dizeres_imagem": _clean_text(result.get("dizeres_imagem"), 1000),
        "produto_url": result.get("produto_url"),
        "bula_url": result.get("bula_url"),
        "imagem_url": result.get("imagem_url"),
        "fonte_dominio": None,
        "confianca": (result.get("confianca") or "baixa").lower(),
        "evidencia": (result.get("evidencia") or "").strip()[:3000],
        "evidencias_campos": {
            key: _clean_text(value, 2000)
            for key, value in (result.get("evidencias_campos") or {}).items()
        },
        "confiancas_campos": {
            key: value if value in {"alta", "media", "baixa"} else "baixa"
            for key, value in (result.get("confiancas_campos") or {}).items()
        },
        "citacoes": result.get("citacoes") or [],
        "evidencia_visual": result.get("evidencia_visual"),
        "identidade_confirmada": result.get("identidade_confirmada") is True,
        "criterios_identidade": result.get("criterios_identidade") or {},
    }
    urls = [
        normalized["produto_url"],
        normalized["bula_url"],
        normalized["imagem_url"],
        *normalized["citacoes"],
    ]
    normalized["dominio_valido"] = any(_allowed_url(url, domains) for url in urls if url)
    for field in ("produto_url", "bula_url", "imagem_url"):
        if normalized[field] and not _allowed_url(normalized[field], domains):
            normalized[field] = None
    official_urls = [
        normalized["produto_url"],
        normalized["bula_url"],
        *[url for url in normalized["citacoes"] if _allowed_url(url, domains)],
    ]
    normalized["fonte_dominio"] = next(
        (_domain(url) for url in official_urls if _domain(url)),
        None,
    )
    return normalized


def evidence_status(result):
    if not result["encontrado"]:
        return "nao_encontrado"
    if not result["dominio_valido"]:
        return "dominio_invalido"
    if not result["identidade_confirmada"]:
        return "revisao_manual"
    identity_confidence = result["confiancas_campos"].get(
        "tipo_produto", result["confianca"]
    )
    if identity_confidence != "alta" or len(result["evidencia"]) < 20:
        return "revisao_manual"
    if result["tipo_produto"] in NON_MED_TYPES:
        return "nao_medicamento"
    if result["tipo_produto"] in MED_TYPES:
        critical = ("tarja", "receita_retida", "exibir_imagem_publica")
        if any(
            result["confiancas_campos"].get(field) != "alta"
            for field in critical
        ):
            return "revisao_manual"
        return "confirmado"
    return "revisao_manual"


def compare_with_cache(conn, product, result):
    if not result.get("encontrado") or not result.get("identidade_confirmada"):
        return {"chave": _chave(product["nome"]), "comparacao_ignorada": True}, []
    chave = _chave(product["nome"])
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ac.chave, ac.encontrado, ac.nome_anvisa, ac.laboratorio,
                   ac.principio_ativo, ac.tarja, ac.receita_retida,
                   ac.venda_online_permitida, ac.exibir_imagem_publica,
                   ac.dizeres_receita, ac.dizeres_imagem, ac.url_bula,
                   ac.url_bula_fabricante,
                   cls.tipo AS tipo_ean, cls.confianca AS tipo_confianca
            FROM anvisa_cache ac
            LEFT JOIN ecommerce_classificacao_ean cls ON cls.ean=%s
            WHERE ac.chave=%s
            LIMIT 1
            """,
            (product["ean"], chave),
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                """
                SELECT ac.chave, ac.encontrado, ac.nome_anvisa, ac.laboratorio,
                       ac.principio_ativo, ac.tarja, ac.receita_retida,
                       ac.venda_online_permitida, ac.exibir_imagem_publica,
                       ac.dizeres_receita, ac.dizeres_imagem, ac.url_bula,
                       ac.url_bula_fabricante,
                       cls.tipo AS tipo_ean, cls.confianca AS tipo_confianca
                FROM medicamentos m
                JOIN anvisa_cache ac ON ac.id_produto=m.id
                LEFT JOIN ecommerce_classificacao_ean cls ON cls.ean=%s
                WHERE LTRIM(COALESCE(m.barra_norm, m.barra::text, ''), '0')=%s
                ORDER BY ac.criado_em DESC
                LIMIT 1
                """,
                (product["ean"], product["ean"]),
            )
            row = cur.fetchone()
    cache = dict(row) if row else {
        "chave": chave,
        "encontrado": False,
        "tipo_ean": product.get("tipo_atual") or None,
    }
    mapping = {
        "tipo_produto": "tipo_ean",
        "laboratorio_confirmado": "laboratorio",
        "principio_ativo": "principio_ativo",
        "tarja": "tarja",
        "receita_retida": "receita_retida",
        "venda_online_permitida": "venda_online_permitida",
        "exibir_imagem_publica": "exibir_imagem_publica",
        "dizeres_receita": "dizeres_receita",
        "dizeres_imagem": "dizeres_imagem",
    }
    divergences = []
    confidence = result.get("confiancas_campos") or {}
    for source_field, cache_field in mapping.items():
        new_value = result.get(source_field)
        old_value = cache.get(cache_field)
        if source_field == "tarja" and new_value == "sem_tarja":
            new_value = None
        if new_value is None:
            continue
        if source_field not in {"laboratorio_confirmado", "principio_ativo"}:
            if confidence.get(source_field, result.get("confianca")) != "alta":
                continue
        old_norm = _norm(str(old_value)) if isinstance(old_value, str) else old_value
        new_norm = _norm(str(new_value)) if isinstance(new_value, str) else new_value
        if old_norm != new_norm:
            divergences.append({
                "campo": source_field,
                "cache": old_value,
                "fabricante": new_value,
                "confianca": confidence.get(source_field, result.get("confianca")),
                "evidencia": (result.get("evidencias_campos") or {}).get(source_field),
            })
    if result.get("bula_url") and result["bula_url"] not in {
        cache.get("url_bula"), cache.get("url_bula_fabricante")
    }:
        divergences.append({
            "campo": "bula_url",
            "cache": cache.get("url_bula_fabricante") or cache.get("url_bula"),
            "fabricante": result["bula_url"],
            "confianca": result.get("confianca"),
            "evidencia": result.get("evidencia"),
        })
    return cache, divergences


def save_evidence(conn, product, result, raw_result, status, cache, divergences):
    params = {
        "ean": product["ean"],
        "chave": _chave(product["nome"]),
        "nome": product["nome"],
        "lab_informado": product.get("laboratorio_original", product["laboratorio"]),
        "lab_confirmado": result["laboratorio_confirmado"],
        "tipo": result["tipo_produto"],
        "registro": result["registro_anvisa"],
        "principio": result["principio_ativo"],
        "apresentacao": result["apresentacao"],
        "tarja": result["tarja"],
        "classificacao_tarja": result["classificacao_tarja_fabricante"],
        "receita": result["receita_retida"],
        "venda": result["venda_online_permitida"],
        "exibir": result["exibir_imagem_publica"],
        "dizeres_receita": result["dizeres_receita"],
        "dizeres_imagem": result["dizeres_imagem"],
        "produto_url": result["produto_url"],
        "bula_url": result["bula_url"],
        "imagem_url": result["imagem_url"],
        "dominio": result["fonte_dominio"],
        "confianca": result["confianca"],
        "evidencia": result["evidencia"],
        "evidencias_campos": json.dumps(
            result["evidencias_campos"], ensure_ascii=False
        ),
        "confiancas_campos": json.dumps(
            result["confiancas_campos"], ensure_ascii=False
        ),
        "citacoes": json.dumps(result["citacoes"], ensure_ascii=False),
        "resposta": json.dumps(raw_result, ensure_ascii=False),
        "cache": json.dumps(cache, ensure_ascii=False, default=str),
        "divergencias": json.dumps(divergences, ensure_ascii=False, default=str),
        "status": status,
        "modelo": MODEL,
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ecommerce_regulatorio_ean
                (ean, chave_anvisa, nome_estoque, laboratorio_informado,
                 laboratorio_confirmado, tipo_produto, registro_anvisa,
                 principio_ativo, apresentacao, tarja,
                 classificacao_tarja_fabricante, receita_retida,
                 venda_online_permitida, exibir_imagem_publica,
                 dizeres_receita, dizeres_imagem, produto_url, bula_url,
                 imagem_url, fonte_dominio, confianca, evidencia,
                 evidencias_campos, confiancas_campos, citacoes, resposta_ia,
                 cache_antes, divergencias, status, modelo_ia, consultado_em)
            VALUES
                (%(ean)s,%(chave)s,%(nome)s,%(lab_informado)s,
                 %(lab_confirmado)s,%(tipo)s,%(registro)s,%(principio)s,
                 %(apresentacao)s,%(tarja)s,%(classificacao_tarja)s,
                 %(receita)s,%(venda)s,%(exibir)s,
                 %(dizeres_receita)s,%(dizeres_imagem)s,%(produto_url)s,
                 %(bula_url)s,%(imagem_url)s,%(dominio)s,%(confianca)s,
                 %(evidencia)s,%(evidencias_campos)s::jsonb,
                 %(confiancas_campos)s::jsonb,%(citacoes)s::jsonb,
                 %(resposta)s::jsonb,%(cache)s::jsonb,%(divergencias)s::jsonb,
                 %(status)s,%(modelo)s,NOW())
            ON CONFLICT (ean) DO UPDATE SET
                chave_anvisa=EXCLUDED.chave_anvisa,
                nome_estoque=EXCLUDED.nome_estoque,
                laboratorio_informado=EXCLUDED.laboratorio_informado,
                laboratorio_confirmado=EXCLUDED.laboratorio_confirmado,
                tipo_produto=EXCLUDED.tipo_produto,
                registro_anvisa=EXCLUDED.registro_anvisa,
                principio_ativo=EXCLUDED.principio_ativo,
                apresentacao=EXCLUDED.apresentacao,
                tarja=EXCLUDED.tarja,
                classificacao_tarja_fabricante=EXCLUDED.classificacao_tarja_fabricante,
                receita_retida=EXCLUDED.receita_retida,
                venda_online_permitida=EXCLUDED.venda_online_permitida,
                exibir_imagem_publica=EXCLUDED.exibir_imagem_publica,
                dizeres_receita=EXCLUDED.dizeres_receita,
                dizeres_imagem=EXCLUDED.dizeres_imagem,
                produto_url=EXCLUDED.produto_url,
                bula_url=EXCLUDED.bula_url,
                imagem_url=EXCLUDED.imagem_url,
                fonte_dominio=EXCLUDED.fonte_dominio,
                confianca=EXCLUDED.confianca,
                evidencia=EXCLUDED.evidencia,
                evidencias_campos=EXCLUDED.evidencias_campos,
                confiancas_campos=EXCLUDED.confiancas_campos,
                citacoes=EXCLUDED.citacoes,
                resposta_ia=EXCLUDED.resposta_ia,
                cache_antes=EXCLUDED.cache_antes,
                divergencias=EXCLUDED.divergencias,
                status=EXCLUDED.status,
                modelo_ia=EXCLUDED.modelo_ia,
                consultado_em=NOW()
            """,
            params,
        )
    conn.commit()


def apply_result(conn, cnpj, product, result):
    ean = product["ean"]
    chave = _chave(product["nome"])
    tipo = result["tipo_produto"]
    field_confidence = result.get("confiancas_campos") or {}

    def trusted(field):
        return result.get(field) if field_confidence.get(field) == "alta" else None

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ecommerce_classificacao_ean
                (ean, nome_ref, tipo, categoria, confianca, modelo_ia, classificado_em)
            VALUES (%s,%s,%s,%s,'alta',%s,NOW())
            ON CONFLICT (ean) DO UPDATE SET
                nome_ref=EXCLUDED.nome_ref,
                tipo=EXCLUDED.tipo,
                categoria=EXCLUDED.categoria,
                confianca='alta',
                modelo_ia=EXCLUDED.modelo_ia,
                classificado_em=NOW()
            """,
            (ean, product["nome"], tipo, "medicamento" if tipo in MED_TYPES else tipo, MODEL),
        )

        if tipo in MED_TYPES:
            tarja_db = None if result["tarja"] == "sem_tarja" else result["tarja"]
            cur.execute(
                """
                INSERT INTO anvisa_cache
                    (chave, encontrado, laboratorio, principio_ativo, tarja,
                     receita_retida, venda_online_permitida,
                     exibir_imagem_publica, dizeres_receita, dizeres_imagem,
                     url_bula_fabricante,
                     fonte_fabricante_url, fonte_fabricante_dominio,
                     fonte_fabricante_confianca, fonte_fabricante_consultada_em)
                VALUES (%s,TRUE,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'alta',NOW())
                ON CONFLICT (chave) DO UPDATE SET
                    encontrado=TRUE,
                    laboratorio=COALESCE(EXCLUDED.laboratorio, anvisa_cache.laboratorio),
                    principio_ativo=COALESCE(
                        EXCLUDED.principio_ativo, anvisa_cache.principio_ativo
                    ),
                    tarja=EXCLUDED.tarja,
                    receita_retida=EXCLUDED.receita_retida,
                    venda_online_permitida=COALESCE(
                        EXCLUDED.venda_online_permitida,
                        anvisa_cache.venda_online_permitida
                    ),
                    exibir_imagem_publica=EXCLUDED.exibir_imagem_publica,
                    dizeres_receita=COALESCE(
                        EXCLUDED.dizeres_receita, anvisa_cache.dizeres_receita
                    ),
                    dizeres_imagem=COALESCE(
                        EXCLUDED.dizeres_imagem, anvisa_cache.dizeres_imagem
                    ),
                    url_bula_fabricante=COALESCE(
                        EXCLUDED.url_bula_fabricante, anvisa_cache.url_bula_fabricante
                    ),
                    fonte_fabricante_url=EXCLUDED.fonte_fabricante_url,
                    fonte_fabricante_dominio=EXCLUDED.fonte_fabricante_dominio,
                    fonte_fabricante_confianca='alta',
                    fonte_fabricante_consultada_em=NOW()
                """,
                (
                    chave, result["laboratorio_confirmado"],
                    result["principio_ativo"], tarja_db,
                    result["receita_retida"], trusted("venda_online_permitida"),
                    result["exibir_imagem_publica"], trusted("dizeres_receita"),
                    trusted("dizeres_imagem"), result["bula_url"],
                    result["produto_url"] or result["bula_url"],
                    result["fonte_dominio"],
                ),
            )

            image_url = None
            if result["exibir_imagem_publica"] is True and result["imagem_url"]:
                image_url = result["imagem_url"]
            elif result["exibir_imagem_publica"] is False:
                if tarja_db == "preta":
                    image_url = GENERIC_TARJA_PRETA_IMG
                elif tarja_db == "vermelha":
                    image_url = GENERIC_TARJA_VERMELHA_IMG
            if image_url:
                cur.execute(
                    """
                    INSERT INTO ecommerce_produto_imagens
                        (cnpjloja, ean, imagem_url, updated_at)
                    VALUES (%s,%s,%s,NOW())
                    ON CONFLICT (cnpjloja, ean) DO UPDATE SET
                        imagem_url=EXCLUDED.imagem_url, updated_at=NOW()
                    """,
                    (cnpj, ean, image_url),
                )

        cur.execute(
            """
            UPDATE ecommerce_regulatorio_ean
            SET status='aplicado', aplicado_em=NOW()
            WHERE ean=%s
            """,
            (ean,),
        )
    conn.commit()


def main():
    parser = argparse.ArgumentParser()
    store = parser.add_mutually_exclusive_group()
    store.add_argument("--cnpj")
    store.add_argument("--cidade", default="Sao Pedro")
    product_filter = parser.add_mutually_exclusive_group()
    product_filter.add_argument("--ean")
    product_filter.add_argument(
        "--eans",
        help="lista de EANs separados por virgula para auditoria comparativa",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--todos",
        action="store_true",
        help="audita todos os produtos visiveis da loja, respeitando o cache",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--descobrir-lab",
        action="store_true",
        help="mantido por compatibilidade; descoberta agora e o padrao",
    )
    parser.add_argument(
        "--sem-descobrir-lab",
        action="store_true",
        help="nao pesquisa novos fabricantes; usa apenas fontes ja confirmadas",
    )
    parser.add_argument(
        "--sem-vision",
        action="store_true",
        help="nao analisa a embalagem oficial com Claude Vision",
    )
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        sys.exit("ANTHROPIC_API_KEY ausente")

    conn = _connect()
    ensure_schema(conn)
    registry = load_source_registry(conn)
    cnpj = resolve_store(conn, args.cnpj, args.cidade)
    eans = [value.strip() for value in (args.eans or "").split(",") if value.strip()]
    limit = 100000 if args.todos else max(1, args.limit)
    products = fetch_products(conn, cnpj, limit, args.ean, eans, args.force)
    print(f"Produtos selecionados: {len(products)} | modo: {'APPLY' if args.apply else 'DRY-RUN'}")

    stats = {}
    for index, product in enumerate(products, 1):
        product["laboratorio_original"] = product["laboratorio"]
        product["laboratorio"], lab_inferido = _infer_lab(
            product["nome"], product["laboratorio"]
        )
        lab_alias, domains = registry_lookup(product["laboratorio"], registry)
        if not domains and not args.sem_descobrir_lab:
            try:
                discovery = discover_lab(product)
                if discovery:
                    save_discovered_source(conn, discovery)
                    lab_alias = discovery["laboratorio"]
                    domains = (discovery["dominio_oficial"],)
                    registry[_norm(lab_alias)] = (lab_alias, domains)
                    product["laboratorio"] = lab_alias
                    lab_inferido = True
            except Exception as exc:
                print(f"[{index}/{len(products)}] LAB-ERRO {product['ean']}: {exc}")
        if not domains:
            status = "laboratorio_sem_adapter"
            stats[status] = stats.get(status, 0) + 1
            print(
                f"[{index}/{len(products)}] SKIP {product['ean']} "
                f"{product['nome'][:45]} | lab={product['laboratorio'] or '?'}"
            )
            continue
        if lab_inferido:
            print(
                f"[{index}/{len(products)}] LAB  {product['ean']} "
                f"{product['laboratorio_original'] or '?'} -> {product['laboratorio']}"
            )
        try:
            raw = research_product(product, lab_alias, domains)
            if not raw.get("encontrado") and not args.sem_descobrir_lab:
                try:
                    discovery = discover_lab(product)
                    discovered_domain = (
                        discovery.get("dominio_oficial") if discovery else None
                    )
                    if discovery and discovered_domain not in domains:
                        save_discovered_source(conn, discovery)
                        lab_alias = discovery["laboratorio"]
                        domains = (discovered_domain,)
                        product["laboratorio"] = lab_alias
                        print(
                            f"[{index}/{len(products)}] REATRIBUIR {product['ean']} "
                            f"-> {lab_alias} ({discovered_domain})"
                        )
                        raw = research_product(product, lab_alias, domains)
                except Exception as exc:
                    print(
                        f"[{index}/{len(products)}] REDESCOBRIR-ERRO "
                        f"{product['ean']}: {exc}"
                    )
            if raw.get("imagem_url") and not args.sem_vision:
                try:
                    visual = inspect_official_image(raw["imagem_url"], domains)
                    raw = merge_visual_evidence(raw, visual)
                except Exception as exc:
                    print(f"[{index}/{len(products)}] VISION-ERRO {product['ean']}: {exc}")
            result = normalize_result(raw, domains)
            status = evidence_status(result)
            cache, divergences = compare_with_cache(conn, product, result)
            save_evidence(
                conn, product, result, raw, status, cache, divergences
            )
            if args.apply and status in {"confirmado", "nao_medicamento"}:
                apply_result(conn, cnpj, product, result)
                status = "aplicado"
            stats[status] = stats.get(status, 0) + 1
            print(
                f"[{index}/{len(products)}] {status.upper():18} {product['ean']} "
                f"| {result['tipo_produto'] or '?'} | {result['tarja'] or '-'} "
                f"| {result['fonte_dominio'] or '-'} | divergencias={len(divergences)}"
            )
            for divergence in divergences:
                print(
                    f"    DIFF {divergence['campo']}: "
                    f"cache={divergence['cache']!r} -> "
                    f"fabricante={divergence['fabricante']!r}"
                )
        except AnthropicBillingError as exc:
            conn.rollback()
            print(f"[{index}/{len(products)}] SALDO ANTHROPIC INSUFICIENTE: {exc}")
            stats["saldo_insuficiente"] = stats.get("saldo_insuficiente", 0) + 1
            break
        except Exception as exc:
            conn.rollback()
            status = "erro"
            stats[status] = stats.get(status, 0) + 1
            print(f"[{index}/{len(products)}] ERRO {product['ean']}: {exc}")
        time.sleep(max(0, args.delay))

    conn.close()
    print("\nResumo:")
    for status, count in sorted(stats.items()):
        print(f"  {status}: {count}")


if __name__ == "__main__":
    main()
