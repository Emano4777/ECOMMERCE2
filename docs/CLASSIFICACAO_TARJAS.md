# Classificacao automatica das tarjas do e-commerce

`scripts/classificar_tarjas_ecommerce.py` grava a classificacao em `anvisa_cache`,
por chave `EAN:<codigo>`. Nao altera familias de medicamentos nem arquivos de imagem.
Tarja vermelha/preta bloqueia a foto comercial; isentos permitem a foto original.
O cache por EAN ja e consumido pelo catalogo, pagina do produto, API e carrinho.

## Catalogo e lojas novas

- Usa `ecommerce_alpha_produtos`, compartilhada por `alpha_sync.py` e
  `automatiza_sync.py`. Inclui as lojas integradas antes da abertura publica.
- Inclui estoque legado e `automatiza_estoque` para lojas publicas sem catalogo
  integrado. Nao mistura estoque antigo quando a loja ja migrou para a integracao.
- Seleciona ativos com estoque e deduplica por EAN entre lojas. Assim como a vitrine,
  ocultacoes legadas nao se aplicam ao catalogo integrado; no estoque legado sao respeitadas.
- `--cnpj` e opcional: por padrao processa todas as lojas elegiveis. Novas lojas
  integradas entram sem editar o cron. Nao acessa o MySQL local da loja.

## Evidencias e atualizacao automatica

1. Descobre e baixa a lista PMC atual no site da Anvisa, com cache de 24 horas,
   SHA-256 e verificacao de data, tamanho, cabecalho e quantidade de linhas.
2. Identifica colunas pelo cabecalho, compara EAN exato, nomes e doses. So aplica
   automaticamente quando todas as linhas oficiais daquele EAN possuem a mesma
   tarja explicita. `- (*)` e ausencia de resultado nao significam isencao.
3. Os casos restantes entram em lotes de pesquisa online (20 por execucao).
   Prioriza EANs ainda nao pesquisados e possiveis medicamentos sem revisao previa.
   Suporta Tavily, Brave, Serper e busca web Anthropic. Baixa apenas fontes
   primarias de dominios permitidos, inclusive PDFs, e arquiva o documento original.
4. O modelo identifica os blocos numerados de identidade/apresentacao e dizeres
   legais. Os trechos sao obtidos diretamente do documento, nao redigidos pela IA. O codigo verifica
   se as citacoes existem no documento, se a classificacao e explicita e se ha
   correspondencia de EAN ou de nome/numeros da apresentacao e fabricante.
   Receita sem cor explicita nao prova tarja vermelha sem corroboracao CMED;
   retencao de receita nao prova tarja preta. Snippets e varejistas nao sao prova.
5. Comprovado: grava automaticamente. Contraditorio/incompleto: preserva o cadastro
   anterior, registra motivo e tenta novamente apos sete dias. Uma revisao manual
   anterior divergente nunca e sobrescrita automaticamente.

Nao existe promessa de classificacao de 100% dos EANs: falta de documentos,
bloqueios dos fabricantes, descricoes incompletas e falta de credito impedem
confirmacao. A IA nao tem permissao de gravar diretamente nem executar comandos.
A rotina trata tarja/imagem; nao reclassifica regras de retencao ou venda online.

## Operacao

Dependencias: requirements.txt e `pypdf==6.19.0` (requirements-tarjas.txt).
Variaveis: DATABASE_URL, ANTHROPIC_API_KEY e a chave do provedor escolhido:
TAVILY_API_KEY, BRAVE_SEARCH_API_KEY ou SERPER_API_KEY/SERPER_API_KEYS.
`--search-provider auto` prioriza Tavily, Brave e Serper, nessa ordem, conforme as
chaves existentes. Anthropic web search exige `--search-provider anthropic`;
nao existe troca silenciosa para busca paga com tokens adicionais.
Modelo opcional: TARJA_AUDIT_MODEL; padrao claude-haiku-4-5-20251001.
Pesquisa/modelo consomem os creditos das contas configuradas. Cada produto faz no
maximo duas pesquisas e uma extracao; erros interrompem o lote apos tres falhas.
`--online-daily-limit 20` limita a 20 produtos por dia UTC, inclusive tentativas
com falha e simulacoes. O contador persiste entre reinicios. CMED nao usa creditos.
Na instalacao atual, o cron fixa `--search-provider tavily`. Ha teto adicional
rigido de 1.000 creditos Tavily por mes UTC, mesmo se a conta tiver bonus ou plano
maior. Antes de CADA consulta basic (1 credito), verifica GET /usage e reserva o
credito em `tavily_credits.json`. Timeouts nao devolvem reserva. Consumo desconhecido
bloqueia a busca. Ao esgotar, continua CMED e retoma busca no mes seguinte.
Nao utiliza extract/crawl/research da Tavily nem fallback pago. A leitura de sites
e PDFs e direta. O custo da extracao por Anthropic continua usando o saldo de IA.

```
python scripts/classificar_tarjas_ecommerce.py --env .env --state-dir /caminho/privado --online-limit 0
python scripts/classificar_tarjas_ecommerce.py --env .env --state-dir /caminho/privado --online-limit 20 --apply
python -m unittest discover -s scripts -p test_classificar_tarjas.py
```

Sem `--apply`: simulacao, sem alteracoes no banco. Documentos/checkpoints de busca
podem ser gravados no diretorio de estado mesmo em simulacao.

HostGator: `scripts/run_classificar_tarjas.sh`, agendado no minuto 23 de cada hora,
horario de Brasilia. O agendamento anterior de `run_anvisa_sync_alpha.sh` e
substituido (backup do crontab antes da alteracao). Importacoes Alpha/Automatiza
continuam rodando normalmente. Nenhum e-mail e enviado.

Estado privado: `/root/scripts/dns_ecommerce_web/tarja_state`.
`latest_todas.json`: contagens, lojas, motivos, decisoes e fontes por EAN.
`sources/`: documentos originais por hash. `cron.log`: falhas e resumos.
`ecommerce_tarja_historico`: registro anterior completo e evidencia de cada mudanca,
na mesma transacao da atualizacao, permitindo restauracao por EAN/run_id.
O lock PostgreSQL impede execucoes concorrentes; cada produto tem transacao propria.
Falha de download CMED aborta sem usar arquivo antigo como se fosse atualizado.

Fonte CMED: https://www.gov.br/anvisa/pt-br/assuntos/medicamentos/cmed/precos


### Fallback e retomada
O cron usa `tavily-serper`: Tavily permanece limitado a 1.000 creditos mensais, depois tenta as chaves Serper existentes. Sem saldo, cada chave espera 24h para uma nova verificacao; HTTP 429 espera 1h e erros transitorios 15 minutos. O estado persistente `search_backoff.json` guarda apenas hashes das chaves. O teto Tavily reabre no proximo mes UTC. Sem provedores, as consultas online param; CMED continua.

O classificador `scripts/classificar_medicamentos.py --only-estoque` reaproveita classificacoes completas salvas, deduplica EANs e processa somente pendencias da base medicamentos presentes no estoque integrado (Alpha e Automatiza). `--ean` respeita o cache; somente `--force` refaz a IA. Bloqueio no banco impede execucoes simultaneas. Produtos sem registro em medicamentos nao entram neste classificador.

Dependencia adicional do classificador farmaceutico: `pip install -r requirements-classificacao.txt`. Cron diario na HostGator: 04:43 (relogio do servidor), ate 500 EANs novos por rodada, com trava para evitar sobreposicao. Log: `/root/scripts/dns_ecommerce_web/classificacao_state/cron.log`.

Classificacoes verificadas por EAN no anvisa_cache (tarja valida, revisao, fonte e apresentacao) encerram a fila e nao expiram. Decisoes online positivas no historico tambem nao expiram enquanto a identidade confere. Apenas consultas inconclusivas mantem intervalo de 7 dias; falhas de provedor seguem o circuito de pausa. Palpites antigos sem evidencia nao contam como verificacao.
