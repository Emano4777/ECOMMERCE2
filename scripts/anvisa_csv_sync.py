#!/usr/bin/env python3
"""
anvisa_csv_sync.py — Complementa e valida anvisa_cache com o CSV oficial da ANVISA.

Fonte primária:
  https://dados.anvisa.gov.br/dados/DADOS_ABERTOS_MEDICAMENTOS.csv

Comportamento:
  - Hard override: CAP=Sim  → tarja preta, exibir_imagem_publica=FALSE (sempre)
  - Hard override: RESTRICAO_HOSPITALAR=Sim → tarja vermelha (não faz downgrade de preta)
  - Soft fill: demais campos usa COALESCE (não sobrescreve dados do bulário)
  - Log de correções: qualquer tarja alterada pelo CSV é reportada

Uso:
  python scripts/anvisa_csv_sync.py               # complementa e corrige gaps
  python scripts/anvisa_csv_sync.py --dry-run     # simula sem alterar BD
  python scripts/anvisa_csv_sync.py --validate    # valida 10 EANs do cache vs CSV, depois roda
  python scripts/anvisa_csv_sync.py --stats       # só exibe estatísticas do cache
  python scripts/anvisa_csv_sync.py --force       # processa todos (incluindo encontrado=TRUE)
"""
from __future__ import annotations

import os, sys, re, io, csv, time, argparse, unicodedata, logging, ssl
import urllib.request
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

CSV_URL = "https://dados.anvisa.gov.br/dados/DADOS_ABERTOS_MEDICAMENTOS.csv"

_STOP = {
    "com","de","do","da","dos","das","para","por","em","e","ou",
    "mg","mcg","ml","ui","gr","cp","caps","comp","tab","un","und",
    "sol","solucao","injetavel","oral","topico","cutaneo","subl",
    "cpr","drg","amp","fco","bsa","gel","crem","pom","sup","xpe",
    "susp","solu","gota","gotas","soln","inj",
    "rev","retard","ret","iny","inf","efervescente","spray",
    "comprimido","comprimidos","capsula","capsulas","softgel","gelcap",
    "dragea","drageias","xarope","pomada","creme","supositorio",
    "injecao","injetavel","solucao","suspensao","emulsao","granulado",
    "pastilha","pastilhas","sublingual","transdermico","inalacao",
    "revestido","revestidos","liberacao","prolongada","retardada",
    "efervescente","mastigavel","dispersivel","orodisp","orodispersivel",
    "frasco","frascos","litro","litros","copo","copinho","dosador","medidor",
    "conta","seringa","caneta","nebulizador","inalador","vaporizador",
    "generico","generica","similar","bioequivalente",
    "cloridrato","bromidrato","dicloridrato",
    "hemitartarato","hemifumarato","maleato","fumarato","succinato",
    "besilato","tartarato","monoidratado","monoidratada",
}

_MIP_DCI = {
    "PARACETAMOL","DIPIRONA","IBUPROFENO","ACIDO ACETILSALICILICO",
    "LORATADINA","CETIRIZINA","DEXCLORFENIRAMINA","CLOROFENIRAMINA",
    "RANITIDINA","OMEPRAZOL","PANTOPRAZOL","FAMOTIDINA",
    "SIMETICONA","DIMETICONA","LACTULOSE","BISACODIL",
    "METOCLOPRAMIDA","DOMPERIDONA","DIMENIDRINATO",
    "BENZOCAINA","CETILPIRIDINIO","CLOREXIDINA","POVIDONA",
    "DEXPANTENOL","VITAMINA C","ACIDO ASCORBICO",
    "ZINCO","MAGNESIO","CALCIO","FERRO QUELATO",
    "FLUCONAZOL","MICONAZOL","CLOTRIMAZOL","NISTATINA",
    "LOPERAMIDA","TANINO","CARVAO ATIVADO",
    "NAPROXENO","DICLOFENACO","NIMESULIDA",
    "AMBROXOL","ACETILCISTEINA","GUAIFENESINA","BROMEXINA",
    "SALBUTAMOL","IPRATROPIO",
    "LIDOCAINA","BENZOCAINA",
    "NEOMICINA","BACITRACINA","MUPIROCINA",
    "VITAMINA A","VITAMINA D","VITAMINA E","VITAMINA B12",
    "COMPLEXO B","ACIDO FOLICO",
    "AGUA BORICADA","AGUA OXIGENADA","PERMANGANATO POTASSIO",
    "SOLUCAO FISIOLOGICA","SORO FISIOLOGICO",
}


def _sem_acento(s: str) -> str:
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")


def _chave(nome: str) -> str:
    """Normaliza nome → chave de lookup (mesma lógica do app.py _anvisa_chave)."""
    tks = re.sub(r"[^\w\s]", " ", _sem_acento(nome or "").upper()).split()
    words = []
    for w in tks:
        if w.lower() in _STOP or any(c.isdigit() for c in w) or len(w) < 4:
            continue
        words.append(w)
        if len(words) >= 2:
            break
    return " ".join(words)


# Prefixos de formas salinas que precedem o nome do fármaco no principio_ativo
_PA_SALT_RE = re.compile(
    r"^(DI)?CLORIDRATO|BROMIDRATO|HEMITARTARATO|HEMIFUMARATO|TARTARATO"
    r"|MALEATO|FUMARATO|SUCCINATO|BESILATO|ACETATO|FOSFATO|SULFATO"
    r"|OXALATO|CITRATO|VALPROATO|MONOIDRATAD[AO]|ANIDRO|MONOIDRAT"
    r"|TRIHIDRATAD[AO]|DIHIDRATAD[AO]",
    re.I,
)

def _chave_pa(pa: str) -> str:
    """
    Normaliza principio_ativo para matching contra CSV.
    Remove prefixos de forma salina (CLORIDRATO DE, HEMITARTARATO DE, etc.)
    e aplica _chave() no restante.
    """
    pa = _sem_acento((pa or "").upper())
    # Remove prefixo de sal: "CLORIDRATO DE " → ""
    pa = re.sub(r"^(" + _PA_SALT_RE.pattern + r")\s+DE\s+", "", pa, flags=re.I)
    # Também remove "(PORT. 344/98 LISTA ...)" etc.
    pa = re.sub(r"\(PORT[^)]*\)", "", pa)
    return _chave(pa)


def _is_sim(val: str | None) -> bool:
    return (val or "").strip().upper() in ("S", "SIM", "YES", "1", "TRUE")


# Chaves OTC que NÃO devem ser sobreescritas pelo CSV.
# Motivo: são produtos comuns (antissépticos, cosméticos) cujo _chave() colide com
# registros farmacêuticos específicos na ANVISA (ex: "ÁGUA PARA INJEÇÃO", "ÁLCOOL 70% HEMAFARMA").
_CHAVES_OTC_ISENTO: frozenset[str] = frozenset({
    # Antissépticos/cosméticos cujo _chave() colide com versão farmacêutica registrada
    "AGUA OXIGENADA", "AGUA BORICADA", "AGUA DESTILADA", "AGUA MELISSA",
    "AGUA", "ALCOOL ETILICO", "ALCOOL GEL", "ALCOOL ANTISSEPTICO", "ALCOOL IODADO",
    "SORO FISIOLOGICO", "ANTISSEPTICO", "CANFORA", "AMONIA",
    # Vitaminas OTC — versão injetável/farmacêutica contamina tablets comuns
    "ACIDO ASCORBICO", "ACIDO FOLICO", "VITAMINA", "VITAM",
    # Chaves genéricas demais (1ª palavra genérica contamina tudo)
    "NOVA", "FONT", "CARVAO VEGETAL",
    # Marcas cosméticas / higiene cujo nome coincide com entrada ANVISA tarjada
    "ASEPXIA", "ASEPXIA SECATIVO", "ASEPXIA FORTE",
    "CAREFREE", "CAREFREE PROT",
    "CIFLOGEX", "CIFLOGEX DIET", "CIFLOGEX LIMAO", "CIFLOGEX MENTA",
    "AVENE", "AVENE AGUA",
    "BEPANTOL", "BEPANTOL DERMA",
    "BIODERMA", "BIODERMA SENSIBIO",
    "VICHY", "VICHY LIFTACTIV",
    "NEUTROGENA", "NEUTROGENA HIDRATANTE",
    "NIVEA", "NIVEA HIDRATANTE",
})

# Padrões de CLASSE_TERAPEUTICA que indicam substância controlada (tarja preta).
# Regra de exclusão: se a classe contiver "NAO " + padrão (ex: "NAO NARCOTICOS"), NÃO classifica como preta.
_CLASSE_PRETA = (
    "PSICOTR",       # psicotrópico(s)
    "ENTORPEC",      # entorpecente(s)
    "NARCOT",        # narcóticos — mas NÃO "NAO NARCOTICOS"
    "OPIOIDE",       # opioides — mas NÃO "NAO OPIOIDE"
    "OPIOID",
    "ANSIOL",        # ansioliticos (benzodiazepinas: alprazolam, clonazepam, diazepam...)
    "HIPNOT",        # hipnóticos (zolpidem, midazolam...)
    "PSICOANEL",     # psicoanalético (metilfenidato, lisdexanfetamina...)
    "PSICOANAL",     # variante sem acento
    "ANFETAM",       # anfetaminas
)


def _derivar_tarja(row: dict, principio_ativo: str) -> tuple[str | None, bool | None, str]:
    """
    Retorna (tarja, exibir_imagem_publica, motivo).
    motivo é uma string descrevendo a origem da classificação.
    """
    pa_upper = _sem_acento((principio_ativo or "").upper())

    if _is_sim(row.get("CAP")):
        return "preta", False, "CAP=Sim (substância controlada)"

    if _is_sim(row.get("RESTRICAO_HOSPITALAR")):
        return "vermelha", False, "RESTRICAO_HOSPITALAR=Sim"

    classe = _sem_acento((row.get("CLASSE_TERAPEUTICA") or "").upper())
    for pat in _CLASSE_PRETA:
        if pat in classe and ("NAO " + pat) not in classe:
            return "preta", False, f"CLASSE_TERAPEUTICA={row.get('CLASSE_TERAPEUTICA','')}"

    for dci in _MIP_DCI:
        if dci in pa_upper:
            return None, True, f"MIP IN-285/2024 ({dci})"

    return "vermelha", False, "padrao-conservador"


def _get_conn():
    dsn = os.getenv("DATABASE_URL") or os.getenv("DDATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL não configurada.")
    dsn_clean = re.sub(r"[?&]sslmode=[^&]*", "", dsn)
    return psycopg2.connect(dsn_clean, connect_timeout=30, sslmode="require")


def download_csv(url: str) -> list[dict]:
    log.info("Baixando CSV ANVISA: %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    # Cria contexto SSL sem verificação (gov.br usa cert de cadeia não distribuída no Python/Windows)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
        raw = resp.read()
    log.info("CSV baixado: %.1f MB", len(raw) / 1_048_576)

    for enc in ("latin-1", "utf-8", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    rows = list(reader)
    log.info("CSV: %d registros, colunas: %s", len(rows), list(rows[0].keys()) if rows else [])
    return rows


def build_index(csv_rows: list[dict]) -> tuple[dict, dict]:
    """
    Retorna (index_nome, index_pa):
    - index_nome: chave(nome_produto) → list[dict]
    - index_pa:   chave_pa(principio_ativo) → list[dict]  (matching secundário)
    """
    sample = csv_rows[0] if csv_rows else {}
    cols = list(sample.keys())

    def _find_col(*candidates):
        for c in candidates:
            for col in cols:
                if col.strip().upper() == c.upper():
                    return col
        return None

    col_produto = _find_col("PRODUTO", "NOME_PRODUTO", "DESCRICAO", "NAME")
    col_pa      = _find_col("PRINCIPIO_ATIVO", "PRINCIPIO ATIVO", "SUBSTANCIA", "DCI",
                            "NOME_TECNICO", "NOME_GENERICO")
    col_cap     = _find_col("CAP")
    col_hosp    = _find_col("RESTRICAO_HOSPITALAR")
    col_classe  = _find_col("CLASSE_TERAPEUTICA", "CLASSE_TERAPEUTICA_DCBX")

    log.info("Colunas detectadas → produto=%s | principio_ativo=%s | CAP=%s | hosp=%s | classe=%s",
             col_produto, col_pa, col_cap, col_hosp, col_classe)

    index_nome: dict[str, list[dict]] = {}  # por nome do produto
    index_pa:   dict[str, list[dict]] = {}  # por principio ativo (normalizado sem sal)

    # Para o index_pa, agrega o "pior" (mais restritivo) por chave
    # para que um PA controlado em qualquer produto domine
    _pa_best: dict[str, dict] = {}

    for row in csv_rows:
        nome_produto = row.get(col_produto, "") if col_produto else ""
        pa           = row.get(col_pa, "")       if col_pa      else ""

        norm = {
            "PRODUTO":              nome_produto.strip(),
            "PRINCIPIO_ATIVO":      pa.strip(),
            "CAP":                  row.get(col_cap, "")   if col_cap   else "",
            "RESTRICAO_HOSPITALAR": row.get(col_hosp, "") if col_hosp  else "",
            "CLASSE_TERAPEUTICA":   row.get(col_classe, "") if col_classe else "",
        }

        # Índice por nome do produto
        for nome_idx in filter(None, [nome_produto]):
            ch = _chave(nome_idx)
            if ch and len(ch) >= 4:
                index_nome.setdefault(ch, []).append(norm)

        # Índice por principio ativo (com normalização de sal)
        if pa:
            # Indexa por cada PA individual (pode ter múltiplos separados por vírgula)
            for pa_part in re.split(r"[,;]", pa):
                pa_part = pa_part.strip()
                ch_pa = _chave_pa(pa_part)
                if ch_pa and len(ch_pa) >= 4:
                    index_pa.setdefault(ch_pa, []).append(norm)
                    # Mantém o mais restritivo como "melhor" representante
                    existing = _pa_best.get(ch_pa)
                    if existing is None:
                        _pa_best[ch_pa] = norm
                    else:
                        # Prefere o que tiver classe mais restritiva
                        _, _, m_new = _derivar_tarja(norm, pa_part)
                        _, _, m_old = _derivar_tarja(existing, existing.get("PRINCIPIO_ATIVO",""))
                        tarja_new, _, _ = _derivar_tarja(norm, pa_part)
                        tarja_old, _, _ = _derivar_tarja(existing, existing.get("PRINCIPIO_ATIVO",""))
                        if tarja_new == "preta" and tarja_old != "preta":
                            _pa_best[ch_pa] = norm

    # Para o index_pa, usa o representante mais restritivo como primeiro elemento
    for ch, best in _pa_best.items():
        entries = index_pa.get(ch, [])
        if entries and entries[0] is not best:
            entries.remove(best) if best in entries else None
            entries.insert(0, best)

    log.info("Índice CSV: nome=%d chaves | pa=%d chaves únicas", len(index_nome), len(index_pa))
    return index_nome, index_pa


def _print_validation_table(rows: list[dict], index_nome: dict, index_pa: dict) -> None:
    """Imprime tabela comparando cache atual vs CSV para validação visual."""
    header = f"{'EAN':<15} {'CHAVE':<22} {'TARJA_CACHE':<13} {'TARJA_CSV':<13} {'EXIBIR_CACHE':<14} {'MATCH':<8} {'STATUS':<12} MOTIVO"
    print("\n" + "="*len(header))
    print("VALIDAÇÃO: anvisa_cache vs CSV ANVISA")
    print("="*len(header))
    print(header)
    print("-"*len(header))

    for row in rows:
        chave       = row["chave"] or ""
        tarja_cache = row["tarja"] or "NULL"
        exibir_cache = str(row["exibir_imagem_publica"]) if row["exibir_imagem_publica"] is not None else "NULL"
        ean          = row.get("ean") or "—"

        # Tenta matching primário (nome) e secundário (PA)
        matches = index_nome.get(chave, [])
        match_tipo = "nome"
        if not matches and row.get("principio_ativo"):
            for pa_part in re.split(r"[,;]", row["principio_ativo"]):
                ch_pa = _chave_pa(pa_part.strip())
                if ch_pa:
                    matches = index_pa.get(ch_pa, [])
                    if matches:
                        match_tipo = "PA"
                        break

        if not matches:
            print(f"{ean:<15} {chave:<22} {tarja_cache:<13} {'N/A':<13} {exibir_cache:<14} {'SEM_MATCH':<8} {'SEM_MATCH':<12} sem correspondência no CSV")
            continue

        csv_data = matches[0]
        pa = csv_data["PRINCIPIO_ATIVO"] or row.get("principio_ativo") or ""
        tarja_csv, exibir_csv, motivo = _derivar_tarja(csv_data, pa)
        tarja_csv_str = tarja_csv or "NULL"

        if tarja_cache == tarja_csv_str:
            status = "OK"
        elif tarja_csv == "preta" and tarja_cache != "preta":
            status = "CORRIGIR↑"
        elif tarja_csv == "vermelha" and tarja_cache == "NULL":
            status = "PREENCHER"
        else:
            status = "DIVERGE"

        print(f"{ean:<15} {chave:<22} {tarja_cache:<13} {tarja_csv_str:<13} {exibir_cache:<14} {match_tipo:<8} {status:<12} {motivo}")

    print("="*len(header) + "\n")


def run(dry_run: bool, force: bool, stats_only: bool, validate: bool):
    conn = _get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Estatísticas do cache
    cur.execute("""
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE encontrado = TRUE)  AS encontrados,
          COUNT(*) FILTER (WHERE encontrado = FALSE OR encontrado IS NULL) AS nao_encontrados,
          COUNT(*) FILTER (WHERE tarja IS NULL AND encontrado = TRUE) AS sem_tarja,
          COUNT(*) FILTER (WHERE exibir_imagem_publica IS NULL AND encontrado = TRUE) AS sem_exibir_flag,
          COUNT(*) FILTER (WHERE tarja = 'preta')    AS tarja_preta,
          COUNT(*) FILTER (WHERE tarja = 'vermelha') AS tarja_vermelha
        FROM anvisa_cache
    """)
    st = dict(cur.fetchone())
    log.info(
        "Cache atual: total=%d encontrados=%d nao_encontrados=%d "
        "sem_tarja=%d sem_exibir_flag=%d preta=%d vermelha=%d",
        st["total"], st["encontrados"], st["nao_encontrados"],
        st["sem_tarja"], st["sem_exibir_flag"], st["tarja_preta"], st["tarja_vermelha"]
    )

    if stats_only:
        cur.close(); conn.close()
        return

    # Baixar CSV
    try:
        csv_rows = download_csv(CSV_URL)
    except Exception as e:
        log.error("Falha ao baixar CSV: %s", e)
        cur.close(); conn.close()
        sys.exit(1)

    index_nome, index_pa = build_index(csv_rows)

    # Modo validação: mostra 10 amostras do cache vs CSV antes de processar
    if validate:
        cur.execute("""
            SELECT ac.id, ac.chave, ac.tarja, ac.exibir_imagem_publica,
                   ac.principio_ativo, ac.encontrado,
                   m.barra AS ean, m.descricao AS nome_produto
            FROM anvisa_cache ac
            LEFT JOIN medicamentos m ON m.id = ac.id_produto
            WHERE ac.encontrado = TRUE
            ORDER BY random()
            LIMIT 10
        """)
        val_rows = [dict(r) for r in cur.fetchall()]
        _print_validation_table(val_rows, index_nome, index_pa)

    # Buscar entradas do cache para processar
    if force:
        cur.execute("""
            SELECT ac.id, ac.chave, ac.tarja, ac.principio_ativo,
                   ac.nome_anvisa, ac.exibir_imagem_publica,
                   m.barra AS ean
            FROM anvisa_cache ac
            LEFT JOIN medicamentos m ON m.id = ac.id_produto
        """)
    else:
        cur.execute("""
            SELECT ac.id, ac.chave, ac.tarja, ac.principio_ativo,
                   ac.nome_anvisa, ac.exibir_imagem_publica,
                   m.barra AS ean
            FROM anvisa_cache ac
            LEFT JOIN medicamentos m ON m.id = ac.id_produto
            WHERE ac.encontrado = FALSE
               OR ac.encontrado IS NULL
               OR ac.tarja IS NULL
               OR ac.principio_ativo IS NULL
               OR ac.exibir_imagem_publica IS NULL
        """)
    cache_rows = cur.fetchall()
    log.info("Entradas do cache para processar: %d", len(cache_rows))

    updated = skipped = matched = corrected = 0

    for entry in cache_rows:
        chave = entry["chave"] or ""

        if chave in _CHAVES_OTC_ISENTO:
            skipped += 1
            continue

        # Matching primário: por chave (nome do produto)
        matches = index_nome.get(chave, [])

        # Matching secundário: por principio_ativo normalizado (strip de forma salina)
        if not matches and entry.get("principio_ativo"):
            pa_cache = entry["principio_ativo"] or ""
            for pa_part in re.split(r"[,;]", pa_cache):
                ch_pa = _chave_pa(pa_part.strip())
                if ch_pa:
                    matches = index_pa.get(ch_pa, [])
                    if matches:
                        break

        if not matches:
            skipped += 1
            continue

        matched += 1
        csv_data = matches[0]
        pa       = csv_data["PRINCIPIO_ATIVO"] or entry.get("principio_ativo") or ""
        nome_anv = csv_data["PRODUTO"]         or entry.get("nome_anvisa")     or ""

        tarja_atual  = entry.get("tarja")
        exibir_atual = entry.get("exibir_imagem_publica")

        tarja_csv, exibir_csv, motivo = _derivar_tarja(csv_data, pa)

        # Hard override: CAP=Sim → tarja preta sempre (substância controlada é fato legal)
        # Hard override: RESTRICAO_HOSPITALAR=Sim → vermelha se não for já preta
        if tarja_csv == "preta":
            novo_tarja  = "preta"
            novo_exibir = False
        elif tarja_csv == "vermelha" and _is_sim(csv_data.get("RESTRICAO_HOSPITALAR")) and tarja_atual != "preta":
            novo_tarja  = "vermelha"
            novo_exibir = False
        else:
            # Soft fill: preenche apenas NULLs, não sobrescreve dados do bulário
            novo_tarja  = tarja_atual  if tarja_atual  is not None else tarja_csv
            novo_exibir = exibir_atual if exibir_atual is not None else exibir_csv

        novo_pa   = entry.get("principio_ativo") or pa or None
        novo_nome = entry.get("nome_anvisa")     or nome_anv or None

        # Detecta correção (mudança de valor existente, não só preenchimento de NULL)
        eh_correcao = (tarja_atual is not None and novo_tarja != tarja_atual)

        if dry_run:
            acao = "CORRIGIR" if eh_correcao else "PREENCHER"
            log.info("[DRY] %s chave=%s ean=%s tarja:%s→%s exibir:%s→%s (%s)",
                     acao, chave, entry.get("ean") or "—",
                     tarja_atual, novo_tarja, exibir_atual, novo_exibir, motivo)
            updated += 1
            if eh_correcao:
                corrected += 1
            continue

        cur.execute("""
            UPDATE anvisa_cache SET
                encontrado            = TRUE,
                tarja                 = %s,
                principio_ativo       = COALESCE(principio_ativo, %s),
                nome_anvisa           = COALESCE(nome_anvisa, %s),
                exibir_imagem_publica = %s
            WHERE id = %s
        """, (novo_tarja, novo_pa, novo_nome, novo_exibir, entry["id"]))

        if eh_correcao:
            corrected += 1
            log.warning("CORREÇÃO: id=%d chave=%s ean=%s tarja: %s → %s (%s)",
                        entry["id"], chave, entry.get("ean") or "—",
                        tarja_atual, novo_tarja, motivo)
        updated += 1

    if not dry_run:
        conn.commit()

    log.info(
        "Concluído: matched=%d updated=%d corrected=%d skipped=%d%s",
        matched, updated, corrected, skipped,
        " (DRY RUN)" if dry_run else ""
    )

    # Stats finais
    cur.execute("""
        SELECT
          COUNT(*) FILTER (WHERE encontrado = TRUE)  AS encontrados,
          COUNT(*) FILTER (WHERE tarja = 'preta')    AS tarja_preta,
          COUNT(*) FILTER (WHERE tarja = 'vermelha') AS tarja_vermelha,
          COUNT(*) FILTER (WHERE tarja IS NULL AND encontrado = TRUE) AS sem_tarja
        FROM anvisa_cache
    """)
    sf = dict(cur.fetchone())
    log.info(
        "Cache pós-sync: encontrados=%d preta=%d vermelha=%d sem_tarja=%d",
        sf["encontrados"], sf["tarja_preta"], sf["tarja_vermelha"], sf["sem_tarja"]
    )

    cur.close()
    conn.close()


def main():
    ap = argparse.ArgumentParser(description="Complementa e valida anvisa_cache com CSV oficial ANVISA")
    ap.add_argument("--dry-run",  action="store_true", help="Simula sem alterar BD")
    ap.add_argument("--force",    action="store_true", help="Processa todos, incluindo já encontrados")
    ap.add_argument("--stats",    action="store_true", help="Só exibe estatísticas do cache")
    ap.add_argument("--validate", action="store_true", help="Exibe tabela de validação de 10 EANs antes de processar")
    args = ap.parse_args()

    start = datetime.now()
    log.info("=== anvisa_csv_sync iniciado %s%s ===",
             start.strftime("%Y-%m-%d %H:%M:%S"),
             " [DRY RUN]" if args.dry_run else "")
    run(dry_run=args.dry_run, force=args.force, stats_only=args.stats, validate=args.validate)
    log.info("=== Concluído em %.1fs ===", (datetime.now() - start).total_seconds())


if __name__ == "__main__":
    main()
