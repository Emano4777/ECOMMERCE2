"""
buscar_imagens_cosmeticos.py — Busca imagens de cosméticos, higiene e puericultura
via DSP (VTEX) e Droga Raia por EAN, salvando em produto_canon.

Como produto_canon é por EAN (não por loja), uma imagem encontrada aqui
beneficia automaticamente TODAS as lojas que têm aquele EAN em estoque.

Categorias cobertas (não estão no anvisa_cache):
  cosmeticos  — shampoo, condicionador, hidratante, esmalte, maquiagem, perfume
  higiene     — sabonete, desodorante, absorvente, preservativo, protetor solar
  puericultura — fralda, mamadeira, chupeta, lenço umedecido, baby

Uso:
    py scripts/buscar_imagens_cosmeticos.py               # dry-run, 100 EANs, todas cats
    py scripts/buscar_imagens_cosmeticos.py --apply       # grava
    py scripts/buscar_imagens_cosmeticos.py --limite 50
    py scripts/buscar_imagens_cosmeticos.py --ean 7891150037465
    py scripts/buscar_imagens_cosmeticos.py --categoria puericultura
    py scripts/buscar_imagens_cosmeticos.py -v
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from app import db
from scripts.revalidar_tarja_vtex import (
    _vtex_fetch,
    _raia_fetch,
    _extrair_dados_vtex,
    _upload_cloudinary,
    _PLACEHOLDERS_POUPAQUI,
)

# ── Palavras-chave por categoria ──────────────────────────────────────────────

_KEYWORDS: dict[str, list[str]] = {
    "cosmeticos": [
        "shampoo", " sh ", "condicionador", " cond ", "creme pent",
        "leave-in", "leave in", "mascara trat", "mascara capilar",
        "serum", "sérum", "elseve", "seda ", "pantene", "garnier",
        "siage", "tresemme", "loreal", "l oreal", "wella", "keune",
        "hidratante", "creme facial", "creme corp", "loção", "locao",
        "sabonete liq", "cicatricure", "nivea", "dove ", "giovanna",
        "natura ", "boticario", "avon ", "esmalte", "maquiagem",
        "batom", "blush", "rimel", "mascara olho", "perfume",
        "colonia", "agua de colonia", "oil body",
    ],
    "higiene": [
        "sabonete barra", "sabonete gel", "desodorante", "des rexona",
        "des nivea", "des dove", "des gilette", "absorvente", "absorv ",
        "preserv", "camisinha", "fio dental", "escova dent",
        "creme dent", "pasta dent", "enxaguante", "antisseptico buc",
        "algodao", "curativo", "band-aid", "repelente", "protetor solar",
        "fps", "protetor labial", "depilatorio", "depilatório",
        "barbeador", "aparelho barb", "lamina barb",
    ],
    "puericultura": [
        "fralda", "lenco umed", "lenço umed", "mamadeira", "chupeta",
        "buba ", "mam ", "huggies", "pampers", "baby ", "bebê ", "bebe ",
        "recem nasc", "recém nasc", "pomada assad", "talco infantil",
        "aspirador nasal", "termometro", "termômetro",
    ],
}

_ALL_KEYWORDS = [kw for kws in _KEYWORDS.values() for kw in kws]

_SEM_IMAGEM_COND = """(
    pc.ean IS NULL
    OR pc.imagem_cosmos IS NULL
    OR TRIM(pc.imagem_cosmos) = ''
    OR pc.imagem_cosmos LIKE '%%CAIXA_GEN%%'
    OR pc.imagem_cosmos LIKE '%%ChatGPT_Image%%'
)"""


def _buscar_eans(cur, categoria: str | None, limite: int, ean_filtro: str | None) -> list[dict]:
    if ean_filtro:
        cur.execute("""
            SELECT ean, nome FROM (
                SELECT DISTINCT COALESCE(e.barras_norm, e.barras) AS ean,
                       e.descricao AS nome
                FROM estoque e
                WHERE COALESCE(e.barras_norm, e.barras) = %s AND e.estoque > 0
                UNION
                SELECT DISTINCT ae.ean, ae.descricao_produto AS nome
                FROM automatiza_estoque ae
                WHERE ae.ean = %s AND ae.quantidade_estoque > 0
            ) t LIMIT 1
        """, (ean_filtro, ean_filtro))
        rows = cur.fetchall()
        return [dict(r) for r in rows] if rows else [{"ean": ean_filtro, "nome": ean_filtro}]

    keywords = _KEYWORDS.get(categoria, _ALL_KEYWORDS) if categoria else _ALL_KEYWORDS
    ilike_e  = " OR ".join(["e.descricao ILIKE %s"]           * len(keywords))
    ilike_ae = " OR ".join(["ae.descricao_produto ILIKE %s"]  * len(keywords))
    params   = [f"%{kw}%" for kw in keywords]

    cur.execute(f"""
        SELECT DISTINCT ON (ean) ean, nome
        FROM (
            SELECT
                COALESCE(e.barras_norm, e.barras) AS ean,
                e.descricao AS nome
            FROM estoque e
            LEFT JOIN produto_canon pc ON pc.ean = COALESCE(e.barras_norm, e.barras)
            WHERE COALESCE(e.barras_norm, e.barras) IS NOT NULL
              AND COALESCE(e.barras_norm, e.barras) ~ '^[1-9][0-9]{{7,12}}$'
              AND e.estoque > 0
              AND ({ilike_e})
              AND {_SEM_IMAGEM_COND}

            UNION

            SELECT
                ae.ean,
                ae.descricao_produto AS nome
            FROM automatiza_estoque ae
            LEFT JOIN produto_canon pc ON pc.ean = ae.ean
            WHERE ae.ean IS NOT NULL
              AND ae.ean ~ '^[1-9][0-9]{{7,12}}$'
              AND ae.quantidade_estoque > 0
              AND ({ilike_ae})
              AND {_SEM_IMAGEM_COND}
        ) t
        ORDER BY ean
        LIMIT %s
    """, params + params + [limite])

    return [dict(r) for r in cur.fetchall()]


def main():
    parser = argparse.ArgumentParser(
        description="Busca imagens de cosméticos/higiene/puericultura via DSP e Raia."
    )
    parser.add_argument("--apply",     action="store_true", help="Grava no banco + upload Cloudinary")
    parser.add_argument("--limite",    type=int, default=100, metavar="N")
    parser.add_argument("--ean",       help="Processa apenas este EAN")
    parser.add_argument("--categoria", choices=list(_KEYWORDS.keys()),
                        help="Filtra por categoria (padrão: todas)")
    parser.add_argument("--delay",     type=float, default=0.8, metavar="S")
    parser.add_argument("--commit-cada", type=int, default=50, metavar="N")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    conn = db()
    cur  = conn.cursor()

    registros = _buscar_eans(cur, args.categoria, args.limite, args.ean)
    cat_label = args.categoria or "todas"
    print(f"EANs a processar [{cat_label}]: {len(registros)}")
    if not registros:
        print("Nenhum EAN encontrado.")
        cur.close(); conn.close(); return

    n_imagem = n_sem_resposta = n_branded = 0
    pendentes_commit = 0

    for reg in registros:
        ean  = reg["ean"]
        nome = (reg.get("nome") or ean)[:70]

        if args.verbose:
            print(f"\n[{ean}] {nome}")

        # ── Consulta DSP ──────────────────────────────────────────────────
        produto_vtex = _vtex_fetch(ean, verbose=args.verbose)
        dados_dsp    = _extrair_dados_vtex(produto_vtex, verbose=args.verbose) if produto_vtex else None
        imagem_dsp   = (dados_dsp or {}).get("imagem")
        exibir_dsp   = (dados_dsp or {}).get("exibir_imagem")

        # ── Consulta Raia (quando DSP não trouxe imagem real) ─────────────
        dados_raia  = None
        imagem_raia = None
        if not imagem_dsp and exibir_dsp is not False:
            time.sleep(args.delay * 0.4)
            dados_raia  = _raia_fetch(ean, verbose=args.verbose)
            imagem_raia = (dados_raia or {}).get("imagem")

        imagem = imagem_dsp or imagem_raia

        if not imagem:
            if exibir_dsp is False or (dados_raia and dados_raia.get("exibir_imagem") is False):
                n_branded += 1
                if args.verbose:
                    print("  [branded] farmácias mostram imagem de template, pulando")
            else:
                n_sem_resposta += 1
                if args.verbose:
                    print("  [sem imagem] não encontrado em DSP nem Raia")
            time.sleep(args.delay)
            continue

        fonte = "vtex_dsp" if imagem_dsp else "vtex_raia"
        print(f"  [imagem {fonte}] {ean}  {nome[:40]}  {imagem[:65]}...")
        n_imagem += 1

        if args.apply:
            url_final      = imagem
            url_cloudinary = _upload_cloudinary(imagem, ean, verbose=args.verbose)
            if url_cloudinary:
                url_final = url_cloudinary
                if args.verbose:
                    print(f"  [cloudinary OK] {url_cloudinary[:70]}...")
            else:
                print(f"  [cloudinary FALHOU] usando URL original")

            placeholders = list(_PLACEHOLDERS_POUPAQUI)

            cur.execute("""
                UPDATE produto_canon
                   SET imagem_cosmos = %s,
                       fonte         = %s,
                       atualizado_em = NOW()
                WHERE ean = %s
                  AND (
                    imagem_cosmos IS NULL
                    OR TRIM(imagem_cosmos) = ''
                    OR imagem_cosmos LIKE '%%CAIXA_GEN%%'
                    OR imagem_cosmos LIKE '%%ChatGPT_Image%%'
                  )
            """, (url_final, fonte, ean))

            if cur.rowcount == 0:
                cur.execute("""
                    INSERT INTO produto_canon (ean, descricao_canon, imagem_cosmos, fonte, atualizado_em)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (ean) DO UPDATE
                      SET imagem_cosmos = EXCLUDED.imagem_cosmos,
                          fonte         = EXCLUDED.fonte,
                          atualizado_em = NOW()
                    WHERE produto_canon.imagem_cosmos IS NULL
                       OR TRIM(produto_canon.imagem_cosmos) = ''
                       OR produto_canon.imagem_cosmos LIKE '%%CAIXA_GEN%%'
                       OR produto_canon.imagem_cosmos LIKE '%%ChatGPT_Image%%'
                """, (ean, nome, url_final, fonte))

            pendentes_commit += 1
            if pendentes_commit >= args.commit_cada:
                conn.commit()
                print(f"  [commit parcial] {pendentes_commit} gravadas")
                pendentes_commit = 0

        time.sleep(args.delay)

    # Commit final
    if args.apply:
        if pendentes_commit > 0:
            conn.commit()
        print(f"\nGravado." if n_imagem else "\nNenhuma imagem nova encontrada.")
    else:
        conn.rollback()
        if n_imagem:
            print(f"\nDry-run: {n_imagem} imagem(ns) encontrada(s) — use --apply para gravar.")
        else:
            print("\nNenhuma imagem nova encontrada.")

    print(
        f"\nTotal: {len(registros)} | Com imagem: {n_imagem} "
        f"| Branded: {n_branded} | Sem resposta: {n_sem_resposta}"
    )
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
