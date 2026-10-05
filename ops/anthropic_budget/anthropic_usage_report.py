"""Read-only usage report; amounts are estimated USD, not billing statements."""
import argparse,json,sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

p=argparse.ArgumentParser()
p.add_argument('--db',default='/root/scripts/anthropic_budget/usage.sqlite3')
p.add_argument('--month',default=datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%Y-%m'))
a=p.parse_args()
c=sqlite3.connect('file:'+a.db+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
try:
    rows=c.execute('''SELECT routine,model,state,count(*) calls,
       sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,
       round(sum(coalesce(cost,reserved))/1000000.0,6) estimated_or_reserved_usd
       FROM calls WHERE month=? AND error IS NOT 'preflight'
       GROUP BY routine,model,state ORDER BY estimated_or_reserved_usd DESC''',(a.month,)).fetchall()
    blocks=c.execute('SELECT * FROM blocks WHERE day LIKE ? ORDER BY day,routine',(a.month+'%',)).fetchall()
    print(json.dumps({'usage':[dict(r) for r in rows],'blocked':[dict(r) for r in blocks]},ensure_ascii=False,indent=2))
finally:c.close()
