"""
Restaura SOMENTE os produtos onde:
  - produto_canon.imagem_cosmos IS NULL  (foi apagado)
  - anvisa_cache.exibir_imagem_publica = FALSE  (DSP confirmou que e tarjado)

Nesses casos seta imagem_cosmos com o placeholder Poupaqui correto.
NAO toca em mais nada.
"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
import psycopg2, psycopg2.extras

VERMELHA = "https://res.cloudinary.com/dizfq460q/image/upload/v1778783063/CAIXA_GEN%C3%89RICO_-_POUPAQUI_itiyth.jpg"
PRETA    = "https://res.cloudinary.com/dizfq460q/image/upload/v1778783450/ChatGPT_Image_14_de_mai._de_2026_15_30_35_wuovpb.png"

conn = psycopg2.connect(
    os.environ["DATABASE_URL"],
    cursor_factory=psycopg2.extras.RealDictCursor,
    options="-c statement_timeout=0",
    connect_timeout=30,
)
cur = conn.cursor()

# Busca EANs afetados: imagem_cosmos NULL + anvisa diz exibir=False
cur.execute("""
    SELECT DISTINCT pc.ean, COALESCE(ac.tarja,'vermelha') AS tarja
    FROM produto_canon pc
    JOIN medicamentos m
      ON LTRIM(COALESCE(pc.ean,''),'0') = LTRIM(COALESCE(m.barra_norm,m.barra,''),'0')
    JOIN anvisa_cache ac
      ON ac.encontrado = TRUE
     AND ac.exibir_imagem_publica = FALSE
     AND ac.chave = UPPER(SPLIT_PART(
             REGEXP_REPLACE(m.descricao, '[^\\w\\s]', ' ', 'g'),
             ' ', 1
         ))
    WHERE pc.imagem_cosmos IS NULL
""")
rows = cur.fetchall()
print(f"Produtos para restaurar (exibir=False + imagem_cosmos NULL): {len(rows)}")

restaurados = 0
for r in rows:
    ph = PRETA if (r["tarja"] or "").lower() == "preta" else VERMELHA
    cur.execute(
        "UPDATE produto_canon SET imagem_cosmos=%s, fonte='placeholder_poupaqui' WHERE ean=%s AND imagem_cosmos IS NULL",
        (ph, r["ean"])
    )
    if cur.rowcount:
        restaurados += 1
        print(f"  [{r['tarja']}] {r['ean']}")

conn.commit()
print(f"\nRestaurados: {restaurados}")
cur.close()
conn.close()
