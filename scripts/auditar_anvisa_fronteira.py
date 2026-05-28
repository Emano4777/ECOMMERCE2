import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import anvisa_sync as sync  # noqa: E402
from app import _anvisa_schema, db  # noqa: E402


FORTE_MED_RE = re.compile(
    r"\b\d+\s*(mg|mcg|ui)\b|comprim|caps|cpr\b|drg\b|amp\b|injet|xarope|"
    r"pomada|creme|gel\b|col[ií]rio|gotas|suspens[aã]o|solu[cç][aã]o|"
    r"\bcloridrato\b|\bmaleato\b|\bacetato\b|\bsulfato\b|\bbesilato\b",
    re.IGNORECASE,
)


def _claude_boundary(row):
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    prompt = (
        "Decida se este item de ecommerce de farmacia deveria estar no cache ANVISA de medicamentos. "
        "Responda SOMENTE JSON: "
        '{"deveria_ser_anvisa":true/false,"motivo":"curto","chave_melhor":"texto ou null"}.\n'
        "Medicamentos e OTC registrados no bulario devem ser true. Perfumaria, cosmetico, suplemento, alimento, "
        "acessorio, taxa e material hospitalar sem bula devem ser false.\n\n"
        f"{json.dumps(row, ensure_ascii=False, default=str)[:3000]}"
    )
    body = json.dumps({
        "model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        "max_tokens": 240,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
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
    with urllib.request.urlopen(req, timeout=35) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
    return json.loads(text.strip())


def _catalogo_por_chave():
    conn = db()
    cur = conn.cursor()
    cur.execute(sync._catalogo_sql(False))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    by_key = {}
    for row in rows:
        nome = row.get("nome") or ""
        tipo = (row.get("tipo_ia") or "").strip().lower()
        principio = (row.get("principio_ativo_ia") or "").strip()
        chave_base = principio if tipo in sync._TIPOS_ANVISA_MED and principio else nome
        chave = sync._chave(chave_base)
        if not chave:
            continue
        bucket = by_key.setdefault(chave, {"nomes": set(), "eans": set(), "tipos": set(), "forte_med": False, "nao_anvisa": False})
        bucket["nomes"].add(nome)
        bucket["eans"].add(row.get("ean"))
        if tipo:
            bucket["tipos"].add(tipo)
        bucket["forte_med"] = bucket["forte_med"] or bool(FORTE_MED_RE.search(nome)) or tipo in sync._TIPOS_ANVISA_MED
        bucket["nao_anvisa"] = bucket["nao_anvisa"] or sync._ignorar_catalogo_anvisa(nome) or tipo in sync._TIPOS_NAO_ANVISA
    return by_key


def main():
    parser = argparse.ArgumentParser(description="Audita falso negativo/positivo do anvisa_cache.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--use-claude", action="store_true")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    _anvisa_schema()
    catalogo = _catalogo_por_chave()
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT chave, encontrado, nome_anvisa, principio_ativo, tarja
        FROM anvisa_cache
        WHERE chave = ANY(%s)
    """, (list(catalogo.keys()),))
    cache = {r["chave"]: dict(r) for r in cur.fetchall()}

    suspeitos = []
    for chave, meta in catalogo.items():
        row = cache.get(chave)
        if not row:
            continue
        if row.get("encontrado") is False and meta["forte_med"] and not meta["nao_anvisa"]:
            suspeitos.append(("falso_negativo", chave, row, meta))
        elif row.get("encontrado") is True and meta["nao_anvisa"] and not meta["forte_med"]:
            suspeitos.append(("falso_positivo", chave, row, meta))

    suspeitos = suspeitos[: max(0, args.limit)]
    aplicar_false_neg = []
    aplicar_false_pos = []
    for tipo, chave, row, meta in suspeitos:
        decisao = None
        if args.use_claude:
            sample = {
                "tipo_suspeita": tipo,
                "chave": chave,
                "cache": row,
                "nomes_exemplo": sorted(meta["nomes"])[:6],
                "tipos": sorted(meta["tipos"]),
            }
            try:
                decisao = _claude_boundary(sample)
            except Exception as exc:
                print(f"[erro claude] {chave}: {exc}")
        if decisao:
            deveria = decisao.get("deveria_ser_anvisa")
            print(f"[{tipo}] {chave} claude={deveria} motivo={decisao.get('motivo')} melhor={decisao.get('chave_melhor')}")
            if tipo == "falso_negativo" and deveria is True:
                aplicar_false_neg.append(chave)
            if tipo == "falso_positivo" and deveria is False:
                aplicar_false_pos.append(chave)
        else:
            print(f"[{tipo}] {chave} nomes={sorted(meta['nomes'])[:3]} cache={row}")

    if args.apply:
        for chave in aplicar_false_neg:
            cur.execute("DELETE FROM anvisa_cache WHERE chave=%s AND encontrado=FALSE", (chave,))
        for chave in aplicar_false_pos:
            cur.execute("""
                UPDATE anvisa_cache
                   SET encontrado=FALSE, nome_anvisa=NULL, principio_ativo=NULL, tarja=NULL,
                       receita_retida=NULL, venda_online_permitida=NULL, exibir_imagem_publica=NULL,
                       dizeres_receita=NULL, dizeres_imagem=NULL, criado_em=NOW()
                 WHERE chave=%s
            """, (chave,))
        conn.commit()
    else:
        conn.rollback()
    print(f"suspeitos analisados: {len(suspeitos)}")
    print(f"false negatives para rebusca: {len(aplicar_false_neg)}")
    print(f"false positives para limpar: {len(aplicar_false_pos)}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
