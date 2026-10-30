"""Poupinha no modelo local (Ollama): consultas e encaminhamento decididos pelo codigo, sem rede nem banco."""
import json
import os
import unittest
from unittest.mock import Mock, patch

_ENV = dict(os.environ)
import app  # noqa: E402  (o import carrega o .env: OLLAMA_URL, modelo... vazariam para os outros testes)
os.environ.clear()
os.environ.update(_ENV)


def _resposta_ollama(texto, corpos):
    def fake(req, timeout=None):
        corpos.append(json.loads(req.data.decode()))
        m = Mock()
        m.__enter__ = lambda s: s
        m.__exit__ = lambda *a: False
        m.read.return_value = json.dumps({"message": {"content": texto}}).encode()
        return m
    return fake


class PoupinhaLocalTests(unittest.TestCase):
    def test_pedir_atendente_encaminha_de_verdade_sem_chamar_modelo(self):
        with patch.object(app, "_executar_ferramenta_suporte", return_value="ESCALADO_OK loja") as ferr, \
                patch("urllib.request.urlopen") as rede:
            texto, pessoal, escalou = app._ollama_suporte_responder("quero falar com um atendente",
                                                                    consumidor_id="c1", chat_id="ch1")
        self.assertTrue(escalou)
        self.assertIn("chamei um atendente", texto)
        self.assertEqual(ferr.call_args[0][0], "escalar_atendimento")
        self.assertEqual(ferr.call_args[0][1]["categoria"], "cliente_pediu")
        rede.assert_not_called()

    def test_cancelar_vai_para_atendente(self):
        with patch.object(app, "_executar_ferramenta_suporte", return_value="ESCALADO_OK loja") as ferr:
            texto, _, escalou = app._ollama_suporte_responder("quero cancelar meu pedido", consumidor_id="c1",
                                                              chat_id="ch1")
        self.assertTrue(escalou)
        self.assertEqual(ferr.call_args[0][1]["categoria"], "acao_humana")
        self.assertIn("cancelamento", texto.lower())

    def test_produto_consultado_vai_como_dado_e_mesmo_num_ctx(self):
        corpos = []
        with patch.object(app, "_executar_ferramenta_suporte", return_value="Dipirona 500mg: R$ 4,99") as ferr, \
                patch("urllib.request.urlopen", _resposta_ollama("Temos **Dipirona** por R$ 4,99.", corpos)), \
                patch.dict(os.environ, {"POUPAQUI_OLLAMA_NUM_CTX": ""}):
            texto, pessoal, escalou = app._ollama_suporte_responder("tem dipirona?", consumidor_id="c1", chat_id="ch1")
        self.assertEqual(ferr.call_args[0][:2], ("consultar_produto", {"nome": "dipirona"}))
        self.assertEqual(texto, "Temos Dipirona por R$ 4,99.")          # sem markdown no balao
        self.assertTrue(pessoal)                                         # dado consultado: nunca vai pro cache
        self.assertFalse(escalou)
        corpo = corpos[0]
        self.assertNotIn("tools", corpo)
        self.assertEqual(corpo["options"]["num_ctx"], 4096)              # igual ao WhatsApp: sem recarregar o modelo
        self.assertIn("Dados consultados agora no sistema", corpo["messages"][-1]["content"])
        self.assertNotIn("cancelar um pedido pendente", corpo["messages"][0]["content"])

    def test_remedio_para_sintoma_nao_consulta_produto(self):
        corpos = []
        with patch.object(app, "_executar_ferramenta_suporte") as ferr, \
                patch("urllib.request.urlopen", _resposta_ollama("Qual produto você procura?", corpos)):
            texto, pessoal, _ = app._ollama_suporte_responder("Tem remédio para gases ?", consumidor_id="c1",
                                                              chat_id="ch1")
        ferr.assert_not_called()
        self.assertFalse(pessoal)                                        # resposta generica pode ir pro cache
        self.assertEqual(texto, "Qual produto você procura?")

    def test_reserva_claude_so_quando_local_falha(self):
        with patch.object(app, "_ollama_suporte_responder", return_value=(None, False, False)), \
                patch.object(app, "_claude_suporte_responder", return_value=("ok claude", False, False)) as cl, \
                patch.dict(os.environ, {"POUPINHA_IA_MOTOR": "", "POUPINHA_FALLBACK_CLAUDE": ""}):
            self.assertEqual(app._suporte_responder("oi")[0], "ok claude")
            cl.assert_called_once()
        with patch.object(app, "_ollama_suporte_responder", return_value=(None, False, False)), \
                patch.object(app, "_claude_suporte_responder") as cl, \
                patch.dict(os.environ, {"POUPINHA_IA_MOTOR": "", "POUPINHA_FALLBACK_CLAUDE": "0"}):
            self.assertIsNone(app._suporte_responder("oi")[0])
            cl.assert_not_called()
        with patch.object(app, "_ollama_suporte_responder", return_value=("ok local", False, False)), \
                patch.object(app, "_claude_suporte_responder") as cl, \
                patch.dict(os.environ, {"POUPINHA_IA_MOTOR": ""}):
            self.assertEqual(app._suporte_responder("oi")[0], "ok local")
            cl.assert_not_called()



class EscaladaAbandonadaTests(unittest.TestCase):
    class Cur:
        def __init__(self, velha, humano):
            self.r, self.sql = {"velha": velha, "humano": humano}, []

        def execute(self, sql, params=None):
            self.sql.append(sql)

        def fetchone(self):
            return self.r

    def chat(self, **kw):
        return {"id": "ch", "status": "escalada_admin", "motivo_categoria": "sem_resposta",
                "escalada_em": "2026-10-15", **kw}

    def test_falha_da_ia_sem_humano_ha_horas_volta_para_ia(self):
        self.assertTrue(app._suporte_escalada_abandonada(self.Cur(True, False), self.chat()))

    def test_mantem_com_humano_nos_outros_casos(self):
        self.assertFalse(app._suporte_escalada_abandonada(self.Cur(True, True), self.chat()))     # humano respondeu
        self.assertFalse(app._suporte_escalada_abandonada(self.Cur(False, False), self.chat()))   # ainda recente
        cur = self.Cur(True, False)
        self.assertFalse(app._suporte_escalada_abandonada(cur, self.chat(motivo_categoria="cliente_pediu")))
        self.assertEqual(cur.sql, [])                                                             # nem consulta
        self.assertFalse(app._suporte_escalada_abandonada(cur, self.chat(status="bot")))


if __name__ == "__main__":
    unittest.main()
