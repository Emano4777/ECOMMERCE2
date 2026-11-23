"""Isolated database fixture; sends only to a fake provider."""
import uuid
from datetime import datetime, timedelta, timezone


def exercise(real_conn, module):
    class Fixture:
        def cursor(self):
            return real_conn.cursor()
        def commit(self):
            pass
        def rollback(self):
            pass

    conn = Fixture()
    schema = 'test_ml_sale_copy_' + uuid.uuid4().hex
    try:
        with conn.cursor() as cur:
            cur.execute('CREATE SCHEMA ' + schema)
            cur.execute('SET LOCAL search_path TO ' + schema)
            cur.execute('''CREATE TABLE ml_wa_order_alerts (
                seller_id TEXT,order_id TEXT,created_at TIMESTAMPTZ,
                sent_at TIMESTAMPTZ,skipped BOOLEAN DEFAULT FALSE)''')
            cur.execute("INSERT INTO ml_wa_order_alerts VALUES (%s,'old',NOW()-INTERVAL '1 day',NOW(),FALSE)", (module.SELLER,))
        state = {'fail': True, 'status': 'paid'}
        sent = []
        def api(path, token):
            if path == '/users/me':
                return {'id': module.SELLER}
            if path == '/orders/new':
                return {'id': 'new', 'seller': {'id': module.SELLER}, 'status': state['status'],
                        'date_closed': (datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(),
                        'order_items': [{'quantity': 1, 'item': {'title': 'Ricosol'}}]}
            raise AssertionError(path)
        def send(phone, text):
            assert phone == '5517991436759'
            if state['fail']:
                return False
            sent.append(text)
            return True
        run = lambda: module.run(conn, lambda **kw: 'fake', api, send)
        assert run()['sent'] == 0  # No replay of old store alerts.
        with conn.cursor() as cur:
            cur.execute("INSERT INTO ml_wa_order_alerts VALUES (%s,'new',NOW(),NULL,FALSE)", (module.SELLER,))
            cur.execute('SELECT * FROM ml_wa_order_alerts ORDER BY order_id')
            before = cur.fetchall()
        assert run()['failed'] == 1
        state['fail'] = False
        assert run()['sent'] == 1
        assert run()['sent'] == 0
        assert len(sent) == 1
        assert '/vendas/new/detalhe' in sent[0]
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM ml_wa_order_alerts ORDER BY order_id')
            assert cur.fetchall() == before  # The store ledger was never changed.
        print('Owner alert integration passed: activation cutoff, retry, dedupe, correct number; store ledger unchanged; no real messages.')
    finally:
        real_conn.rollback()  # All fixture DDL/data disappear, including on failure.
