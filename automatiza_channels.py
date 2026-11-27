"""Channel-specific offers, read from Automatiza's directed price books."""
import json
import os
from decimal import Decimal, ROUND_HALF_UP
import pymysql


def read_connection():
    # Reuse the existing read-only reporting account, without copying its password.
    path = os.environ["AUTOMATIZA_READ_CONFIG"]
    with open(path, encoding="utf-8-sig") as f:
        cfg = json.load(f)["AppSettings"]["ConnectionString"]
    parts = dict((k.strip().lower().replace(" ", ""), v.strip()) for k, v in
                 (p.split("=", 1) for p in cfg.split(";") if "=" in p))
    return pymysql.connect(host=parts.get("server", "localhost"), port=int(parts.get("port", 3306)),
        user=parts.get("userid") or parts.get("uid") or parts.get("user"),
        password=parts.get("password") or parts.get("pwd"),
        database=parts.get("database", "automatiza"), connect_timeout=10, read_timeout=60,
        cursorclass=pymysql.cursors.DictCursor, charset="utf8mb4")


def book_price(row):
    if Decimal(str(row.get("precQtde_Minima") or 1)) != 1:
        return None  # Do not turn a multi-buy discount into a unit price.
    value = Decimal(str(row["precValor"]))
    formation = row["precFormacao"]
    if formation == "PRECO_FIXO":
        result = value
    elif formation == "DESCONTO" and 0 <= value < 100:
        result = Decimal(str(row["preco_venda"])) * (1 - value / 100)
    else:
        raise ValueError("Formacao de preco nao suportada no caderno")
    return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if result > 0 else None


def read_books(rows):
    products = {int(r["codigo_mercadoria"]): r for r in rows}
    books = {}
    with read_connection() as conn:
        with conn.cursor() as cur:
            for channel, env in (("ecommerce", "AUTOMATIZA_CADERNO_ECOMMERCE"), ("mercado_livre", "AUTOMATIZA_CADERNO_ML")):
                book = int(os.environ[env])
                cur.execute("""SELECT Codigo,prdiDescricao_ FROM precificacao_direcionamento
                    WHERE Codigo=%s AND Situacao='ATIVO'
                      AND (prdiData_Inicio IS NULL OR prdiData_Inicio<=NOW())
                      AND (prdiData_Termino IS NULL OR prdiData_Termino>=NOW())""", (book,))
                header = cur.fetchone()
                if not header:
                    raise RuntimeError("Caderno de precos ausente, inativo ou fora da validade")
                if channel == "mercado_livre":
                    cur.execute("""SELECT 1 FROM itens_precificacao_direcionamento_parceiro_ecommerce
                        WHERE Codigo_Precificacao_Direcionamento=%s AND Codigo_Parceiro_Ecommerce=9 AND filiCodigo=1""", (book,))
                    if not cur.fetchone():
                        raise RuntimeError("Caderno ML sem direcionamento para Mercado Livre")
                cur.execute("""SELECT Codigo_Mercadoria,precFormacao,precValor,precQtde_Minima
                    FROM precificacao WHERE Codigo_Precificacao_Direcionamento=%s AND Situacao='ATIVO'
                    ORDER BY precData_Alteracao,Codigo""", (book,))
                entries = {}
                for offer in cur.fetchall():
                    code = offer["Codigo_Mercadoria"]
                    if code not in products:
                        continue
                    value = book_price(dict(products[code], **offer))
                    if value is not None:
                        entries[code] = {"preco": value, "caderno": book}
                if not entries:
                    raise RuntimeError("Caderno sem precos unitarios validos; sincronizacao interrompida")
                books[channel] = entries
    return books


def apply_ecommerce_book(rows, books):
    for row in rows:
        offer = books["ecommerce"].get(int(row["codigo_mercadoria"]))
        if offer:
            row["preco_venda_delivery"] = offer["preco"]


def save_ml_book(cur, cnpjloja, rows, books):
    cur.execute("""CREATE TABLE IF NOT EXISTS ecommerce_automatiza_ml_precos (
        cnpjloja TEXT NOT NULL, ean TEXT NOT NULL, codigo_mercadoria BIGINT NOT NULL,
        caderno INTEGER NOT NULL, preco NUMERIC(15,2) NOT NULL, estoque INTEGER NOT NULL,
        sincronizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(cnpjloja,ean))""")
    cur.execute("DELETE FROM ecommerce_automatiza_ml_precos WHERE cnpjloja=%s", (cnpjloja,))
    for row in rows:
        code = int(row["codigo_mercadoria"])
        offer = books["mercado_livre"].get(code)
        if offer and row.get("EAN"):
            cur.execute("""INSERT INTO ecommerce_automatiza_ml_precos
                (cnpjloja,ean,codigo_mercadoria,caderno,preco,estoque) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(cnpjloja,ean) DO UPDATE SET codigo_mercadoria=EXCLUDED.codigo_mercadoria,
                caderno=EXCLUDED.caderno,preco=EXCLUDED.preco,estoque=EXCLUDED.estoque,sincronizado_em=NOW()""",
                (cnpjloja,row["EAN"],code,offer["caderno"],offer["preco"],max(0,int(row["estoque"] or 0))))
