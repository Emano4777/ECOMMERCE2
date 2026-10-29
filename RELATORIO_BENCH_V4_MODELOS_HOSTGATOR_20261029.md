# Benchmark v4 — modelos no ollama-piloto HostGator (29/10/2026, 08:50–09:50 BRT)

Continuação de `RELATORIO_BENCH_V3_MODELOS_HOSTGATOR_20261029.md`. Nada foi reinstalado e nenhum teste concluído foi repetido. Nenhuma mensagem foi enviada pelo WhatsApp, o banco e o worker não foram tocados e a produção não foi alterada.

## 1. O que foi feito nesta sessão
1. Conferi o estado antes de agir: o v3 estava concluído e o servidor saudável (load 0,88, 12,5 GiB livres, piloto ocioso).
2. Importei dois modelos mais atuais que cabem em 6 GiB, com `hostgator_import_model_blobs.py` e SHA256 verificado: **ministral-3:3b** (Mistral, dez/2025, 2,95 GB) e **gemma3:4b** (3,35 GB). Descartei por tamanho gemma3n (7,8–9,8 GB), qwen3.5:9b (6,55 GB) e ministral-3:8b (6,0 GB).
3. Rodei o v3 inalterado nos dois modelos novos, para comparar direto com os anteriores: `HOSTGATOR_BENCH_V3_MINISTRAL3_3B.json` e `HOSTGATOR_BENCH_V3_GEMMA3_4B.json`.
4. Criei o **v4** (`scripts/hostgator_bench_v4_remote.py`, `_run.py`, `_score.py`):
   - Prompt com exemplos de tom e de ironia.
   - Fatos fixos injetados como viriam do catálogo: dipirona/paracetamol isentos; amoxicilina e Rivotril com receita.
   - Regra de emergência (SAMU 192).
   - 21 cenários: os 14 do v3 mais sarcasmo na fila, ironia de preço, alergia citada 2 falas antes, regionalismo nordestino, elogio sincero (para pegar falsa ironia), intoxicação de criança e "morrendo de dor kkk".
5. Rodei o v4 em qwen3.5, qwen3, gemma3 (2 repetições), gemma4:e2b (1 repetição) e qwen3.5 com `think=true` (1 repetição).
6. Testei o gemma4:e4b com **aumento temporário só de memória** (MemoryHigh 8,5G / MemoryMax 9,5G, CPU inalterada), usando um vigia (`scripts/hostgator_limite_temporario_watchdog.py`).

## 2. Notas (0–10, leitura humana de todas as respostas mais o avaliador automático)

| Modelo (prompt v4) | PT-BR natural | Gírias/humor | Ironia/sarcasmo | Contexto | Confiabilidade | Velocidade (mediana) | **Geral** |
|---|---|---|---|---|---|---|---|
| **gemma4:e2b** | 6,5 | 5 | 7 (3/4) | 5 | **9** (zero invenção) | 5 (8,8 s; 4,7 tok/s) | **7,0** |
| qwen3.5:4b-q4_K_M | 6 | 6 | 6 | 6 | 6 | 5 (9,9 s; 15 tok/s) | 5,5 |
| qwen3:4b-instruct | 8 | 8 | 6 | 7 | **2** | 9 (3,6 s) | 4,0 |
| ministral-3:3b (só v3) | 4 | 5 | 4 | 5 | 3 | 9 (3,3 s) | 3,0 |
| gemma3:4b | 8 | 7 | 3 | 3 | **1** | 6 (8,0 s) | 2,5 |
| qwen3.5 + think | — | — | — | — | — | 0 (55 s, resposta vazia) | 1,0 |
| gemma4:e4b | não avaliado: não cabe com segurança (ver §4) | | | | | | — |

Avaliador automático do v4 (regras endurecidas para horário/estoque inventado): gemma4:e2b **20/21**, qwen3 35/42, qwen3.5 34/42, gemma3 28/42, qwen3.5+think 0/21.

### Destaques por modelo
- **gemma4:e2b**: não inventou pedido, preço, horário, estoque nem regra de receita.
  - Acertos: na intoxicação da criança mandou ligar para o SAMU 192 e ir ao pronto-socorro. Percebeu a ironia da entrega ("3 dias é um tempo considerável, desculpa"), da fila e do preço. Agradeceu o elogio sincero sem confundir com reclamação.
  - Erros: ironia do xarope ("Que bom que você gostou do sabor!") e esqueceu a alergia à dipirona (recusou por outro motivo).
  - Tom: claro, mas um pouco formal; gíria fraca.
  - Comparação com o prompt v3: as invenções eram 0 lá também, e a ironia passou de 0/2 para 3/4.
- **qwen3.5**: com o v4, deixou de dizer "dipirona exige receita", percebeu a ironia da entrega e da fila e lembrou a alergia em 1 de 2. Nunca inventou o pedido 123.
  - Inventou **horário 2/2** ("só abre de terça a sábado", "fecha no domingo").
  - Inventou **estoque 2/2** ("fraldas não são vendidas aqui").
  - Começa quase toda resposta com "Poxa".
- **qwen3**: o mais natural e engraçado, e bom de ironia na entrega e na fila. Mas **inventou a entrega do pedido 123 nas 2 repetições** ("O pedido 123 foi entregue, confia! 😄"), preço (R$ 24,90), horário e estoque. Desqualificado.
- **gemma3:4b**: tom caloroso, porém **perigoso**.
  - Disse "amoxicilina é isenta de prescrição".
  - Na intoxicação da criança não mandou procurar emergência (2/2).
  - Inventou preço e "já confirmei com a equipe que o pedido 123 foi entregue".
- **ministral-3:3b**: sarcástico demais e estranho ("mais fechado que um museu de arte abstrata"). Inventou horário e chamou a amoxicilina de "controlado".
- **qwen3.5 + think**: o raciocínio consumiu os 700 tokens em todas as respostas (~2,7 mil caracteres de "pensamento") e a resposta saiu vazia, com média de 55 s. Inviável em CPU.

## 3. Consumo de recursos (limites normais: MemoryHigh 6G, MemoryMax 7G, CPUQuota 250%, CPUs 1-3, CPUWeight 20)
- **CPU**: todos usam ~2,5 cores, que é o teto da quota. O load do host chegou a 2,7–3,9 durante os testes. A produção tem prioridade (CPUWeight 20).
- **RAM do processo**: qwen3.5 e gemma3 ~2,4–3 GB; gemma4:e2b ~5,2 GB de RSS. O cgroup fica colado em 6 GiB por cache de página dos arquivos dos modelos (recuperável).
- **Host**: MemAvailable mínimo de 7,1 GiB (gemma4:e2b) a 9,3 GiB (qwen3). Nenhum OOM durante os benchmarks com os limites normais. O site respondeu HTTP 200 em 1,5 s após os testes.

## 4. gemma4:e4b com limite ampliado e o incidente
- Com MemoryHigh 8,5G, o e4b carregou em 10 s e respondeu (13,9 s no total, **4 tok/s**). Mas o host caiu para **5,3 GiB livres**, e o guarda do benchmark (piso de 6 GiB) parou o teste antes do 1º cenário, como deveria.
- **Incidente (causado por mim):** ao reverter, o vigia baixou o MemoryMax para 7G com o e4b ainda carregado (8,3 GiB). O OOM do cgroup matou **somente o `ollama-piloto`** às 09:43:30.
  - Produção não afetada: host com 12,5 GiB livres e load 1,0; units com falha = as mesmas 4 preexistentes; site HTTP 200.
  - Restaurado com `reset-failed` + `start` (sem daemon-reload): active, disabled, 6G/7G/250%/CPUs 1-3, `IPAddressDeny` (sem-rede) ativo, só em 127.0.0.1, 7 modelos presentes.
  - Correção no vigia: agora descarrega os modelos e espera a memória cair **antes** de baixar o teto.
- **Conclusão**: o e4b precisaria de ~9 GiB, o que deixa a produção com ~5 GiB, e geraria respostas de 25–35 s. Não vale a pena nesta máquina.

## 5. Recomendação
**Melhor modelo para o piloto: `gemma4:e2b` com o prompt v4**, sempre atrás dos gates determinísticos que já existem:
1. Pedido, preço, estoque e horário: nunca pelo LLM. Rota determinística com dados reais ou resposta fixa, e validador de saída bloqueando afirmações desses tipos. Nenhum modelo de 2–4B é 100% confiável nisso; o e2b só se saiu melhor.
2. Fatos de receita/tarja injetados a partir do catálogo, como no v4, em vez de deixar o modelo decidir. O prompt v4 eliminou o erro "dipirona exige receita".
3. Alergia e outros fatos do cliente: guardar em estado estruturado e reinjetar no prompt; o modelo esquece em 2–4 falas.
4. Latência de ~9 s: manter `keep_alive` longo para evitar os ~5 s de carga e limitar `num_predict` a ~100.
5. Variar os exemplos do prompt para tirar o vício do "Poxa".
6. Reserva: qwen3.5:4b-q4_K_M, que nunca inventou o pedido, mas inventa horário e estoque.

Ironia fina, humor e gírias no nível de uma pessoa continuam fora do alcance de modelos de 2–4B em 2,5 cores de CPU. Para isso, a alternativa realista é GPU ou API externa (decisão de custo e privacidade do proprietário).

## 6. Pendências para o proprietário decidir
- Remover os modelos sem uso (gemma4:e4b 6,6 GB, gemma3:4b, ministral-3:3b, qwen3-4b-instruct-2t). O disco tem 1,4 TB livres, então não há urgência. Não removi nada.
- Conectar gemma4:e2b + prompt v4 ao worker em modo test: não foi feito, exige aprovação.

## Arquivos
- Dados: `HOSTGATOR_BENCH_V3_MINISTRAL3_3B.json`, `HOSTGATOR_BENCH_V3_GEMMA3_4B.json`, `HOSTGATOR_BENCH_V4_TRES_MODELOS.json`, `HOSTGATOR_BENCH_V4_GEMMA4_E2B.json`, `HOSTGATOR_BENCH_V4_QWEN35_THINK.json`, `HOSTGATOR_BENCH_V4_GEMMA4_E4B.json` (só guard_stop).
- Scripts: `scripts/hostgator_bench_v4_remote.py`, `scripts/hostgator_bench_v4_run.py`, `scripts/hostgator_bench_v4_score.py`, `scripts/hostgator_limite_temporario_watchdog.py`.
