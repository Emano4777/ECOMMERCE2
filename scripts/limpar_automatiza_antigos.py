#!/usr/bin/env python3
"""
Limpeza periódica das tabelas automatiza_* no Supabase.

Executa 3 fases em ordem:
  1. automatiza_estoque  — deleta registros antigos, mantém só o mais recente
                          por (cnpj_loja, ean) para todos os CNPJs.
  2. automatiza_entradas — deleta registros com mais de 30 dias.
  3. automatiza_import_lotes — deleta lotes sem nenhum filho válido
                               (sem estoque atual, sem vendas, sem entradas,
                               sem usuarios). A FK CASCADE limpa o que restar.

NUNCA toca em automatiza_vendas.

Uso:
  python scripts/limpar_automatiza_antigos.py            # executa
  python scripts/limpar_automatiza_antigos.py --dry-run  # simula, sem deletar
"""
from __future__ import annotations

import os
import sys
import argparse
import logging
from datetime import datetime

import psycopg2
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def get_conn():
    dsn = os.getenv("DATABASE_URL") or os.getenv("DDATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL não configurada.")
    # psycopg2 pode rejeitar sslmode=require na query string — forçar via kwarg
    import re as _re
    dsn_clean = _re.sub(r'[?&]sslmode=[^&]*', '', dsn)
    return psycopg2.connect(dsn_clean, connect_timeout=30, sslmode="require")


def _count(cur, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def fase1_estoque(cur, dry_run: bool) -> int:
    """Mantém só o registro mais recente por (cnpj_loja, ean). Retorna deleted."""
    sql_count = """
        SELECT COUNT(*)
        FROM automatiza_estoque ae
        WHERE ean IS NOT NULL
          AND ae.id NOT IN (
              SELECT DISTINCT ON (cnpj_loja, ean) id
              FROM automatiza_estoque
              WHERE ean IS NOT NULL
              ORDER BY cnpj_loja, ean, recebido_em DESC
          )
    """
    cur.execute(sql_count)
    to_delete = cur.fetchone()[0]
    log.info("Fase 1 — estoque: %d registros antigos a remover", to_delete)

    if dry_run or to_delete == 0:
        return to_delete

    sql_delete = """
        DELETE FROM automatiza_estoque
        WHERE ean IS NOT NULL
          AND id NOT IN (
              SELECT DISTINCT ON (cnpj_loja, ean) id
              FROM automatiza_estoque
              WHERE ean IS NOT NULL
              ORDER BY cnpj_loja, ean, recebido_em DESC
          )
    """
    cur.execute(sql_delete)
    deleted = cur.rowcount
    log.info("Fase 1 — estoque: %d registros deletados", deleted)
    return deleted


def fase2_entradas(cur, dry_run: bool) -> int:
    """Deleta entradas com mais de 30 dias. Retorna deleted."""
    sql_count = """
        SELECT COUNT(*) FROM automatiza_entradas
        WHERE recebido_em < NOW() - INTERVAL '30 days'
    """
    cur.execute(sql_count)
    to_delete = cur.fetchone()[0]
    log.info("Fase 2 — entradas >30d: %d registros a remover", to_delete)

    if dry_run or to_delete == 0:
        return to_delete

    cur.execute(
        "DELETE FROM automatiza_entradas WHERE recebido_em < NOW() - INTERVAL '30 days'"
    )
    deleted = cur.rowcount
    log.info("Fase 2 — entradas >30d: %d registros deletados", deleted)
    return deleted


def fase3_lotes(cur, dry_run: bool) -> int:
    """
    Deleta lotes que não têm mais nenhum filho (estoque atual, vendas,
    entradas recentes ou usuarios). A FK CASCADE limpa qualquer resíduo
    nas tabelas filhas — mas vendas = 0 nesses lotes (garantido pela query).
    Retorna deleted.
    """
    sql_count = """
        SELECT COUNT(*) FROM automatiza_import_lotes l
        WHERE
          NOT EXISTS (SELECT 1 FROM automatiza_estoque  ae WHERE ae.lote_id = l.id)
          AND NOT EXISTS (SELECT 1 FROM automatiza_vendas   v  WHERE v.lote_id  = l.id)
          AND NOT EXISTS (SELECT 1 FROM automatiza_entradas en WHERE en.lote_id = l.id)
          AND NOT EXISTS (SELECT 1 FROM automatiza_usuarios  u  WHERE u.lote_id  = l.id)
    """
    cur.execute(sql_count)
    to_delete = cur.fetchone()[0]
    log.info("Fase 3 — lotes órfãos: %d a remover", to_delete)

    if dry_run or to_delete == 0:
        return to_delete

    sql_delete = """
        DELETE FROM automatiza_import_lotes
        WHERE
          NOT EXISTS (SELECT 1 FROM automatiza_estoque  ae WHERE ae.lote_id = automatiza_import_lotes.id)
          AND NOT EXISTS (SELECT 1 FROM automatiza_vendas   v  WHERE v.lote_id  = automatiza_import_lotes.id)
          AND NOT EXISTS (SELECT 1 FROM automatiza_entradas en WHERE en.lote_id = automatiza_import_lotes.id)
          AND NOT EXISTS (SELECT 1 FROM automatiza_usuarios  u  WHERE u.lote_id  = automatiza_import_lotes.id)
    """
    cur.execute(sql_delete)
    deleted = cur.rowcount
    log.info("Fase 3 — lotes órfãos: %d deletados", deleted)
    return deleted


def main():
    parser = argparse.ArgumentParser(description="Limpa tabelas automatiza_* antigas")
    parser.add_argument("--dry-run", action="store_true", help="Simula sem deletar")
    args = parser.parse_args()

    if args.dry_run:
        log.info("=== DRY RUN — nenhum dado será alterado ===")

    start = datetime.now()
    log.info("=== Iniciando limpeza automatiza — %s ===", start.strftime("%Y-%m-%d %H:%M:%S"))

    conn = get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                before_estoque = _count(cur, "automatiza_estoque")
                before_entradas = _count(cur, "automatiza_entradas")
                before_lotes = _count(cur, "automatiza_import_lotes")

                d1 = fase1_estoque(cur, args.dry_run)
                d2 = fase2_entradas(cur, args.dry_run)
                d3 = fase3_lotes(cur, args.dry_run)

                if args.dry_run:
                    log.info("DRY RUN — rollback (nada alterado)")
                    conn.rollback()
                else:
                    after_estoque = _count(cur, "automatiza_estoque")
                    after_entradas = _count(cur, "automatiza_entradas")
                    after_lotes = _count(cur, "automatiza_import_lotes")

                    log.info(
                        "Resultado: estoque %d→%d (-%d) | entradas %d→%d (-%d) | lotes %d→%d (-%d)",
                        before_estoque, after_estoque, d1,
                        before_entradas, after_entradas, d2,
                        before_lotes, after_lotes, d3,
                    )

        elapsed = (datetime.now() - start).total_seconds()
        log.info("=== Concluído em %.1fs ===", elapsed)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
