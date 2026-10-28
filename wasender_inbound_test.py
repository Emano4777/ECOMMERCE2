"""Store inbound WA Sender test replies; no automatic outgoing messages."""
import re

TEST_PHONE = "5517991436759"

def _digits(s):
    return re.sub(r"\D", "", str(s or ""))

def record_test_replies(payload, connection_factory, logger):
    data = payload.get("data") or {}
    msgs = data.get("messages") or data.get("message") or []
    if isinstance(msgs, dict):
        msgs = [msgs]
    if not isinstance(msgs, list):
        return 0
    count = 0
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        key = msg.get("key") or {}
        if key.get("fromMe") is not False:
            continue
        if str(key.get("remoteJid") or "").endswith("@g.us"):
            continue
        sender = key.get("cleanedSenderPn") or key.get("senderPn") or key.get("remoteJid") or ""
        phone = _digits(str(sender).split("@")[0])
        if len(phone) in (10, 11):
            phone = "55" + phone
        if phone != TEST_PHONE:
            continue
        message = msg.get("message") or {}
        body = (msg.get("messageBody") or message.get("conversation")
                or (message.get("extendedTextMessage") or {}).get("text")
                or data.get("messageBody") or data.get("text") or "")
        body = str(body).strip()[:4000]
        mid = str(key.get("id") or msg.get("id") or "").strip()
        if not mid or not body:
            continue
        try:
            conn = connection_factory()
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ecommerce_wa_respostas_teste (
                    id BIGSERIAL PRIMARY KEY,
                    message_id TEXT NOT NULL UNIQUE,
                    telefone TEXT NOT NULL,
                    mensagem TEXT NOT NULL,
                    recebido_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    resposta_ia TEXT,
                    status TEXT NOT NULL DEFAULT 'aguardando_aprovacao'
                )
            """)
            cur.execute("""
                INSERT INTO ecommerce_wa_respostas_teste(message_id, telefone, mensagem)
                VALUES (%s,%s,%s) ON CONFLICT (message_id) DO NOTHING
            """, (mid, phone, body))
            conn.commit()
            cur.close()
            count += 1
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.exception("Failed storing WA Sender test reply")
            raise
    return count
