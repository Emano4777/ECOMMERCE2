"""Conversas reais (prints de 09/11/2026): a IA nao pode ficar em "nao entendi / pergunte de outro jeito",
repetir a mesma triagem nem responder "a pagina abre normalmente" para um import que nao atualiza.
O certo: entender o que foi relatado e abrir um chamado para o Emanuel, acalmando o contato. Offline."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import poupaqui_ai_assistente as A  # noqa: E402
import poupaqui_ai_suporte as S  # noqa: E402
from tests.test_wa_assistente import LOJA_A, NAO_VERIF  # noqa: E402
from tests.test_wa_suporte import resp  # noqa: E402

PROIBIDO = ("fiquei devendo", "de outro jeito", "nao entendi", "outras palavras", "pode me contar o que aconteceu")


def n(txt):
    return A.normalize(txt)


class TestImportEErroDoServidor(unittest.TestCase):
    def test_import_nao_atualiza_abre_chamado_sem_interrogar(self):
        for frase in ["Não está atualizando o IMPORT, está com algum bug no sistema mesmo?",
                      "Está com problema para baixar o import?",
                      "a cotação não atualiza desde cedo"]:
            r = resp([frase])
            self.assertEqual(r.acao, "ANSWER", frase)
            self.assertTrue(r.escalar, frase)
            self.assertNotIn("nao consegue entrar", n(r.reply), frase)
            self.assertNotIn("clica em enviar", n(r.reply), frase)
            self.assertIn("nao prova", n(r.reply), frase)

    def test_print_com_internal_server_error(self):
        hist = [{"de": "contato", "texto": "Está com problema para baixar o import?"}]
        r = resp(["[Imagem enviada pelo contato] Internal Server Error The server encountered an internal error",
                  "quando vou baixar o import aparece esta mensagem"], hist=hist)
        self.assertTrue(r.escalar)
        self.assertIn("internal server error", n(r.reply))
        self.assertIn("nada que voce fez", n(r.reply))
        self.assertNotIn("sobre o pedido", n(r.reply))          # import nao e pedido
        for p in PROIBIDO:
            self.assertNotIn(p, n(r.reply))

    def test_print_de_erro_sozinho(self):
        r = resp(["[Imagem enviada pelo contato] Internal Server Error"])
        self.assertTrue(r.escalar)
        for p in PROIBIDO:
            self.assertNotIn(p, n(r.reply))

    def test_seguimento_curto_herda_o_import(self):
        hist = [{"de": "contato", "texto": "Não está atualizando o IMPORT"},
                {"de": "assistente", "texto": "Verifiquei agora: a página abre normalmente."}]
        r = resp(["então, o problema é que não atualiza e pode ser que eu não consiga os valores do dia"],
                 hist=hist)
        self.assertTrue(r.escalar)
        for p in PROIBIDO:
            self.assertNotIn(p, n(r.reply))

    def test_deteccao(self):
        self.assertEqual(S.detectar_suporte(n("nao esta atualizando o import")), "pe_fora")
        self.assertEqual(S.detectar_suporte(n("internal server error")), "pe_fora")
        self.assertEqual(S.extrair_relato(n("baixar o import"))["acao"], "arquivo")
        self.assertIsNone(S.detectar_suporte(n("qual o horario de corte da germed")))


class TestSemBecoSemSaida(unittest.TestCase):
    def test_sem_vinculo_que_ja_contou_o_problema_vira_chamado(self):
        r = resp(["então, o problema é que não atualiza o pedido e pode ser que eu não consiga os valores"],
                 ident=NAO_VERIF)
        self.assertTrue(r.escalar)
        for p in PROIBIDO:
            self.assertNotIn(p, n(r.reply))

    def test_sem_vinculo_pergunta_objetiva_explica_e_abre_chamado(self):
        for frase in ["meu pedido chegou?", "qual horario de corte da germed?"]:
            r = resp([frase], ident=NAO_VERIF)
            self.assertTrue(r.escalar, frase)
            self.assertIn("nao esta vinculado", n(r.reply), frase)
            for p in PROIBIDO:
                self.assertNotIn(p, n(r.reply))

    def test_frase_pronta_longa_nao_se_repete(self):
        r1 = resp(["deu problema"], ident=LOJA_A)
        self.assertFalse(r1.escalar)                     # vago: pergunta uma vez
        hist = [{"de": "contato", "texto": "deu problema"}, {"de": "assistente", "texto": r1.reply}]
        r2 = resp(["deu problema"], ident=LOJA_A, hist=hist)
        self.assertNotEqual(n(r1.reply), n(r2.reply))
        self.assertTrue(r2.escalar)

    def test_modelo_falhou_vira_chamado_e_nao_fiquei_devendo(self):
        from tests.test_wa_suporte import FakeRepoSuporte

        def llm_quebrado(textos, historico, **kw):
            raise RuntimeError("timeout")
        r = A.responder(["consegue me ajudar a montar a apresentação da reunião de amanhã?"], (), LOJA_A, [],
                        FakeRepoSuporte(), None, llm_quebrado)
        self.assertEqual(r.acao, "ANSWER")
        self.assertTrue(r.escalar)
        for p in PROIBIDO:
            self.assertNotIn(p, n(r.reply))

    def test_seguimento_de_chamado_aberto_e_curto(self):
        r1 = resp(["Não está atualizando o IMPORT"])
        aberto = A.completar_resposta(r1.reply, "enviado", 296, r1.equipe)
        hist = [{"de": "contato", "texto": "Não está atualizando o IMPORT"}, {"de": "assistente", "texto": aberto}]
        r2 = resp(["[Imagem enviada pelo contato] Internal Server Error"], hist=hist)
        self.assertTrue(r2.escalar)
        self.assertNotIn("verifiquei", n(r2.reply))
        self.assertIn("servidor", n(r2.reply))
        r3 = A.responder(["Emanuel?"], (), LOJA_A, hist, None, None, None)
        self.assertIn("chamado", n(r3.reply))
        for p in PROIBIDO:
            self.assertNotIn(p, n(r3.reply))

    def test_frase_do_chamado_cita_o_dono_e_acalma(self):
        f = A.frase_encaminhamento("enviado", 296)
        self.assertIn(f"chamado para o {A.DONO_NUMERO}", f)
        self.assertIn("protocolo 296", f)
        self.assertIn("preocupar", f)
        self.assertIn("equipe financeira", A.frase_encaminhamento("enviado", 1, "equipe financeira"))
        self.assertTrue(A.promete_encaminhamento("Já abri um chamado para você"))


if __name__ == "__main__":
    unittest.main()
