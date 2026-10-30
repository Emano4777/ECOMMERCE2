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

Motor (ecommerce_wa_ia_config.motor, padrao 'servidor'): so o worker cujo
engine bate com o motor do banco processa a fila -- uma unica via de envio,
sem corrida entre o cron do servidor (Anthropic) e o worker local (Ollama,
scripts/wa_ia_worker_local.py).

Escalacao (HUMAN, ou ANSWER com escalado=TRUE): fica pendente em
ecommerce_wa_ia_decisoes (resolvido_em NULL) e, se POUPAQUI_WA_IA_ALERTA_PHONES
estiver configurado, a equipe recebe um alerta no WhatsApp com resumo tecnico
(respeita o modo: nada em shadow; em test so pra allowlist). O alerta sai ANTES
da resposta ao contato, e a resposta so diz "ja passei para a equipe" se o
alerta foi entregue (alerta_status enviado/parcial); sem canal ou com falha ela
diz honestamente que nao conseguiu avisar e informa o protocolo.
Com POUPAQUI_WA_IA_ACK_HUMANO=1 o contato recebe GENERIC_ACK_REPLY (texto fixo,
nunca do modelo) -- unica resposta possivel pra contato nao verificado.
"""
import json
import os
import re
import uuid
from datetime import datetime, timezone

import psycopg2.extensions

from poupaqui_ai_policy import (
    DEFAULT_MODEL, GENERIC_ACK_REPLY, Decision, is_opt_out, normalize, preliminary_gate, validate_answer,
)

MODES = ("off", "shadow", "test", "live")
ENGINES = ("servidor", "local")
HUMAN_LOCK_REASON = "conversa ja encaminhada a humano"
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
ALTER TABLE ecommerce_wa_ia_decisoes ADD COLUMN IF NOT EXISTS alerta_status TEXT;
ALTER TABLE ecommerce_wa_ia_decisoes ADD COLUMN IF NOT EXISTS alerta_em TIMESTAMPTZ;
ALTER TABLE ecommerce_wa_mensagens ADD COLUMN IF NOT EXISTS origem_envio TEXT;
ALTER TABLE ecommerce_wa_ia_decisoes ADD COLUMN IF NOT EXISTS escalado BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE ecommerce_wa_ia_decisoes ADD COLUMN IF NOT EXISTS resumo JSONB;
ALTER TABLE ecommerce_wa_mensagens ADD COLUMN IF NOT EXISTS midia JSONB;
ALTER TABLE ecommerce_wa_contatos ADD COLUMN IF NOT EXISTS lid TEXT;
ALTER TABLE ecommerce_wa_contatos ADD COLUMN IF NOT EXISTS memoria TEXT;
ALTER TABLE ecommerce_wa_contatos ADD COLUMN IF NOT EXISTS memoria_em TIMESTAMPTZ;
ALTER TABLE ecommerce_wa_contatos ADD COLUMN IF NOT EXISTS memoria_n INT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_wa_contatos_lid ON ecommerce_wa_contatos (lid);
CREATE TABLE IF NOT EXISTS ecommerce_wa_atendimento_humano (
    telefone TEXT PRIMARY KEY,
    ativo BOOLEAN NOT NULL DEFAULT TRUE,
    origem TEXT NOT NULL,
    desde TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ate TIMESTAMPTZ,
    ultima_manual_em TIMESTAMPTZ,
    devolvido_em TIMESTAMPTZ,
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS ecommerce_wa_envios_auto (
    id BIGSERIAL PRIMARY KEY,
    telefone TEXT NOT NULL,
    corpo_hash TEXT NOT NULL,
    sistema TEXT,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_wa_envios_auto_tel ON ecommerce_wa_envios_auto (telefone, criado_em DESC);
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


_MIDIA_CAMPOS = ("url", "mimetype", "mediaKey", "fileSha256", "fileLength", "fileName", "directPath", "seconds")
MIDIA_IMAGEM_TIPOS = ("image/jpeg", "image/png", "image/webp")
MIDIA_VIDEO_TIPOS = ("video/mp4", "video/3gpp", "video/quicktime", "video/webm")
MIDIA_AUDIO_TIPOS = ("audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac", "audio/wav", "audio/x-wav", "audio/webm")


def _media_info(message):
    """Metadados para baixar imagem/print ou audio pela decrypt-media da WA Sender (o arquivo do
    WhatsApp vem criptografado; sem guardar isso na chegada nao da para ler depois)."""
    message = message if isinstance(message, dict) else {}
    for campo, tipos in (("imageMessage", MIDIA_IMAGEM_TIPOS), ("documentMessage", MIDIA_IMAGEM_TIPOS),
                         ("audioMessage", MIDIA_AUDIO_TIPOS), ("videoMessage", MIDIA_VIDEO_TIPOS)):
        m = message.get(campo)
        if isinstance(m, dict) and str(m.get("mimetype") or "").split(";")[0].strip() in tipos \
                and m.get("url") and m.get("mediaKey"):
            return {"campo": campo, **{k: m[k] for k in _MIDIA_CAMPOS if m.get(k) is not None}}
    return None


def _lid(valor):
    """'123456@lid' -> '123456@lid'; qualquer outra coisa -> None."""
    v = str(valor or "").strip()
    return v if re.fullmatch(r"\d{5,25}@lid", v) else None


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
            "midia": _media_info(message),
            "lid": _lid(key.get("senderLid") or jid),
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
                INSERT INTO ecommerce_wa_contatos (telefone, nome_whatsapp, ultima_msg_em, lid)
                VALUES (%s, %s, NOW(), %s)
                ON CONFLICT (telefone) DO UPDATE SET
                    nome_whatsapp = COALESCE(EXCLUDED.nome_whatsapp, ecommerce_wa_contatos.nome_whatsapp),
                    lid = COALESCE(EXCLUDED.lid, ecommerce_wa_contatos.lid),
                    ultima_msg_em = NOW(), atualizado_em = NOW()
            """, (it["telefone"], it["nome_whatsapp"], it.get("lid")))
            cur.execute("""
                INSERT INTO ecommerce_wa_mensagens
                    (message_id, telefone, direcao, tipo, corpo, nome_whatsapp, wa_timestamp, midia)
                VALUES (%s, %s, 'in', %s, %s, %s, %s, %s)
                ON CONFLICT (message_id) DO NOTHING
                RETURNING id
            """, (it["message_id"], it["telefone"], it["tipo"], it["corpo"],
                  it["nome_whatsapp"], it["wa_timestamp"],
                  json.dumps(it["midia"]) if it.get("midia") else None))
            if cur.fetchone():
                novas += 1
    conn.commit()
    return novas


# ─── Atendimento humano (takeover por contato) ─────────────────────────────
#
# Mensagem que SAI do numero (fromMe) e o sinal de que alguem respondeu. Ela
# e classificada pela origem:
#   ia      -> resposta da propria IA (decisao 'enviando'/'enviado' com o mesmo
#              texto pro mesmo telefone). O msgId da WA Sender e numerico e nao
#              e o key.id do webhook, por isso o casamento e por conteudo.
#   sistema -> envio automatico conhecido (alerta da IA, notificacao de status
#              do Pedido Eletronico, registro em ecommerce_wa_envios_auto,
#              prefixos de POUPAQUI_WA_IA_PREFIXOS_AUTO)
#   manual  -> o resto: operador digitou no celular/WhatsApp Web. Silencia a IA
#              SO nesse contato por POUPAQUI_WA_IA_SILENCIO_MIN (renova a cada
#              mensagem manual) e marca as mensagens pendentes dele como
#              respondidas pelo humano -- a IA nunca volta pra responder conversa
#              que o operador ja pegou.
# Na duvida vira 'manual': o erro possivel e a IA ficar quieta, nunca falar
# junto com o operador.

PEDIU_HUMANO_REASON = "contato pediu atendente humano"


def pediu_humano_reply(alerta_status, protocolo=None, anterior_em=None):
    """Texto fixo (nunca do modelo) para quem pediu atendente. So diz que chamou
    alguem se o alerta para a equipe foi ENTREGUE."""
    prot = f" (protocolo {protocolo})" if protocolo else ""
    if alerta_status in ("enviado", "parcial"):
        return f"Claro! Já chamei alguém da equipe{prot}, que continua o atendimento com você por aqui."
    if alerta_status == "ja_avisada":
        quando = f" às {_hora_local(anterior_em)}" if anterior_em else ""
        return (f"Claro! A equipe já recebeu o seu caso{quando}{prot}. Daqui em diante quem responde é um "
                "atendente.")
    return (f"Claro! Daqui em diante quem responde é um atendente. Não consegui avisar a equipe automaticamente "
            f"agora, mas deixei registrado{prot}; se for urgente, fale com a equipe pelo canal de costume.")


def _hora_local(dt):
    from poupaqui_ai_assistente import _agora
    return f"{_agora(dt):%H:%M}"
HUMANO_ATIVO_REASON = "atendimento humano ativo neste contato"
_PEDIU_HUMANO_RE = re.compile(
    r"\b(falar|conversar|atendimento|passa\w*|chama\w*|quero|preciso)\b.{0,25}"
    r"\b(atendente|humano|humana|pessoa|alguem|gente de verdade|operador\w*|responsavel)\b")
_JANELA_IA_MIN = 15


def _norm_text(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _corpo_hash(texto):
    import hashlib
    return hashlib.sha256(_norm_text(texto).encode("utf-8")).hexdigest()


def parse_outbound(payload):
    """Mensagens privadas que SAIRAM do numero da loja (fromMe=True)."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or {}
    if isinstance(data, dict) and isinstance(data.get("key"), dict):
        # message.sent oficial: key direto em data e conteudo em data.message
        msgs = [data]
    else:
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
        if key.get("fromMe") is not True:
            continue
        jid = str(key.get("remoteJid") or "")
        if jid.endswith(_SKIP_JID_SUFFIXES) or jid.startswith("status@"):
            continue
        # remoteJid so com digitos ("+5517...") tambem e contato privado
        numerico = bool(re.fullmatch(r"\+?\d{10,15}", jid))
        dest = (key.get("cleanedRecipientPn") or key.get("recipientPn") or key.get("remoteJidAlt")
                or (jid if jid.endswith("@s.whatsapp.net") or numerico else ""))
        phone = normalize_phone(dest) if not str(dest).endswith("@lid") else ""
        lid = None if phone else _lid(dest or jid)
        mid = str(key.get("id") or msg.get("id") or "").strip()
        if not (phone or lid) or not mid:
            continue
        message = msg.get("message") or {}
        kind = _message_kind(message)
        if kind == "ignore":
            continue
        out.append({"message_id": mid[:200], "telefone": phone, "lid": lid, "tipo": kind,
                    "corpo": _message_text(msg, message)})
    return out


def estrutura_fromme(payload):
    """Diagnostico SEM dados pessoais das mensagens enviadas (fromMe) que nao viraram registro:
    so nomes de campos e o tipo de endereco. Ajuda a descobrir o formato real da WA Sender."""
    data = (payload or {}).get("data") or {}
    msgs = [data] if isinstance(data.get("key"), dict) else (data.get("messages") or data.get("message") or [])
    msgs = [msgs] if isinstance(msgs, dict) else (msgs if isinstance(msgs, list) else [])
    out = []
    for m in msgs:
        key = (m or {}).get("key") or {} if isinstance(m, dict) else {}
        if key.get("fromMe") is True:
            jid = str(key.get("remoteJid") or "")
            out.append({"key": sorted(key), "jid": jid.split("@")[-1] if "@" in jid else ("digitos" if jid else ""),
                        "msg": sorted(k for k in m if k != "key")})
    return out


def _auto_prefixes():
    raw = os.getenv("POUPAQUI_WA_IA_PREFIXOS_AUTO") or "[PoupAqui IA]"
    return [p.strip() for p in raw.split("|") if p.strip()]


def _table_exists(cur, name):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    return cur.fetchone()[0]


def classify_outbound(cur, phone, corpo):
    """'ia' | 'sistema' | 'manual' para uma mensagem que saiu do numero."""
    texto = _norm_text(corpo)
    if texto:
        cur.execute("""SELECT 1 FROM ecommerce_wa_ia_decisoes
                       WHERE telefone = %s AND envio_status IN ('enviando', 'enviado')
                         AND regexp_replace(btrim(resposta), '\\s+', ' ', 'g') = %s
                         AND criado_em > NOW() - (%s || ' minutes')::interval LIMIT 1""",
                    (phone, texto, str(_JANELA_IA_MIN)))
        if cur.fetchone():
            return "ia"
        if any(texto.startswith(p) for p in _auto_prefixes()):
            return "sistema"
        cur.execute("""SELECT 1 FROM ecommerce_wa_envios_auto WHERE telefone = %s AND corpo_hash = %s
                         AND criado_em > NOW() - INTERVAL '15 minutes' LIMIT 1""", (phone, _corpo_hash(texto)))
        if cur.fetchone():
            return "sistema"
    # notificacao de status do Pedido Eletronico: guarda o destino no jid do retorno
    if _table_exists(cur, "pedido_status_whatsapp_log"):
        cur.execute("""SELECT 1 FROM pedido_status_whatsapp_log
                       WHERE sent_ok AND created_at > NOW() - INTERVAL '5 minutes'
                         AND regexp_replace(COALESCE(response_body, ''), '\\D', '', 'g') LIKE %s LIMIT 1""",
                    ("%" + phone + "%",))
        if cur.fetchone():
            return "sistema"
    return "manual"


def registrar_envio_automatico(conn, phone, texto, sistema=None):
    """Para quem envia automatico pelo mesmo numero: registra ANTES de enviar
    para o webhook nao confundir com o operador (e silenciar a IA a toa)."""
    ensure_schema(conn)
    with _cur(conn) as cur:
        cur.execute("INSERT INTO ecommerce_wa_envios_auto (telefone, corpo_hash, sistema) VALUES (%s, %s, %s)",
                    (normalize_phone(phone), _corpo_hash(texto), sistema))
    conn.commit()


def _lock_contact(cur, phone):
    """Serializa webhook (mensagem manual) e worker (envio da IA) no mesmo contato."""
    cur.execute("""INSERT INTO ecommerce_wa_contatos (telefone) VALUES (%s)
                   ON CONFLICT (telefone) DO NOTHING""", (phone,))
    cur.execute("SELECT telefone FROM ecommerce_wa_contatos WHERE telefone = %s FOR UPDATE", (phone,))


def _set_takeover(cur, phone, origem, minutos):
    """Liga/renova o atendimento humano. minutos=None -> ate devolver explicito.
    Uma trava sem prazo nunca e encurtada por uma mensagem manual."""
    cur.execute("""
        INSERT INTO ecommerce_wa_atendimento_humano (telefone, ativo, origem, desde, ate, ultima_manual_em)
        VALUES (%s, TRUE, %s, NOW(), CASE WHEN %s::int IS NULL THEN NULL
                                          ELSE NOW() + (%s::int * INTERVAL '1 minute') END,
                CASE WHEN %s = 'manual_outbound' THEN NOW() END)
        ON CONFLICT (telefone) DO UPDATE SET
            origem = CASE WHEN ecommerce_wa_atendimento_humano.ativo
                               AND ecommerce_wa_atendimento_humano.ate IS NULL
                          THEN ecommerce_wa_atendimento_humano.origem ELSE EXCLUDED.origem END,
            desde = CASE WHEN ecommerce_wa_atendimento_humano.ativo
                              AND (ecommerce_wa_atendimento_humano.ate IS NULL
                                   OR ecommerce_wa_atendimento_humano.ate > NOW())
                         THEN ecommerce_wa_atendimento_humano.desde ELSE NOW() END,
            ate = CASE WHEN EXCLUDED.ate IS NULL THEN NULL
                       WHEN ecommerce_wa_atendimento_humano.ativo
                            AND ecommerce_wa_atendimento_humano.ate IS NULL THEN NULL
                       ELSE GREATEST(EXCLUDED.ate, CASE WHEN ecommerce_wa_atendimento_humano.ativo
                                                        THEN ecommerce_wa_atendimento_humano.ate END) END,
            ativo = TRUE,
            ultima_manual_em = COALESCE(EXCLUDED.ultima_manual_em, ecommerce_wa_atendimento_humano.ultima_manual_em),
            devolvido_em = NULL, atualizado_em = NOW()
    """, (phone, origem, minutos, minutos, origem))


def store_outbound(payload, conn):
    """Grava mensagens que sairam do numero e liga o takeover nas manuais.
    Idempotente por message_id. Retorna {'ia': n, 'sistema': n, 'manual': n}."""
    items = parse_outbound(payload)
    cont = {"ia": 0, "sistema": 0, "manual": 0}
    if not items:
        return cont
    ensure_schema(conn)
    janela = _cfg_int("POUPAQUI_WA_IA_SILENCIO_MIN", 120)
    with _cur(conn) as cur:
        for it in items:
            phone = it["telefone"]
            if not phone:
                cur.execute("SELECT telefone FROM ecommerce_wa_contatos WHERE lid = %s LIMIT 2", (it["lid"],))
                achados = cur.fetchall()
                if len(achados) != 1:
                    cont["sem_telefone"] = cont.get("sem_telefone", 0) + 1
                    continue
                phone = achados[0][0]
            _lock_contact(cur, phone)
            cur.execute("SELECT 1 FROM ecommerce_wa_mensagens WHERE message_id = %s", (it["message_id"],))
            if cur.fetchone():
                continue
            origem = classify_outbound(cur, phone, it["corpo"])
            cont[origem] += 1
            if origem == "ia":
                # o worker ja gravou a resposta (com o msgId da API): nao duplica o historico
                cur.execute("""SELECT 1 FROM ecommerce_wa_mensagens WHERE telefone = %s AND direcao = 'out_ia'
                                 AND corpo = %s AND recebido_em > NOW() - (%s || ' minutes')::interval LIMIT 1""",
                            (phone, it["corpo"], str(_JANELA_IA_MIN)))
                if cur.fetchone():
                    continue
            cur.execute("""INSERT INTO ecommerce_wa_mensagens
                               (message_id, telefone, direcao, tipo, corpo, status, processado_em, origem_envio)
                           VALUES (%s, %s, %s, %s, %s, 'processado', NOW(), %s)
                           ON CONFLICT (message_id) DO NOTHING""",
                        (it["message_id"], phone, "out_manual" if origem == "manual" else f"out_{origem}",
                         it["tipo"], it["corpo"], origem))
            if origem == "manual":
                _set_takeover(cur, phone, "manual_outbound", janela)
                # o operador ja respondeu: o que estava na fila e dele, nao da IA
                cur.execute("""UPDATE ecommerce_wa_mensagens SET status = 'respondido_humano', processado_em = NOW()
                               WHERE telefone = %s AND direcao = 'in' AND status = 'pendente'""", (phone,))
    conn.commit()
    return cont


def takeover_ativo(conn_or_cur, phone):
    """Dict do atendimento humano vigente no contato, ou None."""
    def q(cur):
        cur.execute("""SELECT origem, desde, ate, ultima_manual_em FROM ecommerce_wa_atendimento_humano
                       WHERE telefone = %s AND ativo AND (ate IS NULL OR ate > NOW())""", (phone,))
        r = cur.fetchone()
        return {"origem": r[0], "desde": r[1], "ate": r[2], "ultima_manual_em": r[3]} if r else None
    if hasattr(conn_or_cur, "execute"):
        return q(conn_or_cur)
    with _cur(conn_or_cur) as cur:
        return q(cur)


def assumir_atendimento(conn, phone, minutos=None, origem="operador_comando"):
    """Operador assume o contato explicitamente (sem prazo = ate devolver)."""
    ensure_schema(conn)
    phone = normalize_phone(phone)
    if not phone:
        raise ValueError("telefone invalido")
    with _cur(conn) as cur:
        _lock_contact(cur, phone)
        _set_takeover(cur, phone, origem, minutos)
    conn.commit()
    return phone


def devolver_atendimento(conn, phone):
    """Retomada EXPLICITA pela IA. O que chegou durante o atendimento humano
    continua com o humano: a IA so responde mensagens novas daqui pra frente."""
    ensure_schema(conn)
    phone = normalize_phone(phone)
    with _cur(conn) as cur:
        _lock_contact(cur, phone)
        cur.execute("""UPDATE ecommerce_wa_atendimento_humano SET ativo = FALSE, devolvido_em = NOW(),
                       atualizado_em = NOW() WHERE telefone = %s AND ativo""", (phone,))
        n = cur.rowcount
        cur.execute("""UPDATE ecommerce_wa_mensagens SET status = 'respondido_humano', processado_em = NOW()
                       WHERE telefone = %s AND direcao = 'in' AND status = 'pendente'""", (phone,))
    conn.commit()
    return n


def atendimentos_humanos(conn):
    ensure_schema(conn)
    with _cur(conn) as cur:
        cur.execute("""SELECT telefone, origem, desde, ate, ultima_manual_em FROM ecommerce_wa_atendimento_humano
                       WHERE ativo AND (ate IS NULL OR ate > NOW()) ORDER BY desde""")
        rows = cur.fetchall()
    conn.commit()
    return [{"telefone": t, "origem": o, "desde": d, "ate": a, "ultima_manual_em": u} for t, o, d, a, u in rows]


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


def alert_phones(mode):
    """Destinos do alerta de escalacao. Em test so os que estao na allowlist."""
    raw = os.getenv("POUPAQUI_WA_IA_ALERTA_PHONES") or ""
    phones = {normalize_phone(p) for p in raw.split(",") if normalize_phone(p)}
    if mode == "test":
        return phones & test_phones()
    return phones if mode == "live" else set()


def get_db_engine(conn):
    with _cur(conn) as cur:
        cur.execute("SELECT valor FROM ecommerce_wa_ia_config WHERE chave = 'motor'")
        row = cur.fetchone()
    return row[0] if row and row[0] in ENGINES else "servidor"


def set_db_engine(conn, engine):
    if engine not in ENGINES:
        raise ValueError("motor invalido")
    ensure_schema(conn)
    with _cur(conn) as cur:
        cur.execute("""
            INSERT INTO ecommerce_wa_ia_config (chave, valor) VALUES ('motor', %s)
            ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor, atualizado_em = NOW()
        """, (engine,))
    conn.commit()


# ─── Identidade e contexto verificado ──────────────────────────────────────

def _split_phones(raw):
    return [p for p in re.split(r"[/,;|]| e ", str(raw or "")) if len(digits(p)) >= 8]


def identify_contact(conn, phone, manual=None):
    """Match SOMENTE por telefone (ou vinculo manual feito por admin). Nome
    exibido no WhatsApp nunca entra aqui -- vai pro contexto como nao verificado."""
    if manual and manual[0]:
        tipo, ref = manual
        out = {"verificada": True, "metodo": "vinculo_manual", "tipo": tipo, "ref": ref,
               "candidatos": [{"tipo": tipo, "ref": ref}]}
        if tipo == "interno":
            # equipe interna que tambem e o telefone de uma loja: essa loja vira o padrao quando a
            # mensagem nao cita nenhuma (citando outra, vale a citada)
            pelo_tel = identify_contact(conn, phone, None)
            if pelo_tel.get("verificada") and pelo_tel.get("tipo") == "loja":
                out["loja_padrao"] = {"ref": pelo_tel["ref"], "nome": pelo_tel.get("nome")}
        return out
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
    quem = {"in": "contato", "out_manual": "operador", "out_sistema": "sistema"}
    return [{"de": quem.get(d, "assistente"), "tipo": t, "texto": c,
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


AUDIO_SEM_FALA = "[Áudio sem fala reconhecível]"


def texto_da_midia(lido, legenda=""):
    """Texto que substitui a midia no fluxo: audio vira a propria fala transcrita; imagem vira o
    que o modelo de visao viu, marcado como imagem."""
    if lido.get("video"):
        partes = ["[Vídeo enviado pelo contato]"]
        if legenda:
            partes.append(f"Legenda: {legenda}.")
        if lido.get("resumo"):
            partes.append(f"O que aparece: {str(lido['resumo']).strip()}")
        if lido.get("fala"):
            partes.append(f"Fala no vídeo: {str(lido['fala']).strip()}")
        if lido.get("texto"):
            partes.append(f"Texto na tela: {str(lido['texto']).strip()}")
        return " ".join(partes)[:1500]
    if lido.get("audio") and lido.get("sem_fala"):
        return AUDIO_SEM_FALA
    if lido.get("audio"):
        corte = " (áudio longo: só o começo foi transcrito)" if lido.get("cortado") else ""
        return f"[Áudio transcrito{corte}] {str(lido.get('texto') or '').strip()}"[:1500]
    partes = ["[Imagem enviada pelo contato]"]
    if legenda:
        partes.append(f"Legenda: {legenda}.")
    if lido.get("resumo"):
        partes.append(f"O que mostra: {str(lido['resumo']).strip()}")
    if lido.get("texto"):
        partes.append(f"Texto na imagem: {str(lido['texto']).strip()}")
    return " ".join(partes)[:1500]


def _ler_midias(conn, batch, texts, kinds, leitor):
    """Le cada imagem do lote. A chave de criptografia (midia) e apagada depois, lida ou nao."""
    for i, b in enumerate(batch):
        midia = b[6]
        if kinds[i] not in ("image", "document", "audio", "video") or not midia:
            continue
        try:
            lido = leitor(b[1], midia if isinstance(midia, dict) else json.loads(midia))
        except Exception as exc:  # registra so o tipo do erro (sem conteudo) para diagnostico
            import sys
            print(f"leitor_midia falhou ({kinds[i]}): {type(exc).__name__}", file=sys.stderr, flush=True)
            lido = None
        with _cur(conn) as cur:
            if lido and (lido.get("texto") or lido.get("resumo") or lido.get("fala") or lido.get("sem_fala")):
                texts[i], kinds[i] = texto_da_midia(lido, b[3]), "text"
                cur.execute("UPDATE ecommerce_wa_mensagens SET corpo = %s, midia = NULL WHERE id = %s",
                            (texts[i], b[0]))
            else:
                cur.execute("UPDATE ecommerce_wa_mensagens SET midia = NULL WHERE id = %s", (b[0],))
        conn.commit()
    # midia que nao deu para ler, junto com mensagens entendidas: responde o resto em vez de cair
    # inteiro no "nao consigo abrir"
    if any(k == "text" and t and t != AUDIO_SEM_FALA for k, t in zip(kinds, texts)):
        for i, k in enumerate(kinds):
            if k in ("image", "document", "audio", "video"):
                kinds[i], texts[i] = "text", ""


def process_contact(conn, phone, mode, classifier, sender, state, logger=None, model_label=None, assistente=None,
                    leitor_midia=None):
    """Processa a sequencia pendente de um contato. Retorna dict de auditoria ou None.

    assistente: callable(conn, textos, tipos, identidade, historico) -> Resposta
    leitor_midia: callable(message_id, midia) -> {"texto", "resumo"} | None. Imagem lida vira
    texto e segue o fluxo normal; sem leitor ou se falhar, continua como midia.
    (poupaqui_ai_assistente.assistente_worker). Quando presente substitui o
    classificador; a resposta vem com texto BASE e, se escalar, o worker:
      1. grava a decisao (resposta ainda nao liberada para envio);
      2. envia o alerta para a equipe e registra o resultado;
      3. so entao completa o texto com o que de fato aconteceu
         (frase_encaminhamento: 'ja passei' SO com alerta entregue);
      4. reconfere takeover sob trava e libera o envio."""
    from poupaqui_ai_assistente import completar_resposta, promete_encaminhamento
    max_attempts = 3
    with _cur(conn) as cur:
        cur.execute("""
            UPDATE ecommerce_wa_mensagens
            SET status = 'processando', reservado_em = NOW(), tentativas = tentativas + 1
            WHERE id IN (SELECT id FROM ecommerce_wa_mensagens
                         WHERE telefone = %s AND direcao = 'in' AND status = 'pendente'
                         FOR UPDATE SKIP LOCKED)
            RETURNING id, message_id, tipo, corpo, nome_whatsapp, recebido_em, midia
        """, (phone,))
        batch = sorted(cur.fetchall())
    conn.commit()
    if not batch:
        return None
    ids = [b[0] for b in batch]
    EM_CURSO["ids"] = list(ids)
    primeira_em = min(b[5] for b in batch)
    status_msgs = "processado"
    try:
        texts = [b[3] for b in batch]
        kinds = [b[2] for b in batch]
        if leitor_midia is not None:
            _ler_midias(conn, batch, texts, kinds, leitor_midia)
        push_name = next((b[4] for b in reversed(batch) if b[4]), None)
        with _cur(conn) as cur:
            cur.execute("""SELECT opt_out, vinculo_manual_tipo, vinculo_manual_ref, memoria
                           FROM ecommerce_wa_contatos WHERE telefone = %s""", (phone,))
            contato = cur.fetchone() or (False, None, None, None)
        opt_out = bool(contato[0])
        identity = {"verificada": False, "metodo": None, "tipo": None, "ref": None, "candidatos": []}
        model_name = None
        origem = "regra"
        escalar, equipe, resumo = False, "equipe", None

        if any(is_opt_out(t) for t in texts):
            with _cur(conn) as cur:
                cur.execute("""UPDATE ecommerce_wa_contatos SET opt_out = TRUE, opt_out_em = NOW(),
                               atualizado_em = NOW() WHERE telefone = %s""", (phone,))
            decision, opt_out = Decision("SILENCE", "", "opt-out solicitado pelo contato"), True
        elif opt_out:
            decision = Decision("SILENCE", "", "contato com opt-out")
        elif takeover_ativo(conn, phone):
            decision, origem, status_msgs = Decision("SILENCE", "", HUMANO_ATIVO_REASON), "humano", "em_humano"
        elif any(_PEDIU_HUMANO_RE.search(normalize(t)) for t in texts):
            decision = Decision("HUMAN", "", PEDIU_HUMANO_REASON)
        elif assistente is not None:
            # o assistente responde saudacao, saude, financeiro etc. com texto
            # proprio e testado: o gate do classificador nao se aplica
            if _human_open(conn, phone, _cfg_int("POUPAQUI_WA_IA_HUMAN_LOCK_HORAS", 4)):
                decision = Decision("HUMAN", "", HUMAN_LOCK_REASON)
            else:
                identity = identify_contact(conn, phone, (contato[1], contato[2]))
                if contato[3]:
                    identity["memoria"] = contato[3]   # o que a IA ja aprendeu deste contato (perfil curto)
                hist = _history(conn, phone, ids, _cfg_int("POUPAQUI_WA_IA_HISTORICO_MAX", 20))
                conn.commit()  # sem transacao aberta durante diagnosticos/sondas
                model_name = model_label or "assistente"
                r = assistente(conn, texts, kinds, identity, hist)
                conn.rollback()
                if r.acao == "ANSWER" and promete_encaminhamento(r.reply):
                    # defesa: texto base nunca pode afirmar que a equipe foi avisada
                    decision, origem = Decision("HUMAN", "", "AI resposta afirmava encaminhamento"), "falha_ia"
                else:
                    decision = Decision(r.acao if r.acao in ("ANSWER", "HUMAN", "SILENCE") else "HUMAN",
                                        r.reply if r.acao == "ANSWER" else "", (r.motivo or r.acao)[:300])
                    tecnica = r.acao == "HUMAN" and re.match(r"(diagnostico|consulta) falhou", r.motivo or "")
                    origem = "falha_ia" if tecnica else "assistente"
                    escalar = bool(r.escalar) and r.acao == "ANSWER"
                    equipe = r.equipe or "equipe"
                    resumo = dict(r.resumo or {})
                    if r.fontes:
                        resumo["checagens"] = list(resumo.get("checagens") or []) + list(r.fontes)
                    resumo["intencao"] = r.intencao
                    anteriores = [h["texto"] for h in hist if h.get("de") == "contato" and h.get("texto")][-3:]
                    if escalar and anteriores:
                        resumo["relato_anterior"] = anteriores
        else:
            decision = preliminary_gate(texts, kinds)
            if decision is not None:
                origem = "gate"
            elif _human_open(conn, phone, _cfg_int("POUPAQUI_WA_IA_HUMAN_LOCK_HORAS", 4)):
                decision = Decision("HUMAN", "", HUMAN_LOCK_REASON)
            else:
                identity = identify_contact(conn, phone, (contato[1], contato[2]))
                facts = verified_context(conn, identity)
                payload = {
                    "identidade_verificada": identity if identity["verificada"] else None,
                    "identidade_nao_verificada": None if identity["verificada"] else {
                        "motivo": identity["metodo"],
                        "nome_exibido_no_whatsapp_nao_confiavel": push_name},
                    "dados_verificados": facts,
                    "historico_recente": _history(conn, phone, ids,
                                                  _cfg_int("POUPAQUI_WA_IA_HISTORICO_MAX", 20)),
                    "mensagens_novas": [{"tipo": k, "texto": t} for k, t in zip(kinds, texts)],
                }
                # fecha a transacao de leitura antes da IA: modelo local em CPU
                # passa dos 20s de idle_in_transaction_session_timeout
                conn.commit()
                model_name = model_label or os.getenv("POUPAQUI_AI_MODEL", DEFAULT_MODEL)
                decision = validate_answer(classifier(payload), texts, identity["verificada"], facts)
                # falha tecnica (rede, credencial, resposta invalida) vai pra
                # revisao humana mas nao trava a conversa como "em atendimento"
                origem = "falha_ia" if decision.reason.startswith(_AI_FAILURE_REASONS) else "modelo"

        # Texto a enviar: ANSWER validado, ou o aviso fixo de escalacao (opt-in).
        # frase=True -> o texto final depende do resultado REAL do alerta.
        reply = decision.reply if decision.decision == "ANSWER" else ""
        frase = escalar and bool(reply)
        if decision.reason == PEDIU_HUMANO_REASON:
            reply, frase = pediu_humano_reply(None), True  # texto fixo, nunca do modelo
        elif (decision.decision == "HUMAN" and decision.reason != HUMAN_LOCK_REASON
                and os.getenv("POUPAQUI_WA_IA_ACK_HUMANO") == "1"
                and not _ack_sent_recently(conn, phone, _cfg_int("POUPAQUI_WA_IA_ACK_HORAS", 12))):
            reply = GENERIC_ACK_REPLY

        envio = "nao_enviar"
        if reply:
            if mode in ("off", "shadow"):
                envio = "shadow"
            elif opt_out:
                envio = "bloqueado_opt_out"
            elif mode == "test" and phone not in test_phones():
                envio = "bloqueado_fora_allowlist"
            elif state["envios"] >= _cfg_int("POUPAQUI_WA_IA_MAX_ENVIOS_RUN", 10):
                envio = "bloqueado_limite_execucao"
            elif _sends_since(conn, phone, "1 hour") >= (
                    _cfg_int("POUPAQUI_WA_IA_TEST_MAX_HORA", 30)
                    if mode == "test" and phone in test_phones()
                    else _cfg_int("POUPAQUI_WA_IA_MAX_HORA", 3)):
                envio = "bloqueado_limite_hora"
            elif _sends_since(conn, phone, "1 day") >= (
                    _cfg_int("POUPAQUI_WA_IA_TEST_MAX_DIA", 100)
                    if mode == "test" and phone in test_phones()
                    else _cfg_int("POUPAQUI_WA_IA_MAX_DIA", 8)):
                envio = "bloqueado_limite_dia"
            else:
                envio = "enviando"

        # Escalacao: HUMAN (menos a trava) ou ANSWER que precisa da equipe.
        # Alerta duplicado: se a equipe ja foi alertada deste contato na janela,
        # nao alerta de novo e a resposta cita o alerta anterior (se confirmado).
        precisa_equipe = not opt_out and (escalar or (decision.decision == "HUMAN"
                                                      and decision.reason != HUMAN_LOCK_REASON))
        alertas, anterior = set(), None
        if precisa_equipe:
            anterior = _last_alert(conn, phone, _cfg_int("POUPAQUI_WA_IA_ALERTA_MIN", 60))
            if anterior is None:
                alertas = alert_phones(mode)
        alerta_status = "enviando" if alertas else None
        # a resposta so e liberada depois do alerta (fase 2)
        envio_ini = "aguardando_alerta" if (frase and alertas and envio == "enviando") else envio

        with _cur(conn) as cur:
            # Corrida com o operador: a decisao (e o 'enviando') so e gravada com
            # o contato travado; store_outbound trava a mesma linha. Se o humano
            # respondeu ou assumiu enquanto a IA pensava, nao envia.
            _lock_contact(cur, phone)
            if origem != "humano":
                cur.execute("""SELECT 1 FROM ecommerce_wa_mensagens WHERE telefone = %s
                                 AND direcao = 'out_manual' AND recebido_em >= %s LIMIT 1""", (phone, primeira_em))
                if cur.fetchone() or takeover_ativo(cur, phone):
                    decision = Decision("SILENCE", "", HUMANO_ATIVO_REASON + " (assumiu durante o processamento)")
                    origem, status_msgs, alertas, alerta_status = "humano", "respondido_humano", set(), None
                    envio = envio_ini = "cancelado_humano" if reply else "nao_enviar"
                    reply, frase, precisa_equipe = "", False, False
            if decision.reason == PEDIU_HUMANO_REASON:
                _set_takeover(cur, phone, "pedido_contato", _cfg_int("POUPAQUI_WA_IA_PEDIU_HUMANO_MIN", 240))
            cur.execute("""
                INSERT INTO ecommerce_wa_ia_decisoes
                    (telefone, message_ids, modo, decisao, resposta, motivo, origem, identidade, modelo,
                     envio_status, alerta_status, escalado, resumo)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (phone, [b[1] for b in batch], mode, decision.decision, (reply if not frase else None) or None,
                  decision.reason, origem, json.dumps(identity, ensure_ascii=False, default=str),
                  model_name, envio_ini, alerta_status, bool(escalar and precisa_equipe),
                  json.dumps(resumo, ensure_ascii=False, default=str) if resumo else None))
            decisao_id = cur.fetchone()[0]
            ja_disse = False
            if frase and not alertas and _confirmado(anterior) and reply:
                # "a equipe ja recebeu esse caso..." uma vez basta: nao repete em toda mensagem
                cur.execute("""SELECT 1 FROM ecommerce_wa_mensagens WHERE telefone = %s AND direcao = 'out_ia'
                               AND recebido_em > NOW() - INTERVAL '3 hours' AND corpo LIKE %s LIMIT 1""",
                            (phone, "%já recebeu esse caso%"))
                ja_disse = cur.fetchone() is not None
            if frase and not alertas and not ja_disse:
                # sem alerta novo: o resultado ja e conhecido (anterior confirmado,
                # sem canal, shadow, fora da allowlist) -> texto final agora
                reply = _texto_final(decision, reply, "ja_avisada" if _confirmado(anterior) else "indisponivel",
                                     anterior[0] if _confirmado(anterior) else decisao_id, equipe, anterior,
                                     completar_resposta)
                cur.execute("UPDATE ecommerce_wa_ia_decisoes SET resposta = %s WHERE id = %s", (reply, decisao_id))
            cur.execute("""UPDATE ecommerce_wa_mensagens SET status = %s, decisao_id = %s,
                           processado_em = NOW() WHERE id = ANY(%s)""", (status_msgs, decisao_id, ids))
        # Commit ANTES de enviar: se cair no meio, a decisao fica 'enviando' e
        # nunca e reenviada (no maximo uma vez).
        conn.commit()
    except Exception:
        if logger:
            logger.exception("wa_ia: falha processando contato %s", phone[-4:])
        _release(conn, ids, max_attempts)
        raise

    result = {"decisao_id": decisao_id, "telefone_fim": phone[-4:], "decisao": decision.decision,
              "origem": origem, "envio": envio_ini, "mensagens": len(batch)}
    if precisa_equipe:
        result["escalado"] = True
        if anterior is not None and not alertas:
            result["alerta"] = "ja_avisada" if _confirmado(anterior) else "duplicado_sem_confirmacao"
    if alertas:
        if identity.get("metodo") is None:  # gate decidiu antes de identificar
            try:
                identity = identify_contact(conn, phone, (contato[1], contato[2]))
                conn.commit()
            except Exception:
                conn.rollback()
        result["alerta"] = _send_alert(conn, decisao_id, phone, alertas, sender, decision, origem,
                                       identity, push_name, texts, resumo)
        if frase:
            reply = _texto_final(decision, reply, result["alerta"], decisao_id, equipe, None, completar_resposta)
            with _cur(conn) as cur:
                if envio_ini == "aguardando_alerta":
                    # o alerta levou ~1s: reconfere o operador sob a mesma trava
                    _lock_contact(cur, phone)
                    cur.execute("""SELECT 1 FROM ecommerce_wa_mensagens WHERE telefone = %s
                                     AND direcao = 'out_manual' AND recebido_em >= %s LIMIT 1""",
                                (phone, primeira_em))
                    humano = cur.fetchone() is not None or (
                        takeover_ativo(cur, phone) is not None and decision.reason != PEDIU_HUMANO_REASON)
                    envio = "cancelado_humano" if humano else "enviando"
                cur.execute("UPDATE ecommerce_wa_ia_decisoes SET resposta = %s, envio_status = %s WHERE id = %s",
                            (reply, envio if envio_ini == "aguardando_alerta" else envio_ini, decisao_id))
            conn.commit()
            result["envio"] = envio if envio_ini == "aguardando_alerta" else envio_ini
    if result["envio"] == "enviando":
        state["envios"] += 1
        status, detalhe, out_id = "falhou", "", None
        try:
            resp = sender(phone, reply)
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
                            (out_id or f"ia-{decisao_id}", phone, reply, decisao_id))
        conn.commit()
        result["envio"] = status
    return result


def contatos_para_memoria(conn, limite=1, novas_min=6):
    """Contatos com conversa nova suficiente para atualizar o perfil aprendido, parados ha 3+ min
    (nao atrapalha conversa em andamento) e sem atendimento humano ativo."""
    with _cur(conn) as cur:
        cur.execute("""
            SELECT c.telefone, c.memoria, COUNT(m.id) AS n
            FROM ecommerce_wa_contatos c
            JOIN ecommerce_wa_mensagens m ON m.telefone = c.telefone AND m.direcao = 'in'
            WHERE NOT c.opt_out
            GROUP BY c.telefone, c.memoria, c.memoria_n
            HAVING COUNT(m.id) - c.memoria_n >= %s AND MAX(m.recebido_em) < NOW() - INTERVAL '3 minutes'
            ORDER BY MAX(m.recebido_em) DESC LIMIT %s""", (novas_min, limite))
        rows = [r for r in cur.fetchall() if not takeover_ativo(cur, r[0])]
    conn.commit()
    return rows


def historico_para_memoria(conn, phone, limite=40):
    out = _history(conn, phone, [], limite)
    conn.commit()
    return out


def salvar_memoria(conn, phone, memoria, n):
    with _cur(conn) as cur:
        cur.execute("""UPDATE ecommerce_wa_contatos SET memoria = %s, memoria_em = NOW(), memoria_n = %s,
                       atualizado_em = NOW() WHERE telefone = %s""", (memoria, n, phone))
    conn.commit()


def _confirmado(anterior):
    return bool(anterior) and anterior[1] in ("enviado", "parcial")


def _texto_final(decision, base, status, protocolo, equipe, anterior, completar_resposta):
    em = anterior[2] if anterior else None
    if decision.reason == PEDIU_HUMANO_REASON:
        return pediu_humano_reply(status, protocolo, em)
    return completar_resposta(base, status, protocolo, equipe, em)


def _ack_sent_recently(conn, phone, hours):
    with _cur(conn) as cur:
        cur.execute("""SELECT 1 FROM ecommerce_wa_ia_decisoes
                       WHERE telefone = %s AND resposta = %s AND envio_status IN ('enviando', 'enviado')
                         AND criado_em > NOW() - (%s || ' hours')::interval LIMIT 1""",
                    (phone, GENERIC_ACK_REPLY, str(hours)))
        return cur.fetchone() is not None


def _last_alert(conn, phone, minutes):
    """(decisao_id, alerta_status, alerta_em) do ultimo alerta deste contato na
    janela, ou None. 'enviando' = em andamento/caiu no meio: nao realerta, mas
    tambem nao serve de prova de entrega."""
    with _cur(conn) as cur:
        cur.execute("""SELECT id, alerta_status, COALESCE(alerta_em, criado_em) FROM ecommerce_wa_ia_decisoes
                       WHERE telefone = %s AND alerta_status IN ('enviando', 'enviado', 'parcial')
                         AND criado_em > NOW() - (%s || ' minutes')::interval
                       ORDER BY id DESC LIMIT 1""", (phone, str(minutes)))
        return cur.fetchone()


def _alert_sent_recently(conn, phone, minutes):
    return _last_alert(conn, phone, minutes) is not None


_SENSIVEL_RE = [
    (re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"), "[cpf]"),
    (re.compile(r"\b(?:\d[ -]?){15,19}\b"), "[numero]"),
    (re.compile(r"(?i)\b(senha|password|pin|token)\s*[:=]?\s*\S+"), r"\1 [oculto]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[email]"),
]


def mascarar(texto):
    """Tira do alerta o que nao precisa estar la (CPF, cartao, senha, e-mail)."""
    out = str(texto or "")
    for rx, sub in _SENSIVEL_RE:
        out = rx.sub(sub, out)
    return out


def _relato_curto(falas, limite=400):
    """Falas anteriores legiveis no alerta: imagem vira so "o que mostra"; cada fala ate 160
    caracteres; se passar do limite, descarta as mais antigas inteiras (nunca corta no meio)."""
    itens = []
    for t in falas:
        t = _norm_text(t)
        if t.startswith("[Imagem enviada pelo contato]"):
            t = "[imagem] " + t.split(" Texto na imagem:")[0].replace("[Imagem enviada pelo contato]", "").strip()
        itens.append(t if len(t) <= 160 else t[:157].rstrip() + "...")
    while len(" | ".join(itens)) > limite and len(itens) > 1:
        itens.pop(0)
    return " | ".join(itens)[:limite]


def alert_text(decisao_id, phone, decision, origem, identity, push_name, texts, resumo=None):
    """Resumo tecnico objetivo: relato exato, identidade verificada, sintomas,
    checagens feitas. So o que foi observado -- nunca causa presumida."""
    if identity.get("verificada"):
        quem = f"{identity.get('tipo')}: {identity.get('nome') or identity.get('ref')}"
        if identity.get("tipo") == "loja" and identity.get("ref"):
            quem += f" / CNPJ {identity['ref']}"
    else:
        quem = f"nao verificado (nome exibido: {push_name or '-'})"
    ultima = mascarar(" | ".join(_norm_text(t) for t in texts if t))
    ultima = (ultima if len(ultima) <= 500 else ultima[:497].rstrip() + "...") or "(midia/sem texto)"
    linhas = [f"[PoupAqui IA] Atendimento humano necessario #{decisao_id}",
              f"Contato: +{phone} ({quem})"]
    resumo = resumo or {}
    if resumo.get("assunto"):
        linhas.append(f"Assunto: {resumo['assunto']}")
    if resumo.get("relato_anterior"):
        linhas.append("Relato anterior: " + mascarar(_relato_curto(resumo["relato_anterior"])))
    linhas.append(f"Relato: {ultima}")
    if resumo.get("sintomas"):
        linhas.append("Sintomas: " + "; ".join(mascarar(s) for s in resumo["sintomas"])[:400])
    if resumo.get("checagens"):
        linhas.append("Checagens: " + "; ".join(str(c) for c in resumo["checagens"])[:500])
    motivo = resumo.get("motivo_escalacao") or decision.reason
    linhas.append(f"Motivo: {str(motivo)[:160]} [{origem}]")
    return "\n".join(linhas)


def _send_alert(conn, decisao_id, phone, destinos, sender, decision, origem, identity, push_name, texts,
                resumo=None):
    """Alerta a equipe. Ja foi marcado 'enviando' junto com a decisao: nunca
    e reenviado, mesmo se o processo cair aqui. Retorna o status REAL:
    'enviado' (todos), 'parcial' (algum), 'falhou' (nenhum)."""
    texto = alert_text(decisao_id, phone, decision, origem, identity, push_name, texts, resumo)
    ok = 0
    for dest in sorted(destinos):
        try:
            if sender(dest, texto):
                ok += 1
        except Exception:
            pass
    status = "enviado" if ok == len(destinos) else ("parcial" if ok else "falhou")
    with _cur(conn) as cur:
        cur.execute("""UPDATE ecommerce_wa_ia_decisoes SET alerta_status = %s,
                       alerta_em = CASE WHEN %s <> 'falhou' THEN NOW() END WHERE id = %s""",
                    (status, status, decisao_id))
    conn.commit()
    return status


# o que o worker tem nas maos agora: num desligamento (systemctl restart) as mensagens voltam para a
# fila na hora, em vez de ficarem 'processando' ate 10 min e o contato sem resposta
EM_CURSO = {"ids": []}


def devolver_em_curso(conn):
    """Mensagens reservadas por este processo voltam a 'pendente' (desligamento no meio da resposta)."""
    ids, EM_CURSO["ids"] = EM_CURSO["ids"], []
    if not ids:
        return 0
    with _cur(conn) as cur:
        cur.execute("""UPDATE ecommerce_wa_mensagens SET status = 'pendente', reservado_em = NULL,
                       tentativas = GREATEST(tentativas - 1, 0) WHERE id = ANY(%s) AND status = 'processando'""",
                    (ids,))
        n = cur.rowcount
    conn.commit()
    return n


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


def renew_worker_lease(conn, token):
    """Estende o lease (worker local com IA em CPU pode passar do TTL)."""
    with _cur(conn) as cur:
        cur.execute("UPDATE ecommerce_wa_ia_config SET atualizado_em = NOW() WHERE chave = 'worker_lease' AND valor = %s",
                    (token,))
        ok = cur.rowcount == 1
    conn.commit()
    return ok


def release_worker_lease(conn, token):
    with _cur(conn) as cur:
        cur.execute("UPDATE ecommerce_wa_ia_config SET valor = 'livre' WHERE chave = 'worker_lease' AND valor = %s",
                    (token,))
    conn.commit()


def run_worker(conn, classifier, sender, logger=None, max_contacts=None, engine="servidor", model_label=None,
               assistente=None, leitor_midia=None):
    ensure_schema(conn)
    mode = effective_mode(get_db_mode(conn))
    motor = get_db_engine(conn)
    summary = {"modo": mode, "motor": motor, "contatos": 0, "resultados": [], "erros": 0}
    if mode == "off":
        return summary
    if engine != motor:
        summary["ignorado"] = f"motor ativo e '{motor}', este worker e '{engine}'"
        return summary
    token = acquire_worker_lease(conn)
    if not token:
        summary["ignorado"] = "outra execucao em andamento"
        return summary
    try:
        debounce = _cfg_int("POUPAQUI_WA_IA_DEBOUNCE_SEG", 40)
        max_age = _cfg_int("POUPAQUI_WA_IA_MAX_IDADE_MIN", 360)
        max_contacts = max_contacts or _cfg_int("POUPAQUI_WA_IA_MAX_CONTATOS_RUN", 8)
        # em test so a allowlist entra no worker: contato real nao e consumido, expirado nem
        # gera alerta (antes era processado e so bloqueado no envio)
        escopo, escopo_args = "", ()
        if mode == "test":
            escopo, escopo_args = " AND telefone = ANY(%s)", (sorted(test_phones()),)
        with _cur(conn) as cur:
            # reservas presas (worker caiu no meio) voltam pra fila
            cur.execute("""UPDATE ecommerce_wa_mensagens
                           SET status = CASE WHEN tentativas >= 3 THEN 'erro' ELSE 'pendente' END,
                               reservado_em = NULL
                           WHERE status = 'processando' AND reservado_em < NOW() - INTERVAL '10 minutes'""")
            # nao responde conversa velha (ex.: acumulada enquanto estava 'off')
            cur.execute("""UPDATE ecommerce_wa_mensagens SET status = 'expirado', processado_em = NOW()
                           WHERE status = 'pendente' AND direcao = 'in'
                             AND recebido_em < NOW() - (%s || ' minutes')::interval""" + escopo,
                        (str(max_age),) + escopo_args)
            summary["expiradas"] = cur.rowcount
            # so pega contato cuja ultima mensagem tem >= debounce (sequencia terminou)
            cur.execute("""SELECT telefone FROM ecommerce_wa_mensagens
                           WHERE status = 'pendente' AND direcao = 'in'""" + escopo + """
                           GROUP BY telefone
                           HAVING MAX(recebido_em) < NOW() - (%s || ' seconds')::interval
                           ORDER BY MIN(recebido_em) LIMIT %s""", escopo_args + (str(debounce), max_contacts))
            phones = [r[0] for r in cur.fetchall()]
        conn.commit()
        state = {"envios": 0}
        for phone in phones:
            # lease perdido (expirou e outro worker pegou): para sem processar mais
            if not renew_worker_lease(conn, token):
                summary["interrompido"] = "lease perdido"
                break
            try:
                res = process_contact(conn, phone, mode, classifier, sender, state, logger, model_label,
                                      assistente, leitor_midia)
            except Exception:
                EM_CURSO["ids"] = []   # erro normal: o proprio fluxo ja tratou as mensagens
                summary["erros"] += 1
                continue
            EM_CURSO["ids"] = []       # concluido; num desligamento (SystemExit) a lista fica para devolver
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
            "modo_efetivo": effective_mode(db_mode), "motor": get_db_engine(conn), "fila": fila,
            "decisoes_24h": dec, "humano_pendente": humanos}


def pending_human(conn, limit=50):
    """Fila de escalacoes abertas pra equipe (mais antigas primeiro)."""
    ensure_schema(conn)
    with _cur(conn) as cur:
        cur.execute("""
            SELECT d.id, d.telefone, d.criado_em, d.motivo, d.origem, d.alerta_status, d.resumo->>'assunto',
                   (SELECT string_agg(m.corpo, ' | ' ORDER BY m.id) FROM ecommerce_wa_mensagens m
                     WHERE m.message_id = ANY(d.message_ids)) AS texto
            FROM ecommerce_wa_ia_decisoes d
            WHERE (d.decisao = 'HUMAN' OR d.escalado) AND d.resolvido_em IS NULL AND d.motivo <> %s
            ORDER BY d.criado_em LIMIT %s""", (HUMAN_LOCK_REASON, limit))
        rows = cur.fetchall()
    conn.commit()
    return [{"id": i, "telefone": t, "em": e.isoformat() if e else None, "motivo": m, "origem": o,
             "alerta": a, "assunto": s, "texto": (x or "")[:300]} for i, t, e, m, o, a, s, x in rows]


def resolve_human(conn, phone):
    """Encerra as escalacoes abertas de um contato (libera a trava humana)."""
    with _cur(conn) as cur:
        cur.execute("""UPDATE ecommerce_wa_ia_decisoes SET resolvido_em = NOW()
                       WHERE telefone = %s AND (decisao = 'HUMAN' OR escalado) AND resolvido_em IS NULL""",
                    (normalize_phone(phone),))
        n = cur.rowcount
    conn.commit()
    return n
