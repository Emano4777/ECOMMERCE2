"""
Auditoria de imagens liberadas para medicamentos ativos do catalogo Alpha.

1. Aplica a MESMA logica corrigida de _marcar_tarja_batch/produto_detalhe
   (tarja vermelha/preta sempre bloqueia; exibir_imagem_publica=True nunca
   sobrepoe tarja conhecida; fallback conservador por principio ativo).
2. Para os que MESMO ASSIM continuam sem sinal de bloqueio nos dados
   estruturados (ex.: nenhum fabricante conhecido tem tarja cadastrada),
   manda a imagem pro Claude com visao e pergunta se a propria foto mostra
   tarja vermelha/preta, texto de "venda sob prescricao" ou logo de outra
   rede de farmacia.
3. Qualquer resposta positiva grava um registro conservador no anvisa_cache
   (exibir_imagem_publica=False, tarja='vermelha' se desconhecida) pra que
   o app passe a bloquear a imagem real automaticamente.

Uso:
    python scripts/auditar_imagens_liberadas_visual.py --dry-run
    python scripts/auditar_imagens_liberadas_visual.py --limite 20
    python scripts/auditar_imagens_liberadas_visual.py

Nao apaga nem sobrescreve nada que ja estava mais restritivo.
"""
import os
import re
import json
import base64
import argparse
import ssl
import time
import urllib.request

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

DATABASE_URL = os.environ["DATABASE_URL"]
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

GENERIC_TARJA_VERMELHA_IMG = "https://res.cloudinary.com/dizfq460q/image/upload/v1778783063/CAIXA_GEN%C3%89RICO_-_POUPAQUI_itiyth.jpg"
GENERIC_TARJA_PRETA_IMG = "https://res.cloudinary.com/dizfq460q/image/upload/v1778783450/ChatGPT_Image_14_de_mai._de_2026_15_30_35_wuovpb.png"
_MEDICINE_PLACEHOLDER_URLS = {GENERIC_TARJA_VERMELHA_IMG, GENERIC_TARJA_PRETA_IMG}

# Mesma lista de _ANVISA_STOP_WORDS do app.py — manter em sincronia.
_ANVISA_STOP_WORDS = {
    "com", "de", "do", "da", "dos", "das", "para", "por", "em", "e", "ou",
    "mg", "mcg", "ml", "ui", "gr", "cp", "caps", "comp", "tab", "un", "und",
    "sol", "solucao", "injetavel", "oral", "topico", "cutaneo", "subl",
    "cpr", "drg", "amp", "fco", "bsa", "gel", "crem", "pom", "sup", "xpe",
    "susp", "solu", "gota", "gotas", "soln", "inj",
    "rev", "retard", "ret", "iny", "inf", "efervescente", "spray",
    "comprimido", "comprimidos", "capsula", "capsulas", "softgel", "gelcap",
    "dragea", "drageias", "xarope", "pomada", "creme", "supositorio",
    "injecao", "suspensao", "emulsao", "granulado",
    "pastilha", "pastilhas", "sublingual", "transdermico", "inalacao",
    "revestido", "revestidos", "liberacao", "prolongada", "retardada",
    "mastigavel", "dispersivel", "orodisp", "orodispersivel",
    "frasco", "frascos", "litro", "litros", "copo", "copinho", "dosador", "medidor",
    "conta", "seringa", "caneta", "nebulizador", "inalador", "vaporizador",
    "generico", "generica", "similar", "bioequivalente",
    "cloridrato", "bromidrato", "dicloridrato", "hemitartarato", "hemifumarato",
    "maleato", "fumarato", "succinato", "besilato", "tartarato",
    "monoidratado", "monoidratada", "hemif", "succ",
    "clor", "dclor", "brom", "sulf", "malt", "fum", "besil", "tart",
    "hidroclor", "medoxomila", "flacodin",
    "germed", "vitamedic", "biolab", "globo", "greenbios", "uniphar",
    "farmax", "quimica", "bellaphytus", "rioquimica", "medley", "sandoz",
    "torrent", "teuto", "eurofarma", "prati", "donaduzzi", "neo", "geolab",
    "pharlab", "pharma", "laboratorio", "laboratorios",
    "natulab", "multilab", "airela", "pharmascience", "biosintetica",
}

_TIPOS_NAO_MEDICAMENTO = {"suplemento", "perfumaria", "dermocosmetico", "nutricao", "outro", "cosmetico", "higiene", "varejo"}


def anvisa_chave(nome):
    tks = re.sub(r"[^\w\s]", " ", nome or "").upper().split()
    words = []
    for w in tks:
        if w.lower() in _ANVISA_STOP_WORDS or any(c.isdigit() for c in w) or len(w) < 4:
            continue
        words.append(w)
        if len(words) >= 2:
            break
    return " ".join(words)


def restritividade(row):
    if row.get("exibir_imagem_publica") is False:
        return 2
    if row.get("exibir_imagem_publica") is None and (row.get("tarja") or "").strip().lower() in ("preta", "vermelha"):
        return 1
    return 0


def bloqueado_por_dados(candidatos):
    if not candidatos:
        return False, None
    melhor = max(candidatos, key=restritividade)
    return restritividade(melhor) >= 1, melhor


def claude_vision_check(image_url, nome, timeout=25):
    if not ANTHROPIC_API_KEY:
        return None
    try:
        req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            img_bytes = r.read()
            content_type = r.headers.get("Content-Type", "image/jpeg")
    except Exception as e:
        return {"erro": f"download_falhou: {e}"}

    media_type = content_type if content_type.startswith("image/") else "image/jpeg"
    img_b64 = base64.b64encode(img_bytes).decode("ascii")

    prompt = (
        "Esta e uma foto de produto de farmacia brasileira, catalogo de e-commerce. "
        f"Nome do produto no catalogo: {nome}\n"
        "Responda SOMENTE com JSON valido, sem markdown:\n"
        '{"tarja_vermelha_ou_preta_visivel": true/false, '
        '"texto_prescricao_visivel": true/false, '
        '"farmacia_concorrente_visivel": true/false, '
        '"nome_farmacia_concorrente": "nome ou null", '
        '"confianca": "alta/media/baixa"}\n'
        "REGRAS:\n"
        "- tarja_vermelha_ou_preta_visivel: true se a embalagem mostra uma faixa/tarja vermelha ou preta (padrao ANVISA de medicamento controlado/prescricao)\n"
        "- texto_prescricao_visivel: true se aparece texto tipo 'venda sob prescricao medica', 'so pode ser vendido com retencao de receita', 'uso restrito'\n"
        "- farmacia_concorrente_visivel: true se aparece logo/marca de QUALQUER rede de farmacia (Pague Menos, Droga Raia, Drogasil, Panvel, etc) que NAO seja generica/neutra\n"
        "- Se a imagem nao carregar ou nao for embalagem de medicamento, retorne tudo false com confianca baixa"
    )
    payload = json.dumps({
        "model": "claude-sonnet-5",
        "max_tokens": 250,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
            {"type": "text", "text": prompt},
        ]}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
        method="POST",
    )
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = (data.get("content") or [{}])[0].get("text", "").strip()
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
        return json.loads(text)
    except Exception as e:
        return {"erro": str(e)}


def main():
    ap = argparse.ArgumentParser(description="Auditoria visual de imagens de medicamentos do catalogo Alpha")
    ap.add_argument("--limite", type=int, default=0, help="limita quantos EANs verificar visualmente (0 = sem limite)")
    ap.add_argument("--dry-run", action="store_true", help="nao grava nada, so reporta")
    args = ap.parse_args()

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT ON (LTRIM(COALESCE(ap.ean,''),'0'))
               LTRIM(COALESCE(ap.ean,''),'0') AS ean_key, ap.ean, ap.nome, ap.classificacao,
               COALESCE(m.tipo_ia, cls.tipo) AS tipo_conhecido,
               COALESCE(epi.imagem_url, ap.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem),'')) AS imagem
        FROM ecommerce_alpha_produtos ap
        LEFT JOIN medicamentos m ON LTRIM(COALESCE(m.barra_norm,m.barra,''),'0') = LTRIM(COALESCE(ap.ean,''),'0')
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        LEFT JOIN produto_canon pc ON LTRIM(COALESCE(pc.ean,''),'0') = LTRIM(COALESCE(ap.ean,''),'0')
                                   AND pc.fonte NOT IN ('cosmos_miss','ia_miss','placeholder_broken')
        LEFT JOIN ecommerce_classificacao_ean cls ON cls.ean = LTRIM(COALESCE(ap.ean,''),'0')
        LEFT JOIN ecommerce_produto_imagens epi ON LTRIM(COALESCE(epi.ean,''),'0') = LTRIM(COALESCE(ap.ean,''),'0')
        WHERE COALESCE(ap.inativo,false) = false AND COALESCE(ap.estoque,0) > 0
        ORDER BY LTRIM(COALESCE(ap.ean,''),'0'), ap.estoque DESC
    """)
    produtos = [dict(r) for r in cur.fetchall()]
    print(f"Total EANs ativos distintos: {len(produtos)}")

    candidatos = []
    for p in produtos:
        tipo = (p.get("tipo_conhecido") or "").strip().lower()
        if tipo in _TIPOS_NAO_MEDICAMENTO:
            continue
        classif = (p.get("classificacao") or "").upper()
        eh_medicamento_alpha = any(x in classif for x in ("GENERICO", "SIMILAR", "ETICO", "REFERENCIA", "MEDICAMENTOS", "MARCA"))
        ch = anvisa_chave(p.get("nome") or "")
        parece_medicamento = bool(re.search(r"\d+\s*(mg|mcg|ml|ui|g)\b", (p.get("nome") or "").lower()))
        if not (tipo and tipo not in _TIPOS_NAO_MEDICAMENTO) and not eh_medicamento_alpha and not parece_medicamento:
            continue
        if not ch:
            continue
        img = (p.get("imagem") or "").strip()
        if not img or img in _MEDICINE_PLACEHOLDER_URLS:
            continue
        candidatos.append({**p, "chave": ch, "imagem": img})

    print(f"Candidatos medicamento com imagem real exposta: {len(candidatos)}")

    first_words = sorted({c["chave"].split(" ", 1)[0] for c in candidatos})
    exact_chaves = sorted({c["chave"] for c in candidatos})
    cur.execute(
        "SELECT chave, tarja, receita_retida, exibir_imagem_publica FROM anvisa_cache "
        "WHERE (SPLIT_PART(chave,' ',1) = ANY(%s) OR chave = ANY(%s)) AND encontrado = TRUE",
        (first_words, exact_chaves),
    )
    by_first_word = {}
    for r in cur.fetchall():
        r = dict(r)
        by_first_word.setdefault(r["chave"].split(" ", 1)[0], []).append(r)

    ja_bloqueados_por_dados = 0
    precisam_visual = []
    for c in candidatos:
        cands_grupo = by_first_word.get(c["chave"].split(" ", 1)[0], [])
        bloqueado, _ = bloqueado_por_dados(cands_grupo)
        if bloqueado:
            ja_bloqueados_por_dados += 1
        else:
            precisam_visual.append(c)

    print(f"Ja corrigidos so com o fix de logica (tarja conhecida em algum fabricante): {ja_bloqueados_por_dados}")
    print(f"Precisam checagem visual (sem QUALQUER sinal de tarja nos dados): {len(precisam_visual)}")

    if args.limite:
        precisam_visual = precisam_visual[:args.limite]
        print(f"(limitado a {len(precisam_visual)} pelo --limite)")

    if not ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY nao configurada — abortando checagem visual.")
        cur.close()
        conn.close()
        return

    resultados = []
    for i, c in enumerate(precisam_visual, 1):
        print(f"[{i}/{len(precisam_visual)}] {c['ean']} {c['nome'][:50]!r} chave={c['chave']!r} ...", end=" ")
        r = claude_vision_check(c["imagem"], c["nome"])
        if not r or r.get("erro"):
            print(f"ERRO: {r.get('erro') if r else 'sem resposta'}")
            continue
        precisa_bloquear = (
            r.get("tarja_vermelha_ou_preta_visivel") is True
            or r.get("texto_prescricao_visivel") is True
            or r.get("farmacia_concorrente_visivel") is True
        ) and r.get("confianca") != "baixa"
        print(f"tarja={r.get('tarja_vermelha_ou_preta_visivel')} receita={r.get('texto_prescricao_visivel')} "
              f"concorrente={r.get('farmacia_concorrente_visivel')} ({r.get('nome_farmacia_concorrente')}) "
              f"conf={r.get('confianca')} -> {'BLOQUEAR' if precisa_bloquear else 'ok'}")
        resultados.append({**c, "visual": r, "bloquear": precisa_bloquear})
        time.sleep(0.5)

    a_bloquear = [r for r in resultados if r["bloquear"]]
    print(f"\n=== Total a bloquear via deteccao visual: {len(a_bloquear)} ===")
    for r in a_bloquear:
        print(f"  EAN={r['ean']} chave={r['chave']!r} nome={r['nome'][:60]!r} motivo={r['visual']}")

    if args.dry_run:
        print("\n--dry-run: nada foi gravado.")
        cur.close()
        conn.close()
        return

    for r in a_bloquear:
        cur.execute(
            """
            INSERT INTO anvisa_cache (chave, encontrado, tarja, exibir_imagem_publica, receita_retida)
            VALUES (%s, TRUE, 'vermelha', FALSE, FALSE)
            ON CONFLICT (chave) DO UPDATE SET
                exibir_imagem_publica = FALSE,
                tarja = COALESCE(anvisa_cache.tarja, 'vermelha'),
                encontrado = TRUE
            """,
            (r["chave"],),
        )
    conn.commit()
    print(f"\nGravado no anvisa_cache: {len(a_bloquear)} chaves marcadas exibir_imagem_publica=FALSE.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
