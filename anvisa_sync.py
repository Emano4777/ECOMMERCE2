"""
anvisa_sync.py — Sincronização ANVISA standalone (sem Flask).
Roda direto no terminal ou via anvisa_sync.bat.

Uso:
    python anvisa_sync.py              # processa apenas os pendentes
    python anvisa_sync.py --forcar     # apaga "não encontrados" e rebusca tudo
    python anvisa_sync.py --limite 50  # processa no máximo 50 chaves (teste)
"""
import sys
import os
import re
import json
import argparse
import subprocess
import tempfile

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import psycopg2
import psycopg2.extras


DATABASE_URL = os.environ["DATABASE_URL"]

GENERIC_TARJA_VERMELHA_IMG = "https://res.cloudinary.com/dizfq460q/image/upload/v1778783063/CAIXA_GEN%C3%89RICO_-_POUPAQUI_itiyth.jpg"
GENERIC_TARJA_PRETA_IMG = "https://res.cloudinary.com/dizfq460q/image/upload/v1778783450/ChatGPT_Image_14_de_mai._de_2026_15_30_35_wuovpb.png"

_STOP_WORDS = {
    "com","de","do","da","dos","das","para","por","em","e","ou",
    "mg","mcg","ml","ui","gr","cp","caps","comp","tab","un","und",
    "sol","solucao","injetavel","oral","topico","cutaneo","subl",
    "cpr","drg","amp","fco","bsa","gel","crem","pom","sup","xpe",
    "rev","retard","ret","iny","inf","efervescente","spray",
    "comprimido","comprimidos","capsula","capsulas","softgel","gelcap",
    "dragea","drageias","xarope","pomada","creme","supositorio",
    "injecao","injetavel","solucao","suspensao","emulsao","granulado",
    "pastilha","pastilhas","sublingual","transdermico","inalacao",
    "revestido","revestidos","liberacao","prolongada","retardada",
    "efervescente","mastigavel","dispersivel","orodisp","orodispersivel",
    # Embalagem (recipiente/unidade) — nunca faz parte do nome ANVISA
    "frasco","frascos","litro","litros",
    # Rótulos comerciais — nunca aparecem em nomes ANVISA registrados
    "generico","generica","similar","bioequivalente",
    # Prefixos de sal farmacológico (nunca são o nome ANVISA)
    "cloridrato","bromidrato","dicloridrato","hemitartarato","hemifumarato",
    "maleato","fumarato","succinato","besilato","tartarato",
    "monoidratado","monoidratada","hemif","succ",
    # Nomes de laboratório que aparecem como 2ª palavra no estoque
    "germed","vitamedic","biolab","globo","greenbios","uniphar",
    "farmax","quimica","bellaphytus","rioquimica","medley","sandoz",
    "torrent","teuto","eurofarma","prati","donaduzzi","neo","geolab",
    "pharlab","pharma","laboratorio","laboratorios",
    "natulab","multilab","airela","pharmascience","vitamedic","biosintetica",
    # Sufixos de forma/composição que mascaram INN quando 2ª palavra
    "hidroclor",   # OLMESARTANA HIDROCLOR → OLMESARTANA
    "medoxomila",  # OLMESARTANA MEDOXOMILA → OLMESARTANA
    "flacodin",    # SIMETICONA FLACODIN → SIMETICONA
}

# Mapeamento nome-comercial → INN para busca no bulário ANVISA.
# Keyed pela PRIMEIRA palavra do nome no estoque (maiúscula, sem acento).
# IMPORTANTE: manter sincronizado com _MARCA_TO_INN em app.py.
_MARCA_TO_INN = {
    # Analgésicos / AINEs
    "ALIVIUM":      "IBUPROFENO",              # ibuprofeno (Aché)
    "BUPROVIL":     "IBUPROFENO",              # ibuprofeno (Multilab)
    "ARTRINID":     "INDOMETACINA",            # indometacina 50mg inj (União)
    "NIMELIT":      "NIMESULIDA",              # nimesulida 100mg/gotas (Geolab)
    "BENZIFLEX":    "CLONIXINATO LISINA",      # clonixinato de lisina (EMS)
    "CODEX":        "CODEINA",                 # paracetamol+codeína — TARJA PRETA
    "COXYM":        "COLCHICINA",              # colchicina 0,5mg (UQ)
    # Espasmolíticos
    "BUSCOPAN":     "BUTILBROMETO ESCOPOLAMINA",
    "BUSCOPLEX":    "BUTILBROMETO ESCOPOLAMINA",
    # Antibióticos / Antiparasitários
    "AZITROPHAR":   "AZITROMICINA",
    "BELFACTRIM":   "SULFAMETOXAZOL TRIMETOPRIMA",
    "BELMIRAX":     "MEBENDAZOL",              # confirmado nas descricoes
    "BACINA":       "NEOMICINA BACITRACINA",
    "CIPRIXIN":     "CIPROFLOXACINO",          # colírio + dexametasona (Geolab)
    # Anti-hipertensivos / Cardiovascular
    "ARADOIS":      "LOSARTANA",               # losartana ±HCTZ (Biolab)
    "BESILAPIN":    "ANLODIPINO",              # besilato anlodipino (Geolab)
    "FEDIPINA":     "NIFEDIPINO",
    "CARBIDOL":     "CARBIDOPA LEVODOPA",      # 25+250mg Parkinson (Teuto)
    # Corticosteroides
    "BETAPROSPAN":  "BETAMETASONA",            # depot injetável (EMS)
    "BETRICORT":    "BETAMETASONA",            # creme/pomada (Geolab)
    "BIOFLADEX":    "BETAMETASONA",            # aerossol dérmico
    "CELERGIN":     "BETAMETASONA",            # + dexclorfeniramina (EMS)
    "CELESTAMINE":  "BETAMETASONA",            # + dexclorfeniramina (Schering)
    "CELESTONE":    "BETAMETASONA",            # (Schering/MSD)
    "CELESTRAT":    "BETAMETASONA",            # + dexclorfeniramina (UQ)
    "CORTICORTEN":  "PREDNISONA",              # 5/20mg (Neoquímica)
    # Anti-histamínicos
    "ALLEXOFEDRIN": "FEXOFENADINA",            # 120/180mg ±pseudoefedrina (EMS)
    "ARLIVRY":      "LORATADINA",              # xarope (Natulab)
    "ALERADINA":    "LORATADINA",              # (Multilab)
    "BERITIN":      "CETIRIZINA",              # kids xarope (Vitamedic)
    # Mucolíticos / Broncodilatadores
    "AMBROL":       "AMBROXOL",                # 15/30mg xarope (Brasterapica)
    "AMBROXMEL":    "AMBROXOL",                # (Cimed)
    "BRONQTRAT":    "AMBROXOL",                # (Natulab)
    "AERODINI":     "SALBUTAMOL",              # 100mcg/dose inalador (Teuto)
    "CELETIL":      "SALBUTAMOL",              # +ambroxol xarope (Geolab)
    # Vitaminas / outros medicamentos
    "BENERVA":      "TIAMINA",                 # vitamina B1 300mg (Sanofi)
    "ANTIAZIL":     "HIDROXIDO ALUMINIO",      # +Mg(OH)2 antiácido
    "CISTEIL":      "ACETILCISTEINA",          # NAC 200/600mg (Geolab)
    "CONTRACEP":    "MEDROXIPROGESTERONA",     # 150mg inj anticoncepcional
    "BENZODERM":    "PEROXIDO BENZOILA",       # peróxido de benzoíla (Pharmascience)
    # Inibidores de bomba de prótons (IBP)
    "ELPRAZOL":     "ESOMEPRAZOL",              # esomeprazol 20mg (Pharlab)
    "ESOP":         "ESOMEPRAZOL",              # esomeprazol 20/40mg (Multilab/Novaquímica)
    # Mucolíticos/Expectorantes
    "EMSEXPECT":    "AMBROXOL",                 # xarope expectorante (EMS)
    "EMSEXPECTOR":  "AMBROXOL",                 # xarope expectorante (EMS)
    "EXPECVEM":     "GUAIFENESINA",             # guaifenesina 200mg/15ml (Airela)
    "FLUCETIL":     "ACETILCISTEINA",           # acetilcisteína 600mg (Maxinutri)
    # Contraceptivos
    "ETINIL":       "ETINILESTRADIOL GESTODENO",  # etinilestradiol+gestodeno (Biosintetica)
    # Anti-histamínico / Ansiolítico
    "DROXY":        "HIDROXIZINA",              # cloridrato de hidroxizina 25mg (EMS/Multilab)
    # Colírio antiglaucoma
    "DRUSOLOL":     "DORZOLAMIDA TIMOLOL",      # dorzolamida 2% + timolol 0,5% (Farmasa)
    # AINEs
    "FARMOXICAM":   "PIROXICAM",               # piroxicam 20mg (Pharlab)
    # Antiflatulento
    "FLACODIN":     "SIMETICONA",              # simeticona 125mg/75mg (Vidora)
    # Antifúngico
    "FUNOK":        "ITRACONAZOL",             # itraconazol 100mg (Multilab)
    # Antiácido
    "GASTROBEM":    "HIDROXIDO ALUMINIO",       # Al/Mg-hidroxido + dimeticona (Natulab)
    # Antiflatulento
    "LUFTAL":       "SIMETICONA",              # simeticona 125/40mg (Reckitt Benckiser)
    # Laxativos
    "LACTUGOLD":    "LACTULOSE",               # lactulose 667mg/ml xarope (Arte Nativa)
    "NATULAXE":     "BISACODILA",              # bisacodila 34mg caps (Natulab) — ANVISA usa forma com 'A'
    # Analgésico/Antipirético
    "TILEMAXY":     "PARACETAMOL",             # paracetamol gotas/xarope (Natulab)
    # Antifúngico oral
    "NISTAMAX":     "NISTATINA",               # nistatina 100.000UI/ml susp (Natulab)
    # Antibiótico amoxicilina+clavulanato
    "POLICLAVUMOXIL": "AMOXICILINA CLAVULANATO",  # amox+clavulanato (EMS)
    # Antibióticos tópicos
    "NEMICINA":     "NEOMICINA",               # neomicina 3,5mg/g pomada dérmicaa (Delta)
    # Anti-inflamatórios
    "NEOTAREN":     "DICLOFENACO",             # diclofenaco sódico 50mg (NeoQuímica)
    # Diurético
    "NEOSEMID":     "FUROSEMIDA",              # furosemida 40mg (NeoQuímica)
    # Antidiarreico
    "KAOSEC":       "LOPERAMIDA",              # loperamida 2mg (Pharmascience)
    # Variação de nome INN no estoque (sem 'A' final)
    "LORATADIN":    "LORATADINA",              # loratadina (variação de grafia no estoque)
}


def _chave(nome):
    tks = re.sub(r"[^\w\s]", " ", nome or "").upper().split()
    # Se a 1ª palavra for um nome comercial conhecido, retorna o INN diretamente.
    if tks and tks[0] in _MARCA_TO_INN:
        return _MARCA_TO_INN[tks[0]]
    words = []
    for w in tks:
        if w.lower() in _STOP_WORDS or any(c.isdigit() for c in w) or len(w) < 4:
            continue
        words.append(w)
        if len(words) >= 2:
            break
    return " ".join(words)


def _db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def _cache_row(chave, dados):
    return (
        chave,
        dados.get("encontrado", False),
        dados.get("nome_anvisa"),
        dados.get("laboratorio"),
        dados.get("situacao"),
        dados.get("principio_ativo"),
        dados.get("url_bula"),
        dados.get("serve_para"),
        dados.get("como_usar"),
        dados.get("alertas"),
        dados.get("id_produto"),
        dados.get("tarja"),
        dados.get("jwt_bula"),
        dados.get("receita_retida"),
        dados.get("venda_online_permitida"),
        dados.get("exibir_imagem_publica"),
        dados.get("dizeres_receita"),
        dados.get("dizeres_imagem"),
    )


def _salvar_many(rows, page_size=50):
    if not rows:
        return 0
    conn = _db()
    try:
        cur = conn.cursor()
        psycopg2.extras.execute_values(cur, """
            INSERT INTO anvisa_cache
              (chave, encontrado, nome_anvisa, laboratorio, situacao,
               principio_ativo, url_bula, serve_para, como_usar, alertas,
               id_produto, tarja, jwt_bula, receita_retida, venda_online_permitida,
               exibir_imagem_publica, dizeres_receita, dizeres_imagem, criado_em)
            VALUES %s
            ON CONFLICT (chave) DO UPDATE SET
              encontrado      = EXCLUDED.encontrado,
              nome_anvisa     = EXCLUDED.nome_anvisa,
              laboratorio     = EXCLUDED.laboratorio,
              situacao        = EXCLUDED.situacao,
              principio_ativo = EXCLUDED.principio_ativo,
              url_bula        = EXCLUDED.url_bula,
              serve_para      = EXCLUDED.serve_para,
              como_usar       = EXCLUDED.como_usar,
              alertas         = EXCLUDED.alertas,
              id_produto      = EXCLUDED.id_produto,
              tarja           = EXCLUDED.tarja,
              jwt_bula        = EXCLUDED.jwt_bula,
              receita_retida  = EXCLUDED.receita_retida,
              venda_online_permitida = EXCLUDED.venda_online_permitida,
              exibir_imagem_publica = EXCLUDED.exibir_imagem_publica,
              dizeres_receita = EXCLUDED.dizeres_receita,
              dizeres_imagem  = EXCLUDED.dizeres_imagem,
              criado_em       = NOW()
        """, rows, template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())", page_size=page_size)
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return len(rows)


_INDICADORES_MEDICAMENTO = re.compile(
    r"\b(\d+\s*mg|\d+\s*mcg|\d+\s*ml|\d+\s*g\b|\d+\s*ui|"
    r"comprimido|capsula|caps\b|cpr\b|drg\b|ampola|amp\b|"
    r"xarope|pomada|creme|gel\b|supositorio|injetavel|"
    r"solucao|suspensao|spray|sublingual|efervescente|"
    r"antibiotico|analgesico|antiinflamatorio|vitamina|"
    r"colirio|gotas\b|tintura|xpe\b|frasco\b)",
    re.IGNORECASE,
)


def _parece_medicamento(nome: str) -> bool:
    """Retorna True se o nome tem indício de ser medicamento registrado na ANVISA."""
    return bool(_INDICADORES_MEDICAMENTO.search(nome))


def _placeholder_tarja(tarja):
    tarja = (tarja or "").strip().lower()
    if tarja == "preta":
        return GENERIC_TARJA_PRETA_IMG
    if tarja == "vermelha":
        return GENERIC_TARJA_VERMELHA_IMG
    return None


def _aplicar_restricoes_imagem(chaves=None, page_size=200):
    """Grava placeholder Poupaqui para medicamentos que nao podem exibir imagem publica."""
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ecommerce_produto_imagens (
            cnpjloja TEXT NOT NULL,
            ean TEXT NOT NULL,
            imagem_url TEXT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (cnpjloja, ean)
        )
    """)
    chaves = sorted({c for c in (chaves or []) if c})
    if chaves:
        cur.execute("""
            SELECT chave, tarja, exibir_imagem_publica
            FROM anvisa_cache
            WHERE encontrado=TRUE AND tarja IN ('preta','vermelha') AND chave = ANY(%s)
        """, (chaves,))
    else:
        cur.execute("""
            SELECT chave, tarja, exibir_imagem_publica
            FROM anvisa_cache
            WHERE encontrado=TRUE AND tarja IN ('preta','vermelha')
        """)
    restricoes = {r["chave"]: dict(r) for r in cur.fetchall()}
    if not restricoes:
        cur.close()
        return 0

    cur.execute("""
        SELECT e.cnpj AS cnpjloja, e.barras AS ean, COALESCE(m.descricao, e.descricao) AS nome
        FROM estoque e
        LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
        WHERE e.estoque > 0 AND COALESCE(e.barras, e.barras_norm, '') <> ''

        UNION ALL

        SELECT ae.cnpj_loja AS cnpjloja, ae.ean, COALESCE(m.descricao, ae.descricao_produto) AS nome
        FROM automatiza_estoque ae
        LEFT JOIN medicamentos m ON m.barra_norm = ae.ean
        WHERE ae.quantidade_estoque > 0 AND COALESCE(ae.ean, '') <> ''
    """)
    upserts = []
    seen = set()
    for row in cur.fetchall():
        cnpj = (row.get("cnpjloja") or "").strip()
        ean = (row.get("ean") or "").strip()
        regra = restricoes.get(_chave(row.get("nome") or ""))
        if not cnpj or not ean or not regra or regra.get("exibir_imagem_publica") is True:
            continue
        placeholder = _placeholder_tarja(regra.get("tarja"))
        key = (cnpj, ean)
        if placeholder and key not in seen:
            seen.add(key)
            upserts.append((cnpj, ean, placeholder))

    if upserts:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO ecommerce_produto_imagens (cnpjloja, ean, imagem_url, updated_at)
            VALUES %s
            ON CONFLICT (cnpjloja, ean) DO UPDATE
              SET imagem_url=EXCLUDED.imagem_url, updated_at=NOW()
        """, upserts, template="(%s,%s,%s,NOW())", page_size=page_size)
    conn.commit()
    cur.close()
    conn.close()
    return len(upserts)


def main():
    ap = argparse.ArgumentParser(description="Sincronização ANVISA standalone")
    ap.add_argument("--forcar", action="store_true",
                    help="Apaga registros 'não encontrado' e rebusca todos")
    ap.add_argument("--limite", type=int, default=0,
                    help="Processar no máximo N chaves (0 = sem limite, útil para testes)")
    ap.add_argument("--todos", action="store_true",
                    help="Inclui produtos sem indicativo de medicamento (muito mais lento)")
    ap.add_argument("--db-batch", type=int, default=40,
                    help="Quantidade de resultados ANVISA para salvar por commit (padrao: 40)")
    ap.add_argument("--sem-imagens", action="store_true",
                    help="Nao grava placeholders no catalogo ao final")
    args = ap.parse_args()
    db_batch = max(1, min(int(args.db_batch or 40), 200))

    conn = _db()
    cur  = conn.cursor()

    if args.forcar:
        cur.execute("DELETE FROM anvisa_cache WHERE encontrado=FALSE")
        conn.commit()
        print("Produtos encontrados serao reprocessados para atualizar restricoes sanitarias.\n")
        print("Registros 'não encontrado' apagados do cache.\n")

    # Garante schema do anvisa_cache (idempotente)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS anvisa_cache (
            chave TEXT PRIMARY KEY, encontrado BOOLEAN DEFAULT FALSE,
            nome_anvisa TEXT, laboratorio TEXT, situacao TEXT,
            principio_ativo TEXT, url_bula TEXT, serve_para TEXT,
            como_usar TEXT, alertas TEXT, id_produto INTEGER,
            tarja TEXT, criado_em TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS id_produto INTEGER")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS tarja TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS jwt_bula TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS receita_retida BOOLEAN")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS venda_online_permitida BOOLEAN")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS exibir_imagem_publica BOOLEAN")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS dizeres_receita TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS dizeres_imagem TEXT")

    # Garante que as tabelas de catálogo existem
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ecommerce_catalogo_oculto (
            cnpjloja TEXT NOT NULL, ean TEXT NOT NULL, PRIMARY KEY (cnpjloja, ean)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ecommerce_catalogo_extra (
            cnpjloja TEXT NOT NULL, ean TEXT NOT NULL, PRIMARY KEY (cnpjloja, ean)
        )
    """)
    conn.commit()

    # Produtos DNS/Vitnatu visíveis + extras adicionados manualmente pelas lojas
    cur.execute("""
        SELECT DISTINCT COALESCE(m.descricao, e.descricao) AS nome
        FROM estoque e
        LEFT JOIN omie_estoque_dns dns ON dns.ean_norm = COALESCE(e.barras_norm, e.barras)
        LEFT JOIN medicamentos m       ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        WHERE e.estoque > 0
          AND COALESCE(m.descricao, e.descricao) IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM ecommerce_catalogo_oculto co
              WHERE co.cnpjloja = e.cnpj AND co.ean = e.barras
          )
          AND (dns.ean_norm IS NOT NULL
               OR mi.cloudinary_url IS NOT NULL
               OR NULLIF(TRIM(m.imagem), '') IS NOT NULL
               OR e.descricao ILIKE ANY(ARRAY[
                    '%%anasol%%','%%vit natu%%','%%vitnatu%%',
                    '%%pronabol%%','%%ricosol%%','%%unispray%%','%%goodvit%%'
                  ])
               OR EXISTS (
                    SELECT 1 FROM ecommerce_catalogo_extra ex
                    WHERE ex.cnpjloja = e.cnpj AND ex.ean = e.barras
               ))

        UNION

        SELECT DISTINCT COALESCE(m.descricao, ae.descricao_produto) AS nome
        FROM automatiza_estoque ae
        LEFT JOIN omie_estoque_dns dns ON dns.ean_norm = ae.ean
        LEFT JOIN medicamentos m       ON m.barra_norm = ae.ean
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        WHERE ae.quantidade_estoque > 0
          AND COALESCE(m.descricao, ae.descricao_produto) IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM ecommerce_catalogo_oculto co
              WHERE co.cnpjloja = ae.cnpj_loja AND co.ean = ae.ean
          )
          AND (dns.ean_norm IS NOT NULL
               OR mi.cloudinary_url IS NOT NULL
               OR NULLIF(TRIM(m.imagem), '') IS NOT NULL
               OR ae.descricao_produto ILIKE ANY(ARRAY[
                    '%%anasol%%','%%vit natu%%','%%vitnatu%%',
                    '%%pronabol%%','%%ricosol%%','%%unispray%%','%%goodvit%%'
                  ])
               OR EXISTS (
                    SELECT 1 FROM ecommerce_catalogo_extra ex
                    WHERE ex.cnpjloja = ae.cnpj_loja AND ex.ean = ae.ean
               ))
    """)
    nomes_raw = [r["nome"] for r in cur.fetchall() if r["nome"]]

    # Chaves já em cache (90 dias)
    if args.forcar:
        cached = set()
    else:
        cur.execute("""
            SELECT chave
            FROM anvisa_cache
            WHERE criado_em >= DATE_TRUNC('year', NOW())
              AND (
                    encontrado = FALSE
                    OR (receita_retida IS NOT NULL AND exibir_imagem_publica IS NOT NULL)
                  )
        """)
        cached = {r["chave"] for r in cur.fetchall()}
    cur.close()
    conn.close()  # libera antes do worker; cada batch abre/fecha sua própria conexão

    # Deduplica e filtra pendentes
    vistas: set = set()
    chaves_pendentes: list = []
    for nome in nomes_raw:
        ch = _chave(nome)
        if ch and ch not in vistas and ch not in cached:
            vistas.add(ch)
            chaves_pendentes.append(ch)

    total = len(chaves_pendentes)
    print(f"Produtos DNS/Vitnatu unicos: {len(nomes_raw)}")
    print(f"Já em cache (<=90 dias)    : {len(cached)}")
    print(f"A processar agora          : {total}")

    if args.limite and total > args.limite:
        chaves_pendentes = chaves_pendentes[:args.limite]
        total = args.limite
        print(f"(limitado a {total} por --limite)")

    if not total:
        print("\nNada a processar. Cache já atualizado.")
        if args.sem_imagens:
            atualizadas = 0
            print("Atualizacao de imagens sanitarias pulada por --sem-imagens.")
        else:
            atualizadas = _aplicar_restricoes_imagem(page_size=max(100, db_batch * 5))
            print(f"Imagens sanitarias atualizadas no catalogo: {atualizadas}")
        return

    print(f"\nIniciando worker ANVISA...\n")

    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_anvisa_pw_worker.py")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

    # Usa arquivos temporários para evitar deadlock de pipe no Windows:
    # escrever 890+ linhas no stdin antes do worker ler qualquer coisa esgota
    # o buffer do pipe (~4096 bytes) e trava o processo permanentemente.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt",
                                     delete=False, newline="\n") as fin:
        for ch in chaves_pendentes:
            fin.write(ch + "\n")
        input_path = fin.name

    output_path = input_path.replace(".txt", "_out.jsonl")

    import time as _time

    ok = 0
    falha = 0
    feitos = 0
    pending_rows = []
    chaves_processadas = set()

    try:
        proc = subprocess.Popen(
            [sys.executable, worker, "--input", input_path, "--output", output_path],
            stderr=sys.stderr,
            env=env,
        )

        ok = 0
        falha = 0
        feitos = 0
        pending_rows = []
        chaves_processadas = set()

        # Aguarda o worker criar o arquivo de saída
        while not os.path.exists(output_path):
            if proc.poll() is not None:
                break  # worker morreu antes de criar o arquivo
            _time.sleep(0.5)

        with open(output_path, encoding="utf-8") as fout:
            while True:
                line = fout.readline()
                if line:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec   = json.loads(line)
                        dados = rec.get("dados") or {}
                        chave_rec = rec["chave"]
                        # Nao salvar 403 — tentar de novo na proxima execucao
                        if not dados.get("bloqueado_403"):
                            pending_rows.append(_cache_row(chave_rec, dados))
                        chaves_processadas.add(chave_rec)
                        if len(pending_rows) >= db_batch:
                            _salvar_many(pending_rows, page_size=db_batch)
                            pending_rows.clear()
                        feitos += 1
                        pct = round(feitos / total * 100)
                        if dados.get("encontrado"):
                            ok += 1
                            print(f"[{feitos:4}/{total}] {pct:3}%  [OK]  {rec['chave']}"
                                  f"  ->  {dados.get('nome_anvisa', '')}")
                        elif dados.get("bloqueado_403"):
                            falha += 1
                            print(f"[{feitos:4}/{total}] {pct:3}%  [403] {rec['chave']}  - bloqueado, sera reprocessado")
                        else:
                            falha += 1
                            print(f"[{feitos:4}/{total}] {pct:3}%  [--]  {rec['chave']}  - nao encontrado")
                    except Exception as exc:
                        feitos += 1
                        falha += 1
                        print(f"[{feitos:4}/{total}]  [!]  ERRO: {exc}")
                else:
                    # Sem nova linha — verifica se o worker ainda está rodando
                    if proc.poll() is not None:
                        break  # worker terminou e não há mais linhas
                    _time.sleep(0.3)  # aguarda próxima linha do worker

        if pending_rows:
            _salvar_many(pending_rows, page_size=db_batch)
            pending_rows.clear()

        proc.wait()
    finally:
        try:
            os.unlink(input_path)
        except OSError:
            pass
        try:
            os.unlink(output_path)
        except OSError:
            pass

    if args.sem_imagens:
        atualizadas = 0
        print("Atualizacao de imagens sanitarias pulada por --sem-imagens.")
    else:
        atualizadas = _aplicar_restricoes_imagem(chaves=chaves_processadas, page_size=max(100, db_batch * 5))

    print(f"\n{'='*55}")
    print(f"Concluido!  OK={ok} encontrados   NAO={falha} nao encontrados")
    print(f"Imagens sanitarias atualizadas no catalogo: {atualizadas}")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()
