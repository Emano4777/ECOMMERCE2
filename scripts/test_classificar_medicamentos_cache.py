"""Valida respostas e selecao sem SDK, credenciais ou chamadas pagas."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

tree=ast.parse(Path(__file__).with_name('classificar_medicamentos.py').read_text(encoding='utf-8'))
selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('classify_batch','fetch_products')]
ns={'json':json,'MODEL':'test','SYSTEM_PROMPT':'test',
    'TIPOS_VALIDOS':{'generico','referencia','similar','varejo'},
    'psycopg2':SimpleNamespace(extras=SimpleNamespace(RealDictCursor=object))}
exec(compile(ast.Module(body=selected,type_ignores=[]),'classifier','exec'),ns)

class CacheTests(unittest.TestCase):
    def test_single_ean_respects_saved_data_and_uses_parameters(self):
        conn=MagicMock()
        ns['fetch_products'](conn,ean_filter="00123'",only_estoque=True)
        sql,params=conn.cursor.return_value.__enter__.return_value.execute.call_args.args
        self.assertIn('DISTINCT ON',sql)
        self.assertIn('NOT EXISTS',sql)
        self.assertNotIn("123'",sql)
        self.assertEqual(params,["123'"])

    def classify(self,results):
        client=MagicMock()
        client.messages.create.return_value.content=[SimpleNamespace(text=json.dumps(results))]
        return ns['classify_batch'](client,[dict(barra_norm='12345678',descricao='Produto',classe='',laboratorio='',marca='')])

    def test_foreign_ean_rejected(self):
        with self.assertRaises(ValueError):
            self.classify([dict(barra_norm='99999999',tipo='generico',confianca='alta')])

    def test_duplicate_ean_rejected(self):
        row=dict(barra_norm='12345678',tipo='generico',confianca='alta')
        with self.assertRaises(ValueError): self.classify([row,row])

    def test_invalid_type_is_not_replaced_with_retail(self):
        with self.assertRaises(ValueError):
            self.classify([dict(barra_norm='12345678',tipo='inventado',confianca='alta')])
