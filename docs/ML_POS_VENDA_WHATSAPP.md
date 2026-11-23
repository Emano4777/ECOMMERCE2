# Avisos de pós-venda — São Pedro

Destino: WhatsApp 55 19 99505-8165, vendedor ML 3501375634.

Fluxo independente de `ml_order_alerts.py`, da rota de novas vendas e do timer
`dns-ml-whatsapp.timer`. Nenhuma tabela de novas vendas é alterada.

O timer HostGator `dns-ml-aftersales.timer` consulta a cada dois minutos a rota
local autenticada `/api/cron/ml-aftersales-sao-pedro`, com o cliente
`scripts/cron_ml_aftersales.py` e `/etc/dns-ml-whatsapp.env`. Vercel e chamadas
externas são rejeitadas.

São consultados casos abertos do vendedor e casos encerrados atualizados desde
a ativação, com janela máxima de 90 dias. Reclamações, mediações, devoluções,
trocas e cancelamentos registrados como casos de pós-venda entram no fluxo.
Os detalhes de devolução são consultados quando o caso informa uma devolução.
Não inclui conversas comuns fora das reclamações, nem todos os cancelamentos
de pedidos que não gerem caso na API de pós-venda.

Cada mensagem mostra o caso, a situação, o motivo informado e, quando disponíveis,
a venda, os produtos, o prazo obrigatório do vendedor e o progresso da devolução.
Os produtos exibidos são os da venda; a mensagem orienta conferir no caso quais
itens foram afetados. O link usa `/vendas/{id}/detalhe`; se o recurso não for
uma venda, informa o recurso e aponta para a central.

`ml_wa_aftersales_config` registra a ativação. `ml_wa_aftersales_events` guarda o
texto e um hash do estado da ocorrência, ações/prazos, atualização do caso e
progresso da devolução. Um evento enviado não é reenviado em consultas iguais.
Mudanças geram um novo evento. Falhas de envio permanecem pendentes para nova
tentativa; é enviado no máximo um evento por execução. Como em qualquer envio
externo sem chave de idempotência do provedor, uma falha entre o envio confirmado
e a gravação no banco pode gerar repetição na recuperação.

Um lock transacional PostgreSQL serializa as execuções, inclusive com pooler em
modo transacional. O token é obtido antes desse lock. A conta e o vendedor da
venda/caso são conferidos antes de compor o alerta. O fluxo apenas avisa: não
responde clientes, aprova devoluções ou executa reembolsos.

Verificação: `systemctl status dns-ml-aftersales.timer` e
`journalctl -u dns-ml-aftersales.service`. Testes unitários em
`tests/test_ml_aftersales_alerts.py`; teste PostgreSQL isolado em
`tests/ml_aftersales_postgres_check.py` usa provedor falso e não envia mensagens.

Referências: documentação oficial Mercado Livre, Gerenciar reclamações e
Gerenciar devoluções (`post-purchase/v1/claims` e `post-purchase/v2/claims/{id}/returns`).
