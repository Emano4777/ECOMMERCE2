"""
limpar_imagens_branded_cloudinary.py

Varredura no Cloudinary para encontrar e remover imagens branded de concorrentes
(templates "VENDA SOB PRESCRICAO MEDICA" + logo da loja).

Logica de identificacao:
  - Lista ativos da pasta catalogo_produtos/ no Cloudinary
  - O public_id tem o EAN embutido (ex: catalogo_produtos/7891234567)
  - Para EANs com anvisa_cache.exibir_imagem_publica = FALSE ou tarja nao-nula,
    a imagem NO cloudinary e provavelmente o template branded do concorrente
    (nesses casos o app usa nosso placeholder Poupaqui, nao uma imagem real)
  - Deleta do Cloudinary e limpa produto_canon

Uso:
    py scripts/limpar_imagens_branded_cloudinary.py        # dry-run
    py scripts/limpar_imagens_branded_cloudinary.py --apply  # apaga de verdade
    py scripts/limpar_imagens_branded_cloudinary.py --folder catalogo_produtos
"""
import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from app import db, _anvisa_schema  # noqa: E402


def _init_cloudinary():
    try:
        import cloudinary, cloudinary.api
        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME", ""),
            api_key=os.getenv("CLOUDINARY_API_KEY", ""),
            api_secret=os.getenv("CLOUDINARY_API_SECRET", ""),
        )
        return cloudinary.api
    except ImportError:
        print("ERRO: modulo cloudinary nao instalado")
        return None


def _listar_assets(api, folder: str) -> list[dict]:
    """Lista todos os assets de uma pasta no Cloudinary (pagina por pagina)."""
    assets = []
    next_cursor = None
    while True:
        kwargs = {"type": "upload", "prefix": folder + "/", "max_results": 500, "resource_type": "image"}
        if next_cursor:
            kwargs["next_cursor"] = next_cursor
        result = api.resources(**kwargs)
        assets.extend(result.get("resources", []))
        next_cursor = result.get("next_cursor")
        print(f"  ... {len(assets)} assets carregados", end="\r")
        if not next_cursor:
            break
    print()
    return assets


def _ean_do_public_id(public_id: str) -> str | None:
    """Extrai EAN numerico do public_id. Ex: 'catalogo_produtos/7891234567' -> '7891234567'."""
    parte = public_id.split("/")[-1]
    digits = re.sub(r"\D", "", parte)
    return digits if len(digits) >= 7 else None


def main():
    parser = argparse.ArgumentParser(
        description="Remove imagens branded de concorrentes do Cloudinary e produto_canon."
    )
    parser.add_argument("--apply",  action="store_true", help="Apaga de verdade")
    parser.add_argument("--folder", default="catalogo_produtos,produtos", help="Pastas no Cloudinary (separadas por virgula)")
    args = parser.parse_args()

    api = _init_cloudinary()
    if not api:
        return

    _anvisa_schema()
    conn = db()
    cur = conn.cursor()

    # 1. Lista assets do Cloudinary (todas as pastas)
    folders = [f.strip() for f in args.folder.split(",") if f.strip()]
    assets = []
    for folder in folders:
        print(f"Listando assets em '{folder}/'...")
        folder_assets = _listar_assets(api, folder)
        print(f"  -> {len(folder_assets)} assets")
        assets.extend(folder_assets)
    print(f"Total de assets: {len(assets)}")

    if not assets:
        print("Pasta vazia ou nao existe.")
        cur.close(); conn.close(); return

    # 2. Extrai EANs dos public_ids
    ean_para_asset = {}
    for a in assets:
        ean = _ean_do_public_id(a["public_id"])
        if ean:
            ean_para_asset[ean] = a

    print(f"EANs reconhecidos nos public_ids: {len(ean_para_asset)}")

    # 3. Verifica no anvisa_cache quais sao tarjados/bloqueados
    eans_lista = list(ean_para_asset.keys())

    # Busca via chave derivada do nome (anvisa_cache usa chave, nao EAN diretamente)
    from app import _anvisa_chave

    cur.execute("""
        SELECT COALESCE(m.barra_norm, e.barras) AS ean,
               COALESCE(m.descricao, e.descricao) AS nome
        FROM estoque e
        LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
        WHERE COALESCE(e.barras_norm, e.barras) = ANY(%s)
        UNION
        SELECT COALESCE(m.barra_norm, ae.ean),
               COALESCE(m.descricao, ae.descricao_produto)
        FROM automatiza_estoque ae
        LEFT JOIN medicamentos m ON m.barra_norm = ae.ean
        WHERE ae.ean = ANY(%s)
    """, (eans_lista, eans_lista))
    ean_nome = {r["ean"]: r["nome"] for r in cur.fetchall() if r["ean"]}

    # Monta set de chaves ANVISA bloqueadas
    cur.execute("""
        SELECT chave FROM anvisa_cache
        WHERE encontrado = TRUE
          AND (
            exibir_imagem_publica = FALSE
            OR (
              tarja IN ('preta', 'vermelha')
              AND (exibir_imagem_publica IS NULL OR exibir_imagem_publica = FALSE)
            )
          )
    """)
    chaves_bloqueadas = {r["chave"] for r in cur.fetchall()}

    # 4. Identifica assets a apagar
    para_apagar = []
    for ean, asset in ean_para_asset.items():
        nome = ean_nome.get(ean, "")
        chave = _anvisa_chave(nome) if nome else ""
        if chave in chaves_bloqueadas:
            para_apagar.append({
                "ean": ean,
                "public_id": asset["public_id"],
                "url": asset.get("secure_url", ""),
                "chave": chave,
            })

    print(f"\nBrandeds/bloqueados a remover: {len(para_apagar)}")
    if not para_apagar:
        print("Nenhuma imagem branded identificada.")
        cur.close(); conn.close(); return

    for item in para_apagar[:20]:  # mostra os primeiros 20
        print(f"  {item['public_id']}  [{item['chave']}]")
    if len(para_apagar) > 20:
        print(f"  ... e mais {len(para_apagar) - 20}")

    if not args.apply:
        print(f"\nDry-run — use --apply para apagar {len(para_apagar)} assets")
        cur.close(); conn.close(); return

    # 5. Apaga em lotes de 100 (limite da API Cloudinary)
    apagados = 0
    erros = 0
    for i in range(0, len(para_apagar), 100):
        lote = para_apagar[i:i+100]
        public_ids = [x["public_id"] for x in lote]
        try:
            result = api.delete_resources(public_ids)
            deleted = result.get("deleted", {})
            ok = sum(1 for v in deleted.values() if v == "deleted")
            apagados += ok
            erros += len(lote) - ok
            print(f"  Lote {i//100 + 1}: {ok} apagados")
        except Exception as exc:
            print(f"  Lote {i//100 + 1}: ERRO {exc}")
            erros += len(lote)

    # 6. Limpa produto_canon para esses EANs
    eans_apagados = [x["ean"] for x in para_apagar]
    # Busca URLs cloudinary desses EANs no banco
    cur.execute("""
        UPDATE produto_canon
           SET imagem_cosmos = NULL, fonte = NULL
        WHERE ean = ANY(%s::text[])
          AND imagem_cosmos ILIKE '%%cloudinary%%'
    """, (eans_apagados,))
    banco_limpos = cur.rowcount
    conn.commit()

    print(f"\nCloudinary apagados: {apagados} | Erros: {erros}")
    print(f"Banco limpos: {banco_limpos}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
