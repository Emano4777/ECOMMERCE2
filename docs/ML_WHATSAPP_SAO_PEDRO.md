# Aviso de vendas Mercado Livre — São Pedro

Ativo para CNPJ 54185432000143, vendedor 3501375634, WhatsApp 5519995058165.
Não altera publicação, estoque ou pausa dos anúncios. Não emite nota fiscal.

O timer HostGator `dns-ml-whatsapp.timer` consulta a cada minuto o endpoint
POST `/api/cron/ml-whatsapp-sao-pedro`. O cliente é `scripts/cron_ml_whatsapp.py`.
O serviço usa `/etc/dns-ml-whatsapp.env` (modo 600), com `ML_WA_CRON_SECRET`
e `ML_WA_BASE_URL=http://127.0.0.1:8002`. Usa a mesma autenticação dos crons
existentes, sem expor credenciais na mensagem ou na linha de comando.

Somente status `paid`, vendedor confirmado por `/users/me`, e vendas com
`date_closed >= activated_at` são elegíveis. O cursor de consulta fica em
`ml_wa_alert_config`; há sobreposição de dez minutos para consistência eventual.
`ml_wa_order_alerts` é uma fila persistente independente do aviso do e-commerce.
Falhas ficam pendentes para tentativa posterior; uma venda é enviada por ciclo.
Um advisory lock PostgreSQL impede execução concorrente. Como qualquer envio
externo sem chave de idempotência do provedor, uma queda entre a aceitação pelo
WhatsApp e o commit pode excepcionalmente causar repetição.

O prazo vem de `/shipments/{id}/sla`, campo `expected_date`, convertido para
America/Sao_Paulo. Se indisponível, o aviso informa que é preciso conferir a venda.
O link exige login do administrador ou colaborador autorizado na conta vendedora.
O aviso inclui a previsão de liberação da etiqueta de `/shipments/{id}/lead_time`,
campo `buffering.date`. Esse campo representa o dia programado e não é convertido
para o dia anterior pelo fuso de Brasília. A mensagem não promete um horário de
liberação e lembra de respeitar o limite de recebimento da agência. Quando o envio
está `ready_to_ship` com `ready_to_print` ou `printed`, informa etiqueta disponível.
Sem informação válida, orienta conferir a venda. Avisos já enviados não são reenviados.

Os pedidos ML desta loja são excluídos dos crons genéricos de WhatsApp e Alpha.
`alpha_sync.export_paid_order` também impede exportá-los, inclusive manualmente,
durante a migração do ERP. As demais lojas e o e-commerce mantêm seu fluxo.

Operação: `systemctl status dns-ml-whatsapp.timer` e
`journalctl -u dns-ml-whatsapp.service`. Para parar somente este aviso:
`systemctl disable --now dns-ml-whatsapp.timer`.

Validação: testes de elegibilidade, conteúdo, fuso horário e prazo ausente;
verificação PostgreSQL de falha/reenvio/deduplicação com remetente simulado;
consulta autenticada real à conta e ao endpoint no servidor, sem mensagem teste.
