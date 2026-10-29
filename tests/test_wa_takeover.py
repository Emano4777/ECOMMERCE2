"""Testes do atendimento humano por contato (takeover).

Unitarios rodam sempre. Integracao: schema temporario isolado no DATABASE_URL
(apagado no fim), IA e envio simulados -- nenhum WhatsApp sai. Inclui corrida
real com duas conexoes Postgres (webhook x worker) e trava de linha.
Rodar: python -m unittest tests.test_wa_takeover -v
"""
import os
import sys
import threading
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import poupaqui_wa_atendimento as wa  # noqa: E402
from poupaqui_ai_policy import Decision  # noqa: E402
from tests.test_wa_atendimento import TEST_PHONE, SchemaConn, payload  # noqa: E402

OUTRO = "5517988887777"


def saida(mid, phone=TEST_PHONE, text="Oi, aqui é o Emanuel, já vejo seu pedido"):
    p = payload(mid, phone=phone, text=text, from_me=True)
    del p["data"]["messages"]["key"]["cleanedSenderPn"]
    p["event"] = "messages.upsert"
    return p


class ParseOutboundTests(unittest.TestCase):
    def test_captura_so_from_me(self):
        self.assertEqual(wa.parse_outbound(payload("A")), [])  # recebida
        it = wa.parse_outbound(saida("B"))
        self.assertEqual((it[0]["telefone"], it[0]["message_id"]), (TEST_PHONE, "B"))
        self.assertEqual(wa.parse_inbound(saida("B")), [])  # inbound continua ignorando fromMe

    def test_ignora_grupo_status_e_sem_destino(self):
        g = saida("C"); g["data"]["messages"]["key"]["remoteJid"] = "123-456@g.us"
        s = saida("D"); s["data"]["messages"]["key"]["remoteJid"] = "status@broadcast"
        lid = saida("E"); lid["data"]["messages"]["key"]["remoteJid"] = "999999@lid"
        self.assertEqual(wa.parse_outbound(g) + wa.parse_outbound(s), [])
        # @lid sem telefone: vira item com lid, resolvido pelo cadastro do contato no store_outbound
        self.assertEqual([(i["telefone"], i["lid"]) for i in wa.parse_outbound(lid)], [("", "999999@lid")])
        self.assertEqual(wa.estrutura_fromme(lid)[0]["jid"], "lid")

    def test_message_sent_formato_oficial(self):
        # docs WA Sender (message.sent): key direto em data, conteudo em data.message, remoteJid "+55..."
        p = {"event": "message.sent", "data": {"key": {"id": "3EB0X1", "fromMe": True, "remoteJid": "+" + TEST_PHONE},
                                               "message": {"conversation": "Oi, aqui é o Emanuel"}}}
        it = wa.parse_outbound(p)
        self.assertEqual([(i["message_id"], i["telefone"], i["corpo"]) for i in it],
                         [("3EB0X1", TEST_PHONE, "Oi, aqui é o Emanuel")])
        p["data"]["key"]["fromMe"] = False
        self.assertEqual(wa.parse_outbound(p), [])

    def test_pedido_de_humano(self):
        for t in ("quero falar com um atendente", "Preciso falar com alguém, por favor",
                  "me passa pra uma pessoa"):
            self.assertTrue(wa._PEDIU_HUMANO_RE.search(wa.normalize(t)), t)
        for t in ("qual o corte da germed", "o atendente da germed me ligou"):
            self.assertFalse(wa._PEDIU_HUMANO_RE.search(wa.normalize(t)), t)


@unittest.skipUnless(os.getenv("DATABASE_URL") or os.path.exists(".env"), "sem banco")
class TakeoverIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        # so a conexao: o .env do piloto (alertas, OLLAMA_URL do tunel, modelo) nao pode vazar
        from dotenv import dotenv_values
        os.environ.setdefault("DATABASE_URL", dotenv_values(".env").get("DATABASE_URL") or "")
        cls.schema = "wa_ia_test_" + uuid.uuid4().hex[:8]
        # timeouts: o teste falha em vez de ficar esperando banco/trava indefinidamente
        opts = "-c statement_timeout=30000 -c lock_timeout=30000 -c idle_in_transaction_session_timeout=60000"
        cls.raw = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10, options=opts)
        cls.raw2 = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10, options=opts)
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
            cur.execute("""INSERT INTO fornecedores (nome, telefone, ativo, horario_corte, prazo_entrega_desc)
                           VALUES ('GERMED', '', TRUE, '20:00', '1 dia util')""")
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
        os.environ["POUPAQUI_WA_IA_TEST_PHONES"] = f"{TEST_PHONE},{OUTRO}"
        os.environ.pop("POUPAQUI_WA_IA_SILENCIO_MIN", None)
        with self.conn.cursor() as cur:
            cur.execute("""TRUNCATE ecommerce_wa_mensagens, ecommerce_wa_ia_decisoes, ecommerce_wa_contatos,
                           ecommerce_wa_ia_config, ecommerce_wa_atendimento_humano, ecommerce_wa_envios_auto,
                           pedido_status_whatsapp_log""")
        self.conn.commit()
        self.sent, self.calls = [], []
        for p in (TEST_PHONE, OUTRO):
            self.link(p)

    # helpers
    def link(self, phone):
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO ecommerce_wa_contatos (telefone, vinculo_manual_tipo, vinculo_manual_ref)
                           VALUES (%s,'interno','teste') ON CONFLICT (telefone) DO UPDATE
                           SET vinculo_manual_tipo='interno', vinculo_manual_ref='teste'""", (phone,))
        self.conn.commit()

    def receive(self, mid, phone=TEST_PHONE, text="Qual o horario de corte da Germed?"):
        return wa.store_inbound(payload(mid, phone=phone, text=text), self.conn)

    def age(self, seconds=120):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE ecommerce_wa_mensagens SET recebido_em = recebido_em - %s * INTERVAL '1 second'",
                        (seconds,))
        self.conn.commit()

    def classifier(self, hook=None):
        def fn(p):
            self.calls.append(p)
            if hook:
                hook()
            return Decision("ANSWER", "O horário de corte da Germed é 20:00.", "teste")
        return fn

    def sender(self, phone, text):
        self.sent.append((phone, text))
        return {"success": True, "data": {"msgId": f"{len(self.sent)}"}}

    def worker(self, classifier=None):
        wa.set_db_mode(self.conn, "test")
        return wa.run_worker(self.conn, classifier or self.classifier(), self.sender)

    def status_de(self, mid):
        with self.conn.cursor() as cur:
            cur.execute("SELECT status FROM ecommerce_wa_mensagens WHERE message_id = %s", (mid,))
            return cur.fetchone()[0]

    def ultima_decisao(self, phone=TEST_PHONE):
        with self.conn.cursor() as cur:
            cur.execute("""SELECT decisao, envio_status, origem, motivo FROM ecommerce_wa_ia_decisoes
                           WHERE telefone = %s ORDER BY id DESC LIMIT 1""", (phone,))
            r = cur.fetchone()
        self.conn.commit()
        return r

    # ── classificacao da saida ─────────────────────────────────────────────
    def test_manual_silencia_so_aquele_contato(self):
        self.assertEqual(wa.store_outbound(saida("OP1"), self.conn)["manual"], 1)
        self.assertIsNotNone(wa.takeover_ativo(self.conn, TEST_PHONE))
        self.assertIsNone(wa.takeover_ativo(self.conn, OUTRO))
        self.conn.commit()
        self.receive("M1"); self.receive("M2", phone=OUTRO); self.age()
        self.worker()
        self.assertEqual(self.sent, [(OUTRO, "O horário de corte da Germed é 20:00.")])
        self.assertEqual(self.status_de("M1"), "em_humano")
        self.assertEqual(self.ultima_decisao()[2], "humano")
        self.assertEqual(len(self.calls), 1)  # a IA nem foi consultada pro contato em atendimento

    def test_eco_da_propria_ia_nao_e_takeover(self):
        self.receive("M1"); self.age()
        self.worker()
        self.assertEqual(len(self.sent), 1)
        r = wa.store_outbound(saida("ECO1", text=self.sent[0][1]), self.conn)
        self.assertEqual(r, {"ia": 1, "sistema": 0, "manual": 0})
        self.assertIsNone(wa.takeover_ativo(self.conn, TEST_PHONE))
        self.conn.commit()

    def test_envios_automaticos_nao_sao_takeover(self):
        r1 = wa.store_outbound(saida("S1", text="[PoupAqui IA] Atendimento humano necessario #3"), self.conn)
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO pedido_status_whatsapp_log (sent_ok, response_body) VALUES
                           (TRUE, %s)""", ('{"success":true,"data":{"msgId":1,"jid":"+%s"}}' % TEST_PHONE,))
        self.conn.commit()
        r2 = wa.store_outbound(saida("S2", text="Seu pedido 123 foi faturado"), self.conn)
        wa.registrar_envio_automatico(self.conn, OUTRO, "Lembrete: retire sua encomenda", "lembrete")
        r3 = wa.store_outbound(saida("S3", phone=OUTRO, text="Lembrete:  retire sua encomenda"), self.conn)
        self.assertEqual([r1["sistema"], r2["sistema"], r3["sistema"]], [1, 1, 1])
        self.assertEqual(wa.atendimentos_humanos(self.conn), [])

    def test_idempotente(self):
        wa.store_outbound(saida("OP1"), self.conn)
        self.assertEqual(wa.store_outbound(saida("OP1"), self.conn), {"ia": 0, "sistema": 0, "manual": 0})
        self.assertEqual(len(wa.atendimentos_humanos(self.conn)), 1)

    # ── mensagens antigas e retomada ───────────────────────────────────────
    def test_nao_responde_mensagem_antiga_depois_da_janela(self):
        self.receive("M1")
        wa.store_outbound(saida("OP1"), self.conn)  # operador respondeu M1
        self.assertEqual(self.status_de("M1"), "respondido_humano")
        with self.conn.cursor() as cur:  # janela venceu
            cur.execute("UPDATE ecommerce_wa_atendimento_humano SET ate = NOW() - INTERVAL '1 minute'")
        self.conn.commit()
        self.age()
        self.worker()
        self.assertEqual(self.sent, [])  # M1 nao volta pra fila
        self.receive("M2", text="e da navarro?"); self.age()
        self.worker()
        self.assertEqual(len(self.sent), 1)  # mensagem nova depois da janela: IA volta sozinha

    def test_assumir_sem_prazo_e_devolver_explicito(self):
        wa.assumir_atendimento(self.conn, TEST_PHONE)
        wa.store_outbound(saida("OP1"), self.conn)  # manual nao encurta a trava sem prazo
        self.assertIsNone(wa.takeover_ativo(self.conn, TEST_PHONE)["ate"])
        self.conn.commit()
        self.receive("M1"); self.age()
        self.worker()
        self.assertEqual(self.sent, [])
        self.receive("M2"); self.age(5)  # chegou e ainda esta na fila quando o operador devolve
        self.assertEqual(wa.devolver_atendimento(self.conn, TEST_PHONE), 1)
        self.assertEqual(self.status_de("M2"), "respondido_humano")
        self.age()
        self.worker()
        self.assertEqual(self.sent, [])  # nada do periodo humano e respondido
        self.receive("M3"); self.age()
        self.worker()
        self.assertEqual(len(self.sent), 1)

    def test_janela_configuravel_e_renovada(self):
        os.environ["POUPAQUI_WA_IA_SILENCIO_MIN"] = "1"
        wa.store_outbound(saida("OP1"), self.conn)
        ate1 = wa.takeover_ativo(self.conn, TEST_PHONE)["ate"]
        self.conn.commit()
        time.sleep(1.1)
        wa.store_outbound(saida("OP2", text="mais uma"), self.conn)
        t = wa.takeover_ativo(self.conn, TEST_PHONE)
        self.conn.commit()
        self.assertGreater(t["ate"], ate1)
        with self.conn.cursor() as cur:
            cur.execute("SELECT EXTRACT(EPOCH FROM (ate - NOW())) FROM ecommerce_wa_atendimento_humano")
            self.assertLess(cur.fetchone()[0], 61)
        self.conn.commit()

    def test_contato_pede_humano(self):
        self.receive("M1", text="quero falar com um atendente"); self.age()
        self.worker()
        # sem POUPAQUI_WA_IA_ALERTA_PHONES: texto fixo honesto, nunca "ja chamei"
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], TEST_PHONE)
        self.assertTrue(self.sent[0][1].startswith("Claro! Daqui em diante quem responde é um atendente."))
        self.assertNotIn("Já chamei", self.sent[0][1])
        self.assertEqual(self.calls, [])  # texto fixo, sem modelo
        self.assertEqual(wa.takeover_ativo(self.conn, TEST_PHONE)["origem"], "pedido_contato")
        self.conn.commit()
        self.receive("M2", text="alo?"); self.age()
        self.worker()
        self.assertEqual(len(self.sent), 1)
        # o eco da frase fixa enviada pela IA nao vira 'manual'
        self.assertEqual(wa.store_outbound(saida("ECO", text=self.sent[0][1]), self.conn)["ia"], 1)

    # ── corrida ────────────────────────────────────────────────────────────
    def test_operador_responde_enquanto_ia_pensa(self):
        """Webhook do operador chega (outra conexao) durante a chamada ao modelo."""
        self.receive("M1"); self.age()
        hook = lambda: wa.store_outbound(saida("OP1"), self.conn2)  # noqa: E731
        self.worker(self.classifier(hook))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.ultima_decisao()[:3], ("SILENCE", "cancelado_humano", "humano"))
        self.assertEqual(self.status_de("M1"), "respondido_humano")

    def test_trava_serializa_webhook_e_worker(self):
        """Webhook segura a linha do contato (transacao aberta); o worker espera e,
        quando o webhook confirma, desiste do envio."""
        self.receive("M1"); self.age()
        cur2 = self.conn2.cursor()
        wa._lock_contact(cur2, TEST_PHONE)
        cur2.execute("""INSERT INTO ecommerce_wa_mensagens (message_id, telefone, direcao, corpo, status)
                        VALUES ('OPX', %s, 'out_manual', 'ja vejo', 'processado')""", (TEST_PHONE,))
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", self.worker()))
        t.start()
        time.sleep(2)
        self.assertTrue(t.is_alive(), "worker deveria estar bloqueado na trava do contato")
        self.assertEqual(self.sent, [])
        self.conn2.commit()
        t.join(30)
        self.assertFalse(t.is_alive())
        self.assertEqual(self.sent, [])
        self.assertEqual(out["r"]["resultados"][0]["envio"], "cancelado_humano")

    def test_ia_confirma_antes_do_operador_envia_uma_vez(self):
        """Ordem inversa: decisao da IA ja gravada -> envia; o manual que chega
        depois silencia as proximas, sem duplicar nada."""
        self.receive("M1"); self.age()
        self.worker()
        self.assertEqual(len(self.sent), 1)
        wa.store_outbound(saida("OP1", text="complementando: chega amanha"), self.conn)
        self.receive("M2", text="obrigado, e a navarro?"); self.age()
        self.worker()
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
