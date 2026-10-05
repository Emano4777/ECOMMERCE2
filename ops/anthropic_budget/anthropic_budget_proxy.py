"""Local-only Anthropic gateway. No prompts or API keys are persisted.

Rates: https://platform.claude.com/docs/en/about-claude/pricing (2026-10-05).
Amounts in integer microdollars; reservations survive errors/restarts.
"""
import argparse
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

RATES = {
    'claude-haiku-4-5-20251001': (1, 5),
    'claude-haiku-4-5': (1, 5),
    'claude-sonnet-4-20250514': (3, 15),
    'claude-sonnet-4-5-20250929': (3, 15),
    'claude-sonnet-4-6': (3, 15),
    'claude-sonnet-5': (2, 10),
}

class BudgetBlocked(Exception): pass

class Ledger:
    def __init__(self,path,config):
        self.path=path; self.config=config
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('''CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, created REAL, day TEXT, month TEXT, routine TEXT,
                model TEXT, state TEXT, reserved INTEGER, cost INTEGER,
                input_tokens INTEGER, output_tokens INTEGER, cache_write INTEGER,
                cache_read INTEGER, request_id TEXT, error TEXT)''')
            c.execute('CREATE TABLE IF NOT EXISTS pauses (name TEXT PRIMARY KEY, until REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS blocks (day TEXT,routine TEXT,reason TEXT,count INTEGER,PRIMARY KEY(day,routine,reason))')

    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=10)
        try:
            with c: yield c
        finally:c.close()

    def settings(self):
        with open(self.config,encoding='utf-8') as f: cfg=json.load(f)
        if not cfg.get('enabled'): raise BudgetBlocked('anthropic_disabled')
        for key in ('daily_usd','monthly_usd'):
            if key=='daily_usd' and cfg.get(key) is None: continue
            if not isinstance(cfg.get(key),(int,float)) or not math.isfinite(cfg[key]) or cfg[key]<0:
                raise BudgetBlocked('budget_not_configured')
        return cfg

    def reserve(self,routine,model,amount):
        cfg=self.settings(); now=datetime.now(ZoneInfo('America/Sao_Paulo'))
        day=now.date().isoformat(); month=day[:7]; ident=uuid.uuid4().hex
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM pauses WHERE until>?',(time.time(),)).fetchone():
                raise BudgetBlocked('provider_cooldown')
            for column,value,key in [('day',day,'daily_usd'),('month',month,'monthly_usd')]:
                if cfg.get(key) is None: continue
                used=c.execute(f'SELECT coalesce(sum(coalesce(cost,reserved)),0) FROM calls WHERE {column}=?',(value,)).fetchone()[0]
                if used+amount>math.floor(cfg[key]*1000000): raise BudgetBlocked(key+'_exhausted')
            c.execute('INSERT INTO calls(id,created,day,month,routine,model,state,reserved) VALUES(?,?,?,?,?,?,?,?)',
                      (ident,time.time(),day,month,routine,model,'reserved',amount))
        return ident

    def settle(self,ident,data):
        usage=data.get('usage')
        if not isinstance(usage,dict) or 'input_tokens' not in usage or 'output_tokens' not in usage:
            self.failed(ident,'missing_usage',False); return
        with self.connect() as c:
            model=c.execute('SELECT model FROM calls WHERE id=?',(ident,)).fetchone()[0]
            inp,out=RATES[model]
            i=usage['input_tokens']; o=usage['output_tokens']
            cache=usage.get('cache_creation',{}) or {}
            cw=usage.get('cache_creation_input_tokens',0); cr=usage.get('cache_read_input_tokens',0)
            cache_cost=(cache.get('ephemeral_5m_input_tokens',0)*1.25+cache.get('ephemeral_1h_input_tokens',0)*2)*inp
            if not cache: cache_cost=cw*2*inp  # conservative if API omits TTL breakdown
            cost=math.ceil(i*inp+o*out+cache_cost+cr*inp*.1)
            c.execute('UPDATE calls SET state=?,cost=?,input_tokens=?,output_tokens=?,cache_write=?,cache_read=?,request_id=? WHERE id=?',
                      ('completed',cost,i,o,cw,cr,data.get('id'),ident))

    def failed(self,ident,reason,definitive=False):
        with self.connect() as c:
            c.execute('UPDATE calls SET state=?,error=?,cost=? WHERE id=?',('rejected' if definitive else 'uncertain',reason,0 if definitive else None,ident))

    def pause(self,seconds):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO pauses VALUES(?,?)',('provider',time.time()+seconds))

    def blocked(self,routine,reason):
        day=datetime.now(ZoneInfo('America/Sao_Paulo')).date().isoformat()
        with self.connect() as c:
            c.execute('INSERT INTO blocks VALUES(?,?,?,1) ON CONFLICT(day,routine,reason) DO UPDATE SET count=count+1',(day,routine,reason))

def request_json(path,body,headers):
    req=Request('https://api.anthropic.com'+path,data=json.dumps(body).encode(),headers=headers,method='POST')
    with urlopen(req,timeout=90) as response: return json.load(response)

def execute(ledger,routine,body,headers,send=request_json):
    model=body.get('model'); maximum=body.get('max_tokens')
    ledger.settings()
    if model not in RATES: raise BudgetBlocked('unpriced_model')
    if type(maximum) is not int or not 0<maximum<=64000: raise BudgetBlocked('invalid_output_limit')
    if body.get('stream') or body.get('service_tier') not in (None,'standard') or body.get('inference_geo') not in (None,'global'):
        raise BudgetBlocked('unsupported_billing_mode')
    if any(t.get('type') not in (None,'custom') for t in body.get('tools',[])):
        raise BudgetBlocked('server_tools_need_explicit_budget')
    # Preflight checks budgets/cooldown before even the free count endpoint.
    ident=ledger.reserve(routine,model,0)
    try:
        count_body={k:body[k] for k in ('model','messages','system','tools','tool_choice','thinking') if k in body}
        count=send('/v1/messages/count_tokens',count_body,headers)['input_tokens']
        if type(count) is not int or not 0<=count<=180000:
            ledger.failed(ident,'unsupported_context_size',True)
            raise BudgetBlocked('unsupported_context_size')
        inp,out=RATES[model]
        amount=math.ceil((count*1.15+1024)*inp*2 + maximum*out)
        ledger.failed(ident,'preflight',True)
        ident=ledger.reserve(routine,model,amount)
        data=send('/v1/messages',body,headers)
        ledger.settle(ident,data)
        return data
    except HTTPError as exc:
        definitive=400<=exc.code<500
        ledger.failed(ident,'http_'+str(exc.code),definitive)
        # Do not persist upstream error text (may contain request content).
        if exc.code in (400,401,402,403): ledger.pause(86400)
        elif exc.code==429: ledger.pause(3600)
        raise BudgetBlocked('upstream_http_'+str(exc.code)) from None
    except BudgetBlocked:
        raise
    except Exception:
        ledger.failed(ident,'network_or_response_error',False)
        ledger.pause(900)
        raise BudgetBlocked('upstream_uncertain') from None

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def reply(self,status,data):
        payload=json.dumps(data).encode()
        self.send_response(status); self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(payload))); self.end_headers()
        try:self.wfile.write(payload)
        except (BrokenPipeError,ConnectionResetError):pass
    def do_GET(self):
        self.reply(200 if self.path=='/health' else 404,{'status':'ok' if self.path=='/health' else 'not_found'})
    def do_POST(self):
        import re
        match=re.fullmatch(r'/([A-Za-z0-9_.-]{1,180})/v1/messages(?:\?beta=true)?',self.path)
        if not match: self.reply(404,{'error':{'type':'not_found','message':'unsupported_endpoint'}}); return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=32*1024*1024: raise BudgetBlocked('invalid_body_size')
            headers={k:self.headers[k] for k in ('x-api-key','anthropic-version','anthropic-beta') if k in self.headers}
            headers['Content-Type']='application/json'
            if not headers.get('x-api-key'): raise BudgetBlocked('missing_key')
            data=execute(self.server.ledger,match[1],json.loads(self.rfile.read(length)),headers)
            self.reply(200,data)
        except BudgetBlocked as exc:
            self.server.ledger.blocked(match[1],str(exc))
            self.reply(400,{'type':'error','error':{'type':'invalid_request_error','message':'Budget guard: '+str(exc)}})
        except Exception:
            self.reply(400,{'type':'error','error':{'type':'invalid_request_error','message':'Budget guard unavailable'}})

def main():
    p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--config',required=True);p.add_argument('--port',type=int,default=8769)
    a=p.parse_args();server=ThreadingHTTPServer(('127.0.0.1',a.port),Handler)
    server.ledger=Ledger(a.db,a.config);server.serve_forever()

if __name__=='__main__':main()
