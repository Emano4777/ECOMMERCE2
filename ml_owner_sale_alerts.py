"""Additional new-sale recipient, without changing the store's alert outbox."""
from ml_order_alerts import STORE, SELLER, eligible, message

PHONE = '5517991436759'
LOCK = 93501375635


def run(conn, get_token, api_get, send):
    token = get_token(cnpjloja=STORE)
    conn.commit()
    if not token or str((api_get('/users/me', token) or {}).get('id')) != SELLER:
        raise RuntimeError('Conta ML divergente no aviso adicional de vendas')
    with conn.cursor() as cur:
        cur.execute('SELECT pg_try_advisory_xact_lock(%s) AS locked', (LOCK,))
        if not cur.fetchone()['locked']:
            conn.rollback()
            return {'ok': True, 'busy': True}
    try:
        result = _run(conn, token, api_get, send)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def _run(conn, token, api_get, send):
    with conn.cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS ml_wa_sale_copy_config (
            seller_id TEXT NOT NULL, phone TEXT NOT NULL,
            activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_success_at TIMESTAMPTZ,
            PRIMARY KEY(seller_id,phone))''')
        cur.execute('''CREATE TABLE IF NOT EXISTS ml_wa_sale_copies (
            seller_id TEXT NOT NULL, phone TEXT NOT NULL, order_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), sent_at TIMESTAMPTZ,
            attempts INTEGER NOT NULL DEFAULT 0, skipped BOOLEAN NOT NULL DEFAULT FALSE,
            last_error TEXT, PRIMARY KEY(seller_id,phone,order_id))''')
        cur.execute('ALTER TABLE ml_wa_sale_copies ADD COLUMN IF NOT EXISTS wa_message_id TEXT')
        cur.execute('''INSERT INTO ml_wa_sale_copy_config(seller_id,phone)
            VALUES (%s,%s) ON CONFLICT DO NOTHING''', (SELLER, PHONE))
        cur.execute('SELECT activated_at FROM ml_wa_sale_copy_config WHERE seller_id=%s AND phone=%s', (SELLER, PHONE))
        activated_at = cur.fetchone()['activated_at']
        # The original sale job discovers paid sales. This observer only reads it.
        cur.execute('''INSERT INTO ml_wa_sale_copies(seller_id,phone,order_id)
            SELECT seller_id,%s,order_id FROM ml_wa_order_alerts
            WHERE seller_id=%s AND created_at >= %s AND NOT skipped
            ON CONFLICT DO NOTHING''', (PHONE, SELLER, activated_at))
        cur.execute('''SELECT order_id FROM ml_wa_sale_copies
            WHERE seller_id=%s AND phone=%s AND sent_at IS NULL AND NOT skipped
            ORDER BY attempts,created_at LIMIT 1''', (SELLER, PHONE))
        row = cur.fetchone()
    sent = failed = 0
    if row:
        oid = row['order_id']
        order = api_get('/orders/' + oid, token)
        if not order:
            with conn.cursor() as cur:
                cur.execute('''UPDATE ml_wa_sale_copies SET attempts=attempts+1,
                    last_error='Falha ao consultar venda' WHERE seller_id=%s AND phone=%s AND order_id=%s''',
                    (SELLER, PHONE, oid))
            failed = 1
        elif not eligible(order, activated_at):
            with conn.cursor() as cur:
                cur.execute('''UPDATE ml_wa_sale_copies SET skipped=TRUE
                    WHERE seller_id=%s AND phone=%s AND order_id=%s''', (SELLER, PHONE, oid))
        else:
            sid = (order.get('shipping') or {}).get('id')
            sla = api_get(f'/shipments/{sid}/sla', token) if sid else None
            lead = api_get(f'/shipments/{sid}/lead_time', token) if sid else None
            shipment = api_get(f'/shipments/{sid}', token) if sid else None
            try:
                ok, wa_message_id = send(PHONE, message(order, sla, lead, shipment))
            except Exception:
                ok, wa_message_id = False, None
            with conn.cursor() as cur:
                cur.execute('''UPDATE ml_wa_sale_copies SET attempts=attempts+1,
                    sent_at=CASE WHEN %s THEN NOW() ELSE NULL END,last_error=%s,
                    wa_message_id=COALESCE(%s,wa_message_id)
                    WHERE seller_id=%s AND phone=%s AND order_id=%s''',
                    (ok, None if ok else 'WhatsApp não confirmou envio', wa_message_id, SELLER, PHONE, oid))
            sent, failed = int(ok), int(not ok)
    with conn.cursor() as cur:
        cur.execute('''UPDATE ml_wa_sale_copy_config SET last_success_at=NOW()
            WHERE seller_id=%s AND phone=%s''', (SELLER, PHONE))
    return {'ok': not failed, 'sent': sent, 'failed': failed}
