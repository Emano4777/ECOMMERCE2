"""Per-store ERP ownership shared by the cloud app and local agents."""

def ensure_schema(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS ecommerce_erp_lojas (
        cnpjloja TEXT PRIMARY KEY,
        sistema TEXT NOT NULL CHECK (sistema IN ('alpha', 'automatiza')),
        alterado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")


def source_for_store(cur, cnpjloja):
    # Existing deployments have no routing table and retain their Alpha flow.
    cur.execute("SELECT to_regclass('public.ecommerce_erp_lojas') AS routing_table")
    row = cur.fetchone()
    exists = row.get('routing_table') if isinstance(row, dict) else row[0]
    if not exists:
        return 'alpha'
    cur.execute("SELECT sistema FROM ecommerce_erp_lojas WHERE cnpjloja=%s", (cnpjloja,))
    row = cur.fetchone()
    return (row.get('sistema') if isinstance(row, dict) else row[0]) if row else 'alpha'
