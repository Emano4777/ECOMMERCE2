#!/usr/bin/env python
"""
Enriquece anvisa_cache com classificação farmacológica via Claude IA.

Estratégia em duas fases:
  Fase 1 — classifica cada princípio ativo (INN) único uma vez e aplica em
            todos os produtos com aquele INN. Evita inconsistência e reduz
            chamadas à API em ~80%.
  Fase 2 — produtos sem principio_ativo preenchido: Claude infere o INN
            pelo nome e classifica individualmente.

Após gravar, exibe relatório de discrepâncias onde tarja_ia difere da tarja
original (flags para auditoria manual).

Uso:
    py scripts/enriquecer_medicamentos_ia.py              # dry-run (não grava)
    py scripts/enriquecer_medicamentos_ia.py --apply      # grava no banco
    py scripts/enriquecer_medicamentos_ia.py --apply --fase 1   # só INNs com principio_ativo
    py scripts/enriquecer_medicamentos_ia.py --apply --fase 2   # só sem principio_ativo
    py scripts/enriquecer_medicamentos_ia.py --apply --inn DIPIRONA
    py scripts/enriquecer_medicamentos_ia.py --apply --force    # reprocessa já enriquecidos
"""

import argparse
import csv
import json
import os
import re as _re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

MODELO = "claude-haiku-4-5-20251001"
COMMIT_A_CADA = 100
DELAY_S = 0.5
EXPORTS_DIR = ROOT / "exports"
CONFIANCAS_VALIDAS = {"alta", "media", "baixa"}
INDICADOS_VALIDOS = {"adultos", "crianças", "ambos", "gestantes com cautela"}
CHAVES_OBRIGATORIAS = {
    "inn_identificado",
    "tarja",
    "tarja_confianca",
    "classificacao_farmacologica",
    "para_que_serve",
    "como_tomar",
    "principais_cuidados",
    "indicado_para",
}


def db():
    database_url = (
        os.getenv("DATABASE_URL", "").strip()
        or os.getenv("DDATABASE_URL", "").strip()
    )
    if not database_url:
        raise RuntimeError("DATABASE_URL não configurada.")
    return psycopg2.connect(database_url, cursor_factory=RealDictCursor)

TARJAS_VALIDAS = {
    "Sem tarja",
    "Tarja amarela",
    "Tarja vermelha sem retenção",
    "Tarja vermelha com retenção",
    "Tarja preta",
}

# Mapa tarja_ia → valor legível para comparar com coluna `tarja` original
# (original usa: None, 'vermelha', 'preta')

# Termos de sal/forma que não alteram a classe farmacológica para fins de tarja
_SUFIXOS_SAL = _re.compile(
    r"\b(monoidratado?|anidro?|cloridrato|bromidrato|dicloridrato|hemitartarato|"
    r"hemifumarato|maleato|fumarato|succinato|besilato|tartarato|citrato|fosfato|"
    r"sódico?|potássico?|cálcico?|magnésico?|de)\b",
    _re.IGNORECASE,
)


def _normalizar_inn(raw):
    """
    Transforma a string principio_ativo em chave canônica para deduplicação:
      - Quebra por ',' ou ';'
      - Normaliza cada componente: lowercase, remove sufixos de sal, strip
      - Remove duplicatas
      - Ordena e junta com ' + '
    """
    if not raw:
        return ""
    partes = _re.split(r"[,;]+", raw)
    componentes = set()
    for p in partes:
        normalizado = _SUFIXOS_SAL.sub("", p).lower()
        normalizado = " ".join(normalizado.split())  # colapsa espaços
        if normalizado:
            componentes.add(normalizado)
    return " + ".join(sorted(componentes))


_TARJA_IA_PARA_ORIG = {
    "Sem tarja":                    None,
    "Tarja amarela":                None,        # amarela não existe na coluna original
    "Tarja vermelha sem retenção":  "vermelha",
    "Tarja vermelha com retenção":  "vermelha",
    "Tarja preta":                  "preta",
}

SYSTEM_PROMPT = """Você é um farmacêutico especialista em legislação sanitária brasileira e farmacologia clínica.

Regras obrigatórias:
1. Classifique a tarja com base EXCLUSIVAMENTE no princípio ativo (INN). Ignore sufixos de nome comercial (laboratório, forma farmacêutica, faixa etária) que não alteram o INN.
2. Se o princípio ativo não estiver disponível, identifique-o pelo nome do produto antes de classificar.
3. Retorne APENAS JSON válido — sem texto adicional, sem markdown, sem backticks.
4. tarja_confianca:
   - "alta": INN e regime de dispensação são inequívocos.
   - "media": INN reconhecido, mas faltam apresentação, concentração, via ou outro dado que possa alterar a orientação.
   - "baixa": INN desconhecido, ambíguo, insuficiente ou não registrado no Brasil.
5. Linguagem simples para o paciente. Sem jargão médico.
6. principais_cuidados: exatamente 3 bullets separados por \\n, cada um começando com "• ".
7. Nunca invente dose, frequência, duração ou número de comprimidos. Em "como_tomar", dê somente orientação geral e mande seguir bula/prescrição.
8. Não declare indicação por faixa etária sem segurança. Se apresentação ou concentração puder mudar a faixa etária, use confiança "media".
9. A tarja atual e a categoria atual são apenas referências potencialmente erradas.
10. "Tarja amarela" não é uma classe de dispensação inferível pelo princípio ativo. Não use essa opção apenas porque o produto é de prescrição.

Classificação operacional:
- "Sem tarja": MIPs — paracetamol, dipirona, ibuprofeno OTC (≤400mg), antitérmicos, antiácidos, vitaminas, fitoterápicos OTC, mucolíticos OTC (acebrofilina, ambroxol, guaifenesina, carbocisteína), anti-histamínicos OTC (loratadina, cetirizina OTC), antifúngicos tópicos OTC (miconazol creme, clotrimazol creme).
- "Tarja amarela": use somente quando houver evidência explícita na entrada de que esse é o rótulo operacional desejado; nunca infira pelo INN.
- "Tarja vermelha sem retenção": medicamentos sob prescrição sem retenção, incluindo anti-hipertensivos, hipoglicemiantes, estatinas, hormônios tireoidianos e AINEs de prescrição.
- "Tarja vermelha com retenção": antimicrobianos RDC 20/2011 — azitromicina, amoxicilina, amoxicilina+clavulanato, ampicilina, ciprofloxacino, levofloxacino, norfloxacino, cefalexina, cefadroxila, metronidazol, tinidazol, sulfametoxazol+trimetoprima, doxiciclina, tetraciclina, nitrofurantoína, claritromicina, eritromicina, clindamicina.
- "Tarja preta": medicamentos sujeitos a notificação de receita A ou B. Não informe a lista regulatória específica se não tiver certeza.

Retorne exatamente este JSON:
{
  "inn_identificado": "<INN que você usou para classificar>",
  "tarja": "<escolha exatamente um valor: Sem tarja, Tarja amarela, Tarja vermelha sem retenção, Tarja vermelha com retenção ou Tarja preta>",
  "tarja_confianca": "<escolha exatamente um valor: alta, media ou baixa>",
  "classificacao_farmacologica": "<grupo farmacológico, ex: AINE, IECA, benzodiazepínico>",
  "para_que_serve": "<máximo 3 linhas, linguagem simples>",
  "como_tomar": "<orientação geral, máximo 2 linhas>",
  "principais_cuidados": "• <cuidado 1>\\n• <cuidado 2>\\n• <cuidado 3>",
  "indicado_para": "<escolha exatamente um valor: adultos, crianças, ambos ou gestantes com cautela>"
}"""

AUDITOR_PROMPT = """Você é o revisor farmacêutico de uma classificação gerada por outra IA.

Revise de forma adversarial e retorne APENAS JSON válido, sem markdown.

Reprove ou corrija quando houver:
- tarja inferida pelo nome comercial em vez do princípio ativo;
- "Tarja amarela" usada como sinônimo de prescrição simples;
- dose, frequência, duração ou número de unidades inventados sem apresentação e concentração;
- indicação, faixa etária ou cuidado incompatível com os dados;
- afirmação regulatória específica duvidosa;
- contradição entre princípio ativo, classificação e finalidade;
- campos fora dos enums exigidos.

Se faltarem apresentação, concentração ou via, "como_tomar" deve ser genérico:
"Use somente conforme a bula e a orientação do médico ou farmacêutico."

Retorne:
{
  "aprovado": true|false,
  "confianca_final": "<escolha exatamente um valor: alta, media ou baixa>",
  "motivos": ["motivo curto"],
  "resposta_corrigida": {
    "inn_identificado": "...",
    "tarja": "<escolha exatamente um valor: Sem tarja, Tarja amarela, Tarja vermelha sem retenção, Tarja vermelha com retenção ou Tarja preta>",
    "tarja_confianca": "<escolha exatamente um valor: alta, media ou baixa>",
    "classificacao_farmacologica": "...",
    "para_que_serve": "...",
    "como_tomar": "...",
    "principais_cuidados": "• ...\\n• ...\\n• ...",
    "indicado_para": "<escolha exatamente um valor: adultos, crianças, ambos ou gestantes com cautela>"
  }
}"""


# ─── helpers ──────────────────────────────────────────────────────────────────

def _garantir_schema(conn):
    cur = conn.cursor()
    for col in [
        "tarja_ia TEXT",
        "tarja_ia_confianca TEXT",
        "classificacao_ia TEXT",
        "para_que_serve_ia TEXT",
        "como_tomar_ia TEXT",
        "principais_cuidados_ia TEXT",
        "indicado_para_ia TEXT",
    ]:
        cur.execute(f"ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS {col}")
    conn.commit()
    cur.close()


def _post_claude(system_prompt, user_content, api_key, max_tokens=700):
    """Executa uma chamada JSON ao Claude."""
    body = json.dumps({
        "model": MODELO,
        "max_tokens": max_tokens,
        "temperature": 0,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = "".join(
            p.get("text", "") for p in data.get("content", []) if p.get("type") == "text"
        ).strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:].strip()
        return json.loads(raw), None
    except (urllib.error.URLError, TimeoutError) as e:
        return None, f"network_error: {e}"
    except json.JSONDecodeError as e:
        return None, f"json_decode_error: {e}"
    except Exception as e:
        return None, f"error: {e}"


def _montar_entrada(principio_ativo, nome_produto, laboratorio, tarja_atual,
                    serve_para, categoria_atual=None):
    inn_label = principio_ativo or "(não disponível — identifique pelo nome do produto)"
    return (
        f"Princípio ativo: {inn_label}\n"
        f"Nome do produto: {nome_produto or 'desconhecido'}\n"
        f"Laboratório: {laboratorio or 'desconhecido'}\n"
        f"Tarja atual no sistema: {tarja_atual or 'não classificada'}\n"
        f"Categoria atual: {categoria_atual or 'não disponível'}\n"
        f"Informação atual (pode estar incompleta):\n{(serve_para or '')[:500]}"
    )


def _chamar_claude(principio_ativo, nome_produto, laboratorio, tarja_atual,
                   serve_para, api_key, categoria_atual=None):
    """Chama Claude Haiku. Retorna (dict_parsed, None) ou (None, str_erro)."""
    user_content = _montar_entrada(
        principio_ativo, nome_produto, laboratorio, tarja_atual,
        serve_para, categoria_atual,
    )
    return _post_claude(SYSTEM_PROMPT, user_content, api_key)


def _auditar_com_claude(entrada, candidato, api_key):
    """Segunda opinião independente; retorna resposta corrigida e motivos."""
    user_content = (
        f"ENTRADA ORIGINAL:\n{entrada}\n\n"
        "RESPOSTA CANDIDATA:\n"
        f"{json.dumps(candidato, ensure_ascii=False)}"
    )
    revisao, erro = _post_claude(AUDITOR_PROMPT, user_content, api_key, max_tokens=900)
    if erro:
        return None, [], erro
    if not isinstance(revisao, dict):
        return None, [], "auditoria_invalida"
    corrigida = revisao.get("resposta_corrigida")
    if not isinstance(corrigida, dict):
        return None, revisao.get("motivos") or [], "auditoria_sem_resposta_corrigida"
    corrigida["tarja_confianca"] = revisao.get("confianca_final", "baixa")
    return corrigida, revisao.get("motivos") or [], None


def _reparar_com_claude(entrada, resposta, erros, api_key):
    prompt = """Corrija um JSON farmacêutico que falhou em regras formais.
Retorne APENAS o JSON corrigido, sem markdown e sem explicações.
Preserve a tarja quando ela não estiver entre os erros.
Nunca inclua dose, frequência, duração ou número de unidades em como_tomar.
principais_cuidados deve ser uma string com exatamente 3 linhas começando por "• ".
Escolha exatamente UM valor de cada lista:
- tarja: "Sem tarja", "Tarja amarela", "Tarja vermelha sem retenção", "Tarja vermelha com retenção", "Tarja preta".
- tarja_confianca: "alta", "media", "baixa".
- indicado_para: "adultos", "crianças", "ambos", "gestantes com cautela".
Nunca devolva a lista inteira, opções separadas por |, explicações ou texto adicional no valor."""
    user_content = (
        f"ENTRADA ORIGINAL:\n{entrada}\n\n"
        f"ERROS OBRIGATÓRIOS A CORRIGIR:\n- " + "\n- ".join(erros) + "\n\n"
        f"JSON A CORRIGIR:\n{json.dumps(resposta, ensure_ascii=False)}"
    )
    return _post_claude(prompt, user_content, api_key, max_tokens=800)


def _normalizar_valor_enum(valor):
    if not isinstance(valor, str):
        return valor
    return " ".join(valor.strip().split()).casefold()


def _normalizar_texto_comparacao(valor):
    texto = unicodedata.normalize("NFD", str(valor or "").casefold())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return " ".join(_re.findall(r"[a-z0-9]+", texto))


def _inn_sustentado_pelo_nome(inn, nome_produto):
    """Evita aceitar INN inventado quando o princípio ativo não veio do banco."""
    inn_tokens = {
        token for token in _normalizar_texto_comparacao(inn).split()
        if len(token) >= 5
    }
    nome_tokens = set(_normalizar_texto_comparacao(nome_produto).split())
    return bool(inn_tokens & nome_tokens)


def _canonicalizar_enums(parsed):
    """Corrige somente diferenças inequívocas de caixa e espaços."""
    if not isinstance(parsed, dict):
        return parsed
    resultado = dict(parsed)
    mapas = {
        "tarja": {_normalizar_valor_enum(v): v for v in TARJAS_VALIDAS},
        "tarja_confianca": {
            _normalizar_valor_enum(v): v for v in CONFIANCAS_VALIDAS
        },
        "indicado_para": {
            _normalizar_valor_enum(v): v for v in INDICADOS_VALIDOS
        },
    }
    for campo, mapa in mapas.items():
        normalizado = _normalizar_valor_enum(resultado.get(campo))
        if normalizado in mapa:
            resultado[campo] = mapa[normalizado]
    return resultado


def _erros_validacao(parsed):
    erros = []
    if not isinstance(parsed, dict):
        return ["resposta não é objeto JSON"]
    if not CHAVES_OBRIGATORIAS.issubset(parsed):
        ausentes = sorted(CHAVES_OBRIGATORIAS - set(parsed))
        erros.append(f"campos ausentes: {', '.join(ausentes)}")
    if parsed.get("tarja") not in TARJAS_VALIDAS:
        erros.append(f"tarja fora do enum: {parsed.get('tarja')!r}")
    if parsed.get("tarja_confianca") not in CONFIANCAS_VALIDAS:
        erros.append(
            f"tarja_confianca fora do enum: {parsed.get('tarja_confianca')!r}"
        )
    if parsed.get("indicado_para") not in INDICADOS_VALIDOS:
        erros.append(f"indicado_para fora do enum: {parsed.get('indicado_para')!r}")
    for campo in CHAVES_OBRIGATORIAS:
        if not isinstance(parsed.get(campo), str) or not parsed[campo].strip():
            erros.append(f"{campo} vazio ou não textual")
    cuidados = parsed.get("principais_cuidados")
    if isinstance(cuidados, str):
        linhas = cuidados.splitlines()
        if len(linhas) != 3 or any(not item.startswith("• ") for item in linhas):
            erros.append("principais_cuidados deve conter exatamente 3 bullets")
    como_tomar = parsed.get("como_tomar") or ""
    padrao_dose = _re.compile(
        r"\b\d+(?:[.,]\d+)?\s*(?:mg|ml|comprimidos?|cápsulas?|gotas?)\b|"
        r"\ba cada\s+\d+|\b\d+\s*(?:x|vezes)\s+(?:ao|por)\s+dia\b",
        _re.IGNORECASE,
    )
    if padrao_dose.search(como_tomar):
        erros.append("como_tomar contém dose ou frequência específica")
    return erros


def _validar(parsed):
    return parsed if not _erros_validacao(parsed) else None


def _classificar_e_auditar(principio_ativo, nome_produto, laboratorio,
                           tarja_atual, serve_para, api_key,
                           categoria_atual=None):
    entrada = _montar_entrada(
        principio_ativo, nome_produto, laboratorio, tarja_atual,
        serve_para, categoria_atual,
    )
    candidato, erro = _chamar_claude(
        principio_ativo, nome_produto, laboratorio, tarja_atual,
        serve_para, api_key, categoria_atual,
    )
    if erro:
        return None, [], erro
    if not isinstance(candidato, dict):
        return None, [], "classificacao_invalida"
    candidato = _canonicalizar_enums(candidato)

    time.sleep(DELAY_S)
    revisado, motivos, erro = _auditar_com_claude(entrada, candidato, api_key)
    if erro:
        return None, motivos, erro
    revisado = _canonicalizar_enums(revisado)
    tarjas_divergentes = (
        candidato.get("tarja") in TARJAS_VALIDAS
        and revisado.get("tarja") in TARJAS_VALIDAS
        and candidato["tarja"] != revisado["tarja"]
    )
    if tarjas_divergentes:
        motivos = list(motivos) + [
            f"Classificador sugeriu {candidato['tarja']} e auditor sugeriu "
            f"{revisado['tarja']}; revisão manual obrigatória."
        ]
        revisado["tarja_confianca"] = "baixa"
    erros_locais = _erros_validacao(revisado)
    if erros_locais:
        time.sleep(DELAY_S)
        reparado, erro = _reparar_com_claude(
            entrada, revisado, erros_locais, api_key,
        )
        if erro:
            return None, motivos, f"falha_no_reparo: {erro}"
        revisado = _canonicalizar_enums(reparado)
        if tarjas_divergentes:
            revisado["tarja_confianca"] = "baixa"
        erros_locais = _erros_validacao(revisado)
    validado = _validar(revisado)
    if validado is None:
        detalhes = "; ".join(erros_locais)
        return None, motivos, f"resposta_reparada_reprovada: {detalhes}"
    if not principio_ativo and not _inn_sustentado_pelo_nome(
        validado.get("inn_identificado"), nome_produto,
    ):
        validado["tarja_confianca"] = "baixa"
        motivos = list(motivos) + [
            "Princípio ativo ausente no banco e INN inferido não aparece "
            "claramente no nome do produto; revisão manual obrigatória."
        ]
    return validado, motivos, None


def _gravar(cur, chaves, por_inn, validado, apply):
    """
    UPDATE anvisa_cache.
    chaves: lista de valores para filtrar (principio_ativo ou chave).
    por_inn=True usa IN (principio_ativo), False usa chave = único valor.
    """
    if not apply or not chaves:
        return
    vals = (
        validado["tarja"],
        validado["tarja_confianca"],
        validado.get("classificacao_farmacologica") or "",
        validado.get("para_que_serve") or "",
        validado.get("como_tomar") or "",
        validado.get("principais_cuidados") or "",
        validado.get("indicado_para") or "",
    )
    if por_inn:
        cur.execute(
            """
            UPDATE anvisa_cache SET
                tarja_ia               = %s,
                tarja_ia_confianca     = %s,
                classificacao_ia       = %s,
                para_que_serve_ia      = %s,
                como_tomar_ia          = %s,
                principais_cuidados_ia = %s,
                indicado_para_ia       = %s
            WHERE principio_ativo = ANY(%s)
            """,
            (*vals, list(chaves)),
        )
    else:
        cur.execute(
            """
            UPDATE anvisa_cache SET
                tarja_ia               = %s,
                tarja_ia_confianca     = %s,
                classificacao_ia       = %s,
                para_que_serve_ia      = %s,
                como_tomar_ia          = %s,
                principais_cuidados_ia = %s,
                indicado_para_ia       = %s
            WHERE chave = %s
            """,
            (*vals, chaves[0]),
        )


def _escrever_csv(caminho, registros):
    """Escreve registros com chaves diferentes sem perder colunas."""
    if not registros:
        return
    campos = []
    vistos = set()
    for registro in registros:
        for campo in registro:
            if campo not in vistos:
                vistos.add(campo)
                campos.append(campo)
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=campos, restval="")
        writer.writeheader()
        writer.writerows(registros)


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Enriquece medicamentos via Claude IA.")
    parser.add_argument("--apply",  action="store_true", help="grava no banco")
    parser.add_argument("--force",  action="store_true", help="reprocessa já enriquecidos")
    parser.add_argument("--fase",   type=int, choices=[1, 2], help="1=só INN, 2=só sem INN")
    parser.add_argument("--inn",    help="filtra por princípio ativo (ex: DIPIRONA)")
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="máximo de grupos/produtos por execução (padrão: 100; 0=todos)",
    )
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit deve ser zero ou positivo")

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("[ERRO] ANTHROPIC_API_KEY não definida.")
        sys.exit(1)

    conn = db()
    if args.apply:
        _garantir_schema(conn)
    cur = conn.cursor()

    stats   = {"alta": 0, "media": 0, "baixa": 0, "erro": 0, "gravados": 0}
    baixa_csv = []
    discrepancias = []

    # ── FASE 1: INNs únicos (agrupados por INN normalizado) ─────────────────
    if args.fase in (None, 1):
        inn_where = ["encontrado = TRUE", "principio_ativo IS NOT NULL", "principio_ativo <> ''"]
        inn_params = []
        if args.inn:
            inn_where.append("principio_ativo ILIKE %s")
            inn_params.append(f"%{args.inn}%")

        # Busca um representante por principio_ativo bruto; agrupamento
        # real (por INN normalizado) acontece em Python logo abaixo.
        cur.execute(
            f"""
            SELECT principio_ativo,
                   MAX(nome_anvisa)  AS nome_anvisa,
                   MAX(laboratorio)  AS laboratorio,
                   MAX(tarja)        AS tarja,
                   MAX(serve_para)   AS serve_para,
                   COUNT(*)          AS n_produtos,
                   BOOL_OR(tarja_ia IS NOT NULL) AS ja_processado
            FROM anvisa_cache
            WHERE {' AND '.join(inn_where)}
            GROUP BY principio_ativo
            ORDER BY principio_ativo
            """,
            inn_params,
        )
        brutos = [dict(r) for r in cur.fetchall()]

        # Agrupa strings brutas pela chave normalizada
        grupos: dict[str, list] = defaultdict(list)
        for row in brutos:
            chave_norm = _normalizar_inn(row["principio_ativo"])
            grupos[chave_norm].append(row)

        # Filtra grupos já processados (a menos que --force)
        if not args.force:
            grupos = {k: v for k, v in grupos.items()
                      if not all(r["ja_processado"] for r in v)}

        itens_grupo = sorted(grupos.items())
        if args.limit:
            itens_grupo = itens_grupo[:args.limit]
        total_grupos = len(itens_grupo)
        print(f"\n=== FASE 1: {total_grupos} grupos de INN únicos "
              f"(de {len(brutos)} variações brutas) ===\n")

        for i, (chave_norm, grupo) in enumerate(itens_grupo, 1):
            # Representante: linha com mais conteúdo em serve_para
            rep = max(grupo, key=lambda r: len(r.get("serve_para") or ""))
            inns_originais = [r["principio_ativo"] for r in grupo]
            n_prod = sum(r["n_produtos"] for r in grupo)
            label = chave_norm[:50] if chave_norm else inns_originais[0][:50]
            print(f"[{i:>4}/{total_grupos}] {label:<52} ({n_prod} prod) ", end="", flush=True)

            validado, motivos_auditoria, erro = _classificar_e_auditar(
                chave_norm or rep["principio_ativo"],
                rep["nome_anvisa"], rep["laboratorio"],
                rep["tarja"], rep["serve_para"], api_key,
            )
            if validado is None:
                print(f"ERRO: {erro}")
                stats["erro"] += 1
                time.sleep(DELAY_S)
                continue

            confianca = validado["tarja_confianca"]
            inn_usado = validado.get("inn_identificado") or chave_norm
            motivo_log = "; ".join(str(m) for m in motivos_auditoria[:2])
            print(f"tarja={validado['tarja']!r:<38} confianca={confianca}"
                  f"{f'  auditoria={motivo_log}' if motivo_log else ''}")
            stats[confianca] += 1

            if confianca == "baixa":
                baixa_csv.append({
                    "fase": 1,
                    "inn_normalizado": chave_norm,
                    "inns_originais": "; ".join(inns_originais),
                    "nome_anvisa": rep.get("nome_anvisa") or "",
                    "tarja_atual": rep.get("tarja") or "",
                    "tarja_ia_sugerida": validado["tarja"],
                    "classificacao": validado.get("classificacao_farmacologica") or "",
                    "motivos_auditoria": "; ".join(str(m) for m in motivos_auditoria),
                    "n_produtos_afetados": n_prod,
                })
            else:
                # Atualiza TODOS os INNs originais do grupo de uma vez
                _gravar(cur, inns_originais, por_inn=True, validado=validado, apply=args.apply)
                stats["gravados"] += n_prod
                orig = (rep.get("tarja") or "").strip().lower() or None
                esperado = _TARJA_IA_PARA_ORIG.get(validado["tarja"])
                if orig and orig != esperado:
                    discrepancias.append({
                        "inn_normalizado": chave_norm,
                        "tarja_original": orig,
                        "tarja_ia": validado["tarja"],
                        "n_produtos": n_prod,
                    })

            if i % COMMIT_A_CADA == 0 and args.apply:
                conn.commit()
                print(f"  → commit ({i}/{total_grupos})")

            time.sleep(DELAY_S)

        if args.apply:
            conn.commit()

    # ── FASE 2: sem principio_ativo ─────────────────────────────────────────
    if args.fase in (None, 2):
        sem_inn_where = [
            "encontrado = TRUE",
            "(principio_ativo IS NULL OR principio_ativo = '')",
            "(nome_anvisa IS NOT NULL OR chave IS NOT NULL)",
        ]
        sem_inn_params = []
        if not args.force:
            sem_inn_where.append("tarja_ia IS NULL")
        if args.inn:
            sem_inn_where.append("chave ILIKE %s")
            sem_inn_params.append(f"%{args.inn}%")

        cur.execute(
            f"""
            SELECT chave, nome_anvisa, laboratorio, tarja, serve_para
            FROM anvisa_cache
            WHERE {' AND '.join(sem_inn_where)}
            ORDER BY chave
            """,
            sem_inn_params,
        )
        sem_inn = [dict(r) for r in cur.fetchall()]
        if args.limit:
            sem_inn = sem_inn[:args.limit]
        total_sem = len(sem_inn)
        print(f"\n=== FASE 2: {total_sem} produtos sem princípio ativo preenchido ===\n")

        for i, row in enumerate(sem_inn, 1):
            chave = row["chave"]
            nome  = row.get("nome_anvisa") or chave
            print(f"[{i:>4}/{total_sem}] {chave:<45} ", end="", flush=True)

            validado, motivos_auditoria, erro = _classificar_e_auditar(
                None,  # principio_ativo desconhecido — Claude infere pelo nome
                nome, row["laboratorio"], row["tarja"], row["serve_para"], api_key,
            )
            if validado is None:
                print(f"ERRO: {erro}")
                stats["erro"] += 1
                time.sleep(DELAY_S)
                continue

            confianca = validado["tarja_confianca"]
            inn_usado = validado.get("inn_identificado") or "?"
            motivo_log = "; ".join(str(m) for m in motivos_auditoria[:2])
            print(f"tarja={validado['tarja']!r:<38} confianca={confianca}  inn={inn_usado}"
                  f"{f'  auditoria={motivo_log}' if motivo_log else ''}")
            stats[confianca] += 1

            if confianca == "baixa":
                baixa_csv.append({
                    "fase": 2,
                    "principio_ativo": inn_usado,
                    "nome_anvisa": nome,
                    "tarja_atual": row.get("tarja") or "",
                    "tarja_ia_sugerida": validado["tarja"],
                    "classificacao": validado.get("classificacao_farmacologica") or "",
                    "motivos_auditoria": "; ".join(str(m) for m in motivos_auditoria),
                    "n_produtos_afetados": 1,
                })
            else:
                _gravar(cur, [chave], por_inn=False, validado=validado, apply=args.apply)
                stats["gravados"] += 1
                orig = (row.get("tarja") or "").strip().lower() or None
                esperado = _TARJA_IA_PARA_ORIG.get(validado["tarja"])
                if orig and orig != esperado:
                    discrepancias.append({
                        "principio_ativo": inn_usado,
                        "tarja_original": orig,
                        "tarja_ia": validado["tarja"],
                        "n_produtos": 1,
                    })

            if i % COMMIT_A_CADA == 0 and args.apply:
                conn.commit()
                print(f"  → commit ({i}/{total_sem})")

            time.sleep(DELAY_S)

        if args.apply:
            conn.commit()

    cur.close()
    conn.close()

    # ── CSV baixa confiança ─────────────────────────────────────────────────
    if baixa_csv:
        EXPORTS_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        csv_path = EXPORTS_DIR / f"tarja_baixa_confianca_{ts}.csv"
        _escrever_csv(csv_path, baixa_csv)
        print(f"\nCSV baixa confiança: {csv_path}  ({len(baixa_csv)} registros)")

    # ── CSV discrepâncias ───────────────────────────────────────────────────
    if discrepancias:
        EXPORTS_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        disc_path = EXPORTS_DIR / f"tarja_discrepancias_{ts}.csv"
        _escrever_csv(disc_path, discrepancias)
        print(f"CSV discrepâncias  : {disc_path}  ({len(discrepancias)} registros)")

    # ── Relatório final ─────────────────────────────────────────────────────
    print(f"\n{'='*58}")
    print(f"Alta confiança    : {stats['alta']}")
    print(f"Média confiança   : {stats['media']}")
    print(f"Baixa confiança   : {stats['baixa']}  (ver CSV acima)")
    print(f"Erros             : {stats['erro']}")
    if args.apply:
        print(f"Linhas gravadas   : {stats['gravados']}  (produtos atualizados)")
    else:
        print("Modo dry-run — nenhuma alteração gravada no banco.")
    if discrepancias:
        print(f"Discrepâncias     : {len(discrepancias)}  (tarja_ia ≠ tarja original — ver CSV)")
    print("="*58)


if __name__ == "__main__":
    main()
