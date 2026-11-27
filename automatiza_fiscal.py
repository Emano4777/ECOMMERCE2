"""Cache only authorized XMLs belonging to an exported order of this store."""
import gzip
import xml.etree.ElementTree as ET
from automatiza_channels import read_connection

NS = {"n": "http://www.portalfiscal.inf.br/nfe"}


def authorized_xml(blob, cnpjloja):
    data = bytes(blob) if not isinstance(blob, str) else blob.encode("utf-8")
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    if len(data) > 5_000_000 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("XML fiscal invalido")
    root = ET.fromstring(data)
    def text(path):
        return root.findtext(path, default="", namespaces=NS).strip()
    issuer = text(".//n:emit/n:CNPJ")
    key = text(".//n:protNFe/n:infProt/n:chNFe")
    status = text(".//n:protNFe/n:infProt/n:cStat")
    env = text(".//n:ide/n:tpAmb")
    model = text(".//n:ide/n:mod")
    number = text(".//n:ide/n:nNF")
    inf = root.find(".//n:infNFe", NS)
    if issuer != cnpjloja or status not in {"100", "150"} or env != "1" or model not in {"55", "65"} or len(key) != 44 or not key.isdigit() or inf is None or inf.get("Id") != "NFe" + key:
        raise ValueError("XML sem autorizacao de producao ou emitente divergente")
    return data.decode("utf-8-sig"), key, number, model


def ensure_schema(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS ecommerce_automatiza_xml (
        pedido_id UUID PRIMARY KEY,cnpjloja TEXT NOT NULL,chave_acesso TEXT NOT NULL,
        numero_nota TEXT,modelo TEXT NOT NULL,xml_nfe TEXT NOT NULL,
        sincronizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),ml_enviado_em TIMESTAMPTZ,
        ml_erro TEXT,ml_tentativa_em TIMESTAMPTZ)""")
    # ml_tentativa_count/ml_alerta_em: antes uma nota rejeitada pelo ML ficava
    # so tentando de novo a cada 5min pra sempre, sem ninguem ser avisado se
    # continuasse falhando -- usados pra avisar o dono no WhatsApp apos
    # algumas tentativas seguidas sem sucesso (ver automatiza_ml_sync.sync).
    cur.execute("ALTER TABLE ecommerce_automatiza_xml ADD COLUMN IF NOT EXISTS ml_tentativa_count INTEGER NOT NULL DEFAULT 0")
    cur.execute("ALTER TABLE ecommerce_automatiza_xml ADD COLUMN IF NOT EXISTS ml_alerta_em TIMESTAMPTZ")


def sync_xml(cnpjloja, local_connect):
    with local_connect() as conn:
        with conn.cursor() as cur:
            ensure_schema(cur)
            conn.commit()
            cur.execute("""SELECT p.id,p.automatiza_pedido_codigo FROM ecommerce_pedidos p
                WHERE p.cnpjloja=%s AND p.automatiza_pedido_codigo IS NOT NULL
                AND p.status NOT IN ('cancelado','estornado','reembolsado')
                AND NOT EXISTS(SELECT 1 FROM ecommerce_automatiza_xml x WHERE x.pedido_id=p.id)
                ORDER BY p.criado_em DESC LIMIT 100""", (cnpjloja,))
            pending = cur.fetchall()
            count = 0
            if not pending:
                return {"ok": True, "total": 0}
            with read_connection() as erp:
                with erp.cursor() as acur:
                    for order in pending:
                        acur.execute("""SELECT x.noarArquivo FROM nota n
                            JOIN nota_arquivo_xml x ON x.noarChave_Acesso=n.notaNFe_Chave_Acesso AND x.filiCodigo=n.filiCodigo
                            WHERE n.Codigo_Pedido_Ecommerce=%s AND n.filiCodigo=1
                            AND n.notaAutorizada=1 AND (n.notaData_Cancelamento IS NULL OR n.notaData_Cancelamento<'1901-01-01')
                            ORDER BY n.Codigo DESC LIMIT 1""", (order["automatiza_pedido_codigo"],))
                        row = acur.fetchone()
                        if not row:
                            continue
                        xml,key,number,model = authorized_xml(row["noarArquivo"], cnpjloja)
                        cur.execute("""INSERT INTO ecommerce_automatiza_xml
                            (pedido_id,cnpjloja,chave_acesso,numero_nota,modelo,xml_nfe)
                            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(pedido_id) DO NOTHING""",
                            (order["id"],cnpjloja,key,number,model,xml))
                        count += cur.rowcount
            conn.commit()
            return {"ok": True,"total": count}
