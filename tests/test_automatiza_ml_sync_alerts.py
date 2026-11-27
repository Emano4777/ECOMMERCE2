"""Nota fiscal recusada pelo Mercado Livre so ficava tentando de novo
sozinha a cada 5min (ou, no caso de conflito de chave, nunca se resolvia
sozinha), sem ninguem ser avisado que a etiqueta ficou travada por causa
da nota -- achado investigando por que a liberacao de etiqueta e de nota
nao geravam nenhum aviso ativo."""
import unittest
from automatiza_ml_sync import _alertar_nota_fiscal_problema


class FakeCursor:
    def __init__(self, select_result):
        self.select_result = select_result
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql.strip().split()[0].upper(), params))

    def fetchone(self):
        return self.select_result


class FakeConn:
    def __init__(self):
        self.committed = 0

    def commit(self):
        self.committed += 1


class AlertarNotaFiscalTests(unittest.TestCase):
    INVOICE = {"pedido_id": "abc-123", "ml_order_id": "2000019826806408"}

    def test_no_send_callable_never_touches_db(self):
        cur = FakeCursor(select_result=None)
        _alertar_nota_fiscal_problema(cur, FakeConn(), None, self.INVOICE, "erro qualquer")
        self.assertEqual(cur.executed, [])

    def test_already_alerted_does_not_resend(self):
        cur = FakeCursor(select_result={"ml_tentativa_count": 5, "ml_alerta_em": "2026-11-27"})
        sent = []
        _alertar_nota_fiscal_problema(cur, FakeConn(), lambda p, t: sent.append((p, t)) or (True, "id"),
                                       self.INVOICE, "erro qualquer")
        self.assertEqual(sent, [])

    def test_below_threshold_does_not_alert_yet(self):
        cur = FakeCursor(select_result={"ml_tentativa_count": 2, "ml_alerta_em": None})
        sent = []
        _alertar_nota_fiscal_problema(cur, FakeConn(), lambda p, t: sent.append((p, t)) or (True, "id"),
                                       self.INVOICE, "erro qualquer", tentativas_minimas=3)
        self.assertEqual(sent, [])

    def test_reaching_threshold_sends_once_and_marks_alerted(self):
        cur = FakeCursor(select_result={"ml_tentativa_count": 3, "ml_alerta_em": None})
        conn = FakeConn()
        sent = []
        _alertar_nota_fiscal_problema(cur, conn, lambda p, t: sent.append((p, t)) or (True, "id"),
                                       self.INVOICE, "HTTP 400", tentativas_minimas=3)
        self.assertEqual(len(sent), 1)
        self.assertIn("2000019826806408", sent[0][1])
        self.assertIn("HTTP 400", sent[0][1])
        self.assertIn(("UPDATE", ("abc-123",)), cur.executed)
        self.assertEqual(conn.committed, 1)

    def test_failed_send_does_not_mark_alerted(self):
        cur = FakeCursor(select_result={"ml_tentativa_count": 3, "ml_alerta_em": None})
        conn = FakeConn()
        _alertar_nota_fiscal_problema(cur, conn, lambda p, t: (False, None),
                                       self.INVOICE, "HTTP 400", tentativas_minimas=3)
        self.assertEqual(conn.committed, 0)


if __name__ == "__main__":
    unittest.main()
