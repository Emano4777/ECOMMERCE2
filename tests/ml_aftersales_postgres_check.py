"""Integration check in an isolated temporary Postgres schema; no WhatsApp calls."""
import uuid
from copy import deepcopy
from urllib.parse import urlparse, parse_qs


def exercise(conn, module):
    # Keep the entire fixture on one transaction/backend even behind a pooler.
    real_conn = conn

    class TransactionFixture:
        def cursor(self):
            return real_conn.cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

    conn = TransactionFixture()
    schema = 'test_ml_aftersales_' + uuid.uuid4().hex
    with conn.cursor() as cur:
        cur.execute('CREATE SCHEMA ' + schema)
        cur.execute('SET search_path TO ' + schema)
    conn.commit()
    state = {'return_status': 'shipped', 'claim_status': 'opened', 'fail_send': True}
    sent = []
    claim = {'id': 123, 'resource': 'order', 'resource_id': 456, 'type': 'return',
             'status': 'opened', 'stage': 'claim', 'reason_id': 'PDD-1',
             'last_updated': '2026-11-23T18:00:00Z',
             'players': [{'user_id': int(module.SELLER), 'role': 'respondent', 'available_actions': []}]}

    def api(path, token):
        if path == '/users/me':
            return {'id': int(module.SELLER)}
        if '/search?' in path:
            status = parse_qs(urlparse(path).query)['status'][0]
            rows = [claim] if status == state['claim_status'] else []
            return {'paging': {'total': len(rows)}, 'data': rows}
        if path.endswith('/returns'):
            return {'id': 9, 'status': state['return_status'], 'status_money': 'retained'}
        if path.endswith('/claims/123'):
            result = deepcopy(claim)
            result['status'] = state['claim_status']
            return result
        if path == '/orders/456':
            return {'id': 456, 'seller': {'id': int(module.SELLER)},
                    'order_items': [{'quantity': 2, 'item': {'title': 'Ricosol'}}]}
        raise AssertionError(path)

    def send(phone, body):
        assert phone == module.PHONE
        if state['fail_send']:
            return False
        sent.append(body)
        return True

    try:
        run = lambda: module.run(conn, lambda **kw: 'fake', api, send)
        assert run()['failed'] == 1
        state['fail_send'] = False
        assert run()['sent'] == 1
        assert run()['sent'] == 0
        state['return_status'] = 'delivered'
        assert run()['sent'] == 1
        state['claim_status'] = 'closed'
        assert run()['sent'] == 1
        assert run()['sent'] == 0
        assert len(sent) == 3
        with conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s", (schema,))
            tables = {r['tablename'] for r in cur.fetchall()}
            assert tables == {'ml_wa_aftersales_config', 'ml_wa_aftersales_events'}, tables
        print('Postgres integration passed: failed-send retry, dedupe, return progress, closure, separate tables; no real messages.')
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute('SET search_path TO public')
            cur.execute('DROP SCHEMA ' + schema + ' CASCADE')
        conn.commit()
        real_conn.rollback()
