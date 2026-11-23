"""HostGator-only additional recipient for new Mercado Livre sales."""
import json
import os
import urllib.request

request = urllib.request.Request(
    'http://127.0.0.1:8002/api/cron/ml-owner-sales', data=b'{}', method='POST',
    headers={'Authorization': 'Bearer ' + os.environ['ML_WA_CRON_SECRET'], 'Content-Type': 'application/json'})
with urllib.request.urlopen(request, timeout=240) as response:
    result = json.load(response)
print(json.dumps(result))
if not result.get('ok'):
    raise SystemExit(1)
