import gzip
import os
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch
from automatiza_channels import book_price
from automatiza_sync import _preco_calc, _controlled, _export_reference, _validate_filial
from automatiza_fiscal import authorized_xml
from automatiza_ml_sync import is_virtual_bundle
from automatiza_sync import _pedido_payload


class DirectedPriceTests(unittest.TestCase):
    def test_ml_discounts_match_registered_prices(self):
        for base, discount, expected in [(72.99,45.21,"39.99"),(89.99,44.45,"49.99"),(44.99,11.11,"39.99")]:
            self.assertEqual(book_price(dict(preco_venda=base,precValor=discount,precFormacao="DESCONTO",precQtde_Minima=1)),Decimal(expected))

    def test_fixed_price_does_not_apply_multi_buy_to_one_unit(self):
        self.assertEqual(book_price(dict(precValor="52.99",precFormacao="PRECO_FIXO",precQtde_Minima=1)),Decimal("52.99"))
        for qty,value in [(2,10),(1,0)]:
            self.assertIsNone(book_price(dict(precValor=value,precFormacao="PRECO_FIXO",precQtde_Minima=qty)))

    def test_unsupported_formula_fails_closed(self):
        with self.assertRaises(ValueError):
            book_price(dict(precValor=10,precFormacao="MARKUP",precQtde_Minima=1))

    def test_delivery_price_can_exceed_list(self):
        self.assertEqual(_preco_calc(dict(preco_venda=10,preco_venda_delivery=12)),(12,None,12))

    def test_nonempty_n_does_not_mean_controlled(self):
        for value in ["N","NAO","0",None]:
            self.assertFalse(_controlled(value))
        self.assertTrue(_controlled("S"))

    def test_stable_external_reference(self):
        with patch.dict(os.environ,{"AUTOMATIZA_FULL_REFERENCE":"1"}):
            self.assertEqual(_export_reference(dict(id="abcdef00-other",ml_order_id="20000123")),"ML20000123")
            self.assertEqual(_export_reference(dict(id="abcdef00-other")),"abcdef00-other")

    def test_virtual_bundle_is_not_a_unit_even_when_api_omits_bundle_body(self):
        self.assertTrue(is_virtual_bundle({"tags":["bundle"],"bundle":None}))
        self.assertFalse(is_virtual_bundle({"tags":["kit_component"]}))

    def test_wrong_filial_is_rejected(self):
        cur=Mock();cur.fetchall.return_value=[{"codigo_filial":1,"cpnj":"64369730000147"}]
        with self.assertRaises(RuntimeError):
            _validate_filial(cur,"54185432000143")


class FiscalTests(unittest.TestCase):
    def xml(self,cnpj="54185432000143",status="100",env="1"):
        key="1"*44
        return (f'<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe{key}"><ide><mod>55</mod><nNF>1</nNF><tpAmb>{env}</tpAmb></ide><emit><CNPJ>{cnpj}</CNPJ></emit></infNFe></NFe><protNFe><infProt><chNFe>{key}</chNFe><cStat>{status}</cStat></infProt></protNFe></nfeProc>').encode()

    def test_authorized_gzip_xml(self):
        text,key,number,model=authorized_xml(gzip.compress(self.xml()),"54185432000143")
        self.assertEqual((key,number,model),("1"*44,"1","55"))

    def test_wrong_store_cancelled_and_test_xml_rejected(self):
        for xml in [self.xml(cnpj="64369730000147"),self.xml(status="101"),self.xml(env="2")]:
            with self.assertRaises(ValueError):
                authorized_xml(xml,"54185432000143")


class MercadoLivrePackTests(unittest.TestCase):
    def test_paid_components_share_one_erp_total_and_group(self):
        cur=Mock()
        cur.fetchone.return_value={"id":"a","ml_pack_id":"900","ml_pack_order_ids":["101","102"],"total":10}
        cur.fetchall.side_effect=[[
            {"id":"a","ml_order_id":"101","ml_shipping_id":"500","pagamento_status":"approved","status":"pago","total":10},
            {"id":"b","ml_order_id":"102","ml_shipping_id":"500","pagamento_status":"approved","status":"pago","total":20},
        ],[{"codigo_mercadoria":1,"ean":"789","qty":1}]]
        order,items=_pedido_payload(cur,"a","54185432000143")
        self.assertEqual(order["total"],30)
        self.assertEqual(order["_erp_group_ids"],["a","b"])
        with patch.dict(os.environ,{"AUTOMATIZA_FULL_REFERENCE":"1"}):
            self.assertEqual(_export_reference(order),"MLP900")

    def test_incomplete_pack_waits_before_any_export(self):
        cur=Mock()
        cur.fetchone.return_value={"id":"a","ml_pack_id":"900","ml_pack_order_ids":["101","102"]}
        cur.fetchall.return_value=[{"id":"a","ml_order_id":"101","ml_shipping_id":"500","total":10}]
        with self.assertRaisesRegex(RuntimeError,"incompleto"):
            _pedido_payload(cur,"a","54185432000143")
