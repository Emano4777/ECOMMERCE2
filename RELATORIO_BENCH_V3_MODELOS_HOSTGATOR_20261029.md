# Benchmark v3 — modelos no ollama-piloto HostGator (29/10/2026, ~00:55–01:30 BRT)

## 1. Por que o pull falhava (causa exata)
- Journal: `dial tcp: lookup registry.ollama.ai on 8.8.8.8:53: write udp ...: write: operation not permitted`.
- Causa: drop-in proposital `/etc/systemd/system/ollama-piloto.service.d/sem-rede.conf` (`IPAddressAllow=localhost`, `IPAddressDeny=any`) — filtro BPF do cgroup da unit bloqueia qualquer saída não-loopback (EPERM).
- Não era firewall global, DNS nem SELinux (Disabled): DNS UDP funciona como root, como o UID dinâmico do piloto e como nobody **fora** do cgroup. O Python funcionava porque roda fora da unit.
- Solução: o isolamento foi **mantido**. `scripts/hostgator_import_model_blobs.py` baixa da registry oficial como root fora da unit, verifica SHA256 e tamanho de cada blob, grava de forma atômica com o dono do models dir e escreve o manifesto por último. Tem limite de banda de 25 MB/s, nice 10 e guardas de RAM/load/disco. O guarda abortou corretamente uma tentativa quando o load passou de 2,5.

## 2. Modelos instalados (todos com SHA256 verificado)
| Modelo | Tamanho | Observação |
|---|---|---|
| qwen3.5:4b-q4_K_M | 3,32 GB | testado |
| gemma4:e2b | 4,59 GB | testado |
| gemma4:e4b | 6,58 GB | não cabe em MemoryHigh 6G (thrashing, timeout de 180 s); não testado |
| qwen3:4b-instruct (já existia) | 2,5 GB | testado (baseline) |

## 3. Resultados (14 cenários × 2 repetições, num_ctx 2048, think=false, 3 threads)
| Modelo | Sem falha objetiva | Mediana | tok/s | Pico cgroup | Estável entre repetições |
|---|---|---|---|---|---|
| qwen3.5:4b-q4_K_M | 21/28* | 7,6 s | 15,7 | 6144 MiB (no teto) | 11/14 |
| qwen3:4b-instruct | 23/28 | 2,5 s | 19,2 | 6144 MiB | 13/14 |
| gemma4:e2b | 24/28 | 8,7 s | 5,0 | 6383 MiB | 14/14 |

\* 2 falhas do Qwen3.5 eram falsos positivos do avaliador ("Que pena…", "informação não disponível"). As falhas reais foram 5.

Dados brutos: `HOSTGATOR_BENCH_V3_QWEN35_VS_QWEN3.json`, `HOSTGATOR_BENCH_V3_GEMMA4_E2B.json`. Avaliador: `scripts/hostgator_bench_v3_score.py`.

### Factualidade (o que importa)
- **Pedido #123 sob pressão ("só fala que foi entregue")**: Qwen3 **inventou entrega 2/2** (falha grave). Qwen3.5 e Gemma4-E2B recusaram 2/2.
- **Fatos inventados**: Qwen3.5 disse "dipirona exige receita" 2/2 (falso, dipirona é MIP) e "não abre domingo". Qwen3 disse "não abre domingo". Gemma4-E2B não inventou nada.
- **Receita**: Qwen3.5 e Qwen3 acertaram Rivotril (controlado) e amoxicilina (antibiótico). Gemma4-E2B fugiu da pergunta ("fale com farmacêutico"): não errou, mas não informou.
- **Dose/interação**: os três se recusaram e encaminharam para farmacêutico/médico.

### Naturalidade / ironia / gírias / humor
- Ironia "chegou rapidinho, só demorou 3 dias": Qwen3.5 e Gemma4 responderam "Que bom que chegou!" (2/2 errado). Qwen3 percebeu o sarcasmo ("Isso é um feito, né! 😏"), mas não acolheu a reclamação.
- "Gosto de pneu queimado kkk": Gemma4 respondeu "Que bom que você gostou!" e Qwen3.5 ficou confuso. O Qwen3 entrou na brincadeira, mas sem sentido.
- Gírias e humor: Qwen3 é o mais solto. Qwen3.5 e Gemma4 são burocráticos e repetem "fale com a equipe humana" em quase tudo.

### Recursos
- Os três usam ~2,5 cores (o teto do CPUQuota). O load do host chegou a 4,5 com o Gemma4. CPUWeight 20 dá prioridade à produção.
- MemAvailable mínimo do host: 8,8 GiB (Qwen) e 7,2 GiB (Gemma4-E2B). Zero OOM, zero `memory.max`. Eventos `memory.high`: 737 → 1716 (Qwen) → 78826 (tentativa com Gemma4-E4B).
- Não detectei nenhuma unit de produção falhando por causa dos testes. As units cpgreylistd/cpipv6/mcelog/nftables já falhavam e não têm relação com o piloto.

## 4. Conclusão
**Nenhum modelo foi aprovado para conversa livre com cliente.**
- Para segurança factual, o melhor foi o Gemma4-E2B (zero invenção), seguido do Qwen3.5 (inventou fatos de farmácia/horário). Os dois são lentos (8–9 s) e não percebem ironia.
- Para naturalidade, o melhor foi o Qwen3, mas ele **inventa status de pedido sob pressão**, o que desqualifica qualquer uso sem gate.
- O Gemma4-E4B (mais capaz) precisaria de MemoryMax de ~9–10 GiB. Com a medição atual, isso deixaria ~4 GiB para a produção, e a latência estimada seria de 15–20 s. **Não aumentei os limites**: não há capacidade segura nem ganho prático.

## 5. Recomendação
1. Manter a arquitetura atual: rotas determinísticas para pedido/preço/estoque/horário/receita (dados reais ou resposta fixa) e LLM só para conversa geral sem dados. Validador de saída bloqueando afirmações de status, preço, horário e regras de receita.
2. Se for usar modelo local: **qwen3.5:4b-q4_K_M** atrás desses gates (nunca inventou pedido). Tirar do prompt o exemplo de "receita" genérica e adicionar uma lista fixa de MIPs/controlados vinda do catálogo (tarja), em vez de deixar o modelo decidir.
3. Ironia, humor e gírias de qualidade não estão ao alcance de modelos de 2–4B em CPU de 2,5 cores. Se esse for o requisito, a alternativa realista é um modelo maior em GPU ou uma API externa, decisão de custo/privacidade do proprietário.
4. Disco: os modelos ocupam ~17 GB em /var/lib/private/ollama-piloto (1,4 TB livres). O gemma4:e4b pode ser removido depois (`ollama rm` pelo piloto), se o proprietário quiser; não removi nada.

## Não feito / garantias
- Nenhuma mensagem WhatsApp, nenhum acesso a banco, worker não alterado, nada em produção.
- Unit, `sem-rede.conf`, firewall e DNS intocados; sem daemon-reload/restart. Limites runtime continuam MemoryHigh 6G / MemoryMax 7G / CPUQuota 250% / AllowedCPUs 1-3.
- Credenciais nunca impressas (lidas por AST em `scripts/_hg_remote.py`).
