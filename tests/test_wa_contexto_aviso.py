"""Conversas reais de 11/11/2026: complemento de aviso, 'Vi a imagem' depois da saudacao e onde mandar e-mail."""
import unittest

import poupaqui_ai_assistente as A
from tests.test_wa_assistente import FakeRepo

NV = {'verificada': False}
LLM = lambda textos, historico, **k: 'Entendi!'
LIVE = [{'de': 'contato', 'texto': '[Imagem enviada pelo contato] Bom dia Emanuel tudo bem, segue itens da Live Febrafar, '
                                    'via Santa Cruz e Panpharma, peço que divulgue aos associados. Ótimas vendas pra nós'},
        {'de': 'contato', 'texto': '[Arquivo: LIVE FEBRAFAR 11.11_Santa cruz e Panpharma.xlsx]'},
        {'de': 'assistente', 'texto': 'Bom dia! Vi a imagem. Recebi a planilha LIVE FEBRAFAR 11.11.xlsx, obrigado! '
                                      'Fica aqui para o Emanuel ver.'},
        {'de': 'contato', 'texto': 'Obrigada, lembrando que as ofertas tem validade pra hoje'},
        {'de': 'assistente', 'texto': 'Entendido! Vou reforçar com a galera que as ofertas valem só até hoje.'}]


class ContextoAvisoTests(unittest.TestCase):
    def test_complemento_da_oferta_nao_vira_pedido_de_dados(self):
        r = A.responder(['Faturamento via Mercado Farma'], ['text'], NV, LIVE, FakeRepo(), None, LLM)
        self.assertEqual((r.acao, r.escalar), ('ANSWER', False))
        self.assertEqual(r.reply, 'Anotado: faturamento via Mercado Farma. Fica junto com o que você mandou para o '
                                  'Emanuel ver.')
        self.assertNotIn('número cadastrado', r.reply)

    def test_sem_aviso_antes_ou_com_pergunta_segue_o_fluxo_normal(self):
        conversa = [{'de': 'contato', 'texto': 'oi'}, {'de': 'assistente', 'texto': 'Oi! Tudo bem?'}]
        self.assertIsNone(A._complemento_de_aviso(['Faturamento via Mercado Farma'], 'faturamento via mercado farma', conversa))
        self.assertIsNone(A._complemento_de_aviso(['Qual o faturamento de outubro?'], 'qual o faturamento de outubro',
                                                  LIVE))
        self.assertIsNone(A._complemento_de_aviso(['o sistema caiu'], 'o sistema caiu', LIVE))
        self.assertIsNone(A._complemento_de_aviso(['preciso do relatório de vendas'], 'preciso do relatorio de vendas',
                                                  LIVE))

    def test_vi_a_imagem_vem_depois_da_saudacao(self):
        self.assertEqual(A._com_aviso_midia('Boa tarde! Recebi o objetivo de novembro.', 'Vi a imagem.'),
                         'Boa tarde! Vi a imagem. Recebi o objetivo de novembro.')
        self.assertEqual(A._com_aviso_midia('Recebi a planilha.', 'Vi a imagem.'), 'Vi a imagem. Recebi a planilha.')
        self.assertEqual(A._com_aviso_midia('Bom dia, tudo bem? Recebi.', 'Vi a imagem.'),
                         'Vi a imagem. Bom dia, tudo bem? Recebi.')

    def test_onde_mandar_por_email_e_este_whatsapp(self):
        for pergunta in ('Em qual e-mail posso mandar ?', 'qual email pra enviar os dados?', 'posso mandar por email?'):
            r = A.responder([pergunta], ['text'], NV, [], FakeRepo(), None, LLM)
            self.assertEqual(r.reply, 'Pode mandar por aqui mesmo, neste WhatsApp, que chega direto para o Emanuel.',
                             pergunta)
            self.assertFalse(r.escalar)
        r = A.responder(['não recebi o e-mail que vocês mandaram'], ['text'], NV, [], FakeRepo(), None, LLM)
        self.assertNotIn('neste WhatsApp', r.reply)


if __name__ == '__main__':
    unittest.main()


class CombinadoComODonoTests(unittest.TestCase):
    """Tamires (19/11/2026): 'Vamos fechar? Mas consigo o Pix só semana que vem' recebia 'este número não está
    vinculado a uma loja… abri um chamado'. Combinado/negociação com o dono é conversa, não pedido de dado da loja."""

    def test_combinado_de_pagamento_e_conversa(self):
        llm = lambda textos, historico, **k: 'Combinado! Fica para a semana que vem.'
        for msg in ('Olá bom dia!\n\nVamos fechar ? Mas consigo o Pix só semana que vem 🥺', 'vou pagar o boleto amanhã'):
            r = A.responder([msg], ['text'], NV, [], FakeRepo(), None, llm)
            self.assertNotIn('vinculado', r.reply, msg)
            self.assertFalse(r.escalar, msg)

    def test_pedido_de_dado_da_loja_continua_explicando(self):
        for msg in ('cadê meu boleto?', 'qual o status do meu pedido da germed?', 'manda a segunda via do boleto'):
            r = A.responder([msg], ['text'], NV, [], FakeRepo(), None, LLM)
            self.assertTrue(r.escalar, msg)  # abre o chamado para o Emanuel
            final = A.completar_resposta(r.reply, 'enviado', 633)
            self.assertNotIn('vinculado', final, msg)  # resposta simples, sem explicar vínculo de número
            self.assertIn('Abri um chamado para o Emanuel verificar (protocolo 633)', final)

    def test_toda_resposta_da_ia_e_assinada(self):
        import poupaqui_wa_atendimento as W
        self.assertEqual(W.assinar_ia('Combinado!'), 'Combinado!\n\n_🤖 IA do Emanuel_')
        self.assertEqual(W.assinar_ia(W.assinar_ia('Oi')), W.assinar_ia('Oi'))  # não assina duas vezes
