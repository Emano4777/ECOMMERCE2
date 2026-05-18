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
}


def _chave(nome):
    words = []
    for w in re.sub(r"[^\w\s]", " ", nome or "").upper().split():
        if w.lower() in _STOP_WORDS or any(c.isdigit() for c in w) or len(w) < 4:
            continue
        words.append(w)
        if len(words) >= 2:
            break
    return " ".join(words)


def _db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def _salvar(conn, chave, dados):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO anvisa_cache
          (chave, encontrado, nome_anvisa, laboratorio, situacao,
           principio_ativo, url_bula, serve_para, como_usar, alertas,
           id_produto, tarja, jwt_bula, criado_em)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
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
          criado_em       = NOW()
    """, (
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
    ))
    conn.commit()
    cur.close()


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


def main():
    ap = argparse.ArgumentParser(description="Sincronização ANVISA standalone")
    ap.add_argument("--forcar", action="store_true",
                    help="Apaga registros 'não encontrado' e rebusca todos")
    ap.add_argument("--limite", type=int, default=0,
                    help="Processar no máximo N chaves (0 = sem limite, útil para testes)")
    ap.add_argument("--todos", action="store_true",
                    help="Inclui produtos sem indicativo de medicamento (muito mais lento)")
    args = ap.parse_args()

    conn = _db()
    cur  = conn.cursor()

    if args.forcar:
        cur.execute("DELETE FROM anvisa_cache WHERE encontrado=FALSE")
        conn.commit()
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
    cur.execute("SELECT chave FROM anvisa_cache WHERE criado_em > NOW() - INTERVAL '90 days'")
    cached = {r["chave"] for r in cur.fetchall()}
    cur.close()

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
        conn.close()
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

    try:
        proc = subprocess.Popen(
            [sys.executable, worker, "--input", input_path, "--output", output_path],
            stderr=sys.stderr,
            env=env,
        )

        ok = 0
        falha = 0
        feitos = 0

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
                        _salvar(conn, rec["chave"], dados)
                        feitos += 1
                        pct = round(feitos / total * 100)
                        if dados.get("encontrado"):
                            ok += 1
                            print(f"[{feitos:4}/{total}] {pct:3}%  [OK]  {rec['chave']}"
                                  f"  ->  {dados.get('nome_anvisa', '')}")
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

    conn.close()

    print(f"\n{'='*55}")
    print(f"Concluido!  OK={ok} encontrados   NAO={falha} nao encontrados")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()
