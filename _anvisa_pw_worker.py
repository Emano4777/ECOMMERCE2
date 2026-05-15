"""
ANVISA worker — usa requests (sem Playwright) para buscar dados do Bulário ANVISA.
Lê chaves do arquivo --input, escreve resultados JSONL em --output.
"""
import sys
import json
import re
import time
import argparse
import unicodedata
import urllib.parse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ANVISA_BASE = "https://consultas.anvisa.gov.br"

_HEADERS = {
    "Authorization": "Guest",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://consultas.anvisa.gov.br/",
    "Origin": "https://consultas.anvisa.gov.br",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

_FALLBACK_BLOCKLIST = frozenset([
    "CARBONATO", "VITAMINA", "GLICERINA", "EXTRATO", "MESILATO",
    "REVEST", "LEGRAND", "GLENMARK", "BIOSINTETICA", "CIMED",
    "DOSADOR", "GOTAS", "ZYDUS",
])

# Prefixos de sal que ANVISA usa no nome oficial (ex: "CLORIDRATO DE METFORMINA").
# Injetados como fallback quando a busca pelo INN sozinho retorna 0 resultados.
_SALT_PREFIXES = [
    "CLORIDRATO DE",
    "BESILATO DE",
    "MALEATO DE",
    "FUMARATO DE",
    "HEMIFUMARATO DE",
    "TARTARATO DE",
    "HEMITARTARATO DE",
    "SUCCINATO DE",
    "BROMIDRATO DE",
    "DICLORIDRATO DE",
    "BISSULFATO DE",
    "OXALATO DE",
    "CITRATO DE",
    "DIPROPIONATO DE",
    "FUROATO DE",
    "ACETATO DE",
]

_GENERIC_FIRST_WORDS = frozenset([
    "VITAMINA", "GLICERINA", "CAPILAR", "EXTRATO", "OLEO", "OMEGA",
    "AGUA", "FITA", "HORA", "PROT", "SORO", "ROSA", "CERA",
    "CINCO", "CLORETO", "FENO", "ACIDO", "MESILATO",
    # Chaves compostas que geram falso positivo se 2ª palavra não constar no nome
    "INALADOR",  # INALADOR COMPRESSOR ≠ Inalador Vick
    "COMPLEXO",  # COMPLEXO MAGNESIO ≠ Complexo B
])

# Chaves de 1 palavra genéricas demais que sempre geram falso positivo.
# Retornam imediatamente {encontrado: False} sem consultar a API.
_CHAVES_BLOQUEADAS = frozenset([
    "NATU",     # prefixo de marca Vitnatu, encontra "Natulaxe" (errado)
    "MULTI",    # genérico demais, encontra "Multiler" (errado)
    "VITAMINA", # genérico demais, encontra "Vitamina D3" vermelha (errado)
])

# A API ANVISA é sensível a acentos — para drogas cujo nome oficial tem acento
# em posição que torna a busca ASCII inútil, definimos os termos acentuados aqui.
_TENTATIVAS_ACENTUADAS = {
    "ACIDO VALPROICO":    ["ÁCIDO VALPRÓICO",        "VALPRÓICO"],
    "ACIDO URSODESOXICO": ["ÁCIDO URSODESOXICÓLICO",  "URSODESOXICÓLICO"],
}


def _norm(s: str) -> str:
    """Remove acentos e converte para maiúsculas para comparações sem acento."""
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii").upper()


def _match_valido(chave, nome_anvisa):
    """Rejeita falso positivo quando 1ª palavra é genérica e o nome retornado
    não contém as outras palavras da chave (comparação sem acento)."""
    palavras = _norm(chave).split()
    if len(palavras) < 2 or palavras[0] not in _GENERIC_FIRST_WORDS:
        return True
    nome_norm = _norm(nome_anvisa)
    return any(p in nome_norm for p in palavras[1:])


def _melhor_item(chave, items):
    """Retorna o item mais específico da lista (sem acento nas comparações).

    Tier 1 — começa com a chave → mais específico (menor nome).
    Tier 2 — contém TODAS as palavras da chave → para combos como AMOXICILINA CLAVULANATO.
    Tier 3 — qualquer item → menor nome.
    """
    if not items:
        return None
    chave_norm = _norm(chave)
    palavras_chave = chave_norm.split()

    # Tier 1: nome começa com a chave normalizada
    candidatos = [
        i for i in items
        if _norm(i.get("nomeProduto", "")).startswith(chave_norm)
    ]

    if not candidatos and len(palavras_chave) >= 2:
        # Tier 2: nome contém TODAS as palavras da chave
        candidatos = [
            i for i in items
            if all(p in _norm(i.get("nomeProduto", "")) for p in palavras_chave)
        ]

    pool = candidatos if candidatos else items

    # Para chave de 1 INN, prefere produtos sem combinação ("+") — evita associar
    # bisoprolol puro com "Fumarato De Bisoprolol+ Hidroclorotiazida".
    if len(palavras_chave) == 1:
        sem_combo = [i for i in pool if "+" not in (i.get("nomeProduto") or "")]
        if sem_combo:
            pool = sem_combo

    # Para chave de 2 palavras, exige produto com ≤1 componente extra (≤1 "+").
    # Evita "PARACETAMOL CAFEINA" → "Paracetamol + Carisoprodol + Diclofenaco + Cafeína".
    # Se não existir versão simples, rejeita (retorna None → não encontrado).
    if len(palavras_chave) == 2:
        simples = [i for i in pool if (i.get("nomeProduto") or "").count("+") <= 1]
        if not simples:
            return None
        pool = simples

    return min(pool, key=lambda i: len(i.get("nomeProduto", "")))


def _extrair_pdf(pdf_bytes):
    try:
        import pdfplumber
        import io
        text = ""
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for pg in pdf.pages[:40]:
                t = pg.extract_text() or ""
                text += t + "\n"
        if not text.strip():
            return None, None, None, None
        tu = text.upper()

        _TODOS_INIC = [
            "INDICAÇÕES", "PARA QUE ESTE MEDICAMENTO", "PARA QUE SERVE",
            "CONTRAINDICAÇÕES", "QUANDO NÃO DEVO USAR",
            "POSOLOGIA", "COMO USAR ESTE MEDICAMENTO", "MODO DE USAR",
            "ADVERTÊNCIAS E PRECAUÇÕES", "ADVERTÊNCIAS", "PRECAUÇÕES",
            "REAÇÕES ADVERSAS", "INTERAÇÕES MEDICAMENTOSAS",
            "SUPERDOSE", "ARMAZENAMENTO", "COMO CONSERVAR",
            "DIZERES LEGAIS", "RESULTADOS DE EFICÁCIA",
        ]
        _RE_TOC_LINE = re.compile(r"^\s*\d+[\s.]+[A-ZÁÉÍÓÚÃÕ]", re.MULTILINE)

        def _e_toc(trecho):
            linhas = [l for l in trecho.split("\n") if l.strip()]
            if not linhas:
                return True
            toc_count = sum(1 for l in linhas if _RE_TOC_LINE.match(l))
            return toc_count / len(linhas) > 0.4

        def secao(inicios, fins):
            for kw in inicios:
                pos = 0
                while True:
                    idx = tu.find(kw, pos)
                    if idx < 0:
                        break
                    pos = idx + 1
                    start = idx + len(kw)
                    while start < len(tu) and tu[start] in " \t\r\n:?!":
                        start += 1
                    end = min(start + 5000, len(tu))
                    for fkw in fins + _TODOS_INIC:
                        fidx = tu.find(fkw, start + 60)
                        if 0 < fidx - start < end - start:
                            end = fidx
                    if end <= start:
                        end = min(start + 5000, len(tu))
                    trecho = text[start:end].strip()
                    if len(trecho) < 60:
                        continue
                    if _e_toc(trecho):
                        continue
                    _LIMITE = 4500
                    if len(trecho) > _LIMITE:
                        trecho = trecho[:_LIMITE]
                        ultimo_ponto = max(
                            trecho.rfind(". "), trecho.rfind(".\n"),
                            trecho.rfind("! "), trecho.rfind("?\n"),
                        )
                        if ultimo_ponto > _LIMITE // 2:
                            trecho = trecho[:ultimo_ponto + 1]
                    return trecho
            return None

        serve_para = secao(
            ["PARA QUE ESTE MEDICAMENTO É INDICADO", "PARA QUE ESTE MEDICAMENTO",
             "PARA QUE É INDICADO", "PARA QUE SERVE", "INDICAÇÕES",
             "O QUE É ESTE MEDICAMENTO"],
            ["CONTRAINDICAÇÕES", "QUANDO NÃO DEVO USAR", "COMO USAR",
             "POSOLOGIA", "ADVERTÊNCIAS", "PRECAUÇÕES", "REAÇÕES ADVERSAS"],
        )
        como_usar = secao(
            ["COMO USAR ESTE MEDICAMENTO", "POSOLOGIA E MODO DE USAR",
             "COMO DEVO USAR", "POSOLOGIA E MODO DE ADMINISTRAÇÃO",
             "POSOLOGIA", "MODO DE USAR", "MODO DE ADMINISTRAÇÃO"],
            ["REAÇÕES ADVERSAS", "ADVERTÊNCIAS", "COMO CONSERVAR",
             "ARMAZENAMENTO", "SUPERDOSE", "INTERAÇÕES MEDICAMENTOSAS",
             "DIZERES LEGAIS"],
        )
        alertas = secao(
            ["ADVERTÊNCIAS E PRECAUÇÕES", "QUANDO NÃO DEVO USAR ESTE MEDICAMENTO",
             "QUANDO NÃO DEVO USAR", "QUAIS OS MALES QUE ESTE MEDICAMENTO PODE ME CAUSAR",
             "QUAIS OS RISCOS", "CONTRAINDICAÇÕES", "ADVERTÊNCIAS", "PRECAUÇÕES"],
            ["REAÇÕES ADVERSAS", "INTERAÇÕES MEDICAMENTOSAS",
             "COMO USAR", "POSOLOGIA", "SUPERDOSE", "DIZERES LEGAIS"],
        )
        tarja_pdf = None
        if re.search(
            r"TARJA\s+PRETA"
            r"|RECEITA\s+DE\s+CONTROLE\s+ESPECIAL"
            r"|SUJEITO\s+A\s+CONTROLE\s+ESPECIAL"
            r"|NOTIFICA[CÇ][AÃ]O\s+DE\s+RECEITA"
            r"|RECEITA\s+DE\s+CONTROLE"
            r"|LISTA\s+[A-E]\d"
            r"|PORT(?:ARIA)?\s*344"
            r"|PSICOTR[OÓ]PICO",
            tu,
        ):
            tarja_pdf = "preta"
        elif re.search(
            r"VENDA\s+SOB\s+PRESCRI[CÇ][AÃ]O|USO\s+SOB\s+PRESCRI[CÇ][AÃ]O"
            r"|TARJA\s+VERMELHA|MEDICAMENTO\s+SUJEITO\s+A\s+PRESCRI"
            r"|SOMENTE\s+(?:COM|SOB)\s+PRESCRI",
            tu,
        ):
            tarja_pdf = "vermelha"

        return serve_para, como_usar, alertas, tarja_pdf
    except Exception:
        return None, None, None, None


def _make_session():
    s = requests.Session()
    s.headers.update(_HEADERS)
    # Visita a homepage para pegar cookies de sessão (idêntico ao que o browser faz)
    try:
        s.get(f"{ANVISA_BASE}/", timeout=15)
    except Exception:
        pass
    return s


def _get_json(session, url, retries=3):
    """GET JSON com retry e backoff. Loga status HTTP em stderr."""
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=25)
            if r.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"  [RATE LIMIT] HTTP 429 — aguardando {wait}s...",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            if not r.ok:
                print(f"  [WARN] HTTP {r.status_code}: {url[:100]}",
                      file=sys.stderr, flush=True)
                if attempt < retries - 1:
                    time.sleep(3 * (attempt + 1))
                continue
            return r.json()
        except requests.exceptions.Timeout:
            print(f"  [TIMEOUT] tentativa {attempt + 1}: {url[:100]}",
                  file=sys.stderr, flush=True)
            if attempt < retries - 1:
                time.sleep(5)
        except Exception as exc:
            print(f"  [ERR] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            if attempt < retries - 1:
                time.sleep(3)
    return None


def _api_bulario(chave, session):
    """Busca chave na API ANVISA. Retorna lista de items ou [].

    Ordem de tentativas (para no 1º hit):
    1. Chave completa
    2. [2 palavras] insere "DE" entre elas  (ex: CLORIDRATO METFORMINA → CLORIDRATO DE METFORMINA)
    3. [2 palavras, p0 ≥ 8 chars] só a 1ª palavra               (ex: BISOPROLOL REVEST → BISOPROLOL)
    4. [2 palavras, p0 curta/genérica] só a 2ª palavra           (ex: ACIDO VALPROICO → VALPROICO)
    5. [1 palavra] injeta prefixos de sal na chave               (ex: ANLODIPINO → BESILATO DE ANLODIPINO)
    6. [2 palavras, p0 não bloqueada] injeta sal na 1ª palavra   (ex: BISOPROLOL → FUMARATO DE BISOPROLOL)
    7. [2 palavras, p1 não bloqueada] injeta sal na 2ª palavra   (ex: ACIDO VALPROICO → ACIDO VALPROICO p/ sal)
    """
    palavras = chave.split()
    p0 = palavras[0] if palavras else ""
    p1 = palavras[1] if len(palavras) >= 2 else ""
    tentativas = [chave]

    if len(palavras) == 2:
        tentativas.append(f"{p0} DE {p1}")

    # Fallback para 1ª palavra quando é longa o suficiente
    if len(palavras) > 1 and len(p0) >= 8 and p0 not in _FALLBACK_BLOCKLIST:
        tentativas.append(p0)

    # Fallback para 2ª palavra quando 1ª é genérica/curta (ex: ACIDO VALPROICO → VALPROICO)
    if (len(palavras) == 2
            and (len(p0) < 8 or p0 in _GENERIC_FIRST_WORDS)
            and len(p1) >= 5
            and p1 not in _FALLBACK_BLOCKLIST):
        tentativas.append(p1)

    # Sal injection na chave de 1 palavra
    if len(palavras) == 1 and len(p0) >= 5:
        for salt in _SALT_PREFIXES:
            tentativas.append(f"{salt} {chave}")

    # Sal injection na 1ª palavra de chave de 2 palavras
    # Ex: BISOPROLOL REVEST → FUMARATO DE BISOPROLOL
    if len(palavras) >= 2 and len(p0) >= 5 and p0 not in _FALLBACK_BLOCKLIST:
        for salt in _SALT_PREFIXES:
            tentativas.append(f"{salt} {p0}")

    # Sal injection na 2ª palavra quando 1ª é genérica/curta
    # Ex: ACIDO VALPROICO → CLORIDRATO DE VALPROICO (improvável mas completa a busca)
    if (len(palavras) == 2
            and (len(p0) < 8 or p0 in _GENERIC_FIRST_WORDS)
            and len(p1) >= 5
            and p1 not in _FALLBACK_BLOCKLIST):
        for salt in _SALT_PREFIXES:
            tentativas.append(f"{salt} {p1}")

    # Tentativas acentuadas para drogas onde a API não responde à forma ASCII
    for extra in _TENTATIVAS_ACENTUADAS.get(chave, []):
        tentativas.append(extra)

    # Remove duplicatas mantendo ordem
    seen: set = set()
    tentativas_unicas = []
    for t in tentativas:
        if t not in seen:
            seen.add(t)
            tentativas_unicas.append(t)

    for q in tentativas_unicas:
        q_enc = urllib.parse.quote(q)
        url = (
            f"{ANVISA_BASE}/api/consulta/bulario"
            f"?column=PRODUTO&count=10"
            f"&filter[nomeProduto]={q_enc}"
            f"&order=asc&page=1"
        )
        data = _get_json(session, url)
        if data is None:
            continue
        items = data.get("content") or data.get("data") or []
        if not isinstance(items, list):
            items = []
        if items:
            return items

    return []


def _buscar(chave, session):
    if chave in _CHAVES_BLOQUEADAS:
        return {"encontrado": False}
    try:
        items = _api_bulario(chave, session)
        if not items:
            return {"encontrado": False}

        item = _melhor_item(chave, items)
        if item is None or not _match_valido(chave, item.get("nomeProduto", "")):
            return {"encontrado": False}

        id_prod  = item["idProduto"]
        jwt_bula = item.get("idBulaPacienteProtegido", "")

        url_detail = f"{ANVISA_BASE}/api/consulta/medicamento/produtos/codigo/{id_prod}"
        detail = _get_json(session, url_detail) or {}

        serve_para = como_usar = alertas = tarja_pdf = None
        if jwt_bula:
            bula_url = (
                f"{ANVISA_BASE}/api/consulta/medicamentos/arquivo/bula/parecer"
                f"/{jwt_bula}/?Authorization="
            )
            try:
                r = session.get(bula_url, timeout=60)
                ct = r.headers.get("content-type", "")
                if r.ok and "html" not in ct and len(r.content) <= 8_000_000:
                    serve_para, como_usar, alertas, tarja_pdf = _extrair_pdf(r.content)
            except Exception as exc:
                print(f"  [PDF ERR] {exc}", file=sys.stderr, flush=True)

        nome = (detail.get("nomeComercial") or item.get("nomeProduto", "")).title()
        lab  = (
            (detail.get("empresa") or {}).get("razaoSocial")
            or item.get("razaoSocial", "")
        ).title()
        pa = detail.get("principioAtivo", "")

        # Tarja: 1) PDF completo  2) campo tipoReceituario  3) regex nas seções
        tarja = tarja_pdf

        if tarja is None:
            # Palavras que indicam CONTROLE ESPECIAL (Portaria 344) → tarja preta
            _PRETA_KW = (
                "CONTROLE ESPECIAL", "PSICOTR",
                "LISTA A", "LISTA B", "LISTA C5",
                "ENTORPECENTE", "NOTIFICACAO DE RECEITA", "NOTIFICAÇÃO DE RECEITA",
            )
            for src in (detail, item):
                val = str(
                    src.get("tipoReceituario") or src.get("tipo_receituario") or ""
                ).strip()
                if not val or val.lower() in ("none", "null", ""):
                    continue
                val_up = val.upper()
                if any(kw in val_up for kw in _PRETA_KW):
                    tarja = "preta"
                elif "ISENTO" in val_up:
                    tarja = None  # OTC, isento de prescrição
                else:
                    tarja = "vermelha"  # com/sem retenção = prescrição simples
                break

        if tarja is None:
            textos = " ".join(filter(None, [alertas or "", como_usar or "", serve_para or ""]))
            tu2 = textos.upper()
            if re.search(
                r"TARJA\s+PRETA"
                r"|RECEITA\s+DE\s+CONTROLE\s+ESPECIAL"
                r"|SUJEITO\s+A\s+CONTROLE\s+ESPECIAL"
                r"|NOTIFICA[CÇ][AÃ]O\s+DE\s+RECEITA"
                r"|LISTA\s+[AB]\d"
                r"|PORT(?:ARIA)?\s*344"
                r"|PSICOTR[OÓ]PICO",
                tu2,
            ):
                tarja = "preta"
            elif re.search(
                r"VENDA\s+SOB\s+PRESCRI[CÇ][AÃ]O|USO\s+SOB\s+PRESCRI[CÇ][AÃ]O"
                r"|SOMENTE\s+(?:COM|SOB)\s+PRESCRI|TARJA\s+VERMELHA"
                r"|MEDICAMENTO\s+SUJEITO\s+A\s+PRESCRI",
                tu2,
            ):
                tarja = "vermelha"

        return {
            "encontrado":      True,
            "nome_anvisa":     nome,
            "laboratorio":     lab,
            "situacao":        "Ativo",
            "principio_ativo": pa,
            "id_produto":      id_prod,
            "url_bula":        f"https://consultas.anvisa.gov.br/#/medicamentos/{id_prod}",
            "serve_para":      serve_para,
            "como_usar":       como_usar,
            "alertas":         alertas,
            "tarja":           tarja,
        }
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
        return {"encontrado": False, "erro": str(exc)}


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--input",  default=None)
    ap.add_argument("--output", default=None)
    args, _ = ap.parse_known_args()

    print("Iniciando sessão ANVISA (requests)...", file=sys.stderr, flush=True)
    session = _make_session()
    print("Sessão pronta.\n", file=sys.stderr, flush=True)

    if args.input and args.output:
        with open(args.input, encoding="utf-8") as fin:
            chaves = [l.strip() for l in fin if l.strip()]
        with open(args.output, "w", encoding="utf-8") as fout:
            for chave in chaves:
                dados = _buscar(chave, session)
                fout.write(json.dumps({"chave": chave, "dados": dados}, ensure_ascii=False) + "\n")
                fout.flush()
                time.sleep(0.8)  # respeita rate limit da ANVISA
    else:
        for line in sys.stdin:
            chave = line.strip()
            if not chave:
                continue
            dados = _buscar(chave, session)
            print(json.dumps({"chave": chave, "dados": dados}, ensure_ascii=False), flush=True)
            time.sleep(0.8)


if __name__ == "__main__":
    main()
