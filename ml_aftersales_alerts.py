"""Separate after-sales outbox; never reads or writes the paid-sale alert tables."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ml_order_alerts import STORE, SELLER, PHONE, parse_date

LOCK = 83501375635
BASE = '/post-purchase/v1/claims'


def seller_player(claim):
    return next((p for p in claim.get('players') or []
                 if str(p.get('user_id')) == SELLER and p.get('role') == 'respondent'), None)


def snapshot(claim, returns):
    """Only case state and seller actions, with update time for new case activity."""
    player = seller_player(claim) or {}
    data = {k: claim.get(k) for k in ('id', 'type', 'status', 'stage', 'reason_id',
                                     'last_updated', 'resolution', 'resource', 'resource_id')}
    data['actions'] = player.get('available_actions') or []
    data['returns'] = [{k: r.get(k) for k in ('id', 'status', 'status_money', 'last_updated')}
                       | {'shipping': {k: (r.get('shipping') or {}).get(k) for k in ('id', 'status')}}
                       for r in returns]
    return data


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def message(claim, returns, order=None):
    kind = {'return': 'Devolução', 'mediations': 'Reclamação', 'ml_case': 'Caso de pós-venda',
            'cancel_sale': 'Cancelamento', 'cancel_purchase': 'Cancelamento',
            'change': 'Troca', 'fulfillment': 'Ocorrência de entrega'}.get(claim.get('type'), 'Pós-venda')
    status = {'opened': 'Aberto', 'closed': 'Encerrado'}.get(claim.get('status'), claim.get('status') or 'Confira no painel')
    stage = {'claim': 'Atendimento entre comprador e vendedor', 'dispute': 'Mediação do Mercado Livre',
             'recontact': 'Caso retomado', 'stale': 'Em análise pelo Mercado Livre'}.get(claim.get('stage'))
    lines = ['⚠️ Pós-venda — Mercado Livre', 'Loja São Pedro', f'{kind} #{claim["id"]}', f'Situação: {status}']
    if stage:
        lines.append(f'Etapa: {stage}')
    reason = str(claim.get('reason_id') or '')
    reason_text = {'PNR': 'Produto não recebido', 'PDD': 'Produto diferente ou com problema',
                   'CS': 'Compra cancelada'}.get(reason.split('-')[0], reason)
    if reason_text:
        lines.append(f'Motivo informado pelo ML: {reason_text}')
    if order:
        lines += [f'Venda #{order["id"]}', '', 'Produtos da venda:']
        for item in order.get('order_items') or []:
            title = ' '.join(str((item.get('item') or {}).get('title') or 'Produto').split())[:200]
            lines.append(f'• {item.get("quantity", 1)} un. — {title}')
        lines.append('Confira no caso quais itens e quantidades foram afetados.')
    for ret in returns:
        state = {'opened': 'Solicitada', 'shipped': 'A caminho', 'delivered': 'Entregue',
                 'closed': 'Encerrada', 'cancelled': 'Cancelada', 'not_delivered': 'Não entregue',
                 'failed': 'Falhou', 'expired': 'Prazo expirado'}.get(ret.get('status'), ret.get('status') or 'Confira no painel')
        lines.append(f'Devolução #{ret.get("id", "—")}: {state}')
        money = {'retained': 'Valor retido', 'refunded': 'Reembolso ao comprador',
                 'available': 'Valor disponível'}.get(ret.get('status_money'))
        if money:
            lines.append(f'Financeiro da devolução: {money}')
    actions = (seller_player(claim) or {}).get('available_actions') or []
    deadlines = [parse_date(a.get('due_date')) for a in actions if a.get('mandatory')]
    deadlines = [d for d in deadlines if d]
    if deadlines:
        deadline = min(deadlines).astimezone(ZoneInfo('America/Sao_Paulo')).strftime('%d/%m/%Y às %H:%M (Brasília)')
        lines += ['', f'Prazo de ação/resposta informado pelo ML: {deadline}']
    elif claim.get('status') == 'opened':
        lines += ['', 'Confira no painel se há ação pendente e o prazo para responder.']
    lines += ['', 'Abra o Mercado Livre para consultar e tratar o caso.']
    if order:
        lines.append(f'https://www.mercadolivre.com.br/vendas/{order["id"]}/detalhe')
    else:
        lines += [f'Recurso: {claim.get("resource")} #{claim.get("resource_id")}',
                  'https://vendedores.mercadolivre.com.br/vendas/omni/lista']
    return '\n'.join(lines)


def collect(api_get, token, activated_at):
    claims = {}
    for status in ('opened', 'closed'):
        offset = 0
        while True:
            params = {'players.user_id': SELLER, 'players.role': 'respondent', 'status': status,
                      'limit': 30, 'offset': offset, 'sort': 'last_updated:desc'}
            if status == 'closed':
                # Retain a generous window for return progress after a case closes.
                since = max(activated_at, datetime.now(timezone.utc) - timedelta(days=90))
                params['range'] = 'last_updated:after:' + since.isoformat(timespec='milliseconds')
            result = api_get(BASE + '/search?' + urlencode(params), token)
            if not isinstance(result, dict) or not isinstance(result.get('data'), list) or 'paging' not in result:
                raise RuntimeError('Falha ao consultar pós-venda')
            for claim in result['data']:
                if claim.get('id'):
                    claims[str(claim['id'])] = claim
            offset += 30
            if offset >= int(result['paging']['total']):
                break
            if offset >= 3000:
                raise RuntimeError('Volume de pós-venda excedeu o limite de paginação')
    return claims


def run(conn, get_token, api_get, send):
    token = get_token(cnpjloja=STORE)
    conn.commit()
    if not token or str((api_get('/users/me', token) or {}).get('id')) != SELLER:
        raise RuntimeError('Conta ML indisponível ou divergente no pós-venda')
    with conn.cursor() as cur:
        # Transaction-scoped locks also work through transaction-mode poolers.
        cur.execute('SELECT pg_try_advisory_xact_lock(%s) AS locked', (LOCK,))
        locked = cur.fetchone()['locked']
    if not locked:
        conn.rollback()
        return {'ok': True, 'busy': True}

    class Transaction:
        def cursor(self):
            return conn.cursor()

        def commit(self):
            pass

    try:
        result = _run(Transaction(), token, api_get, send)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def _run(conn, token, api_get, send):
    with conn.cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS ml_wa_aftersales_config (
            seller_id TEXT PRIMARY KEY, activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_success_at TIMESTAMPTZ)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS ml_wa_aftersales_events (
            seller_id TEXT NOT NULL, claim_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            body TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            sent_at TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            PRIMARY KEY(seller_id, claim_id, fingerprint))''')
        cur.execute('INSERT INTO ml_wa_aftersales_config(seller_id) VALUES (%s) ON CONFLICT DO NOTHING', (SELLER,))
        cur.execute('SELECT activated_at FROM ml_wa_aftersales_config WHERE seller_id=%s', (SELLER,))
        activated_at = cur.fetchone()['activated_at']
    conn.commit()
    claims = collect(api_get, token, activated_at)
    for cid in claims:
        claim = api_get(BASE + '/' + cid, token)
        if not claim or not seller_player(claim):
            raise RuntimeError('Reclamação indisponível ou vendedor divergente')
        returns = []
        entities = claim.get('related_entities') or []
        if claim.get('type') == 'return' or 'return' in entities:
            result = api_get('/post-purchase/v2/claims/' + cid + '/returns', token)
            if not isinstance(result, (list, dict)):
                raise RuntimeError('Falha ao consultar devolução')
            returns = result if isinstance(result, list) else result.get('returns', [result] if result.get('id') else [])
        data = snapshot(claim, returns)
        digest = fingerprint(data)
        with conn.cursor() as cur:
            cur.execute('''SELECT 1 FROM ml_wa_aftersales_events
                WHERE seller_id=%s AND claim_id=%s AND fingerprint=%s''', (SELLER, cid, digest))
            exists = cur.fetchone()
        conn.commit()
        if exists:
            continue
        order = None
        if claim.get('resource') == 'order':
            order = api_get('/orders/' + str(claim['resource_id']), token)
            if not order or str((order.get('seller') or {}).get('id')) != SELLER:
                raise RuntimeError('Venda indisponível ou vendedor divergente')
        with conn.cursor() as cur:
            cur.execute('''INSERT INTO ml_wa_aftersales_events(seller_id,claim_id,fingerprint,body)
                VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING''', (SELLER, cid, digest, message(claim, returns, order)))
        conn.commit()
    with conn.cursor() as cur:
        cur.execute('''SELECT claim_id,fingerprint,body FROM ml_wa_aftersales_events
            WHERE seller_id=%s AND sent_at IS NULL ORDER BY attempts,created_at LIMIT 1''', (SELLER,))
        pending = cur.fetchall()
    conn.commit()
    sent = failed = 0
    for event in pending:
        try:
            ok = bool(send(PHONE, event['body']))
        except Exception:
            ok = False
        with conn.cursor() as cur:
            cur.execute('''UPDATE ml_wa_aftersales_events SET attempts=attempts+1,
                sent_at=CASE WHEN %s THEN NOW() ELSE NULL END,last_error=%s
                WHERE seller_id=%s AND claim_id=%s AND fingerprint=%s''',
                (ok, None if ok else 'WhatsApp não confirmou envio', SELLER, event['claim_id'], event['fingerprint']))
        conn.commit()
        sent += int(ok)
        failed += int(not ok)
    with conn.cursor() as cur:
        cur.execute('UPDATE ml_wa_aftersales_config SET last_success_at=NOW() WHERE seller_id=%s', (SELLER,))
    conn.commit()
    return {'ok': not failed, 'cases': len(claims), 'sent': sent, 'failed': failed}
