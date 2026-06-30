import os
from dotenv import load_dotenv
HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
os.environ["ALPHA_DB_HOST"] = "aws-1-us-east-2.pooler.supabase.com"
os.environ["ALPHA_DB_PORT"] = "6543"
os.environ["ALPHA_DB_NAME"] = "postgres"
os.environ["ALPHA_DB_USER"] = "alpha7.wosjlqxbfajctoeztrug"
os.environ["ALPHA_DB_PASSWORD"] = "fODWFqHamoyM4RkD7pQnpk6ot1Dh"
os.environ["ALPHA_DB_SCHEMA"] = "alpha7"
os.environ["ALPHA_DB_SSLMODE"] = "require"
import app as A
CNPJ = "54185432000143"

conn = A._new_conn(); cur = conn.cursor()
cur.execute("SELECT cnpjloja, estoque_min_publicacao FROM ecommerce_config_loja WHERE cnpjloja=%s", (CNPJ,))
print("config loja:", dict(cur.fetchone()))
cur.close(); conn.close()

batch = [p for p in A.get_dns_products_batch([CNPJ])]
print(f"\nget_dns_products_batch -> {len(batch)} produtos:")
for r in batch:
    print("   ", r.get("ean"), "|", (r.get("nome") or "")[:45], "| qty:", r.get("qty"))
rep = [r for r in batch if (r.get("ean") or "").lstrip("0")=="7894650009567"]
print("\nREPELENTE no batch?", bool(rep))
