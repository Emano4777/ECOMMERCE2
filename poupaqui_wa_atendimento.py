"""Atendimento WhatsApp com IA (PoupAqui) -- recebimento, historico e worker.

Fluxo:
  webhook WA Sender -> store_inbound()  (todas as conversas privadas, idempotente
                                          por message_id; ignora fromMe/grupos/
                                          canais/status)
  cron 1/min       -> run_worker()      (agrupa sequencia por contato, decide
                                          SILENCE/ANSWER/HUMAN, audita, e so envia
                                          se o modo permitir)

Modos (kill-switch): off < shadow < test < live.
  off    -> worker nao processa nada (webhook continua guardando historico)
  shadow -> classifica e audita, nunca envia
  test   -> envia ANSWER apenas para telefones da allowlist de teste
  live   -> envia ANSWER para qualquer contato verificado
Modo efetivo = menor entre o valor no banco (ecommerce_wa_ia_config.modo,
padrao 'off') e o teto do ambiente POUPAQUI_WA_IA_MAX_MODE (padrao 'test').
Para desligar tudo: modo 'off' no banco OU POUPAQUI_WA_IA_MAX_MODE=off.
"""
import json
import os
import re
import uuid
from datetime import datetime, timezone

import psycopg2.extensions

from poupaqui_ai_policy import (
    DEFAULT_MODEL, Decision, is_opt_out, preliminary_gate, validate_answer,
)

MODES = ("off", "shadow", "test", "live")
DEFAULT_TEST_PHONES = "5517991436759"
_AI_FAILURE_REASONS = ("AI ", "anthropic sdk", "model stop_reason", "invalid model", "invalid classification",
                       "invalid reply")

_SKIP_JID_SUFFIXES = ("@g.us", "@newsletter", "@broadcast")
_IGNORED_KINDS = ("protocolMessage", "reactionMessage", "editedMessage", "pollUpdateMessage",
                  "senderKeyDistributionMessage", "messageContextInfo")
_KIND_MAP = {
    "conversation": "text", "extendedTextMessage": "text",
    "imageMessage": "image", "videoMessage": "video", "audioMessage": "audio",
    "documentMessage": "document", "documentWithCaptionMessage": "document",
    "stickerMessage": "sticker", "contactMessage": "contact", "contactsArrayMessage": "contact",
    "locationMessage": "location", "liveLocationMessage": "location",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ecommerce_wa_contatos (
    telefone TEXT PRIMARY KEY,
    nome_whatsapp TEXT,
    vinculo_manual_tipo TEXT,
    vinculo_manual_ref TEXT,
    opt_out BOOLEAN NOT NULL DEFAULT FALSE,
    opt_out_em TIMESTAMPTZ,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ultima_msg_em TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS ecommerce_wa_mensagens (
    id BIGSERIAL PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE,
    telefone TEXT NOT NULL,
    direcao TEXT NOT NULL,
    tipo TEXT NOT NULL DEFAULT 'text',
    corpo TEXT NOT NULL DEFAULT '',
    nome_whatsapp TEXT,
    wa_timestamp TIMESTAMPTZ,
    recebido_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'pendente',
    tentativas INT NOT NULL DEFAULT 0,
    reservado_em TIMESTAMPTZ,
    decisao_id BIGINT,
    processado_em TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_wa_mensagens_fila ON ecommerce_wa_mensagens (status, telefone, recebido_em);
CREATE INDEX IF NOT EXISTS ix_wa_mensagens_hist ON ecommerce_wa_mensagens (telefone, recebido_em DESC);
CREATE TABLE IF NOT EXISTS ecommerce_wa_ia_decisoes (
    id BIGSERIAL PRIMARY KEY,
    telefone TEXT NOT NULL,
    message_ids TEXT[] NOT NULL,
    modo TEXT NOT NULL,
    decisao TEXT NOT NULL,
    resposta TEXT,
    motivo TEXT,
    origem TEXT NOT NULL,
    identidade JSONB,
    modelo TEXT,
    envio_status TEXT NOT NULL DEFAULT 'nao_enviar',
    envio_detalhe TEXT,
    enviado_em TIMESTAMPTZ,
    resolvido_em TIMESTAMPTZ,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_wa_ia_decisoes_tel ON ecommerce_wa_ia_decisoes (telefone, criado_em DESC);
CREATE TABLE IF NOT EXISTS ecommerce_wa_ia_config (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL,
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _cur(conn):
    # cursor de tuplas mesmo se a conexao do app usar RealDictCursor
    return conn.cursor(cursor_factory=psycopg2.extensions.cursor)


_schema_ok = False


def ensure_schema(conn, force=False):
    # uma vez por processo: CREATE INDEX IF NOT EXISTS ainda pega lock na tabela
    global _schema_ok
    if _schema_ok and not force:
        return
    with _cur(conn) as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()
    _schema_ok = True


# ─── Telefones ──────────────────────────────────────────────────────────────

def digits(value):
    return re.sub(r"\D", "", str(value or ""))


def normalize_phone(value):
    """'5517991436759' para numeros BR; outros paises ficam so com digitos."""
    d = digits(str(value or "").split("@")[0].split(":")[0])
    if len(d) in (10, 11):
        d = "55" + d
    return d if 10 <= len(d) <= 15 else ""


def phone_key(value):
    """Chave tolerante ao 9o digito: DDD + 8 ultimos digitos (BR)."""
    d = digits(value)
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    if len(d) in (10, 11):
        return d[:2] + d[-8:]
    return d if len(d) >= 8 else ""


# ─── Parse do webhook ──────────────────────────────────────────────────────

def _message_kind(message):
    if not isinstance(message, dict) or not message:
        return "text"
    for k in message:
        if k in _KIND_MAP:
            return _KIND_MAP[k]
    if any(k in _IGNORED_KINDS for k in message):
        return "ignore"  # reacao, edicao, protocolo: nao e mensagem nova
    return "other"


def _message_text(msg, message):
    message = message if isinstance(message, dict) else {}
    body = (msg.get("messageBody") or message.get("conversation")
            or (message.get("extendedTextMessage") or {}).get("text")
            or (message.get("imageMessage") or {}).get("caption")
            or (message.get("videoMessage") or {}).get("caption")
            or (message.get("documentMessage") or {}).get("caption") or "")
    return str(body).strip()[:4000]


def parse_inbound(payload):
    """Extrai mensagens privadas recebidas. Ignora fromMe, grupos, canais e status."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or {}
    msgs = data.get("messages") or data.get("message") or []
    if isinstance(msgs, dict):
        msgs = [msgs]
    if not isinstance(msgs, list):
        return []
    out = []
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        key = msg.get("key") or {}
        if key.get("fromMe") is not False:
            continue
        jid = str(key.get("remoteJid") or "")
        if jid.endswith(_SKIP_JID_SUFFIXES) or jid.startswith("status@"):
            continue
        sender = key.get("cleanedSenderPn") or key.get("senderPn") or (jid if jid.endswith("@s.whatsapp.net") else "")
        phone = normalize_phone(sender)
        mid = str(key.get("id") or msg.get("id") or "").strip()
        if not phone or not mid:
            continue
        message = msg.get("message") or {}
        kind = _message_kind(message)
        if kind == "ignore":
            continue
        ts = msg.get("messageTimestamp")
        try:
            wa_ts = datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None
        except (TypeError, ValueError, OverflowError, OSError):
            wa_ts = None
        out.append({
            "message_id": mid[:200],
            "telefone": phone,
            "tipo": kind,
            "corpo": _message_text(msg, message),
            "nome_whatsapp": str(msg.get("pushName") or "").strip()[:120] or None,
            "wa_timestamp": wa_ts,
        })
    return out


def store_inbound(payload, conn):
    """Grava historico + contato. Idempotente por message_id. Retorna qtd nova."""
    items = parse_inbound(payload)
    if not items:
        return 0
    ensure_schema(conn)
    novas = 0
    with _cur(conn) as cur:
        for it in items:
            cur.execute("""
                INSERT INTO ecommerce_wa_contatos (telefone, nome_whatsapp, ultima_msg_em)
                VALUES (%s, %s, NOW())
                ON CONFLICT (telefone) DO UPDATE SET
                    nome_whatsapp = COALESCE(EXCLUDED.nome_whatsapp, ecommerce_wa_contatos.nome_whatsapp),
                    ultima_msg_em = NOW(), atualizado_em = NOW()
            """, (it["telefone"], it["nome_whatsapp"]))
            cur.execute("""
                INSERT INTO ecommerce_wa_mensagens
                    (message_id, telefone, direcao, tipo, corpo, nome_whatsapp, wa_timestamp)
                VALUES (%s, %s, 'in', %s, %s, %s, %s)
                ON CONFLICT (message_id) DO NOTHING
                RETURNING id
            """, (it["message_id"], it["telefone"], it["tipo"], it["corpo"],
                  it["nome_whatsapp"], it["wa_timestamp"]))
            if cur.fetchone():
                novas += 1
    conn.commit()
    return novas


# ─── Modo / kill-switch ────────────────────────────────────────────────────

def _mode_rank(mode):
    return MODES.index(mode) if mode in MODES else 0


def get_db_mode(conn):
    with _cur(conn) as cur:
        cur.execute("SELECT valor FROM ecommerce_wa_ia_config WHERE chave = 'modo'")
        row = cur.fetchone()
    return row[0] if row and row[0] in MODES else "off"


def set_db_mode(conn, mode):
    if mode not in MODES:
        raise ValueError("modo invalido")
    ensure_schema(conn)
    with _cur(conn) as cur:
        cur.execute("""
            INSERT INTO ecommerce_wa_ia_config (chave, valor) VALUES ('modo', %s)
            ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor, atualizado_em = NOW()
        """, (mode,))
    conn.commit()


def env_mode_ceiling():
    m = (os.getenv("POUPAQUI_WA_IA_MAX_MODE") or "test").strip().lower()
    return m if m in MODES else "off"


def effective_mode(db_mode, ceiling=None):
    ceiling = ceiling or env_mode_ceiling()
    return MODES[min(_mode_rank(db_mode), _mode_rank(ceiling))]


def test_phones():
    raw = os.getenv("POUPAQUI_WA_IA_TEST_PHONES") or DEFAULT_TEST_PHONES
    return {normalize_phone(p) for p in raw.split(",") if normalize_phone(p)}


# ─── Identidade e contexto verificado ──────────────────────────────────────

def _split_phones(raw):
    return [p for p in re.split(r"[/,;|]| e ", str(raw or "")) if len(digits(p)) >= 8]


def identify_contact(conn, phone, manual=None):
    """Match SOMENTE por telefone (ou vinculo manual feito por admin). Nome
    exibido no WhatsApp nunca entra aqui -- vai pro contexto como nao verificado."""
    if manual and manual[0]:
        tipo, ref = manual
        return {"verificada": True, "metodo": "vinculo_manual", "tipo": tipo, "ref": ref,
                "candidatos": [{"tipo": tipo, "ref": ref}]}
    key = phone_key(phone)
    cands = {}
    if key:
        with _cur(conn) as cur:
            cur.execute("SELECT cnpjloja, usuario, razao, telefone FROM users WHERE COALESCE(telefone, '') <> ''")
            for cnpj, usuario, razao, tel in cur.fetchall():
                if any(phone_key(p) == key for p in _split_phones(tel)):
                    cands[("loja", str(cnpj or usuario))] = {
                        "tipo": "loja", "ref": str(cnpj or ""), "nome": razao or usuario, "usuario": usuario}
            cur.execute("SELECT id, nome, cnpj, telefone FROM fornecedores WHERE COALESCE(telefone, '') <> '' AND ativo IS NOT FALSE")
            for fid, nome, cnpj, tel in cur.fetchall():
                if any(phone_key(p) == key for p in _split_phones(tel)):
                    cands[("fornecedor", str(fid))] = {
                        "tipo": "fornecedor", "ref": str(fid), "nome": nome, "cnpj": (cnpj or "").strip()}
    lista = list(cands.values())
    if len(lista) == 1:
        c = lista[0]
        return {"verificada": True, "metodo": "telefone", "tipo": c["tipo"], "ref": c["ref"],
                "nome": c.get("nome"), "candidatos": lista}
    return {"verificada": False, "metodo": "telefone_ambiguo" if lista else "nao_encontrado",
            "tipo": None, "ref": None, "candidatos": lista}


def _fornecedor_info(row):
    nome, corte, horas, desc, obs = row
    info = {"fornecedor": nome}
    if corte:
        info["horario_corte_pedido"] = corte.strftime("%H:%M")
    if desc:
        info["prazo_entrega"] = desc
    elif horas:
        info["prazo_entrega"] = f"{horas} horas"
    if obs:
        info["observacao_entrega"] = obs
    return info


def verified_context(conn, identity):
    """Fatos do sistema que a IA pode citar. Nada de pedidos/financeiro."""
    if not identity.get("verificada"):
        return {}
    ctx = {}
    with _cur(conn) as cur:
        if identity["tipo"] in ("loja", "interno"):
            if identity["tipo"] == "loja" and identity.get("ref"):
                cur.execute("SELECT razao, cnpjloja FROM users WHERE cnpjloja = %s LIMIT 1", (identity["ref"],))
                r = cur.fetchone()
                if r:
                    ctx["loja"] = {"razao_social": r[0], "cnpj": r[1]}
            cur.execute("""SELECT nome, horario_corte, prazo_entrega_horas, prazo_entrega_desc, obs_entrega
                           FROM fornecedores WHERE ativo IS NOT FALSE ORDER BY nome""")
            ctx["fornecedores_entrega"] = [_fornecedor_info(r) for r in cur.fetchall()
                                           if r[1] or r[2] or r[3]]
        elif identity["tipo"] == "fornecedor":
            cur.execute("""SELECT nome, horario_corte, prazo_entrega_horas, prazo_entrega_desc, obs_entrega
                           FROM fornecedores WHERE id = %s""", (identity["ref"],))
            r = cur.fetchone()
            if r:
                ctx["fornecedor_cadastrado"] = _fornecedor_info(r)
    return ctx


# ─── Worker ────────────────────────────────────────────────────────────────

def _cfg_int(name, default):
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _history(conn, phone, exclude_ids, limit=20):
    with _cur(conn) as cur:
        cur.execute("""
            SELECT direcao, tipo, corpo, recebido_em FROM ecommerce_wa_mensagens
            WHERE telefone = %s AND NOT (id = ANY(%s))
            ORDER BY recebido_em DESC, id DESC LIMIT %s
        """, (phone, list(exclude_ids), limit))
        rows = cur.fetchall()
    return [{"de": "contato" if d == "in" else "assistente", "tipo": t, "texto": c,
             "em": e.isoformat() if e else None} for d, t, c, e in reversed(rows)]


def _sends_since(conn, phone, interval):
    with _cur(conn) as cur:
        cur.execute("""SELECT COUNT(*) FROM ecommerce_wa_ia_decisoes
                       WHERE telefone = %s AND envio_status IN ('enviando', 'enviado')
                         AND criado_em > NOW() - %s::interval""", (phone, interval))
        return cur.fetchone()[0]


def _human_open(conn, phone, hours):
    with _cur(conn) as cur:
        cur.execute("""SELECT 1 FROM ecommerce_wa_ia_decisoes
                       WHERE telefone = %s AND decisao = 'HUMAN' AND resolvido_em IS NULL
                         AND origem <> 'falha_ia' AND criado_em > NOW() - (%s || ' hours')::interval LIMIT 1""", (phone, str(hours)))
        return cur.fetchone() is not None


def _release(conn, ids, max_attempts):
    conn.rollback()
    with _cur(conn) as cur:
        cur.execute("""UPDATE ecommerce_wa_mensagens
                       SET status = CASE WHEN tentativas >= %s THEN 'erro' ELSE 'pendente' END,
                           reservado_em = NULL
                       WHERE id = ANY(%s) AND status = 'processando'""", (max_attempts, list(ids)))
    conn.commit()


def process_contact(conn, phone, mode, classifier, sender, state, logger=None):
    """Processa a sequencia pendente de um contato. Retorna dict de auditoria ou None."""
    max_attempts = 3
    with _cur(conn) as cur:
        cur.execute("""
            UPDATE ecommerce_wa_mensagens
            SET status = 'processando', reservado_em = NOW(), tentativas = tentativas + 1
            WHERE id IN (SELECT id FROM ecommerce_wa_mensagens
                         WHERE telefone = %s AND direcao = 'in' AND status = 'pendente'
                         FOR UPDATE SKIP LOCKED)
            RETURNING id, message_id, tipo, corpo, nome_whatsapp
        """, (phone,))
        batch = sorted(cur.fetchall())
    conn.commit()
    if not batch:
        return None
    ids = [b[0] for b in batch]
    try:
        texts = [b[3] for b in batch]
        kinds = [b[2] for b in batch]
        push_name = next((b[4] for b in reversed(batch) if b[4]), None)
        with _cur(conn) as cur:
            cur.execute("""SELECT opt_out, vinculo_manual_tipo, vinculo_manual_ref
                           FROM ecommerce_wa_contatos WHERE telefone = %s""", (phone,))
            contato = cur.fetchone() or (False, None, None)
        opt_out = bool(contato[0])
        identity = {"verificada": False, "metodo": None, "tipo": None, "ref": None, "candidatos": []}
        model_name = None
        origem = "regra"

        if any(is_opt_out(t) for t in texts):
            with _cur(conn) as cur:
                cur.execute("""UPDATE ecommerce_wa_contatos SET opt_out = TRUE, opt_out_em = NOW(),
                               atualizado_em = NOW() WHERE telefone = %s""", (phone,))
            decision, opt_out = Decision("SILENCE", "", "opt-out solicitado pelo contato"), True
        elif opt_out:
            decision = Decision("SILENCE", "", "contato com opt-out")
        else:
            decision = preliminary_gate(texts, kinds)
            if decision is not None:
                origem = "gate"
            elif _human_open(conn, phone, _cfg_int("POUPAQUI_WA_IA_HUMAN_LOCK_HORAS", 4)):
                decision = Decision("HUMAN", "", "conversa ja encaminhada a humano")
            else:
                identity = identify_contact(conn, phone, (contato[1], contato[2]))
                payload = {
                    "identidade_verificada": identity if identity["verificada"] else None,
                    "identidade_nao_verificada": None if identity["verificada"] else {
                        "motivo": identity["metodo"],
                        "nome_exibido_no_whatsapp_nao_confiavel": push_name},
                    "dados_verificados": verified_context(conn, identity),
                    "historico_recente": _history(conn, phone, ids),
                    "mensagens_novas": [{"tipo": k, "texto": t} for k, t in zip(kinds, texts)],
                }
                model_name = os.getenv("POUPAQUI_AI_MODEL", DEFAULT_MODEL)
                decision = validate_answer(classifier(payload), texts, identity["verificada"])
                # falha tecnica (rede, credencial, resposta invalida) vai pra
                # revisao humana mas nao trava a conversa como "em atendimento"
                origem = "falha_ia" if decision.reason.startswith(_AI_FAILURE_REASONS) else "modelo"

        envio = "nao_enviar"
        if decision.decision == "ANSWER":
            if mode in ("off", "shadow"):
                envio = "shadow"
            elif opt_out:
                envio = "bloqueado_opt_out"
            elif mode == "test" and phone not in test_phones():
                envio = "bloqueado_fora_allowlist"
            elif state["envios"] >= _cfg_int("POUPAQUI_WA_IA_MAX_ENVIOS_RUN", 10):
                envio = "bloqueado_limite_execucao"
            elif _sends_since(conn, phone, "1 hour") >= _cfg_int("POUPAQUI_WA_IA_MAX_HORA", 3):
                envio = "bloqueado_limite_hora"
            elif _sends_since(conn, phone, "1 day") >= _cfg_int("POUPAQUI_WA_IA_MAX_DIA", 8):
                envio = "bloqueado_limite_dia"
            else:
                envio = "enviando"

        with _cur(conn) as cur:
            cur.execute("""
                INSERT INTO ecommerce_wa_ia_decisoes
                    (telefone, message_ids, modo, decisao, resposta, motivo, origem, identidade, modelo, envio_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (phone, [b[1] for b in batch], mode, decision.decision, decision.reply or None,
                  decision.reason, origem, json.dumps(identity, ensure_ascii=False, default=str),
                  model_name, envio))
            decisao_id = cur.fetchone()[0]
            cur.execute("""UPDATE ecommerce_wa_mensagens SET status = 'processado', decisao_id = %s,
                           processado_em = NOW() WHERE id = ANY(%s)""", (decisao_id, ids))
        # Commit ANTES de enviar: se cair no meio, a decisao fica 'enviando' e
        # nunca e reenviada (no maximo uma vez).
        conn.commit()
    except Exception:
        if logger:
            logger.exception("wa_ia: falha processando contato %s", phone[-4:])
        _release(conn, ids, max_attempts)
        raise

    result = {"decisao_id": decisao_id, "telefone_fim": phone[-4:], "decisao": decision.decision,
              "origem": origem, "envio": envio, "mensagens": len(batch)}
    if envio == "enviando":
        state["envios"] += 1
        status, detalhe, out_id = "falhou", "", None
        try:
            resp = sender(phone, decision.reply)
            if resp:
                status = "enviado"
                data = resp.get("data") if isinstance(resp, dict) else None
                out_id = str((data or {}).get("msgId") or (data or {}).get("id") or "") or None
            else:
                detalhe = "sender retornou falso"
        except Exception as exc:
            detalhe = f"{type(exc).__name__}: {str(exc)[:200]}"
        with _cur(conn) as cur:
            cur.execute("""UPDATE ecommerce_wa_ia_decisoes SET envio_status = %s, envio_detalhe = %s,
                           enviado_em = CASE WHEN %s = 'enviado' THEN NOW() END WHERE id = %s""",
                        (status, detalhe or None, status, decisao_id))
            if status == "enviado":
                cur.execute("""INSERT INTO ecommerce_wa_mensagens
                                   (message_id, telefone, direcao, tipo, corpo, status, decisao_id, processado_em)
                               VALUES (%s, %s, 'out_ia', 'text', %s, 'processado', %s, NOW())
                               ON CONFLICT (message_id) DO NOTHING""",
                            (out_id or f"ia-{decisao_id}", phone, decision.reply, decisao_id))
        conn.commit()
        result["envio"] = status
    return result


def acquire_worker_lease(conn, ttl_minutes=5):
    """Lock de execucao por lease em tabela. Advisory lock de sessao nao serve:
    o DATABASE_URL passa pelo pooler do Supabase em modo transacao, entao o
    unlock pode cair em outro backend e o lock vazaria. Lease expira sozinho
    se o worker morrer no meio."""
    token = uuid.uuid4().hex
    with _cur(conn) as cur:
        cur.execute("""
            INSERT INTO ecommerce_wa_ia_config (chave, valor, atualizado_em) VALUES ('worker_lease', %s, NOW())
            ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor, atualizado_em = NOW()
            WHERE ecommerce_wa_ia_config.valor = 'livre'
               OR ecommerce_wa_ia_config.atualizado_em < NOW() - (%s || ' minutes')::interval
            RETURNING valor
        """, (token, str(ttl_minutes)))
        ok = cur.fetchone() is not None
    conn.commit()
    return token if ok else None


def release_worker_lease(conn, token):
    with _cur(conn) as cur:
        cur.execute("UPDATE ecommerce_wa_ia_config SET valor = 'livre' WHERE chave = 'worker_lease' AND valor = %s",
                    (token,))
    conn.commit()


def run_worker(conn, classifier, sender, logger=None, max_contacts=None):
    ensure_schema(conn)
    mode = effective_mode(get_db_mode(conn))
    summary = {"modo": mode, "contatos": 0, "resultados": [], "erros": 0}
    if mode == "off":
        return summary
    token = acquire_worker_lease(conn)
    if not token:
        summary["ignorado"] = "outra execucao em andamento"
        return summary
    try:
        debounce = _cfg_int("POUPAQUI_WA_IA_DEBOUNCE_SEG", 40)
        max_age = _cfg_int("POUPAQUI_WA_IA_MAX_IDADE_MIN", 360)
        max_contacts = max_contacts or _cfg_int("POUPAQUI_WA_IA_MAX_CONTATOS_RUN", 8)
        with _cur(conn) as cur:
            # reservas presas (worker caiu no meio) voltam pra fila
            cur.execute("""UPDATE ecommerce_wa_mensagens
                           SET status = CASE WHEN tentativas >= 3 THEN 'erro' ELSE 'pendente' END,
                               reservado_em = NULL
                           WHERE status = 'processando' AND reservado_em < NOW() - INTERVAL '10 minutes'""")
            # nao responde conversa velha (ex.: acumulada enquanto estava 'off')
            cur.execute("""UPDATE ecommerce_wa_mensagens SET status = 'expirado', processado_em = NOW()
                           WHERE status = 'pendente' AND direcao = 'in'
                             AND recebido_em < NOW() - (%s || ' minutes')::interval""", (str(max_age),))
            summary["expiradas"] = cur.rowcount
            # so pega contato cuja ultima mensagem tem >= debounce (sequencia terminou)
            cur.execute("""SELECT telefone FROM ecommerce_wa_mensagens
                           WHERE status = 'pendente' AND direcao = 'in'
                           GROUP BY telefone
                           HAVING MAX(recebido_em) < NOW() - (%s || ' seconds')::interval
                           ORDER BY MIN(recebido_em) LIMIT %s""", (str(debounce), max_contacts))
            phones = [r[0] for r in cur.fetchall()]
        conn.commit()
        state = {"envios": 0}
        for phone in phones:
            try:
                res = process_contact(conn, phone, mode, classifier, sender, state, logger)
            except Exception:
                summary["erros"] += 1
                continue
            if res:
                summary["contatos"] += 1
                summary["resultados"].append(res)
        summary["envios"] = state["envios"]
        return summary
    finally:
        try:
            conn.rollback()
            release_worker_lease(conn, token)
        except Exception:
            pass


def status_summary(conn):
    ensure_schema(conn)
    db_mode = get_db_mode(conn)
    with _cur(conn) as cur:
        cur.execute("SELECT status, COUNT(*) FROM ecommerce_wa_mensagens WHERE direcao = 'in' GROUP BY status")
        fila = dict(cur.fetchall())
        cur.execute("""SELECT decisao, envio_status, COUNT(*) FROM ecommerce_wa_ia_decisoes
                       WHERE criado_em > NOW() - INTERVAL '24 hours' GROUP BY 1, 2""")
        dec = [{"decisao": d, "envio": e, "qtd": q} for d, e, q in cur.fetchall()]
        cur.execute("""SELECT COUNT(*) FROM ecommerce_wa_ia_decisoes
                       WHERE decisao = 'HUMAN' AND resolvido_em IS NULL""")
        humanos = cur.fetchone()[0]
    return {"modo_banco": db_mode, "teto_ambiente": env_mode_ceiling(),
            "modo_efetivo": effective_mode(db_mode), "fila": fila,
            "decisoes_24h": dec, "humano_pendente": humanos}
