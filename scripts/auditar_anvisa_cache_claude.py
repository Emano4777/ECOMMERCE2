import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import _anvisa_schema, db  # noqa: E402


CAMPOS_ATUALIZAVEIS = (
    "tarja",
    "receita_retida",
    "venda_online_permitida",
    "exibir_imagem_publica",
    "dizeres_receita",
    "dizeres_imagem",
)


def _load_chaves(path):
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def _json_default(value):
    return str(value)


def _claude_review(row):
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY ausente")

    payload_row = {
        "chave": row.get("chave"),
        "nome_anvisa": row.get("nome_anvisa"),
        "principio_ativo": row.get("principio_ativo"),
        "situacao": row.get("situacao"),
        "tarja": row.get("tarja"),
        "receita_retida": row.get("receita_retida"),
        "venda_online_permitida": row.get("venda_online_permitida"),
        "exibir_imagem_publica": row.get("exibir_imagem_publica"),
        "dizeres_receita": row.get("dizeres_receita"),
        "dizeres_imagem": row.get("dizeres_imagem"),
        "serve_para": row.get("serve_para"),
        "como_usar": row.get("como_usar"),
        "alertas": row.get("alertas"),
    }
    prompt = (
        "Revise este registro do cache ANVISA de um ecommerce de farmacia brasileiro. "
        "Use apenas as evidencias no JSON; nao invente bula nem indicacao. "
        "Corrija somente erro claro ou incoerencia sanitaria.\n\n"
        "Regras:\n"
        "- tarja deve ser 'preta', 'vermelha' ou null. Nao classifique como preta apenas por 'controle especial' solto; "
        "tarja preta exige evidencia forte de tarja preta/lista A ou B/notificacao A ou B/psicotropico claro.\n"
        "- receita_retida true quando houver retencao/controle/notificacao/antimicrobiano; false se for prescricao simples; null se incerto.\n"
        "- venda_online_permitida false quando a propria informacao indicar proibicao/restricao incompatível com ecommerce; "
        "true se claramente permitido; null se incerto.\n"
        "- exibir_imagem_publica false quando houver restricao de imagem/embalagem ou quando a tarja exigir placeholder; "
        "true se imagem publica for permitida; null se incerto.\n"
        "- dizeres_receita e dizeres_imagem devem ser frases curtas, objetivas e coerentes com os campos; mantenha null se nao houver base.\n\n"
        "Responda SOMENTE JSON valido neste formato:\n"
        '{"tarja":"preta|vermelha|null","receita_retida":true/false/null,'
        '"venda_online_permitida":true/false/null,"exibir_imagem_publica":true/false/null,'
        '"dizeres_receita":"texto ou null","dizeres_imagem":"texto ou null",'
        '"confianca":"alta|media|baixa","problemas":["..."]}\n\n'
        f"Registro:\n{json.dumps(payload_row, ensure_ascii=False, default=_json_default)[:12000]}"
    )
    body = json.dumps(
        {
            "model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            "max_tokens": 700,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
    return json.loads(text.strip())


def _normalize_review(review):
    out = {}
    tarja = review.get("tarja")
    out["tarja"] = tarja if tarja in ("preta", "vermelha") else None
    for key in ("receita_retida", "venda_online_permitida", "exibir_imagem_publica"):
        out[key] = review.get(key) if isinstance(review.get(key), bool) else None
    for key in ("dizeres_receita", "dizeres_imagem"):
        val = review.get(key)
        out[key] = val.strip()[:500] if isinstance(val, str) and val.strip() else None
    return out


def main():
    parser = argparse.ArgumentParser(description="Audita registros anvisa_cache com Claude.")
    parser.add_argument("--apply", action="store_true", help="grava correcoes no banco")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--recent-days", type=int, default=0)
    parser.add_argument("--chaves-file")
    args = parser.parse_args()

    _anvisa_schema()
    chaves = _load_chaves(args.chaves_file)
    conn = db()
    cur = conn.cursor()
    where = ["encontrado=TRUE"]
    params = []
    if chaves:
        where.append("chave = ANY(%s)")
        params.append(chaves)
    elif args.recent_days:
        where.append("criado_em >= NOW() - (%s || ' days')::interval")
        params.append(args.recent_days)
    sql = f"""
        SELECT chave, nome_anvisa, principio_ativo, situacao, tarja, receita_retida,
               venda_online_permitida, exibir_imagem_publica, dizeres_receita,
               dizeres_imagem, serve_para, como_usar, alertas
        FROM anvisa_cache
        WHERE {' AND '.join(where)}
        ORDER BY criado_em DESC
    """
    if args.limit:
        sql += " LIMIT %s"
        params.append(args.limit)
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]

    mudancas = []
    for idx, row in enumerate(rows, 1):
        try:
            review_raw = _claude_review(row)
            review = _normalize_review(review_raw)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            print(f"[{idx}/{len(rows)}] ERRO {row.get('chave')}: {exc}")
            continue

        changed = {
            key: review[key]
            for key in CAMPOS_ATUALIZAVEIS
            if review.get(key) != row.get(key)
        }
        problemas = review_raw.get("problemas") or []
        confianca = review_raw.get("confianca") or ""
        if changed:
            mudancas.append((row["chave"], changed))
            print(f"[{idx}/{len(rows)}] corrigir {row['chave']} conf={confianca} campos={list(changed)} problemas={problemas}")
        else:
            print(f"[{idx}/{len(rows)}] ok {row['chave']} conf={confianca}")

    if args.apply and mudancas:
        for chave, changed in mudancas:
            sets = ", ".join([f"{key}=%s" for key in changed])
            values = list(changed.values()) + [chave]
            cur.execute(f"UPDATE anvisa_cache SET {sets}, criado_em=NOW() WHERE chave=%s", values)
        conn.commit()
    else:
        conn.rollback()

    print(f"linhas analisadas: {len(rows)}")
    print(f"mudancas {'gravadas' if args.apply else 'em dry-run'}: {len(mudancas)}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
