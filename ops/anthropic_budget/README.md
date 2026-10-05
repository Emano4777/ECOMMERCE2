# Orcamento Anthropic compartilhado

HostGator: servico `anthropic-budget.service`, somente `127.0.0.1:8769`.
Limite autorizado: **US$15/mes**, sem limite diario independente. Config privada em
`/root/scripts/anthropic_budget/config.json`: enabled=true, monthly_usd=15, daily_usd=null.
Periodos usam America/Sao_Paulo; contagem comeca na ativacao, sem reconstruir gastos anteriores.

Banco persistente `/root/scripts/anthropic_budget/usage.sqlite3`, fora dos deploys.
Reservas atomicas somam chamadas concorrentes dos dois sites e crons; modelo sem tarifa,
streaming e ferramentas pagas de servidor nao suportadas sao bloqueados.
Consulta token count antes de reservar entrada com margem e saida maxima; reconcilia por usage.
Custos sao estimados pelas tarifas oficiais, nao sao a fatura. Falhas ambiguas mantem reservas.
Sem saldo/auth pausa 24h; rate limit 1h; rede 15min. Nada de fallback direto para Anthropic.
Nao armazena chaves, prompts nem respostas; apenas rotina, modelo, tokens, estado e custo.

Relatorio: `/root/scripts/dns_ecommerce_web/venv/bin/python3.11 /root/scripts/anthropic_budget/anthropic_usage_report.py --month 2026-10`.
Para alterar limite: editar config privada atomicamente. Nao precisa reiniciar gateway.
Nao zere o SQLite ao atualizar: ele preserva o teto entre reinicios/deploys.
Ao atualizar servico: copiar arquivos para /root/scripts/anthropic_budget e reiniciar anthropic-budget.
Os helpers de cada repositorio/pasta scripts encaminham as chamadas e identificam sua rotina.
Chamadas feitas fora destes sistemas, com a mesma chave, nao passam por este controle local.

Fontes: https://platform.claude.com/docs/en/about-claude/pricing e
https://platform.claude.com/docs/en/api/messages/count_tokens (consulta 2026-10-05).
