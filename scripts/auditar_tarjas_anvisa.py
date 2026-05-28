import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import (  # noqa: E402
    _NOME_RECEITA_RETIDA_RE,
    _TARJA_VERMELHA_RE,
    _anvisa_schema,
    db,
)


BLACK_STRONG_RE = re.compile(
    r"tarja\s+preta"
    r"|notifica[cç][aã]o\s+de\s+receita\s+[ab]"
    r"|\blista\s+(?:a1|a2|a3|b1|b2)\b",
    re.IGNORECASE,
)

KNOWN_BLACK_RE = re.compile(
    r"\bnitrazepam\b|\bclonazepam\b|\balprazolam\b|\bdiazepam\b|\blorazepam\b"
    r"|\bbromazepam\b|\bzolpidem\b|\bzopiclona\b|\bmidazolam\b"
    r"|\bmetilfenidato\b|\blisdexanfetamina\b|\bmorfina\b|\bmetadona\b|\boxicodona\b",
    re.IGNORECASE,
)

RED_STRONG_RE = re.compile(
    r"tarja\s+vermelha"
    r"|venda\s+sob\s+prescri[cç][aã]o"
    r"|uso\s+sob\s+prescri[cç][aã]o"
    r"|receita\s+de\s+controle\s+especial"
    r"|controle\s+especial"
    r"|reten[cç][aã]o\s+(?:de|da)\s+receita"
    r"|antimicrobian[oa]s?",
    re.IGNORECASE,
)


def _blob(row):
    return " ".join(
        str(row.get(k) or "")
        for k in (
            "chave",
            "nome_anvisa",
            "principio_ativo",
            "tarja",
            "dizeres_receita",
            "dizeres_imagem",
            "alertas",
            "como_usar",
        )
    )


def _identity_blob(row):
    return " ".join(
        str(row.get(k) or "")
        for k in (
            "chave",
            "nome_anvisa",
            "principio_ativo",
            "tarja",
            "dizeres_receita",
            "dizeres_imagem",
        )
    )


def _classificar_local(row):
    text = _blob(row)
    identity = _identity_blob(row)
    current = (row.get("tarja") or "").strip().lower()
    if BLACK_STRONG_RE.search(identity) or KNOWN_BLACK_RE.search(identity):
        return "preta", "sinal forte de tarja preta"
    if (
        current == "vermelha"
        or row.get("receita_retida") is True
        or RED_STRONG_RE.search(text)
        or _TARJA_VERMELHA_RE.search(text)
        or _NOME_RECEITA_RETIDA_RE.search(text)
    ):
        return "vermelha", "prescricao/retencao sem sinal forte de tarja preta"
    if current in ("preta", "vermelha"):
        return current, "mantido por ausencia de evidencia melhor"
    return None, "sem evidencia de tarja"


def _claude_classificar(row):
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    prompt = (
        "Classifique a tarja sanitaria do medicamento brasileiro abaixo. "
        "Responda somente JSON: "
        '{"tarja":"preta|vermelha|null","receita_retida":true|false|null,"motivo":"curto"}. '
        "Use tarja preta apenas quando houver evidencia forte de tarja preta/notificacao A/B/listas A/B "
        "ou psicotropico/entorpecente correspondente. Controle especial sozinho pode ser tarja vermelha.\n\n"
        + json.dumps(row, ensure_ascii=False, default=str)[:6000]
    )
    body = json.dumps(
        {
            "model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            "max_tokens": 300,
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
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
        parsed = json.loads(text.strip())
        tarja = parsed.get("tarja")
        if tarja in ("preta", "vermelha") or tarja is None:
            return tarja, parsed.get("motivo") or "claude"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
        print(f"[claude erro] {row.get('chave')}: {exc}")
    return None


def main():
    parser = argparse.ArgumentParser(description="Audita e corrige tarja preta/vermelha no anvisa_cache.")
    parser.add_argument("--apply", action="store_true", help="grava as correcoes no banco")
    parser.add_argument("--use-claude", action="store_true", help="usa Claude para casos ambiguos")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--chave")
    args = parser.parse_args()

    _anvisa_schema()
    conn = db()
    cur = conn.cursor()
    where = "encontrado=TRUE AND (LOWER(COALESCE(tarja,'')) IN ('preta','vermelha') OR receita_retida IS TRUE)"
    params = []
    if args.chave:
        where += " AND chave ILIKE %s"
        params.append(args.chave)
    sql = f"""
        SELECT chave, nome_anvisa, principio_ativo, tarja, receita_retida,
               dizeres_receita, dizeres_imagem, alertas, como_usar
        FROM anvisa_cache
        WHERE {where}
        ORDER BY chave
    """
    if args.limit:
        sql += " LIMIT %s"
        params.append(args.limit)
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]

    mudancas = []
    for row in rows:
        nova, motivo = _classificar_local(row)
        atual = (row.get("tarja") or "").strip().lower() or None
        if args.use_claude and (atual == "preta" and nova != "preta"):
            claude = _claude_classificar(row)
            if claude is not None:
                nova, motivo = claude
        if nova != atual:
            mudancas.append((row["chave"], atual, nova, motivo))
            print(f"[corrigir] {row['chave']}: {atual} -> {nova} ({motivo})")

    if args.apply and mudancas:
        for chave, _atual, nova, _motivo in mudancas:
            cur.execute("UPDATE anvisa_cache SET tarja=%s WHERE chave=%s", (nova, chave))
        conn.commit()
    else:
        conn.rollback()

    print(f"linhas analisadas: {len(rows)}")
    print(f"mudancas {'gravadas' if args.apply else 'em dry-run'}: {len(mudancas)}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
