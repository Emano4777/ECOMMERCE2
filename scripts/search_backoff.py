"""Circuit breaker persistente: sem segredos nos identificadores/logs."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError

def key_id(key):
    return 'serper:'+hashlib.sha256(key.encode()).hexdigest()[:16]

def load(cfg):
    path=Path(cfg['_state_dir'])/'search_backoff.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

def ready(cfg, name):
    row=load(cfg).get(name,{})
    return time.time()>=row.get('retry_at',0)

def record(cfg, name, error=None, monthly=False):
    data=load(cfg)
    if error is None:
        data.pop(name,None)
    else:
        delay=86400 if isinstance(error,HTTPError) and error.code in (400,401,402,403) else 3600 if isinstance(error,HTTPError) and error.code==429 else 900
        if monthly:
            now=datetime.now(timezone.utc)
            retry=datetime(now.year+(now.month==12),1 if now.month==12 else now.month+1,1,tzinfo=timezone.utc).timestamp()
        else: retry=time.time()+delay
        data[name]={'retry_at':retry,'reason':type(error).__name__,'http_status':getattr(error,'code',None)}
    path=Path(cfg['_state_dir'])/'search_backoff.json'
    tmp=path.with_suffix('.tmp'); tmp.write_text(json.dumps(data,indent=2),encoding='utf-8'); tmp.replace(path)
