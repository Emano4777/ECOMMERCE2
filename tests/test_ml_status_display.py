"""O status exibido pro lojista (badge na lista de pedidos, chip ativo em
'Alterar status') precisa refletir o status REAL do envio no Mercado Livre,
e nao so o status interno (que so mudava quando o lojista clicava o botao
manualmente e por isso ficava desatualizado -- achado quando o lojista
reportou que o status continuava igual mesmo com o pedido ja enviado/
entregue no Mercado Livre)."""
import os
import unittest

_ENV = dict(os.environ)
import app  # noqa: E402  (o import carrega o .env; nao deixa vazar para os outros testes)
os.environ.clear()
os.environ.update(_ENV)


class MlStatusBucketTests(unittest.TestCase):
    def test_preparing_stages_map_to_pago(self):
        for status in ("pending", "handling", "ready_to_ship"):
            self.assertEqual(app._ml_status_bucket(status), "pago")

    def test_in_transit_maps_to_enviado(self):
        self.assertEqual(app._ml_status_bucket("shipped"), "enviado")
        self.assertEqual(app._ml_status_bucket("not_delivered"), "enviado")

    def test_terminal_stages(self):
        self.assertEqual(app._ml_status_bucket("delivered"), "entregue")
        self.assertEqual(app._ml_status_bucket("cancelled"), "cancelado")

    def test_unknown_or_missing_status_does_not_override_internal_status(self):
        self.assertIsNone(app._ml_status_bucket(None))
        self.assertIsNone(app._ml_status_bucket(""))
        self.assertIsNone(app._ml_status_bucket("algo_novo_da_api"))

    def test_bucket_is_case_insensitive(self):
        self.assertEqual(app._ml_status_bucket("SHIPPED"), "enviado")


class MlStatusLabelTests(unittest.TestCase):
    def test_known_statuses_get_portuguese_label(self):
        self.assertEqual(app._ml_status_label("shipped"), "Em trânsito")
        self.assertEqual(app._ml_status_label("delivered"), "Entregue")

    def test_unknown_status_falls_back_to_raw_value(self):
        self.assertEqual(app._ml_status_label("algo_novo_da_api"), "algo_novo_da_api")
        self.assertEqual(app._ml_status_label(None), "—")


if __name__ == "__main__":
    unittest.main()
