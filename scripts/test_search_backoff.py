import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
import search_backoff as b
import classificar_tarjas_ecommerce as m

class BackoffTests(unittest.TestCase):
    def test_monthly_cap_falls_back_and_stays_suspended(self):
        with TemporaryDirectory() as d:
            cfg={'_state_dir':d,'TARJA_SEARCH_PROVIDER':'tavily-serper'}
            calls=[]
            def search(p,c):
                calls.append(c['TARJA_SEARCH_PROVIDER'])
                if calls[-1]=='tavily': raise m.SearchBudgetExhausted('limit')
                return ['source']
            with patch.object(m,'_web_search_provider',side_effect=search):
                self.assertEqual(m.web_search({},cfg),['source'])
                m.web_search({},cfg)
            self.assertEqual(calls,['tavily','serper','serper'])

    def test_cooldown_expires_and_does_not_store_key(self):
        with TemporaryDirectory() as d:
            cfg={'_state_dir':d}; identity=b.key_id('private-key')
            with patch.object(b.time,'time',return_value=100):
                b.record(cfg,identity,HTTPError('url',400,'credits',None,None))
                self.assertFalse(b.ready(cfg,identity))
            self.assertNotIn('private-key',str(b.load(cfg)))
            with patch.object(b.time,'time',return_value=86501):
                self.assertTrue(b.ready(cfg,identity))

    def test_exhausted_serper_not_retried_each_product(self):
        with TemporaryDirectory() as d:
            cfg={'_state_dir':d,'TARJA_SEARCH_PROVIDER':'serper','SERPER_API_KEYS':'one,two'}
            p={'ean':'123','nomes':['produto'],'fabricante':''}
            with patch.object(m,'api_json',side_effect=HTTPError('url',400,'credits',None,None)) as api:
                for _ in range(2):
                    with self.assertRaises(m.SearchBudgetExhausted): m.web_search(p,cfg)
                self.assertEqual(api.call_count,2)
