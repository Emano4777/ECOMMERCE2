"""Scheduled classification of inbound WhatsApp messages.

Run every minute from a trusted worker. This stage intentionally does NOT send
messages. Sending must be enabled only after contact verification and audit.
"""
import os
import psycopg2
from dotenv import load_dotenv
from poupaqui_ai_policy import classify_with_anthropic

def run():
    load_dotenv()
    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ecommerce_wa_ai_triagem (
                        message_id TEXT PRIMARY KEY,
                        telefone TEXT NOT NULL,
                        mensagem TEXT NOT NULL,
                        decisao TEXT NOT NULL,
                        resposta_sugerida TEXT,
                        motivo TEXT,
                        processado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT m.message_id, m.telefone, m.mensagem
                FROM ecommerce_wa_respostas_teste m
                LEFT JOIN ecommerce_wa_ai_triagem t ON t.message_id = m.message_id
                WHERE t.message_id IS NULL
                ORDER BY m.recebido_em ASC LIMIT 20
            """)
            items = cur.fetchall()
        for message_id, phone, body in items:
            result = classify_with_anthropic(body)
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ecommerce_wa_ai_triagem
                            (message_id, telefone, mensagem, decisao, resposta_sugerida, motivo)
                        VALUES (%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (message_id) DO NOTHING
                    """, (message_id, phone, body, result.decision, result.reply, result.reason))
        print("Classified", len(items), "messages; outbound sending disabled")
    finally:
        conn.close()

if __name__ == "__main__":
    run()
