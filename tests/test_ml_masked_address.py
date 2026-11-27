"""O Mercado Livre mascara nome/telefone/rua/numero/complemento do endereco
de entrega com uma sequencia de "X" ate a etiqueta ser gerada. Usar esse
valor sem checar sobrescrevia o nome real do comprador (vindo do buyer) com
"XXXXXXX" -- achado numa venda real (pedido #2000019826806408, Fabio
Guimaraes Neves virou "XXXXXXX" no painel)."""
import os
import unittest

_ENV = dict(os.environ)
import app  # noqa: E402  (o import carrega o .env; nao deixa vazar para os outros testes)
os.environ.clear()
os.environ.update(_ENV)


class MlMaskedFieldTests(unittest.TestCase):
    def test_all_x_sequences_are_masked(self):
        self.assertTrue(app._ml_is_masked_field("XXXXXXX"))
        self.assertTrue(app._ml_is_masked_field("xxxx"))
        self.assertTrue(app._ml_is_masked_field("X"))

    def test_real_values_are_not_masked(self):
        self.assertFalse(app._ml_is_masked_field("Fabio Guimaraes Neves"))
        self.assertFalse(app._ml_is_masked_field("Rua XV de Novembro"))
        self.assertFalse(app._ml_is_masked_field("123"))

    def test_empty_or_none_is_not_masked(self):
        self.assertFalse(app._ml_is_masked_field(""))
        self.assertFalse(app._ml_is_masked_field(None))
        self.assertFalse(app._ml_is_masked_field("   "))


if __name__ == "__main__":
    unittest.main()
