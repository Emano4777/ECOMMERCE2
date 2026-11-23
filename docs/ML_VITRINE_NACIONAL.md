# Vitrine nacional Mercado Livre

Somente os anúncios registrados para São Pedro são elegíveis. São filtrados
vendedor, status ativo, estoque positivo, links públicos e imagens HTTPS.
O Anasol Kids FPS 90 retirado pelo lote de dezembro/2026 é excluído por EAN.

## Atualização no HostGator

`dns-ml-national-catalog.timer` executa a cada dois minutos
`scripts/cron_ml_catalog.py`, usando `/etc/dns-ml-whatsapp.env` para a credencial
já existente. O cliente chama exclusivamente o loopback
`http://127.0.0.1:8002/api/cron/ml-national-catalog`.
O endpoint exige autenticação, origem local e rejeita o ambiente Vercel.

O cron atualiza atomicamente `/root/scripts/dns_ecommerce_web/ml-national-catalog.json`.
Esse arquivo contém apenas os dados públicos dos cards. Falhas de consulta não
substituem a última versão válida. Após dez minutos sem atualização, os cards
ficam ocultos para evitar divulgar estoque antigo.

`/api/produtos-nacionais` apenas lê esse arquivo e filtra a busca. Não consulta
Mercado Livre, tokens ou banco de dados. Visitantes não disparam sincronizações.
Não existe agendamento dessa vitrine na Vercel.

Verificação: `systemctl status dns-ml-national-catalog.timer` e
`journalctl -u dns-ml-national-catalog.service`.
