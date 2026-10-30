"""Diagnóstico isolado da função real via AST; sem importar/iniciar app.py.

Banco, armazenamento e logger são simulados. Não carrega .env nem chama IA.
"""
import ast
import os
import re
from pathlib import Path
import secrets
import unittest
from unittest.mock import MagicMock, Mock, patch

from flask import Flask, jsonify, request
import poupaqui_wa_atendimento as wa
import wasender_inbound_test as receiver


class WebhookDiagnosticoTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1].joinpath('app.py').read_text(encoding='utf-8-sig')
        fn = next(n for n in ast.parse(source).body
                  if isinstance(n, ast.FunctionDef) and n.name == 'crm_wasender_webhook')
        fn.decorator_list = []
        self.app = Flask(__name__)
        self.db = Mock()
        self.ns = dict(os=os, re=re, _sem_acento=lambda s: s, secrets=secrets, request=request, jsonify=jsonify,
                       app=self.app, db=self.db, _CRON_SECRET='synthetic-secret')
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'app.py', 'exec'), self.ns)
        self.app.add_url_rule('/webhook', view_func=self.ns[fn.name], methods=['POST'])
        self.client = self.app.test_client()
        self.inbound = self.enterContext(patch.object(wa, 'store_inbound', return_value=1))
        self.outbound = self.enterContext(patch.object(wa, 'store_outbound', return_value={}))
        self.enterContext(patch.object(receiver, 'record_test_replies', return_value=0))
        self.enterContext(patch.dict(os.environ, {'WASENDER_WEBHOOK_SECRET': 'synthetic-secret'}))

    def post(self, event, from_me=False):
        return self.client.post('/webhook', headers={'X-Webhook-Signature': 'synthetic-secret'},
                                json={'event': event, 'data': {'messages': {
                                    'key': {'id': 'synthetic-id', 'fromMe': from_me,
                                            'remoteJid': '5500000000000@s.whatsapp.net'},
                                    'message': {'conversation': 'synthetic'}}}})

    def test_received_routes_to_storage(self):
        self.assertEqual(self.post('messages.received').status_code, 200)
        self.inbound.assert_called_once()

    def test_upsert_inbound_should_route_to_storage(self):
        self.assertEqual(self.post('messages.upsert').status_code, 200)
        self.inbound.assert_called_once()

    def test_upsert_outbound_routes_to_storage(self):
        self.assertEqual(self.post('messages.upsert', True).status_code, 200)
        self.outbound.assert_called_once()

    def test_outbound_failure_returns_retryable_error(self):
        self.outbound.side_effect = RuntimeError('synthetic storage failure')
        self.app.logger.disabled = True
        response = self.post('messages.upsert', True)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json, {'ok': False, 'error': 'storage_failed'})
        self.inbound.assert_not_called()
        self.db.return_value.rollback.assert_called_once()

    def test_sent_storage_failure_even_if_rollback_fails(self):
        self.outbound.side_effect = RuntimeError('synthetic storage failure')
        self.db.return_value.rollback.side_effect = RuntimeError('synthetic rollback failure')
        self.app.logger.disabled = True
        self.assertEqual(self.post('message.sent', True).status_code, 503)
        self.inbound.assert_not_called()

    def test_bad_signature_cannot_reach_database(self):
        self.assertEqual(self.client.post('/webhook', json={}).status_code, 401)
        self.db.assert_not_called()

    def test_inbound_failure_returns_retryable_error(self):
        self.inbound.side_effect = RuntimeError('synthetic storage failure')
        self.app.logger.disabled = True
        self.assertEqual(self.post('messages.received').status_code, 503)

    def test_mixed_batch_retry_after_partial_commit(self):
        # Stateful storage doubles: exercise the actual direction parsers and
        # retry contract, without pretending to validate PostgreSQL transactions.
        saved, takeovers = set(), []
        fail = [True]

        def outbound(payload, conn):
            novas = 0
            for item in wa.parse_outbound(payload):
                if item['message_id'] not in saved:
                    saved.add(item['message_id'])
                    takeovers.append(item['telefone'])
                    novas += 1
            return {'ia': 0, 'sistema': 0, 'manual': novas}   # mesmo formato do store_outbound real

        def inbound(payload, conn):
            if fail and fail.pop():
                raise RuntimeError('synthetic inbound failure after outbound commit')
            count = 0
            for item in wa.parse_inbound(payload):
                if item['message_id'] not in saved:
                    saved.add(item['message_id'])
                    count += 1
            return count

        self.outbound.side_effect = outbound
        self.inbound.side_effect = inbound
        self.app.logger.disabled = True
        def msg(mid, direction):
            return {'key': {'id': mid, 'fromMe': direction,
                            'remoteJid': '5500000000000@s.whatsapp.net'},
                    'message': {'conversation': 'synthetic'}}
        body = {'event': 'messages.upsert', 'data': {'messages': [
            msg('IN', False), msg('OUT', True), msg('IN', False), msg('OUT', True),
            msg('UNKNOWN', None)]}}
        def send():
            return self.client.post('/webhook', json=body,
                                    headers={'X-Webhook-Signature': 'synthetic-secret'})
        self.assertEqual(send().status_code, 503)
        self.assertEqual(saved, {'OUT'})
        response = send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['messages_stored'], 1)
        self.assertEqual(saved, {'IN', 'OUT'})
        self.assertEqual(send().json['messages_stored'], 0)
        self.assertEqual(len(takeovers), 1)

    def test_upsert_opt_out_only_for_inbound_in_mixed_batch(self):
        messages = [{'key': {'id': str(i), 'fromMe': direction,
                             'remoteJid': '5500000000000@s.whatsapp.net'},
                     'messageBody': 'parar'} for i, direction in enumerate([True, False, None])]
        response = self.client.post('/webhook', json={'event': 'messages.upsert',
                                   'data': {'messages': messages}},
                                   headers={'X-Webhook-Signature': 'synthetic-secret'})
        self.assertEqual(response.status_code, 200)
        self.db.return_value.cursor.return_value.execute.assert_called_once()


class StorageRegressionTests(unittest.TestCase):
    """Real storage functions, scripted SQL results; no connection or schema DDL."""

    def setUp(self):
        self.conn = MagicMock()
        self.cur = self.conn.cursor.return_value.__enter__.return_value
        self.enterContext(patch.object(wa, 'ensure_schema'))

    def payload(self, from_me):
        return {'data': {'messages': {'key': {'id': 'synthetic-id', 'fromMe': from_me,
                'remoteJid': '5500000000000@s.whatsapp.net'},
                'message': {'conversation': 'synthetic'}}}}

    def test_duplicate_inbound_does_not_add_history(self):
        self.cur.fetchone.side_effect = [(1,), None]
        self.assertEqual(wa.store_inbound(self.payload(False), self.conn), 1)
        self.assertEqual(wa.store_inbound(self.payload(False), self.conn), 0)
        inserts = [call.args[0] for call in self.cur.execute.call_args_list
                   if 'INSERT INTO ecommerce_wa_mensagens' in call.args[0]]
        self.assertEqual(len(inserts), 2)
        self.assertTrue(all('ON CONFLICT (message_id) DO NOTHING' in sql for sql in inserts))

    def test_duplicate_manual_does_not_renew_takeover(self):
        self.cur.fetchone.side_effect = [None, (1,)]
        classify = self.enterContext(patch.object(wa, 'classify_outbound', return_value='manual'))
        takeover = self.enterContext(patch.object(wa, '_set_takeover'))
        self.assertEqual(wa.store_outbound(self.payload(True), self.conn)['manual'], 1)
        self.assertEqual(wa.store_outbound(self.payload(True), self.conn)['manual'], 0)
        takeover.assert_called_once()
        classify.assert_called_once()
        updates = [call for call in self.cur.execute.call_args_list
                   if "SET status = 'respondido_humano'" in call.args[0]]
        self.assertEqual(len(updates), 1)

    def test_ia_and_system_do_not_activate_takeover(self):
        takeover = self.enterContext(patch.object(wa, '_set_takeover'))
        for origin in ('ia', 'sistema'):
            with self.subTest(origin=origin), patch.object(wa, 'classify_outbound', return_value=origin):
                self.cur.fetchone.return_value = None
                self.assertEqual(wa.store_outbound(self.payload(True), self.conn)[origin], 1)
        takeover.assert_not_called()

    def test_storage_exception_does_not_commit(self):
        self.cur.execute.side_effect = RuntimeError('synthetic SQL failure')
        with self.assertRaises(RuntimeError):
            wa.store_outbound(self.payload(True), self.conn)
        self.conn.commit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
