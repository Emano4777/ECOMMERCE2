"""Reported regressions. No delivery, no database, no real contact messages."""
import json
from unittest.mock import Mock,patch
import poupaqui_ai_assistente as A
from tests.test_wa_assistente import resp,NAO_VERIF


def test_document_with_social_question_is_acknowledged_without_model_or_sending():
    model=Mock(side_effect=AssertionError('A greeting needs no model to acknowledge a file'))
    result=resp(['Boa noite, Emanuel... Tudo bem? COTAÇÃO OK','[Arquivo: DISMAP 03.11 (ESTOQUE ATUALIZADO).xlsx]'],
        tipos=['text','document'],llm=model)
    assert result.intencao=='arquivo'
    assert 'Recebi' in result.reply and 'DISMAP' in result.reply


def test_public_link_is_not_a_private_order_lookup_or_incident():
    result=resp(['Em qual e-mail posso mandar? Também preciso que reenvie o link do pedido eletrônico'],NAO_VERIF)
    assert result.intencao=='guia_publico'
    assert 'https://pedidoeletronico.poupaqui.com.br/' in result.reply
    assert 'e-mail confirmado' in result.reply
    assert 'Conferi de novo' not in result.reply


def test_general_business_discussion_is_not_rejected_for_mentioning_the_subject():
    assert A.problema_conversa('Entendi, vocês já usam ferramentas de CRM no acompanhamento dos clientes.',
        ['Falamos de gestão de relacionamento com clientes.']) is None
    assert A.problema_conversa('Podemos conversar sobre o ecommerce e a implantação nas lojas.',
        ['Tenho interesse em conhecer.']) is None


def test_unverified_calendar_cannot_be_confirmed():
    assert A.problema_conversa('Segunda funciona sim, pode marcar.', ['Podemos marcar segunda?'])


def test_operator_offer_is_preserved_without_changing_human_takeover():
    history=[{'de':'operador','texto':'Tem interesse em conhecer o novo ecommerce e aplicativo?'},
             {'de':'contato','texto':'Tenho sim'}]
    response=Mock();response.read.return_value=json.dumps({'done_reason':'stop','message':{
        'content':'Entendi, você quer conhecer a ferramenta.'}}).encode()
    response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
    with patch.object(A.urllib.request,'urlopen',return_value=response) as request:
        A.ollama_geral()(['Sobre isso'],history)
    body=json.loads(request.call_args.args[0].data)
    assert any('operador humano Emanuel' in m['content'] and 'ecommerce' in m['content'] for m in body['messages'])


def test_arrival_in_a_story_does_not_become_an_order_incident():
    history=[{'de':'contato','texto':'O motoboy do iFood confundiu a avenida com a cidade.'}]
    result=resp(['No feriado o motoboy confundiu o endereço, chegou aqui achando ruim','Falei não tenho culpa, é sistema'],
        NAO_VERIF,hist=history,llm=lambda *_:'Entendi, ele confundiu os lugares e ainda reclamou com você.')
    assert result.intencao=='geral'
    assert 'Pedido Eletrônico' not in result.reply
