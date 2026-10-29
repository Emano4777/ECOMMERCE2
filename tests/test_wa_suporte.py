"""Testes do suporte tecnico (poupaqui_ai_suporte) -- offline.

Repositorio em memoria com DUAS lojas e sonda HTTP falsa: nao acessa banco,
nao acessa rede, nao chama IA e nao envia WhatsApp.
Rodar: python -m unittest tests.test_wa_suporte -v
"""
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import poupaqui_ai_assistente as A  # noqa: E402
import poupaqui_ai_suporte as S  # noqa: E402
from tests.test_wa_assistente import AGORA, LOJA_A, LOJA_B, NAO_VERIF, FakeRepo  # noqa: E402

HOJE = AGORA.date()


class FakeRepoSuporte(FakeRepo):
    def __init__(self, pedidos_60=7, lojas_60=4, dias_itens=None, dias_tot=None, catalogo=True, media=6.0,
                 hist=(30, 31), compras_3h=5, media_compras=4.0, forn_3h=3):
        super().__init__()
        self.ecom = {"criados_3h": compras_3h, "pagos_3h": max(0, compras_3h - 1), "media_3h": media_compras,
                     "ultimo_geral": AGORA - timedelta(minutes=20), "ultimo_loja": None}
        self.forn_3h = forn_3h
        self.hist = list(hist)
        self.ativ = {"pedidos_60min": pedidos_60, "lojas_60min": lojas_60, "media_mesma_hora": media,
                     "ultimo_geral": AGORA - timedelta(minutes=5), "ultimo_loja": AGORA - timedelta(hours=20)}
        out = date(2026, 10, 1)
        completo = {out + timedelta(days=i) for i in range(26)}  # 01..26/10
        self.vendas = {"11111111000111": (dias_itens if dias_itens is not None else completo,
                                          dias_tot if dias_tot is not None else completo),
                       "22222222000122": (set(), set())}
        self.cfg = {"11111111000111": {"catalogo_publico": catalogo, "estoque_min": 1, "sync_em": None},
                    "22222222000122": {"catalogo_publico": True, "estoque_min": 1, "sync_em": None}}
        self.estoque = {
            "11111111000111": (date(2026, 10, 26), [
                {"ean": "7891000000011", "descricao": "DIPIRONA 500MG C/10", "qtd": 12.0, "origem": "estoque"},
                {"ean": "7891000000022", "descricao": "PROTETOR SOLAR FPS50", "qtd": 0.0, "origem": "estoque"},
                {"ean": "7891000000033", "descricao": "VITAMINA C 1G", "qtd": 5.0, "origem": "estoque"}]),
            "22222222000122": (date(2026, 10, 26), [
                {"ean": "7891000000044", "descricao": "SHAMPOO BETA EXCLUSIVO", "qtd": 9.0, "origem": "estoque"}]),
        }
        self.ocultos = {("11111111000111", "7891000000033")}

    def atividade_pedidos(self, user_id, fornecedor=None):
        self.chamadas.append(("atividade_pedidos", user_id))
        out = dict(self.ativ)
        if fornecedor:
            out.update({"fornecedor": fornecedor.upper(), "fornecedor_3h": self.forn_3h,
                        "ultimo_fornecedor": AGORA - timedelta(minutes=30)})
        return out

    def atividade_ecommerce(self, cnpj):
        self.chamadas.append(("atividade_ecommerce", cnpj))
        return dict(self.ecom)

    def cobertura_vendas(self, cnpj, ano, mes):
        self.chamadas.append(("cobertura_vendas", cnpj))
        itens, tot = self.vendas.get(cnpj, (set(), set()))
        if mes != 10:
            itens = tot = ({date(ano, mes, 1)} if self.hist and max(self.hist) <= 2 else set())
        return {"itens": set(itens), "totalizadas": set(tot), "ultimo_processado": AGORA.replace(hour=6, minute=10),
                "dias_meses_anteriores": self.hist, "processado_mes_anterior": AGORA.replace(day=2, hour=6)}

    def config_ecommerce(self, cnpj):
        self.chamadas.append(("config_ecommerce", cnpj))
        return self.cfg.get(cnpj)

    def produto_estoque(self, cnpj, ean=None, termos=(), limite=5):
        self.chamadas.append(("produto_estoque", cnpj))
        data, prods = self.estoque.get(cnpj, (None, []))
        achados = [dict(p, oculto=(cnpj, p["ean"]) in self.ocultos) for p in prods
                   if (ean and p["ean"] == ean) or (not ean and termos and
                                                    all(t.upper() in p["descricao"] for t in termos))]
        return {"data_estoque": data, "produtos": achados[:limite]}


def sonda_ok(sistema):
    return {"sistema": sistema, "ok": True, "http": 200, "seg": 0.41, "erro": None}


def sonda_caiu(sistema):
    return {"sistema": sistema, "ok": False, "http": None, "seg": 8.0, "erro": "TimeoutError"}


def resp(textos, ident=LOJA_A, hist=None, repo=None, sonda=sonda_ok):
    repo = repo or FakeRepoSuporte()
    r = A.responder(textos, (), ident, hist, repo, AGORA, None, sonda)
    r._repo = repo
    return r


class TestDeteccao(unittest.TestCase):
    def test_intencoes(self):
        casos = {
            "o pedido eletronico caiu?": "pe_fora",
            "nao consigo entrar no sistema": "pe_fora",
            "o sistema esta fora do ar": "pe_fora",
            "o relatorio de vendas da loja esta com numero errado": "relatorio",
            "as vendas de setembro no bi nao batem": "relatorio",
            "o relatorio nao abre": "rel_fora",
            "o produto dipirona nao aparece no site": "ecommerce",
            "sumiu o protetor do ecommerce": "ecommerce",
            "o ecommerce esta fora do ar": "ecom_fora",
            "como faco pra trocar a senha do sistema": "sistema",
            "qual o horario de corte da germed": None,
            "meu pedido da germed ja saiu": None,
            "meu pedido nao aparece no sistema": None,
        }
        for txt, esperado in casos.items():
            self.assertEqual(S.detectar_suporte(txt), esperado, txt)


def ia(texto):
    return {"de": "assistente", "texto": texto}


def loja(texto):
    return {"de": "contato", "texto": texto}


class TestPedidoEletronicoFora(unittest.TestCase):
    def test_no_ar_com_pedidos_nao_e_queda_geral(self):
        r = resp(["O Pedido Eletrônico caiu?"])
        self.assertEqual(r.acao, "ANSWER")
        self.assertIn("abre normalmente", r.reply)
        self.assertIn("7 pedidos", r.reply)
        self.assertIn("não é queda geral", r.reply)
        self.assertFalse(r.escalar)
        self.assertTrue(any(f.startswith("sonda_http:pedido_eletronico") for f in r.fontes))

    def test_fora_do_ar_escala_sem_inventar_causa(self):
        r = resp(["o sistema está fora do ar"], sonda=sonda_caiu)
        self.assertIn("não respondeu", r.reply)
        self.assertTrue(r.escalar)
        self.assertEqual(r.equipe, "equipe técnica")
        self.assertNotRegex(r.reply, r"resolvid|normaliz|manuten")

    def test_sem_pedidos_na_hora_nao_afirma_causa(self):
        r = resp(["não consigo enviar pedido no sistema"], repo=FakeRepoSuporte(pedidos_60=0, lojas_60=0))
        self.assertIn("não consigo descartar", r.reply)
        self.assertTrue(r.escalar)

    def test_horario_de_pouco_movimento_nao_alarma(self):
        r = resp(["o sistema caiu?"], repo=FakeRepoSuporte(pedidos_60=0, lojas_60=0, media=0.3))
        self.assertIn("movimento costuma ser baixo", r.reply)
        self.assertIn("0,3", r.reply)
        self.assertFalse(r.escalar)

    def test_insistencia_escala(self):
        r = resp(["continua sem funcionar"], hist=[loja("o sistema caiu?")])
        self.assertIn("continua", r.reply)
        self.assertTrue(r.escalar)
        self.assertEqual(r.equipe, "equipe técnica")
        self.assertEqual(r.resumo["motivo_escalacao"], "falha persiste apos orientacao")

    def test_nao_verificado_so_status_publico(self):
        r = resp(["o pedido eletronico caiu?"], NAO_VERIF)
        self.assertIn("abre normalmente", r.reply)
        self.assertNotIn("pedidos", r.reply)  # nada do fluxo interno
        self.assertNotIn(("atividade_pedidos", "uA"), r._repo.chamadas)


class TestNaoFunciona(unittest.TestCase):
    """Cenarios do feedback de 28/10: identificar O QUE falhou antes de escalar."""

    def test_pedido_eletronico_nao_funciona_generico_pergunta_o_que(self):
        r = resp(["o pedido eletronico nao funciona"])
        self.assertEqual((r.acao, r.escalar), ("ANSWER", False))
        self.assertIn("O que acontece aí", r.reply)
        self.assertIn("não consegue entrar", r.reply)
        self.assertIn("erro na hora de enviar", r.reply)
        self.assertIn("7 pedidos", r.reply)   # checagem real, nao so HTTP
        self.assertLessEqual(r.reply.count("?"), 2)  # sem interrogatorio

    def test_login_nao_entra_sem_sistema_pergunta_qual(self):
        repo = FakeRepoSuporte()
        r = resp(["login nao entra"], repo=repo)
        self.assertEqual((r.acao, r.escalar), ("ANSWER", False))
        self.assertIn("Em qual sistema", r.reply)
        self.assertEqual([c for c in repo.chamadas if c[0] == "atividade_pedidos"], [])

    def test_login_resposta_com_sistema_usa_contexto(self):
        hist = [loja("login nao entra"), ia("Em qual sistema você está tentando entrar: Pedido Eletrônico, "
                                            "relatórios ou o painel do e-commerce?")]
        r = resp(["no pedido eletronico"], hist=hist)
        self.assertEqual((r.intencao, r.escalar), ("acesso", False))
        self.assertIn("Caps Lock", r.reply)
        self.assertIn("aba anônima", r.reply)

    def test_login_no_pedido_eletronico_orienta_sem_escalar(self):
        r = resp(["nao consigo entrar no pedido eletronico"])
        self.assertFalse(r.escalar)
        self.assertIn("senha inválida, usuário bloqueado", r.reply)
        self.assertIn("não é queda geral", r.reply)

    def test_login_bloqueado_escala_direto(self):
        r = resp(["nao consigo entrar no pedido eletronico, aparece usuario bloqueado"])
        self.assertTrue(r.escalar)
        self.assertIn("usuario bloqueado", r.reply)
        self.assertIn("não consigo fazer por aqui", r.reply)
        self.assertEqual(r.resumo["motivo_escalacao"], "acesso bloqueado/expirado: so humano libera")

    def test_botao_enviar_pedido_da_erro_pergunta_mensagem_e_fornecedor(self):
        r = resp(["botao enviar pedido da erro"])
        self.assertEqual((r.intencao, r.escalar), ("pe_fora", False))
        self.assertIn("envio está funcionando para outras lojas", r.reply)
        self.assertIn("qual mensagem aparece", r.reply)
        self.assertIn("fornecedor específico", r.reply)
        self.assertIn("Ctrl+F5", r.reply)

    def test_enviar_pedido_com_erro_e_tentativa_escala_com_resumo(self):
        hist = [loja("botao enviar pedido da erro"),
                ia("Verifiquei agora... Enquanto isso, atualize a página com Ctrl+F5 e tente enviar de novo.")]
        r = resp(["aparece erro 500 quando mando o pedido da germed, ja atualizei e nada desde as 9h"], hist=hist)
        self.assertTrue(r.escalar)
        self.assertEqual(r.equipe, "equipe técnica")
        sint = " | ".join(r.resumo["sintomas"])
        self.assertIn("erro 500", sint)
        self.assertIn("atualizou a página", sint)
        self.assertIn("desde as 9h", sint)
        self.assertIn("enviar/finalizar pedido", sint)
        self.assertTrue(any("GERMED" in f for f in r.fontes))   # fluxo do fornecedor citado
        self.assertIn("específico da sua loja", r.reply)
        self.assertNotRegex(r.reply, r"(?i)servidor caiu|bug|instabilidade")  # sem causa inventada

    def test_site_abre_mas_nao_finaliza_compra(self):
        repo = FakeRepoSuporte()
        r = resp(["o site abre mas nao finaliza compra"], repo=repo)
        self.assertEqual((r.intencao, r.escalar), ("ecom_fora", False))
        self.assertIn("Em que etapa trava", r.reply)
        self.assertIn("5 compras foram feitas", r.reply)
        self.assertIn(("atividade_ecommerce", "11111111000111"), repo.chamadas)

    def test_site_sem_compras_fora_do_normal_escala(self):
        r = resp(["o site abre mas nao finaliza compra"], repo=FakeRepoSuporte(compras_3h=0, media_compras=4.0))
        self.assertTrue(r.escalar)
        self.assertIn("nenhuma compra", r.reply)

    def test_http_200_nao_prova_que_compra_funciona(self):
        r = resp(["o site abre mas nao finaliza compra"], repo=FakeRepoSuporte(compras_3h=0, media_compras=0.2))
        self.assertFalse(r.escalar)
        self.assertIn("não garante que a compra esteja passando", r.reply)

    def test_falha_persiste_depois_de_orientacao(self):
        hist = [loja("o pedido eletronico nao abre"),
                ia("Verifiquei agora: a página abre normalmente. Aí pode ser conexão ou navegador: tente "
                   "atualizar com Ctrl+F5 ou abrir em outro navegador.")]
        r = resp(["continua igual, tela branca"], hist=hist)
        self.assertTrue(r.escalar)
        self.assertIn("tela branca", " ".join(r.resumo["sintomas"]))

    def test_resolvido_sem_escalar(self):
        hist = [loja("o pedido eletronico nao abre"), ia("... tente atualizar com Ctrl+F5 ...")]
        r = resp(["agora foi, obrigado"], hist=hist)
        self.assertEqual((r.acao, r.escalar), ("ANSWER", False))
        self.assertIn("Que bom", r.reply)

    def test_outro_assunto_nao_herda_persistencia(self):
        hist = [loja("o relatorio de vendas esta errado"), ia("... me diga qual relatório ...")]
        r = resp(["o pedido eletronico caiu?"], hist=hist)
        self.assertFalse(r.escalar)


class TestEncaminhamento(unittest.TestCase):
    """Texto base nunca afirma que a equipe foi avisada; escalacao traz resumo."""

    CASOS = [
        (["o sistema está fora do ar"], {"sonda": sonda_caiu}),
        (["continua sem funcionar"], {"hist": [{"de": "contato", "texto": "o sistema caiu?"}]}),
        (["não consigo enviar pedido no sistema"], {"repo_kw": {"pedidos_60": 0, "lojas_60": 0}}),
        (["o relatório de vendas está errado, no caixa deu 1800"], {}),
        (["o relatório de vendas está errado"], {"repo_kw": {"dias_itens": set(), "dias_tot": set()}}),
        (["a dipirona não aparece no site"], {}),
        (["o produto dipirona não aparece no site"], {"repo_kw": {"catalogo": False}}),
        (["como faco pra trocar a senha do sistema"], {}),
        (["o relatório de vendas está errado"], {"ident": NAO_VERIF}),
        (["meu pedido da germed ainda nao chegou, era pra ontem"], {}),
        (["o boleto do pedido da germed veio com valor errado"], {}),
        (["quero cancelar meu pedido da germed"], {}),
        (["qual o corte da profarma?"], {}),
        (["qual o corte da medley?"], {}),
        (["status do pedido 9999"], {}),
        (["meu pedido chegou?"], {"ident": NAO_VERIF}),
        (["vocês têm vaga de emprego?"], {}),
        (["segue o pedido"], {}),
        (["login nao entra, usuario bloqueado"], {}),
    ]

    def test_nenhum_texto_base_promete_encaminhamento(self):
        for textos, kw in self.CASOS:
            repo = FakeRepoSuporte(**kw.get("repo_kw", {}))
            r = resp(textos, kw.get("ident", LOJA_A), kw.get("hist"), repo, kw.get("sonda", sonda_ok))
            self.assertFalse(A.promete_encaminhamento(r.reply), f"{textos}: {r.reply}")

    def test_escalacoes_tem_resumo_tecnico(self):
        for textos, kw in self.CASOS:
            repo = FakeRepoSuporte(**kw.get("repo_kw", {}))
            r = resp(textos, kw.get("ident", LOJA_A), kw.get("hist"), repo, kw.get("sonda", sonda_ok))
            if r.escalar:
                self.assertTrue((r.resumo or {}).get("assunto"), f"{textos}: sem resumo")

    def test_detector_de_promessa(self):
        for t in ("Já avisei a equipe.", "A equipe técnica já foi avisada para verificar.",
                  "Já passei para a equipe técnica olhar.", "já deixei avisado", "Avisei a equipe para verificar.",
                  "Já chamei alguém da equipe"):
            self.assertTrue(A.promete_encaminhamento(t), t)
        for t in ("Se continuar, me avisa que eu passo para a equipe técnica.",
                  "Se quiser que a equipe verifique, é só me dizer.", "Quem faz é a equipe."):
            self.assertFalse(A.promete_encaminhamento(t), t)

    def test_frase_conforme_resultado_real_do_alerta(self):
        ok = A.completar_resposta("Base.", "enviado", 42, "equipe técnica")
        self.assertIn("Já passei para a equipe técnica", ok)
        self.assertIn("protocolo 42", ok)
        self.assertEqual(A.completar_resposta("Base.", "parcial", 42), A.completar_resposta("Base.", "enviado", 42))
        for st in ("falhou", "indisponivel", None, "enviando", "shadow"):
            txt = A.completar_resposta("Base.", st, 42, "equipe técnica")
            self.assertNotIn("Já passei", txt, st)
            self.assertIn("não consegui avisar", txt, st)
            self.assertIn("protocolo 42", txt, st)
        dup = A.completar_resposta("Base.", "ja_avisada", 7, anterior_em=AGORA)
        self.assertIn("já recebeu esse caso às 10:15 (protocolo 7)", dup)


class TestRelatorio(unittest.TestCase):
    def test_completo_pede_detalhe(self):
        r = resp(["o relatório de vendas da loja está com número errado"])
        self.assertIn("completos", r.reply)
        self.assertIn("qual relatório", r.reply)
        self.assertFalse(r.escalar)   # arquivos ok e sem detalhe: pergunta antes de escalar
        self.assertNotRegex(r.reply, r"R\$")

    def test_relatorio_valor_errado_com_detalhe_escala(self):
        hist = [{"de": "contato", "texto": "relatorio com valor errado"},
                {"de": "assistente", "texto": "... me diga qual relatório, qual número aparece ..."}]
        r = resp(["o de vendas mostra 15200 e no caixa fechou 18400"], hist=hist)
        self.assertTrue(r.escalar)
        self.assertIn("não consigo conferir daqui", r.reply)
        self.assertEqual(r.resumo["motivo_escalacao"], "arquivos ok, divergência a conferir")

    def test_relatorio_valor_errado_generico(self):
        r = resp(["relatorio com valor errado"])
        self.assertEqual((r.intencao, r.escalar), ("relatorio", False))
        self.assertIn("com o que você está comparando", r.reply)

    def test_lacuna_aponta_dias(self):
        out = date(2026, 10, 1)
        itens = {out + timedelta(days=i) for i in range(26)} - {date(2026, 10, 12), date(2026, 10, 13)}
        r = resp(["as vendas no relatório estão erradas"], repo=FakeRepoSuporte(dias_itens=itens, dias_tot=itens))
        self.assertIn("12/10 a 13/10", r.reply)
        self.assertIn("pode explicar", r.reply)

    def test_divergencia_entre_arquivos(self):
        out = date(2026, 10, 1)
        tudo = {out + timedelta(days=i) for i in range(26)}
        r = resp(["o ticket médio não bate com o relatório"], repo=FakeRepoSuporte(dias_tot=tudo - {date(2026, 10, 5)}))
        self.assertIn("05/10", r.reply)
        self.assertIn("só um dos dois arquivos", r.reply)

    def test_loja_mensal_nao_aponta_dias_faltando(self):
        """Loja que manda 1 arquivo por mes: nunca dizer 'faltam os dias 02 a 30'."""
        mensal = FakeRepoSuporte(dias_itens=set(), dias_tot=set(), hist=(1, 1))
        r = resp(["o relatório de vendas da loja está com número errado"], repo=mensal)
        self.assertIn("uma vez por mês", r.reply)
        self.assertIn("ainda não chegou", r.reply)
        self.assertNotIn("Faltam os dias", r.reply)
        self.assertFalse(r.escalar)
        r = resp(["o relatório de setembro está faltando venda?"], repo=FakeRepoSuporte(hist=(1, 1)))
        self.assertIn("já chegou", r.reply)
        self.assertNotIn("Faltam os dias", r.reply)

    def test_isolamento_usa_cnpj_da_identidade(self):
        r = resp(["o relatório da loja 22222222000122 está errado"])
        self.assertIn("própria loja", r.reply)
        self.assertNotIn(("cobertura_vendas", "22222222000122"), r._repo.chamadas)
        r = resp(["o relatório de vendas está errado"], LOJA_B)
        self.assertIn(("cobertura_vendas", "22222222000122"), r._repo.chamadas)
        self.assertNotIn(("cobertura_vendas", "11111111000111"), r._repo.chamadas)
        self.assertIn("nenhum arquivo", r.reply)

    def test_sem_identidade_pede_numero_cadastrado(self):
        r = resp(["o relatório de vendas está errado"], NAO_VERIF)
        self.assertIn("número cadastrado", r.reply)
        self.assertEqual([c for c in r._repo.chamadas if c[0] == "cobertura_vendas"], [])


class TestEcommerce(unittest.TestCase):
    def test_produto_zerado(self):
        r = resp(["o protetor solar não aparece no site"])
        self.assertIn("estoque zerado", r.reply)
        self.assertFalse(r.escalar)

    def test_produto_oculto(self):
        r = resp(["a vitamina c sumiu do ecommerce"])
        self.assertIn("oculto", r.reply)

    def test_produto_ok_nao_inventa_causa(self):
        r = resp(["a dipirona não aparece no site"])
        self.assertIn("deveria aparecer", r.reply)
        self.assertIn("não consigo confirmar", r.reply)
        self.assertTrue(r.escalar)

    def test_por_ean(self):
        r = resp(["o produto 7891000000022 não aparece no site"])
        self.assertIn("7891000000022", r.reply)

    def test_produto_de_outra_loja_nao_vaza(self):
        r = resp(["o shampoo beta exclusivo não aparece no site"])
        self.assertIn("não encontrei", r.reply)
        self.assertNotIn(("produto_estoque", "22222222000122"), r._repo.chamadas)

    def test_sem_produto_pergunta(self):
        r = resp(["tem produto que não aparece no site"])
        self.assertIn("Qual produto", r.reply)

    def test_catalogo_desativado(self):
        r = resp(["o produto dipirona não aparece no site"], repo=FakeRepoSuporte(catalogo=False))
        self.assertIn("catálogo desativado", r.reply)
        self.assertNotIn("painel", r.reply)  # nao promete caminho que nao foi verificado
        self.assertFalse(r.escalar)          # 88 de 90 lojas estao assim: oferece, nao escala
        self.assertIn("é só me dizer", r.reply)
        aceite = resp(["sim, por favor"], hist=[{"de": "contato", "texto": "o produto dipirona não aparece no site"},
                                                {"de": "assistente", "texto": r.reply}])
        self.assertEqual((aceite.intencao, aceite.escalar), ("oferta", True))


class TestFalhaConsulta(unittest.TestCase):
    def test_erro_de_banco_vira_humano(self):
        class Quebrado(FakeRepoSuporte):
            def cobertura_vendas(self, *a, **k):
                raise TimeoutError()
        r = resp(["o relatório de vendas está errado"], repo=Quebrado())
        self.assertEqual(r.acao, "HUMAN")
        self.assertEqual(r.reply, "")


class FakeConn:
    """Grava SQL e parametros: prova read-only, parametrizado e filtrado por loja."""
    class info:
        transaction_status = 0

    def __init__(self, rows):
        self.rows, self.log = rows, []

    def cursor(self):
        conn = self

        class C:
            def execute(self, sql, params=None):
                conn.log.append((sql, params))

            def fetchall(self):
                return conn.rows.pop(0) if conn.log[-1][0] != "SET TRANSACTION READ ONLY" else []

            def close(self):
                pass
        return C()

    def rollback(self):
        pass


class TestSQL(unittest.TestCase):
    def test_consultas_read_only_parametrizadas(self):
        conn = FakeConn([[("26102026",)], [("789", "DIPIRONA", 3)], []])
        S.RepoSuporte(conn).produto_estoque("11111111000111", termos=["dipirona'; drop table x;--"])
        sqls = [s for s, _ in conn.log]
        self.assertEqual(sqls.count("SET TRANSACTION READ ONLY"), 3)
        for s, p in conn.log:
            if s != "SET TRANSACTION READ ONLY":
                self.assertTrue(s.lstrip().upper().startswith("SELECT"))
                self.assertNotIn("drop", s.lower())
                self.assertEqual(p[0], "11111111000111")  # sempre a loja verificada

    def test_config_nao_le_tokens(self):
        conn = FakeConn([[(True, 1, None)]])
        S.RepoSuporte(conn).config_ecommerce("11111111000111")
        sql = conn.log[-1][0].lower()
        self.assertNotIn("token", sql)
        self.assertNotIn("pix", sql)
        self.assertNotIn("*", sql)


if __name__ == "__main__":
    unittest.main()
