"""Regressao: conversa casual nao herda incidente tecnico antigo."""
import unittest
import poupaqui_ai_assistente as A

HIST = [
    {"de": "contato", "texto": "Meu Pedido Eletronico nao abre, esta com erro"},
    {"de": "assistente", "texto": "Me conta qual erro aparece?"},
]

class TestSaudacaoSemEscalacao(unittest.TestCase):
    def test_conversa_casual_nao_escala(self):
        for texto in ("Oi", "Ola", "Tudo bem?", "Bom dia"):
            with self.subTest(texto=texto):
                r = A.responder([texto], historico=HIST)
                self.assertEqual(r.acao, "ANSWER")
                self.assertFalse(r.escalar)
                self.assertIn(r.intencao, ("saudacao", "smalltalk"))

    def test_problema_continua_escala(self):
        r = A.responder(["Continua com erro"], historico=HIST)
        self.assertTrue(r.escalar)

if __name__ == "__main__":
    unittest.main()
