"""
cosmos_sync.py — Padronização de nomes de produtos via Bluesoft Cosmos + Claude AI.

Uso:
    python cosmos_sync.py              # processa apenas os pendentes
    python cosmos_sync.py --forcar     # reprocessa tudo (exceto manual)
    python cosmos_sync.py --limite 50  # processa no máximo 50 EANs (teste)
    python cosmos_sync.py --so-ia      # roda IA nos que o Cosmos não encontrou
    python cosmos_sync.py --sem-cosmos # pula Cosmos, vai direto para IA
    python cosmos_sync.py --workers 10 # threads paralelas (padrão: 8)

Variáveis de ambiente necessárias:
    DATABASE_URL       — conexão PostgreSQL
    COSMOS_TOKEN       — token Bluesoft Cosmos (grátis em cosmos.bluesoft.com.br)
    ANTHROPIC_API_KEY  — chave Claude API (para fase IA)
"""
import sys
import os
import re
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import psycopg2
import psycopg2.extras
import requests

DATABASE_URL    = os.environ["DATABASE_URL"]
COSMOS_TOKEN    = os.environ.get("COSMOS_TOKEN", "")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

_COSMOS_URL     = "https://api.cosmos.bluesoft.com.br/gtins/{ean}"
_COSMOS_HEADERS = {
    "X-Cosmos-Token": COSMOS_TOKEN,
    "User-Agent":     "Cosmos-API-Request",
    "Content-Type":   "application/json",
}

_BATCH_IA       = 25   # nomes por chamada ao Claude
_SAVE_EVERY     = 50   # commit a cada N resultados (evita perda em falha)

# ── banco ────────────────────────────────────────────────────

def _db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def _ensure_schema(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS produto_canon (
            ean               TEXT PRIMARY KEY,
            descricao_original TEXT,
            descricao_canon   TEXT NOT NULL,
            laboratorio       TEXT,
            fonte             TEXT DEFAULT 'manual',
            criado_em         TIMESTAMPTZ DEFAULT NOW(),
            atualizado_em     TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("ALTER TABLE produto_canon ADD COLUMN IF NOT EXISTS descricao_original TEXT")
    cur.execute("ALTER TABLE produto_canon ADD COLUMN IF NOT EXISTS laboratorio TEXT")
    cur.execute("ALTER TABLE produto_canon ADD COLUMN IF NOT EXISTS categoria TEXT")
    cur.execute("ALTER TABLE produto_canon ADD COLUMN IF NOT EXISTS imagem_cosmos TEXT")
    conn.commit()
    cur.close()


def _salvar_lote(conn, lock, itens):
    """itens: list of (ean, descricao_original, descricao_canon, laboratorio, categoria, imagem_cosmos, fonte)"""
    if not itens:
        return
    with lock:
        cur = conn.cursor()
        psycopg2.extras.execute_values(cur, """
            INSERT INTO produto_canon
                (ean, descricao_original, descricao_canon, laboratorio, categoria, imagem_cosmos, fonte, criado_em, atualizado_em)
            VALUES %s
            ON CONFLICT (ean) DO UPDATE SET
                descricao_original = EXCLUDED.descricao_original,
                descricao_canon    = EXCLUDED.descricao_canon,
                laboratorio        = COALESCE(EXCLUDED.laboratorio, produto_canon.laboratorio),
                categoria          = COALESCE(EXCLUDED.categoria, produto_canon.categoria),
                imagem_cosmos      = COALESCE(EXCLUDED.imagem_cosmos, produto_canon.imagem_cosmos),
                fonte              = EXCLUDED.fonte,
                atualizado_em      = NOW()
        """, itens, template="(%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())", page_size=100)
        conn.commit()
        cur.close()


# ── Cosmos API ───────────────────────────────────────────────

_rate_lock       = threading.Lock()
_last_req_ts     = 0.0
_pause_until     = 0.0    # global: qualquer 429 bloqueia todas as threads
_consec_429      = 0       # 429s consecutivos sem nenhum sucesso
_quota_exhausted = False   # True → para de bater na API
_MIN_INTERVAL    = 0.35    # ~3 req/s global
_thread_local    = threading.local()


def _cosmos_session():
    s = getattr(_thread_local, "cosmos_session", None)
    if s is None:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
        s.mount("https://", adapter)
        s.headers.update(_COSMOS_HEADERS)
        _thread_local.cosmos_session = s
    return s


def _acquire_slot():
    """Bloqueia até conseguir um slot de requisição. Thread-safe, respeita _pause_until."""
    global _last_req_ts, _pause_until
    while True:
        with _rate_lock:
            now = time.monotonic()
            # mais tarde entre: fim da pausa global e fim do intervalo mínimo
            earliest = max(_pause_until, _last_req_ts + _MIN_INTERVAL)
            wait = earliest - now
            if wait <= 0:
                _last_req_ts = now   # atômico: só toma o slot se realmente livre
                return
        time.sleep(max(wait, 0.01))


_COSMOS_ABORT_AFTER = 16  # 429s consecutivos sem sucesso → cota esgotada


def _buscar_cosmos(ean: str):
    """Retorna (nome, lab, cat, img, found)."""
    global _pause_until, _last_req_ts, _consec_429, _quota_exhausted
    if not COSMOS_TOKEN or _quota_exhausted:
        return None, None, None, None, False

    for tentativa in range(8):
        if _quota_exhausted:
            return None, None, None, None, False
        _acquire_slot()
        try:
            r = _cosmos_session().get(_COSMOS_URL.format(ean=ean), timeout=6)
            if r.status_code in (200, 404):
                with _rate_lock:
                    _consec_429 = 0  # qualquer resposta válida reseta o contador
            if r.status_code == 200:
                data  = r.json()
                desc  = (data.get("description") or data.get("descricao") or "").strip()
                brand = (((data.get("brand") or {}).get("name")) or "").strip()
                gpc   = (((data.get("gpc")   or {}).get("description")) or "").strip()
                thumb = (data.get("thumbnail") or "").strip()
                lab   = _titulo(brand) if brand else None
                cat   = _gpc_categoria(gpc)
                img   = thumb if thumb.startswith("http") else None
                if desc and len(desc) > 3:
                    return _titulo(desc), lab, cat, img, True
                return None, None, None, None, False
            if r.status_code == 404:
                return None, None, None, None, False
            if r.status_code == 429:
                backoff = min(2 ** tentativa * 2.0, 64.0)
                with _rate_lock:
                    _consec_429 += 1
                    if _consec_429 >= _COSMOS_ABORT_AFTER and not _quota_exhausted:
                        _quota_exhausted = True
                        print(
                            f"\n  [!] {_consec_429} erros 429 consecutivos — "
                            "cota Cosmos esgotada. Pulando para fase IA.\n"
                            "      (amanhã rode novamente para pegar o restante)\n",
                            flush=True,
                        )
                    deadline = time.monotonic() + backoff
                    _pause_until = max(_pause_until, deadline)
                    _last_req_ts = _pause_until  # descarta slots pré-alocados
                if not _quota_exhausted:
                    print(f"  [429] rate limit — aguardando {backoff:.0f}s", flush=True)
                return None, None, None, None, False  # desiste desta tentativa
            return None, None, None, None, False
        except Exception:
            time.sleep(1)

    return None, None, None, None, False


def _gpc_categoria(gpc_desc: str) -> str | None:
    """Mapeia descrição GPC do Cosmos para nossas categorias internas."""
    if not gpc_desc:
        return None
    g = gpc_desc.lower()
    if any(x in g for x in ['suplemento', 'vitamina', 'mineral', 'protein', 'whey', 'aminoacido', 'bcaa', 'omega']):
        return 'suplemento'
    if any(x in g for x in ['medicament', 'farmac', 'antibiot', 'analges', 'antiinf', 'remedio',
                              'solucao injetavel', 'comprimido', 'capsula', 'xarope', 'pomada oftalm',
                              'colirio', 'supositorio']):
        return 'medicamento'
    if any(x in g for x in ['cosmet', 'maquiag', 'perfum', 'esmalte', 'batom', 'base maquiag',
                              'higiene pessoal', 'sabonete', 'shampoo', 'condicionador',
                              'creme', 'locao', 'protetor solar', 'dermo', 'skin']):
        return 'cosmetico'
    return None


def _titulo(s: str) -> str:
    """Title case respeitando palavras curtas comuns em português."""
    # remove parênteses com conteúdo curto/ruído: (l), (L), (g), (1), (v2), etc.
    s = re.sub(r'\(\s*[a-zA-Z0-9]{1,3}\s*\)', '', s)
    # remove parênteses que sobram vazios ou com só espaços
    s = re.sub(r'\(\s*\)', '', s)
    s = re.sub(r'\s{2,}', ' ', s).strip()
    MINUSC = {"de","da","do","das","dos","e","em","a","o","as","os","para","com","por","ou"}
    partes = s.lower().split()
    return " ".join(
        p if (i > 0 and p in MINUSC) else p.capitalize()
        for i, p in enumerate(partes)
    )


# ── Claude AI em batch ───────────────────────────────────────

def _normalizar_ia(lote: list) -> dict:
    """
    lote: list of (ean, descricao_original)
    Retorna {ean: descricao_normalizada}
    """
    if not ANTHROPIC_KEY:
        return {}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

        nomes = "\n".join(f"{i+1}. {desc}" for i, (_, desc) in enumerate(lote))

        prompt = f"""Você é um especialista em normalização de nomes de produtos farmacêuticos e de saúde/beleza para ecommerce brasileiro.

Abaixo estão {len(lote)} nomes de produtos conforme cadastrados pelas farmácias (podem ter abreviações, siglas, nomes de fabricante, formatos variados).

Normalize cada nome para um formato limpo, legível e padronizado em português brasileiro:
- Use Title Case (ex: "Dipirona Sódica 500mg Comprimido")
- Mantenha dosagem/concentração (ex: "500mg", "10ml", "1%")
- Mantenha forma farmacêutica quando presente (ex: "Comprimido", "Xarope", "Creme", "Cápsula")
- Remova: códigos internos, siglas de fabricante, abreviações como "CPR", "CPS", "TAB", "FR", "AMP" isoladas
- Expanda abreviações conhecidas: "SOL" → "Solução", "COMP" → "Comprimido", "CAP" → "Cápsula", "CREM" → "Creme"
- Se o nome já estiver bom, mantenha como está
- Retorne APENAS os {len(lote)} nomes normalizados, um por linha, na mesma ordem, sem numeração e sem explicações

Nomes:
{nomes}"""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )

        linhas = msg.content[0].text.strip().split("\n")
        resultado = {}
        for i, (ean, _) in enumerate(lote):
            if i < len(linhas):
                nome = linhas[i].strip()
                nome = re.sub(r"^\d+[\.\)]\s+", "", nome).strip()
                nome = re.sub(r"^[-•]\s+", "", nome).strip()
                if nome:
                    resultado[ean] = nome
        return resultado
    except Exception as exc:
        print(f"  [IA ERR] {exc}", file=sys.stderr, flush=True)
        return {}


# ── main ─────────────────────────────────────────────────────

def main():
    global _MIN_INTERVAL
    ap = argparse.ArgumentParser(description="Sincronização Cosmos/IA de nomes de produtos")
    ap.add_argument("--forcar",     action="store_true",
                    help="Reprocessa produtos já em produto_canon (exceto manual)")
    ap.add_argument("--limite",     type=int, default=0,
                    help="Processar no máximo N EANs (0 = sem limite)")
    ap.add_argument("--so-ia",      action="store_true",
                    help="Só roda IA nos que o Cosmos já marcou como não encontrado")
    ap.add_argument("--sem-cosmos", action="store_true",
                    help="Pula Cosmos, processa tudo via IA")
    ap.add_argument("--workers",    type=int, default=8,
                    help="Threads paralelas para o Cosmos (padrão: 8)")
    ap.add_argument("--save-every", type=int, default=_SAVE_EVERY,
                    help="Quantidade de resultados para salvar por commit (padrao: 50)")
    ap.add_argument("--rate",       type=float, default=_MIN_INTERVAL,
                    help="Intervalo global minimo entre chamadas Cosmos em segundos (padrao: 0.35)")
    args = ap.parse_args()
    save_every = max(10, min(int(args.save_every or _SAVE_EVERY), 500))
    _MIN_INTERVAL = max(0.05, float(args.rate or _MIN_INTERVAL))

    conn = _db()
    _ensure_schema(conn)
    db_lock = threading.Lock()
    cur = conn.cursor()

    if args.forcar:
        cur.execute("DELETE FROM produto_canon WHERE fonte != 'manual'")
        conn.commit()
        print("Cache não-manual apagado.\n")

    cur.execute("SELECT barra_norm FROM medicamentos WHERE barra_norm IS NOT NULL")
    eans_anvisa = {r["barra_norm"] for r in cur.fetchall()}

    cur.execute("SELECT ean, fonte FROM produto_canon")
    ja_feitos = {r["ean"]: r["fonte"] for r in cur.fetchall()}

    cur.execute("""
        SELECT DISTINCT
            COALESCE(barras_norm, barras)   AS ean,
            MAX(descricao)                  AS descricao
        FROM estoque
        WHERE estoque > 0
          AND COALESCE(barras_norm, barras) IS NOT NULL
          AND LENGTH(COALESCE(barras_norm, barras)) >= 7
        GROUP BY COALESCE(barras_norm, barras)

        UNION

        SELECT DISTINCT
            ean,
            MAX(descricao_produto) AS descricao
        FROM automatiza_estoque
        WHERE quantidade_estoque > 0
          AND ean IS NOT NULL
          AND LENGTH(ean) >= 7
        GROUP BY ean
    """)
    todos = {r["ean"]: (r["descricao"] or "").strip() for r in cur.fetchall() if r["ean"]}
    cur.close()
    conn.commit()  # libera locks das tabelas antes de iniciar chamadas externas

    if args.so_ia:
        pendentes = {
            ean: todos[ean]
            for ean, fonte in ja_feitos.items()
            if fonte == "cosmos_miss" and ean in todos
        }
    else:
        pendentes = {
            ean: desc
            for ean, desc in todos.items()
            if ean not in eans_anvisa
            and ean not in ja_feitos
            and desc
        }

    total = len(pendentes)
    print(f"EANs no estoque          : {len(todos)}")
    print(f"Cobertos pelo ANVISA     : {len(eans_anvisa)}")
    print(f"Já em produto_canon      : {len(ja_feitos)}")
    print(f"Pendentes nesta execução : {total}")

    if args.limite and total > args.limite:
        pendentes = dict(list(pendentes.items())[:args.limite])
        total = args.limite
        print(f"(limitado a {total} por --limite)")

    if not total:
        print("\nNada a processar. Tudo atualizado.")
        conn.close()
        return

    cosmos_ok   = 0
    cosmos_miss = 0
    ia_ok       = 0
    ia_miss     = 0
    fila_ia: list = []
    counter_lock = threading.Lock()


    # ── Fase 1: Cosmos em paralelo ───────────────────────────
    if not args.sem_cosmos and not args.so_ia:
        if not COSMOS_TOKEN:
            print("\nCOSMOS_TOKEN não configurado — pulando Cosmos.\n")
            fila_ia = list(pendentes.items())
        else:
            workers = min(args.workers, total, 16)
            print(f"\n{'='*60}")
            print(f"Fase 1: Bluesoft Cosmos API  ({total} EANs, {workers} threads)\n")

            itens_lista = list(pendentes.items())
            progresso = [0]

            def processar_ean(item):
                ean, desc = item
                nome, lab, cat, img, found = _buscar_cosmos(ean)
                return ean, desc, nome, lab, cat, img, found

            buffer = []

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(processar_ean, item): item for item in itens_lista}
                for fut in as_completed(futures):
                    ean, desc, nome, lab, cat, img, found = fut.result()
                    with counter_lock:
                        progresso[0] += 1
                        i = progresso[0]
                        pct = round(i / total * 100)
                        if found:
                            cosmos_ok += 1
                            buffer.append((ean, desc, nome, lab, cat, img, "cosmos"))
                            lab_txt = f"  [{lab}]" if lab else ""
                            print(f"[{i:5}/{total}] {pct:3}%  [OK ]  {ean}  →  {nome}{lab_txt}", flush=True)
                        else:
                            cosmos_miss += 1
                            fila_ia.append((ean, desc))
                            buffer.append((ean, desc, desc, None, None, None, "cosmos_miss"))
                            print(f"[{i:5}/{total}] {pct:3}%  [--]  {ean}  ({desc[:50]})", flush=True)

                        if len(buffer) >= save_every:
                            _salvar_lote(conn, db_lock, buffer)
                            buffer.clear()

            if buffer:
                _salvar_lote(conn, db_lock, buffer)
    else:
        fila_ia = list(pendentes.items())

    # ── Fase 2: Claude AI em batch ────────────────────────────
    if fila_ia:
        if not ANTHROPIC_KEY:
            print("\nANTHROPIC_API_KEY não configurado — pulando IA.")
        else:
            print(f"\n{'='*60}")
            print(f"Fase 2: Claude AI  ({len(fila_ia)} produto(s) em lotes de {_BATCH_IA})\n")
            for offset in range(0, len(fila_ia), _BATCH_IA):
                lote = fila_ia[offset:offset + _BATCH_IA]
                resultado = _normalizar_ia(lote)
                batch_salvar = []
                for ean, desc_orig in lote:
                    nome_ia = resultado.get(ean)
                    if nome_ia and nome_ia.strip() != desc_orig.strip():
                        batch_salvar.append((ean, desc_orig, nome_ia, None, None, None, "ia"))
                        ia_ok += 1
                        print(f"  [IA OK]  {ean}  →  {nome_ia}", flush=True)
                    else:
                        batch_salvar.append((ean, desc_orig, desc_orig, None, None, None, "ia_miss"))
                        ia_miss += 1
                        print(f"  [IA --]  {ean}  (manteve original)", flush=True)
                _salvar_lote(conn, db_lock, batch_salvar)
                time.sleep(1.2)

    conn.close()
    print(f"\n{'='*60}")
    print(f"Cosmos : {cosmos_ok:5} encontrados   {cosmos_miss:5} não encontrados")
    print(f"IA     : {ia_ok:5} normalizados  {ia_miss:5} sem melhoria")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
