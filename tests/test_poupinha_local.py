"""Poupinha no modelo local (Ollama): o modelo decide o que o cliente quer, o codigo consulta, sem rede nem banco."""
import json
import os
import unittest
from unittest.mock import Mock, patch

_ENV = dict(os.environ)
import app  # noqa: E402  (o import carrega o .env: OLLAMA_URL, modelo... vazariam para os outros testes)
os.environ.clear()
os.environ.update(_ENV)


def _ollama(decisao, texto, corpos):
    """1a chamada (format json) devolve a decisao; a 2a, o texto da resposta."""
    def fake(req, timeout=None):
        corpo = json.loads(req.data.decode())
        corpos.append(corpo)
        conteudo = json.dumps(decisao) if corpo.get("format") == "json" else texto
        m = Mock()
        m.__enter__ = lambda s: s
        m.__exit__ = lambda *a: False
        m.read.return_value = json.dumps({"message": {"content": conteudo}}).encode()
        return m
    return fake


class PoupinhaLocalTests(unittest.TestCase):
    def test_modelo_decide_atendente_e_o_codigo_encaminha(self):
        corpos = []
        with patch.object(app, "_executar_ferramenta_suporte", return_value="ESCALADO_OK loja") as ferr, \
                patch("urllib.request.urlopen", _ollama({"acao": "atendente"}, "", corpos)):
            texto, pessoal, escalou = app._ollama_suporte_responder("quero falar com uma pessoa",
                                                                    consumidor_id="c1", chat_id="ch1")
        self.assertTrue(escalou)
        self.assertIn("chamei um atendente", texto)
        self.assertEqual(ferr.call_args[0][0], "escalar_atendimento")
        self.assertEqual(ferr.call_args[0][1]["categoria"], "cliente_pediu")
        self.assertEqual(len(corpos), 1)                                  # so a decisao; resposta e fixa

    def test_cancelar_vai_para_atendente_como_acao_humana(self):
        with patch.object(app, "_executar_ferramenta_suporte", return_value="ESCALADO_OK loja") as ferr, \
                patch("urllib.request.urlopen", _ollama({"acao": "atendente"}, "", [])):
            texto, _, escalou = app._ollama_suporte_responder("quero cancelar meu pedido", consumidor_id="c1",
                                                              chat_id="ch1")
        self.assertTrue(escalou)
        self.assertEqual(ferr.call_args[0][1]["categoria"], "acao_humana")

    def test_produto_com_termo_corrigido_pelo_modelo(self):
        corpos = []
        achado = ('Nao encontrei "x". Produtos mais vendidos da loja que podem interessar:\nCIMEGRIPE - R$ 12,99')
        with patch.object(app, "_executar_ferramenta_suporte", return_value=achado) as ferr, \
                patch("urllib.request.urlopen", _ollama({"acao": "produto", "produto": "lubrificante intimo"},
                                                        "Não encontrei **lubrificante** agora.", corpos)), \
                patch.dict(os.environ, {"POUPAQUI_OLLAMA_NUM_CTX": ""}):
            texto, pessoal, escalou = app._ollama_suporte_responder(
                "consegue pesquisar lubrificante intimio?", consumidor_id="c1", chat_id="ch1",
                historico=[{"autor": "consumidor", "mensagem": "oi"}, {"autor": "ia", "mensagem": "Oi! Como ajudo?"}])
        self.assertEqual(ferr.call_args[0][:2], ("consultar_produto", {"nome": "lubrificante intimo", "categoria": None}))
        self.assertEqual(texto, "Não encontrei lubrificante agora.")      # sem markdown no balao
        self.assertTrue(pessoal)                                          # dado consultado: nunca vai pro cache
        decisao, resposta = corpos
        self.assertIn("Cliente: oi", decisao["messages"][1]["content"])  # a decisao ve a conversa
        self.assertEqual(resposta["options"]["num_ctx"], 4096)            # igual ao WhatsApp: sem recarregar o modelo
        self.assertNotIn("CIMEGRIPE", json.dumps(resposta))              # mais vendidos sem relacao nao vao
        self.assertIn("Dados consultados agora no sistema", resposta["messages"][-1]["content"])

    def test_conversa_nao_consulta_nada_nem_pedido(self):
        corpos = []
        with patch.object(app, "_executar_ferramenta_suporte") as ferr, \
                patch("urllib.request.urlopen", _ollama({"acao": "conversa"}, "Tô bem, e você?", corpos)):
            texto, pessoal, _ = app._ollama_suporte_responder("se ta bem?", consumidor_id="c1", chat_id="ch1",
                                                              pedidos_recentes_ctx="- Pedido #A1B2C3D4 Cancelado")
        ferr.assert_not_called()
        self.assertFalse(pessoal)                                         # pode ir pro cache generico
        self.assertEqual(texto, "Tô bem, e você?")
        self.assertNotIn("A1B2C3D4", json.dumps(corpos[1]))             # pedido fora de assunto nao vai

    def test_pergunta_de_pedido_leva_os_pedidos_recentes(self):
        corpos = []
        with patch("urllib.request.urlopen", _ollama({"acao": "pedido"}, "Saiu para entrega.", corpos)):
            app._ollama_suporte_responder("cadê meu pedido?", consumidor_id="c1", chat_id="ch1",
                                          pedidos_recentes_ctx="- Pedido #A1B2C3D4 Saiu para entrega")
        self.assertIn("A1B2C3D4", json.dumps(corpos[1]))

    def test_decisao_que_falha_vira_conversa(self):
        def fake(req, timeout=None):
            if json.loads(req.data.decode()).get("format") == "json":
                raise TimeoutError("lento")
            return _ollama({}, "Oi! Como posso ajudar?", [])(req, timeout)
        with patch.object(app, "_executar_ferramenta_suporte") as ferr, patch("urllib.request.urlopen", fake):
            texto, _, _ = app._ollama_suporte_responder("oi", consumidor_id="c1", chat_id="ch1")
        ferr.assert_not_called()
        self.assertEqual(texto, "Oi! Como posso ajudar?")

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
