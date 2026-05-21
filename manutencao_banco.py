"""
manutencao_banco.py — VACUUM nas tabelas com lixo acumulado.
Rodar: python manutencao_banco.py

Indice duplicado medicamentos_uniq_fabric_barra ja foi removido via Supabase MCP.
"""
import os, time
import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

url = os.environ["DATABASE_URL"]
# Pooler modo sessao (porta 5432) suporta VACUUM
url_sessao = url.replace(":6543/", ":5432/")

print("Conectando...")
conn = psycopg2.connect(url_sessao)
conn.autocommit = True
cur = conn.cursor()
cur.execute("SET statement_timeout = 0")   # sem timeout para manutenção
cur.execute("SET lock_timeout = '10s'")    # desiste rápido se tabela estiver travada

TABELAS = [
    # vendageral, compras, carrinhos_backup_daily ja foram feitos
    ("acode_si_91_xml_analitico_retroativo","15 dias sem vacuum, 28k dead — mais pesado"),
    ("medicamentos_relacoes",               "30 dias sem vacuum"),
    ("ecommerce_produto_imagens",           "alta rotatividade"),
]

for tabela, motivo in TABELAS:
    print(f"\n  VACUUM ANALYZE {tabela}  ({motivo})")
    print("  aguardando...", flush=True)
    t0 = time.monotonic()
    cur.execute(f"VACUUM ANALYZE {tabela}")
    print(f"  OK — {time.monotonic()-t0:.1f}s")

cur.close()
conn.close()
print("\nManutenção concluída.")
