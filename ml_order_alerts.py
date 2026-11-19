"""Independent paid-order WhatsApp outbox for Sao Pedro (no ERP export)."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

STORE = "54185432000143"
SELLER = "3501375634"
PHONE = "5519995058165"
LOCK = 73501375634


def parse_date(value):
    if not value:
        return None
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def eligible(order, activated_at):
    paid_at = parse_date(order.get("date_closed"))
    return (str((order.get("seller") or {}).get("id")) == SELLER
            and order.get("status") == "paid"
            and paid_at is not None and paid_at >= activated_at)


def message(order, sla):
    deadline = parse_date((sla or {}).get("expected_date"))
    deadline_text = (deadline.astimezone(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y às %H:%M (Brasília)")
                     if deadline else "Ainda não informado pelo Mercado Livre; confira na venda.")
    lines = ["🛒 Nova venda paga — Mercado Livre", "Loja São Pedro", f"Venda #{order['id']}", "", "Produtos:"]
    for item in order.get("order_items") or []:
        title = " ".join(str((item.get("item") or {}).get("title") or "Produto").split())
        lines.append(f"• {item.get('quantity', 1)} un. — {title}")
    lines += ["", f"Prazo de postagem: {deadline_text}", "", "Abrir venda e preparar nota/etiqueta:",
              f"https://www.mercadolivre.com.br/vendas/{order['id']}"]
    return "\n".join(lines)


def run(conn, get_token, api_get, send):
    """Session lock serializes cron/web workers; failed sends stay pending."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s) AS locked", (LOCK,))
        locked = cur.fetchone()["locked"]
    conn.commit()
    if not locked:
        return {"ok": True, "busy": True}
    try:
        return _run(conn, get_token, api_get, send)
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))
        conn.commit()


def _run(conn, get_token, api_get, send):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS ml_wa_alert_config (
            seller_id TEXT PRIMARY KEY, activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            scanned_at TIMESTAMPTZ, last_success_at TIMESTAMPTZ)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS ml_wa_order_alerts (
            seller_id TEXT NOT NULL, order_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), sent_at TIMESTAMPTZ,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            skipped BOOLEAN NOT NULL DEFAULT FALSE, PRIMARY KEY(seller_id, order_id))""")
        cur.execute("INSERT INTO ml_wa_alert_config(seller_id) VALUES (%s) ON CONFLICT DO NOTHING", (SELLER,))
        cur.execute("SELECT * FROM ml_wa_alert_config WHERE seller_id=%s", (SELLER,))
        config = cur.fetchone()
    conn.commit()
    token = get_token(cnpjloja=STORE)
    conn.commit()
    if not token:
        raise RuntimeError("ML alert: token indisponível")
    me = api_get("/users/me", token)
    if not me or str(me.get("id")) != SELLER:
        raise RuntimeError("ML alert: conta conectada divergente")
    started = datetime.now(timezone.utc)
    since = max(config["activated_at"], (config["scanned_at"] or config["activated_at"]) - timedelta(minutes=10))
    offset = 0
    while True:
        query = urlencode({"seller": SELLER, "order.status": "paid", "order.date_closed.from": since.isoformat(),
                           "order.date_closed.to": started.isoformat(),
                           "sort": "date_asc", "limit": 50, "offset": offset})
        result = api_get("/orders/search?" + query, token)
        if not isinstance(result, dict) or "results" not in result or "paging" not in result:
            raise RuntimeError("ML alert: falha ao consultar vendas")
        with conn.cursor() as cur:
            for order in result["results"]:
                if eligible(order, config["activated_at"]):
                    cur.execute("""INSERT INTO ml_wa_order_alerts(seller_id,order_id) VALUES (%s,%s)
                        ON CONFLICT DO NOTHING""", (SELLER, str(order["id"])))
        conn.commit()
        offset += 50
        if offset >= int(result["paging"]["total"]):
            break
    with conn.cursor() as cur:
        cur.execute("UPDATE ml_wa_alert_config SET scanned_at=%s WHERE seller_id=%s", (started, SELLER))
        cur.execute("""SELECT order_id FROM ml_wa_order_alerts WHERE seller_id=%s
            AND sent_at IS NULL AND NOT skipped ORDER BY attempts, created_at LIMIT 1""", (SELLER,))
        pending = cur.fetchall()
    conn.commit()
    sent = 0
    failed = 0
    for row in pending:
        oid = row["order_id"]
        order = api_get(f"/orders/{oid}", token)
        if not order:
            with conn.cursor() as cur:
                cur.execute("""UPDATE ml_wa_order_alerts SET attempts=attempts+1,
                    last_error='Falha ao consultar venda' WHERE seller_id=%s AND order_id=%s""", (SELLER, oid))
            conn.commit()
            failed += 1
            continue
        if not eligible(order, config["activated_at"]):
            with conn.cursor() as cur:
                cur.execute("UPDATE ml_wa_order_alerts SET skipped=TRUE WHERE seller_id=%s AND order_id=%s", (SELLER, oid))
            conn.commit()
            continue
        sid = (order.get("shipping") or {}).get("id")
        sla = api_get(f"/shipments/{sid}/sla", token) if sid else None
        ok = bool(send(PHONE, message(order, sla)))
        with conn.cursor() as cur:
            cur.execute("""UPDATE ml_wa_order_alerts SET attempts=attempts+1,
                sent_at=CASE WHEN %s THEN NOW() ELSE NULL END, last_error=%s
                WHERE seller_id=%s AND order_id=%s""", (ok, None if ok else "WhatsApp não confirmou envio", SELLER, oid))
        conn.commit()
        sent += int(ok)
        failed += int(not ok)
    with conn.cursor() as cur:
        cur.execute("UPDATE ml_wa_alert_config SET last_success_at=NOW() WHERE seller_id=%s", (SELLER,))
    conn.commit()
    return {"ok": failed == 0, "sent": sent, "failed": failed}
