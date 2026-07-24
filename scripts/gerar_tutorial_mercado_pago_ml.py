from __future__ import annotations

import html
import zipfile
from pathlib import Path


OUT = Path("docs/Tutorial_Mercado_Pago_Mercado_Livre_Poupaqui.docx")


def p(text: str = "", style: str | None = None) -> str:
    st = f'<w:pStyle w:val="{style}"/>' if style else ""
    if not text:
        return f"<w:p><w:pPr>{st}</w:pPr></w:p>"
    lines = text.split("\n")
    runs = []
    for i, part in enumerate(lines):
        if i:
            runs.append("<w:r><w:br/></w:r>")
        runs.append(f"<w:r><w:t>{html.escape(part)}</w:t></w:r>")
    return f"<w:p><w:pPr>{st}</w:pPr>{''.join(runs)}</w:p>"


def bullet(text: str) -> str:
    return (
        '<w:p><w:pPr><w:pStyle w:val="ListBullet"/></w:pPr>'
        f"<w:r><w:t>{html.escape(text)}</w:t></w:r></w:p>"
    )


def numbered(text: str) -> str:
    return (
        '<w:p><w:pPr><w:pStyle w:val="ListNumber"/></w:pPr>'
        f"<w:r><w:t>{html.escape(text)}</w:t></w:r></w:p>"
    )


def doc_xml(body: list[str]) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    {''.join(body)}
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>
'''


CONTENT = [
    p("Tutorial para configurar Mercado Pago, Mercado Livre e Mercado Envios no Poupaqui", "Title"),
    p("Objetivo", "Heading1"),
    p(
        "Este passo a passo orienta a loja a pegar as credenciais do Mercado Pago, configurar a conta do Mercado Livre para vender com Mercado Envios e preencher as telas corretas dentro do Poupaqui. O documento foi baseado no fluxo que já existe hoje no sistema."
    ),
    p("Resumo rápido", "Heading1"),
    bullet("Mercado Pago: cada loja informa suas próprias credenciais em Painel > Configurações da loja > Mercado Pago."),
    bullet("Mercado Livre: o sistema usa uma integração central do Poupaqui. A loja não cola token manualmente; ela usa Painel > Mercado Livre para conectar/reconectar, validar endereço e acompanhar pedidos."),
    bullet("Mercado Envios: é ativado no painel do Mercado Livre. No Poupaqui, os anúncios são publicados pelo Precificador com a opção Mercado Envios marcada."),
    bullet("Pedidos do Mercado Livre entram no painel da loja com origem Mercado Livre, pagamento Mercado Livre e tipo de entrega entrega."),
    p("Parte 1 - Mercado Pago", "Heading1"),
    p("Para que serve no Poupaqui", "Heading2"),
    p(
        "O Mercado Pago é usado pelo ecommerce próprio da loja para gerar PIX com QR Code, receber cartão e confirmar pagamento automaticamente. O sistema usa o Access Token no servidor e a Public Key no checkout transparente."
    ),
    p("O que a loja precisa ter antes", "Heading2"),
    bullet("Conta Mercado Pago ativa no CNPJ ou CPF correto da loja."),
    bullet("Acesso ao painel Mercado Pago Developers."),
    bullet("Credenciais de produção liberadas para vender de verdade."),
    p("Como pegar o Access Token e a Public Key", "Heading2"),
    numbered("Acesse https://www.mercadopago.com.br/developers/panel/app com o login da loja."),
    numbered("Entre em Suas integrações."),
    numbered("Abra uma aplicação existente ou crie uma nova aplicação para o ecommerce."),
    numbered("Entre em Credenciais de produção."),
    numbered("Copie o Access Token de produção. Normalmente ele começa com APP_USR-."),
    numbered("Copie também a Public Key de produção. Ela será usada para cartão dentro do checkout."),
    numbered("Guarde essas informações com segurança. O Access Token é uma chave privada e não deve ser enviado por WhatsApp aberto, planilha compartilhada ou e-mail sem controle."),
    p("Onde configurar no Poupaqui", "Heading2"),
    numbered("Entre no painel administrativo da loja no Poupaqui."),
    numbered("Abra Painel > Configurações da loja."),
    numbered("Na seção Formas de pagamento aceitas, marque PIX se a loja aceitar PIX automático."),
    numbered("Marque Cartão se a loja aceitar cartão pelo gateway."),
    numbered("No campo Gateway usado por esta loja, selecione Mercado Pago."),
    numbered("Na seção Mercado Pago, cole o Access Token no campo Access Token da sua conta Mercado Pago."),
    numbered("Cole a Public Key no campo Public Key da sua conta Mercado Pago."),
    numbered("Clique em Salvar configurações."),
    p("Como validar", "Heading2"),
    bullet("Faça uma compra de teste pequena no ecommerce da loja e verifique se o PIX aparece com QR Code ou se o cartão abre dentro do checkout."),
    bullet("No código existe a rota de teste /painel/config/mp-test, que consulta /users/me e tenta criar um PIX de R$ 1,00. Ela pode ser usada por suporte técnico quando necessário."),
    p("Cuidados importantes", "Heading2"),
    bullet("Use credenciais de produção para vendas reais. Credenciais de teste não recebem dinheiro real."),
    bullet("Se renovar as credenciais no Mercado Pago, substitua Access Token e Public Key no Poupaqui imediatamente."),
    bullet("Se cartão não abrir no carrinho, geralmente falta a Public Key ou ela está incorreta."),
    bullet("Se PIX ou cartão não confirmar automaticamente, revise o Access Token e a configuração de webhooks/notificações no ambiente técnico."),
    p("Parte 2 - Mercado Livre", "Heading1"),
    p("Como a integração funciona no Poupaqui hoje", "Heading2"),
    p(
        "A integração atual do código está no modelo Conta Mercado Livre por loja. Existe uma aplicação Mercado Livre configurada por variáveis técnicas do sistema, como ML_APP_ID, ML_CLIENT_SECRET e ML_REDIRECT_URI. Quando a loja conecta sua conta, o sistema salva access_token, refresh_token e validade vinculados ao CNPJ da loja na tabela ml_tokens_loja e renova automaticamente quando necessário."
    ),
    p(
        "Isso significa que cada loja não precisa procurar um token manual do Mercado Livre para colar no Poupaqui. O processo correto é entrar no painel da própria loja e autorizar a conta pelo botão Conectar conta Mercado Livre."
    ),
    p("Como conectar ou reconectar", "Heading2"),
    numbered("No Poupaqui, entre no painel da loja."),
    numbered("Abra o menu Mercado Livre."),
    numbered("Confira o Status da conexão."),
    numbered("Se aparecer Não conectado, clique em Conectar conta Mercado Livre."),
    numbered("O Mercado Livre abrirá a tela de autorização. Entre com a conta vendedora correta e autorize a aplicação."),
    numbered("Ao voltar para o Poupaqui, a tela deve mostrar Conta conectada e o user da conta."),
    numbered("Se já estiver conectado, mas pedidos ou publicações começarem a falhar, clique em Reconectar."),
    p("Dados técnicos exibidos no painel", "Heading2"),
    bullet("APP ID: identifica a aplicação Mercado Livre usada pelo Poupaqui."),
    bullet("Webhook URL: endpoint que recebe notificações de pedidos do Mercado Livre. No código é /ml/webhook."),
    bullet("Callback URL: endereço para onde o Mercado Livre retorna depois da autorização. No código atual aparece https://www.drogariaspoupaqui.com.br/ml/callback."),
    p("Parte 3 - Mercado Envios", "Heading1"),
    p("O que é necessário ativar no Mercado Livre", "Heading2"),
    numbered("Entre na conta vendedora do Mercado Livre."),
    numbered("Acesse https://www.mercadolivre.com.br/vender com a conta vendedora correta."),
    numbered("No Mercado Livre, entre pela área Vender/Vendas e procure as configurações da conta vendedora, endereço de despacho e envios."),
    numbered("Confira se o endereço de despacho da loja está cadastrado e se Mercado Envios está ativo para a conta."),
    numbered("Se o Mercado Livre pedir dados adicionais, complete o cadastro, endereço fiscal, telefone, horários e regras da conta."),
    numbered("Depois de ativado, os anúncios publicados com Mercado Envios passam a mostrar frete calculado pelo Mercado Livre para o comprador."),
    p("Eles buscam o produto na loja?", "Heading2"),
    p(
        "Depende da modalidade liberada pelo Mercado Livre para aquela conta, região e operação. Mercado Envios não significa automaticamente coleta na loja. Em muitos casos, a loja precisa imprimir a etiqueta, embalar o pedido e postar em agência, Correios, ponto de envio ou local indicado pelo Mercado Livre. Em outras contas/regiões, o Mercado Livre pode liberar coleta ou modalidades específicas. A regra válida sempre aparece no painel do Mercado Livre, dentro da venda e das configurações de envio."
    ),
    p("O que o Poupaqui faz no Mercado Envios", "Heading2"),
    bullet("No painel Mercado Livre, o sistema verifica se existe endereço de despacho cadastrado na conta."),
    bullet("No Precificador, ao publicar um produto no Mercado Livre, a opção Mercado Envios já vem marcada por padrão."),
    bullet("Tecnicamente, o sistema envia o anúncio com shipping.mode = me2, local_pick_up conforme marcado e free_shipping conforme marcado."),
    bullet("O Poupaqui não agenda coleta nem escolhe agência. Essa parte operacional fica dentro do Mercado Livre."),
    bullet("Quando o pedido ML entra no Poupaqui, o sistema busca o endereço do comprador pelo shipment_id e grava no pedido."),
    p("Como publicar produto com Mercado Envios pelo Poupaqui", "Heading2"),
    numbered("Entre no painel da loja."),
    numbered("Abra o Precificador."),
    numbered("Localize o produto desejado."),
    numbered("Clique no botão ML do produto."),
    numbered("Revise título, preço, quantidade, categoria, fotos e descrição."),
    numbered("Na seção Entrega, mantenha Mercado Envios marcado."),
    numbered("Marque Frete grátis somente se a estratégia comercial permitir absorver o custo conforme regras do Mercado Livre."),
    numbered("Marque Retirada na loja somente se a loja aceitar retirada local pelo anúncio."),
    numbered("Clique em Publicar no ML."),
    numbered("Após publicar, o sistema salva o ml_item_id, EAN, preço, categoria, CNPJ da loja que publicou e status do anúncio."),
    p("Parte 4 - Como o pedido do Mercado Livre aparece no Poupaqui", "Heading1"),
    p("Fluxo automático", "Heading2"),
    numbered("Cliente compra no Mercado Livre."),
    numbered("Mercado Livre notifica o Poupaqui pelo webhook /ml/webhook."),
    numbered("O sistema enfileira o order_id em ml_order_queue."),
    numbered("O sistema busca os dados do pedido na API do Mercado Livre."),
    numbered("O sistema identifica os itens e procura o produto publicado na tabela ml_items."),
    numbered("A loja atribuída é a loja que publicou o item pelo Poupaqui. Se o anúncio não tiver vínculo local, o sistema usa uma loja cadastrada como fallback."),
    numbered("O pedido é criado em ecommerce_pedidos com origem mercado_livre, forma de pagamento mercado_livre, status pago ou pendente conforme retorno do ML e tipo de entrega entrega."),
    numbered("O pedido aparece no menu Pedidos da loja com a identificação Mercado Livre."),
    p("Se o pedido não aparecer", "Heading2"),
    numbered("Abra Painel > Mercado Livre."),
    numbered("Na seção Importar pedido do Mercado Livre, cole o ID do pedido ML, apenas números."),
    numbered("Clique em Importar."),
    numbered("Se existirem pedidos presos na fila, clique em Sincronizar fila."),
    numbered("Se aparecer erro de autorização ou PolicyAgent, use Reconectar no painel Mercado Livre."),
    p("Parte 5 - Como a loja deve operar a entrega", "Heading1"),
    numbered("Quando vender pelo Mercado Livre, abra a venda no Mercado Livre para ver a etiqueta, prazo e forma exata de envio exigida."),
    numbered("Separe o produto na loja usando o pedido que apareceu no Poupaqui."),
    numbered("Embale conforme padrão do Mercado Livre e exigências de medicamentos/cosméticos quando aplicável."),
    numbered("Imprima e cole a etiqueta de envio pelo Mercado Livre, quando houver."),
    numbered("Poste ou entregue no ponto indicado pelo Mercado Livre, ou aguarde coleta somente se a conta tiver coleta habilitada e a venda indicar essa modalidade."),
    numbered("Atualize o status operacional no Poupaqui para manter a loja organizada."),
    numbered("Quando o pedido for entregue, o Poupaqui tenta avisar o Mercado Livre automaticamente ao confirmar entrega. Também existe o botão Avisar entrega no Mercado Livre em pedidos ML entregues."),
    p("Diferença entre entrega do ecommerce próprio e Mercado Envios", "Heading2"),
    bullet("Ecommerce próprio Poupaqui: a loja configura raio de entrega, valor de frete, pedido mínimo e operação local em Painel > Configurações da loja."),
    bullet("Mercado Livre com Mercado Envios: frete, prazo, etiqueta e regra de postagem/coleta são definidos pelo Mercado Livre."),
    bullet("Não misture os dois fretes. O raio de entrega do Poupaqui não define o frete do Mercado Livre."),
    p("Checklist para passar para a loja", "Heading1"),
    bullet("Conta Mercado Pago acessível e validada."),
    bullet("Access Token e Public Key de produção copiados do Mercado Pago Developers."),
    bullet("Gateway Mercado Pago selecionado no Poupaqui, com PIX/cartão marcados conforme a operação."),
    bullet("Conta Mercado Livre conectada no menu Mercado Livre do Poupaqui."),
    bullet("Endereço de despacho configurado no Mercado Livre."),
    bullet("Mercado Envios habilitado no perfil de envio do Mercado Livre."),
    bullet("Produto publicado pelo Precificador com Mercado Envios marcado."),
    bullet("Pedido de teste acompanhado no Mercado Livre e no Poupaqui."),
    bullet("Equipe da loja treinada para imprimir etiqueta, embalar e postar/coletar conforme instrução da venda no Mercado Livre."),
    p("Links úteis", "Heading1"),
    bullet("Mercado Pago Developers - aplicações: https://www.mercadopago.com.br/developers/panel/app"),
    bullet("Documentação Mercado Pago - credenciais: https://www.mercadopago.com.br/developers/pt/docs/checkout-pro/additional-content/credentials"),
    bullet("Mercado Livre - área de venda: https://www.mercadolivre.com.br/vender"),
    bullet("Mercado Livre - página inicial da conta: https://www.mercadolivre.com.br"),
]


CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
</Types>
'''

RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
'''

DOC_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
'''

NUMBERING = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="1">
    <w:multiLevelType w:val="singleLevel"/>
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/>
      <w:numFmt w:val="bullet"/>
      <w:lvlText w:val="•"/>
      <w:lvlJc w:val="left"/>
      <w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr>
    </w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="2">
    <w:multiLevelType w:val="singleLevel"/>
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/>
      <w:numFmt w:val="decimal"/>
      <w:lvlText w:val="%1."/>
      <w:lvlJc w:val="left"/>
      <w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr>
    </w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>
  <w:num w:numId="2"><w:abstractNumId w:val="2"/></w:num>
</w:numbering>
'''

STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos"/><w:sz w:val="22"/></w:rPr>
    <w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:rPr><w:b/><w:sz w:val="36"/><w:color w:val="111827"/></w:rPr>
    <w:pPr><w:spacing w:after="240"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:rPr><w:b/><w:sz w:val="28"/><w:color w:val="111827"/></w:rPr>
    <w:pPr><w:spacing w:before="260" w:after="120"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:rPr><w:b/><w:sz w:val="24"/><w:color w:val="374151"/></w:rPr>
    <w:pPr><w:spacing w:before="180" w:after="80"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="ListBullet">
    <w:name w:val="List Bullet"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr><w:ind w:left="720" w:hanging="360"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="ListNumber">
    <w:name w:val="List Number"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr><w:ind w:left="720" w:hanging="360"/></w:pPr>
  </w:style>
</w:styles>
'''


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out = OUT
    try:
        zf = zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED)
    except PermissionError:
        out = OUT.with_name(f"{OUT.stem}_corrigido.docx")
        zf = zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED)
    with zf as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/_rels/document.xml.rels", DOC_RELS)
        z.writestr("word/styles.xml", STYLES)
        z.writestr("word/numbering.xml", NUMBERING)
        z.writestr("word/document.xml", doc_xml(CONTENT))
    print(out.resolve())


if __name__ == "__main__":
    main()
