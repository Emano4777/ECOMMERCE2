import json
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from anthropic_budget_proxy import Ledger, BudgetBlocked, execute

class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();p=Path(self.temp.name)
  self.cfg=p/'config.json';self.cfg.write_text(json.dumps(dict(enabled=True,daily_usd=.1,monthly_usd=.2)))
  self.ledger=Ledger(str(p/'ledger.db'),str(self.cfg))
  self.body={'model':'claude-haiku-4-5-20251001','max_tokens':100,'messages':[{'role':'user','content':'test'}]}
 def tearDown(self):self.temp.cleanup()
 def test_concurrency_cannot_overspend(self):
  def reserve(_):
   try:self.ledger.reserve('test',self.body['model'],30000);return 1
   except BudgetBlocked:return 0
  with ThreadPoolExecutor(8) as pool:self.assertEqual(sum(pool.map(reserve,range(10))),3)
 def test_usage_reconciles_reservation(self):
  def send(path,body,headers):
   return {'input_tokens':100} if path.endswith('count_tokens') else {'id':'msg_test','usage':{'input_tokens':100,'output_tokens':20}}
  execute(self.ledger,'tarjas',self.body,{},send)
  with self.ledger.connect() as c:
   self.assertEqual(c.execute("SELECT cost FROM calls WHERE state='completed'").fetchone()[0],200)
 def test_uncertain_call_keeps_reservation(self):
  def send(path,body,headers):
   if path.endswith('count_tokens'):return {'input_tokens':100}
   raise TimeoutError()
  with self.assertRaises(BudgetBlocked):execute(self.ledger,'test',self.body,{},send)
  with self.ledger.connect() as c:
   self.assertGreater(c.execute("SELECT reserved FROM calls WHERE state='uncertain'").fetchone()[0],0)
 def test_monthly_limit(self):
  self.cfg.write_text(json.dumps(dict(enabled=True,daily_usd=1,monthly_usd=.01)))
  with self.assertRaises(BudgetBlocked):self.ledger.reserve('test',self.body['model'],11000)
 def test_monthly_fifteen_without_daily_cap(self):
  self.cfg.write_text(json.dumps(dict(enabled=True,daily_usd=None,monthly_usd=15)))
  self.ledger.reserve('ecommerce',self.body['model'],14000000)
  self.ledger.reserve('pedido',self.body['model'],1000000)
  with self.assertRaises(BudgetBlocked):self.ledger.reserve('pedido',self.body['model'],1)
 def test_unknown_model_never_calls_api(self):
  with self.assertRaises(BudgetBlocked):execute(self.ledger,'test',dict(self.body,model='unknown'),{},lambda *a:self.fail())
 def test_no_balance_stops_future_calls(self):
  def send(*a):raise HTTPError('url',400,'credit',None,None)
  with self.assertRaises(BudgetBlocked):execute(self.ledger,'test',self.body,{},send)
  with self.assertRaises(BudgetBlocked):execute(self.ledger,'test',self.body,{},lambda *a:self.fail())
 def test_disabled_never_calls_api(self):
  self.cfg.write_text('{"enabled":false}')
  with self.assertRaises(BudgetBlocked):execute(self.ledger,'test',self.body,{},lambda *a:self.fail())
 def test_restart_retains_spending(self):
  self.ledger.reserve('test',self.body['model'],90000)
  other=Ledger(self.ledger.path,self.ledger.config)
  with self.assertRaises(BudgetBlocked):other.reserve('test',self.body['model'],20000)
 def test_missing_usage_keeps_reservation(self):
  def send(path,*args):return {'input_tokens':100} if path.endswith('count_tokens') else {'content':[]}
  execute(self.ledger,'test',self.body,{},send)
  with self.ledger.connect() as c:
   state,cost,reserved=c.execute("SELECT state,cost,reserved FROM calls WHERE error='missing_usage'").fetchone()
   self.assertEqual(state,'uncertain');self.assertIsNone(cost);self.assertGreater(reserved,0)
 def test_over_budget_stops_before_generation(self):
  self.ledger.reserve('test',self.body['model'],99000)
  def send(path,*args):
   self.assertTrue(path.endswith('count_tokens'))
   return {'input_tokens':100}
  with self.assertRaises(BudgetBlocked):execute(self.ledger,'test',self.body,{},send)
 def test_custom_tool_allowed_server_tools_blocked(self):
  with self.assertRaises(BudgetBlocked):
   execute(self.ledger,'test',dict(self.body,tools=[{'type':'web_search_20250305'}]),{},lambda *a:self.fail())
 def test_integrations_preserve_future_and_sdk(self):
  from prepare_integrations import transform
  text='"""Example"""\nfrom __future__ import annotations\nimport anthropic\nurl="https://api.anthropic.com/v1/messages"\nc=anthropic.Anthropic(api_key="test")\n'
  transformed,count=transform(text)
  self.assertEqual(count,2)
  self.assertIn('base_url=_anthropic_budget_endpoint(base=True)',transformed)
  self.assertNotIn('https://api.anthropic.com',transformed)
