"""Testes do piloto Ollama local do atendimento WhatsApp IA.

O Ollama e simulado por um servidor HTTP em 127.0.0.1 (porta aleatoria) que
imita /api/chat, /api/version, /api/tags e /api/ps. Integracao usa o mesmo
schema temporario isolado de tests/test_wa_atendimento.py. Nenhum WhatsApp sai.
Rodar: python -m unittest tests.test_wa_ollama -v
"""
import importlib.util
import json
import os
import sys
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# trava de lock vira erro em 10s em vez de pendurar a suite
TIMEOUTS_DB = "-c statement_timeout=30000 -c lock_timeout=10000 -c idle_in_transaction_session_timeout=60000"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import poupaqui_wa_atendimento as wa  # noqa: E402
from poupaqui_ai_policy import (  # noqa: E402
    DEFAULT_OLLAMA_MODEL, GENERIC_ACK_REPLY, Decision, classify_with_ollama, compact_payload,
    ollama_classifier, reply_problem, ungrounded_numbers, validate_answer,
)

# Saida real do qwen3:4b (Thinking-2507) com think=false, num_predict=100 (28/10/2026)
QWEN_THINKING_LEAK = ("Okay, the user asked when their order will arrive. They said they placed the order "
                      "yesterday.\n\nFirst, I need to check the status of the order. But the user mentioned "
                      "not to invent status. So I can't make up any status. The response should be in")
from tests.test_wa_atendimento import TEST_PHONE, SchemaConn, payload  # noqa: E402


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeOllama:
    """Servidor fake. `reply` = dict de decisao, str crua, ou callable(body)."""

    def __init__(self):
        self.requests, self.reply, self.status, self.delay = [], None, 200, 0
        self.done_reason, self.models = "stop", [DEFAULT_OLLAMA_MODEL]
        self.thinking, self.done, self.finetune = None, True, "Instruct"
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/version":
                    return self._json(200, {"version": "0.0-fake"})
                if self.path == "/api/tags":
                    return self._json(200, {"models": [{"name": m} for m in fake.models]})
                if self.path == "/api/ps":
                    return self._json(200, {"models": [{"name": "qwen3:4b", "size": 3 * 2**30}]})
                return self._json(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/show":
                    return self._json(200, {"model_info": {"general.finetune": fake.finetune}})
                fake.requests.append(body)
                if fake.delay:
                    time.sleep(fake.delay)
                if fake.status != 200:
                    return self._json(fake.status, {"error": "model not found"})
                r = fake.reply(body) if callable(fake.reply) else fake.reply
                content = r if isinstance(r, str) else json.dumps(r)
                msg = {"role": "assistant", "content": content}
                if fake.thinking:
                    msg["thinking"] = fake.thinking
                self._json(200, {"message": msg, "done": fake.done, "done_reason": fake.done_reason})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


FATOS = {"fornecedores_entrega": [{"fornecedor": "GERMED", "horario_corte_pedido": "20:00",
                                   "prazo_entrega": "1 dia util"}]}


class OllamaClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeOllama()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()

    def setUp(self):
        f = self.fake
        f.requests, f.status, f.delay, f.done_reason = [], 200, 0, "stop"
        f.thinking, f.done = None, True
        f.reply = {"decision": "ANSWER", "reply": "O corte da GERMED é 20:00.", "reason": "dado"}

    def call(self, **kw):
        return classify_with_ollama({"mensagens_novas": [{"texto": "corte germed?"}]},
                                    base_url=self.fake.url, model="qwen3:4b", **kw)

    def test_structured_request_and_answer(self):
        d = self.call()
        self.assertEqual((d.decision, d.reply), ("ANSWER", "O corte da GERMED é 20:00."))
        body = self.fake.requests[0]
        self.assertEqual(body["format"]["properties"]["decision"]["enum"], ["SILENCE", "ANSWER", "HUMAN"])
        self.assertIs(body["stream"], False)
        self.assertIs(body["think"], False)
        self.assertEqual(body["options"]["temperature"], 0)
        self.assertLessEqual(body["options"]["num_predict"], 400)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("HUMAN", body["messages"][0]["content"])

    def test_default_model_is_not_thinking_tag(self):
        self.assertNotEqual(DEFAULT_OLLAMA_MODEL, "qwen3:4b")

    def test_num_predict_capped(self):
        for env, esperado in (("100000", 400), ("1", 64), ("abc", 256)):
            os.environ["POUPAQUI_OLLAMA_NUM_PREDICT"] = env
            try:
                self.fake.requests = []
                self.call()
                self.assertEqual(self.fake.requests[0]["options"]["num_predict"], esperado)
            finally:
                del os.environ["POUPAQUI_OLLAMA_NUM_PREDICT"]

    def test_think_block_fails_closed(self):
        # nao limpa por regex: qualquer raciocinio -> HUMAN, mesmo com JSON valido depois
        self.fake.reply = '<think>hmm</think>{"decision":"SILENCE","reply":"","reason":"aviso"}'
        d = self.call()
        self.assertEqual(d.decision, "HUMAN")
        self.assertTrue(d.reason.startswith("AI model emitted reasoning"))

    def test_thinking_field_fails_closed(self):
        self.fake.thinking = "Okay, the user asked about the cutoff..."
        self.assertEqual(self.call().decision, "HUMAN")

    def test_observed_qwen_leak_truncated_is_human(self):
        # caso real: content = raciocinio em ingles, cortado em 100 tokens
        self.fake.reply, self.fake.done_reason = QWEN_THINKING_LEAK, "length"
        d = self.call()
        self.assertEqual((d.decision, d.reply), ("HUMAN", ""))
        self.fake.done_reason = "stop"   # mesmo sem truncar: nao e JSON -> HUMAN
        self.assertEqual(self.call().decision, "HUMAN")

    def test_incomplete_or_unknown_done_is_human(self):
        self.fake.done = False
        self.assertEqual(self.call().decision, "HUMAN")
        self.fake.done, self.fake.done_reason = True, None
        self.assertEqual(self.call().decision, "HUMAN")
        self.fake.done_reason = "load"
        self.assertEqual(self.call().decision, "HUMAN")

    def test_invalid_output_is_human(self):
        self.fake.reply = "claro! posso ajudar"
        self.assertEqual(self.call().decision, "HUMAN")
        self.fake.reply = {"decision": "TALVEZ", "reply": "", "reason": ""}
        self.assertEqual(self.call().decision, "HUMAN")

    def test_truncated_output_is_human(self):
        self.fake.done_reason = "length"
        self.assertEqual(self.call().decision, "HUMAN")

    def test_model_missing_is_ai_failure(self):
        self.fake.status = 404
        d = self.call()
        self.assertEqual(d.decision, "HUMAN")
        self.assertTrue(d.reason.startswith("AI request failed"))

    def test_timeout_is_ai_failure(self):
        self.fake.delay = 1.5
        d = self.call(timeout=0.3)
        self.assertEqual(d.decision, "HUMAN")
        self.assertTrue(d.reason.startswith("AI request failed"))

    def test_server_down_is_ai_failure(self):
        d = classify_with_ollama({}, base_url="http://127.0.0.1:9", timeout=2)
        self.assertTrue(d.reason.startswith("AI request failed"))

    def test_non_loopback_refused_without_request(self):
        d = classify_with_ollama({}, base_url="http://192.168.0.10:11434")
        self.assertEqual(d.decision, "HUMAN")
        self.assertTrue(d.reason.startswith("AI "))
        self.assertEqual(self.fake.requests, [])

    def test_compact_payload_limits_history(self):
        p = {"historico_recente": [{"de": "contato", "texto": "x" * 5000}] * 30,
             "mensagens_novas": [{"tipo": "text", "texto": "y" * 5000}]}
        c = compact_payload(p)
        self.assertEqual(len(c["historico_recente"]), 8)
        self.assertEqual(len(c["historico_recente"][0]["texto"]), 400)
        self.assertEqual(len(c["mensagens_novas"][0]["texto"]), 800)
        self.assertEqual(len(p["historico_recente"]), 30)  # original intacto

    def test_classifier_records_latency(self):
        stats = []
        ollama_classifier(base_url=self.fake.url, stats=stats)({})
        self.assertEqual(len(stats), 1)


class GroundingTests(unittest.TestCase):
    def ans(self, text):
        return validate_answer(Decision("ANSWER", text, "r"), ["corte germed?"], True, FATOS)

    def test_grounded_time_passes(self):
        self.assertEqual(self.ans("O horário de corte da GERMED é 20:00.").decision, "ANSWER")
        self.assertEqual(self.ans("O corte da Germed é às 20h.").decision, "ANSWER")
        self.assertEqual(self.ans("A GERMED entrega em 1 dia útil.").decision, "ANSWER")

    def test_invented_time_or_number_blocked(self):
        self.assertEqual(self.ans("O corte da GERMED é 21:00.").decision, "HUMAN")
        self.assertEqual(self.ans("A GERMED entrega em 2 dias.").decision, "HUMAN")
        self.assertEqual(ungrounded_numbers("corte 18h30", FATOS), ["18:30"])

    def test_without_facts_param_keeps_old_behavior(self):
        d = validate_answer(Decision("ANSWER", "O corte é às 21:00.", "r"), ["?"], True)
        self.assertEqual(d.decision, "ANSWER")


class ReplyQualityTests(unittest.TestCase):
    """Texto do modelo so sai se for resposta final completa em PT-BR."""

    def ans(self, text):
        return validate_answer(Decision("ANSWER", text, "r"), ["corte germed?"], True, FATOS)

    def test_good_portuguese_passes(self):
        for t in ("O horário de corte da GERMED é 20:00.",
                  "A GERMED entrega em 1 dia útil, com corte às 20h!",
                  "Olá! O corte de pedidos da GERMED é às 20:00."):
            self.assertIsNone(reply_problem(t), t)
            self.assertEqual(self.ans(t).decision, "ANSWER", t)

    def test_reasoning_and_english_blocked(self):
        for t in (QWEN_THINKING_LEAK,
                  "Okay, o corte da GERMED é 20:00.",
                  "<think>pensando</think> O corte da GERMED é 20:00.",
                  "The cutoff time for GERMED is 20:00.",
                  "Let me check: o corte da GERMED é 20:00.",
                  "The user wants o corte da GERMED."):
            d = self.ans(t)
            self.assertEqual((d.decision, d.reply), ("HUMAN", ""), t)
            self.assertTrue(d.reason.startswith("invalid reply"), t)

    def test_truncated_or_malformed_blocked(self):
        for t in ("O horário de corte da GERMED é 20:00 e o prazo de entrega da",
                  '{"decision": "ANSWER"}',
                  "**O corte da GERMED** é 20:00.",
                  "O corte é 20:00.\n\nObs: a entrega é em 1 dia útil.",
                  "O corte é 20:00. A entrega é em 1 dia útil. Bom dia. Até logo.",
                  "   "):
            self.assertEqual(self.ans(t).decision, "HUMAN", t)

    def test_promise_blocked(self):
        self.assertEqual(self.ans("Vou verificar o corte da GERMED para você.").decision, "HUMAN")
        self.assertEqual(self.ans("Já encaminhei sua dúvida para a equipe.").decision, "HUMAN")

    def test_no_info_or_meta_reply_blocked(self):
        # caso real do benchmark qwen3:4b-instruct ("e da profarma?")
        for t in ("Os dados verificados não incluem informações sobre a Profarma.",
                  "Não tenho acesso ao status do seu pedido.",
                  "Não consigo dizer quando o pedido chegará.",
                  "Essa informação não consta no sistema."):
            self.assertEqual(self.ans(t).decision, "HUMAN", t)

    def test_quality_failure_counts_as_ai_failure(self):
        self.assertTrue(self.ans(QWEN_THINKING_LEAK).reason.startswith(wa._AI_FAILURE_REASONS))


class BenchAndSenderTests(unittest.TestCase):
    def test_bench_cases_with_reckless_model(self):
        bench = _load_script("wa_ia_ollama_bench")
        # modelo "imprudente": sempre responde inventando horario
        rows = bench.run_cases(lambda p: Decision("ANSWER", "O corte é 22:00.", "x"))
        by = {r["caso"]: r for r in rows}
        self.assertEqual(by["nome_falso"]["decisao"], "HUMAN")           # nao verificado
        self.assertEqual(by["contexto_corte"]["decisao"], "HUMAN")       # horario inventado
        self.assertEqual(by["saudacao"]["origem"], "gate")
        self.assertEqual(by["sensivel"]["decisao"], "HUMAN")
        self.assertEqual(by["confirmacoes_consecutivas"]["decisao"], "SILENCE")
        ok = bench.run_cases(lambda p: Decision("ANSWER", "O corte da GERMED é 20:00.", "x"))
        self.assertEqual({r["caso"]: r for r in ok}["contexto_corte"]["decisao"], "ANSWER")

    def test_sender_refuses_outside_allowlist_without_network(self):
        worker = _load_script("wa_ia_worker_local")
        os.environ["POUPAQUI_WA_IA_MAX_MODE"] = "test"
        os.environ["POUPAQUI_WA_IA_TEST_PHONES"] = TEST_PHONE
        prev = os.environ.get("WASENDER_API_KEY")
        os.environ["WASENDER_API_KEY"] = "x"
        try:
            with self.assertRaisesRegex(RuntimeError, "allowlist"):
                worker.make_sender()("5517988881111", "oi")
        finally:
            if prev is None:
                del os.environ["WASENDER_API_KEY"]
            else:
                os.environ["WASENDER_API_KEY"] = prev

    def test_alert_phones_respect_mode(self):
        os.environ["POUPAQUI_WA_IA_TEST_PHONES"] = TEST_PHONE
        os.environ["POUPAQUI_WA_IA_ALERTA_PHONES"] = f"{TEST_PHONE},5517988881111"
        try:
            self.assertEqual(wa.alert_phones("shadow"), set())
            self.assertEqual(wa.alert_phones("test"), {TEST_PHONE})
            self.assertEqual(wa.alert_phones("live"), {TEST_PHONE, "5517988881111"})
        finally:
            del os.environ["POUPAQUI_WA_IA_ALERTA_PHONES"]

    def test_health_check(self):
        worker = _load_script("wa_ia_worker_local")
        fake = FakeOllama()
        try:
            os.environ["OLLAMA_URL"] = fake.url
            ok, info = worker.ollama_health()
            self.assertTrue(ok)
            self.assertEqual(info["carregados"][0]["ram_mb"], 3072)
            fake.finetune = "Thinking"          # qwen3:4b = Qwen3-4B-Thinking-2507
            ok, info = worker.ollama_health()
            self.assertFalse(ok)
            self.assertIn("raciocinio", info["erro"])
            fake.finetune = "Instruct"
            fake.models = ["llama3.2:3b"]
            self.assertFalse(worker.ollama_health()[0])
            os.environ["OLLAMA_URL"] = "http://10.0.0.5:11434"
            self.assertFalse(worker.ollama_health()[0])
        finally:
            del os.environ["OLLAMA_URL"]
            fake.close()


@unittest.skipUnless(os.getenv("DATABASE_URL") or os.path.exists(".env"), "sem banco")
class OllamaWorkerIntegrationTests(unittest.TestCase):
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
            cur.execute("""INSERT INTO users VALUES ('11111111000111','loja1','LOJA UM LTDA','(17) 98888-1111')""")
            cur.execute("""INSERT INTO fornecedores (nome, telefone, ativo, horario_corte, prazo_entrega_desc)
                           VALUES ('GERMED','11 96666-3333',TRUE,'20:00','1 dia util')""")
        cls.conn.commit()
        wa.ensure_schema(cls.conn, force=True)
        cls.fake = FakeOllama()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        cls.raw.rollback()
        with cls.raw.cursor() as cur:
            cur.execute(f"DROP SCHEMA {cls.schema} CASCADE")
        cls.raw.commit()
        cls.raw.close()

    def setUp(self):
        os.environ["POUPAQUI_WA_IA_MAX_MODE"] = "test"
        os.environ["POUPAQUI_WA_IA_TEST_PHONES"] = TEST_PHONE
        for k in ("POUPAQUI_WA_IA_ACK_HUMANO", "POUPAQUI_WA_IA_ALERTA_PHONES"):
            os.environ.pop(k, None)
        with self.conn.cursor() as cur:
            cur.execute("""TRUNCATE ecommerce_wa_mensagens, ecommerce_wa_ia_decisoes,
                           ecommerce_wa_contatos, ecommerce_wa_ia_config""")
        self.conn.commit()
        self.sent = []
        self.fake.requests = []
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in
                                 ("POUPAQUI_WA_IA_ACK_HUMANO", "POUPAQUI_WA_IA_ALERTA_PHONES")])
        self.fake.reply = {"decision": "ANSWER", "reply": "O horário de corte da GERMED é 20:00.", "reason": "dado"}

    def receive(self, mid, phone=TEST_PHONE, text="Qual o horario de corte da Germed?", **kw):
        wa.store_inbound(payload(mid, phone=phone, text=text, **kw), self.conn)
        with self.conn.cursor() as cur:
            cur.execute("UPDATE ecommerce_wa_mensagens SET recebido_em = recebido_em - INTERVAL '2 minutes'")
        self.conn.commit()

    def link(self, phone=TEST_PHONE):
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO ecommerce_wa_contatos (telefone, vinculo_manual_tipo, vinculo_manual_ref)
                           VALUES (%s,'interno','teste') ON CONFLICT (telefone) DO UPDATE
                           SET vinculo_manual_tipo='interno', vinculo_manual_ref='teste'""", (phone,))
        self.conn.commit()

    def sender(self, phone, text):
        self.sent.append((phone, text))
        return {"success": True, "data": {"msgId": f"out-{len(self.sent)}"}}

    def run_local(self, mode="test", engine="local", classifier=None):
        wa.set_db_mode(self.conn, mode)
        return wa.run_worker(self.conn, classifier or ollama_classifier(base_url=self.fake.url),
                             self.sender, engine=engine, model_label="ollama:qwen3:4b")

    def decisions(self):
        with self.conn.cursor() as cur:
            cur.execute("""SELECT decisao, envio_status, origem, motivo, resposta, alerta_status, modelo
                           FROM ecommerce_wa_ia_decisoes ORDER BY id""")
            return cur.fetchall()

    def test_engine_gating_single_send_path(self):
        self.link(); self.receive("M1")
        res = self.run_local()                      # motor padrao = servidor
        self.assertIn("motor ativo e 'servidor'", res["ignorado"])
        self.assertEqual(self.fake.requests, [])
        wa.set_db_engine(self.conn, "local")
        res = self.run_local(engine="servidor")     # cron do servidor nao rouba a fila
        self.assertIn("ignorado", res)
        res = self.run_local()
        self.assertEqual(res["resultados"][0]["envio"], "enviado")
        self.assertEqual(self.sent, [(TEST_PHONE, "O horário de corte da GERMED é 20:00.")])
        self.assertEqual(self.decisions()[0][6], "ollama:qwen3:4b")

    def test_end_to_end_shadow_then_test(self):
        wa.set_db_engine(self.conn, "local")
        self.link(); self.receive("M1")
        self.run_local("shadow")
        self.assertEqual(self.decisions()[0][1], "shadow")
        self.assertEqual(self.sent, [])
        ctx = json.loads(self.fake.requests[0]["messages"][1]["content"])
        self.assertEqual(ctx["dados_verificados"]["fornecedores_entrega"][0]["horario_corte_pedido"], "20:00")
        self.receive("M2", text="e o prazo da germed?")
        self.run_local("test")
        self.assertEqual(len(self.sent), 1)
        hist = json.loads(self.fake.requests[1]["messages"][1]["content"])["historico_recente"]
        self.assertEqual(hist[0]["texto"], "Qual o horario de corte da Germed?")

    def test_no_open_transaction_during_model_call(self):
        wa.set_db_engine(self.conn, "local")
        self.link(); self.receive("M1")
        import psycopg2.extensions as ext
        seen = []

        def clf(p):
            seen.append(self.raw.status)
            return Decision("SILENCE", "", "x")
        self.run_local(classifier=clf)
        self.assertEqual(seen, [ext.STATUS_READY])

    def test_invented_time_from_model_goes_human(self):
        wa.set_db_engine(self.conn, "local")
        self.link(); self.receive("M1")
        self.fake.reply = {"decision": "ANSWER", "reply": "O corte da GERMED é 22:00.", "reason": "x"}
        self.run_local()
        self.assertEqual(self.decisions()[0][0], "HUMAN")
        self.assertEqual(self.sent, [])

    def test_reasoning_leak_never_sent(self):
        wa.set_db_engine(self.conn, "local")
        self.link(); self.receive("M1", text="Fiz um pedido ontem. Quando chega?")
        self.fake.reply = {"decision": "ANSWER", "reply": QWEN_THINKING_LEAK, "reason": "x"}
        self.run_local()
        d = self.decisions()[0]
        self.assertEqual((d[0], d[2], d[4]), ("HUMAN", "falha_ia", None))
        self.fake.reply = QWEN_THINKING_LEAK          # texto cru, truncado
        self.fake.done_reason = "length"
        try:
            self.receive("M2", text="e ai?")
            self.run_local()
        finally:
            self.fake.done_reason = "stop"
        self.assertEqual(self.decisions()[1][0], "HUMAN")
        self.assertEqual(self.sent, [])

    def test_unknown_contact_only_gets_fixed_ack(self):
        os.environ["POUPAQUI_WA_IA_ACK_HUMANO"] = "1"
        wa.set_db_engine(self.conn, "local")
        self.receive("M1", push="LOJA UM LTDA")     # nome falso, numero nao cadastrado
        self.run_local()
        d = self.decisions()[0]
        self.assertEqual((d[0], d[1], d[4]), ("HUMAN", "enviado", GENERIC_ACK_REPLY))
        self.assertEqual(self.sent, [(TEST_PHONE, GENERIC_ACK_REPLY)])
        self.receive("M2", text="alo?")             # trava humana: sem novo aviso
        self.run_local()
        self.assertEqual(len(self.sent), 1)

    def test_ack_disabled_by_default(self):
        wa.set_db_engine(self.conn, "local")
        self.receive("M1")
        self.run_local()
        self.assertEqual(self.sent, [])

    def test_human_alert_to_team_once(self):
        os.environ["POUPAQUI_WA_IA_ALERTA_PHONES"] = TEST_PHONE
        # a loja precisa estar na allowlist: em test, contato de fora nem entra no worker
        os.environ["POUPAQUI_WA_IA_TEST_PHONES"] = f"{TEST_PHONE},5517988881111"
        wa.set_db_engine(self.conn, "local")
        self.receive("M1", phone="5517988881111", text="o boleto veio errado")   # loja real, identificada pelo telefone
        res = self.run_local()
        self.assertEqual(res["resultados"][0]["alerta"], "enviado")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], TEST_PHONE)                 # so a equipe recebe
        self.assertIn("+5517988881111", self.sent[0][1])
        self.assertIn("loja: LOJA UM LTDA", self.sent[0][1])         # identificado pelo telefone
        wa.resolve_human(self.conn, "5517988881111")
        self.receive("M2", phone="5517988881111", text="e o pix?")
        self.run_local()
        self.assertEqual(len(self.sent), 1)                           # dedupe 60 min

    def test_no_alert_in_shadow(self):
        os.environ["POUPAQUI_WA_IA_ALERTA_PHONES"] = TEST_PHONE
        wa.set_db_engine(self.conn, "local")
        self.receive("M1", text="o boleto veio errado")
        self.run_local("shadow")
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.decisions()[0][5])

    def test_pending_and_resolve(self):
        wa.set_db_engine(self.conn, "local")
        self.receive("M1", text="tenho uma reclamacao")
        self.run_local()
        pend = wa.pending_human(self.conn)
        self.assertEqual(len(pend), 1)
        self.assertIn("reclamacao", pend[0]["texto"])
        self.assertEqual(wa.resolve_human(self.conn, TEST_PHONE), 1)
        self.assertEqual(wa.pending_human(self.conn), [])

    def test_lease_renew(self):
        token = wa.acquire_worker_lease(self.conn)
        self.assertTrue(wa.renew_worker_lease(self.conn, token))
        self.assertFalse(wa.renew_worker_lease(self.conn, "outro"))
        wa.release_worker_lease(self.conn, token)

    def test_ollama_down_counts_as_ai_failure_not_lock(self):
        wa.set_db_engine(self.conn, "local")
        self.link(); self.receive("M1")
        wa.set_db_mode(self.conn, "test")
        wa.run_worker(self.conn, ollama_classifier(base_url="http://127.0.0.1:9"), self.sender, engine="local")
        self.assertEqual(self.decisions()[0][2], "falha_ia")
        self.receive("M2")
        self.run_local()
        self.assertEqual(len(self.sent), 1)

    def test_shadow_servidor_only_allowlist_and_writes_nothing(self):
        shadow = _load_script("wa_ia_shadow_servidor")
        self.link(); self.receive("M1")
        self.receive("M2", phone="5517988881111", text="qual o corte da germed?")
        seqs = shadow.last_sequences(self.conn, wa.test_phones(), 24)
        self.assertEqual(list(seqs), [TEST_PHONE])
        self.conn.rollback()
        res = shadow.shadow_contact(self.conn, TEST_PHONE, seqs[TEST_PHONE],
                                    ollama_classifier(base_url=self.fake.url))
        self.assertEqual(res["decisao"], "ANSWER")
        self.assertEqual(self.decisions(), [])
        with self.conn.cursor() as cur:
            cur.execute("SELECT DISTINCT status FROM ecommerce_wa_mensagens")
            self.assertEqual(cur.fetchall(), [("pendente",)])
        self.conn.rollback()

    def test_shadow_servidor_connection_is_read_only(self):
        import psycopg2
        shadow = _load_script("wa_ia_shadow_servidor")
        conn = shadow.connect_read_only()
        try:
            with self.assertRaises(psycopg2.errors.ReadOnlySqlTransaction):
                with conn.cursor() as cur:
                    cur.execute(f"CREATE TABLE {self.schema}.nao_deveria (x INT)")
        finally:
            conn.rollback()
            conn.close()


class ShadowServidorStaticTests(unittest.TestCase):
    def test_no_send_path(self):
        src = open(os.path.join(ROOT, "scripts", "wa_ia_shadow_servidor.py"), encoding="utf-8").read()
        for proibido in ("wasender", "make_sender", "run_worker", "set_db_mode", "set_db_engine",
                         "process_contact", "INSERT", "UPDATE", "DELETE"):
            self.assertNotIn(proibido, src)


if __name__ == "__main__":
    unittest.main()
