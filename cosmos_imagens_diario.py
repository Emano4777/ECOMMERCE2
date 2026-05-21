"""
cosmos_imagens_diario.py - preenche imagens por EAN usando a API Bluesoft Cosmos.

Fluxo seguro para a cota gratuita:
  - consulta no maximo 25 EANs por dia por padrao;
  - prioriza produtos em estoque sem imagem, com perfumaria/similares antes;
  - registra cada consulta para nao gastar cota repetindo EAN sem resultado;
  - baixa a thumbnail do Cosmos para o Storage do projeto;
  - propaga a imagem para medicamentos, produto_canon e ecommerce_produto_imagens.

Uso:
  python cosmos_imagens_diario.py
  python cosmos_imagens_diario.py --limit 25
  python cosmos_imagens_diario.py --dry-run
  python cosmos_imagens_diario.py --force
"""

import argparse
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from app import (  # noqa: E402
    db,
    _digits,
    _CLOUDINARY_OK,
    _download_image_for_storage,
    _ensure_precificador_schema,
    _ensure_produto_canon_schema,
    _upsert_catalog_image_all_stores,
    upload_to_supabase_storage,
)

try:
    import cloudinary.uploader
except Exception:
    cloudinary = None

COSMOS_TOKEN = os.getenv("COSMOS_TOKEN", "").strip()
COSMOS_URL = "https://api.cosmos.bluesoft.com.br/gtins/{ean}.json"
DEFAULT_PER_TOKEN_LIMIT = 25
RETRY_MISS_DAYS = 30

PERFUMARIA_RE = (
    "perfume|deo col[oô]nia|col[oô]nia|desodorante|sabonete|shampoo|condicionador|"
    "creme|hidratante|protetor|solar|fps|dermo|lo[cç][aã]o|gel|esmalte|batom|"
    "maquiagem|tintura|absorvente|fralda|escova|pasta dental|enxaguante|fio dental|"
    "repelente|talco|oleo|óleo|serum|s[eé]rum|mascara|máscara"
)


def _token_id(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _load_tokens():
    raw = os.getenv("COSMOS_TOKENS", "")
    tokens = []
    if COSMOS_TOKEN:
        tokens.append(COSMOS_TOKEN)
    tokens.extend(t.strip() for t in re.split(r"[\s,;]+", raw) if t.strip())
    result = []
    seen = set()
    for token in tokens:
        tid = _token_id(token)
        if tid not in seen:
            seen.add(tid)
            result.append({"token": token, "id": tid})
    return result


def _ensure_schema(conn):
    _ensure_precificador_schema()
    _ensure_produto_canon_schema()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cosmos_imagem_consultas (
            ean              TEXT PRIMARY KEY,
            status           TEXT NOT NULL,
            descricao_cosmos TEXT,
            laboratorio      TEXT,
            categoria        TEXT,
            thumbnail        TEXT,
            storage_url      TEXT,
            http_status      INTEGER,
            erro             TEXT,
            token_id         TEXT,
            tentativas       INTEGER DEFAULT 1,
            consultado_em    TIMESTAMPTZ DEFAULT NOW(),
            atualizado_em    TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    cur.execute("ALTER TABLE cosmos_imagem_consultas ADD COLUMN IF NOT EXISTS token_id TEXT")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cosmos_token_uso (
            id          SERIAL PRIMARY KEY,
            token_id    TEXT NOT NULL,
            ean         TEXT,
            http_status INTEGER,
            usado_em    TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cosmos_img_status ON cosmos_imagem_consultas(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cosmos_img_consultado ON cosmos_imagem_consultas(consultado_em)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cosmos_token_uso_dia ON cosmos_token_uso(token_id, usado_em)")
    conn.commit()
    cur.close()


def _token_usage_today(conn):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT token_id,
               COUNT(*) AS chamadas,
               BOOL_OR(http_status = 429) AS rate_limited
        FROM cosmos_token_uso
        WHERE usado_em::date = CURRENT_DATE
        GROUP BY token_id
        """
    )
    usage = {
        r["token_id"]: {
            "chamadas": int(r["chamadas"] or 0),
            "rate_limited": bool(r["rate_limited"]),
        }
        for r in cur.fetchall()
    }
    cur.close()
    return usage


def _record_token_use(conn, token_id, ean, http_status):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO cosmos_token_uso (token_id, ean, http_status)
        VALUES (%s,%s,%s)
        """,
        (token_id, ean, http_status),
    )
    cur.close()


def _available_tokens(conn, tokens, per_token_limit):
    usage = _token_usage_today(conn)
    available = []
    for item in tokens:
        used = usage.get(item["id"], {"chamadas": 0, "rate_limited": False})
        remaining = max(0, per_token_limit - used["chamadas"])
        if remaining > 0 and not used["rate_limited"]:
            available.append({**item, "used": used["chamadas"], "remaining": remaining})
    return available


def _remaining_today(conn, tokens, per_token_limit):
    return sum(t["remaining"] for t in _available_tokens(conn, tokens, per_token_limit))


def _candidate_rows(conn, limit, force=False, retry_days=RETRY_MISS_DAYS):
    cur = conn.cursor()
    force_sql = "" if force else f"""
      AND NOT EXISTS (
        SELECT 1
        FROM cosmos_imagem_consultas ci
        WHERE ci.ean = base.ean
          AND (
            ci.status = 'found'
            OR (ci.consultado_em::date = CURRENT_DATE AND ci.status NOT IN ('rate_limited','dry_found'))
            OR (ci.status IN ('not_found','no_image','download_error','upload_error')
                AND ci.consultado_em > NOW() - INTERVAL '{int(retry_days)} days')
          )
      )
    """
    cur.execute(
        f"""
        WITH base_raw AS (
            SELECT
              COALESCE(NULLIF(TRIM(e.barras_norm), ''), NULLIF(TRIM(e.barras), '')) AS ean,
              MAX(e.descricao) AS nome,
              SUM(COALESCE(e.estoque, 0)) AS qtd,
              TRUE AS em_estoque
            FROM estoque e
            WHERE COALESCE(e.estoque, 0) > 0
              AND COALESCE(NULLIF(TRIM(e.barras_norm), ''), NULLIF(TRIM(e.barras), '')) IS NOT NULL
            GROUP BY COALESCE(NULLIF(TRIM(e.barras_norm), ''), NULLIF(TRIM(e.barras), ''))

            UNION ALL

            SELECT
              NULLIF(TRIM(ae.ean), '') AS ean,
              MAX(ae.descricao_produto) AS nome,
              SUM(COALESCE(ae.quantidade_estoque, 0)) AS qtd,
              TRUE AS em_estoque
            FROM automatiza_estoque ae
            WHERE COALESCE(ae.quantidade_estoque, 0) > 0
              AND NULLIF(TRIM(ae.ean), '') IS NOT NULL
            GROUP BY NULLIF(TRIM(ae.ean), '')

            UNION ALL

            SELECT
              COALESCE(NULLIF(TRIM(m.barra_norm), ''), NULLIF(TRIM(m.barra), '')) AS ean,
              MAX(m.descricao) AS nome,
              0 AS qtd,
              FALSE AS em_estoque
            FROM medicamentos m
            WHERE COALESCE(NULLIF(TRIM(m.barra_norm), ''), NULLIF(TRIM(m.barra), '')) IS NOT NULL
              AND (m.imagem IS NULL OR TRIM(m.imagem) = '')
            GROUP BY COALESCE(NULLIF(TRIM(m.barra_norm), ''), NULLIF(TRIM(m.barra), ''))
        ),
        base AS (
            SELECT
              ean,
              MAX(nome) AS nome,
              SUM(qtd) AS qtd,
              BOOL_OR(em_estoque) AS em_estoque
            FROM base_raw
            WHERE LENGTH(REGEXP_REPLACE(COALESCE(ean, ''), '\\D', '', 'g')) >= 8
            GROUP BY ean
        ),
        faltantes AS (
            SELECT base.*
            FROM base
            LEFT JOIN medicamentos m
              ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(REGEXP_REPLACE(base.ean, '\\D', '', 'g'), '0')
            WHERE NOT EXISTS (
                SELECT 1
                FROM ecommerce_produto_imagens epi
                WHERE LTRIM(COALESCE(epi.ean, ''), '0') = LTRIM(REGEXP_REPLACE(base.ean, '\\D', '', 'g'), '0')
                  AND epi.imagem_url IS NOT NULL
                  AND TRIM(epi.imagem_url) <> ''
            )
              AND NOT EXISTS (
                SELECT 1
                FROM medicamentos mm
                WHERE LTRIM(COALESCE(mm.barra_norm, mm.barra, ''), '0') = LTRIM(REGEXP_REPLACE(base.ean, '\\D', '', 'g'), '0')
                  AND mm.imagem IS NOT NULL
                  AND TRIM(mm.imagem) <> ''
            )
            {force_sql}
        )
        SELECT
          REGEXP_REPLACE(ean, '\\D', '', 'g') AS ean,
          nome,
          qtd,
          em_estoque,
          CASE
            WHEN nome ~* %s THEN 0
            WHEN nome ~* 'gen[eé]rico|generico' THEN 3
            WHEN em_estoque THEN 1
            ELSE 2
          END AS prioridade
        FROM faltantes
        ORDER BY prioridade ASC, em_estoque DESC, qtd DESC, nome
        LIMIT %s
        """,
        (PERFUMARIA_RE, limit),
    )
    rows = []
    seen_eans = set()
    for r in cur.fetchall():
        item = dict(r)
        ean = item.get("ean")
        if not ean or ean in seen_eans:
            continue
        seen_eans.add(ean)
        rows.append(item)
    cur.close()
    return rows


def _cosmos_get(conn, ean, token_item):
    if not token_item:
        raise RuntimeError("Nenhum token Cosmos disponivel")
    req = urllib.request.Request(
        COSMOS_URL.format(ean=ean),
        headers={
            "X-Cosmos-Token": token_item["token"],
            "User-Agent": "Cosmos-API-Request",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            _record_token_use(conn, token_item["id"], ean, r.status)
            return r.status, json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:500]
        _record_token_use(conn, token_item["id"], ean, e.code)
        return e.code, {"erro": body}


def _categoria_from_gpc(gpc_desc):
    text = (gpc_desc or "").lower()
    if any(x in text for x in ("cosmet", "perfum", "higiene", "sabonete", "shampoo", "protetor", "creme", "dermo")):
        return "cosmetico"
    if any(x in text for x in ("suplement", "vitamina", "mineral", "protein", "omega")):
        return "suplemento"
    if any(x in text for x in ("medicament", "farmac", "comprimido", "capsula", "xarope", "pomada", "colirio")):
        return "medicamento"
    return None


def _save_consulta(conn, ean, status, http_status=None, data=None, storage_url=None, erro=None, token_id=None):
    data = data or {}
    brand = data.get("brand") or {}
    gpc = data.get("gpc") or {}
    descricao = (data.get("description") or data.get("descricao") or "").strip() or None
    laboratorio = (brand.get("name") or "").strip() or None
    gpc_desc = (gpc.get("description") or "").strip()
    categoria = _categoria_from_gpc(gpc_desc)
    thumbnail = (data.get("thumbnail") or "").strip() or None

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO cosmos_imagem_consultas (
            ean, status, descricao_cosmos, laboratorio, categoria, thumbnail,
            storage_url, http_status, erro, token_id, tentativas, consultado_em, atualizado_em
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,NOW(),NOW())
        ON CONFLICT (ean) DO UPDATE SET
            status = EXCLUDED.status,
            descricao_cosmos = COALESCE(EXCLUDED.descricao_cosmos, cosmos_imagem_consultas.descricao_cosmos),
            laboratorio = COALESCE(EXCLUDED.laboratorio, cosmos_imagem_consultas.laboratorio),
            categoria = COALESCE(EXCLUDED.categoria, cosmos_imagem_consultas.categoria),
            thumbnail = COALESCE(EXCLUDED.thumbnail, cosmos_imagem_consultas.thumbnail),
            storage_url = COALESCE(EXCLUDED.storage_url, cosmos_imagem_consultas.storage_url),
            http_status = EXCLUDED.http_status,
            erro = EXCLUDED.erro,
            token_id = COALESCE(EXCLUDED.token_id, cosmos_imagem_consultas.token_id),
            tentativas = cosmos_imagem_consultas.tentativas + 1,
            consultado_em = NOW(),
            atualizado_em = NOW()
        """,
        (ean, status, descricao, laboratorio, categoria, thumbnail, storage_url, http_status, erro, token_id),
    )
    cur.close()


def _save_product_data(conn, ean, data, storage_url):
    brand = data.get("brand") or {}
    gpc = data.get("gpc") or {}
    descricao = (data.get("description") or data.get("descricao") or "").strip()
    laboratorio = (brand.get("name") or "").strip() or None
    categoria = _categoria_from_gpc((gpc.get("description") or "").strip())
    thumbnail = (data.get("thumbnail") or "").strip() or None

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO produto_canon (
            ean, descricao_original, descricao_canon, laboratorio, categoria, imagem_cosmos, fonte, criado_em, atualizado_em
        )
        VALUES (%s,%s,%s,%s,%s,%s,'cosmos',NOW(),NOW())
        ON CONFLICT (ean) DO UPDATE SET
            descricao_original = COALESCE(EXCLUDED.descricao_original, produto_canon.descricao_original),
            descricao_canon = COALESCE(EXCLUDED.descricao_canon, produto_canon.descricao_canon),
            laboratorio = COALESCE(EXCLUDED.laboratorio, produto_canon.laboratorio),
            categoria = COALESCE(EXCLUDED.categoria, produto_canon.categoria),
            imagem_cosmos = COALESCE(EXCLUDED.imagem_cosmos, produto_canon.imagem_cosmos),
            fonte = CASE WHEN produto_canon.fonte = 'manual' THEN produto_canon.fonte ELSE 'cosmos' END,
            atualizado_em = NOW()
        """,
        (ean, descricao or None, descricao or f"Produto {ean}", laboratorio, categoria, storage_url or thumbnail),
    )
    if storage_url:
        cur.execute(
            """
            UPDATE medicamentos
            SET imagem = %s
            WHERE LTRIM(COALESCE(barra_norm, barra, ''), '0') = LTRIM(%s, '0')
              AND (imagem IS NULL OR TRIM(imagem) = '')
            """,
            (storage_url, ean),
        )
        _upsert_catalog_image_all_stores(cur, ean, storage_url)
    cur.close()


def _upload_product_image(raw, ean, ext, content_type, prefer_cloudinary=True):
    if prefer_cloudinary and _CLOUDINARY_OK and cloudinary is not None:
        try:
            result = cloudinary.uploader.upload(
                (f"{ean}.{ext}", raw),
                resource_type="image",
                folder="produtos/cosmos-ean",
                public_id=ean,
                overwrite=True,
                unique_filename=False,
            )
            secure_url = result.get("secure_url")
            if secure_url:
                return secure_url, "cloudinary"
        except Exception as exc:
            print(f"  [cloudinary] falha upload {ean}: {exc}", flush=True)

    storage_url = upload_to_supabase_storage(raw, f"cosmos-ean/{ean}.{ext}", content_type)
    return storage_url, "supabase" if storage_url else None


def process_one(conn, row, token_item, dry_run=False):
    ean = row["ean"]
    http_status, data = _cosmos_get(conn, ean, token_item)
    if http_status == 429:
        conn.commit()
        return "RATE_LIMIT", None
    if http_status == 404:
        _save_consulta(conn, ean, "not_found", http_status=http_status, data=data, erro="EAN nao encontrado", token_id=token_item["id"])
        conn.commit()
        return "MISS", None
    if http_status != 200:
        _save_consulta(conn, ean, "error", http_status=http_status, data=data, erro=str(data.get("erro") or "Erro Cosmos")[:500], token_id=token_item["id"])
        conn.commit()
        return "ERRO", None

    thumbnail = (data.get("thumbnail") or "").strip()
    if not thumbnail.startswith("http"):
        _save_consulta(conn, ean, "no_image", http_status=http_status, data=data, erro="Sem thumbnail", token_id=token_item["id"])
        if not dry_run:
            _save_product_data(conn, ean, data, None)
        conn.commit()
        return "SEM_IMAGEM", None

    if dry_run:
        _save_consulta(conn, ean, "dry_found", http_status=http_status, data=data, storage_url=thumbnail, token_id=token_item["id"])
        conn.commit()
        return "DRY_OK", thumbnail

    raw, ext, content_type = _download_image_for_storage(thumbnail)
    if not raw:
        _save_consulta(conn, ean, "download_error", http_status=http_status, data=data, erro="Falha ao baixar thumbnail", token_id=token_item["id"])
        conn.commit()
        return "DOWNLOAD_ERRO", None

    storage_url, storage_provider = _upload_product_image(raw, ean, ext, content_type)
    if not storage_url:
        _save_consulta(conn, ean, "upload_error", http_status=http_status, data=data, erro="Falha no upload Cloudinary/Supabase", token_id=token_item["id"])
        conn.commit()
        return "UPLOAD_ERRO", None

    _save_product_data(conn, ean, data, storage_url)
    _save_consulta(conn, ean, "found", http_status=http_status, data=data, storage_url=storage_url, token_id=token_item["id"])
    conn.commit()
    return f"OK_{storage_provider.upper()}", storage_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Maximo total de consultas nesta execucao")
    parser.add_argument("--per-token-limit", type=int, default=DEFAULT_PER_TOKEN_LIMIT, help="Maximo diario por token Cosmos")
    parser.add_argument("--dry-run", action="store_true", help="Consulta e registra, mas nao baixa/salva imagens finais")
    parser.add_argument("--force", action="store_true", help="Ignora historico de misses recentes")
    parser.add_argument("--retry-days", type=int, default=RETRY_MISS_DAYS, help="Dias para tentar novamente EAN sem imagem")
    parser.add_argument("--sleep", type=float, default=1.0, help="Pausa entre consultas")
    args = parser.parse_args()

    conn = db()
    _ensure_schema(conn)
    tokens = _load_tokens()

    remaining_by_tokens = _remaining_today(conn, tokens, args.per_token_limit)
    remaining = min(remaining_by_tokens, args.limit) if args.limit else remaining_by_tokens
    print("=" * 60)
    print("Cosmos imagens diario")
    print("=" * 60)
    print(f"  tokens ativos    : {len(tokens)}")
    print(f"  cota por token   : {args.per_token_limit}/dia")
    print(f"  cota total hoje  : {len(tokens) * args.per_token_limit}")
    print(f"  restantes hoje   : {remaining}")
    print(f"  dry-run          : {'sim' if args.dry_run else 'nao'}")

    if not tokens:
        print("ERRO: configure COSMOS_TOKEN ou COSMOS_TOKENS no .env")
        return 2
    if remaining <= 0:
        print("Nada a fazer: limite diario ja atingido.")
        return 0

    rows = _candidate_rows(conn, remaining, force=args.force, retry_days=args.retry_days)
    if not rows:
        print("Nenhum EAN candidato sem imagem encontrado.")
        return 0

    ok = miss = erro = 0
    processed_eans = set()
    for idx, row in enumerate(rows, 1):
        ean = row["ean"]
        if ean in processed_eans:
            continue
        processed_eans.add(ean)
        nome = (row.get("nome") or "")[:55]
        while True:
            available = _available_tokens(conn, tokens, args.per_token_limit)
            if not available:
                status, url = "SEM_TOKEN", None
                break
            token_item = available[0]
            try:
                status, url = process_one(conn, row, token_item, dry_run=args.dry_run)
            except Exception as exc:
                _save_consulta(conn, ean, "exception", erro=str(exc)[:500], token_id=token_item["id"])
                conn.commit()
                status, url = "EXCEPTION", None
            if status == "RATE_LIMIT":
                print(f"[{idx:02}/{len(rows):02}] RATE_LIMIT    {ean} {nome} token={token_item['id']}")
                continue
            break

        if status.startswith("OK") or status == "DRY_OK":
            ok += 1
        elif status in ("MISS", "SEM_IMAGEM"):
            miss += 1
        else:
            erro += 1

        print(f"[{idx:02}/{len(rows):02}] {status:13} {ean} {nome}")
        if url:
            print(f"              {url}")
        if status == "SEM_TOKEN":
            print("Todos os tokens Cosmos atingiram a cota de hoje. Encerrando.")
            break
        time.sleep(max(0.0, args.sleep))

    print("-" * 60)
    print(f"Finalizado: ok={ok} sem_imagem/nao_encontrado={miss} erros={erro} em {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
