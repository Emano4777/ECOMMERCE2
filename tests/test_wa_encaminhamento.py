"""Encaminhamento a equipe: a IA so diz "ja passei para a equipe" com alerta ENTREGUE.

Unitarios rodam sempre (texto do alerta, mascara, frases). Integracao: schema
temporario isolado no DATABASE_URL (apagado no fim), assistente e envio
simulados -- nenhum WhatsApp sai, nada de producao e alterado. Conexoes com
connect_timeout e statement/lock timeout para nao ficar presa esperando banco.
Rodar: python -m unittest tests.test_wa_encaminhamento -v
"""
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import poupaqui_ai_assistente as A  # noqa: E402
import poupaqui_wa_atendimento as wa  # noqa: E402
from poupaqui_ai_policy import Decision  # noqa: E402
from tests.test_wa_atendimento import TEST_PHONE, SchemaConn, payload  # noqa: E402
from tests.test_wa_takeover import saida  # noqa: E402

EQUIPE = "5517911112222"
LOJA = {"verificada": True, "metodo": "telefone", "tipo": "loja", "ref": "11111111000111", "nome": "LOJA UM LTDA"}
TIMEOUTS = "-c statement_timeout=30000 -c lock_timeout=30000 -c idle_in_transaction_session_timeout=60000"


class AlertaTextoTests(unittest.TestCase):
    def test_resumo_tecnico_completo_e_sem_dado_sensivel(self):
        resumo = {"assunto": "Pedido Eletrônico: enviar/finalizar pedido",
                  "sintomas": ["ação: enviar/finalizar pedido", "erro relatado: \"erro 500\"", "já tentou: atualizou"],
                  "checagens": ["sonda_http:pedido_eletronico -> 200 em 0.4s", "pedidos3: 7 pedidos/60min de 4 lojas"],
                  "motivo_escalacao": "falha persiste apos orientacao",
                  "relato_anterior": ["botao enviar pedido da erro"]}
        txt = wa.alert_text(42, "5517988881111", Decision("ANSWER", "x", "m"), "assistente", LOJA, None,
                            ["continua, meu cpf 123.456.789-09 senha: abc123 email x@y.com"], resumo)
        self.assertTrue(txt.startswith("[PoupAqui IA]"))       # webhook classifica como 'sistema'
        for esperado in ("#42", "LOJA UM LTDA", "CNPJ 11111111000111", "Assunto: Pedido Eletrônico",
                         "erro 500", "200 em 0.4s", "7 pedidos/60min", "falha persiste", "botao enviar pedido"):
            self.assertIn(esperado, txt)
        for proibido in ("123.456.789-09", "abc123", "x@y.com"):
            self.assertNotIn(proibido, txt)

    def test_pediu_humano_so_diz_que_chamou_com_alerta_entregue(self):
        self.assertIn("Já chamei", wa.pediu_humano_reply("enviado", 9))
        self.assertIn("Já chamei", wa.pediu_humano_reply("parcial", 9))
        for st in (None, "falhou", "indisponivel"):
            txt = wa.pediu_humano_reply(st, 9)
            self.assertNotIn("Já chamei", txt)
            self.assertIn("Não consegui avisar", txt)
            self.assertIn("protocolo 9", txt)


def fake_assistente(reply="Verifiquei agora: a página abre normalmente. Como continua, precisa de alguém olhando.",
                    escalar=True, acao="ANSWER", calls=None):
    def fn(conn, textos, tipos, identidade, historico):
        if calls is not None:
            calls.append({"textos": textos, "identidade": identidade, "historico": historico})
        return A.Resposta(acao, reply if acao == "ANSWER" else "", "teste", escalar, "pe_fora",
                          fontes=["sonda_http:pedido_eletronico -> 200 em 0.4s"], equipe="equipe técnica",
                          resumo={"assunto": "Pedido Eletrônico: enviar/finalizar pedido",
                                  "sintomas": ["erro relatado: \"erro 500\""]})
    return fn


@unittest.skipUnless(os.getenv("DATABASE_URL") or os.path.exists(".env"), "sem banco")
class EncaminhamentoIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        # so a conexao: o .env do piloto (alertas, OLLAMA_URL do tunel, modelo) nao pode vazar
        from dotenv import dotenv_values
        os.environ.setdefault("DATABASE_URL", dotenv_values(".env").get("DATABASE_URL") or "")
        cls.schema = "wa_ia_test_" + uuid.uuid4().hex[:8]
        cls.raw = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10, options=TIMEOUTS)
        cls.raw2 = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10, options=TIMEOUTS)
        with cls.raw.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {cls.schema}")
        cls.raw.commit()
        cls.conn = SchemaConn(cls.raw, cls.schema)
        cls.conn2 = SchemaConn(cls.raw2, cls.schema)
        with cls.conn.cursor() as cur:
            cur.execute("""CREATE TABLE users (cnpjloja TEXT, usuario TEXT, razao TEXT, telefone TEXT);
                           CREATE TABLE fornecedores (id BIGSERIAL, nome TEXT, cnpj CHAR(14), telefone TEXT,
                               ativo BOOLEAN, horario_corte TIME, prazo_entrega_horas INT,
                               prazo_entrega_desc TEXT, obs_entrega TEXT);
                           CREATE TABLE pedido_status_whatsapp_log (id BIGSERIAL, sent_ok BOOLEAN,
                               response_body TEXT, created_at TIMESTAMPTZ DEFAULT NOW())""")
            cur.execute("INSERT INTO users VALUES ('11111111000111','loja1','LOJA UM LTDA','(17) 99143-6759')")
        cls.conn.commit()
        wa.ensure_schema(cls.conn, force=True)

    @classmethod
    def tearDownClass(cls):
        cls.raw.rollback(); cls.raw2.rollback()
        with cls.raw.cursor() as cur:
            cur.execute(f"DROP SCHEMA {cls.schema} CASCADE")
        cls.raw.commit()
        cls.raw.close(); cls.raw2.close()

    def setUp(self):
        os.environ["POUPAQUI_WA_IA_MAX_MODE"] = "test"
        os.environ["POUPAQUI_WA_IA_TEST_PHONES"] = f"{TEST_PHONE},{EQUIPE}"
        os.environ["POUPAQUI_WA_IA_ALERTA_PHONES"] = EQUIPE
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in
                                 ("POUPAQUI_WA_IA_ALERTA_PHONES", "POUPAQUI_WA_IA_ACK_HUMANO")])
        with self.conn.cursor() as cur:
            cur.execute("""TRUNCATE ecommerce_wa_mensagens, ecommerce_wa_ia_decisoes, ecommerce_wa_contatos,
                           ecommerce_wa_ia_config, ecommerce_wa_atendimento_humano, ecommerce_wa_envios_auto""")
        self.conn.commit()
        self.sent = []
        self.falhar_para = set()

    # helpers
    def receive(self, mid, text="o pedido eletronico continua dando erro"):
        wa.store_inbound(payload(mid, phone=TEST_PHONE, text=text), self.conn)
        with self.conn.cursor() as cur:
            cur.execute("UPDATE ecommerce_wa_mensagens SET recebido_em = recebido_em - INTERVAL '2 minutes'")
        self.conn.commit()

    def sender(self, phone, text):
        self.sent.append((phone, text))
        if phone in self.falhar_para:
            raise RuntimeError("WA Sender HTTP 500")
        return {"success": True, "data": {"msgId": f"out-{len(self.sent)}"}}

    def worker(self, assistente, mode="test", sender=None):
        wa.set_db_mode(self.conn, mode)
        return wa.run_worker(self.conn, None, sender or self.sender, assistente=assistente)

    def decisao(self):
        with self.conn.cursor() as cur:
            cur.execute("""SELECT decisao, envio_status, alerta_status, resposta, escalado, resumo, origem, id
                           FROM ecommerce_wa_ia_decisoes ORDER BY id DESC LIMIT 1""")
            r = cur.fetchone()
        self.conn.commit()
        return r

    # ── alerta entregue -> so entao "ja passei" ─────────────────────────────
    def test_alerta_sai_antes_e_resposta_confirma_com_protocolo(self):
        self.receive("M1")
        res = self.worker(fake_assistente())
        self.assertEqual(res["resultados"][0]["alerta"], "enviado")
        self.assertEqual([p for p, _ in self.sent], [EQUIPE, TEST_PHONE])   # alerta ANTES da resposta
        alerta, resposta = self.sent[0][1], self.sent[1][1]
        self.assertIn("Assunto: Pedido Eletrônico", alerta)
        self.assertIn("erro 500", alerta)
        self.assertIn("sonda_http", alerta)
        self.assertIn("LOJA UM LTDA", alerta)                              # identidade verificada pelo telefone
        d = self.decisao()
        self.assertIn("Já passei para a equipe técnica", resposta)
        self.assertIn(f"protocolo {d[7]}", resposta)
        self.assertEqual((d[0], d[1], d[2], d[4]), ("ANSWER", "enviado", "enviado", True))
        self.assertEqual(d[3], resposta)                                   # auditoria = o que saiu
        # eco da resposta (com protocolo) volta pelo webhook como 'ia', nao como operador
        self.assertEqual(wa.store_outbound(saida("ECO", text=resposta), self.conn)["ia"], 1)
        self.assertEqual(wa.store_outbound(saida("ECO2", phone=EQUIPE, text=alerta), self.conn)["sistema"], 1)

    def test_alerta_falhou_resposta_honesta(self):
        self.falhar_para = {EQUIPE}
        self.receive("M1")
        res = self.worker(fake_assistente())
        self.assertEqual(res["resultados"][0]["alerta"], "falhou")
        resposta = self.sent[-1][1]
        self.assertEqual(self.sent[-1][0], TEST_PHONE)
        self.assertNotIn("Já passei", resposta)
        self.assertIn("não consegui avisar", resposta)
        self.assertEqual(self.decisao()[2], "falhou")

    def test_sem_canal_configurado_resposta_honesta(self):
        os.environ.pop("POUPAQUI_WA_IA_ALERTA_PHONES")
        self.receive("M1")
        self.worker(fake_assistente())
        self.assertEqual([p for p, _ in self.sent], [TEST_PHONE])
        self.assertIn("não consegui avisar", self.sent[0][1])
        d = self.decisao()
        self.assertIsNone(d[2])
        self.assertTrue(d[4])                                              # fica na fila de pendencias
        self.assertEqual(len(wa.pending_human(self.conn)), 1)
        self.assertEqual(wa.pending_human(self.conn)[0]["assunto"], "Pedido Eletrônico: enviar/finalizar pedido")

    def test_shadow_nao_envia_nada_e_audita_texto_honesto(self):
        self.receive("M1")
        self.worker(fake_assistente(), mode="shadow")
        self.assertEqual(self.sent, [])
        d = self.decisao()
        self.assertEqual((d[1], d[2]), ("shadow", None))
        self.assertIn("não consegui avisar", d[3])

    def test_dedupe_nao_realerta_e_cita_alerta_anterior(self):
        self.receive("M1")
        self.worker(fake_assistente())
        primeiro = self.decisao()[7]
        self.receive("M2", text="e ai, alguma novidade? continua")
        self.worker(fake_assistente())
        self.assertEqual([p for p, _ in self.sent], [EQUIPE, TEST_PHONE, TEST_PHONE])  # 1 alerta so
        self.assertIn(f"já recebeu esse caso", self.sent[-1][1])
        self.assertIn(f"protocolo {primeiro}", self.sent[-1][1])

    def test_resolvido_sem_escalar_nao_alerta(self):
        self.receive("M1", text="qual o corte da germed?")
        self.worker(fake_assistente(reply="O horário de corte da GERMED é 20:00.", escalar=False))
        self.assertEqual(self.sent, [(TEST_PHONE, "O horário de corte da GERMED é 20:00.")])
        d = self.decisao()
        self.assertEqual((d[2], d[4]), (None, False))
        self.assertEqual(wa.pending_human(self.conn), [])

    def test_assistente_que_promete_encaminhamento_e_barrado(self):
        self.receive("M1")
        self.worker(fake_assistente(reply="Já avisei a equipe técnica, que vai olhar.", escalar=False))
        d = self.decisao()
        self.assertEqual((d[0], d[6]), ("HUMAN", "falha_ia"))
        self.assertNotIn(TEST_PHONE, [p for p, _ in self.sent])           # nada saiu para o contato

    def test_contato_pede_humano_sem_canal_nao_diz_que_chamou(self):
        os.environ.pop("POUPAQUI_WA_IA_ALERTA_PHONES")
        self.receive("M1", text="quero falar com um atendente")
        self.worker(fake_assistente())
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("Já chamei", self.sent[0][1])
        self.assertIn("Não consegui avisar", self.sent[0][1])
        self.assertIsNotNone(wa.takeover_ativo(self.conn, TEST_PHONE))
        self.conn.commit()

    def test_contato_pede_humano_com_alerta_entregue(self):
        self.receive("M1", text="quero falar com um atendente")
        self.worker(fake_assistente())
        self.assertEqual([p for p, _ in self.sent], [EQUIPE, TEST_PHONE])
        self.assertIn("Já chamei alguém da equipe", self.sent[1][1])

    # ── takeover humano ─────────────────────────────────────────────────────
    def test_takeover_ativo_assistente_nem_e_chamado(self):
        calls = []
        wa.store_outbound(saida("OP1"), self.conn)
        self.receive("M1")
        self.worker(fake_assistente(calls=calls))
        self.assertEqual((calls, self.sent), ([], []))

    def test_operador_responde_durante_o_alerta_resposta_cancelada(self):
        """O alerta leva ~1s; se o operador respondeu nesse meio-tempo, a IA nao fala junto."""
        self.receive("M1")

        def sender(phone, text):
            self.sent.append((phone, text))
            if phone == EQUIPE:  # webhook do operador chega por outra conexao enquanto o alerta sai
                wa.store_outbound(saida("OP1", text="deixa comigo, ja vejo"), self.conn2)
            return {"success": True, "data": {"msgId": "x"}}
        res = self.worker(fake_assistente(), sender=sender)
        self.assertEqual([p for p, _ in self.sent], [EQUIPE])
        self.assertEqual(res["resultados"][0]["envio"], "cancelado_humano")
        self.assertEqual(self.decisao()[1], "cancelado_humano")

    # ── shadow do servidor com o assistente novo: so leitura ───────────────
    def test_shadow_servidor_assistente_nao_grava_nem_envia(self):
        from tests.test_wa_ollama import _load_script
        from tests.test_wa_suporte import FakeRepoSuporte, sonda_ok
        shadow = _load_script("wa_ia_shadow_servidor")
        self.receive("M1", text="o pedido eletronico nao funciona")
        seqs = shadow.last_sequences(self.conn, wa.test_phones(), 24)
        self.conn.rollback()
        repo = FakeRepoSuporte()
        res = shadow.shadow_assistente(self.conn, TEST_PHONE, seqs[TEST_PHONE], sonda=sonda_ok,
                                       repo_factory=lambda conn: repo)
        self.assertEqual((res["decisao"], res["escalar"], res["verificado"]), ("ANSWER", False, True))
        self.assertIn("O que acontece aí", res["resposta"])
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ecommerce_wa_ia_decisoes")
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT DISTINCT status FROM ecommerce_wa_mensagens")
            self.assertEqual(cur.fetchall(), [("pendente",)])
        self.conn.rollback()
        self.assertEqual(self.sent, [])

    # ── ponta a ponta com o assistente real (repo em memoria) ──────────────
    def test_ponta_a_ponta_assistente_real(self):
        from tests.test_wa_suporte import FakeRepoSuporte, sonda_ok
        repo = FakeRepoSuporte()
        assistente = A.assistente_worker(sonda=sonda_ok, repo_factory=lambda conn: repo)
        self.receive("M1", text="o pedido eletronico nao funciona")
        self.worker(assistente)
        self.assertEqual([p for p, _ in self.sent], [TEST_PHONE])            # 1o relato: pergunta, nao escala
        self.assertIn("O que acontece aí", self.sent[0][1])
        self.receive("M2", text="quando clico em enviar o pedido da germed aparece erro 500")
        self.worker(assistente)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Ctrl+F5", self.sent[1][1])
        self.receive("M3", text="continua")
        self.worker(assistente)
        self.assertEqual([p for p, _ in self.sent[2:]], [EQUIPE, TEST_PHONE])
        alerta, resposta = self.sent[2][1], self.sent[3][1]
        self.assertIn("erro 500", alerta)
        self.assertIn("Relato anterior:", alerta)
        self.assertIn("quando clico em enviar", alerta)
        self.assertIn("Já passei para a equipe técnica", resposta)


if __name__ == "__main__":
    unittest.main()
