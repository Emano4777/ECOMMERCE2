import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import openpyxl
import classificar_tarjas_ecommerce as m

class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.p={'ean':'7896181915171','nomes':['BUSONID 32 MCG 120 DOSES'],'fabricante':'ACHE','cmed_tarjas_explicitas':['vermelha']}
        self.r=dict(produto='BUSONID',apresentacao='32 MCG SUS NAS 120 DOSES',principio_ativo='BUDESONIDA',laboratorio='ACHE',tarja='vermelha')

    def test_no_source_is_not_otc(self):
        self.assertIsNone(m.decide_cmed(self.p,[])[0])

    def test_ambiguous_requires_other_source(self):
        self.assertIsNone(m.decide_cmed(self.p,[self.r,{**self.r,'tarja':None}])[0])

    def test_red_and_otc_conflict(self):
        self.assertIsNone(m.decide_cmed(self.p,[self.r,{**self.r,'tarja':'sem_tarja'}])[0])

    def test_exact_presentation(self):
        self.assertEqual(m.decide_cmed(self.p,[self.r])[0]['tarja'],'vermelha')

    def test_wrong_dose_rejected(self):
        self.assertIsNone(m.decide_cmed({**self.p,'nomes':['BUSONID 64 MCG 120 DOSES']},[self.r])[0])

    def test_conflicting_store_names_rejected(self):
        self.assertIsNone(m.decide_cmed({**self.p,'nomes':self.p['nomes']+['CORUS 25 MG']},[self.r])[0])

    def test_dynamic_sheet_and_columns(self):
        with TemporaryDirectory() as d:
            p=Path(d)/'test.xlsx'; w=openpyxl.Workbook(); s=w.active; s.append(['Capa'])
            s=w.create_sheet('Nova aba'); s.append(['APRESENTACAO','EAN 2','LABORATORIO','TARJA','SUBSTANCIA','PRODUTO'])
            s.append(['32 MCG','7896181915171','ACHE','Tarja Vermelha','BUDESONIDA','BUSONID']); w.save(p)
            self.assertEqual(m.load_cmed(p,1)[self.p['ean']][0]['tarja'],'vermelha')
            with self.assertRaises(ValueError): m.load_cmed(p)

    def online(self,quote='VENDA SOB PRESCRICAO',tarja='vermelha'):
        identity='BUSONID 32 MCG 120 DOSES ACHE'
        doc={'text':identity+' '+quote,'url':'https://www.ache.com.br/produto/busonid','sha256':'a'}
        out=dict(tarja=tarja,mesma_apresentacao=True,conflito=False,documento=0,identidade_citada=identity,classificacao_citada=quote)
        return doc,out

    def test_online_real_quote(self):
        d,o=self.online(); self.assertEqual(m.validate_online(self.p,[d],o)[0]['tarja'],'vermelha')

    def test_hallucinated_quote(self):
        d,o=self.online(); o['classificacao_citada']='ISENTO DE PRESCRICAO';o['tarja']='sem_tarja'
        self.assertIsNone(m.validate_online(self.p,[d],o)[0])

    def test_retailer_rejected(self):
        d,o=self.online(); d['url']='https://www.drogasil.com.br/busonid'
        self.assertIsNone(m.validate_online(self.p,[d],o)[0])

    def test_no_warning_is_not_otc(self):
        d,o=self.online('CONSULTE SEU MEDICO','sem_tarja')
        self.assertIsNone(m.validate_online(self.p,[d],o)[0])

    def test_retention_is_not_black(self):
        d,o=self.online('VENDA SOB PRESCRICAO COM RETENCAO','preta')
        self.assertIsNone(m.validate_online(self.p,[d],o)[0])

    def test_invalid_urls(self):
        for url in ['http://www.ache.com.br','https://ache.com.br.evil.test','https://localhost/test','https://127.0.0.1','https://user@ache.com.br']:
            self.assertFalse(m.primary_url(url))

    def test_ean_padding(self):
        self.assertEqual(m.ean_key('07896181915171'),self.p['ean'])
        self.assertIsNone(m.ean_key('00000000'))
        self.assertIsNone(m.ean_key('123'))

    def test_prescription_alone_does_not_prove_red(self):
        d,o=self.online()
        self.assertIsNone(m.validate_online({**self.p,'cmed_tarjas_explicitas':[]},[d],o)[0])

    def test_explicit_otc_can_restore_image_classification(self):
        d,o=self.online('MEDICAMENTO ISENTO DE PRESCRICAO','sem_tarja')
        p={**self.p,'cmed_tarjas_explicitas':['sem_tarja']}
        self.assertEqual(m.validate_online(p,[d],o)[0]['tarja'],'sem_tarja')

    def test_source_cannot_override_conflicting_cmed(self):
        d,o=self.online('MEDICAMENTO ISENTO DE PRESCRICAO','sem_tarja')
        self.assertIsNone(m.validate_online(self.p,[d],o)[0])

    def test_tavily_stops_at_1000_even_with_bonus_or_paid_limit(self):
        usage={'account':{'plan_usage':1000,'plan_limit':1500},'key':{'usage':1000,'limit':None}}
        with self.assertRaises(m.SearchBudgetExhausted): m.tavily_allowance(usage,0)

    def test_tavily_timeout_reservations_also_count(self):
        usage={'account':{'plan_usage':10,'plan_limit':1500},'key':{'usage':10,'limit':None}}
        with self.assertRaises(m.SearchBudgetExhausted): m.tavily_allowance(usage,1000)

    def test_tavily_last_credit_and_provider_limit(self):
        usage={'account':{'plan_usage':999,'plan_limit':1500},'key':{'usage':20,'limit':None}}
        self.assertEqual(m.tavily_allowance(usage,15),1)
        usage['key']['limit']=20
        with self.assertRaises(m.SearchBudgetExhausted): m.tavily_allowance(usage,15)

    def test_unknown_usage_blocks_paid_search(self):
        with self.assertRaises(RuntimeError): m.tavily_allowance({},0)

if __name__=='__main__': unittest.main()
