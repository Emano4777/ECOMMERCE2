import ast
from pathlib import Path
import re
import unittest
from unittest.mock import MagicMock


class ApresentacaoTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name('app_patched.py')
        if not path.exists():
            path = Path(__file__).resolve().parents[1] / 'app.py'
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        names = {'_anvisa_chave_ean', '_anvisa_cache_ean', '_detectar_tarja', '_marcar_tarja_batch'}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.ns = dict(re=re, _anvisa_schema=lambda: None,
                       _anvisa_chave=lambda name: 'VITAMINA',
                       _TIPOS_NAO_MEDICAMENTO={'suplemento', 'cosmetico'},
                       _CHAVES_OTC_ISENTO={'VITAMINA'},
                       _vitamina_d_dose_alta=lambda name: False,
                       _ibuprofeno_dose_alta=lambda name: False,
                       _classificar_produto=lambda name: 'suplemento',
                       _exige_receita_digital_entrega=lambda *a: False,
                       _looks_like_other_pharmacy_brand=lambda url: False,
                       _placeholder_for_tarja=lambda tarja: 'placeholder-' + tarja,
                       _forma_injetavel=lambda name: None, _forma_solida_oral=lambda name: None)
        exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),self.ns)

    def run_batch(self, products, records):
        conn=MagicMock()
        cur=conn.cursor.return_value
        def execute(sql,args=None):
            if 'SELECT chave FROM' in sql:
                cur.fetchall.return_value=[{'chave':r['chave']} for r in records]
            elif 'WHERE chave = ANY' in sql:
                cur.fetchall.return_value=records
            elif 'SPLIT_PART' in sql:
                raise AssertionError('Apresentações exatas não devem consultar a família')
        cur.execute.side_effect=execute
        return self.ns['_marcar_tarja_batch'](products,conn,ensure_schema=False)

    def record(self,ean,tarja,exibir=None):
        return dict(chave='EAN:'+ean,override_manual=True,tarja=tarja,tarja_ia=tarja,
                    exibir_imagem_publica=exibir if exibir is not None else tarja=='sem_tarja',receita_retida=False)

    def test_dose_alta_classificada_como_suplemento_bloqueia(self):
        p=dict(ean='7896112403241',nome='VITAMINA D3 7000UI 12CPS',categoria='suplemento',imagem='embalagem.jpg')
        r=self.run_batch([p],[self.record(p['ean'],'vermelha')])[0]
        self.assertEqual(r['imagem'],'placeholder-vermelha')
        self.assertTrue(r['imagem_bloqueada_anvisa'])
        self.assertEqual(r['tarja'],'vermelha')

    def test_suplemento_nao_herda_tarja_de_vitamina_medicamento(self):
        p=dict(ean='7896112402541',nome='VITAMINA D3+ZINCO',categoria='suplemento',imagem='suplemento.jpg')
        r=self.run_batch([p],[self.record(p['ean'],'sem_tarja')])[0]
        self.assertIsNone(r['tarja'])
        self.assertEqual(r['imagem'],'suplemento.jpg')
        self.assertTrue(r['exibir_imagem_publica'])

    def test_mip_do_fornecedor_nao_libera_ean_oficial_tarjado(self):
        p=dict(ean='7896112403241',nome='VITAMINA D3',categoria='medicamento',classificacao='MIPs',imagem='foto.jpg')
        r=self.run_batch([p],[self.record(p['ean'],'vermelha',True)])[0]
        self.assertEqual(r['imagem'],'placeholder-vermelha')

    def test_apresentacoes_diferentes_na_mesma_lista(self):
        ps=[dict(ean=e,nome='VITAMINA D3',categoria='suplemento',imagem=e+'.jpg') for e in ('7896112403241','7896112402541')]
        out=self.run_batch(ps,[self.record(ps[0]['ean'],'vermelha'),self.record(ps[1]['ean'],'sem_tarja')])
        self.assertEqual(out[0]['tarja'],'vermelha')
        self.assertIsNone(out[1]['tarja'])

    def test_ausencia_de_ean_verificado_nao_inventa_classificacao(self):
        p=dict(ean='7890000000000',nome='VITAMINA D3',categoria='suplemento',imagem='foto.jpg')
        r=self.run_batch([p],[])[0]
        self.assertNotIn('tarja',r)
        self.assertEqual(r['imagem'],'foto.jpg')

    def test_ean_padding_e_validacao(self):
        f=self.ns['_anvisa_chave_ean']
        self.assertEqual(f('07896112403241'),f('7896112403241'))
        for e in ('123','7E12','00000000',None):
            self.assertEqual(f(e),'')

    def test_lookup_detalhe_exige_revisao_confirmada(self):
        cur=MagicMock();cur.fetchone.return_value=self.record('7896112402541','sem_tarja')
        r=self.ns['_anvisa_cache_ean'](cur,'7896112402541')
        self.assertIn('override_manual=TRUE',cur.execute.call_args.args[0])
        self.assertEqual(r['tarja_ia'],'sem_tarja')

if __name__=='__main__':unittest.main()
