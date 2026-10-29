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
    noticia = bool(_NOTICIA_RE.search(normalize(consulta)))
    out = []
    for regiao in ("br-pt", "wt-wt"):
        try:
            with DDGS(timeout=timeout) as d:
                # noticia: so da ultima semana (sem isso vinha jogo de meses atras como se fosse 'ontem')
                brutos = (list(d.news(consulta, region=regiao, max_results=max_resultados, timelimit="w"))
                          if noticia else []) or \
                    list(d.text(consulta, region=regiao, max_results=max_resultados))
        except Exception:
            brutos = []
        for b in brutos:
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
    return out[:max_resultados]


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


def cotacao():
    """Dolar, euro e bitcoin em reais (AwesomeAPI, sem chave) em texto para o prompt, ou None."""
    try:
        c = _get_json("https://economia.awesomeapi.com.br/json/last/USD-BRL,EUR-BRL,BTC-BRL")
    except Exception:
        return None
    nomes = {"USDBRL": "Dólar comercial", "EURBRL": "Euro", "BTCBRL": "Bitcoin"}
    linhas = ["Cotações em reais (fonte: AwesomeAPI, consultada agora):"]
    for k, nome in nomes.items():
        v = c.get(k)
        if v:
            linhas.append(f"{nome}: compra R$ {float(v['bid']):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                          + f" (variação no dia {v.get('pctChange')}%, atualizado {str(v.get('create_date'))[:16]}).")
    return "\n".join(linhas) if len(linhas) > 1 else None


def contexto_para_prompt(resultados, hoje):
    """Bloco de resultados para o system prompt do modelo."""
    linhas = [f"Resultados de busca na internet (hoje é {hoje:%d/%m/%Y}). Use SÓ o que está aqui para fatos "
              "atuais; se a resposta não estiver nos resultados, diga que não encontrou. Pode citar a fonte "
              "pelo nome do site, sem link:"]
    for i, r in enumerate(resultados, 1):
        data = f" ({r['data']})" if r.get("data") else ""
        linhas.append(f"{i}. {r['titulo']}{data} — {r['trecho']} [{r['fonte']}]")
    return "\n".join(linhas)
