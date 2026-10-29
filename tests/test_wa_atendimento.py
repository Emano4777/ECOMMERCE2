"""Testes do atendimento WhatsApp IA.

Unitarios rodam sempre. Os de integracao usam o DATABASE_URL num schema
temporario isolado (users/fornecedores falsos), apagado no final; IA e envio
sao simulados -- nenhum WhatsApp sai e nenhuma chamada a Anthropic e feita.
Rodar: python -m unittest tests.test_wa_atendimento -v
"""
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import poupaqui_wa_atendimento as wa  # noqa: E402
from poupaqui_ai_policy import (  # noqa: E402
    Decision, preliminary_gate, safe_decision, validate_answer, is_opt_out,
)

# trava de lock vira erro em 10s em vez de pendurar a suite
TIMEOUTS_DB = "-c statement_timeout=30000 -c lock_timeout=10000 -c idle_in_transaction_session_timeout=60000"
TEST_PHONE = "5517991436759"


def payload(mid, phone=TEST_PHONE, text="oi", from_me=False, jid=None, push="Fulano", message=None):
    return {"event": "messages.received", "data": {"messages": {
        "key": {"id": mid, "fromMe": from_me, "remoteJid": jid or f"{phone}@s.whatsapp.net",
                "cleanedSenderPn": phone},
        "pushName": push, "messageBody": text, "messageTimestamp": 1761700000,
        "message": message if message is not None else {"conversation": text},
    }}}


class ParseTests(unittest.TestCase):
    def test_private_message(self):
        items = wa.parse_inbound(payload("A1", text="Qual o horario de corte?"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["telefone"], TEST_PHONE)
        self.assertEqual(items[0]["tipo"], "text")
        self.assertEqual(items[0]["nome_whatsapp"], "Fulano")

    def test_imagem_guarda_metadados_para_download(self):
        img = {"imageMessage": {"url": "https://mmg.whatsapp.net/x", "mimetype": "image/jpeg", "mediaKey": "K==",
                                "fileSha256": "S==", "fileLength": "1234", "caption": "olha o erro", "jpegThumbnail": "T"}}
        it = wa.parse_inbound(payload("I1", text="olha o erro", message=img))[0]
        self.assertEqual(it["tipo"], "image")
        self.assertEqual(it["midia"], {"campo": "imageMessage", "url": "https://mmg.whatsapp.net/x",
                                       "mimetype": "image/jpeg", "mediaKey": "K==", "fileSha256": "S==",
                                       "fileLength": "1234"})
        pdf = {"documentMessage": {"url": "u", "mimetype": "application/pdf", "mediaKey": "K"}}
        self.assertIsNone(wa.parse_inbound(payload("I2", message=pdf))[0]["midia"])
        self.assertIsNone(wa.parse_inbound(payload("I3"))[0]["midia"])

    def test_audio_guarda_metadados_e_vira_fala(self):
        aud = {"audioMessage": {"url": "https://mmg.whatsapp.net/a", "mimetype": "audio/ogg; codecs=opus",
                                "mediaKey": "K==", "seconds": 12, "ptt": True}}
        it = wa.parse_inbound(payload("AU1", text="", message=aud))[0]
        self.assertEqual(it["tipo"], "audio")
        self.assertEqual(it["midia"]["campo"], "audioMessage")
        self.assertEqual(it["midia"]["seconds"], 12)
        self.assertEqual(wa.texto_da_midia({"audio": True, "texto": "meu pedido nao chegou"}),
                         "[Áudio transcrito] meu pedido nao chegou")
        self.assertIn("só o começo", wa.texto_da_midia({"audio": True, "texto": "x", "cortado": True}))

    def test_ignores_from_me_groups_channels_status(self):
        self.assertEqual(wa.parse_inbound(payload("A", from_me=True)), [])
        self.assertEqual(wa.parse_inbound(payload("B", jid="123-456@g.us")), [])
        self.assertEqual(wa.parse_inbound(payload("C", jid="123@newsletter")), [])
        self.assertEqual(wa.parse_inbound(payload("D", jid="status@broadcast")), [])

    def test_missing_from_me_is_ignored(self):
        p = payload("E")
        del p["data"]["messages"]["key"]["fromMe"]
        self.assertEqual(wa.parse_inbound(p), [])

    def test_reaction_ignored_and_media_kind(self):
        self.assertEqual(wa.parse_inbound(payload("F", message={"reactionMessage": {"text": "👍"}})), [])
        items = wa.parse_inbound(payload("G", text="", message={"audioMessage": {}}))
        self.assertEqual(items[0]["tipo"], "audio")

    def test_lid_uses_sender_pn(self):
        p = payload("H", jid="99999@lid")
        self.assertEqual(wa.parse_inbound(p)[0]["telefone"], TEST_PHONE)

    def test_phone_key_tolerates_ninth_digit(self):
        self.assertEqual(wa.phone_key("5517991436759"), wa.phone_key("(17) 9143-6759"))
        self.assertEqual(wa.phone_key("(17) 99143-6759"), "1791436759")


class PolicyTests(unittest.TestCase):
    def test_gates(self):
        self.assertEqual(preliminary_gate(["Bom dia!"]).decision, "SILENCE")
        self.assertEqual(preliminary_gate(["ok", "obrigado"]).decision, "SILENCE")
        self.assertEqual(preliminary_gate(["O boleto venceu, como pago?"]).decision, "HUMAN")
        self.assertEqual(preliminary_gate(["posso tomar esse remédio?"]).decision, "HUMAN")
        self.assertEqual(preliminary_gate([""], ["audio"]).decision, "HUMAN")
        self.assertIsNone(preliminary_gate(["Qual o horário de corte da Germed?"]))
        self.assertIsNone(preliminary_gate(["Teste CRM 2"]))

    def test_opt_out(self):
        self.assertTrue(is_opt_out(" PARAR "))
        self.assertFalse(is_opt_out("vou parar na loja"))

    def test_safe_decision_fail_closed(self):
        self.assertEqual(safe_decision("not json").decision, "HUMAN")
        self.assertEqual(safe_decision('{"decision":"ANSWER","reply":"","reason":"x"}').decision, "HUMAN")
        self.assertEqual(safe_decision('{"decision":"MAYBE"}').decision, "HUMAN")
        d = safe_decision('{"decision":"SILENCE","reply":"oi","reason":"aviso"}')
        self.assertEqual((d.decision, d.reply), ("SILENCE", ""))

    def test_validate_answer(self):
        ok = Decision("ANSWER", "O horário de corte da Germed é 20:00.", "dado verificado")
        self.assertEqual(validate_answer(ok, ["corte germed?"], True).decision, "ANSWER")
        self.assertEqual(validate_answer(ok, ["corte germed?"], False).decision, "HUMAN")
        link = Decision("ANSWER", "Veja em https://x.com", "r")
        self.assertEqual(validate_answer(link, ["?"], True).decision, "HUMAN")
        money = Decision("ANSWER", "Custa R$ 10", "r")
        self.assertEqual(validate_answer(money, ["?"], True).decision, "HUMAN")
        pedido = Decision("ANSWER", "Seu pedido 123456 foi enviado", "r")
        self.assertEqual(validate_answer(pedido, ["?"], True).decision, "HUMAN")

    def test_effective_mode(self):
        self.assertEqual(wa.effective_mode("live", "test"), "test")
        self.assertEqual(wa.effective_mode("shadow", "live"), "shadow")
        self.assertEqual(wa.effective_mode("live", "off"), "off")
        self.assertEqual(wa.effective_mode("lixo", "live"), "off")
        self.assertEqual(wa.effective_mode("live", "lixo"), "off")


class SchemaConn:
    """Conexao que fixa o search_path com SET LOCAL a cada transacao. SET de
    sessao NAO pode ser usado: o DATABASE_URL e o pooler do Supabase em modo
    transacao, e um SET de sessao poderia vazar para backends de producao."""

    def __init__(self, real, schema):
        self.real, self.schema = real, schema

    def cursor(self, *a, **kw):
        import psycopg2.extensions as ext
        cur = self.real.cursor(*a, **kw)
        if self.real.status == ext.STATUS_READY:
            cur.execute(f"SET LOCAL search_path TO {self.schema}")
        return cur

    def __getattr__(self, name):
        return getattr(self.real, name)


@unittest.skipUnless(os.getenv("DATABASE_URL") or os.path.exists(".env"), "sem banco")
class WorkerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        # so a conexao: o .env do piloto (alertas, OLLAMA_URL do tunel, modelo) nao pode vazar
        from dotenv import dotenv_values
        os.environ.setdefault("DATABASE_URL", dotenv_values(".env").get("DATABASE_URL") or "")
        cls.schema = "wa_ia_test_" + uuid.uuid4().hex[:8]
        cls.raw = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10,
                                   options=TIMEOUTS_DB)
        with cls.raw.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {cls.schema}")
        cls.raw.commit()
        cls.conn = SchemaConn(cls.raw, cls.schema)
        with cls.conn.cursor() as cur:
            cur.execute("""CREATE TABLE users (cnpjloja TEXT, usuario TEXT, razao TEXT, telefone TEXT);
                           CREATE TABLE fornecedores (id BIGSERIAL, nome TEXT, cnpj CHAR(14), telefone TEXT,
                               ativo BOOLEAN, horario_corte TIME, prazo_entrega_horas INT,
                               prazo_entrega_desc TEXT, obs_entrega TEXT)""")
            cur.execute("""INSERT INTO users VALUES
                ('11111111000111','loja1','LOJA UM LTDA','(17) 98888-1111'),
                ('22222222000122','loja2','LOJA DOIS','(17) 97777-2222'),
                ('33333333000133','loja3','LOJA TRES','(17) 97777-2222')""")
            cur.execute("""INSERT INTO fornecedores (nome, telefone, ativo, horario_corte, prazo_entrega_desc)
                VALUES ('GERMED','11 96666-3333',TRUE,'20:00','1 dia util'),
                       ('MEDLEY','',TRUE,NULL,NULL)""")
        cls.conn.commit()
        wa.ensure_schema(cls.conn, force=True)

    @classmethod
    def tearDownClass(cls):
        cls.raw.rollback()
        with cls.raw.cursor() as cur:
            cur.execute(f"DROP SCHEMA {cls.schema} CASCADE")
        cls.raw.commit()
        cls.raw.close()

    def setUp(self):
        os.environ["POUPAQUI_WA_IA_MAX_MODE"] = "test"
        os.environ["POUPAQUI_WA_IA_TEST_PHONES"] = TEST_PHONE
        with self.conn.cursor() as cur:
            cur.execute("""TRUNCATE ecommerce_wa_mensagens, ecommerce_wa_ia_decisoes,
                           ecommerce_wa_contatos, ecommerce_wa_ia_config""")
        self.conn.commit()
        self.calls, self.sent = [], []

    # helpers
    def receive(self, mid, phone=TEST_PHONE, text="Qual o horario de corte da Germed?", **kw):
        return wa.store_inbound(payload(mid, phone=phone, text=text, **kw), self.conn)

    def age(self, seconds=120):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE ecommerce_wa_mensagens SET recebido_em = recebido_em - %s * INTERVAL '1 second'",
                        (seconds,))
        self.conn.commit()

    def link(self, phone, tipo="interno", ref="teste"):
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO ecommerce_wa_contatos (telefone, vinculo_manual_tipo, vinculo_manual_ref)
                           VALUES (%s,%s,%s) ON CONFLICT (telefone) DO UPDATE
                           SET vinculo_manual_tipo=EXCLUDED.vinculo_manual_tipo,
                               vinculo_manual_ref=EXCLUDED.vinculo_manual_ref""", (phone, tipo, ref))
        self.conn.commit()

    def classifier(self, decision="ANSWER", reply="O horário de corte da Germed é 20:00."):
        def fn(p):
            self.calls.append(p)
            return Decision(decision, reply if decision == "ANSWER" else "", "teste")
        return fn

    def sender(self, ok=True):
        def fn(phone, text):
            self.sent.append((phone, text))
            return {"success": True, "data": {"msgId": f"out-{len(self.sent)}"}} if ok else False
        return fn

    def run_worker(self, mode, classifier=None, sender=None):
        wa.set_db_mode(self.conn, mode)
        return wa.run_worker(self.conn, classifier or self.classifier(), sender or self.sender())

    def decisions(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT decisao, envio_status, origem, motivo FROM ecommerce_wa_ia_decisoes ORDER BY id")
            return cur.fetchall()

    # tests
    def test_webhook_idempotent(self):
        self.assertEqual(self.receive("M1"), 1)
        self.assertEqual(self.receive("M1"), 0)
        self.assertEqual(self.receive("M2", from_me=True), 0)

    def test_off_mode_does_nothing(self):
        self.receive("M1"); self.age()
        res = self.run_worker("off")
        self.assertEqual(res["contatos"], 0)
        self.assertEqual(self.calls, [])

    def test_shadow_never_sends(self):
        self.link(TEST_PHONE)
        self.receive("M1"); self.age()
        res = self.run_worker("shadow")
        self.assertEqual(res["resultados"][0]["envio"], "shadow")
        self.assertEqual(self.sent, [])

    def test_test_mode_sends_once_to_allowlisted_verified(self):
        self.link(TEST_PHONE)
        self.receive("M1"); self.age()
        res = self.run_worker("test")
        self.assertEqual(res["resultados"][0]["envio"], "enviado")
        self.assertEqual(len(self.sent), 1)
        ctx = self.calls[0]["dados_verificados"]["fornecedores_entrega"]
        self.assertEqual(ctx[0]["horario_corte_pedido"], "20:00")
        # reprocessar e reentregar o mesmo webhook nao duplica envio
        self.receive("M1"); self.age()
        self.run_worker("test")
        self.assertEqual(len(self.sent), 1)
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ecommerce_wa_mensagens WHERE direcao='out_ia'")
            self.assertEqual(cur.fetchone()[0], 1)

    def test_imagem_lida_vira_texto_e_chave_some(self):
        self.link(TEST_PHONE)
        img = {"imageMessage": {"url": "https://mmg.whatsapp.net/x", "mimetype": "image/png", "mediaKey": "K=="}}
        self.receive("IMG1", text="", message=img); self.age()
        lidos = []

        def leitor(mid, midia):
            lidos.append((mid, midia["mediaKey"]))
            return {"texto": "Erro ao enviar pedido HTTP 500", "resumo": "print de erro do Pedido Eletronico"}
        wa.set_db_mode(self.conn, "test")
        wa.run_worker(self.conn, self.classifier(), self.sender(), leitor_midia=leitor)
        self.assertEqual(lidos, [("IMG1", "K==")])
        nova = self.calls[0]["mensagens_novas"][-1]
        self.assertEqual(nova["tipo"], "text")
        self.assertIn("Erro ao enviar pedido HTTP 500", nova["texto"])
        with self.conn.cursor() as cur:
            cur.execute("SELECT tipo, corpo, midia FROM ecommerce_wa_mensagens WHERE message_id = 'IMG1'")
            tipo, corpo, midia = cur.fetchone()
        self.conn.commit()
        self.assertEqual(tipo, "image")
        self.assertTrue(corpo.startswith("[Imagem enviada pelo contato]"))
        self.assertIsNone(midia)   # chave de criptografia nao fica guardada

    def test_imagem_sem_leitor_continua_midia(self):
        self.link(TEST_PHONE)
        img = {"imageMessage": {"url": "u", "mimetype": "image/png", "mediaKey": "K=="}}
        self.receive("IMG2", text="", message=img); self.age()
        wa.set_db_mode(self.conn, "test")
        wa.run_worker(self.conn, self.classifier(), self.sender(),
                      leitor_midia=lambda mid, m: (_ for _ in ()).throw(RuntimeError("falhou")))
        with self.conn.cursor() as cur:
            cur.execute("SELECT corpo, midia FROM ecommerce_wa_mensagens WHERE message_id = 'IMG2'")
            corpo, midia = cur.fetchone()
        self.conn.commit()
        self.assertEqual((corpo, midia), ("", None))

    def test_lid_do_contato_resolve_mensagem_manual(self):
        # recebida traz LID + telefone; a enviada (operador) vem so com o LID
        self.receive("L1", text="oi", jid="123456789@lid")
        with self.conn.cursor() as cur:
            cur.execute("SELECT lid FROM ecommerce_wa_contatos WHERE telefone = %s", (TEST_PHONE,))
            self.assertEqual(cur.fetchone()[0], "123456789@lid")
        self.conn.commit()
        saida = {"event": "messages.upsert", "data": {"messages": {
            "key": {"id": "OUT1", "fromMe": True, "remoteJid": "123456789@lid"},
            "message": {"conversation": "Oi, aqui é o Emanuel, já vejo pra você"}}}}
        self.assertEqual(wa.store_outbound(saida, self.conn)["manual"], 1)
        self.assertIsNotNone(wa.takeover_ativo(self.conn, TEST_PHONE))
        self.conn.commit()
        desconhecido = {"event": "messages.upsert", "data": {"messages": {
            "key": {"id": "OUT2", "fromMe": True, "remoteJid": "987654321@lid"},
            "message": {"conversation": "oi"}}}}
        self.assertEqual(wa.store_outbound(desconhecido, self.conn).get("sem_telefone"), 1)

    def test_midia_que_falha_junto_com_texto_responde_o_resto(self):
        self.link(TEST_PHONE)
        aud = {"audioMessage": {"url": "u", "mimetype": "audio/ogg; codecs=opus", "mediaKey": "K=="}}
        self.receive("MIX1", text="", message=aud)
        self.receive("MIX2", text="Qual o horario de corte da Germed?"); self.age()
        wa.set_db_mode(self.conn, "test")
        wa.run_worker(self.conn, self.classifier(), self.sender(),
                      leitor_midia=lambda mid, m: (_ for _ in ()).throw(RuntimeError("falhou")))
        novas = self.calls[0]["mensagens_novas"]
        self.assertEqual([n["tipo"] for n in novas], ["text", "text"])   # audio nao derruba o lote
        self.assertEqual(novas[-1]["texto"], "Qual o horario de corte da Germed?")

    def test_test_mode_blocks_non_allowlisted(self):
        # em test, contato fora da allowlist nem entra no worker: fica pendente, sem IA nem alerta
        self.receive("M1", phone="5517988881111"); self.age()   # loja verificada, fora da allowlist
        res = self.run_worker("test")
        self.assertEqual(res["resultados"], [])
        self.assertEqual((self.sent, self.calls, self.decisions()), ([], [], []))
        with self.conn.cursor() as cur:
            cur.execute("SELECT status FROM ecommerce_wa_mensagens WHERE message_id = 'M1'")
            self.assertEqual(cur.fetchone()[0], "pendente")
        # mensagem velha de fora da allowlist tambem nao e expirada pelo worker de test
        self.age(10 ** 6)
        self.assertEqual(self.run_worker("test")["expiradas"], 0)

    def test_env_ceiling_beats_db_live(self):
        self.receive("M1", phone="5517988881111"); self.age()
        res = self.run_worker("live")   # teto do ambiente = test
        self.assertEqual(res["modo"], "test")
        self.assertEqual(self.sent, [])

    def test_live_mode_sends_to_verified_store(self):
        os.environ["POUPAQUI_WA_IA_MAX_MODE"] = "live"
        self.receive("M1", phone="5517988881111"); self.age()
        self.run_worker("live")
        self.assertEqual(len(self.sent), 1)

    def test_unverified_contact_answer_becomes_human(self):
        self.receive("M1"); self.age()   # numero de teste sem vinculo nem cadastro
        self.run_worker("test")
        self.assertEqual(self.decisions()[0][0], "HUMAN")
        self.assertIsNone(self.calls[0]["identidade_verificada"])
        self.assertEqual(self.sent, [])

    def test_ambiguous_phone_not_verified(self):
        ident = wa.identify_contact(self.conn, "5517977772222")
        self.assertFalse(ident["verificada"])
        self.assertEqual(ident["metodo"], "telefone_ambiguo")
        forn = wa.identify_contact(self.conn, "5511966663333")
        self.assertEqual((forn["verificada"], forn["tipo"]), (True, "fornecedor"))

    def test_push_name_is_not_identity(self):
        self.receive("M1", push="LOJA UM LTDA"); self.age()
        self.run_worker("test")
        self.assertIsNone(self.calls[0]["identidade_verificada"])

    def test_groups_sequence_and_debounce(self):
        self.link(TEST_PHONE)
        self.receive("M1", text="Oi"); self.receive("M2", text="tudo bem?")
        self.receive("M3", text="qual horario de corte da germed")
        res = self.run_worker("test")
        self.assertEqual(res["contatos"], 0)   # ultima msg recente: espera terminar
        self.age()
        res = self.run_worker("test")
        self.assertEqual(res["contatos"], 1)
        self.assertEqual(len(self.calls[0]["mensagens_novas"]), 3)
        self.assertEqual(len(self.decisions()), 1)

    def test_sensitive_goes_human_without_model(self):
        self.link(TEST_PHONE)
        self.receive("M1", text="Meu boleto veio errado"); self.age()
        self.run_worker("test")
        self.assertEqual(self.decisions()[0][:3], ("HUMAN", "nao_enviar", "gate"))
        self.assertEqual(self.calls, [])

    def test_greeting_silence_without_model(self):
        self.receive("M1", text="Bom dia"); self.age()
        self.run_worker("test")
        self.assertEqual(self.decisions()[0][0], "SILENCE")
        self.assertEqual(self.calls, [])

    def test_opt_out(self):
        self.link(TEST_PHONE)
        self.receive("M1", text="PARAR"); self.age()
        self.run_worker("test")
        self.receive("M2"); self.age()
        self.run_worker("test")
        self.assertEqual([d[0] for d in self.decisions()], ["SILENCE", "SILENCE"])
        self.assertEqual(self.sent, [])

    def test_human_lock(self):
        self.link(TEST_PHONE)
        self.receive("M1", text="tenho uma reclamacao"); self.age()
        self.run_worker("test")
        self.receive("M2"); self.age()
        self.run_worker("test")
        self.assertEqual(self.decisions()[1][3], "conversa ja encaminhada a humano")
        self.assertEqual(self.sent, [])

    def test_rate_limit_per_hour(self):
        # numero da allowlist em test usa o limite proprio (POUPAQUI_WA_IA_TEST_MAX_HORA)
        os.environ["POUPAQUI_WA_IA_MAX_HORA"] = "1"
        os.environ["POUPAQUI_WA_IA_TEST_MAX_HORA"] = "1"
        self.addCleanup(os.environ.pop, "POUPAQUI_WA_IA_TEST_MAX_HORA", None)
        try:
            self.link(TEST_PHONE)
            self.receive("M1"); self.age(); self.run_worker("test")
            self.receive("M2"); self.age(); self.run_worker("test")
            self.assertEqual(len(self.sent), 1)
            self.assertEqual(self.decisions()[1][1], "bloqueado_limite_hora")
        finally:
            del os.environ["POUPAQUI_WA_IA_MAX_HORA"]

    def test_send_failure_not_retried(self):
        self.link(TEST_PHONE)
        self.receive("M1"); self.age()
        self.run_worker("test", sender=self.sender(ok=False))
        self.run_worker("test", sender=self.sender(ok=False))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.decisions()[0][1], "falhou")

    def test_classifier_crash_releases_messages(self):
        self.link(TEST_PHONE)
        self.receive("M1"); self.age()

        def boom(_):
            raise RuntimeError("x")
        res = self.run_worker("test", classifier=boom)
        self.assertEqual(res["erros"], 1)
        with self.conn.cursor() as cur:
            cur.execute("SELECT status, tentativas FROM ecommerce_wa_mensagens")
            self.assertEqual(cur.fetchone(), ("pendente", 1))
        self.run_worker("test")
        self.assertEqual(len(self.sent), 1)

    def test_ai_failure_goes_human_without_locking(self):
        self.link(TEST_PHONE)
        self.receive("M1"); self.age()
        self.run_worker("test", classifier=lambda p: Decision("HUMAN", "", "AI request failed: AuthenticationError"))
        self.assertEqual(self.decisions()[0][:3], ("HUMAN", "nao_enviar", "falha_ia"))
        self.receive("M2"); self.age()
        self.run_worker("test")
        self.assertEqual(len(self.sent), 1)

    def test_old_messages_expire(self):
        self.link(TEST_PHONE)
        self.receive("M1"); self.age(7 * 3600)
        res = self.run_worker("test")
        self.assertEqual((res["expiradas"], res["contatos"]), (1, 0))

    def test_concurrent_worker_skipped(self):
        token = wa.acquire_worker_lease(self.conn)
        self.assertTrue(token)
        self.assertIsNone(wa.acquire_worker_lease(self.conn))
        res = self.run_worker("test")
        self.assertIn("ignorado", res)
        wa.release_worker_lease(self.conn, token)
        self.assertIn("contatos", self.run_worker("test"))
        self.assertNotIn("ignorado", self.run_worker("test"))

    def test_stale_lease_expires(self):
        self.assertTrue(wa.acquire_worker_lease(self.conn))
        with self.conn.cursor() as cur:
            cur.execute("UPDATE ecommerce_wa_ia_config SET atualizado_em = NOW() - INTERVAL '6 minutes' "
                        "WHERE chave = 'worker_lease'")
        self.conn.commit()
        self.assertTrue(wa.acquire_worker_lease(self.conn))


if __name__ == "__main__":
    unittest.main()
