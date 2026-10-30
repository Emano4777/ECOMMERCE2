"""Suporte tecnico dos sistemas PoupAqui -- diagnosticos verificaveis, somente leitura.

Cobre o que mais chega no WhatsApp alem de pedido/corte:
  pe_fora    "o Pedido Eletronico caiu"     -> sonda HTTP + fluxo real de pedidos
  relatorio  "relatorio da loja esta errado" -> arquivos de venda recebidos no mes
  ecommerce  "produto nao aparece no site"  -> estoque enviado, oculto, config da loja
  sistema    "como faco X no sistema"        -> sem guia verificado: pede detalhe e escala

Regras (mesmas do assistente):
  - toda consulta e tipada e parametrizada, so SELECT, e usa o CNPJ/user_id da
    loja VERIFICADA pelo telefone -- o texto da mensagem nunca escolhe a loja;
  - a resposta so afirma o que a checagem mostrou; causa sem evidencia vira
    "nao consigo afirmar" + pedido de detalhe ou escalacao;
  - nada de valores de venda, custo ou preco na resposta (so datas e contagens);
  - a sonda HTTP so acessa URLs fixas desta lista, por GET, sem credencial.
Nao envia nada e nao escreve no banco.
"""
import re
import time
import urllib.error
import urllib.request
from calendar import monthrange
from datetime import date, datetime, timedelta

from poupaqui_ai_assistente import RepoLeitura, Resposta, _agora
from poupaqui_ai_policy import normalize

SITES = {
    "pedido_eletronico": ("Pedido Eletrônico", "https://pedidoeletronico.poupaqui.com.br/"),
    "relatorios": ("site de relatórios", "https://relatorios.poupaqui.com.br/"),
    "ecommerce": ("e-commerce", "https://www.drogariaspoupaqui.com.br/"),
}
ESTOQUE_VELHO_DIAS = 3
ATRASO_ARQUIVO_VENDAS_DIAS = 2   # o arquivo do dia D chega na madrugada de D+2 (observado em out/2026)
HORA_PROCESSAMENTO_VENDAS = 8    # processado ~06:10 (created_at); antes das 8h o dia de D-2 ainda pode faltar
MESES = ["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro",
         "outubro", "novembro", "dezembro"]
UA = "PoupAqui-Assistente-Healthcheck/1.0"


# ─── Sonda HTTP (lista fixa) ───────────────────────────────────────────────

def sonda_http(sistema, timeout=8, tentativas=2):
    """GET na pagina inicial. 2 tentativas: 1 falha isolada de rede nao vira 'caiu'."""
    nome, url = SITES[sistema]
    ultimo = {}
    for _ in range(tentativas):
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read(2048)
                return {"sistema": sistema, "ok": 200 <= r.status < 400, "http": r.status,
                        "seg": round(time.monotonic() - t0, 2), "erro": None}
        except urllib.error.HTTPError as e:
            ultimo = {"sistema": sistema, "ok": e.code < 500 and e.code not in (408, 429), "http": e.code,
                      "seg": round(time.monotonic() - t0, 2), "erro": f"HTTP {e.code}"}
            if ultimo["ok"]:  # 401/403/404 = servidor no ar respondendo
                return ultimo
        except Exception as exc:
            ultimo = {"sistema": sistema, "ok": False, "http": None,
                      "seg": round(time.monotonic() - t0, 2), "erro": type(exc).__name__}
    return ultimo


# ─── Consultas (somente leitura, sempre da loja verificada) ────────────────

class RepoSuporte(RepoLeitura):
    def atividade_pedidos(self, user_id, fornecedor=None):
        """Fluxo real do Pedido Eletronico: agregado de todas as lojas (sem
        identificar nenhuma) + ultimo pedido DESTA loja (+ do fornecedor citado)."""
        r = self._query("""
            SELECT COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '60 minutes'),
                   COUNT(DISTINCT user_id) FILTER (WHERE created_at > NOW() - INTERVAL '60 minutes'),
                   MAX(created_at),
                   MAX(created_at) FILTER (WHERE user_id = %s::uuid)
                   ,COUNT(*) FILTER (WHERE created_at < NOW() - INTERVAL '1 day'
                                       AND EXTRACT(EPOCH FROM (NOW() - created_at))::bigint %% 86400 < 3600) / 7.0
                   ,COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '3 hours' AND UPPER(fornecedor) = %s)
                   ,MAX(created_at) FILTER (WHERE UPPER(fornecedor) = %s)
            FROM pedidos3 WHERE created_at > NOW() - INTERVAL '8 days'""",
                        (user_id, (fornecedor or "").upper(), (fornecedor or "").upper()))[0]
        # media: pedidos na mesma hora do dia nos 7 dias anteriores (movimento normal do horario)
        out = {"pedidos_60min": r[0], "lojas_60min": r[1], "ultimo_geral": r[2], "ultimo_loja": r[3],
               "media_mesma_hora": round(float(r[4] or 0), 1)}
        if fornecedor:
            out.update({"fornecedor": fornecedor.upper(), "fornecedor_3h": r[5], "ultimo_fornecedor": r[6]})
        return out

    def atividade_ecommerce(self, cnpj):
        """Compras do site (origem e-commerce; sem Mercado Livre): so contagens e
        horarios, todas as lojas + ultima desta loja. Nada de valor ou cliente."""
        r = self._query("""
            SELECT COUNT(*) FILTER (WHERE criado_em > NOW() - INTERVAL '3 hours'),
                   COUNT(*) FILTER (WHERE pagamento_confirmado_em > NOW() - INTERVAL '3 hours'),
                   MAX(criado_em),
                   COUNT(*) FILTER (WHERE criado_em < NOW() - INTERVAL '1 day'
                                       AND EXTRACT(EPOCH FROM (NOW() - criado_em))::bigint %% 86400 < 10800) / 7.0,
                   MAX(criado_em) FILTER (WHERE cnpjloja = %s)
            FROM ecommerce_pedidos
            WHERE COALESCE(origem, 'ecommerce') = 'ecommerce' AND criado_em > NOW() - INTERVAL '8 days'""",
                        (cnpj,))[0]
        return {"criados_3h": r[0], "pagos_3h": r[1], "ultimo_geral": r[2],
                "media_3h": round(float(r[3] or 0), 1), "ultimo_loja": r[4]}

    def cobertura_vendas(self, cnpj, ano, mes):
        """Dias do mes com arquivo de venda recebido (por item e totalizada)."""
        def dias(rows):
            out = set()
            for (d,) in rows:
                try:
                    out.add(datetime.strptime(str(d).strip(), "%d%m%Y").date())
                except ValueError:
                    pass
            return out
        itens = dias(self._query("SELECT DISTINCT data FROM vendageral WHERE cnpj = %s AND mes_ref = %s",
                                 (cnpj, date(ano, mes, 1))))
        tot_rows = self._query("""SELECT data, MAX(created_at) FROM vendas_totalizadas
                                  WHERE cnpj = %s AND ano_mes = %s GROUP BY data""", (cnpj, f"{mes:02d}-{ano}"))
        totalizadas = dias([(d,) for d, _ in tot_rows])
        ultimo_proc = max((em for _, em in tot_rows if em), default=None)
        # padrao de envio da loja: arquivo diario ou um arquivo mensal (dia 01)
        m1 = date(ano, mes, 1)
        ant2 = date(ano - 1, mes + 10, 1) if mes <= 2 else date(ano, mes - 2, 1)
        hist = self._query("""SELECT mes_ref, COUNT(DISTINCT data) FROM vendageral
                              WHERE cnpj = %s AND mes_ref >= %s AND mes_ref < %s GROUP BY 1""", (cnpj, ant2, m1))
        ant = self._query("""SELECT MAX(created_at) FROM vendas_totalizadas WHERE cnpj = %s AND ano_mes = %s""",
                          (cnpj, f"{(m1 - timedelta(days=1)):%m-%Y}"))
        return {"itens": itens, "totalizadas": totalizadas, "ultimo_processado": ultimo_proc,
                "dias_meses_anteriores": [n for _, n in hist],
                "processado_mes_anterior": ant[0][0] if ant else None}

    def vendas_mes(self, cnpj, ano, mes):
        """[(dia, valor, vendas, itens)] do mes, do arquivo totalizado (um por dia; reenvio vale o mais novo)."""
        rows = self._query("""SELECT DISTINCT ON (data) data, ticket_medio * quantidade_vendas, quantidade_vendas, itens
                              FROM vendas_totalizadas WHERE cnpj = %s AND ano_mes = %s
                              ORDER BY data, created_at DESC""", (cnpj, f"{mes:02d}-{ano}"))
        out = []
        for d, valor, vendas, itens in rows:
            try:
                out.append((datetime.strptime(str(d).strip(), "%d%m%Y").date(), float(valor or 0),
                            int(vendas or 0), int(itens or 0)))
            except ValueError:
                pass
        return sorted(out)

    def config_ecommerce(self, cnpj):
        # so colunas de publicacao: a tabela tem tokens de pagamento que nunca sao lidos
        rows = self._query("""SELECT catalogo_publico, estoque_min_publicacao, catalogo_sync_em
                              FROM ecommerce_config_loja WHERE cnpjloja = %s LIMIT 1""", (cnpj,))
        if not rows:
            return None
        return {"catalogo_publico": rows[0][0], "estoque_min": rows[0][1], "sync_em": rows[0][2]}

    def produto_estoque(self, cnpj, ean=None, termos=(), limite=5):
        """Produto no ultimo estoque enviado pela loja (FTP) ou pela Automatiza."""
        out, data_estoque = [], None
        r = self._query("SELECT MAX(data) FROM estoque WHERE cnpj = %s", (cnpj,))
        if r and r[0][0]:
            try:
                data_estoque = datetime.strptime(r[0][0].strip(), "%d%m%Y").date()
            except ValueError:
                pass
        if ean:
            rows = self._query("""SELECT barras, descricao, estoque FROM estoque
                                  WHERE cnpj = %s AND (barras = %s OR barras_norm = %s) LIMIT %s""",
                               (cnpj, ean, ean, limite))
        elif termos:
            sql = "SELECT barras, descricao, estoque FROM estoque WHERE cnpj = %s"
            params = [cnpj]
            for t in termos[:4]:
                sql += " AND UPPER(descricao) LIKE %s"
                params.append("%" + re.sub(r"[%_\\]", "", t.upper()) + "%")
            rows = self._query(sql + " ORDER BY estoque DESC NULLS LAST LIMIT %s", tuple(params + [limite]))
        else:
            rows = []
        out = [{"ean": (b or "").strip(), "descricao": d, "qtd": float(q or 0), "origem": "estoque"}
               for b, d, q in rows]
        if not out and data_estoque is None:  # loja integrada pela Automatiza (ex.: SJRP)
            if ean:
                rows = self._query("""SELECT ean, descricao_produto, quantidade_estoque, recebido_em
                                      FROM automatiza_estoque WHERE cnpj_loja = %s AND ean = %s LIMIT %s""",
                                   (cnpj, ean, limite))
            elif termos:
                sql = """SELECT ean, descricao_produto, quantidade_estoque, recebido_em
                         FROM automatiza_estoque WHERE cnpj_loja = %s"""
                params = [cnpj]
                for t in termos[:4]:
                    sql += " AND UPPER(descricao_produto) LIKE %s"
                    params.append("%" + re.sub(r"[%_\\]", "", t.upper()) + "%")
                rows = self._query(sql + " LIMIT %s", tuple(params + [limite]))
            out = [{"ean": (e or "").strip(), "descricao": d, "qtd": float(q or 0), "origem": "automatiza"}
                   for e, d, q, _ in rows]
            if rows:
                data_estoque = _agora(max(x[3] for x in rows)).date()
        if out:
            eans = [p["ean"] for p in out]
            ocultos = {e for (e,) in self._query("""SELECT ean FROM ecommerce_catalogo_oculto
                                                    WHERE cnpjloja = %s AND ean = ANY(%s)""", (cnpj, eans))}
            for p in out:
                p["oculto"] = p["ean"] in ocultos
        return {"data_estoque": data_estoque, "produtos": out}


# ─── Intencao ──────────────────────────────────────────────────────────────

_FALHA = (r"(sai\w* do ar|cai\w* do ar|nao (ta|esta|tao|estao) no ar|caiu|cai|caindo|fora do ar|fora|offline|parad\w*|instavel|travad\w*|travando|travou|trava|lent\w*|"
          r"erro\w*|bug\w*|deu pau|deu ruim|deu problema|com problema|zoad\w*|pifou|nao (abre|abriu|carrega\w*|"
          r"entra|entrou|funciona|funcionando|ta funcionando|"
          r"esta funcionando|envia|enviou|consigo|consegui|vai|da|deixa)|tela branca|nao responde)")
_SIS_PE = r"(pedido eletronico|pedidoeletronico|sistema de pedido\w*|sistema|plataforma|portal|site de pedido\w*)"
_SIS_ECOM = r"(e ?commerce|loja virtual|loja online|drogariaspoupaqui|site da loja|site de vendas|aplicativo|app)"
_SIS_REL = r"(relatorio\w*|\bbi\b|dashboard|painel de vendas|ranking|ticket medio|sell ?out)"
_PE_FORA_RE = re.compile(rf"\b{_SIS_PE}\b.*\b{_FALHA}\b|\b{_FALHA}\b.*\b{_SIS_PE}\b|^(caiu|ta fora|fora do ar)\b")
_ECOM_FORA_RE = re.compile(rf"\b{_SIS_ECOM}\b.*\b{_FALHA}\b|\b{_FALHA}\b.*\b{_SIS_ECOM}\b")
_REL_FORA_RE = re.compile(rf"\b{_SIS_REL}\b.*\b(nao (abre|carrega|entra)|fora do ar|caiu|tela branca|"
                          r"sai\w* do ar|nao (ta|esta|tao|estao) no ar|"
                          r"erro\w* (ao|para|pra) (carregar|abrir|entrar)|nao carreg\w*|travou|travando|travad\w*)\b")
_REL_DIV_RE = re.compile(
    rf"\b({_SIS_REL[1:-1]}|vendas?|faturamento da loja|numeros?|dados)\b.*\b(errad\w*|diverg\w*|nao bat\w*|nao ta batendo|"
    r"diferente\w*|incorret\w*|falt\w*|zerad\w*|estranh\w*|menor|maior|sumi\w*|desatualizad\w*|"
    r"nao atualiz\w*|incomplet\w*|nao aparec\w*|nao (mostra|consta)\w*|deu ruim|deu problema|com problema|bug\w*|"
    r"zoad\w*)\b"
    r"|\b(errad\w*|diverg\w*|nao bat\w*|nao aparec\w*|deu ruim|deu problema|problema|bug\w*|zoad\w*)\b.*\b"
    + _SIS_REL + r"\b")
# pergunta de VALOR de venda (nao divergencia): "quanto eles venderam esse mes", "vendas de setembro"
_VENDAS_Q_RE = re.compile(
    r"\b(quant[oa]s? (que )?(\w+ ){0,5}(vend\w*|fatur\w*)|quant[oa]s? vendas|total (de |das )?vendas?|"
    r"(qual|quanto|como) (foi|e|deu|ta|esta|fica|ficou) (a |o |as |os )?(venda|vendas|faturamento|movimento)\b|"
    r"vendas? d[ao]s? (loja|filial|unidade)|"
    r"vendas? (d[oa] |dest[ea] |dess[ea] |nest[ea] |ness[ea] |n[oa] |de |em |ate )?(mes|semana|ano|hoje|ontem|"
    r"janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)|"
    r"faturamento|relatorio\w* de vendas?|resumo (de |das )?vendas?|ticket medio|"
    r"como (estao|tao|ta|esta|foram|foi) (as )?vendas?)\b")
_PROD_SUMIU_RE = re.compile(
    r"\b(nao (aparec\w*|mostr\w*|encontr\w*|ach\w*|sobe|subiu)|sumi\w*|desaparec\w*|some)\b")
_ONDE_ECOM_RE = re.compile(rf"\b({_SIS_ECOM[1:-1]}|site|catalogo|online|internet|vitrine)\b")
_COMO_RE = re.compile(r"\b(como (eu )?(faco|faz|fazer|consigo|coloco|cadastr\w*|mudo|altero|troco|vejo|acesso|"
                      r"tiro|imprimo|exporto|gero|emito)|onde (eu )?(vejo|fica|acho|encontro|clico))\b")
_COMO_SIS_RE = re.compile(rf"\b({_SIS_PE[1:-1]}|{_SIS_ECOM[1:-1]}|{_SIS_REL[1:-1]}|site|senha|login|"
                          r"carrinho|cotacao|tela|botao|aba|menu|cadastro|usuario)\b")
_EAN_RE = re.compile(r"\b(\d{8}|\d{12,14})\b")
_CNPJ_RE = re.compile(r"\b(\d{14})\b")
_STOP = set("""o a os as um uma uns umas de do da dos das no na nos nas em pra para por com sem que e eh
    ou meu minha meus minhas seu sua esse essa este esta isso aqui ai la ja nao mais muito pouco ta
    esta estao tao foi ser site ecommerce commerce loja virtual online internet app aplicativo catalogo
    vitrine produto produtos item itens remedio remedios medicamento medicamentos codigo ean barras
    aparece aparecem aparecendo aparecer apareceu mostra mostrando encontro encontra encontrei acho acha
    achei sumiu sumiram some desapareceu desapareceram sobe subiu nosso nossa voces vcs voce vc pq porque
    quando hoje ontem bom dia boa tarde noite oi ola pessoal tem temos tinha algum alguns alguma algumas
    varios varias todos todas tudo nada cliente clientes falou disse vendo ver mais mesmo agora ainda
    continua nem sei qual quais isso aquele aquela pelo pela""".split())


# "nao funciona" sem dizer O QUE: as acoes que a loja costuma descrever
_CHECKOUT_RE = re.compile(r"\b((finaliz\w*|conclu\w*|fechar|fecha) (a |uma |o )?(compra|venda)|checkout|"
                          r"pagamento (nao|n) (passa|vai|aprova\w*|conclui\w*|finaliza\w*)|"
                          r"nao (consigo|consegue|conseguem|da pra|deixa) (comprar|pagar|finalizar))\b")
_ACAO_PE_RE = re.compile(r"\b((envi\w*|mand\w*|finaliz\w*|transmit\w*|fech\w*|ger\w*) (o |do |de |meu |um |os )?"
                         r"pedido\w*|botao|carrinho|cotac\w*)\b")
_LOGIN_RE = re.compile(r"\b(login|logar|loga|senha|entrar|entra|entro|acess\w*)\b")
_LOGIN_FALHA_RE = re.compile(r"\b(nao (entra|entro|consigo entrar|consigo logar|loga|acessa|consigo acessar|aceita)|"
                             r"senha (invalid\w*|incorret\w*|errad\w*|nao (aceita|funciona|vai))|"
                             r"(usuario|acesso|login) (bloquead\w*|invalid\w*|nao (existe|encontrado))|"
                             r"login (nao|n) (entra|vai|funciona))\b")
_RESOLVEU_RE = re.compile(r"\b(funcionou|deu certo|agora foi|agora deu|resolveu|resolvido|voltou|normalizou|"
                          r"consegui|ja foi|ja deu)\b")


# normalize() troca "." por espaco: aceita os dois separadores
_DOMINIO_RE = re.compile(r"\b(?:www[. ])?([a-z0-9][a-z0-9-]{2,40})[. ](vercel[. ]app|netlify[. ]app|herokuapp[. ]com|"
                         r"poupaqui[. ]com[. ]br|com[. ]br)\b")
_DOMINIO_SISTEMA = {"pedidoeletronico": "pedido_eletronico", "relatorios": "relatorios", "relatorio": "relatorios",
                    "drogariaspoupaqui": "ecommerce"}
_HOSPEDAGEM_ANTIGA = ("vercel.app", "netlify.app", "herokuapp.com")
_FORA_DO_SISTEMA = {"pedido_eletronico": "pe_fora", "relatorios": "rel_fora", "ecommerce": "ecom_fora"}


def sistema_do_endereco(texto_norm):
    """(sistema, endereco_citado, antigo) do primeiro endereco de sistema nosso no texto/print, ou None.
    Tolera erro de leitura no nome ("pedideletronico"). antigo = hospedagem que nao e mais a oficial."""
    from poupaqui_ai_assistente import _distancia
    for m in _DOMINIO_RE.finditer(texto_norm):
        rotulo, sufixo = m.group(1), m.group(2).replace(" ", ".")
        for nome, sistema in _DOMINIO_SISTEMA.items():
            if _distancia(rotulo, nome, 2) <= (2 if len(nome) >= 10 else 1):
                return sistema, f"{nome}.{sufixo}", sufixo in _HOSPEDAGEM_ANTIGA  # grafia certa, nao a lida
    return None


def detectar_suporte(texto_norm):
    """'pe_fora' | 'ecom_fora' | 'rel_fora' | 'relatorio' | 'ecommerce' | 'sistema' | 'acesso' | None."""
    t = texto_norm
    end = sistema_do_endereco(t)
    if end and re.search(rf"\b{_FALHA}\b|\b(503|502|500|404|unavailable|paused|pausad\w*)\b", t):
        return _FORA_DO_SISTEMA[end[0]]   # o endereco (digitado ou no print) diz qual e o sistema
    if _PROD_SUMIU_RE.search(t) and (_ONDE_ECOM_RE.search(t) or re.search(r"\b(produto|item|remedio|ean)\w*", t)):
        if not re.search(r"\b(pedido|nota|boleto|relatorio)\w*", t):
            return "ecommerce"
    if _REL_FORA_RE.search(t):
        return "rel_fora"
    if _REL_DIV_RE.search(t):
        return "relatorio"
    if _VENDAS_Q_RE.search(t):
        return "vendas"
    if _ECOM_FORA_RE.search(t) or (_CHECKOUT_RE.search(t) and re.search(r"\b(site|cliente\w*|compra)\b", t)):
        return "ecom_fora"
    if _PE_FORA_RE.search(t):
        return "pe_fora"
    if _ACAO_PE_RE.search(t) and re.search(rf"\b{_FALHA}\b", t):
        return "pe_fora"
    if _LOGIN_FALHA_RE.search(t):
        return "acesso"   # entrar em QUAL sistema: decide pelo texto/historico
    if _COMO_RE.search(t) and _COMO_SIS_RE.search(t):
        return "sistema"
    return None


# ─── Relato: o que exatamente falhou (so o que a loja disse, nada inferido) ─

_ERRO_COD_RE = re.compile(r"\b(erro|error|codigo|http)\s*(\d{3,4})\b")
_ERRO_MSG_RE = re.compile(r"\b(aparece|apareceu|aparecendo|da|deu|mostra|mostrando|fala|diz|escrito|mensagem)\b"
                          r"\s+(a mensagem |uma mensagem |o erro |um erro |escrito |que )?(?P<m>[a-z][a-z0-9 ]{6,70})")
_ERRO_ACESSO_RE = re.compile(r"\b((senha|usuario|login|acesso) (invalid\w*|incorret\w*|errad\w*|bloquead\w*|"
                             r"expirad\w*|nao (existe|encontrado|cadastrado|aceita\w*)))\b")
_DESDE_RE = re.compile(r"\b(desde (as |a |o |de )?(\d{1,2}( ?h| horas|:\d{2})?|ontem( a noite| a tarde| cedo)?|"
                       r"hoje( cedo)?|cedo|manha|tarde|segunda|terca|quarta|quinta|sexta|sabado|domingo)|"
                       r"ha (\d+|uma|um|duas|meia) (min\w*|hora\w*|dias?)|hoje cedo|o dia (todo|inteiro)|"
                       r"agora (ha pouco|pouco)|desde cedo)\b")
_ESCOPO_VARIAS_RE = re.compile(r"\b(todas as lojas|outras lojas (tambem|tb|tbm)|outra loja (tambem|tb|tbm)|"
                               r"nenhuma loja|todo mundo|geral)\b")
_ESCOPO_UMA_RE = re.compile(r"\b(so (aqui|na minha|na nossa|nessa|nesta|comigo|no meu|na loja)|so a nossa|"
                            r"outras lojas (estao|tao|ta) (normal|conseguindo|funcionando))\b")
_ETAPA_RE = re.compile(r"\b(?:trava|travou|travando|para|parou|erro|falha|nao passa)\w* (?:no|na|em) "
                       r"(pagamento|frete|endereco|cep|confirmacao|cupom|cadastro|carrinho|entrega)\b")
_TENTOU = [
    (re.compile(r"\b(atualizei|atualizar a pagina|dei f5|ctrl f5|recarreguei|recarregar)\b"), "atualizou a página"),
    (re.compile(r"\b(outro navegador|outro computador|outro pc|outra maquina|no celular|outro celular|"
                r"aba anonima|janela anonima)\b"), "tentou em outro navegador/aparelho"),
    (re.compile(r"\b(sai e entrei|deslogu\w*|loguei de novo|entrei de novo|fiz login de novo)\b"),
     "saiu e entrou de novo"),
    (re.compile(r"\b(reiniciei|reiniciar o (pc|computador)|desliguei e liguei)\b"), "reiniciou o computador"),
    (re.compile(r"\b(limpei (o )?(cache|historico|cookies?))\b"), "limpou cache"),
    (re.compile(r"\b(ja tentei|ja fiz (isso|tudo|o que)|fiz isso|tentei de novo|tentei varias)\b"),
     "disse que já tentou"),
]
_TENTOU_INF = {"atualizou a página": "atualizar a página", "tentou em outro navegador/aparelho":
               "tentar em outro navegador", "saiu e entrou de novo": "sair e entrar de novo",
               "reiniciou o computador": "reiniciar o computador", "limpou cache": "limpar o cache"}
ACOES = {"login": "entrar (login)", "enviar_pedido": "enviar/finalizar pedido",
         "finalizar_compra": "finalizar compra no site", "abrir": "abrir a página", "lento": "lentidão"}


def acao_relatada(t):
    if _CHECKOUT_RE.search(t):
        return "finalizar_compra"
    if _ACAO_PE_RE.search(t) or re.search(r"\bnao (envia|enviou|vai o pedido|sai o pedido)\b", t):
        return "enviar_pedido"
    if _LOGIN_FALHA_RE.search(t) or (_LOGIN_RE.search(t) and re.search(rf"\b{_FALHA}\b", t)):
        return "login"
    if re.search(r"\b(caiu|fora do ar|offline|nao (abre|abriu|carrega\w*|responde)|tela branca)\b", t):
        return "abrir"
    if re.search(r"\b(lent\w*|demorando|travand\w*|travad\w*)\b", t):
        return "lento"
    return None


def sistema_relatado(t):
    if re.search(rf"\b({_SIS_REL[1:-1]})\b", t):
        return "relatorios"
    if re.search(rf"\b({_SIS_ECOM[1:-1]}|painel da loja|precificador|vitrine)\b", t) or _CHECKOUT_RE.search(t):
        return "ecommerce"
    if re.search(r"\b(pedido eletronico|pedidoeletronico|sistema de pedido\w*|site de pedido\w*|cotac\w*|"
                 r"carrinho)\b", t) or _ACAO_PE_RE.search(t):
        return "pedido_eletronico"
    return None


def extrair_relato(texto_norm):
    """Slots do relato. Valores sao trechos do proprio texto, nunca causa."""
    t = texto_norm or ""
    erro = None
    m = _ERRO_ACESSO_RE.search(t) or _ERRO_COD_RE.search(t)
    if m:
        erro = m.group(0)
    elif "tela branca" in t:
        erro = "tela branca"
    else:
        m = _ERRO_MSG_RE.search(t)
        if m and re.search(r"\b(erro|falha|invalid\w*|nao|negad\w*|recusad\w*|reprovad\w*|problema|"
                           r"indisponivel|bloquead\w*|expirad\w*)\b", m.group("m")):
            erro = m.group("m").strip()
    escopo = "varias lojas" if _ESCOPO_VARIAS_RE.search(t) else ("só esta loja" if _ESCOPO_UMA_RE.search(t) else None)
    desde = _DESDE_RE.search(t)
    etapa = _ETAPA_RE.search(t)
    return {"sistema": sistema_relatado(t), "acao": acao_relatada(t), "erro": erro,
            "desde": desde.group(0) if desde else None, "escopo": escopo,
            "etapa": etapa.group(1) if etapa else None,
            "tentou": [nome for rx, nome in _TENTOU if rx.search(t)]}


def juntar_relato(anterior, atual):
    """O que foi dito antes vale, o que foi dito agora prevalece."""
    out = dict(anterior)
    for k, v in atual.items():
        if k == "tentou":
            out[k] = list(dict.fromkeys((anterior.get(k) or []) + (v or [])))
        elif v:
            out[k] = v
    return out


def _relato_txt(rel):
    partes = []
    if rel.get("acao"):
        partes.append(f"ação: {ACOES[rel['acao']]}")
    if rel.get("erro"):
        partes.append(f"erro relatado: \"{rel['erro']}\"")
    if rel.get("etapa"):
        partes.append(f"etapa: {rel['etapa']}")
    if rel.get("desde"):
        partes.append(f"quando: {rel['desde']}")
    if rel.get("escopo"):
        partes.append(f"escopo relatado: {rel['escopo']}")
    if rel.get("tentou"):
        partes.append("já tentou: " + ", ".join(rel["tentou"]))
    return partes


def _termos_produto(texto_norm):
    return [w for w in texto_norm.split() if len(w) >= 3 and w not in _STOP and not w.isdigit()][:4]


def _mes_alvo(texto_norm, hoje):
    for i, nome in enumerate(MESES, 1):
        if re.search(rf"\b{nome}\b", texto_norm):
            ano = hoje.year if i <= hoje.month else hoje.year - 1
            return ano, i
    if re.search(r"\bmes passado\b", texto_norm):
        d = hoje.replace(day=1) - timedelta(days=1)
        return d.year, d.month
    return hoje.year, hoje.month


def _dias_txt(dias):
    """[1,2,3,7] -> '01/10 a 03/10 e 07/10' (intervalos)."""
    dias = sorted(dias)
    grupos, ini, ant = [], None, None
    for d in dias:
        if ini is None:
            ini = ant = d
        elif (d - ant).days == 1:
            ant = d
        else:
            grupos.append((ini, ant))
            ini = ant = d
    if ini is not None:
        grupos.append((ini, ant))
    partes = [f"{a:%d/%m}" if a == b else f"{a:%d/%m} a {b:%d/%m}" for a, b in grupos]
    return partes[0] if len(partes) == 1 else ", ".join(partes[:-1]) + " e " + partes[-1]


def _hora(dt):
    return f"{_agora(dt):%H:%M}"


def _quando(dt, agora):
    d, hoje = _agora(dt).date(), _agora(agora).date()
    rel = {0: "hoje", 1: "ontem"}.get((hoje - d).days)
    return f"{rel} às {_hora(dt)}" if rel else f"em {d:%d/%m} às {_hora(dt)}"


# ─── Diagnosticos ──────────────────────────────────────────────────────────

def _r(acao, reply, motivo, escalar, intencao, ctx, equipe="equipe", resumo=None):
    return Resposta(acao, reply, motivo, escalar, intencao, equipe=equipe, resumo=resumo)


FALHA_TIPOS = ("pe_fora", "ecom_fora", "rel_fora", "acesso")
_ORIENTOU_RE = re.compile(r"\b(ctrl f5|aba anonima|outro navegador|caps lock)\b")


def _ja_orientou(historico):
    """O assistente ja sugeriu um passo local (Ctrl+F5, aba anonima...) nesta conversa."""
    for h in reversed((historico or [])[-6:]):
        if h.get("de") == "operador":
            return False
        if h.get("de") == "assistente" and _ORIENTOU_RE.search(normalize(h.get("texto"))):
            return True
    return False


def _seg(p):
    return str(p.get("seg")).replace(".", ",")


def _plural(n, um, varios):
    return f"{n} {um if n == 1 else varios}"


def diag_falha(sistema, rel, ctx, repo, loja, cnpj, agora, sonda, persistente, intencao, fornecedor=None,
               seguimento=False, perguntado=""):
    """'Nao funciona': checa o que da para checar (pagina no ar + fluxo real) e,
    sem prova de falha geral, descobre O QUE falhou antes de escalar.

    Criterios objetivos de escalacao (equipe tecnica):
      1. sonda HTTP falhou (2 tentativas)                         -> escala ja
      2. fluxo real parado fora do normal do horario              -> escala ja
      3. acesso bloqueado/expirado (liberar acesso e humano)      -> escala ja
      4. falha continua depois de orientacao/tentativas da loja   -> escala
    Fora disso: nao escala; pergunta o que falta saber (acao, mensagem de erro,
    fornecedor, etapa) e sugere UM passo local. Pagina respondendo HTTP 200 NAO
    e tratado como prova de que login/envio/compra funcionam."""
    nome = SITES[sistema][0]
    acao = rel.get("acao")
    hhmm = _hora(agora)
    resumo = {"assunto": f"{nome}: " + (ACOES[acao] if acao else "relato genérico de que não funciona"),
              "sintomas": _relato_txt(rel) + ([f"fornecedor citado: {fornecedor.upper()}"] if fornecedor else [])}

    def escala(txt, motivo):
        return _r("ANSWER", txt, motivo, True, intencao, ctx,
                  equipe="equipe do e-commerce" if sistema == "ecommerce" else "equipe técnica",
                  resumo={**resumo, "motivo_escalacao": motivo})

    p = sonda(sistema)
    ctx["fontes"].append(f"sonda_http:{sistema} -> {p.get('http') or p.get('erro')} em {p.get('seg')}s")
    if not p.get("ok"):
        return escala(f"Verifiquei agora ({hhmm}) e o {nome} não respondeu ({p.get('erro') or 'sem resposta'}, "
                      "2 tentativas), então não é algo só aí na loja.", "sonda: sistema sem resposta")

    # no seguimento da conversa a checagem e refeita, mas o texto fica curto
    fatos = (f"Conferi de novo ({hhmm}): a página continua abrindo" if seguimento
             else f"Verifiquei agora ({hhmm}): a página do {nome} abre normalmente")
    outras_ok = False   # vi outras lojas/clientes concluindo a acao agora
    evidencia = None
    if sistema == "pedido_eletronico" and loja is not None:
        a = repo.atividade_pedidos(loja["user_id"], fornecedor)
        ctx["fontes"].append(f"pedidos3: {a['pedidos_60min']} pedidos/60min de {a['lojas_60min']} lojas; "
                             f"media mesma hora {a.get('media_mesma_hora')}")
        if a["pedidos_60min"] > 0:
            outras_ok = True
            fatos += (f", e {_plural(a['pedidos_60min'], 'pedido foi enviado', 'pedidos foram enviados')} na "
                      f"última hora por {_plural(a['lojas_60min'], 'loja', 'lojas')}")
        elif (a.get("media_mesma_hora") or 0) >= 1:
            ult = f" (o último foi {_quando(a['ultimo_geral'], agora)})" if a.get("ultimo_geral") else ""
            evidencia = (f"nenhum pedido foi enviado por nenhuma loja na última hora{ult}, e nesse horário a média "
                         f"é de {str(a['media_mesma_hora']).replace('.', ',')} por dia")
        else:
            fatos += (". Nenhum pedido saiu na última hora, mas nesse horário o movimento costuma ser baixo "
                      f"(média de {str(a.get('media_mesma_hora') or 0).replace('.', ',')} na última semana)")
        if fornecedor and a.get("fornecedor"):
            n = a.get("fornecedor_3h") or 0
            ctx["fontes"].append(f"pedidos3: {n} pedidos {a['fornecedor']} nas ultimas 3h (todas as lojas)")
            fatos += (f"; para a {a['fornecedor']}, {_plural(n, 'pedido saiu', 'pedidos saíram')} nas últimas 3 "
                      "horas")
    elif sistema == "ecommerce" and acao == "finalizar_compra" and cnpj:
        e = repo.atividade_ecommerce(cnpj)
        ctx["fontes"].append(f"ecommerce_pedidos: {e['criados_3h']} compras/3h, {e['pagos_3h']} pagas/3h; "
                             f"media {e['media_3h']}")
        if e["criados_3h"] > 0:
            outras_ok = True
            fatos += (f", e {_plural(e['criados_3h'], 'compra foi feita', 'compras foram feitas')} no site nas "
                      f"últimas 3 horas ({e['pagos_3h']} com pagamento confirmado)")
        elif (e.get("media_3h") or 0) >= 1:
            evidencia = (f"nenhuma compra foi feita no site nas últimas 3 horas, e nesse horário a média é de "
                         f"{str(e['media_3h']).replace('.', ',')}")
    fatos += "."

    if evidencia:
        return escala(f"{fatos} Mas {evidencia}. Pode ser só o movimento, mas não consigo descartar uma falha "
                      "geral.", "fluxo real parado fora do normal")
    erro = rel.get("erro") or ""
    if acao == "login" and re.search(r"bloquead|expirad|nao (existe|encontrado|cadastrado)", erro):
        return escala(f"{fatos} A mensagem \"{erro}\" é do acesso do seu usuário, e liberar ou redefinir acesso "
                      "eu não consigo fazer por aqui.", "acesso bloqueado/expirado: so humano libera")
    if persistente:
        tent = [t for t in rel.get("tentou") or [] if t != "disse que já tentou"]
        depois = f" mesmo depois de {_TENTOU_INF.get(tent[0], tent[0])}" if len(tent) == 1 else (
            " mesmo depois do que você já tentou" if tent or rel.get("tentou") else "")
        if outras_ok:
            txt = (f"{fatos} Como outras lojas estão conseguindo agora e aí continua{depois}, parece algo "
                   "específico da sua loja, que eu não consigo ver daqui.")
        else:
            txt = f"{fatos} Como aí continua{depois}, precisa de alguém olhando o que acontece na sua loja."
        return escala(txt, "falha persiste apos orientacao")
    if acao == "finalizar_compra" and seguimento and (erro or rel.get("etapa")):
        # a loja ja respondeu a pergunta (etapa/mensagem) e nao ha passo local
        # nem outra checagem que eu consiga fazer daqui
        quem = "outras compras estão passando" if outras_ok else "não vejo falha geral daqui"
        return escala(f"{fatos[:-1]}, então {quem}. Pelo que você descreveu, precisa de alguém olhando essa "
                      "compra de perto.", "checkout: detalhe coletado, sem checagem adicional")

    # primeiro relato: descobrir O QUE falhou (sem interrogatorio: no maximo 2 perguntas)
    nao_prova = {"login": "isso sozinho não garante que o login esteja funcionando",
                 "enviar_pedido": "isso sozinho não garante que o envio esteja funcionando",
                 "finalizar_compra": "isso sozinho não garante que a compra esteja passando"}
    if acao in nao_prova and not outras_ok:
        fatos = fatos[:-1] + f", mas {nao_prova[acao]}."
    elif outras_ok:
        fatos = fatos[:-1] + (", então não é queda geral." if acao != "enviar_pedido"
                              else ", então o envio está funcionando para outras lojas.")
    if acao is None:
        pergunta = {
            "pedido_eletronico": "O que acontece aí: não consegue entrar, dá erro na hora de enviar o pedido, ou "
                                 "a página nem abre?",
            "ecommerce": "O que acontece aí: o site não abre, algum produto não aparece, ou o cliente não "
                         "consegue finalizar a compra?",
            "relatorios": "O que acontece aí: não consegue entrar, a página não carrega, ou algum número está "
                          "estranho?"}[sistema]
        txt = f"{fatos} {pergunta} Se aparecer alguma mensagem, me manda o texto ou um print."
    elif acao == "login":
        txt = fatos + " "
        txt += ("Aparece alguma mensagem quando você tenta entrar (senha inválida, usuário bloqueado) ou a página "
                "só volta para o login? " if not erro and "aparece alguma mensagem" not in perguntado else "")
        txt += ("Se ainda não tentou, confira se o Caps Lock está desligado e tente numa aba anônima "
                "(Ctrl+Shift+N). Se continuar, me avisa que eu passo para a equipe técnica.")
    elif acao == "enviar_pedido":
        faltam = []
        if not erro:
            faltam.append("qual mensagem aparece quando você clica em enviar")
        if not fornecedor:
            faltam.append("se é com um fornecedor específico ou com todos")
        txt = fatos + " "
        if faltam:
            txt += ("Me diz " + " e ".join(faltam) + "? Enquanto isso, atualize a página com Ctrl+F5 e tente "
                    "enviar de novo.")
        else:
            txt += "Atualize a página com Ctrl+F5 e tente enviar de novo."
        txt += " Se continuar, me avisa que eu passo para a equipe técnica com esses detalhes."
    elif acao == "finalizar_compra":
        txt = (f"{fatos} Em que etapa trava: endereço e frete, pagamento ou na confirmação? "
               + ("" if erro else "Aparece alguma mensagem? ")
               + "Se for com um produto específico, me diga qual.")
    elif acao == "abrir":
        txt = (f"{fatos} Aí pode ser conexão ou navegador: tente atualizar com Ctrl+F5 ou abrir em outro "
               "navegador. Outros sites abrem normal aí? Se continuar, me manda um print da tela.")
    else:  # lento
        txt = (f"{fatos} Tente atualizar com Ctrl+F5; se continuar lento, me diga em que tela demora e desde "
               "quando.")
    return _r("ANSWER", txt, f"{sistema}: {acao or 'acao indefinida'}, primeiro relato", False, intencao, ctx)


def diag_acesso_sem_sistema(rel, ctx, intencao):
    erro = rel.get("erro") or ""
    if re.search(r"bloquead|expirad", erro):
        return _r("ANSWER", f"A mensagem \"{erro}\" é do acesso do seu usuário, e liberar ou redefinir acesso eu "
                  "não consigo fazer por aqui. Em qual sistema é: Pedido Eletrônico, relatórios ou o painel do "
                  "e-commerce?", "acesso bloqueado (sistema nao informado)", True, intencao, ctx,
                  equipe="equipe técnica", resumo={"assunto": "Acesso bloqueado/expirado (sistema não informado)",
                                                   "sintomas": _relato_txt(rel)})
    return _r("ANSWER", "Em qual sistema você está tentando entrar: Pedido Eletrônico, relatórios ou o painel do "
              "e-commerce? E aparece alguma mensagem quando tenta (senha inválida, usuário bloqueado)?",
              "acesso: pergunta sistema", False, intencao, ctx)


_DETALHE_REL_RE = re.compile(
    r"\b(compar\w*|no (erp|caixa|pdv|fechamento|sistema da loja|meu sistema|nosso sistema)|deveria (ser|dar)|"
    r"era pra (ser|dar)|tinha que (ser|dar)|o certo (e|seria)|na verdade (e|foi)|vendemos|vendi|faturamos|"
    r"\d{3,})\b")


def diag_relatorio(texto_norm, ctx, repo, loja, cnpj, agora, persistente=False, seguimento=False):
    """Criterio: arquivo faltando/atrasado = evidencia -> escala. Arquivos ok e
    sem dizer qual numero/comparacao -> pergunta (nao escala). Com o detalhe (ou
    insistindo) -> escala, porque a conta do relatorio eu nao confiro daqui."""
    outro = [c for c in _CNPJ_RE.findall(texto_norm) if c != cnpj]
    if outro:
        return _r("ANSWER", "Por aqui eu só consigo consultar os dados da própria loja deste número. Se precisar de "
                  "outra loja, a mensagem precisa vir do número cadastrado dela.", "pediu outra loja", False,
                  "relatorio", ctx)
    hoje = _agora(agora).date()
    ano, mes = _mes_alvo(texto_norm, hoje)
    cob = repo.cobertura_vendas(cnpj, ano, mes)
    ctx["fontes"].append(f"vendageral/vendas_totalizadas {mes:02d}/{ano}: {len(cob['itens'])} dias por item, "
                         f"{len(cob['totalizadas'])} dias totalizados")
    nome_mes = MESES[mes - 1].replace("marco", "março")
    ultimo_dia = date(ano, mes, monthrange(ano, mes)[1])
    # o arquivo de D e processado ~06:10 de D+2: de madrugada o de D+2 ainda nao e devido (nao e lacuna)
    atraso = ATRASO_ARQUIVO_VENDAS_DIAS + (1 if _agora(agora).hour < HORA_PROCESSAMENTO_VENDAS else 0)
    limite = min(ultimo_dia, hoje - timedelta(days=atraso))
    esperados = {date(ano, mes, d) for d in range(1, limite.day + 1)} if limite >= date(ano, mes, 1) else set()
    pedir = ("Para eu entender a diferença, me diga qual relatório (vendas, ticket médio, ranking...), qual número "
             "aparece e com o que você está comparando.")
    detalhe = bool(_DETALHE_REL_RE.search(texto_norm))
    resumo = {"assunto": f"Relatório com número divergente ({mes:02d}/{ano})",
              "sintomas": [f"relato: {texto_norm[:200]}"]}

    def arquivos_ok(txt_ok, motivo, curto):
        if detalhe or persistente:
            # no seguimento (loja respondeu a pergunta) nao repete tudo
            txt = (f"Obrigado pelo detalhe. {curto}" if seguimento else txt_ok)
            return _r("ANSWER", f"{txt} Então a diferença não é falta de arquivo, e a conta do relatório eu não "
                      "consigo conferir daqui.", motivo + ": com detalhe, escala", True, "relatorio", ctx,
                      equipe="equipe técnica",
                      resumo={**resumo, "motivo_escalacao": "arquivos ok, divergência a conferir"})
        return _r("ANSWER", f"{txt_ok} Então não é falta de arquivo. {pedir}", motivo + ": pede detalhe", False,
                  "relatorio", ctx)
    hist = cob.get("dias_meses_anteriores") or []
    if hist and max(hist) <= 2:  # loja manda UM arquivo por mes (datado do dia 01)
        ctx["fontes"].append(f"padrao de envio: mensal (dias/mes anteriores {hist})")
        chegou = cob["itens"] or cob["totalizadas"]
        proc_ant = cob.get("processado_mes_anterior")
        fechou = hoje >= ultimo_dia + timedelta(days=ATRASO_ARQUIVO_VENDAS_DIAS + 1)
        if chegou:
            quando = f" (processado {_quando(cob['ultimo_processado'], agora)})" if cob["ultimo_processado"] else ""
            return arquivos_ok(f"Conferi: a sua loja envia o arquivo de vendas uma vez por mês, e o de {nome_mes} "
                               f"já chegou{quando}.", "mensal recebido",
                               f"O arquivo de vendas de {nome_mes} da sua loja já chegou.")
        if not fechou:
            ant_txt = (f" O do mês anterior foi processado {_quando(proc_ant, agora)}." if proc_ant else "")
            return _r("ANSWER", f"Conferi: a sua loja envia o arquivo de vendas uma vez por mês, depois que o mês "
                      f"fecha.{ant_txt} O de {nome_mes} ainda não chegou, então por enquanto os relatórios de "
                      f"{nome_mes} não têm as vendas da sua loja. Se o número que está estranho é de outro período, "
                      "me diga qual relatório e o que está comparando.", "mensal ainda nao devido", False,
                      "relatorio", ctx)
        return _r("ANSWER", f"Conferi: a sua loja envia o arquivo de vendas uma vez por mês, e o de {nome_mes} não "
                  "chegou. Sem ele os relatórios desse mês ficam sem as vendas da loja.", "mensal atrasado", True,
                  "relatorio", ctx, equipe="equipe técnica",
                  resumo={**resumo, "motivo_escalacao": f"arquivo mensal de {nome_mes} não chegou"})
    if not cob["itens"] and not cob["totalizadas"]:
        return _r("ANSWER", f"Conferi e não chegou nenhum arquivo de vendas da sua loja em {nome_mes}. Os relatórios "
                  "são montados a partir desses arquivos, então sem eles os números ficam zerados ou incompletos.",
                  "sem arquivos de venda no mes", True, "relatorio", ctx, equipe="equipe técnica",
                  resumo={**resumo, "motivo_escalacao": f"nenhum arquivo de vendas em {nome_mes}"})
    falta_itens = esperados - cob["itens"]
    falta_tot = esperados - cob["totalizadas"]
    so_um = (cob["itens"] ^ cob["totalizadas"]) & esperados
    recebidos = cob["itens"] | cob["totalizadas"]
    partes = [f"Conferi os arquivos de vendas da sua loja em {nome_mes}: recebemos de "
              f"{min(recebidos):%d/%m} até {max(recebidos):%d/%m}"]
    if cob["ultimo_processado"]:
        partes[0] += f" (último processado {_quando(cob['ultimo_processado'], agora)})"
    partes[0] += "."
    if falta_itens or falta_tot:
        faltam = sorted((falta_itens | falta_tot) - so_um)
        if faltam:
            partes.append(f"Faltam os dias {_dias_txt(faltam)}: as vendas desses dias ainda não entraram nos "
                          "relatórios, e isso pode explicar a diferença se o período que você está olhando inclui "
                          "esses dias.")
        if so_um:
            partes.append(f"Nos dias {_dias_txt(so_um)} chegou só um dos dois arquivos (por item ou totalizado), "
                          "então relatórios de produto e de ticket médio podem não bater entre si.")
        return _r("ANSWER", " ".join(partes), "lacuna nos arquivos de venda", True, "relatorio", ctx,
                  equipe="equipe técnica", resumo={**resumo, "motivo_escalacao": "lacuna nos arquivos de venda"})
    partes.append(f"Os dias até {limite:%d/%m} estão completos (os arquivos chegam com cerca de "
                  f"{ATRASO_ARQUIVO_VENDAS_DIAS} dias de atraso).")
    return arquivos_ok(" ".join(partes), "arquivos completos",
                       f"Os arquivos de vendas da sua loja em {nome_mes} estão completos até {limite:%d/%m}.")


def _moeda(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def diag_vendas(texto_norm, ctx, repo, cnpj, agora):
    """Total vendido no mes (arquivo totalizado), com o mesmo periodo do mes anterior para comparar."""
    hoje = _agora(agora).date()
    ano, mes = _mes_alvo(texto_norm, hoje)
    nome_mes = MESES[mes - 1].replace("marco", "março")
    dias = repo.vendas_mes(cnpj, ano, mes)
    ctx["fontes"].append(f"vendas_totalizadas {mes:02d}/{ano}: {len(dias)} dias")
    if not dias:
        return _r("ANSWER", f"Ainda não chegou arquivo de vendas da sua loja de {nome_mes}, então não tenho esse total. "
                  f"Os arquivos chegam com cerca de {ATRASO_ARQUIVO_VENDAS_DIAS} dias de atraso (e algumas lojas "
                  "mandam um arquivo só depois que o mês fecha).", "vendas: sem arquivo no mes", False, "vendas", ctx)
    total, n, itens = sum(d[1] for d in dias), sum(d[2] for d in dias), sum(d[3] for d in dias)
    ini, ult = dias[0][0], dias[-1][0]
    mensal = len(dias) == 1 and ini.day == 1 and (ano, mes) != (hoje.year, hoje.month)
    periodo = "pelo arquivo do mês" if mensal else (
        f"de {ini:%d/%m} a {ult:%d/%m}" if ini != ult else f"em {ini:%d/%m}")
    txt = (f"Vendas da sua loja em {nome_mes} ({periodo}): {_moeda(total)} em {n} vendas"
           + (f", ticket médio de {_moeda(total / n)}" if n else "") + f" e {itens} itens.")
    # mesmo periodo do mes anterior (so se ele veio por dia; arquivo mensal nao da para cortar)
    ant_d = date(ano, mes, 1) - timedelta(days=1)
    ant = repo.vendas_mes(cnpj, ant_d.year, ant_d.month)
    if len(ant) > 2 and not mensal:
        mesmo = [d for d in ant if d[0].day <= ult.day]
        tot_ant = sum(d[1] for d in mesmo)
        if tot_ant > 0:
            nome_ant = MESES[ant_d.month - 1].replace("marco", "março")
            txt += (f" No mesmo período de {nome_ant} foram {_moeda(tot_ant)} "
                    f"({(total / tot_ant - 1) * 100:+.0f}%).").replace("+-", "-")
    if (ano, mes) == (hoje.year, hoje.month) and ult < hoje - timedelta(days=1):
        txt += (f" Os dias depois de {ult:%d/%m} ainda não entraram: o arquivo chega com cerca de "
                f"{ATRASO_ARQUIVO_VENDAS_DIAS} dias de atraso.")
    return _r("ANSWER", txt, "vendas do mes", False, "vendas", ctx)


def diag_ecommerce(texto_norm, ctx, repo, cnpj, agora):
    cfg = repo.config_ecommerce(cnpj)
    ctx["fontes"].append("ecommerce_config_loja (publicacao)")
    if cfg is None:
        return _r("ANSWER", "Não encontrei a configuração de e-commerce da sua loja no sistema, então não consigo "
                  "conferir a publicação dos produtos.", "sem config ecommerce", True, "ecommerce", ctx,
                  equipe="equipe do e-commerce", resumo={"assunto": "Loja sem configuração de e-commerce"})
    if cfg["catalogo_publico"] is False:
        return _r("ANSWER", "Conferi: a sua loja está com o catálogo desativado na configuração do e-commerce, e "
                  "com ele desativado nenhum produto da loja aparece no site. Se a sua loja deveria estar vendendo "
                  "pelo site e você quiser que a equipe verifique a ativação, é só me dizer.",
                  "catalogo desativado: oferece ativacao", False, "ecommerce", ctx)
    ean = (_EAN_RE.search(texto_norm) or [None, None])[1]
    termos = [] if ean else _termos_produto(texto_norm)
    if not ean and not termos:
        return _r("ANSWER", "Consigo verificar sim. Qual produto não está aparecendo? Se puder, mande o código de "
                  "barras (EAN), que é mais certeiro que o nome.", "falta produto", False, "ecommerce", ctx)
    res = repo.produto_estoque(cnpj, ean=ean, termos=termos)
    alvo = ean or " ".join(termos).upper()
    ctx["fontes"].append(f"estoque da loja ({res['data_estoque'] or 'sem estoque'}): {len(res['produtos'])} "
                         f"resultado(s) para '{alvo}'")
    hoje = _agora(agora).date()
    de = res["data_estoque"]
    velho = de is not None and (hoje - de).days > ESTOQUE_VELHO_DIAS
    aviso_velho = (f" Atenção: o último estoque que recebemos da sua loja é de {de:%d/%m}; se o produto entrou "
                   "depois disso, ele só aparece quando um estoque novo chegar." if velho else "")
    if de is None and not res["produtos"]:
        return _r("ANSWER", "Não encontrei nenhum arquivo de estoque da sua loja no sistema, e o site só mostra "
                  "produto que está no estoque enviado.", "sem estoque da loja", True, "ecommerce", ctx,
                  equipe="equipe técnica", resumo={"assunto": "Loja sem arquivo de estoque (e-commerce)"})
    if not res["produtos"]:
        return _r("ANSWER", f"Procurei \"{alvo}\" no último estoque que a sua loja enviou ({de:%d/%m}) e não "
                  "encontrei. Produto que não está no arquivo de estoque não aparece no site. Confere o código de "
                  f"barras (EAN)?{aviso_velho}", "produto fora do estoque", velho, "ecommerce", ctx,
                  equipe="equipe técnica",
                  resumo={"assunto": f"Estoque da loja desatualizado (último {de:%d/%m})"} if velho else None)
    linhas, deveria = [], False
    minimo = cfg.get("estoque_min") or 0
    for p in res["produtos"][:3]:
        nome = f"{p['descricao']} (EAN {p['ean']})"
        qtd = int(p["qtd"]) if p["qtd"] == int(p["qtd"]) else p["qtd"]
        if p["oculto"]:
            # /painel/precificador/restaurar (app.py) desfaz o ocultar
            linhas.append(f"{nome}: está marcado como oculto no catálogo da sua loja, por isso não aparece. Dá "
                          "para reexibir no painel da loja, no Precificador (opção de restaurar o produto).")
        elif p["qtd"] <= 0:
            linhas.append(f"{nome}: está com estoque zerado no último arquivo ({de:%d/%m}); produto sem estoque não "
                          "aparece no site.")
        elif minimo and p["qtd"] < minimo:
            linhas.append(f"{nome}: tem {qtd} un., abaixo do estoque mínimo para publicação configurado na loja "
                          f"({minimo}).")
        else:
            deveria = True
            linhas.append(f"{nome}: está no estoque ({qtd} un. em {de:%d/%m}) e não está oculto. Pelas regras "
                          "que eu consigo checar ele deveria aparecer; pode ser cadastro, imagem ou categoria do "
                          "produto, que eu não consigo confirmar daqui.")
    extra = len(res["produtos"]) - 3
    corpo = ("Conferi no sistema:\n" + "\n".join(f"- {l}" for l in linhas) if len(linhas) > 1
             else "Conferi no sistema: " + linhas[0])
    if extra > 0:
        corpo += f"\n(Há mais {extra} produto(s) parecidos; se não for esse, me mande o EAN.)"
    corpo += aviso_velho
    resumo = None
    if deveria or velho:
        resumo = {"assunto": f"Produto não aparece no e-commerce: {alvo}",
                  "motivo_escalacao": ("regras verificáveis ok, causa não identificada" if deveria
                                       else f"estoque desatualizado (último {de:%d/%m})")}
    return _r("ANSWER", corpo, "produto checado no estoque/catalogo", deveria or velho, "ecommerce", ctx,
              equipe="equipe do e-commerce", resumo=resumo)


def diag_sistema(ctx):
    return _r("ANSWER", "Esse passo a passo eu ainda não tenho confirmado, e não quero te passar o caminho errado. "
              "Me diga em qual tela você está e o que quer fazer (se puder, mande um print), que isso ajuda quem "
              "for te orientar.", "como usar: sem guia verificado", True, "sistema", ctx,
              resumo={"assunto": "Dúvida de uso do sistema (sem guia verificado)"})


_SISTEMA_DO_TIPO = {"pe_fora": "pedido_eletronico", "ecom_fora": "ecommerce", "rel_fora": "relatorios"}


def responder_suporte(tipo, texto_norm, ctx, repo, identidade, agora, sonda=None, historico_norm="",
                      atual_norm=None, historico=None):
    """Resposta de suporte tecnico. identidade ja vem resolvida pelo telefone.
    texto_norm: texto para diagnostico (pode incluir o historico quando a
    mensagem atual so diz "continua"); atual_norm: so a mensagem nova."""
    from poupaqui_ai_assistente import _CONTINUA_RE, achar_fornecedor
    sonda = sonda or sonda_http
    atual_norm = atual_norm if atual_norm is not None else texto_norm
    loja_ok = bool(identidade.get("verificada")) and identidade.get("tipo") == "loja"

    # endereco antigo (ex.: pedidoeletronico.vercel.app, pausado): o problema e o endereco, nao o sistema
    end = sistema_do_endereco(atual_norm) or (sistema_do_endereco(texto_norm) if _CONTINUA_RE.search(atual_norm)
                                              else None)
    if end and end[2]:
        nome, url = SITES[end[0]]
        s = sonda(end[0])
        ctx["fontes"].append(f"sonda:{end[0]}")
        no_ar = (f"Conferi agora ({_hora(_agora(agora))}) e o endereço oficial está no ar." if s.get("ok") else
                 f"Conferi agora ({_hora(_agora(agora))}) e o endereço oficial também não respondeu; vou passar "
                 "para a equipe técnica.")
        return _r("ANSWER", f"Esse endereço ({end[1]}) é antigo e foi desativado, por isso aparece essa tela de "
                  f"erro. O {nome} agora fica em {url} . {no_ar} Consegue entrar por lá?",
                  f"endereco antigo do {end[0]}", not s.get("ok"), _FORA_DO_SISTEMA[end[0]], ctx,
                  equipe="equipe técnica", resumo=None if s.get("ok") else {
                      "assunto": f"{nome}: endereço oficial não respondeu", "checagens": [str(s)]})

    # contexto do dialogo: o relato anterior (mesmo sistema) completa o atual
    tipo_hist = detectar_suporte(historico_norm) if historico_norm else None
    rel_atual = extrair_relato(atual_norm)
    sistema = _SISTEMA_DO_TIPO.get(tipo) or rel_atual["sistema"]
    sis_hist = (_SISTEMA_DO_TIPO.get(tipo_hist) or sistema_relatado(historico_norm)) if tipo_hist else None
    mesmo_assunto = bool(tipo_hist) and (sis_hist is None or sistema is None or sis_hist == sistema)
    rel = juntar_relato(extrair_relato(historico_norm), rel_atual) if mesmo_assunto else rel_atual
    if tipo == "acesso":
        sistema = sistema or rel.get("sistema")
        rel["acao"] = "login"
    # Persistencia = ja havia relato do mesmo problema E agora a loja diz que
    # continua, conta o que tentou, ou ja recebeu um passo local (Ctrl+F5...)
    persistente = mesmo_assunto and (tipo_hist in FALHA_TIPOS + ("relatorio",)) and bool(
        _CONTINUA_RE.search(atual_norm) or rel_atual["tentou"] or _ja_orientou(historico))
    if tipo in FALHA_TIPOS:
        if sistema is None:
            return diag_acesso_sem_sistema(rel, ctx, tipo)
        loja = repo.loja_por_cnpj(identidade.get("ref")) if loja_ok and sistema == "pedido_eletronico" else None
        fornecedor = None
        if loja is not None and rel.get("acao") == "enviar_pedido":
            nomes = [f["nome"] for f in repo.fornecedores_entrega()]
            fornecedor = achar_fornecedor(atual_norm, nomes) or (
                achar_fornecedor(historico_norm, nomes) if mesmo_assunto else None)
        ultima_ia = next((normalize(h.get("texto")) for h in reversed(historico or [])
                          if h.get("de") == "assistente"), "")
        return diag_falha(sistema, rel, ctx, repo, loja, str(identidade.get("ref") or "") if loja_ok else None,
                          agora, sonda, persistente, tipo, fornecedor,
                          seguimento=mesmo_assunto and tipo_hist in FALHA_TIPOS, perguntado=ultima_ia)
    if tipo == "sistema":
        return diag_sistema(ctx)
    if not loja_ok:
        return _r("ANSWER", "Consigo verificar isso, mas preciso que a mensagem venha do número cadastrado da loja no "
                  "Pedido Eletrônico, porque são dados da loja. Se o número mudou, quem atualiza o cadastro é a "
                  "equipe.", "identidade nao verificada", True, tipo, ctx,
                  resumo={"assunto": f"Número não cadastrado pedindo dados de loja ({tipo})"})
    cnpj = str(identidade.get("ref") or "")
    if tipo == "vendas":
        # "e setembro?" depois de perguntar outubro: o mes da mensagem nova vence o do historico
        mes_agora = re.search(rf"\b({'|'.join(MESES)}|mes passado)\b", atual_norm)
        return diag_vendas(atual_norm if mes_agora else texto_norm, ctx, repo, cnpj, agora)
    if tipo == "relatorio":
        return diag_relatorio(texto_norm, ctx, repo, None, cnpj, agora, persistente,
                              seguimento=mesmo_assunto and tipo_hist == "relatorio")
    return diag_ecommerce(texto_norm, ctx, repo, cnpj, agora)
