#!/usr/bin/env python3
"""
Importa tabela CMED (Lista de Conformidade) para anvisa_cache via EAN.

Fonte: gov.br/anvisa → CMED → Preços → Listas de Preços → Excel Conformidade
Atualiza tarja no anvisa_cache com base no EAN real — sem heurísticas.

Uso:
    python scripts/cmed_tarja_sync.py xls_conformidade_gov_*.xlsx
    python scripts/cmed_tarja_sync.py xls_conformidade_gov_*.xlsx --dry-run
    python scripts/cmed_tarja_sync.py xls_conformidade_gov_*.xlsx --stats

Estrutura do Excel (cabeçalho na linha 53, dados a partir da 54):
    col  0: SUBSTÂNCIA
    col  5: EAN 1
    col  6: EAN 2
    col  7: EAN 3
    col  8: PRODUTO
    col 10: CLASSE TERAPÊUTICA
    col 65: RESTRIÇÃO HOSPITALAR  (Sim/Não)
    col 66: CAP                   (Sim/Não)
    col 72: TARJA
"""

import os, sys, re, argparse, unicodedata, logging
from pathlib import Path
from collections import defaultdict

try:
    import openpyxl
except ImportError:
    sys.exit("Instale openpyxl: pip install openpyxl")

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.exit("Instale psycopg2: pip install psycopg2-binary")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(
        open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
    )],
)
log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("DDATABASE_URL") or ""

# col 72: valores TARJA → (tarja_db, exibir_imagem_publica)
_TARJA_MAP = {
    "TARJA VERMELHA":               ("vermelha", True),
    "TARJA VERMELHA SOB RESTRICAO": ("vermelha", False),
    "TARJA SEM TARJA":              (None,       True),
    "TARJA PRETA":                  ("preta",    False),
}

# Prioridade ao resolver conflito de EAN com múltiplas entradas
_RANK = {"preta": 3, "vermelha": 2, None: 1}


def _norm(s: object) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFD", str(s)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s).strip().upper()


def _ean_clean(v: object) -> str | None:
    s = re.sub(r"[\s\-\*]", "", str(v).strip()) if v else ""
    return s if len(s) >= 8 and s.isdigit() else None


def _parse_tarja(tarja_raw, cap_raw, hosp_raw) -> tuple[str | None, bool | None]:
    """Retorna (tarja, exibir_imagem_publica) ou (None, None) quando indefinido.
    CAP (Componente Especializado) NÃO é tarja preta — são medicamentos de alto custo do SUS.
    A tarja real vem exclusivamente da coluna TARJA (col 72).
    """
    key = _norm(tarja_raw)
    pair = _TARJA_MAP.get(key)
    if pair is None:
        return (None, None)  # '- (*)' ou campo vazio → não sabe

    tarja, exibir = pair
    if _norm(hosp_raw) == "SIM" and tarja == "vermelha":
        exibir = False  # restrição hospitalar → bloqueia imagem
    return (tarja, exibir)


def load_excel(path: Path) -> dict[str, dict]:
    """Retorna índice EAN → {tarja, exibir, produto, substancia}."""
    log.info("Lendo %s ...", path.name)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active

    index: dict[str, dict] = {}
    total_linhas = sem_ean = sem_tarja = inseridos = 0

    for row in ws.iter_rows(min_row=54, values_only=True):
        if not row[0]:
            continue
        total_linhas += 1

        tarja, exibir = _parse_tarja(row[72], row[66], row[65])
        if tarja is None:
            sem_tarja += 1
            continue  # sem tarja definida (indefinido ou "sem tarja") — não altera o cache

        eans = [_ean_clean(row[5]), _ean_clean(row[6]), _ean_clean(row[7])]
        eans = list({e for e in eans if e})
        if not eans:
            sem_ean += 1
            continue

        entry = {
            "tarja":      tarja,
            "exibir":     exibir,
            "produto":    str(row[8]).strip()  if row[8]  else "",
            "substancia": str(row[0]).strip()  if row[0]  else "",
            "classe":     str(row[10]).strip() if row[10] else "",
        }

        for ean in eans:
            existing = index.get(ean)
            if existing is None or _RANK.get(tarja, 0) > _RANK.get(existing["tarja"], 0):
                index[ean] = entry
                inseridos += 1

    wb.close()
    log.info(
        "CMED: %d linhas | %d EANs classificados | %d sem tarja definida | %d sem EAN",
        total_linhas, len(index), sem_tarja, sem_ean,
    )
    return index


def _get_conn():
    dsn = re.sub(r"[?&]sslmode=[^&]*", "", DATABASE_URL)
    return psycopg2.connect(dsn, connect_timeout=30, sslmode="require",
                            cursor_factory=psycopg2.extras.RealDictCursor)


def sync(ean_index: dict, dry_run: bool = False):
    conn = _get_conn()
    cur = conn.cursor()

    # Busca todos os EANs disponíveis no estoque + medicamentos com seu chave no cache
    log.info("Buscando EANs do estoque e cache ...")
    cur.execute("""
        SELECT
            ac.id          AS cache_id,
            ac.chave,
            ac.tarja       AS tarja_atual,
            ac.exibir_imagem_publica AS exibir_atual,
            ac.encontrado,
            LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') AS ean_med
        FROM anvisa_cache ac
        LEFT JOIN medicamentos m ON m.id = ac.id_produto
        WHERE ac.chave IS NOT NULL
    """)
    cache_rows = cur.fetchall()
    log.info("Entradas no cache: %d", len(cache_rows))

    # Também pega EANs do estoque cruzando com o cache por chave (produto sem id_produto)
    cur.execute("""
        SELECT DISTINCT
            LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0') AS ean,
            e.descricao AS nome
        FROM estoque e
        WHERE COALESCE(e.barras_norm, e.barras) IS NOT NULL
          AND TRIM(COALESCE(e.barras_norm, e.barras)) <> ''
        UNION
        SELECT DISTINCT
            LTRIM(ae.ean, '0') AS ean,
            ae.descricao_produto AS nome
        FROM automatiza_estoque ae
        WHERE ae.ean IS NOT NULL AND TRIM(ae.ean) <> ''
    """)
    estoque_rows = cur.fetchall()
    log.info("EANs no estoque: %d", len(estoque_rows))

    # Importa a mesma _anvisa_chave do app (versão mínima local)
    STOP = {
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
        "cloridrato","bromidrato","dicloridrato","hemitartarato","hemifumarato",
        "maleato","fumarato","succinato","besilato","tartarato","monoidratado","monoidratada",
        "hidroclor","medoxomila","flacodin",
        "germed","vitamedic","biolab","globo","greenbios","uniphar",
        "farmax","quimica","bellaphytus","rioquimica","medley","sandoz",
        "torrent","teuto","eurofarma","prati","donaduzzi","neo","geolab",
        "pharlab","pharma","laboratorio","laboratorios","natulab","multilab",
        "airela","pharmascience","biosintetica",
    }

    def _chave(nome: str) -> str:
        tks = re.sub(r"[^\w\s]", " ",
                     unicodedata.normalize("NFD", (nome or "").upper())
                     .encode("ascii", "ignore").decode("ascii")).split()
        words = []
        for w in tks:
            if w.lower() in STOP or any(c.isdigit() for c in w) or len(w) < 4:
                continue
            words.append(w)
            if len(words) >= 2:
                break
        return " ".join(words)

    # Monta índice chave → cache_id para produtos sem EAN linkado
    chave_to_cache: dict[str, list[int]] = defaultdict(list)
    for r in cache_rows:
        if r["chave"]:
            chave_to_cache[r["chave"]].append(r["cache_id"])

    # Monta índice EAN → lista de cache_ids (via id_produto linkado)
    ean_to_cache: dict[str, list[int]] = defaultdict(list)
    for r in cache_rows:
        if r["ean_med"]:
            ean_to_cache[r["ean_med"].lstrip("0")].append(r["cache_id"])

    # cache_id → row completa
    cache_by_id = {r["cache_id"]: r for r in cache_rows}

    # Acumula updates: cache_id → (tarja, exibir, produto, substancia)
    # Resolve conflitos por prioridade (_RANK): preta > vermelha > None
    pending_updates: dict[int, tuple] = {}
    # Acumula inserts: chave → (tarja, exibir, produto, substancia)
    pending_inserts: dict[str, tuple] = {}
    sem_match = 0

    def _nomes_compativeis(nome_estoque: str, cmed_produto: str, cmed_substancia: str) -> bool:
        """True se houver ao menos 1 palavra significativa em comum."""
        def _tks(s):
            s = unicodedata.normalize("NFD", s.upper()).encode("ascii", "ignore").decode("ascii")
            return {w for w in re.sub(r"[^\w]", " ", s).split() if len(w) >= 4 and w not in STOP}
        est_tks = _tks(nome_estoque)
        cmed_tks = _tks(cmed_produto) | _tks(cmed_substancia)
        return bool(est_tks & cmed_tks)

    def _registrar_update(cache_id: int, novo_tarja, novo_exibir, cmed: dict):
        existing = pending_updates.get(cache_id)
        if existing and _RANK.get(existing[0], 0) >= _RANK.get(novo_tarja, 0):
            return  # já tem prioridade igual ou maior
        pending_updates[cache_id] = (novo_tarja, novo_exibir,
                                     cmed["produto"][:200] or None,
                                     cmed["substancia"][:200] or None)

    # Passo 1: acumula via EAN direto (medicamentos linkados ao cache)
    for ean_raw, cmed in ean_index.items():
        ean = ean_raw.lstrip("0")
        for cid in ean_to_cache.get(ean, []):
            row = cache_by_id.get(cid)
            chave_str = row["chave"] if row else ""
            if not _nomes_compativeis(chave_str, cmed["produto"], cmed["substancia"]):
                log.warning("[SKIP-MISMATCH-P1] chave=%r EAN=%s CMED=%r/%r",
                            chave_str, ean, cmed["produto"][:40], cmed["substancia"][:40])
                continue
            _registrar_update(cid, cmed["tarja"], cmed["exibir"], cmed)

    # Passo 2: acumula via EAN do estoque → chave → cache
    for est in estoque_rows:
        ean = (est["ean"] or "").lstrip("0")
        if not ean:
            continue
        cmed = ean_index.get(ean)
        if not cmed:
            sem_match += 1
            continue
        nome_est = est["nome"] or ""
        if not _nomes_compativeis(nome_est, cmed["produto"], cmed["substancia"]):
            log.warning("[SKIP-MISMATCH] EAN=%s estoque=%r CMED=%r/%r",
                        ean, nome_est[:40], cmed["produto"][:40], cmed["substancia"][:40])
            sem_match += 1
            continue
        ch = _chave(nome_est)
        if not ch:
            continue
        for cid in chave_to_cache.get(ch, []):
            _registrar_update(cid, cmed["tarja"], cmed["exibir"], cmed)

    # Passo 3: acumula inserts (novas chaves sem entrada no cache)
    for est in estoque_rows:
        ean = (est["ean"] or "").lstrip("0")
        cmed = ean_index.get(ean)
        if not cmed:
            continue
        nome_est = est["nome"] or ""
        if not _nomes_compativeis(nome_est, cmed["produto"], cmed["substancia"]):
            continue
        ch = _chave(nome_est)
        if not ch or chave_to_cache.get(ch):
            continue
        existing = pending_inserts.get(ch)
        if not existing or _RANK.get(cmed["tarja"], 0) > _RANK.get(existing[0], 0):
            pending_inserts[ch] = (cmed["tarja"], cmed["exibir"],
                                   cmed["produto"][:200] or None,
                                   cmed["substancia"][:200] or None)

    log.info("Pendentes: %d updates, %d inserts", len(pending_updates), len(pending_inserts))

    # --- Flush updates em lotes ---
    atualizados = ja_ok = 0
    _BATCH = 500
    batch: list[tuple] = []

    def _flush_updates():
        nonlocal atualizados
        if not batch:
            return
        if not dry_run:
            psycopg2.extras.execute_batch(cur, """
                UPDATE anvisa_cache SET
                    tarja                 = %(tarja)s,
                    exibir_imagem_publica = %(exibir)s,
                    encontrado            = TRUE,
                    nome_anvisa           = COALESCE(nome_anvisa, %(produto)s),
                    principio_ativo       = COALESCE(principio_ativo, %(substancia)s)
                WHERE id = %(id)s
            """, batch, page_size=_BATCH)
            conn.commit()
        atualizados += len(batch)
        batch.clear()

    for cid, (tarja, exibir, produto, substancia) in pending_updates.items():
        row = cache_by_id.get(cid)
        if not row:
            continue
        if row["tarja_atual"] == tarja and row["exibir_atual"] == exibir:
            ja_ok += 1
            continue
        batch.append({"tarja": tarja, "exibir": exibir,
                      "produto": produto, "substancia": substancia, "id": cid})
        if len(batch) >= _BATCH:
            _flush_updates()

    _flush_updates()

    # --- Flush inserts em lotes ---
    inseridos = 0
    ins_batch: list[tuple] = []

    def _flush_inserts():
        nonlocal inseridos
        if not ins_batch:
            return
        if not dry_run:
            psycopg2.extras.execute_batch(cur, """
                INSERT INTO anvisa_cache (chave, encontrado, tarja, exibir_imagem_publica,
                                          nome_anvisa, principio_ativo)
                VALUES (%(chave)s, TRUE, %(tarja)s, %(exibir)s, %(produto)s, %(substancia)s)
                ON CONFLICT (chave) DO UPDATE SET
                    tarja                 = EXCLUDED.tarja,
                    exibir_imagem_publica = EXCLUDED.exibir_imagem_publica,
                    encontrado            = TRUE,
                    nome_anvisa           = COALESCE(anvisa_cache.nome_anvisa, EXCLUDED.nome_anvisa),
                    principio_ativo       = COALESCE(anvisa_cache.principio_ativo, EXCLUDED.principio_ativo)
                WHERE anvisa_cache.tarja IS NULL
            """, ins_batch, page_size=_BATCH)
            conn.commit()
        inseridos += len(ins_batch)
        ins_batch.clear()

    for ch, (tarja, exibir, produto, substancia) in pending_inserts.items():
        ins_batch.append({"chave": ch, "tarja": tarja, "exibir": exibir,
                          "produto": produto, "substancia": substancia})
        if len(ins_batch) >= _BATCH:
            _flush_inserts()

    _flush_inserts()

    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE tarja = 'preta')    AS preta,
            COUNT(*) FILTER (WHERE tarja = 'vermelha') AS vermelha,
            COUNT(*) FILTER (WHERE tarja IS NULL AND encontrado) AS sem_tarja,
            COUNT(*) FILTER (WHERE encontrado = TRUE)  AS encontrados
        FROM anvisa_cache
    """)
    stats = dict(cur.fetchone())

    log.info(
        "Concluído%s: atualizados=%d inseridos=%d ja_ok=%d sem_match=%d",
        " (DRY RUN)" if dry_run else "",
        atualizados, inseridos, ja_ok, sem_match,
    )
    log.info("Cache final: %s", stats)

    cur.close()
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Importa CMED para anvisa_cache via EAN")
    parser.add_argument("arquivo", help="Excel CMED (xls_conformidade_gov_*.xlsx)")
    parser.add_argument("--dry-run", action="store_true", help="Simula sem alterar BD")
    parser.add_argument("--stats", action="store_true", help="Só mostra estatísticas do cache")
    args = parser.parse_args()

    if args.stats:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT tarja, COUNT(*) FROM anvisa_cache
            WHERE encontrado = TRUE GROUP BY tarja ORDER BY COUNT(*) DESC
        """)
        for r in cur.fetchall():
            print(f"  {str(r['tarja']):15} {r['count']}")
        cur.close(); conn.close()
        return

    path = Path(args.arquivo)
    if not path.exists():
        sys.exit(f"Arquivo não encontrado: {path}")

    ean_index = load_excel(path)
    sync(ean_index, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
