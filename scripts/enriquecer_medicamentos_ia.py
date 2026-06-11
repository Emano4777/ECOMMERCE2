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
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402

MODELO = "claude-haiku-4-5-20251001"
COMMIT_A_CADA = 100
DELAY_S = 0.5
EXPORTS_DIR = ROOT / "exports"

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
4. tarja_confianca = "alta" sempre que você reconhecer o INN e sua classificação ANVISA. Use "baixa" SOMENTE quando o INN for realmente desconhecido ou não registrado no Brasil.
5. Linguagem simples para o paciente. Sem jargão médico.
6. principais_cuidados: exatamente 3 bullets separados por \\n, cada um começando com "• ".

Classificação de tarja (RDC ANVISA vigente):
- "Sem tarja": MIPs — paracetamol, dipirona, ibuprofeno OTC (≤400mg), antitérmicos, antiácidos, vitaminas, fitoterápicos OTC, mucolíticos OTC (acebrofilina, ambroxol, guaifenesina, carbocisteína), anti-histamínicos OTC (loratadina, cetirizina OTC), antifúngicos tópicos OTC (miconazol creme, clotrimazol creme).
- "Tarja amarela": prescrição sem controle especial — antihipertensivos (atenolol, losartana, enalapril, anlodipina, valsartana, hidroclorotiazida), hipoglicemiantes (metformina, glibenclamida, glipizida), IBPs (omeprazol, pantoprazol, esomeprazol), estatinas (sinvastatina, atorvastatina), broncodilatadores (salbutamol, formoterol, salmeterol), corticoides sistêmicos (prednisona, dexametasona), anticoagulantes (varfarina, rivaroxabana), antidepressivos ISRS (sertralina, fluoxetina, escitalopram), antipsicóticos (haloperidol, risperidona), hormônios tireoidianos (levotiroxina).
- "Tarja vermelha sem retenção": prescrição sem retenção — AINEs de prescrição (aceclofenaco, cetoprofeno, meloxicam, nimesulida, diclofenaco), relaxantes musculares (ciclobenzaprina, carisoprodol), opioides fracos em associação (codeína+paracetamol, tramadol+paracetamol), corticoides tópicos potentes (mometasona, betametasona tópica), antifúngicos sistêmicos de uso curto (fluconazol 150mg dose única).
- "Tarja vermelha com retenção": antimicrobianos RDC 20/2011 — azitromicina, amoxicilina, amoxicilina+clavulanato, ampicilina, ciprofloxacino, levofloxacino, norfloxacino, cefalexina, cefadroxila, metronidazol, tinidazol, sulfametoxazol+trimetoprima, doxiciclina, tetraciclina, nitrofurantoína, claritromicina, eritromicina, clindamicina.
- "Tarja preta": psicotrópicos A1/A2/A3/B1/B2 — clonazepam, diazepam, alprazolam, lorazepam, bromazepam, clobazam, nitrazepam, zolpidem, zopiclona, midazolam, metilfenidato, lisdexanfetamina, anfepramona, femproporex, mazindol, bupropiona (quando anorexígeno); entorpecentes C2 — morfina, oxicodona, codeína isolada, tramadol isolado, fentanil, metadona; precursores — efedrina isolada. Exige Notificação de Receita A ou B.

Retorne exatamente este JSON:
{
  "inn_identificado": "<INN que você usou para classificar>",
  "tarja": "<uma das 5 opções acima, exatamente>",
  "tarja_confianca": "alta|baixa",
  "classificacao_farmacologica": "<grupo farmacológico, ex: AINE, IECA, benzodiazepínico>",
  "para_que_serve": "<máximo 3 linhas, linguagem simples>",
  "como_tomar": "<orientação geral, máximo 2 linhas>",
  "principais_cuidados": "• <cuidado 1>\\n• <cuidado 2>\\n• <cuidado 3>",
  "indicado_para": "adultos|crianças|ambos|gestantes com cautela"
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


def _chamar_claude(principio_ativo, nome_produto, laboratorio, tarja_atual, serve_para, api_key):
    """Chama Claude Haiku. Retorna (dict_parsed, None) ou (None, str_erro)."""
    inn_label = principio_ativo or "(não disponível — identifique pelo nome do produto)"
    user_content = (
        f"Princípio ativo: {inn_label}\n"
        f"Nome do produto: {nome_produto or 'desconhecido'}\n"
        f"Laboratório: {laboratorio or 'desconhecido'}\n"
        f"Tarja atual no sistema: {tarja_atual or 'não classificada'}\n"
        f"Informação atual (pode estar incompleta):\n{(serve_para or '')[:500]}"
    )
    body = json.dumps({
        "model": MODELO,
        "max_tokens": 700,
        "system": SYSTEM_PROMPT,
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


def _validar(parsed):
    if not isinstance(parsed, dict):
        return None
    if parsed.get("tarja") not in TARJAS_VALIDAS:
        return None
    if parsed.get("tarja_confianca") not in ("alta", "baixa"):
        return None
    return parsed


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


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Enriquece medicamentos via Claude IA.")
    parser.add_argument("--apply",  action="store_true", help="grava no banco")
    parser.add_argument("--force",  action="store_true", help="reprocessa já enriquecidos")
    parser.add_argument("--fase",   type=int, choices=[1, 2], help="1=só INN, 2=só sem INN")
    parser.add_argument("--inn",    help="filtra por princípio ativo (ex: DIPIRONA)")
    args = parser.parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("[ERRO] ANTHROPIC_API_KEY não definida.")
        sys.exit(1)

    conn = db()
    _garantir_schema(conn)
    cur = conn.cursor()

    stats   = {"alta": 0, "baixa": 0, "erro": 0, "gravados": 0}
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

        total_grupos = len(grupos)
        print(f"\n=== FASE 1: {total_grupos} grupos de INN únicos "
              f"(de {len(brutos)} variações brutas) ===\n")

        for i, (chave_norm, grupo) in enumerate(sorted(grupos.items()), 1):
            # Representante: linha com mais conteúdo em serve_para
            rep = max(grupo, key=lambda r: len(r.get("serve_para") or ""))
            inns_originais = [r["principio_ativo"] for r in grupo]
            n_prod = sum(r["n_produtos"] for r in grupo)
            label = chave_norm[:50] if chave_norm else inns_originais[0][:50]
            print(f"[{i:>4}/{total_grupos}] {label:<52} ({n_prod} prod) ", end="", flush=True)

            parsed, erro = _chamar_claude(
                chave_norm or rep["principio_ativo"],
                rep["nome_anvisa"], rep["laboratorio"],
                rep["tarja"], rep["serve_para"], api_key,
            )
            if parsed is None:
                print(f"ERRO: {erro}")
                stats["erro"] += 1
                time.sleep(DELAY_S)
                continue

            validado = _validar(parsed)
            if validado is None:
                print(f"INVÁLIDO: {json.dumps(parsed, ensure_ascii=False)[:80]}")
                stats["erro"] += 1
                time.sleep(DELAY_S)
                continue

            confianca = validado["tarja_confianca"]
            inn_usado = validado.get("inn_identificado") or chave_norm
            print(f"tarja={validado['tarja']!r:<38} confianca={confianca}")
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
        total_sem = len(sem_inn)
        print(f"\n=== FASE 2: {total_sem} produtos sem princípio ativo preenchido ===\n")

        for i, row in enumerate(sem_inn, 1):
            chave = row["chave"]
            nome  = row.get("nome_anvisa") or chave
            print(f"[{i:>4}/{total_sem}] {chave:<45} ", end="", flush=True)

            parsed, erro = _chamar_claude(
                None,  # principio_ativo desconhecido — Claude infere pelo nome
                nome, row["laboratorio"], row["tarja"], row["serve_para"], api_key,
            )
            if parsed is None:
                print(f"ERRO: {erro}")
                stats["erro"] += 1
                time.sleep(DELAY_S)
                continue

            validado = _validar(parsed)
            if validado is None:
                print(f"INVÁLIDO: {json.dumps(parsed, ensure_ascii=False)[:80]}")
                stats["erro"] += 1
                time.sleep(DELAY_S)
                continue

            confianca = validado["tarja_confianca"]
            inn_usado = validado.get("inn_identificado") or "?"
            print(f"tarja={validado['tarja']!r:<38} confianca={confianca}  inn={inn_usado}")
            stats[confianca] += 1

            if confianca == "baixa":
                baixa_csv.append({
                    "fase": 2,
                    "principio_ativo": inn_usado,
                    "nome_anvisa": nome,
                    "tarja_atual": row.get("tarja") or "",
                    "tarja_ia_sugerida": validado["tarja"],
                    "classificacao": validado.get("classificacao_farmacologica") or "",
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
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(baixa_csv[0].keys()))
            writer.writeheader()
            writer.writerows(baixa_csv)
        print(f"\nCSV baixa confiança: {csv_path}  ({len(baixa_csv)} registros)")

    # ── CSV discrepâncias ───────────────────────────────────────────────────
    if discrepancias:
        EXPORTS_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        disc_path = EXPORTS_DIR / f"tarja_discrepancias_{ts}.csv"
        with open(disc_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(discrepancias[0].keys()))
            writer.writeheader()
            writer.writerows(discrepancias)
        print(f"CSV discrepâncias  : {disc_path}  ({len(discrepancias)} registros)")

    # ── Relatório final ─────────────────────────────────────────────────────
    print(f"\n{'='*58}")
    print(f"Alta confiança    : {stats['alta']}")
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
