"""Avisos ML (venda paga/copia/pos-venda) guardam o id que a WA Sender
devolveu pro envio, nao so que o provedor "aceitou" -- aceito != entregue
no celular (achado investigando avisos que o cliente relatou como
nao recebidos, apesar de constarem como enviados)."""
import os
import unittest

_ENV = dict(os.environ)
import app  # noqa: E402  (o import carrega o .env; nao deixa vazar para os outros testes)
os.environ.clear()
os.environ.update(_ENV)


class MlWaSendResultTests(unittest.TestCase):
    def test_extracts_id_from_known_wa_sender_response_shapes(self):
        self.assertEqual(app._ml_wa_send_result({"success": True, "data": {"id": "abc"}}), (True, "abc"))
        self.assertEqual(app._ml_wa_send_result({"success": True, "data": {"msgId": "xyz"}}), (True, "xyz"))
        self.assertEqual(app._ml_wa_send_result({"success": True, "data": {"key": {"id": "k1"}}}), (True, "k1"))

    def test_failure_or_missing_data_never_invents_an_id(self):
        self.assertEqual(app._ml_wa_send_result({"success": False}), (False, None))
        self.assertEqual(app._ml_wa_send_result({"success": False, "data": {"id": "would-be-ignored"}}), (False, None))
        self.assertEqual(app._ml_wa_send_result(False), (False, None))
        self.assertEqual(app._ml_wa_send_result(None), (False, None))
        self.assertEqual(app._ml_wa_send_result({"success": True}), (True, None))


if __name__ == "__main__":
    unittest.main()
