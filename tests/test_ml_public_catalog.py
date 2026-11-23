import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import ml_public_catalog as catalog


class NationalCatalogTests(unittest.TestCase):
    def setUp(self):
        self.item = {'id':'MLB123','seller_id':catalog.SELLER,'status':'active','sub_status':[],
                     'available_quantity':3,'price':49.99,'currency_id':'BRL','title':'Kit Ricosol FPS 70',
                     'permalink':'https://produto.mercadolivre.com.br/MLB-123-kit',
                     'pictures':[{'secure_url':'https://http2.mlstatic.com/product.jpg'}],
                     'category_id':'MLB199648','attributes':[{'id':'GTIN','value_name':'1234567890123'}]}

    def test_only_available_own_products_with_safe_customer_links(self):
        self.assertIsNotNone(catalog.public_item(self.item))
        for change in ({'status':'paused'}, {'status':'closed'}, {'available_quantity':0},
                       {'seller_id':'999'}, {'sub_status':['deleted']}, {'price':float('nan')},
                       {'permalink':'https://mercadolivre.com.br.evil.test/item'}, {'pictures':[]}):
            self.assertIsNone(catalog.public_item({**self.item, **change}))
        self.assertIsNone(catalog.public_item({**self.item,'attributes':[{'id':'GTIN','value_name':'7897230302454'}]}))

    def test_search_by_name_category_and_ean_without_location(self):
        product = catalog.public_item(self.item)
        for query in ['Ricosol', 'protetores solares', '1234567890123']:
            self.assertEqual(catalog.matching([product],query),[product])
        self.assertEqual(catalog.matching([product],'perfume'),[])

    def test_cron_snapshot_and_public_reads_never_call_ml(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'catalog.json'
            calls=[]
            def api(path,token):
                calls.append(path)
                return [{'code':200,'body':self.item},{'code':200,'body':{**self.item,'id':'MLB456'}}]
            count=catalog.refresh_catalog(lambda:['MLB123','MLB456'],lambda **kw:'secret',api,path)
            self.assertEqual(count,1)
            self.assertNotIn('secret',path.read_text())
            self.assertEqual(len(catalog.read_catalog('ricosol',path)),1)
            self.assertEqual(len(calls),1)
            before=path.read_text()
            with self.assertRaises(RuntimeError):
                catalog.refresh_catalog(lambda:['MLB123'],lambda **kw:'secret',lambda *a:None,path)
            self.assertEqual(path.read_text(),before)
            with patch.object(catalog.time,'time',return_value=json.loads(before)['updated_at']+601):
                self.assertEqual(catalog.read_catalog('',path),[])
            self.assertEqual(catalog.read_catalog('',Path(directory)/'missing'),[])
