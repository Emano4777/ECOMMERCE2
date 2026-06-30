import os
from dotenv import load_dotenv
HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
import psycopg2
from psycopg2.extras import RealDictCursor

dsn = os.getenv("DATABASE_URL")
conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor, connect_timeout=10)
cur = conn.cursor()
EAN = "7894650009567"  # REPELENTE OFF FAMILY (do precificador)
cur.execute("""
    SELECT ap.cnpjloja, ap.ean, ap.nome, ap.estoque, ap.inativo, ap.imagem_url,
           m.imagem AS med_img, mi.cloudinary_url AS med_cloud,
           pc.imagem_cosmos, pc.fonte AS pc_fonte,
           epi.imagem_url AS loja_img
    FROM ecommerce_alpha_produtos ap
    LEFT JOIN medicamentos m ON LTRIM(COALESCE(m.barra_norm,m.barra,''),'0')=LTRIM(COALESCE(ap.ean,''),'0')
    LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id=m.id
    LEFT JOIN produto_canon pc ON LTRIM(COALESCE(pc.ean,''),'0')=LTRIM(COALESCE(ap.ean,''),'0')
    LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja=ap.cnpjloja AND LTRIM(COALESCE(epi.ean,''),'0')=LTRIM(COALESCE(ap.ean,''),'0')
    WHERE LTRIM(COALESCE(ap.ean,''),'0')=LTRIM(%s,'0')
""", (EAN,))
rows = cur.fetchall()
if not rows:
    print("EAN nao encontrado em ecommerce_alpha_produtos; tentando por nome REPELENTE...")
    cur.execute("SELECT cnpjloja, ean, nome, estoque, inativo, imagem_url FROM ecommerce_alpha_produtos WHERE nome ILIKE '%REPELENTE%'")
    rows = cur.fetchall()
for r in rows:
    print(dict(r))
cur.close(); conn.close()
