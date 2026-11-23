import copy
import unittest
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

from ml_aftersales_alerts import collect, fingerprint, message, snapshot, SELLER


def claim():
    return {'id': 123, 'resource': 'order', 'resource_id': 456, 'type': 'return',
            'status': 'opened', 'stage': 'claim', 'reason_id': 'PDD-1',
            'last_updated': '2026-11-23T18:00:00Z',
            'players': [{'user_id': int(SELLER), 'role': 'respondent',
                         'available_actions': [{'action': 'send_message_to_complainant',
                                                'mandatory': True, 'due_date': '2026-11-24T18:00:00Z'}]}]}


class AftersalesTests(unittest.TestCase):
    def test_message_has_sale_product_deadline_and_correct_link(self):
        order = {'id': 456, 'order_items': [{'quantity': 2, 'item': {'title': 'Ricosol FPS 70'}}]}
        text = message(claim(), [{'id': 9, 'status': 'shipped', 'status_money': 'retained'}], order)
        for value in ('Pós-venda', 'Devolução #123', 'Venda #456', '2 un. — Ricosol',
                      '24/11/2026 às 15:00', '/vendas/456/detalhe', 'A caminho', 'Valor retido'):
            self.assertIn(value, text)
        self.assertNotIn('Nova venda paga', text)

    def test_return_progress_and_new_activity_are_new_events(self):
        c = claim()
        a = fingerprint(snapshot(c, [{'id': 9, 'status': 'shipped'}]))
        self.assertEqual(a, fingerprint(snapshot(copy.deepcopy(c), [{'status': 'shipped', 'id': 9}])))
        self.assertNotEqual(a, fingerprint(snapshot(c, [{'id': 9, 'status': 'delivered'}])))
        c['last_updated'] = '2026-11-23T19:00:00Z'
        self.assertNotEqual(a, fingerprint(snapshot(c, [{'id': 9, 'status': 'shipped'}])))

    def test_unknown_deadline_not_invented_and_other_seller_not_used(self):
        c = claim()
        c['players'][0]['user_id'] = 1
        text = message(c, [])
        self.assertNotIn('24/11/2026', text)
        self.assertIn('Confira no painel', text)

    def test_both_states_pagination_and_seller_scoping(self):
        calls = []

        def api(path, token):
            query = parse_qs(urlparse(path).query)
            calls.append(query)
            self.assertEqual(query['players.user_id'], [SELLER])
            self.assertEqual(query['players.role'], ['respondent'])
            if query['status'] == ['closed']:
                self.assertIn('last_updated:after:', query['range'][0])
                return {'paging': {'total': 0}, 'data': []}
            offset = int(query['offset'][0])
            return {'paging': {'total': 31}, 'data': [{'id': offset + 1}]}

        result = collect(api, 'fake', datetime(2026, 11, 23, tzinfo=timezone.utc))
        self.assertEqual(set(result), {'1', '31'})
        self.assertEqual(len(calls), 3)

    def test_failed_search_cannot_be_treated_as_no_cases(self):
        with self.assertRaises(RuntimeError):
            collect(lambda *a: None, 'fake', datetime.now(timezone.utc))


if __name__ == '__main__':
    unittest.main()
