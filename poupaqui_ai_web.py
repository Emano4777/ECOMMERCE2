"""Busca na internet para a conversa geral do assistente (noticia, esporte, clima, receita, curiosidade).

So para assunto GERAL: pedido, estoque, preco da loja, financeiro e sistemas continuam nas rotas com
dados reais (poupaqui_ai_assistente / poupaqui_ai_suporte). A consulta sai sem dado pessoal (telefone,
CPF/CNPJ, e-mail e numero de pedido sao removidos antes). Provedor: biblioteca ddgs (sem chave).
"""
import re
import time

_PESSOAL_RE = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b|\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b|"
                         r"\S+@\S+|\(?\d{2}\)?\s?9?\d{4}-?\d{4}\b|\bpedido\s*(n[ºo°]?\s*)?\d+\b", re.I)
_NOTICIA_RE = re.compile(r"\b(ontem|hoje|agora|semana|ultim\w*|recente\w*|noticia\w*|jogo|placar|resultado|"
                         r"ganhou|perdeu|campeonato|rodada|eleic\w*|aconteceu|morreu|lancamento|lancou)\b")

_RECENTE_RE = re.compile(r"\b(ontem|hoje|agora|essa semana|nesta semana|esta semana|ultimas noticias|noticias de hoje|acabou de|agorinha)\b")

# pergunta de fato/atualidade (o modelo decide a consulta; isto so evita chamar a busca em papo de rotina)
_PERGUNTA_RE = re.compile(r"\?|\b(quem|quando|qual|quais|quanto|quantos|quantas|onde|como (faz|faco|fazer|"
                          r"funciona|e)|o que (e|foi|aconteceu)|por que|porque|me (conta|fala|explica|ensina|passa)|"
                          r"sabe (se|quem|quando|qual|onde|como)|previsao|cotacao|chov\w*|chuva|clima|temperatura|"
                          r"dolar|euro|bitcoin|placar)\b")


def parece_pergunta_de_fato(norm, original=""):
    """norm vem sem "?" (normalize tira pontuacao): o texto original diz se era pergunta."""
    return (bool(_PERGUNTA_RE.search(norm)) or "?" in str(original)) and len(norm.split()) >= 3


def limpar_consulta(consulta):
    return re.sub(r"\s+", " ", _PESSOAL_RE.sub(" ", str(consulta or ""))).strip()[:150]


def buscar(consulta, max_resultados=5, timeout=10):
    """[{titulo, trecho, fonte, data}] ou []. Nunca levanta: sem internet = sem resultados."""
    consulta = limpar_consulta(consulta)
    if len(consulta) < 3:
        return []
    try:
        from ddgs import DDGS
    except ImportError:
        return []
    from poupaqui_ai_policy import normalize
    n = normalize(consulta)
    noticia = bool(_NOTICIA_RE.search(n))
    # filtro de 1 semana so quando a pergunta pede algo RECENTE de forma explicita; "a ultima copa",
    # "o ultimo jogo" querem a ocorrencia mais recente, que pode ser de meses atras
    so_semana = bool(_RECENTE_RE.search(n))
    out, vistos = [], set()
    for regiao in ("br-pt", "wt-wt"):
        brutos = []
        try:
            with DDGS(timeout=timeout) as d:
                if noticia:   # noticia + busca comum juntas: o modelo escolhe pelas datas
                    try:
                        brutos += list(d.news(consulta, region=regiao, max_results=4,
                                              timelimit="w" if so_semana else None))
                    except Exception:
                        pass
                try:
                    brutos += list(d.text(consulta, region=regiao, max_results=max_resultados))
                except Exception:
                    pass
        except Exception:
            brutos = []
        for b in brutos:
            chave = normalize(str(b.get("title") or ""))[:60]
            if chave in vistos:
                continue
            vistos.add(chave)
            trecho = re.sub(r"\s+", " ", str(b.get("body") or "")).strip()
            if not trecho:
                continue
            url = str(b.get("url") or b.get("href") or "")
            fonte = re.sub(r"^https?://(www\.)?", "", url).split("/")[0] or str(b.get("source") or "")
            out.append({"titulo": str(b.get("title") or "")[:120], "trecho": trecho[:300], "fonte": fonte[:60],
                        "data": str(b.get("date") or "")[:10]})
        if out:
            break
        time.sleep(0.3)
    return out[:max_resultados + 3]


def _get_json(url, timeout=10):
    import json
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "PoupAqui-Assistente/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


_TEMPO_WMO = {0: "céu limpo", 1: "poucas nuvens", 2: "parcialmente nublado", 3: "nublado", 45: "neblina",
              48: "neblina", 51: "garoa fraca", 53: "garoa", 55: "garoa forte", 61: "chuva fraca", 63: "chuva",
              65: "chuva forte", 80: "pancadas de chuva", 81: "pancadas de chuva", 82: "pancadas fortes",
              95: "tempestade", 96: "tempestade com granizo", 99: "tempestade com granizo"}


def clima(cidade):
    """Previsao de 3 dias (Open-Meteo, sem chave) em texto para o prompt, ou None."""
    from urllib.parse import quote
    try:
        geo = _get_json("https://geocoding-api.open-meteo.com/v1/search?count=1&language=pt&name="
                        + quote(str(cidade)[:60]))
        lugar = (geo.get("results") or [None])[0]
        if not lugar:
            return None
        f = _get_json("https://api.open-meteo.com/v1/forecast?latitude={}&longitude={}&timezone=America%2FSao_Paulo"
                      "&forecast_days=3&current=temperature_2m,weather_code&daily=temperature_2m_max,"
                      "temperature_2m_min,precipitation_probability_max,weather_code".format(
                          lugar["latitude"], lugar["longitude"]))
    except Exception:
        return None
    d = f.get("daily") or {}
    nome = ", ".join(x for x in (lugar.get("name"), lugar.get("admin1")) if x)
    linhas = [f"Previsão do tempo para {nome} (fonte: Open-Meteo, consultada agora):"]
    agora = f.get("current") or {}
    if agora:
        linhas.append(f"Agora: {agora.get('temperature_2m')}°C, {_TEMPO_WMO.get(agora.get('weather_code'), 'tempo variável')}.")
    for i, dia in enumerate(d.get("time") or []):
        rotulo = ["hoje", "amanhã", "depois de amanhã"][i] if i < 3 else dia
        linhas.append(f"{rotulo} ({dia[8:10]}/{dia[5:7]}): {_TEMPO_WMO.get(d['weather_code'][i], 'tempo variável')}, "
                      f"mín {d['temperature_2m_min'][i]}°C, máx {d['temperature_2m_max'][i]}°C, "
                      f"chance de chuva {d['precipitation_probability_max'][i]}%.")
    return "\n".join(linhas)


def _reais(v):
    return f"R$ {float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


_COTACAO_CACHE = {"em": 0.0, "texto": None}


def cotacao():
    """Dolar, euro e bitcoin em reais em texto para o prompt, ou None. AwesomeAPI primeiro; o IP
    compartilhado da HostGator leva HTTP 429 la, entao cai para ExchangeRate-API (dolar/euro, referencia
    do dia) + CoinGecko (bitcoin). Cache de 5 min para nao estourar limite de nenhuma."""
    if _COTACAO_CACHE["texto"] and time.monotonic() - _COTACAO_CACHE["em"] < 300:
        return _COTACAO_CACHE["texto"]
    linhas = []
    try:
        c = _get_json("https://economia.awesomeapi.com.br/json/last/USD-BRL,EUR-BRL,BTC-BRL")
        linhas = ["Cotações em reais (fonte: AwesomeAPI, consultada agora):"]
        for k, nome in {"USDBRL": "Dólar comercial", "EURBRL": "Euro", "BTCBRL": "Bitcoin"}.items():
            v = c.get(k)
            if v:
                linhas.append(f"{nome}: compra {_reais(v['bid'])} (variação no dia {v.get('pctChange')}%, "
                              f"atualizado {str(v.get('create_date'))[:16]}).")
    except Exception:
        linhas = []
    if len(linhas) <= 1:
        linhas = ["Cotações em reais (consultadas agora):"]
        try:
            fx = _get_json("https://open.er-api.com/v6/latest/USD")
            brl, eur = fx["rates"]["BRL"], fx["rates"]["EUR"]
            linhas.append(f"Dólar: {_reais(brl)} e Euro: {_reais(brl / eur)} (fonte: ExchangeRate-API, referência "
                          f"do dia {str(fx.get('time_last_update_utc', ''))[5:16]}).")
        except Exception:
            pass
        try:
            b = _get_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=brl"
                          "&include_24hr_change=true")["bitcoin"]
            linhas.append(f"Bitcoin: {_reais(b['brl'])} (variação em 24 h {b.get('brl_24h_change', 0):.2f}%, "
                          "fonte: CoinGecko).")
        except Exception:
            pass
    texto = "\n".join(linhas) if len(linhas) > 1 else None
    if texto:
        _COTACAO_CACHE.update(em=time.monotonic(), texto=texto)
    return texto


def contexto_para_prompt(resultados, hoje):
    """Bloco de resultados para o system prompt do modelo."""
    linhas = [f"Resultados de busca na internet (hoje é {hoje:%d/%m/%Y}). Use SÓ o que está aqui para fatos "
              "atuais; se a resposta não estiver nos resultados, diga que não encontrou. Pode citar a fonte "
              "pelo nome do site, sem link:"]
    for i, r in enumerate(resultados, 1):
        data = f" ({r['data']})" if r.get("data") else ""
        linhas.append(f"{i}. {r['titulo']}{data} — {r['trecho']} [{r['fonte']}]")
    return "\n".join(linhas)
