"""Aplica um manifesto revisado por EAN, com backup e sem alterar famílias.

Uso: python scripts/aplicar_revisao_tarja_ean.py manifesto.json [--apply]
Ausência na CMED não determina isenção. A fonte de cada registro é obrigatória.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re

from dotenv import dotenv_values
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifesto',type=Path)
    parser.add_argument('--env',type=Path,default=Path('.env'))
    parser.add_argument('--backup-dir',type=Path,default=Path('exports'))
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    entries=json.loads(args.manifesto.read_text(encoding='utf-8'))['entries']
    seen=set()
    for r in entries:
        assert re.fullmatch(r'[0-9]{8,14}',r['ean'])
        assert r['ean'] not in seen
        seen.add(r['ean'])
        assert r['tarja'] in ('sem_tarja','vermelha','preta')
        assert r['fonte_url'].startswith('https://') and r['nome_anvisa']
    if not args.apply:
        print(json.dumps(dict(validated=len(entries),apply=False)))
        return
    cfg=dotenv_values(args.env)
    with psycopg2.connect(cfg.get('DATABASE_URL') or cfg.get('DDATABASE_URL') or os.environ.get('DATABASE_URL'),connect_timeout=20,cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='5s'")
            cur.execute("SET LOCAL statement_timeout='90s'")
            keys=['EAN:'+r['ean'].lstrip('0') for r in entries]
            cur.execute('SELECT * FROM anvisa_cache WHERE chave=ANY(%s)',(keys,))
            before=[dict(r) for r in cur.fetchall()]
            args.backup_dir.mkdir(parents=True,exist_ok=True)
            backup=args.backup_dir/('anvisa_ean_before_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.json')
            backup.write_text(json.dumps(dict(keys=keys,rows=before),ensure_ascii=False,default=str,indent=2),encoding='utf-8')
            # Fonte e apresentação ficam acessíveis no próprio cache, além do manifesto.
            cur.execute('ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS fonte_classificacao_url TEXT')
            cur.execute('ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS apresentacao_verificada TEXT')
            values=[]
            for r in entries:
                t=r['tarja'];is_otc=t=='sem_tarja'
                values.append(('EAN:'+r['ean'].lstrip('0'),True,r['nome_anvisa'],r['laboratorio'],r['principio_ativo'],t,t,'oficial_ean',True,is_otc,r['receita_retida'],r['fonte_url'],r['nome_anvisa'],r['fonte'], 'Isento de prescrição' if is_otc else 'Venda sob prescrição'))
            execute_values(cur,'''INSERT INTO anvisa_cache
                (chave,encontrado,nome_anvisa,laboratorio,principio_ativo,tarja,tarja_ia,
                 tarja_ia_confianca,override_manual,exibir_imagem_publica,receita_retida,
                 fonte_classificacao_url,apresentacao_verificada,situacao,dizeres_receita)
                VALUES %s ON CONFLICT(chave) DO UPDATE SET
                 encontrado=EXCLUDED.encontrado,nome_anvisa=EXCLUDED.nome_anvisa,
                 laboratorio=EXCLUDED.laboratorio,principio_ativo=EXCLUDED.principio_ativo,
                 tarja=EXCLUDED.tarja,tarja_ia=EXCLUDED.tarja_ia,
                 tarja_ia_confianca=EXCLUDED.tarja_ia_confianca,override_manual=TRUE,
                 exibir_imagem_publica=EXCLUDED.exibir_imagem_publica,
                 receita_retida=EXCLUDED.receita_retida,
                 fonte_classificacao_url=EXCLUDED.fonte_classificacao_url,
                 apresentacao_verificada=EXCLUDED.apresentacao_verificada,
                 situacao=EXCLUDED.situacao,dizeres_receita=EXCLUDED.dizeres_receita''',values,page_size=250)
            cur.execute('SELECT count(*) AS n FROM anvisa_cache WHERE chave=ANY(%s) AND override_manual=TRUE',(keys,))
            assert cur.fetchone()['n']==len(keys)
        conn.commit()
    print(json.dumps(dict(applied=len(entries),backup=str(backup))))


if __name__=='__main__':main()
