"""
POUPAQUI ECOMMERCE
Ecommerce público da rede Poupaqui — inicialmente somente produtos DNS/Vitnatu.

Dois perfis:
  • Consumidor final  — catálogo aberto sem login  /  /loja/<cnpj>
  • Painel da loja    — gestão de preços com login  /painel/...
"""
import os
import sys
import math
import time
import json
import random
import secrets
import threading
import urllib.request
import urllib.parse
import html
from datetime import datetime, timezone, timedelta
from functools import wraps
import re
import ssl

import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import generate_password_hash, check_password_hash
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, send_from_directory, Response
)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "poupaqui-ecommerce-dev-2026")
app.permanent_session_lifetime = timedelta(days=30)

GENERIC_TARJA_VERMELHA_IMG = "https://res.cloudinary.com/dizfq460q/image/upload/v1778783063/CAIXA_GEN%C3%89RICO_-_POUPAQUI_itiyth.jpg"
GENERIC_TARJA_PRETA_IMG = "https://res.cloudinary.com/dizfq460q/image/upload/v1778783450/ChatGPT_Image_14_de_mai._de_2026_15_30_35_wuovpb.png"

SUPABASE_URL  = "https://wosjlqxbfajctoeztrug.supabase.co"
SUPABASE_ANON = ""  # preenchido após dotenv


@app.get("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "favicon.png", mimetype="image/png")

# ─── dotenv (desenvolvimento local) ───────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

SUPABASE_ANON = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# ─── MERCADO LIVRE ─────────────────────────────────────────────────────────────
ML_APP_ID    = os.getenv("ML_APP_ID", "")
ML_SECRET    = os.getenv("ML_CLIENT_SECRET", "")
ML_REDIRECT  = os.getenv("ML_REDIRECT_URI", "https://ecommerce-2-rosy.vercel.app/ml/callback")
ML_AUTH_URL  = "https://auth.mercadolivre.com.br/authorization"
ML_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
ML_API_BASE  = "https://api.mercadolibre.com"

# ─── CLOUDINARY ───────────────────────────────────────────────────────────────
try:
    import cloudinary
    import cloudinary.uploader
    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME", ""),
        api_key=os.getenv("CLOUDINARY_API_KEY", ""),
        api_secret=os.getenv("CLOUDINARY_API_SECRET", ""),
    )
    _CLOUDINARY_OK = bool(os.getenv("CLOUDINARY_CLOUD_NAME"))
except Exception:
    _CLOUDINARY_OK = False

def _upload_receita_cloudinary(file_bytes, filename):
    """Faz upload de PDF de receita no Cloudinary (resource_type raw). Retorna URL segura ou None."""
    if not _CLOUDINARY_OK:
        return None
    try:
        result = cloudinary.uploader.upload(
            (filename, file_bytes),
            resource_type="raw",
            folder="receitas",
            use_filename=True,
            unique_filename=True,
        )
        return result.get("secure_url")
    except Exception as e:
        app.logger.error("Cloudinary upload error: %s", e)
        return None

# ─── E-MAIL (Resend) ──────────────────────────────────────────────────────────

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM    = os.getenv("RESEND_FROM", "Poupaqui <noreply@drogariaspoupaqui.com.br>")


def _send_email(to: str, subject: str, html_body: str) -> bool:
    """Envia e-mail via Resend API. Retorna True se enviou, False se falhou/não configurado."""
    if not RESEND_API_KEY or not to or "@" not in to:
        app.logger.warning("_send_email skipped: key=%s to=%s", bool(RESEND_API_KEY), to)
        return False
    try:
        payload = json.dumps({"from": RESEND_FROM, "to": [to], "subject": subject, "html": html_body}).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "PoupaquiApp/1.0",
            },
            method="POST",
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
            app.logger.info("_send_email ok: status=%s to=%s", resp.status, to)
            return resp.status in (200, 201)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        app.logger.warning("EMAIL_ERR code=%s key_prefix=%s", exc.code, RESEND_API_KEY[:12])
        app.logger.warning("EMAIL_ERR from=%s to=%s", RESEND_FROM, to)
        app.logger.warning("EMAIL_ERR body=%s", body[:300])
        return False
    except Exception as exc:
        app.logger.warning("EMAIL_ERR exc=%s key_prefix=%s from=%s to=%s", str(exc)[:200], RESEND_API_KEY[:12], RESEND_FROM, to)
        return False


def _email_html_wrapper(titulo: str, conteudo: str) -> str:
    """Envolve conteúdo em layout HTML de e-mail simples no estilo Poupáqui."""
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"/>
<style>
  body{{margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;}}
  .wrap{{max-width:600px;margin:32px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08);}}
  .header{{background:#c8102e;padding:24px 32px;text-align:center;}}
  .header h1{{margin:0;color:#fff;font-size:1.4rem;}}
  .body{{padding:28px 32px;color:#222;font-size:.95rem;line-height:1.6;}}
  .footer{{background:#f9f9f9;border-top:1px solid #eee;padding:16px 32px;text-align:center;font-size:.78rem;color:#888;}}
  .btn{{display:inline-block;margin:16px 0;padding:12px 28px;background:#c8102e;color:#fff;text-decoration:none;border-radius:8px;font-weight:bold;}}
  .info-box{{background:#fff9e6;border:1px solid #f5c842;border-radius:8px;padding:14px 18px;margin:16px 0;}}
</style></head><body>
<div class="wrap">
  <div class="header"><h1>Poupáqui</h1></div>
  <div class="body"><h2 style="margin-top:0;color:#c8102e">{titulo}</h2>{conteudo}</div>
  <div class="footer">Poupáqui — Sua farmácia de confiança · <a href="https://drogariaspoupaqui.com.br" style="color:#c8102e">drogariaspoupaqui.com.br</a></div>
</div></body></html>"""


def _enviar_email_verificacao(user_id: str, email: str) -> bool:
    """Gera token de verificação, persiste no banco e envia e-mail ao consumidor."""
    try:
        token = secrets.token_urlsafe(32)
        conn = db(); cur = conn.cursor()
        cur.execute(
            "UPDATE ecommerce_consumidores SET email_token=%s, email_token_enviado_em=NOW() WHERE id=%s",
            (token, user_id),
        )
        conn.commit(); cur.close()
    except Exception as exc:
        app.logger.warning("_enviar_email_verificacao db error: %s", exc)
        return False
    try:
        base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/") or request.host_url.rstrip("/")
    except Exception:
        base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    link = f"{base}/verificar-email/{token}"
    corpo = (
        f"<p>Clique abaixo para confirmar seu e-mail na Poupáqui:</p>"
        f"<p><a class='btn' href='{link}'>Confirmar meu e-mail</a></p>"
        f"<p style='font-size:.82rem;color:#888'>Se não foi você, ignore este e-mail.</p>"
    )
    return _send_email(email, "✉️ Confirme seu e-mail — Poupáqui", _email_html_wrapper("Confirme seu e-mail", corpo))


# ─── RATE LIMITING (in-memory, best-effort) ───────────────────────────────────

_rl_store: dict = {}
_rl_lock = threading.Lock()


def _rate_limit_check(key: str, max_calls: int, window_secs: int) -> bool:
    """Retorna True se a chamada é permitida, False se excedeu o limite."""
    now = time.time()
    with _rl_lock:
        calls = _rl_store.get(key, [])
        calls = [t for t in calls if now - t < window_secs]
        if len(calls) >= max_calls:
            _rl_store[key] = calls
            return False
        calls.append(now)
        _rl_store[key] = calls
        # limpa entradas antigas para não crescer indefinidamente
        if len(_rl_store) > 5000:
            _rl_store.clear()
        return True


def _rate_limited_api(max_calls: int = 60, window_secs: int = 60):
    """Decorator de rate limit por IP para rotas públicas."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            limit = int(os.getenv("RATE_LIMIT_PUBLIC_API", str(max_calls)))
            ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
            if not _rate_limit_check(f"{f.__name__}:{ip}", limit, window_secs):
                return jsonify({"error": "Muitas requisições. Tente novamente em instantes."}), 429
            return f(*args, **kwargs)
        return wrapper
    return decorator


# ─── DATABASE ─────────────────────────────────────────────────────────────────

_thread_local = threading.local()
_schema_ready = set()
_schema_lock = threading.Lock()
_migrations_loaded = False


def _load_db_migrations():
    """Carrega do banco quais migrações já foram aplicadas (1 query por cold-start)."""
    global _migrations_loaded
    if _migrations_loaded:
        return
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pq_migrations (
                key TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.execute("SELECT key FROM pq_migrations")
        for row in cur.fetchall():
            _schema_ready.add(row["key"])
        cur.close()
        _migrations_loaded = True
    except Exception:
        pass


def _mark_migration_done(key):
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO pq_migrations (key) VALUES (%s) ON CONFLICT DO NOTHING",
            (key,),
        )
        conn.commit()
        cur.close()
    except Exception:
        pass


def _new_conn(statement_timeout_ms: int = 10000):
    dsn = os.getenv("DATABASE_URL") or os.getenv("DDATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL não configurada")
    return psycopg2.connect(
        dsn,
        cursor_factory=RealDictCursor,
        connect_timeout=10,
        options=f"-c statement_timeout={statement_timeout_ms}",
    )


def _new_conn_batch():
    """Conexão dedicada para queries de batch — timeout maior."""
    return _new_conn(statement_timeout_ms=55000)


def db():
    import psycopg2.extensions as pge
    conn = getattr(_thread_local, "conn", None)
    if conn is not None and not conn.closed and conn.status == pge.STATUS_READY:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
    conn = _new_conn()
    _thread_local.conn = conn
    return conn


def reset_db_conn():
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _thread_local.conn = None


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def fmt_brl(val):
    try:
        v = float(val or 0)
    except Exception:
        v = 0.0
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@app.context_processor
def inject_globals():
    consumidor = None
    consumidor_rec_abertas = 0
    if session.get("consumidor_id"):
        consumidor = {
            "id": session.get("consumidor_id"),
            "nome": session.get("consumidor_nome"),
            "email": session.get("consumidor_email"),
            "telefone": session.get("consumidor_telefone"),
            "endereco": session.get("consumidor_endereco"),
            "lat": session.get("consumidor_lat"),
            "lng": session.get("consumidor_lng"),
        }
        try:
            cur = db().cursor()
            cur.execute(
                "SELECT COUNT(*) AS n FROM ecommerce_reclamacoes WHERE consumidor_id=%s AND status NOT IN ('finalizada')",
                (session["consumidor_id"],),
            )
            consumidor_rec_abertas = (cur.fetchone() or {}).get("n", 0) or 0
            cur.close()
        except Exception:
            consumidor_rec_abertas = 0
    return {
        "money": fmt_brl,
        "now": datetime.now(timezone.utc),
        "consumidor": consumidor,
        "consumidor_rec_abertas": consumidor_rec_abertas,
    }


def _ensure_precificador_schema():
    _load_db_migrations()
    if "precificador" in _schema_ready:
        return
    with _schema_lock:
        if "precificador" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_catalogo_oculto (
                cnpjloja TEXT NOT NULL,
                ean TEXT NOT NULL,
                PRIMARY KEY (cnpjloja, ean)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_catalogo_extra (
                cnpjloja TEXT NOT NULL,
                ean TEXT NOT NULL,
                PRIMARY KEY (cnpjloja, ean)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_produto_imagens (
                id SERIAL PRIMARY KEY,
                cnpjloja TEXT NOT NULL,
                ean TEXT NOT NULL,
                imagem_url TEXT NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (cnpjloja, ean)
            )
        """)
        conn.commit()
        cur.close()
        _schema_ready.add("precificador")
        _mark_migration_done("precificador")


def _ensure_catalog_admin_schema():
    _ensure_precificador_schema()
    key = "catalog_admin"
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS catalogo_publico BOOLEAN DEFAULT TRUE")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS estoque_min_publicacao INTEGER DEFAULT 5")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS categorias_publicacao TEXT DEFAULT 'todos'")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS catalogo_sync_em TIMESTAMPTZ")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS catalogo_sync_total INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS catalogo_sync_publicados INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS catalogo_sync_sem_imagem INTEGER DEFAULT 0")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_consumidor_schema():
    _load_db_migrations()
    if "consumidor" in _schema_ready:
        return
    with _schema_lock:
        if "consumidor" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ecommerce_consumidores (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                nome TEXT NOT NULL,
                telefone TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                senha_hash TEXT NOT NULL,
                criado_em TIMESTAMPTZ DEFAULT NOW(),
                atualizado_em TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS consumidor_id UUID")
        cur.execute("ALTER TABLE ecommerce_consumidores ADD COLUMN IF NOT EXISTS endereco TEXT")
        cur.execute("ALTER TABLE ecommerce_consumidores ADD COLUMN IF NOT EXISTS endereco_lat DOUBLE PRECISION")
        cur.execute("ALTER TABLE ecommerce_consumidores ADD COLUMN IF NOT EXISTS endereco_lng DOUBLE PRECISION")
        conn.commit()
        cur.close()
        _schema_ready.add("consumidor")
        _mark_migration_done("consumidor")
    _ensure_consumidor_auth_columns()


def _ensure_consumidor_auth_columns():
    """Migração separada para colunas de verificação de e-mail e reset de senha."""
    key = "consumidor_auth_v1"
    if key in _schema_ready:
        return
    _load_db_migrations()
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("ALTER TABLE ecommerce_consumidores ADD COLUMN IF NOT EXISTS email_verificado BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE ecommerce_consumidores ADD COLUMN IF NOT EXISTS email_token TEXT")
        cur.execute("ALTER TABLE ecommerce_consumidores ADD COLUMN IF NOT EXISTS email_token_enviado_em TIMESTAMPTZ")
        cur.execute("ALTER TABLE ecommerce_consumidores ADD COLUMN IF NOT EXISTS reset_token TEXT")
        cur.execute("ALTER TABLE ecommerce_consumidores ADD COLUMN IF NOT EXISTS reset_token_expira TIMESTAMPTZ")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_delivery_schema():
    _ensure_consumidor_schema()
    if "delivery" in _schema_ready:
        return
    with _schema_lock:
        if "delivery" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS aceita_entrega BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS raio_entrega_km NUMERIC DEFAULT 0")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS cobra_frete BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS valor_frete NUMERIC DEFAULT 0")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS pedido_minimo_entrega NUMERIC DEFAULT 0")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS tipo_entrega TEXT DEFAULT 'retirada'")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS endereco_entrega TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS entrega_lat DOUBLE PRECISION")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS entrega_lng DOUBLE PRECISION")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS entrega_distancia_km NUMERIC")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS frete_valor NUMERIC DEFAULT 0")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS codigo_entrega TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS entregue_em TIMESTAMPTZ")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS cupom_id UUID")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS desconto_cupom NUMERIC DEFAULT 0")
        conn.commit()
        cur.close()
        _schema_ready.add("delivery")
        _mark_migration_done("delivery")
    _ensure_loja_email_column()


def _ensure_loja_email_column():
    """Migração separada para email_notificacao na config da loja."""
    key = "loja_email_notif_v1"
    if key in _schema_ready:
        return
    _load_db_migrations()
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS email_notificacao TEXT")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_receita_schema():
    _ensure_delivery_schema()
    if "receita" in _schema_ready:
        return
    with _schema_lock:
        if "receita" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS receita_url TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS receita_status TEXT DEFAULT NULL")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS receita_reprovada_motivo TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS receita_avaliada_em TIMESTAMPTZ")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS receita_declaracao_digital_valida BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS receita_validou_assinatura BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS receita_validou_prescritor BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS receita_validou_uso_unico BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS receita_validou_regras_sanitarias BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS whatsapp_receita TEXT")
        conn.commit()
        cur.close()
        _schema_ready.add("receita")
        _mark_migration_done("receita")


def _ensure_reclamacao_schema():
    _ensure_receita_schema()
    if "reclamacao" in _schema_ready:
        return
    with _schema_lock:
        if "reclamacao" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_reclamacoes (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                pedido_id UUID NOT NULL,
                cnpjloja TEXT NOT NULL,
                consumidor_id UUID NOT NULL,
                motivo TEXT NOT NULL,
                descricao TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'aberta',
                prazo_loja_responder TIMESTAMPTZ,
                prazo_cliente_confirmar TIMESTAMPTZ,
                advertencia_loja BOOLEAN DEFAULT FALSE,
                aberta_em TIMESTAMPTZ DEFAULT NOW(),
                finalizada_em TIMESTAMPTZ
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_reclamacao_msgs (
                id SERIAL PRIMARY KEY,
                reclamacao_id UUID NOT NULL,
                autor TEXT NOT NULL,
                mensagem TEXT NOT NULL,
                enviada_em TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_loja_advertencias (
                id SERIAL PRIMARY KEY,
                cnpjloja TEXT NOT NULL,
                reclamacao_id UUID,
                motivo TEXT NOT NULL,
                tipo TEXT NOT NULL DEFAULT 'advertencia',
                criada_em TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS prioridade_reduzida BOOLEAN DEFAULT FALSE")
        conn.commit()
        cur.close()
        _schema_ready.add("reclamacao")
        _mark_migration_done("reclamacao")


def _processar_prazos_reclamacao(rec_id):
    """Verifica e aplica penalidades/auto-finalização por prazo vencido. Idempotente."""
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM ecommerce_reclamacoes WHERE id=%s LIMIT 1", (rec_id,))
        rec = cur.fetchone()
        if not rec:
            cur.close()
            return
        now = datetime.now(timezone.utc)
        status = rec["status"]

        if (status == "aberta"
                and rec.get("prazo_loja_responder")
                and now > rec["prazo_loja_responder"]
                and not rec.get("advertencia_loja")):
            # Prazo da loja venceu → advertência
            cnpjloja = rec["cnpjloja"]
            cur.execute(
                "UPDATE ecommerce_reclamacoes SET advertencia_loja=TRUE WHERE id=%s",
                (rec_id,)
            )
            cur.execute(
                """
                INSERT INTO ecommerce_loja_advertencias (cnpjloja, reclamacao_id, motivo, tipo)
                VALUES (%s, %s, %s, 'advertencia')
                """,
                (cnpjloja, rec_id, "Não respondeu reclamação dentro de 48 horas")
            )
            # Conta advertências ativas → reduz prioridade a partir de 3
            cur.execute(
                "SELECT COUNT(*) AS total FROM ecommerce_loja_advertencias WHERE cnpjloja=%s",
                (cnpjloja,)
            )
            total = (cur.fetchone() or {}).get("total", 0) or 0
            if total >= 3:
                cur.execute(
                    "UPDATE ecommerce_config_loja SET prioridade_reduzida=TRUE WHERE cnpjloja=%s",
                    (cnpjloja,)
                )
            conn.commit()

        elif status == "aguardando_cliente" and rec.get("prazo_cliente_confirmar") and now > rec["prazo_cliente_confirmar"]:
            # Prazo do cliente venceu → auto-finaliza sem punição
            cur.execute(
                "UPDATE ecommerce_reclamacoes SET status='finalizada', finalizada_em=NOW() WHERE id=%s",
                (rec_id,)
            )
            conn.commit()

        cur.close()
    except Exception:
        pass


def _ensure_competitor_schema():
    _load_db_migrations()
    if "competitor" in _schema_ready:
        return
    with _schema_lock:
        if "competitor" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_competitor_prices (
                ean TEXT NOT NULL,
                concorrente TEXT NOT NULL,
                nome TEXT,
                preco NUMERIC(10,2),
                preco_original NUMERIC(10,2),
                disponivel BOOLEAN DEFAULT TRUE,
                url TEXT,
                consultado_em TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (ean, concorrente)
            )
        """)
        conn.commit()
        cur.close()
        _schema_ready.add("competitor")
        _mark_migration_done("competitor")


_CONCORRENTES = {
    # Droga Raia usa VTEX IO headless (FastStore/Next.js).
    # O backend VTEX bloqueia acesso externo (CloudFront 403).
    # Estratégia: raspar a página de busca pública SSR como fonte de preço.
    "drogaraia": {
        "nome": "Droga Raia",
        "base": None,              # sem VTEX API público acessível
        "vtex_host": None,
        "base_fallbacks": [],
        "scrape_search": True,    # usa _fetch_raia_via_search_page
        "search_url": "https://www.drogaraia.com.br/search?w={ean}",
        "cor": "#e11d48",
    },
    "drogariasaopaulo": {
        "nome": "Drogaria SP",
        "base": "https://www.drogariasaopaulo.com.br",
        "vtex_host": None,
        "base_fallbacks": [],
        "search_url": "https://www.drogariasaopaulo.com.br/busca/?q={ean}",
        "cor": "#1d4ed8",
    },
}

_VTEX_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_VTEX_HEADERS = {
    "User-Agent": _VTEX_UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def _vtex_get_json(url, timeout=10, host_override=None):
    """GET com TLS fingerprint de Chrome (curl_cffi); retorna objeto Python ou None.
    host_override: seta o header HTTP Host sem alterar o destino TLS — necessário
    para lojas VTEX IO que resolvem o binding pelo Host header (ex: Droga Raia)."""
    referer = url.split("/_v")[0].split("/api")[0] + "/"
    headers = {**_VTEX_HEADERS, "Referer": referer}
    if host_override:
        headers["Host"] = host_override
    try:
        from curl_cffi import requests as cffi_requests
        r = cffi_requests.get(
            url,
            headers=headers,
            impersonate="chrome124",
            timeout=timeout,
            allow_redirects=True,
        )
        if r.status_code != 200 or not r.content:
            return None
        return r.json()
    except ImportError:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except Exception:
            return None
    except Exception:
        return None


def _parse_vtex_catalog(data, base_url):
    """Extrai preço da resposta do endpoint legado /api/catalog_system."""
    if not isinstance(data, list) or not data:
        return None
    produto = data[0]
    nome = produto.get("productName") or produto.get("productTitle")
    link = produto.get("link") or produto.get("linkText") or ""
    if link and not link.startswith("http"):
        link = f"{base_url}/{link.lstrip('/')}"

    preco = preco_original = None
    disponivel = False
    for item in produto.get("items", []):
        for seller in item.get("sellers", []):
            offer = seller.get("commertialOffer", {})
            qty = offer.get("AvailableQuantity", 0)
            if qty and qty > 0:
                disponivel = True
            p  = offer.get("Price")
            lp = offer.get("ListPrice")
            if p and (preco is None or p < preco):
                preco = p
                preco_original = lp
        if preco is not None:
            break

    if preco is None and not disponivel:
        return {"disponivel": False, "preco": None, "preco_original": None, "url": link or None, "nome": nome}
    return {"disponivel": disponivel, "preco": preco, "preco_original": preco_original, "url": link or None, "nome": nome}


def _parse_vtex_intelligent(data, base_url):
    """Extrai preço da resposta do endpoint _v/api/intelligent-search."""
    products = (data or {}).get("products") or []
    if not products:
        return None
    p = products[0]
    nome = p.get("productName") or p.get("name")
    link = p.get("link") or p.get("linkText") or ""
    if link and not link.startswith("http"):
        link = f"{base_url}/{link.lstrip('/')}"

    pr = p.get("priceRange", {})
    selling = pr.get("sellingPrice", {})
    listing = pr.get("listPrice", {})
    preco = selling.get("lowPrice") or selling.get("highPrice")
    preco_original = listing.get("highPrice") or listing.get("lowPrice")
    disponivel = bool(preco and preco > 0)

    if not disponivel:
        return {"disponivel": False, "preco": None, "preco_original": None, "url": link or None, "nome": nome}
    return {"disponivel": True, "preco": preco, "preco_original": preco_original, "url": link or None, "nome": nome}


def _build_vtex_attempts(base_url):
    # channel é obrigatório em algumas contas VTEX IO (ex: drogaraia)
    ch = "%7B%22salesChannel%22%3A%221%22%7D"  # {"salesChannel":"1"} url-encoded
    return [
        # IS sem channel (funciona na maioria das lojas)
        (
            f"{base_url}/_v/api/intelligent-search/product_search"
            f"?query={{ean}}&page=1&count=1&sort=&operator=and&fuzzy=0",
            _parse_vtex_intelligent,
        ),
        # IS com channel explícito (necessário em contas VTEX IO headless)
        (
            f"{base_url}/_v/api/intelligent-search/product_search"
            f"?query={{ean}}&page=1&count=1&sort=&operator=and&fuzzy=0&channel={ch}",
            _parse_vtex_intelligent,
        ),
        # Catalog API por EAN
        (
            f"{base_url}/api/catalog_system/pub/products/search"
            f"?fq=alternateIdValues:{{ean}}&_from=0&_to=1&sc=1",
            _parse_vtex_catalog,
        ),
        # Catalog API full-text
        (
            f"{base_url}/api/catalog_system/pub/products/search"
            f"?ft={{ean}}&_from=0&_to=1&sc=1",
            _parse_vtex_catalog,
        ),
    ]


def _find_json_value(obj, key, _d=0):
    """Busca recursiva de uma chave em dict/list aninhado (máx 12 níveis)."""
    if _d > 12:
        return None
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], (int, float, str)) and obj[key]:
            return obj[key]
        for v in obj.values():
            r = _find_json_value(v, key, _d + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _find_json_value(item, key, _d + 1)
            if r is not None:
                return r
    return None


def _fetch_raia_product_page_price(url):
    """Extrai preço final da página de produto da Droga Raia."""
    import re
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return None
    try:
        r = cffi_requests.get(
            url,
            headers={
                "User-Agent": _VTEX_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                "Referer": "https://www.drogaraia.com.br/",
            },
            impersonate="chrome124",
            timeout=18,
            allow_redirects=True,
        )
        if r.status_code != 200 or not r.text:
            return None
        html = r.text

        # JSON-LD do produto costuma trazer o preço final exibido no card de compra.
        for raw_ld in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE):
            try:
                ld = json.loads(raw_ld)
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    if str(item.get("@type", "")).lower() == "product":
                        offers = item.get("offers") or {}
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        price = offers.get("price") or offers.get("lowPrice")
                        if price:
                            return {
                                "preco": float(price),
                                "preco_original": None,
                                "nome": item.get("name"),
                            }
            except Exception:
                pass

        m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
        if not m:
            return None
        nd = json.loads(m.group(1))
        product_data = nd.get("props", {}).get("pageProps", {}).get("productData", {})
        price_aux = product_data.get("price_aux") or {}
        live_price = ((product_data.get("liveComposition") or {}).get("livePrice") or {})
        price = (
            price_aux.get("value_to")
            or live_price.get("valueTo")
            or live_price.get("bestPrice")
            or product_data.get("price")
        )
        original = price_aux.get("value_from") or live_price.get("valueFrom")
        if price:
            return {
                "preco": float(price),
                "preco_original": float(original) if original else None,
                "nome": product_data.get("name") or product_data.get("productName"),
            }
    except Exception:
        return None
    return None


def _fetch_raia_via_search_page(ean):
    """
    Fallback para Droga Raia: raspa a página de busca SSR (FastStore/Next.js).
    Extrai preço via JSON-LD (structured data) ou __NEXT_DATA__.
    """
    import re
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return None
    url = f"https://www.drogaraia.com.br/search?w={urllib.parse.quote(str(ean))}"
    try:
        r = cffi_requests.get(
            url,
            headers={
                "User-Agent": _VTEX_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                "Referer": "https://www.drogaraia.com.br/",
            },
            impersonate="chrome124",
            timeout=18,
            allow_redirects=True,
        )
        if r.status_code != 200 or not r.text:
            return None
        html = r.text

        # 1) JSON-LD structured data
        for raw_ld in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE):
            try:
                ld = json.loads(raw_ld)
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    if str(item.get("@type", "")).lower() == "product":
                        offers = item.get("offers") or {}
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        price = offers.get("lowPrice") or offers.get("price")
                        if price:
                            return {
                                "disponivel": True,
                                "preco": float(price),
                                "preco_original": None,
                                "url": item.get("url") or url,
                                "nome": item.get("name"),
                            }
            except Exception:
                pass

        # 2) __NEXT_DATA__ (FastStore SSR)
        m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
        if m:
            try:
                nd = json.loads(m.group(1))
                results = (
                    nd.get("props", {})
                      .get("pageProps", {})
                      .get("pageProps", {})
                      .get("results", {})
                )
                products = results.get("products") if isinstance(results, dict) else None
                if products:
                    product = products[0]
                    price = product.get("priceService") or product.get("price")
                    link = product.get("url") or product.get("urlLandingPage")
                    if link and not link.startswith("http"):
                        link = f"https://www.drogaraia.com.br/{link.lstrip('/')}"
                    detail_price = _fetch_raia_product_page_price(link) if link else None
                    if detail_price and detail_price.get("preco"):
                        return {
                            "disponivel": True,
                            "preco": detail_price["preco"],
                            "preco_original": detail_price.get("preco_original"),
                            "url": link or url,
                            "nome": detail_price.get("nome") or product.get("name") or product.get("productName"),
                        }
                    if price:
                        return {
                            "disponivel": True,
                            "preco": float(price),
                            "preco_original": None,
                            "url": link or url,
                            "nome": product.get("name") or product.get("productName"),
                        }
                price = _find_json_value(nd, "lowPrice") or _find_json_value(nd, "spotPrice")
                nome = _find_json_value(nd, "productName") or _find_json_value(nd, "name")
                link = _find_json_value(nd, "linkText") or _find_json_value(nd, "slug")
                if link and not link.startswith("http"):
                    link = f"https://www.drogaraia.com.br/{link.lstrip('/')}"
                if price:
                    return {
                        "disponivel": True,
                        "preco": float(price),
                        "preco_original": None,
                        "url": link or url,
                        "nome": nome,
                    }
            except Exception:
                pass

        # 3) Regex de preço como último recurso (ex: "priceService":15.19)
        m2 = re.search(r'"(?:priceService|lowPrice|spotPrice|sellingPrice)"\s*:\s*([0-9]+(?:\.[0-9]+)?)', html)
        if m2:
            return {
                "disponivel": True,
                "preco": float(m2.group(1)),
                "preco_original": None,
                "url": url,
                "nome": None,
            }
        return None
    except Exception:
        return None


def _fetch_vtex_price(ean, base_url, fallbacks=None, host_override=None):
    """
    Tenta múltiplos endpoints VTEX para obter o preço de um EAN.
    host_override seta o header Host para resolver o binding VTEX IO correto.
    """
    all_bases = [base_url] + (fallbacks or [])
    for base in all_bases:
        attempts = [
            (url_tpl.format(ean=ean), parser)
            for url_tpl, parser in _build_vtex_attempts(base)
        ]
        got_valid_response = False
        for url, parser in attempts:
            raw = _vtex_get_json(url, host_override=host_override)
            if raw is None:
                continue
            got_valid_response = True
            result = parser(raw, base)
            if result is not None:
                return result
        if got_valid_response:
            break  # base respondeu com JSON válido — não tenta fallback
    return None


def _fetch_and_store_competitor_prices(eans):
    """Busca preços de todos os EANs em paralelo e salva no banco. Roda em thread separada."""
    import concurrent.futures
    _ensure_competitor_schema()

    def fetch_one(ean, slug, base, fallbacks=None, host_override=None, scrape_search=False):
        erro = None
        result = None
        try:
            if base:
                result = _fetch_vtex_price(ean, base, fallbacks=fallbacks, host_override=host_override)
            if result is None and scrape_search:
                result = _fetch_raia_via_search_page(ean)
        except Exception as e:
            erro = str(e)[:200]

        if result is None and erro is None:
            erro = "sem_resultado"  # todos endpoints falharam

        try:
            conn = _new_conn(statement_timeout_ms=10000)
            cur = conn.cursor()
            # Garante coluna erro (migracao lazy)
            try:
                cur.execute("ALTER TABLE ecommerce_competitor_prices ADD COLUMN IF NOT EXISTS erro TEXT")
                conn.commit()
            except Exception:
                conn.rollback()

            if result is not None:
                cur.execute("""
                    INSERT INTO ecommerce_competitor_prices
                        (ean, concorrente, nome, preco, preco_original, disponivel, url, consultado_em, erro)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NULL)
                    ON CONFLICT (ean, concorrente) DO UPDATE SET
                        nome=EXCLUDED.nome, preco=EXCLUDED.preco,
                        preco_original=EXCLUDED.preco_original,
                        disponivel=EXCLUDED.disponivel, url=EXCLUDED.url,
                        consultado_em=NOW(), erro=NULL
                """, (
                    ean, slug,
                    result.get("nome"),
                    result.get("preco"),
                    result.get("preco_original"),
                    result.get("disponivel", False),
                    result.get("url"),
                ))
            else:
                # Salva o erro para debug sem sobrescrever preço válido existente
                cur.execute("""
                    INSERT INTO ecommerce_competitor_prices
                        (ean, concorrente, disponivel, consultado_em, erro)
                    VALUES (%s, %s, FALSE, NOW(), %s)
                    ON CONFLICT (ean, concorrente) DO UPDATE SET
                        consultado_em=NOW(), erro=EXCLUDED.erro
                    WHERE ecommerce_competitor_prices.preco IS NULL
                """, (ean, slug, erro))
            conn.commit()
            cur.close()
            conn.close()
            return {"ean": ean, "concorrente": slug, "ok": result is not None, "erro": erro}
        except Exception:
            return {"ean": ean, "concorrente": slug, "ok": False, "erro": "erro_banco"}

    tasks = [
        (ean, slug, info["base"], info.get("base_fallbacks", []),
         info.get("vtex_host"), info.get("scrape_search", False))
        for ean in eans
        for slug, info in _CONCORRENTES.items()
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(fetch_one, ean, slug, base, fbs, host, scrape) for ean, slug, base, fbs, host, scrape in tasks]
        concurrent.futures.wait(futures, timeout=300)
    resultados = []
    for fut in futures:
        try:
            resultados.append(fut.result())
        except Exception:
            pass
    return {
        "total": len(resultados),
        "ok": sum(1 for r in resultados if r and r.get("ok")),
        "falha": sum(1 for r in resultados if r and not r.get("ok")),
        "por_concorrente": {
            slug: {
                "ok": sum(1 for r in resultados if r and r.get("concorrente") == slug and r.get("ok")),
                "falha": sum(1 for r in resultados if r and r.get("concorrente") == slug and not r.get("ok")),
            }
            for slug in _CONCORRENTES
        },
    }


def _ensure_ml_schema():
    _load_db_migrations()
    if "ml" in _schema_ready:
        return
    with _schema_lock:
        if "ml" in _schema_ready:
            return
        conn = db(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ml_tokens (
                id INT PRIMARY KEY DEFAULT 1,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                ml_user_id TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ml_items (
                ml_item_id TEXT PRIMARY KEY,
                ean TEXT NOT NULL,
                titulo TEXT,
                preco NUMERIC(10,2),
                category_id TEXT,
                status TEXT DEFAULT 'active',
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE ml_items ADD COLUMN IF NOT EXISTS category_id TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS origem TEXT DEFAULT 'ecommerce'")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS ml_order_id TEXT")
        conn.commit(); cur.close()
        _schema_ready.add("ml")
        _mark_migration_done("ml")


def _norm_email(email):
    return (email or "").strip().lower()


def _digits(s):
    return re.sub(r"\D+", "", s or "")


_CONTROLADO_TARJA_PRETA_RE = re.compile(
    r"\b(?:B1|B2|A1|A2|A3)\b"
    r"|nitrazepam|clonazepam|alprazolam|diazepam|lorazepam|bromazepam"
    r"|zolpidem|zopiclona|midazolam|fenobarbital|fenitoina|carbamazepina"
    r"|metilfenidato|lisdexanfetamina|morfina|metadona|tramadol|oxicodona",
    re.IGNORECASE,
)

_GENERIC_RE = re.compile(r"\bgen[eé]rico\b|\bgenerico\b", re.IGNORECASE)


def _is_generic_product(nome="", classe="", med=None):
    med = med or {}
    blob = " ".join([
        nome or "",
        classe or "",
        med.get("classe") or "",
        med.get("descricao") or "",
    ])
    return bool(_GENERIC_RE.search(blob))


def _is_black_stripe_product(nome="", anvisa=None, med=None):
    med = med or {}
    anvisa = anvisa or {}
    tarja = (anvisa.get("tarja") or med.get("tarja") or "").strip().lower()
    if tarja == "preta":
        return True
    if tarja == "vermelha":
        return False
    blob = " ".join([
        nome or "",
        med.get("descricao") or "",
        med.get("classe") or "",
        anvisa.get("alertas") or "",
        anvisa.get("como_usar") or "",
        anvisa.get("nome_anvisa") or "",
        anvisa.get("principio_ativo") or "",
    ])
    return bool(_CONTROLADO_TARJA_PRETA_RE.search(blob))


def _generic_placeholder_for(nome="", anvisa=None, med=None):
    med = med or {}
    if not _is_generic_product(nome, med.get("classe"), med):
        return None
    return GENERIC_TARJA_PRETA_IMG if _is_black_stripe_product(nome, anvisa, med) else GENERIC_TARJA_VERMELHA_IMG


def _valid_nome(nome):
    nome = (nome or "").strip()
    parts = [p for p in nome.split() if len(p) >= 2]
    return len(nome) >= 6 and len(parts) >= 2 and bool(re.fullmatch(r"[A-Za-zÀ-ÿ' ]+", nome))


def _valid_email(email):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""))


def _valid_phone(phone):
    d = _digits(phone)
    return len(d) in (10, 11) and len(set(d)) > 2


def _valid_endereco_completo(endereco, lat=None, lng=None):
    text = (endereco or "").strip()
    norm = text.lower()
    cep_ok = bool(re.search(r"\b\d{5}-?\d{3}\b", text))
    uf_ok = bool(re.search(r"(?:^|[\s,\-\/])([A-Za-z]{2})(?:$|[\s,\-\/])", text))
    numero_ok = bool(re.search(r"(?:^|[\s,])(?:n[ºo.]?\s*)?\d+[A-Za-z]?(?:$|[\s,\-])", text))
    rua_ok = bool(re.search(r"\b(rua|r\.|avenida|av\.|alameda|travessa|estrada|rodovia|praça|praca)\b", norm))
    partes = [p.strip() for p in re.split(r"[,;-]", text) if p.strip()]
    coord_ok = lat is not None and lng is not None
    return len(text) >= 18 and cep_ok and uf_ok and numero_ok and rua_ok and len(partes) >= 3 and coord_ok


def _to_float_or_none(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _novo_codigo_entrega():
    return str(random.randint(1000, 9999))


def _consumer_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("consumidor_id"):
            flash("Faça login para continuar.", "info")
            return redirect(url_for("consumidor_login", next=request.full_path))
        return fn(*args, **kwargs)
    return wrapper


def haversine(lat1, lng1, lat2, lng2):
    R = 6371.0
    dLat = math.radians(lat2 - lat1)
    dLng = math.radians(lng2 - lng1)
    a = (
        math.sin(dLat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dLng / 2) ** 2
    )
    return round(R * 2 * math.asin(math.sqrt(a)) * 1.3, 1)


# ─── GEOCODING (Nominatim / OpenStreetMap — gratuito) ─────────────────────────

_nom_lock = threading.Lock()
_nom_last = 0.0


def nominatim_geocode(endereco, uf):
    global _nom_last
    query = f"{endereco}, {uf}, Brasil"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": "1", "countrycodes": "br"}
    )
    with _nom_lock:
        wait = 1.1 - (time.time() - _nom_last)
        if wait > 0:
            time.sleep(wait)
        _nom_last = time.time()
        req = urllib.request.Request(
            url, headers={"User-Agent": "poupaqui-ecommerce/1.0 contato@poupaqui.com.br"}
        )
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read())
                if data:
                    return float(data[0]["lat"]), float(data[0]["lon"])
        except Exception:
            pass
    return None, None


UF_NOMES = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia",
    "CE": "Ceará", "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás",
    "MA": "Maranhão", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul",
    "MG": "Minas Gerais", "PA": "Pará", "PB": "Paraíba", "PR": "Paraná",
    "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro",
    "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rondônia",
    "RR": "Roraima", "SC": "Santa Catarina", "SP": "São Paulo", "SE": "Sergipe",
    "TO": "Tocantins",
}


def _parse_cidade_uf(raw):
    raw = (raw or "").strip()
    m = re.match(r"^(.*?)(?:\s*[-,\/]\s*|\s+)([A-Za-z]{2})$", raw)
    if not m:
        return raw, ""
    return m.group(1).strip(), m.group(2).upper()


def _extract_cep(raw):
    m = re.search(r"\b(\d{5})-?(\d{3})\b", raw or "")
    return f"{m.group(1)}{m.group(2)}" if m else ""


def _viacep_query(cep):
    if not cep:
        return []
    try:
        url = f"https://viacep.com.br/ws/{cep}/json/"
        req = urllib.request.Request(url, headers={"User-Agent": "poupaqui-ecommerce/1.0"})
        with urllib.request.urlopen(req, timeout=5, context=ssl._create_unverified_context()) as r:
            data = json.loads(r.read())
        if data.get("erro"):
            return []
        cidade = data.get("localidade") or ""
        uf = data.get("uf") or ""
        estado = UF_NOMES.get(uf, uf)
        bairro = data.get("bairro") or ""
        rua = data.get("logradouro") or ""
        queries = []
        if rua:
            queries.append(", ".join(x for x in [rua, bairro, cidade, estado, "Brasil"] if x))
        if bairro:
            queries.append(", ".join(x for x in [bairro, cidade, estado, "Brasil"] if x))
        queries.append(", ".join(x for x in [cidade, estado, "Brasil"] if x))
        return queries
    except Exception:
        return []


def _location_queries(termo):
    termo = (termo or "").strip()
    cidade, uf = _parse_cidade_uf(re.sub(r",?\s*\d{5}-?\d{3}\b", "", termo).strip(" ,-"))
    estado = UF_NOMES.get(uf, uf)
    parts = [p.strip() for p in re.split(r"\s*,\s*", termo) if p.strip()]
    no_cep = re.sub(r",?\s*\d{5}-?\d{3}\b", "", termo).strip(" ,-")
    queries = []
    queries.extend(_viacep_query(_extract_cep(termo)))
    queries.append(f"{termo}, Brasil")
    if no_cep and no_cep != termo:
        queries.append(f"{no_cep}, Brasil")
    if uf and cidade:
        queries.append(f"{cidade}, {estado}, Brasil")
        queries.append(f"{cidade}, {uf}, Brasil")
    if len(parts) >= 2:
        # Bairro/área + cidade/UF, útil para endereços sem rua e número.
        bairro = parts[0]
        cidade_uf = parts[1]
        queries.append(f"{bairro}, {cidade_uf}, Brasil")
    seen = set()
    cleaned = []
    for q in queries:
        key = re.sub(r"\s+", " ", q).strip().lower()
        if key and key not in seen:
            seen.add(key)
            cleaned.append(q)
    return cleaned


def _standardize_user_address(raw):
    text = re.sub(r"\s+", " ", (raw or "").strip())
    text = re.sub(r"\bR\.\s*", "Rua ", text, flags=re.I)
    text = re.sub(r"\bAv\.\s*", "Avenida ", text, flags=re.I)
    text = re.sub(r"\s+-\s+", ", ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    return text.strip(" ,")


def _result_matches_term(item, termo):
    a = item.get("address") or {}
    hay = " ".join(str(x or "") for x in [
        item.get("display_name"), a.get("road"), a.get("suburb"), a.get("neighbourhood"),
        a.get("city_district"), a.get("city"), a.get("town"), a.get("village"),
        a.get("postcode"), a.get("state_code"), a.get("state")
    ])
    hay_norm = re.sub(r"[^a-z0-9]+", " ", hay.lower())
    wanted = re.sub(r"[^a-z0-9]+", " ", (termo or "").lower())
    tokens = [t for t in wanted.split() if len(t) > 2 and not t.isdigit()]
    return sum(1 for t in tokens if t in hay_norm) >= max(1, min(3, len(tokens)))


def _geo_override(endereco, endereco2=None, uf=None):
    text = f"{endereco or ''} {endereco2 or ''}".lower()
    uf = (uf or "").upper()
    if uf == "SP" and "são pedro" in text:
        return -22.5483, -47.9139
    return None, None


def _public_store_name(loja):
    endereco = (loja.get("endereco") if hasattr(loja, "get") else "") or ""
    razao = (loja.get("razao") if hasattr(loja, "get") else "") or ""
    base = re.sub(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b", "", endereco)
    base = re.sub(r"\b\d{14}\b", "", base)
    base = re.split(r"\s+-\s+|\s+-\s*$|-\s*$", base, maxsplit=1)[0]
    base = re.sub(r"\s+", " ", base).strip(" -,.")
    if base:
        if re.search(r"\b(poupaqui|poup\s*aqui)\b", base, flags=re.I):
            return base
        return f"Drogaria Poupaqui {base}"
    return re.sub(r"\s+", " ", razao).strip() or "Drogaria Poupaqui"


def _known_city_location_result(termo, cidade, uf):
    if _extract_cep(termo):
        return None
    cidade = (cidade or "").strip()
    uf = (uf or "").upper().strip()
    if not cidade or not uf:
        return None
    lat, lng = _geo_override(cidade, "", uf)
    if not lat:
        return None
    estado = UF_NOMES.get(uf, uf)
    slug = re.sub(r"[^a-z0-9]+", "-", cidade.lower()).strip("-") or "cidade"
    return {
        "place_id": f"known-{uf.lower()}-{slug}",
        "lat": str(lat),
        "lon": str(lng),
        "display_name": f"{cidade} - {uf} - Brasil",
        "name": cidade,
        "class": "place",
        "type": "town",
        "address": {
            "city": cidade,
            "town": cidade,
            "state": estado,
            "state_code": uf,
            "country": "Brasil",
            "country_code": "br",
        },
    }


@app.get("/api/localizacao")
def api_localizacao():
    global _nom_last
    termo = (request.args.get("q") or "").strip()
    if len(termo) < 2:
        return jsonify({"results": []})

    cidade_busca, uf_busca = _parse_cidade_uf(re.sub(r",?\s*\d{5}-?\d{3}\b", "", termo).strip(" ,-"))
    known_result = _known_city_location_result(termo, cidade_busca, uf_busca)
    queries = _location_queries(termo)

    seen = set()
    results = []
    if known_result:
        seen.add(known_result["place_id"])
        results.append(known_result)
    for query in queries:
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {
                "q": query,
                "format": "json",
                "limit": "10",
                "addressdetails": "1",
                "countrycodes": "br",
            }
        )
        try:
            with _nom_lock:
                wait = 1.1 - (time.time() - _nom_last)
                if wait > 0:
                    time.sleep(wait)
                _nom_last = time.time()
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "poupaqui-ecommerce/1.0 contato@poupaqui.com.br"},
                )
                with urllib.request.urlopen(req, timeout=8, context=ssl._create_unverified_context()) as r:
                    data = json.loads(r.read())
        except Exception:
            continue

        for item in data:
            key = item.get("place_id") or f"{item.get('lat')},{item.get('lon')}"
            if key in seen:
                continue
            seen.add(key)
            results.append(item)

    cep_raw = _extract_cep(termo)

    if results and cep_raw:
        best = next((r for r in results if _result_matches_term(r, termo)), results[0])
        synthetic = dict(best)
        synthetic["place_id"] = f"user-{cep_raw}"
        synthetic["display_name"] = _standardize_user_address(termo)
        synthetic["name"] = _standardize_user_address(termo)
        addr = dict(synthetic.get("address") or {})
        addr.setdefault("postcode", re.sub(r"(\d{5})(\d{3})", r"\1-\2", cep_raw))
        synthetic["address"] = addr
        results = [synthetic] + [r for r in results if (r.get("place_id") or "") != synthetic["place_id"]]

    # Fallback: Nominatim retornou vazio mas há CEP → usa ViaCEP + geo_override ou Nominatim por cidade
    if not results and cep_raw:
        try:
            url_vc = f"https://viacep.com.br/ws/{cep_raw}/json/"
            req_vc = urllib.request.Request(url_vc, headers={"User-Agent": "poupaqui-ecommerce/1.0"})
            with urllib.request.urlopen(req_vc, timeout=5, context=ssl._create_unverified_context()) as rv:
                vc = json.loads(rv.read())
            if not vc.get("erro"):
                cidade_vc = vc.get("localidade") or ""
                uf_vc = vc.get("uf") or ""
                estado_vc = UF_NOMES.get(uf_vc, uf_vc)
                lat_fb = lng_fb = None

                # 1º: geo_override (cidades mapeadas)
                if cidade_vc and uf_vc:
                    lat_fb, lng_fb = _geo_override(termo, cidade_vc, uf_vc)

                # 2º: Nominatim só com cidade
                if not lat_fb and cidade_vc:
                    q_cid = f"{cidade_vc}, {estado_vc}, Brasil"
                    url_cid = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
                        {"q": q_cid, "format": "json", "limit": "1", "addressdetails": "1", "countrycodes": "br"}
                    )
                    with _nom_lock:
                        wait = 1.1 - (time.time() - _nom_last)
                        if wait > 0:
                            time.sleep(wait)
                        _nom_last = time.time()
                        req_cid = urllib.request.Request(
                            url_cid, headers={"User-Agent": "poupaqui-ecommerce/1.0 contato@poupaqui.com.br"}
                        )
                        with urllib.request.urlopen(req_cid, timeout=8, context=ssl._create_unverified_context()) as rc:
                            data_cid = json.loads(rc.read())
                    if data_cid:
                        lat_fb = float(data_cid[0]["lat"])
                        lng_fb = float(data_cid[0]["lon"])

                if lat_fb:
                    results = [{
                        "place_id": f"user-{cep_raw}",
                        "lat": str(lat_fb),
                        "lon": str(lng_fb),
                        "display_name": _standardize_user_address(termo),
                        "name": _standardize_user_address(termo),
                        "address": {
                            "city": cidade_vc,
                            "state_code": uf_vc,
                            "postcode": re.sub(r"(\d{5})(\d{3})", r"\1-\2", cep_raw),
                            "country_code": "br",
                        },
                    }]
        except Exception:
            pass

    return jsonify({"results": results[:10]})


def get_or_geocode(cnpjloja, endereco, uf):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT lat, lng FROM ecommerce_lojas_geo WHERE cnpjloja = %s", (cnpjloja,)
    )
    row = cur.fetchone()
    cur.close()
    if row and row["lat"]:
        return float(row["lat"]), float(row["lng"])
    lat, lng = _geo_override(endereco, None, uf)
    if lat:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ecommerce_lojas_geo (cnpjloja, lat, lng)
            VALUES (%s, %s, %s)
            ON CONFLICT (cnpjloja) DO UPDATE
              SET lat = EXCLUDED.lat, lng = EXCLUDED.lng, geocoded_at = NOW()
            """,
            (cnpjloja, lat, lng),
        )
        conn.commit()
        cur.close()
        return lat, lng
    lat, lng = nominatim_geocode(endereco, uf)
    if lat:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ecommerce_lojas_geo (cnpjloja, lat, lng)
            VALUES (%s, %s, %s)
            ON CONFLICT (cnpjloja) DO UPDATE
              SET lat = EXCLUDED.lat, lng = EXCLUDED.lng, geocoded_at = NOW()
            """,
            (cnpjloja, lat, lng),
        )
        conn.commit()
        cur.close()
    return lat, lng


# ─── SUPABASE STORAGE ────────────────────────────────────────────────────────

def upload_to_supabase_storage(file_bytes, path, content_type, bucket="produto-imagens"):
    """Upload bytes to Supabase Storage. Returns public URL or None."""
    auth_key = SUPABASE_SERVICE_KEY or SUPABASE_ANON
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{path}"
    req = urllib.request.Request(
        upload_url, data=file_bytes,
        headers={
            "Authorization": f"Bearer {auth_key}",
            "Content-Type": content_type,
            "x-upsert": "true",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        return f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        app.logger.error("Supabase Storage upload error %s — %s — path=%s", e.code, body, path)
        return None
    except Exception as e:
        app.logger.error("Supabase Storage upload exception: %s — path=%s", e, path)
        return None


# ─── QUERIES DE PRODUTOS DNS ───────────────────────────────────────────────────

_image_fill_lock = threading.Lock()
_image_fill_inflight = set()

_batch_cache: dict = {}
_batch_cache_lock = threading.Lock()
_BATCH_CACHE_TTL = 300  # 5 minutos


def _batch_cache_get(key: tuple):
    with _batch_cache_lock:
        e = _batch_cache.get(key)
        if e and time.time() - e["ts"] < _BATCH_CACHE_TTL:
            return e["data"]
    return None


def _batch_cache_set(key: tuple, data: list):
    with _batch_cache_lock:
        if len(_batch_cache) >= 30:
            oldest = min(_batch_cache, key=lambda k: _batch_cache[k]["ts"])
            del _batch_cache[oldest]
        _batch_cache[key] = {"data": data, "ts": time.time()}


def _batch_cache_clear():
    with _batch_cache_lock:
        _batch_cache.clear()


def _first_valid_url(*values):
    for value in values:
        value = (value or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return None


def _image_ext_from_content_type(content_type, url):
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct == "image/png":
        return "png", ct
    if ct == "image/webp":
        return "webp", ct
    if ct in ("image/jpeg", "image/jpg"):
        return "jpg", "image/jpeg"
    path = urllib.parse.urlparse(url or "").path.lower()
    if path.endswith(".png"):
        return "png", "image/png"
    if path.endswith(".webp"):
        return "webp", "image/webp"
    return "jpg", "image/jpeg"


def _fetch_exact_barcode_image_url(ean):
    ean_digits = _digits(ean)
    if len(ean_digits) < 8:
        return None
    sources = (
        f"https://world.openfoodfacts.org/api/v2/product/{ean_digits}.json"
        "?fields=code,status,product_name,brands,image_front_url,image_url,selected_images",
        f"https://world.openbeautyfacts.org/api/v2/product/{ean_digits}.json"
        "?fields=code,status,product_name,brands,image_front_url,image_url,selected_images",
    )
    headers = {"User-Agent": "PoupaquiEcommerce/1.0 (catalog-image-fill)"}
    for url in sources:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                payload = json.loads(r.read().decode("utf-8", "ignore"))
            if int(payload.get("status") or 0) != 1:
                continue
            if _digits(payload.get("code")) != ean_digits:
                continue
            product = payload.get("product") or {}
            selected = product.get("selected_images") or {}
            front = ((selected.get("front") or {}).get("display") or {})
            img_url = _first_valid_url(
                front.get("pt"),
                front.get("br"),
                front.get("en"),
                product.get("image_front_url"),
                product.get("image_url"),
            )
            if img_url:
                return img_url
        except Exception:
            continue
    return None


def _serper_api_key():
    return (
        os.getenv("SERPER_API_KEY", "").strip()
        or os.getenv("EAN_IMAGE_SERPER_API_KEY", "").strip()
    )


def _post_json(url, payload, headers=None, timeout=12):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "PoupaquiEcommerce/1.0 (catalog-image-fill)",
            **(headers or {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _norm_text(value):
    value = html.unescape(value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _catalog_product_key(nome):
    text = _norm_text(nome)
    text = re.sub(r"\b(capsulas|capsula|caps|cps|comprimidos|comprimido|comp|cp)\b", "cp", text)
    text = re.sub(r"\b(fr|frasco)\b", "fr", text)
    text = re.sub(r"\b(c|com)\s*(\d+)\b", r"c \1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or ""


def _dedupe_products_for_display(produtos):
    best = {}
    for produto in produtos:
        key = _catalog_product_key(produto.get("nome") or "") or (produto.get("ean") or "").strip()
        if not key:
            continue
        current = best.get(key)
        if current is None:
            best[key] = produto
            continue
        cur_img = bool((current.get("imagem") or "").strip())
        new_img = bool((produto.get("imagem") or "").strip())
        cur_dist = current.get("distancia_km")
        new_dist = produto.get("distancia_km")
        replace = False
        if new_img and not cur_img:
            replace = True
        elif new_img == cur_img:
            if new_dist is not None and (cur_dist is None or new_dist < cur_dist):
                replace = True
            elif new_dist == cur_dist:
                try:
                    replace = float(produto.get("preco") or 0) < float(current.get("preco") or 0)
                except Exception:
                    replace = False
        if replace:
            best[key] = produto
    return list(best.values())


def _name_tokens(nome):
    stop = {"com", "para", "por", "das", "dos", "fps", "prot", "solar", "facial", "locao"}
    return [
        token for token in _norm_text(nome).split()
        if len(token) >= 4 and token not in stop and not token.isdigit()
    ][:8]


def _page_matches_product(html_text, nome):
    blob = _norm_text(html_text[:200000])
    tokens = _name_tokens(nome)
    if not tokens:
        return True
    hits = sum(1 for token in tokens if token in blob)
    return hits >= min(2, len(tokens))


def _abs_url(url, base_url):
    url = html.unescape((url or "").strip())
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    return urllib.parse.urljoin(base_url, url)


def _extract_structured_image_url(html_text, base_url):
    candidates = []
    for pattern in (
        r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::secure_url)?["\']',
        r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image(?::src)?["\']',
        r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)["\']',
    ):
        candidates.extend(re.findall(pattern, html_text, flags=re.I))

    for script in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text,
        flags=re.I | re.S,
    )[:5]:
        try:
            data = json.loads(html.unescape(script).strip())
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                image_value = item.get("image")
                if isinstance(image_value, str):
                    candidates.append(image_value)
                elif isinstance(image_value, list):
                    candidates.extend([x for x in image_value if isinstance(x, str)])
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)

    for candidate in candidates:
        img_url = _abs_url(candidate, base_url)
        if img_url and img_url.startswith(("http://", "https://")):
            lower = urllib.parse.urlparse(img_url).path.lower()
            if not any(x in lower for x in ("logo", "sprite", "placeholder", "favicon", "banner", "hero")):
                return img_url
    return None


def _fetch_verified_serper_image_url(ean, nome):
    api_key = _serper_api_key()
    ean_digits = _digits(ean)
    if not api_key or len(ean_digits) < 8:
        return None
    try:
        data = _post_json(
            "https://google.serper.dev/search",
            {"q": f'"{ean_digits}"', "num": 8, "gl": "br", "hl": "pt-br"},
            headers={"X-API-KEY": api_key},
            timeout=12,
        )
    except Exception:
        return None

    for item in (data.get("organic") or [])[:8]:
        page_url = item.get("link")
        if not page_url or not page_url.startswith(("http://", "https://")):
            continue
        try:
            req = urllib.request.Request(
                page_url,
                headers={"User-Agent": "Mozilla/5.0 (Poupaqui image verifier)"},
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "text/html" not in ctype:
                    continue
                html_text = r.read(600000).decode("utf-8", "ignore")
        except Exception:
            continue
        if ean_digits not in _digits(html_text):
            continue
        img_url = _extract_structured_image_url(html_text, page_url)
        if (
            img_url
            and not _looks_like_other_pharmacy_brand(img_url, page_url)
            and not _image_has_other_pharmacy_text(img_url)
            and not _image_looks_non_product(img_url)
        ):
            return img_url
    return None


_OTHER_PHARMACY_BRANDS_RE = re.compile(
    r"drogaria\s+s[aã]o\s+jo[aã]o|saojoao|s[aã]o\s+jo[aã]o"
    r"|droga\s*raia|drogasil|pague\s*menos|panvel|nissei|venancio"
    r"|ultrafarma|drogaria\s+araujo|drogaria\s+minas|farm[aá]cia\s+brito"
    r"|drogaria\s+santa|drogariasantaterezinha|farmacias?\s+heroos|farmaciasheroos|farmais|nova\s*farmais"
    r"|meu\s+mundo\s+fit|formosa|farmasesi|drogaria\s+canabrava",
    re.IGNORECASE,
)

_OCR_TEXT_CACHE = {}

_NON_PRODUCT_IMAGE_RE = re.compile(
    r"sua\s+sa[uú]de|f[aá]cil\s+e\s+acess[ií]vel|tempo\s+e\s+dinheiro"
    r"|delivery|entrega|frete|promo[cç][aã]o|oferta|desconto"
    r"|banner|hero|rem[eé]dios|drogarias?\s+online",
    re.IGNORECASE,
)


def _looks_like_other_pharmacy_brand(*values):
    blob = " ".join(v or "" for v in values)
    return bool(_OTHER_PHARMACY_BRANDS_RE.search(blob))


def _ocr_space_api_key():
    return os.getenv("OCR_SPACE_API_KEY", "helloworld").strip()


def _ocr_image_text(image_url):
    image_url = (image_url or "").strip()
    if not image_url:
        return ""
    cached = _OCR_TEXT_CACHE.get(image_url)
    if cached is not None:
        return cached
    api_key = _ocr_space_api_key()
    if not api_key:
        _OCR_TEXT_CACHE[image_url] = ""
        return ""
    try:
        payload = urllib.parse.urlencode({
            "apikey": api_key,
            "url": image_url,
            "language": "por",
            "scale": "true",
            "OCREngine": "2",
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.ocr.space/parse/image",
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "PoupaquiEcommerce/1.0 (image-ocr)",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        text = " ".join(
            (item.get("ParsedText") or "")
            for item in (data.get("ParsedResults") or [])
            if isinstance(item, dict)
        )
    except Exception:
        text = ""
    _OCR_TEXT_CACHE[image_url] = text
    return text


def _image_has_other_pharmacy_text(image_url):
    return _looks_like_other_pharmacy_brand(_ocr_image_text(image_url))


def _image_looks_non_product(image_url):
    text = _ocr_image_text(image_url)
    return bool(_NON_PRODUCT_IMAGE_RE.search(text or ""))


def _is_untrusted_scraped_image(image_url):
    image_url = image_url or ""
    return (
        "/pedidoeletronico/google_auto/" in image_url
        or "/google_auto/" in image_url
        or "/medicamentos-auto-ean/" in image_url
    )


def _fetch_serper_image_result_url(ean, nome):
    api_key = _serper_api_key()
    ean_digits = _digits(ean)
    if not api_key or len(ean_digits) < 8:
        return None
    try:
        data = _post_json(
            "https://google.serper.dev/images",
            {"q": f'"{ean_digits}"', "num": 10, "gl": "br", "hl": "pt-br"},
            headers={"X-API-KEY": api_key},
            timeout=12,
        )
    except Exception:
        return None

    for item in (data.get("images") or [])[:10]:
        image_url = _first_valid_url(item.get("imageUrl"), item.get("thumbnailUrl"))
        page_url = item.get("link") or ""
        title = item.get("title") or ""
        if not image_url:
            continue
        if _looks_like_other_pharmacy_brand(image_url, page_url, title):
            continue
        if _image_has_other_pharmacy_text(image_url):
            continue
        if _image_looks_non_product(image_url):
            continue
        proof_blob = " ".join([image_url, page_url, title])
        if ean_digits in _digits(proof_blob):
            return image_url
        if page_url.startswith(("http://", "https://")):
            try:
                req = urllib.request.Request(
                    page_url,
                    headers={"User-Agent": "Mozilla/5.0 (Poupaqui image verifier)"},
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if "text/html" not in ctype:
                        continue
                    html_text = r.read(300000).decode("utf-8", "ignore")
                if ean_digits in _digits(html_text):
                    return image_url
            except Exception:
                continue
    return None


def _download_image_for_storage(image_url):
    try:
        req = urllib.request.Request(
            image_url,
            headers={"User-Agent": "PoupaquiEcommerce/1.0 (catalog-image-fill)"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            content_type = r.headers.get("Content-Type", "")
            if not content_type.lower().startswith("image/"):
                return None, None, None
            raw = r.read(4 * 1024 * 1024)
        ext, content_type = _image_ext_from_content_type(content_type, image_url)
        return raw, ext, content_type
    except Exception:
        return None, None, None


def _upsert_catalog_image(cur, cnpjloja, ean, image_url):
    cur.execute(
        """
        INSERT INTO ecommerce_produto_imagens (cnpjloja, ean, imagem_url)
        VALUES (%s, %s, %s)
        ON CONFLICT (cnpjloja, ean) DO UPDATE
          SET imagem_url=EXCLUDED.imagem_url, updated_at=NOW()
        """,
        (cnpjloja, ean, image_url),
    )


def _upsert_catalog_image_all_stores(cur, ean, image_url):
    ean_digits = _digits(ean)
    cur.execute(
        """
        INSERT INTO ecommerce_produto_imagens (cnpjloja, ean, imagem_url)
        SELECT DISTINCT cnpjloja, ean, %s
        FROM (
            SELECT e.cnpj AS cnpjloja, e.barras AS ean
            FROM estoque e
            WHERE e.estoque > 0
              AND LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0') = LTRIM(%s, '0')

            UNION ALL

            SELECT ae.cnpj_loja AS cnpjloja, ae.ean
            FROM automatiza_estoque ae
            WHERE ae.quantidade_estoque > 0
              AND LTRIM(COALESCE(ae.ean, ''), '0') = LTRIM(%s, '0')
        ) x
        WHERE cnpjloja IS NOT NULL AND ean IS NOT NULL AND TRIM(ean) <> ''
        ON CONFLICT (cnpjloja, ean) DO UPDATE
          SET imagem_url=EXCLUDED.imagem_url, updated_at=NOW()
        """,
        (image_url, ean_digits, ean_digits),
    )


def _fill_one_catalog_image(cnpjloja, ean, nome=None):
    ean_digits = _digits(ean)
    ean_key = (ean or "").strip() or ean_digits
    if not cnpjloja or len(ean_digits) < 8:
        return None
    key = (cnpjloja, ean_digits)
    with _image_fill_lock:
        if key in _image_fill_inflight:
            return None
        _image_fill_inflight.add(key)
    try:
        _ensure_precificador_schema()
        conn = db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.descricao, m.classe,
                   COALESCE(mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
            FROM medicamentos m
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            WHERE LTRIM(COALESCE(m.barra_norm,''), '0') = LTRIM(%s, '0')
               OR LTRIM(COALESCE(m.barra,''), '0')      = LTRIM(%s, '0')
            ORDER BY (mi.cloudinary_url IS NOT NULL) DESC, mi.created_at DESC NULLS LAST
            LIMIT 1
            """,
            (ean_digits, ean_digits),
        )
        row = cur.fetchone()
        placeholder = _generic_placeholder_for(nome or "", med=dict(row) if row else {})
        image_url = _first_valid_url(row["imagem"] if row else None)
        if image_url and _looks_like_other_pharmacy_brand(image_url):
            image_url = None
        if image_url and placeholder and _image_has_other_pharmacy_text(image_url):
            image_url = None
        if image_url and placeholder and _is_untrusted_scraped_image(image_url):
            image_url = None
        if not image_url and placeholder:
            _upsert_catalog_image(cur, cnpjloja, ean_key, placeholder)
            _upsert_catalog_image_all_stores(cur, ean_digits, placeholder)
            conn.commit()
            cur.close()
            return placeholder
        cur.execute(
            """
            SELECT imagem_url
            FROM ecommerce_produto_imagens
            WHERE LTRIM(COALESCE(ean,''), '0') = LTRIM(%s, '0')
              AND imagem_url IS NOT NULL AND TRIM(imagem_url) <> ''
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 1
            """,
            (ean_digits,),
        )
        cached = cur.fetchone()
        image_url = image_url or _first_valid_url(cached["imagem_url"] if cached else None)
        if not image_url:
            source_url = _fetch_exact_barcode_image_url(ean_digits)
            if not source_url and nome:
                source_url = _fetch_verified_serper_image_url(ean_digits, nome)
            if not source_url and nome:
                source_url = _fetch_serper_image_result_url(ean_digits, nome)
            if source_url:
                raw, ext, content_type = _download_image_for_storage(source_url)
                if raw:
                    image_url = upload_to_supabase_storage(
                        raw,
                        f"auto-ean/{ean_digits}.{ext}",
                        content_type,
                    )
        if image_url:
            _upsert_catalog_image(cur, cnpjloja, ean_key, image_url)
            _upsert_catalog_image_all_stores(cur, ean_digits, image_url)
            conn.commit()
            cur.close()
            return image_url
        cur.close()
        return None
    except Exception:
        try:
            db().rollback()
        except Exception:
            pass
        return None
    finally:
        with _image_fill_lock:
            _image_fill_inflight.discard(key)


def _fill_missing_catalog_images(produtos, cnpjloja=None, max_sync=3):
    filled = 0
    for produto in produtos:
        if produto.get("imagem"):
            continue
        loja = cnpjloja or produto.get("cnpjloja")
        ean = produto.get("ean")
        if not loja or not ean:
            continue
        image_url = _fill_one_catalog_image(loja, ean, produto.get("nome"))
        if image_url:
            produto["imagem"] = image_url
        filled += 1
        if filled >= max_sync:
            break


def _schedule_fill_images(produtos, cnpjloja=None, limit=10):
    """Dispara o preenchimento de imagens em background, sem bloquear a requisição."""
    snapshot = [
        {"ean": p.get("ean"), "nome": p.get("nome"), "cnpjloja": p.get("cnpjloja")}
        for p in produtos
        if not (p.get("imagem") or "").strip()
    ][:limit]
    if not snapshot:
        return
    def _worker():
        _fill_missing_catalog_images(snapshot, cnpjloja=cnpjloja, max_sync=len(snapshot))
    threading.Thread(target=_worker, daemon=True).start()


def _has_catalog_image(produto):
    return bool((produto.get("imagem") or "").strip())


def _split_catalog_image_status(produtos):
    publicados, bloqueados = [], []
    seen_blocked = set()
    for produto in produtos:
        if _has_catalog_image(produto):
            publicados.append(produto)
            continue
        ean = (produto.get("ean") or "").strip()
        if ean and ean in seen_blocked:
            continue
        if ean:
            seen_blocked.add(ean)
        bloqueados.append(produto)
    return publicados, bloqueados


def _apply_safe_catalog_images(produtos, cur=None):
    if not produtos:
        return produtos
    eans = sorted({_digits(p.get("ean")) for p in produtos if _digits(p.get("ean"))})
    med_by_ean = {}
    if eans:
        _own_cur = cur is None
        if _own_cur:
            cur = db().cursor()
        try:
            cur.execute(
                """
                SELECT DISTINCT ON (LTRIM(COALESCE(barra_norm, barra, ''), '0'))
                       LTRIM(COALESCE(barra_norm, barra, ''), '0') AS ean_key,
                       descricao, classe, laboratorio
                FROM medicamentos
                WHERE LTRIM(COALESCE(barra_norm, barra, ''), '0') = ANY(%s)
                ORDER BY LTRIM(COALESCE(barra_norm, barra, ''), '0'),
                         (classe ILIKE '%%gen%%') DESC,
                         id
                """,
                ([e.lstrip("0") for e in eans],),
            )
            med_by_ean = {r["ean_key"]: dict(r) for r in cur.fetchall()}
        except Exception:
            pass
        finally:
            if _own_cur:
                cur.close()

    for produto in produtos:
        ean_key = _digits(produto.get("ean")).lstrip("0")
        med = med_by_ean.get(ean_key, {})
        anvisa = {"tarja": produto.get("tarja") or ""}
        placeholder = _generic_placeholder_for(produto.get("nome") or "", anvisa=anvisa, med=med)
        imagem_atual = produto.get("imagem") or ""
        if _looks_like_other_pharmacy_brand(imagem_atual) or (placeholder and _is_untrusted_scraped_image(imagem_atual) and _image_has_other_pharmacy_text(imagem_atual)):
            produto["imagem"] = placeholder
            produto["imagem_padrao_poupaqui"] = bool(placeholder)
            produto["imagem_bloqueada_marca_farmacia"] = True
        elif not imagem_atual and placeholder:
            produto["imagem"] = placeholder
            produto["imagem_padrao_poupaqui"] = True
    return produtos


_MARCAS_PROPRIAS = """
    AND (
      dns.ean_norm IS NOT NULL
      OR mi.cloudinary_url IS NOT NULL
      OR NULLIF(TRIM(m.imagem), '') IS NOT NULL
      OR e.descricao ILIKE ANY(ARRAY[
           '%%anasol%%','%%vit natu%%','%%vitnatu%%',
           '%%pronabol%%','%%ricosol%%','%%unispray%%',
           '%%goodvit%%'
         ])
    )
"""

_MARCAS_PROPRIAS_AUTO = """
    AND (
      dns.ean_norm IS NOT NULL
      OR mi.cloudinary_url IS NOT NULL
      OR NULLIF(TRIM(m.imagem), '') IS NOT NULL
      OR ae.descricao_produto ILIKE ANY(ARRAY[
           '%%anasol%%','%%vit natu%%','%%vitnatu%%',
           '%%pronabol%%','%%ricosol%%','%%unispray%%',
           '%%goodvit%%'
         ])
    )
"""

_SQL_ALPHA = """
    WITH eligible AS (
        SELECT
            e.barras,
            e.cnpj                            AS cnpjloja,
            COALESCE(e.barras_norm, e.barras) AS ean_join,
            e.descricao,
            CAST(e.estoque AS INTEGER)        AS qty,
            e.preco_referencial,
            e.custo_medio
        FROM estoque e
        WHERE e.cnpj = %s AND e.estoque > 0 {busca}
          AND (
            COALESCE(e.barras_norm, e.barras) IN (SELECT ean_norm FROM omie_estoque_dns)
            OR COALESCE(e.barras_norm, e.barras) IN (
                SELECT barra_norm FROM medicamentos
                WHERE barra_norm IS NOT NULL
                  AND (
                    NULLIF(TRIM(imagem), '') IS NOT NULL
                    OR id IN (SELECT medicamento_id FROM medicamentos_imagens
                              WHERE cloudinary_url IS NOT NULL)
                  )
            )
            OR e.descricao ILIKE ANY(ARRAY[
                '%%anasol%%','%%vit natu%%','%%vitnatu%%',
                '%%pronabol%%','%%ricosol%%','%%unispray%%','%%goodvit%%'
            ])
          )
        ORDER BY e.descricao
        LIMIT 300
    )
    SELECT
        el.barras                                                            AS ean,
        el.descricao                                                         AS nome,
        el.qty,
        COALESCE(vg.preco_venda, el.preco_referencial)                       AS preco_ref,
        ep.preco_customizado                                                  AS preco_custom,
        COALESCE(ep.preco_customizado, vg.preco_venda, el.preco_referencial) AS preco,
        el.custo_medio                                                        AS custo,
        COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
    FROM eligible el
    LEFT JOIN LATERAL (
        SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
        FROM vendageral
        WHERE cnpj = el.cnpjloja AND ean = el.barras
          AND total_vendasgeral > 0 AND itens > 0
        ORDER BY id DESC
        LIMIT 1
    ) vg ON TRUE
    LEFT JOIN medicamentos m          ON m.barra_norm = el.ean_join
    LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
    LEFT JOIN ecommerce_precos ep     ON ep.cnpjloja = el.cnpjloja AND ep.ean = el.barras
    LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = el.cnpjloja AND epi.ean = el.barras
"""

_SQL_AUTO = """
    WITH eligible AS (
        SELECT
            ae.ean,
            ae.cnpj_loja                          AS cnpjloja,
            ae.descricao_produto,
            CAST(ae.quantidade_estoque AS INTEGER) AS qty,
            ae.valor_final_produto,
            ae.custo
        FROM automatiza_estoque ae
        WHERE ae.cnpj_loja = %s AND ae.quantidade_estoque > 0 {busca}
          AND (
            ae.ean IN (SELECT ean_norm FROM omie_estoque_dns)
            OR ae.ean IN (
                SELECT barra_norm FROM medicamentos
                WHERE barra_norm IS NOT NULL
                  AND (
                    NULLIF(TRIM(imagem), '') IS NOT NULL
                    OR id IN (SELECT medicamento_id FROM medicamentos_imagens
                              WHERE cloudinary_url IS NOT NULL)
                  )
            )
            OR ae.descricao_produto ILIKE ANY(ARRAY[
                '%%anasol%%','%%vit natu%%','%%vitnatu%%',
                '%%pronabol%%','%%ricosol%%','%%unispray%%','%%goodvit%%'
            ])
          )
        ORDER BY ae.descricao_produto
        LIMIT 300
    )
    SELECT
        el.ean,
        el.descricao_produto                                                      AS nome,
        el.qty,
        COALESCE(av.preco_venda, el.valor_final_produto)                          AS preco_ref,
        ep.preco_customizado                                                       AS preco_custom,
        COALESCE(ep.preco_customizado, av.preco_venda, el.valor_final_produto)    AS preco,
        el.custo,
        COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), ''))  AS imagem
    FROM eligible el
    LEFT JOIN LATERAL (
        SELECT ROUND(valor_final_vendido / NULLIF(quantidade_vendida, 0), 2) AS preco_venda
        FROM automatiza_vendas
        WHERE cnpj_loja = el.cnpjloja AND ean = el.ean
          AND valor_final_vendido > 0 AND quantidade_vendida > 0
        ORDER BY id DESC
        LIMIT 1
    ) av ON TRUE
    LEFT JOIN medicamentos m          ON m.barra_norm = el.ean
    LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
    LEFT JOIN ecommerce_precos ep     ON ep.cnpjloja = el.cnpjloja AND ep.ean = el.ean
    LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = el.cnpjloja AND epi.ean = el.ean
"""


def get_dns_products(cnpjloja, q=None, include_hidden=False):
    conn = db()
    cur = conn.cursor()

    _ensure_precificador_schema()

    # EANs ocultos por esta loja
    cur.execute("SELECT ean FROM ecommerce_catalogo_oculto WHERE cnpjloja = %s", (cnpjloja,))
    ocultos = {r["ean"] for r in cur.fetchall()}

    busca_alpha = busca_auto = ""
    args_alpha = [cnpjloja]
    args_auto  = [cnpjloja]
    if q:
        like = f"%{q.lower()}%"
        busca_alpha = "AND LOWER(e.descricao) LIKE %s"
        busca_auto  = "AND LOWER(ae.descricao_produto) LIKE %s"
        args_alpha.append(like)
        args_auto.append(like)

    cur.execute(_SQL_ALPHA.format(busca=busca_alpha), args_alpha)
    alpha = cur.fetchall()

    cur.execute(_SQL_AUTO.format(busca=busca_auto), args_auto)
    auto = cur.fetchall()

    seen, combined = set(), []
    for row in list(alpha) + list(auto):
        ean = (row["ean"] or "").strip()
        if ean not in seen and (include_hidden or ean not in ocultos):
            seen.add(ean)
            d = dict(row)
            d["is_extra"] = False
            d["oculto"] = ean in ocultos
            combined.append(d)

    # Produtos extras incluídos manualmente pela loja
    cur.execute("SELECT ean FROM ecommerce_catalogo_extra WHERE cnpjloja = %s", (cnpjloja,))
    extra_eans = [r["ean"] for r in cur.fetchall() if r["ean"] not in seen]

    if extra_eans:
        cur.execute("""
            SELECT e.barras AS ean, e.descricao AS nome,
                   CAST(e.estoque AS INTEGER) AS qty,
                   COALESCE(vg.preco_venda, e.preco_referencial) AS preco_ref,
                   ep.preco_customizado AS preco_custom,
                   COALESCE(ep.preco_customizado, vg.preco_venda, e.preco_referencial) AS preco,
                   e.custo_medio AS custo,
                   COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
            FROM estoque e
            LEFT JOIN LATERAL (
                SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
                FROM vendageral
                WHERE cnpj = e.cnpj AND ean = e.barras
                  AND total_vendasgeral > 0 AND itens > 0
                ORDER BY id DESC LIMIT 1
            ) vg ON TRUE
            LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = e.cnpj AND ep.ean = e.barras
            LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = e.cnpj AND epi.ean = e.barras
            WHERE e.cnpj = %s AND e.barras = ANY(%s)
        """, (cnpjloja, extra_eans))
        for row in cur.fetchall():
            ean = (row["ean"] or "").strip()
            if ean not in seen:
                seen.add(ean)
                d = dict(row)
                d["is_extra"] = True
                d["oculto"] = False
                combined.append(d)

        remaining = [e for e in extra_eans if e not in seen]
        if remaining:
            cur.execute("""
                SELECT ae.ean, ae.descricao_produto AS nome,
                       CAST(ae.quantidade_estoque AS INTEGER) AS qty,
                       COALESCE(av.preco_venda, ae.valor_final_produto) AS preco_ref,
                       ep.preco_customizado AS preco_custom,
                       COALESCE(ep.preco_customizado, av.preco_venda, ae.valor_final_produto) AS preco,
                       ae.custo AS custo,
                       COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
                FROM automatiza_estoque ae
                LEFT JOIN LATERAL (
                    SELECT ROUND(valor_final_vendido / NULLIF(quantidade_vendida, 0), 2) AS preco_venda
                    FROM automatiza_vendas
                    WHERE cnpj_loja = ae.cnpj_loja AND ean = ae.ean
                      AND valor_final_vendido > 0 AND quantidade_vendida > 0
                    ORDER BY id DESC LIMIT 1
                ) av ON TRUE
                LEFT JOIN medicamentos m ON m.barra_norm = ae.ean
                LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
                LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = ae.cnpj_loja AND ep.ean = ae.ean
                LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = ae.cnpj_loja AND epi.ean = ae.ean
                WHERE ae.cnpj_loja = %s AND ae.ean = ANY(%s)
            """, (cnpjloja, remaining))
            for row in cur.fetchall():
                ean = (row["ean"] or "").strip()
                if ean not in seen:
                    seen.add(ean)
                    d = dict(row)
                    d["is_extra"] = True
                    d["oculto"] = False
                    combined.append(d)

    _apply_safe_catalog_images(combined, cur=cur)
    combined = _dedupe_products_for_display(combined)
    cur.close()
    _schedule_fill_images(combined, cnpjloja=cnpjloja)
    return sorted(combined, key=lambda x: (x.get("nome") or "").lower())


# ─── BATCH DNS PRODUCTS (home page) ──────────────────────────────────────────

_SQL_ALPHA_BATCH = """
    WITH eligible AS (
        SELECT
            e.cnpj                            AS cnpjloja,
            e.barras,
            COALESCE(e.barras_norm, e.barras) AS ean_join,
            e.descricao,
            CAST(e.estoque AS INTEGER)        AS qty,
            e.preco_referencial
        FROM estoque e
        WHERE e.cnpj = ANY(%s) AND e.estoque > 0
          AND (
            COALESCE(e.barras_norm, e.barras) IN (SELECT ean_norm FROM omie_estoque_dns)
            OR COALESCE(e.barras_norm, e.barras) IN (
                SELECT barra_norm FROM medicamentos
                WHERE barra_norm IS NOT NULL
                  AND (
                    NULLIF(TRIM(imagem), '') IS NOT NULL
                    OR id IN (SELECT medicamento_id FROM medicamentos_imagens
                              WHERE cloudinary_url IS NOT NULL)
                  )
            )
            OR e.descricao ILIKE ANY(ARRAY[
                '%%anasol%%','%%vit natu%%','%%vitnatu%%',
                '%%pronabol%%','%%ricosol%%','%%unispray%%','%%goodvit%%'
            ])
          )
        ORDER BY e.descricao
        LIMIT 600
    )
    SELECT
        el.cnpjloja,
        el.barras                                                             AS ean,
        COALESCE(m.descricao, pc.descricao_canon, el.descricao)              AS nome,
        COALESCE(m.laboratorio, pc.laboratorio)                              AS laboratorio,
        CASE WHEN m.id IS NOT NULL THEN 'medicamento' ELSE pc.categoria END  AS categoria,
        el.qty,
        COALESCE(ep.preco_customizado, vg.preco_venda, el.preco_referencial)  AS preco,
        COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
    FROM eligible el
    LEFT JOIN LATERAL (
        SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
        FROM vendageral
        WHERE cnpj = el.cnpjloja AND ean = el.barras
          AND total_vendasgeral > 0 AND itens > 0
        ORDER BY id DESC
        LIMIT 1
    ) vg ON TRUE
    LEFT JOIN medicamentos m          ON m.barra_norm = el.ean_join
    LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
    LEFT JOIN produto_canon pc        ON pc.ean = el.ean_join AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss')
    LEFT JOIN ecommerce_precos ep     ON ep.cnpjloja = el.cnpjloja AND ep.ean = el.barras
    LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = el.cnpjloja AND epi.ean = el.barras
"""

_SQL_AUTO_BATCH = """
    WITH eligible AS (
        SELECT
            ae.cnpj_loja                          AS cnpjloja,
            ae.ean,
            ae.descricao_produto                  AS descricao,
            CAST(ae.quantidade_estoque AS INTEGER) AS qty,
            ae.valor_final_produto
        FROM automatiza_estoque ae
        WHERE ae.cnpj_loja = ANY(%s) AND ae.quantidade_estoque > 0
          AND (
            ae.ean IN (SELECT ean_norm FROM omie_estoque_dns)
            OR ae.ean IN (
                SELECT barra_norm FROM medicamentos
                WHERE barra_norm IS NOT NULL
                  AND (
                    NULLIF(TRIM(imagem), '') IS NOT NULL
                    OR id IN (SELECT medicamento_id FROM medicamentos_imagens
                              WHERE cloudinary_url IS NOT NULL)
                  )
            )
            OR ae.descricao_produto ILIKE ANY(ARRAY[
                '%%anasol%%','%%vit natu%%','%%vitnatu%%',
                '%%pronabol%%','%%ricosol%%','%%unispray%%','%%goodvit%%'
            ])
          )
        ORDER BY ae.descricao_produto
        LIMIT 600
    )
    SELECT
        el.cnpjloja,
        el.ean,
        COALESCE(m.descricao, pc.descricao_canon, el.descricao)                   AS nome,
        COALESCE(m.laboratorio, pc.laboratorio)                                   AS laboratorio,
        CASE WHEN m.id IS NOT NULL THEN 'medicamento' ELSE pc.categoria END        AS categoria,
        el.qty,
        COALESCE(ep.preco_customizado, av.preco_venda, el.valor_final_produto)    AS preco,
        COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), ''))  AS imagem
    FROM eligible el
    LEFT JOIN LATERAL (
        SELECT ROUND(valor_final_vendido / NULLIF(quantidade_vendida, 0), 2) AS preco_venda
        FROM automatiza_vendas
        WHERE cnpj_loja = el.cnpjloja AND ean = el.ean
          AND valor_final_vendido > 0 AND quantidade_vendida > 0
        ORDER BY id DESC
        LIMIT 1
    ) av ON TRUE
    LEFT JOIN medicamentos m          ON m.barra_norm = el.ean
    LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
    LEFT JOIN produto_canon pc        ON pc.ean = el.ean AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss')
    LEFT JOIN ecommerce_precos ep     ON ep.cnpjloja = el.cnpjloja AND ep.ean = el.ean
    LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = el.cnpjloja AND epi.ean = el.ean
"""


def get_dns_products_batch(cnpjs):
    """Fetch DNS products for multiple CNPJs in two batch queries."""
    if not cnpjs:
        return []
    _ensure_catalog_admin_schema()
    try:
        conn_cfg = db()
        cur_cfg = conn_cfg.cursor()
        cur_cfg.execute(
            "SELECT cnpjloja FROM ecommerce_config_loja WHERE catalogo_publico IS FALSE AND cnpjloja = ANY(%s)",
            (cnpjs,),
        )
        bloqueadas = {r["cnpjloja"] for r in cur_cfg.fetchall()}
        cur_cfg.close()
        cnpjs = [c for c in cnpjs if c not in bloqueadas]
    except Exception:
        cnpjs = list(cnpjs)
    if not cnpjs:
        return []
    cache_key = tuple(sorted(cnpjs))
    cached = _batch_cache_get(cache_key)
    if cached is not None:
        return cached
    _ensure_precificador_schema()
    _ensure_produto_canon_schema()
    conn = _new_conn_batch()
    cur = conn.cursor()
    cur.execute(_SQL_ALPHA_BATCH, (cnpjs,))
    alpha = cur.fetchall()
    cur.execute(_SQL_AUTO_BATCH, (cnpjs,))
    auto = cur.fetchall()

    # Ocultos por loja
    cur.execute(
        "SELECT cnpjloja, ean FROM ecommerce_catalogo_oculto WHERE cnpjloja = ANY(%s)",
        (cnpjs,),
    )
    ocultos = {(r["cnpjloja"], r["ean"]) for r in cur.fetchall()}

    # Extras adicionados manualmente pelas lojas
    cur.execute(
        "SELECT cnpjloja, ean FROM ecommerce_catalogo_extra WHERE cnpjloja = ANY(%s)",
        (cnpjs,),
    )
    extra_pairs = [(r["cnpjloja"], r["ean"]) for r in cur.fetchall()]

    seen, combined = set(), []
    for row in list(alpha) + list(auto):
        ean = (row["ean"] or "").strip()
        cnpj = row["cnpjloja"]
        key = (ean, cnpj)
        if key not in seen and (cnpj, ean) not in ocultos:
            seen.add(key)
            combined.append(dict(row))

    # Busca dados dos extras que ainda não apareceram nas queries principais
    if extra_pairs:
        extra_keys = {(cnpj, ean) for cnpj, ean in extra_pairs if (ean, cnpj) not in seen}
        if extra_keys:
            all_extra_eans = list({ean for _, ean in extra_keys})

            cur.execute("""
                SELECT e.cnpj AS cnpjloja, e.barras AS ean,
                       COALESCE(m.descricao, pc.descricao_canon, e.descricao) AS nome,
                       COALESCE(m.laboratorio, pc.laboratorio) AS laboratorio,
                       CASE WHEN m.id IS NOT NULL THEN 'medicamento' ELSE pc.categoria END AS categoria,
                       CAST(e.estoque AS INTEGER) AS qty,
                       COALESCE(ep.preco_customizado, vg.preco_venda, e.preco_referencial) AS preco,
                       COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
                FROM estoque e
                LEFT JOIN LATERAL (
                    SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
                    FROM vendageral
                    WHERE cnpj = e.cnpj AND ean = e.barras
                      AND total_vendasgeral > 0 AND itens > 0
                    ORDER BY id DESC LIMIT 1
                ) vg ON TRUE
                LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
                LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
                LEFT JOIN produto_canon pc ON pc.ean = COALESCE(e.barras_norm, e.barras) AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss')
                LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = e.cnpj AND ep.ean = e.barras
                LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = e.cnpj AND epi.ean = e.barras
                WHERE e.cnpj = ANY(%s) AND e.barras = ANY(%s) AND e.estoque > 0
            """, (cnpjs, all_extra_eans))
            for row in cur.fetchall():
                ean = (row["ean"] or "").strip()
                cnpj = row["cnpjloja"]
                key = (ean, cnpj)
                if key not in seen and (cnpj, ean) in extra_keys and (cnpj, ean) not in ocultos:
                    seen.add(key)
                    combined.append(dict(row))

            cur.execute("""
                SELECT ae.cnpj_loja AS cnpjloja, ae.ean,
                       COALESCE(m.descricao, pc.descricao_canon, ae.descricao_produto) AS nome,
                       COALESCE(m.laboratorio, pc.laboratorio) AS laboratorio,
                       CASE WHEN m.id IS NOT NULL THEN 'medicamento' ELSE pc.categoria END AS categoria,
                       CAST(ae.quantidade_estoque AS INTEGER) AS qty,
                       COALESCE(ep.preco_customizado, av.preco_venda, ae.valor_final_produto) AS preco,
                       COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
                FROM automatiza_estoque ae
                LEFT JOIN LATERAL (
                    SELECT ROUND(valor_final_vendido / NULLIF(quantidade_vendida, 0), 2) AS preco_venda
                    FROM automatiza_vendas
                    WHERE cnpj_loja = ae.cnpj_loja AND ean = ae.ean
                      AND valor_final_vendido > 0 AND quantidade_vendida > 0
                    ORDER BY id DESC LIMIT 1
                ) av ON TRUE
                LEFT JOIN medicamentos m ON m.barra_norm = ae.ean
                LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
                LEFT JOIN produto_canon pc ON pc.ean = ae.ean AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss')
                LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = ae.cnpj_loja AND ep.ean = ae.ean
                LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = ae.cnpj_loja AND epi.ean = ae.ean
                WHERE ae.cnpj_loja = ANY(%s) AND ae.ean = ANY(%s) AND ae.quantidade_estoque > 0
            """, (cnpjs, all_extra_eans))
            for row in cur.fetchall():
                ean = (row["ean"] or "").strip()
                cnpj = row["cnpjloja"]
                key = (ean, cnpj)
                if key not in seen and (cnpj, ean) in extra_keys and (cnpj, ean) not in ocultos:
                    seen.add(key)
                    combined.append(dict(row))

    _apply_safe_catalog_images(combined, cur=cur)
    cur.close()
    try:
        conn.close()
    except Exception:
        pass
    _batch_cache_set(cache_key, combined)
    _schedule_fill_images(combined)
    return combined


# ─── AUTH ─────────────────────────────────────────────────────────────────────

def painel_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("painel_ok"):
            return redirect(url_for("painel_login"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("painel_ok") or not session.get("is_admin"):
            return redirect(url_for("painel_login"))
        return fn(*args, **kwargs)
    return wrapper


# ─── ROTAS PÚBLICAS ───────────────────────────────────────────────────────────

@app.get("/")
def index():
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.cnpjloja, u.razao, u.endereco, u.uf, u.telefone,
               g.lat, g.lng
        FROM users u
        LEFT JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
        WHERE u.is_admin = FALSE
        ORDER BY u.razao
        """
    )
    lojas = cur.fetchall()
    cur.close()
    return render_template("index.html", lojas=lojas)


@app.get("/lojas")
def lojas_vitrine():
    _ensure_lojas_vitrine_schema()

    # Busca do ImageKit (lojas legadas do poupaqui-admin)
    lojas_ik = _load_imagekit_lojas()

    # Busca do Supabase (lojas novas adicionadas pelo painel dns-ecommerce)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT cidade, endereco, telefone, whatsapp, imagem_url FROM ecommerce_lojas_vitrine ORDER BY ordem, cidade")
    rows = cur.fetchall()
    cur.close()
    lojas_sb = [
        {
            "cidade": r["cidade"],
            "endereco": r["endereco"],
            "telefone": r["telefone"],
            "whatsapp": r["whatsapp"],
            "imagem_url": r["imagem_url"],
        }
        for r in rows
    ]

    # Normaliza lojas do ImageKit para o mesmo formato
    lojas_ik_norm = [
        {
            "cidade": l.get("cidade", ""),
            "endereco": l.get("endereco", ""),
            "telefone": l.get("telefone", ""),
            "whatsapp": l.get("whatsapp", ""),
            "imagem_url": l.get("url", ""),
        }
        for l in lojas_ik
    ]

    # Combina: ImageKit primeiro (já vem ordenado por cidade), depois Supabase
    from itertools import chain
    todas = sorted(
        chain(lojas_ik_norm, lojas_sb),
        key=lambda x: (x.get("cidade") or "").lower()
    )

    return render_template("lojas_vitrine.html", lojas=todas)


def _ik_auth_header():
    """Retorna header de autenticação Basic para ImageKit."""
    import base64
    key = os.getenv("IMAGEKIT_PRIVATE_KEY", "")
    token = base64.b64encode(f"{key}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _load_imagekit_lojas(include_file_id=False):
    """Busca lojas no ImageKit. Se include_file_id=True, inclui fileId para admin."""
    import time
    import urllib.request
    import urllib.error
    import json as _json

    if not os.getenv("IMAGEKIT_PRIVATE_KEY"):
        return []

    headers = _ik_auth_header()
    stores = []
    skip = 0
    limit = 100

    try:
        timestamp = int(time.time())
        while True:
            url = (
                f"https://api.imagekit.io/v1/files"
                f"?path=/lojas_poupAqui/&type=file&limit={limit}&skip={skip}"
            )
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    files = _json.loads(resp.read())
            except urllib.error.URLError:
                break

            if not files:
                break

            for img in files:
                meta = img.get("customMetadata") or {}
                endereco = (meta.get("endereco") or "").strip()
                cidade   = (meta.get("cidade")   or "Sem cidade").strip()
                telefone = (meta.get("telefone") or "").strip()
                whatsapp = (meta.get("whatsapp") or "").strip()
                img_url  = img.get("url", "")
                if img_url:
                    img_url = f"{img_url}?t={timestamp}"
                entry = {
                    "cidade":    cidade,
                    "endereco":  endereco,
                    "telefone":  telefone,
                    "whatsapp":  whatsapp,
                    "url":       img_url,
                }
                if include_file_id:
                    entry["fileId"] = img.get("fileId", "")
                stores.append(entry)

            if len(files) < limit:
                break
            skip += limit

        stores.sort(key=lambda l: (l.get("cidade") or "").lower())
    except Exception:
        pass

    return stores


def _ik_delete_file(file_id):
    """Exclui arquivo do ImageKit pelo fileId."""
    import urllib.request
    import urllib.error

    if not file_id:
        raise ValueError("fileId não informado")

    req = urllib.request.Request(
        f"https://api.imagekit.io/v1/files/{file_id}",
        headers=_ik_auth_header(),
        method="DELETE",
    )
    try:
        urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise


def _ik_update_metadata(file_id, cidade, endereco, telefone, whatsapp):
    """Atualiza customMetadata de um arquivo no ImageKit."""
    import urllib.request
    import json as _json

    payload = _json.dumps({
        "customMetadata": {
            "cidade":   cidade,
            "endereco": endereco,
            "telefone": telefone,
            "whatsapp": whatsapp,
        }
    }).encode()

    headers = {**_ik_auth_header(), "Content-Type": "application/json"}
    req = urllib.request.Request(
        f"https://api.imagekit.io/v1/files/{file_id}/details",
        data=payload,
        headers=headers,
        method="PATCH",
    )
    urllib.request.urlopen(req, timeout=30)


def _ensure_lojas_vitrine_schema():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ecommerce_lojas_vitrine (
            id          SERIAL PRIMARY KEY,
            cidade      TEXT NOT NULL,
            endereco    TEXT NOT NULL,
            telefone    TEXT,
            whatsapp    TEXT,
            imagem_url  TEXT,
            ordem       INT DEFAULT 0,
            cnpjloja    TEXT,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # garante coluna cnpjloja mesmo em tabelas criadas antes desta versão
    cur.execute("""
        ALTER TABLE ecommerce_lojas_vitrine
        ADD COLUMN IF NOT EXISTS cnpjloja TEXT
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ecommerce_lojas_vitrine_cliques (
            id         SERIAL PRIMARY KEY,
            cidade     TEXT NOT NULL,
            tipo       TEXT NOT NULL,
            cnpjloja   TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_lvc_cnpj ON ecommerce_lojas_vitrine_cliques(cnpjloja)
        WHERE cnpjloja IS NOT NULL
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_lvc_cidade ON ecommerce_lojas_vitrine_cliques(cidade)
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ecommerce_vitrine_cnpj_map (
            cidade   TEXT PRIMARY KEY,
            cnpjloja TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ecommerce_vitrine_coords (
            cidade TEXT PRIMARY KEY,
            lat    DOUBLE PRECISION NOT NULL,
            lng    DOUBLE PRECISION NOT NULL
        )
    """)
    conn.commit()
    cur.close()


@app.post("/api/lojas/clique")
def api_lojas_clique():
    """Registra um clique de WhatsApp ou Como Chegar na página de lojas."""
    _ensure_lojas_vitrine_schema()
    data = request.get_json(silent=True) or {}
    cidade = (data.get("cidade") or "").strip()
    tipo   = (data.get("tipo") or "").strip()
    if not cidade or tipo not in ("whatsapp", "maps"):
        return jsonify({"ok": False}), 400

    conn = db()
    cur = conn.cursor()
    # tenta achar o cnpjloja pelo mapa explícito (funciona para lojas ImageKit e Cloudinary)
    cur.execute("SELECT cnpjloja FROM ecommerce_vitrine_cnpj_map WHERE cidade = %s LIMIT 1", (cidade,))
    row = cur.fetchone()
    cnpjloja = row["cnpjloja"] if row else None
    # fallback: tabela Cloudinary (lojas com cnpjloja preenchido)
    if not cnpjloja:
        cur.execute("SELECT cnpjloja FROM ecommerce_lojas_vitrine WHERE cidade = %s AND cnpjloja IS NOT NULL LIMIT 1", (cidade,))
        row = cur.fetchone()
        cnpjloja = row["cnpjloja"] if row else None

    cur.execute(
        "INSERT INTO ecommerce_lojas_vitrine_cliques (cidade, tipo, cnpjloja) VALUES (%s,%s,%s)",
        (cidade, tipo, cnpjloja),
    )
    conn.commit()
    cur.close()
    return jsonify({"ok": True})


@app.get("/api/lojas/mapa")
def api_lojas_mapa():
    """Retorna todas as lojas vitrine com coordenadas para o mapa Leaflet."""
    _ensure_lojas_vitrine_schema()
    conn = db()
    cur  = conn.cursor()
    cur.execute("SELECT cidade, lat, lng FROM ecommerce_vitrine_coords")
    coords = {r["cidade"]: (r["lat"], r["lng"]) for r in cur.fetchall()}
    cur.execute(
        "SELECT cidade, endereco, telefone, whatsapp, imagem_url "
        "FROM ecommerce_lojas_vitrine ORDER BY ordem, cidade"
    )
    lojas_sb = [dict(r) for r in cur.fetchall()]
    cur.close()

    lojas_ik = _load_imagekit_lojas()
    lojas_ik_norm = [
        {"cidade": l.get("cidade",""), "endereco": l.get("endereco",""),
         "telefone": l.get("telefone",""), "whatsapp": l.get("whatsapp",""),
         "imagem_url": l.get("url","")}
        for l in lojas_ik
    ]

    from itertools import chain
    resultado = []
    for loja in sorted(chain(lojas_ik_norm, lojas_sb),
                       key=lambda x: (x.get("cidade") or "").lower()):
        c = loja.get("cidade","")
        loja["lat"], loja["lng"] = coords.get(c, (None, None))
        resultado.append(loja)
    return jsonify(resultado)


@app.get("/painel/admin/lojas-vitrine/pendentes-coords")
@admin_required
def admin_lojas_vitrine_pendentes_coords():
    """Retorna JSON com lojas que ainda não têm coordenadas (para geocodificação client-side).
    Usa endereco2 da tabela users (endereço real de rua) via ecommerce_vitrine_cnpj_map."""
    _ensure_lojas_vitrine_schema()
    conn = db()
    cur  = conn.cursor()
    cur.execute("SELECT cidade FROM ecommerce_vitrine_coords")
    ja_feitas = {r["cidade"] for r in cur.fetchall()}

    # Mapa cidade → endereco2 real via cnpj_map + users
    cur.execute("""
        SELECT m.cidade, u.endereco2
        FROM ecommerce_vitrine_cnpj_map m
        JOIN users u ON u.cnpjloja = m.cnpjloja
        WHERE u.endereco2 IS NOT NULL AND u.endereco2 <> ''
    """)
    endereco2_map = {r["cidade"]: r["endereco2"] for r in cur.fetchall()}

    # Lojas Supabase: usa endereco2 se disponível, senão endereco da própria tabela
    cur.execute("SELECT cidade, endereco, cnpjloja FROM ecommerce_lojas_vitrine")
    lojas_sb = []
    for r in cur.fetchall():
        end = endereco2_map.get(r["cidade"]) or r["endereco"] or ""
        lojas_sb.append((r["cidade"], end))
    cur.close()

    lojas_ik = _load_imagekit_lojas()
    lojas_ik_pairs = []
    for l in lojas_ik:
        cidade = l.get("cidade", "")
        # Para IK: endereco do metadata é só o nome da cidade — usa endereco2 do users
        end = endereco2_map.get(cidade) or ""
        lojas_ik_pairs.append((cidade, end))

    from itertools import chain
    pendentes, seen = [], set()
    for cidade, endereco in chain(lojas_ik_pairs, lojas_sb):
        if cidade and cidade not in ja_feitas and cidade not in seen:
            seen.add(cidade)
            pendentes.append({"cidade": cidade, "endereco": endereco})
    return jsonify(pendentes)


@app.post("/painel/admin/lojas-vitrine/salvar-coord")
@admin_required
def admin_lojas_vitrine_salvar_coord():
    """Salva coordenadas de uma loja (chamado pelo geocodificador client-side)."""
    _ensure_lojas_vitrine_schema()
    data = request.get_json(silent=True) or {}
    cidade = (data.get("cidade") or "").strip()
    try:
        lat = float(data["lat"])
        lng = float(data["lng"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "erro": "lat/lng inválidos"}), 400
    if not cidade:
        return jsonify({"ok": False, "erro": "cidade obrigatória"}), 400
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        "INSERT INTO ecommerce_vitrine_coords (cidade,lat,lng) VALUES (%s,%s,%s) "
        "ON CONFLICT (cidade) DO UPDATE SET lat=EXCLUDED.lat, lng=EXCLUDED.lng",
        (cidade, lat, lng),
    )
    conn.commit()
    cur.close()
    return jsonify({"ok": True})


@app.get("/api/lojas-proximas")
@_rate_limited_api(max_calls=60, window_secs=60)
def api_lojas_proximas():
    try:
        lat_usr = float(request.args["lat"])
        lng_usr = float(request.args["lng"])
    except (KeyError, ValueError):
        return jsonify({"error": "lat/lng inválidos"}), 400

    conn = db()
    cur = conn.cursor()
    # LEFT JOIN — retorna todas as lojas, com ou sem geocodificação
    cur.execute(
        """
        SELECT u.cnpjloja, u.razao, u.endereco, u.uf, u.telefone, g.lat, g.lng
        FROM users u
        LEFT JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
        WHERE u.is_admin = FALSE
        """
    )
    lojas = cur.fetchall()
    cur.close()

    com_geo, sem_geo = [], []
    for l in lojas:
        item = {
            "cnpjloja":    l["cnpjloja"],
            "razao":       _public_store_name(l),
            "endereco":    l["endereco"],
            "uf":          l["uf"],
            "telefone":    (l["telefone"] or "").strip(),
            "distancia_km": None,
        }
        lat_l, lng_l = _geo_override(l.get("endereco"), None, l.get("uf"))
        if l["lat"] or lat_l:
            lat_calc = lat_l if lat_l else float(l["lat"])
            lng_calc = lng_l if lng_l else float(l["lng"])
            item["distancia_km"] = round(haversine(lat_usr, lng_usr, lat_calc, lng_calc), 1)
            com_geo.append(item)
        else:
            sem_geo.append(item)

    com_geo.sort(key=lambda x: x["distancia_km"])
    return jsonify(com_geo[:10] + sem_geo[:20])


@app.get("/api/produtos-proximos")
@_rate_limited_api(max_calls=40, window_secs=60)
def api_produtos_proximos():
    _ensure_delivery_schema()
    _ensure_catalog_admin_schema()
    try:
        lat_usr = float(request.args["lat"])
        lng_usr = float(request.args["lng"])
    except (KeyError, ValueError):
        return jsonify({"error": "lat/lng inválidos"}), 400

    raio    = float(request.args.get("raio", 30))
    raio_fallback = max(raio, float(request.args.get("raio_fallback", 60)))
    sem_loc = (lat_usr == 0.0 and lng_usr == 0.0)

    conn = db()
    cur  = conn.cursor()
    fora_raio  = False
    sem_geocode = False
    proximas   = []
    loja_info  = {}

    if not sem_loc:
        cur.execute(
            """
            SELECT u.cnpjloja, u.razao, u.endereco, u.endereco2, u.uf, g.lat, g.lng,
                   COALESCE(c.aceita_entrega, FALSE) AS aceita_entrega,
                   COALESCE(c.raio_entrega_km, 0) AS raio_entrega_km,
                   COALESCE(c.cobra_frete, FALSE) AS cobra_frete,
                   COALESCE(c.valor_frete, 0) AS valor_frete
            FROM users u
            JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
            WHERE u.is_admin = FALSE
              AND g.lat IS NOT NULL
              AND COALESCE(c.catalogo_publico, TRUE) = TRUE
            """
        )
        geocodificadas = cur.fetchall()

        if geocodificadas:
            lojas_dist = []
            for l in geocodificadas:
                lat_l, lng_l = _geo_override(l.get("endereco"), l.get("endereco2"), l.get("uf"))
                if not lat_l:
                    lat_l, lng_l = float(l["lat"]), float(l["lng"])
                dist = haversine(lat_usr, lng_usr, float(lat_l), float(lng_l))
                item = dict(l)
                item["lat"] = lat_l
                item["lng"] = lng_l
                lojas_dist.append({**item, "distancia_km": round(dist, 2)})
            lojas_dist.sort(key=lambda x: x["distancia_km"])

            proximas = [l for l in lojas_dist if l["distancia_km"] <= raio]
            if not proximas:
                proximas  = [l for l in lojas_dist if l["distancia_km"] <= raio_fallback][:3]
                fora_raio = bool(proximas)
            loja_info = {l["cnpjloja"]: l for l in proximas}
        else:
            sem_loc     = True   # nenhuma geocodificada — cai no fallback
            sem_geocode = True   # sinaliza que o problema é falta de geocode

    if sem_loc:
        cur.execute(
            """
            SELECT u.cnpjloja, u.razao, u.endereco
            FROM users u
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
            WHERE u.is_admin = FALSE
              AND COALESCE(c.catalogo_publico, TRUE) = TRUE
            ORDER BY u.razao
            """
        )
        todas    = cur.fetchall()
        proximas = list(todas)
        loja_info = {
            l["cnpjloja"]: {"razao": _public_store_name(l), "distancia_km": None, "aceita_entrega": False, "raio_entrega_km": 0, "cobra_frete": False, "valor_frete": 0}
            for l in todas
        }

    cur.close()

    if not proximas:
        return jsonify({"produtos": [], "fora_raio": False, "raio_km": raio, "raio_fallback_km": raio_fallback, "n_lojas": 0})

    cnpjs        = [l["cnpjloja"] for l in proximas]
    produtos_raw = get_dns_products_batch(cnpjs)

    # Deduplica por EAN: mantém da farmácia mais próxima
    produtos_view = []
    for p in produtos_raw:
        if not _has_catalog_image(p):
            continue
        ean = (p["ean"] or "").strip()
        if not ean:
            continue
        info  = loja_info.get(p["cnpjloja"], {})
        dist  = info.get("distancia_km")
        razao = _public_store_name(info)
        aceita_entrega = bool(info.get("aceita_entrega"))
        raio_entrega = float(info.get("raio_entrega_km") or 0)
        entrega_disponivel = bool(aceita_entrega and dist is not None and dist <= raio_entrega)
        frete_valor = float(info.get("valor_frete") or 0) if entrega_disponivel and info.get("cobra_frete") else 0.0
        entrega_meta = {
            "aceita_entrega": aceita_entrega,
            "raio_entrega_km": raio_entrega,
            "entrega_disponivel": entrega_disponivel,
            "cobra_frete": bool(info.get("cobra_frete")),
            "valor_frete": frete_valor,
        }

        categoria = _classificar_produto(p.get("nome") or "")
        produto_view = {**p, "razao": razao, "distancia_km": dist, "categoria": categoria, **entrega_meta}

        produtos_view.append(produto_view)

    conn2 = db()
    produtos_view = _dedupe_products_for_display(produtos_view)
    _marcar_tarja_batch(produtos_view, conn2)
    conn2.close()

    result = sorted(produtos_view, key=lambda x: (x.get("distancia_km") is None, x.get("distancia_km") or 0, (x.get("nome") or "").lower()))
    return jsonify({
        "produtos":    result[:500],
        "fora_raio":   fora_raio,
        "sem_geocode": sem_geocode,
        "raio_km":     raio,
        "raio_fallback_km": raio_fallback,
        "n_lojas":   len(proximas),
    })


@app.get("/api/produto/<ean>")
@_rate_limited_api(max_calls=120, window_secs=60)
def api_produto(ean):
    conn = db()
    cur = conn.cursor()

    ean_clean  = (ean or "").strip()
    nome_param = (request.args.get("nome") or "").strip()

    cur.execute(
        """
        SELECT m.descricao, m.marca, m.laboratorio, m.classe,
               COALESCE(mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
        FROM medicamentos m
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        WHERE LTRIM(COALESCE(m.barra_norm,''), '0') = LTRIM(%s, '0')
           OR LTRIM(COALESCE(m.barra,''), '0')      = LTRIM(%s, '0')
        LIMIT 1
        """,
        (ean_clean, ean_clean),
    )
    med = cur.fetchone()

    result = {"ean": ean_clean, "imagem_med": None, "marca": "", "laboratorio": "", "classe": ""}
    nome_busca = nome_param

    if med:
        imagem_med = med["imagem"] or None
        placeholder_generico = _generic_placeholder_for(
            nome_busca or med["descricao"] or "",
            med=dict(med),
        )
        if imagem_med and _looks_like_other_pharmacy_brand(imagem_med):
            imagem_med = placeholder_generico
        elif imagem_med and placeholder_generico and _is_untrusted_scraped_image(imagem_med) and _image_has_other_pharmacy_text(imagem_med):
            imagem_med = placeholder_generico
        elif not imagem_med and placeholder_generico:
            imagem_med = placeholder_generico
        result.update({
            "marca":       med["marca"]       or "",
            "laboratorio": med["laboratorio"] or "",
            "classe":      med["classe"]      or "",
            "imagem_med":  imagem_med,
        })
        nome_busca = nome_busca or (med["descricao"] or "")

    vitnatu = None
    if nome_busca:
        stop  = {"com","de","do","da","dos","das","para","por","em","e","ou","cp","ml","mg","un","gr"}
        words = [w for w in nome_busca.upper().split()
                 if len(w) >= 4 and w.lower() not in stop][:4]

        for n in range(len(words), 0, -1):
            conds    = " AND ".join(["UPPER(nome) LIKE %s"] * n)
            patterns = [f"%{w}%" for w in words[:n]]
            cur.execute(
                f"""
                SELECT nome, serve_para, porque_comprar, como_usar, alertas
                FROM vitnatu_produtos
                WHERE ativo = TRUE AND {conds}
                LIMIT 1
                """,
                patterns,
            )
            vitnatu = cur.fetchone()
            if vitnatu:
                break

    result["vitnatu"] = (
        {
            "nome":           vitnatu["nome"],
            "serve_para":     vitnatu["serve_para"]     or "",
            "porque_comprar": vitnatu["porque_comprar"] or "",
            "como_usar":      vitnatu["como_usar"]      or "",
            "alertas":        vitnatu["alertas"]        or "",
        }
        if vitnatu else None
    )

    cur.close()
    return jsonify(result)


@app.get("/api/config-lojas")
@_rate_limited_api(max_calls=30, window_secs=60)
def api_config_lojas():
    _ensure_delivery_schema()
    cnpjs = [c.strip() for c in request.args.get("cnpjs", "").split(",") if c.strip()]
    if not cnpjs:
        return jsonify({})
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.cnpjloja, u.razao, u.endereco, u.telefone,
               COALESCE(c.whatsapp_pedidos, u.telefone) AS whatsapp_pedidos,
               COALESCE(c.aceita_whatsapp, TRUE)        AS aceita_whatsapp,
               COALESCE(c.aceita_pix, TRUE)             AS aceita_pix,
               COALESCE(c.aceita_mp, FALSE)             AS aceita_mp,
               COALESCE(c.aceita_entrega, FALSE)        AS aceita_entrega,
               COALESCE(c.raio_entrega_km, 0)           AS raio_entrega_km,
               COALESCE(c.cobra_frete, FALSE)           AS cobra_frete,
               COALESCE(c.valor_frete, 0)               AS valor_frete,
               COALESCE(c.pedido_minimo_entrega, 0)     AS pedido_minimo_entrega,
               (c.pix_chave IS NOT NULL AND c.pix_chave <> '') AS tem_pix,
               g.lat AS loja_lat, g.lng AS loja_lng
        FROM users u
        LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
        LEFT JOIN ecommerce_lojas_geo g   ON g.cnpjloja = u.cnpjloja
        WHERE u.cnpjloja = ANY(%s)
        """,
        (cnpjs,),
    )
    rows = cur.fetchall()
    cur.close()
    data = {}
    for r in rows:
        item = dict(r)
        item["razao"] = _public_store_name(item)
        data[r["cnpjloja"]] = item
    return jsonify(data)


def _recommendation_tokens(nome):
    text = _norm_text(nome or "")
    stop = {
        "com", "para", "por", "sem", "dos", "das", "uma", "uns", "gen", "neo", "uni",
        "mg", "ml", "gr", "g", "cp", "cpr", "comprimido", "comprimidos", "capsula",
        "capsulas", "xpe", "creme", "generico", "genericos",
    }
    return {t for t in text.split() if len(t) > 2 and t not in stop and not t.isdigit()}


def _recommendation_reason(base_names, product_name, co_purchase=False):
    if co_purchase:
        return "Clientes tambem compraram"
    names = _norm_text(" ".join(base_names))
    prod = _norm_text(product_name)
    if any(w in names for w in ["fralda", "infantil", "bebe"]) and any(w in prod for w in ["lenco", "pomada", "assadura", "talco"]):
        return "Complementa cuidados do bebe"
    if any(w in names for w in ["protetor", "solar", "fps"]) and any(w in prod for w in ["hidratante", "pos sol", "labial", "facial"]):
        return "Combina com protecao e cuidado da pele"
    if any(w in names for w in ["gripe", "resfriado", "tosse", "febre"]) and any(w in prod for w in ["soro", "vitamina", "termometro", "pastilha", "mel"]):
        return "Ajuda a completar o cuidado"
    if any(w in names for w in ["whey", "creatina", "protein"]) and any(w in prod for w in ["vitamina", "omega", "colageno", "bcaa"]):
        return "Sugestao para sua rotina"
    return "Relacionado ao que voce esta vendo"


def _build_recommendations(itens, cnpjlojas, limit=8):
    _ensure_catalog_admin_schema()
    limit = max(1, min(int(limit or 8), 12))
    itens = [i for i in (itens or []) if isinstance(i, dict)]
    base_names = [(i.get("nome") or "").strip() for i in itens if (i.get("nome") or "").strip()]
    exclude_eans = {(i.get("ean") or "").strip() for i in itens if (i.get("ean") or "").strip()}
    cnpjs = [c.strip() for c in (cnpjlojas or []) if c and c.strip()]
    if not cnpjs:
        cnpjs = [c for c in {(i.get("cnpjloja") or "").strip() for i in itens} if c]
    if not cnpjs:
        return {"titulo": "Veja tambem", "subtitulo": "Produtos relacionados disponiveis", "produtos": []}

    produtos = [p for p in get_dns_products_batch(cnpjs) if p.get("ean") and p.get("ean") not in exclude_eans and _has_catalog_image(p)]
    if not produtos:
        return {"titulo": "Veja tambem", "subtitulo": "Produtos relacionados disponiveis", "produtos": []}

    loja_info = {}
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT cnpjloja, razao, endereco FROM users WHERE cnpjloja = ANY(%s)",
        (cnpjs,),
    )
    for r in cur.fetchall():
        item = dict(r)
        loja_info[item["cnpjloja"]] = _public_store_name(item)

    co_scores = {}
    if exclude_eans:
        try:
            cur.execute(
                """
                SELECT i2.ean, COUNT(*) AS score
                FROM ecommerce_pedido_itens i1
                JOIN ecommerce_pedido_itens i2 ON i2.pedido_id = i1.pedido_id
                JOIN ecommerce_pedidos p ON p.id = i1.pedido_id
                WHERE i1.ean = ANY(%s)
                  AND i2.ean <> ALL(%s)
                  AND p.cnpjloja = ANY(%s)
                GROUP BY i2.ean
                ORDER BY score DESC
                LIMIT 40
                """,
                (list(exclude_eans), list(exclude_eans), cnpjs),
            )
            co_scores = {r["ean"]: int(r["score"] or 0) for r in cur.fetchall()}
        except Exception:
            co_scores = {}
    cur.close()
    conn.close()

    base_tokens = set()
    base_cats = set()
    for name in base_names:
        base_tokens |= _recommendation_tokens(name)
        base_cats.add(_classificar_produto(name))

    ranked = []
    for p in produtos:
        pname = p.get("nome") or ""
        cat = _classificar_produto(pname)
        tokens = _recommendation_tokens(pname)
        overlap = len(base_tokens & tokens)
        co = co_scores.get(p.get("ean"), 0)
        score = co * 100 + overlap * 8
        if cat in base_cats:
            score += 16
        if p.get("qty"):
            score += min(int(p.get("qty") or 0), 20) / 10
        if score <= 0:
            score = 1
        ranked.append((score, co > 0, p))

    ranked.sort(key=lambda x: (-x[0], (x[2].get("nome") or "").lower()))
    result = []
    seen = set()
    for _, from_history, p in ranked:
        ean = p.get("ean")
        if ean in seen:
            continue
        seen.add(ean)
        item = dict(p)
        item["razao"] = loja_info.get(item.get("cnpjloja"), item.get("razao") or "Drogaria Poupaqui")
        item["categoria"] = _classificar_produto(item.get("nome") or "")
        item["motivo"] = _recommendation_reason(base_names, item.get("nome") or "", from_history)
        result.append(item)
        if len(result) >= limit:
            break

    titulo = "Quem comprou tambem levou" if co_scores else "Produtos que combinam com sua compra"
    subtitulo = "Sugestoes baseadas em pedidos reais e itens relacionados" if co_scores else "Sugestoes relacionadas ao produto e ao contexto da farmacia"
    return {"titulo": titulo, "subtitulo": subtitulo, "produtos": result}


@app.post("/api/recomendacoes")
def api_recomendacoes():
    data = request.get_json(silent=True) or {}
    itens = data.get("itens") or []
    cnpjlojas = data.get("cnpjlojas") or []
    if data.get("cnpjloja"):
        cnpjlojas.append(data.get("cnpjloja"))
    recs = _build_recommendations(itens, cnpjlojas, data.get("limit") or 8)
    return jsonify(recs)


@app.get("/produto/<ean>")
def produto_detalhe(ean):
    cnpjloja  = (request.args.get("cnpj")  or "").strip()
    nome_hint = (request.args.get("nome") or "").strip()
    conn = db()
    cur  = conn.cursor()

    cur.execute(
        """
        SELECT m.descricao, m.marca, m.laboratorio, m.classe,
               COALESCE(mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
        FROM medicamentos m
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        WHERE LTRIM(COALESCE(m.barra_norm,''),'0') = LTRIM(%s,'0')
           OR LTRIM(COALESCE(m.barra,''),'0')      = LTRIM(%s,'0')
        LIMIT 1
        """,
        (ean, ean),
    )
    med = cur.fetchone()

    nome_busca = nome_hint or (med["descricao"] if med else "")
    vitnatu = None
    if nome_busca:
        stop  = {"com","de","do","da","dos","das","para","por","em","e","ou","cp","ml","mg","un","gr"}
        words = [w for w in nome_busca.upper().split()
                 if len(w) >= 4 and w.lower() not in stop][:4]
        for n in range(len(words), 0, -1):
            conds    = " AND ".join(["UPPER(nome) LIKE %s"] * n)
            patterns = [f"%{w}%" for w in words[:n]]
            cur.execute(
                f"SELECT * FROM vitnatu_produtos WHERE ativo=TRUE AND {conds} LIMIT 1",
                patterns,
            )
            vitnatu = cur.fetchone()
            if vitnatu:
                break

    produto = loja = None
    imagem_custom = None
    if cnpjloja:
        cur.execute(
            "SELECT cnpjloja, razao, endereco, uf, telefone FROM users WHERE cnpjloja=%s AND is_admin=FALSE LIMIT 1",
            (cnpjloja,),
        )
        loja = cur.fetchone()
        if loja:
            loja = dict(loja)
            loja["razao"] = _public_store_name(loja)

        cur.execute(
            "SELECT imagem_url FROM ecommerce_produto_imagens WHERE cnpjloja=%s AND ean=%s LIMIT 1",
            (cnpjloja, ean),
        )
        row_img = cur.fetchone()
        imagem_custom = row_img["imagem_url"] if row_img else None

        cur.execute(
            """
            SELECT e.barras AS ean, e.descricao AS nome,
                   CAST(e.estoque AS INTEGER) AS qty,
                   COALESCE(ep.preco_customizado, vg.preco_venda, e.preco_referencial) AS preco,
                   COALESCE(%s, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
            FROM estoque e
            LEFT JOIN medicamentos m          ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
            LEFT JOIN medicamentos_imagens mi  ON mi.medicamento_id = m.id
            LEFT JOIN ecommerce_precos ep      ON ep.cnpjloja = e.cnpj AND ep.ean = e.barras
            LEFT JOIN LATERAL (
                SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
                FROM vendageral
                WHERE cnpj = e.cnpj AND ean = e.barras
                  AND total_vendasgeral > 0 AND itens > 0
                ORDER BY id DESC LIMIT 1
            ) vg ON TRUE
            WHERE e.cnpj = %s AND (e.barras = %s OR e.barras_norm = %s) AND e.estoque > 0
            LIMIT 1
            """,
            (imagem_custom, cnpjloja, ean, ean),
        )
        produto = cur.fetchone()

        if not produto:
            cur.execute(
                """
                SELECT ae.ean, ae.descricao_produto AS nome,
                       CAST(ae.quantidade_estoque AS INTEGER) AS qty,
                       COALESCE(ep.preco_customizado, av.preco_venda, ae.valor_final_produto) AS preco,
                       COALESCE(%s, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
                FROM automatiza_estoque ae
                LEFT JOIN medicamentos m          ON m.barra_norm = ae.ean
                LEFT JOIN medicamentos_imagens mi  ON mi.medicamento_id = m.id
                LEFT JOIN ecommerce_precos ep      ON ep.cnpjloja = ae.cnpj_loja AND ep.ean = ae.ean
                LEFT JOIN LATERAL (
                    SELECT ROUND(valor_final_vendido / NULLIF(quantidade_vendida, 0), 2) AS preco_venda
                    FROM automatiza_vendas
                    WHERE cnpj_loja = ae.cnpj_loja AND ean = ae.ean
                      AND valor_final_vendido > 0 AND quantidade_vendida > 0
                    ORDER BY id DESC LIMIT 1
                ) av ON TRUE
                WHERE ae.cnpj_loja = %s AND ae.ean = %s AND ae.quantidade_estoque > 0
                LIMIT 1
                """,
                (imagem_custom, cnpjloja, ean),
            )
            produto = cur.fetchone()

    # ANVISA cache lookup (same cursor, before closing)
    anvisa = {}
    _chave_anvisa = _anvisa_chave(nome_busca) if nome_busca else ""
    if _chave_anvisa:
        try:
            _anvisa_schema()   # ensure table exists (idempotent, own connection)
            cur.execute(
                "SELECT * FROM anvisa_cache WHERE chave=%s AND encontrado=TRUE",
                (_chave_anvisa,),
            )
            _row_anv = cur.fetchone()
            if _row_anv:
                anvisa = dict(_row_anv)
                for _campo in ("serve_para", "como_usar", "alertas"):
                    if anvisa.get(_campo):
                        anvisa[_campo] = _normalizar_bula(anvisa[_campo])
        except Exception:
            pass

    cur.close()

    if not produto and not med and not nome_hint:
        flash("Produto não encontrado.", "error")
        return redirect(url_for("index"))

    imagem = imagem_custom or (produto["imagem"] if produto else None) or (med["imagem"] if med else None)
    if not imagem and cnpjloja:
        imagem = _fill_one_catalog_image(cnpjloja, ean, nome_busca)
    nome   = (produto["nome"] if produto else None) or (med["descricao"] if med else nome_hint or "Produto")
    placeholder_generico = _generic_placeholder_for(nome, anvisa=anvisa, med=dict(med) if med else {})
    if imagem and _looks_like_other_pharmacy_brand(imagem):
        imagem = placeholder_generico
    elif imagem and placeholder_generico and _is_untrusted_scraped_image(imagem) and _image_has_other_pharmacy_text(imagem):
        imagem = placeholder_generico
    elif not imagem and placeholder_generico:
        imagem = placeholder_generico
    tipo_produto = _classificar_produto(nome)

    tarja = _detectar_tarja(anvisa)
    # Fallback por nome quando anvisa_cache não tem o produto
    if tarja is None and _NOME_TARJA_VERMELHA_RE.search(nome):
        tarja = "vermelha"
    requer_receita = tarja is not None

    return render_template(
        "produto_detalhe.html",
        ean=ean, nome=nome, imagem=imagem,
        med=dict(med) if med else {},
        vitnatu=dict(vitnatu) if vitnatu else {},
        produto=dict(produto) if produto else {},
        loja=dict(loja) if loja else {},
        anvisa=anvisa,
        tipo_produto=tipo_produto,
        tarja=tarja,
        requer_receita=requer_receita,
    )


@app.get("/carrinho")
def carrinho():
    return render_template("carrinho.html")


def _ensure_carrinho_schema():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ecommerce_carrinho (
            consumidor_id UUID NOT NULL,
            ean TEXT NOT NULL,
            cnpjloja TEXT NOT NULL,
            nome TEXT,
            preco NUMERIC(12,2),
            qty INTEGER DEFAULT 1,
            imagem TEXT,
            razao TEXT,
            atualizado_em TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (consumidor_id, ean, cnpjloja)
        )
    """)
    cur.execute("ALTER TABLE ecommerce_carrinho ADD COLUMN IF NOT EXISTS requer_receita BOOLEAN DEFAULT FALSE")
    conn.commit()
    cur.close()


@app.get("/api/carrinho")
def api_carrinho_get():
    cid = session.get("consumidor_id")
    if not cid:
        return jsonify({"items": []}), 200
    _ensure_carrinho_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT ean, cnpjloja, nome, preco, qty, imagem, razao, COALESCE(requer_receita,FALSE) AS requer_receita FROM ecommerce_carrinho WHERE consumidor_id=%s ORDER BY atualizado_em",
        (cid,),
    )
    items = [
        {"ean": r["ean"], "cnpjloja": r["cnpjloja"], "nome": r["nome"],
         "preco": float(r["preco"] or 0), "qty": r["qty"] or 1,
         "imagem": r["imagem"] or "", "razao": r["razao"] or "",
         "requer_receita": bool(r["requer_receita"])}
        for r in cur.fetchall()
    ]
    cur.close()
    return jsonify({"items": items})


@app.post("/api/carrinho/sync")
def api_carrinho_sync():
    cid = session.get("consumidor_id")
    if not cid:
        return jsonify({"ok": False}), 200
    _ensure_carrinho_schema()
    data = request.get_json(force=True) or {}
    items = data.get("items") or []
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM ecommerce_carrinho WHERE consumidor_id=%s", (cid,))
    for item in items:
        ean = (item.get("ean") or "").strip()
        cnpjloja = (item.get("cnpjloja") or "").strip()
        if not ean or not cnpjloja:
            continue
        cur.execute("""
            INSERT INTO ecommerce_carrinho
              (consumidor_id, ean, cnpjloja, nome, preco, qty, imagem, razao, requer_receita, atualizado_em)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (consumidor_id, ean, cnpjloja) DO UPDATE SET
              nome=EXCLUDED.nome, preco=EXCLUDED.preco, qty=EXCLUDED.qty,
              imagem=EXCLUDED.imagem, razao=EXCLUDED.razao,
              requer_receita=EXCLUDED.requer_receita, atualizado_em=NOW()
        """, (cid, ean, cnpjloja, item.get("nome"), item.get("preco"),
              int(item.get("qty") or 1), item.get("imagem"), item.get("razao"),
              bool(item.get("requer_receita"))))
    conn.commit()
    cur.close()
    return jsonify({"ok": True, "count": len(items)})


@app.get("/entrar")
def consumidor_login():
    if session.get("consumidor_id"):
        return redirect(request.args.get("next") or url_for("meus_pedidos"))
    return render_template("consumidor_login.html", next_url=request.args.get("next") or "")


@app.post("/entrar")
def consumidor_login_post():
    _ensure_consumidor_schema()
    email = _norm_email(request.form.get("email"))
    senha = request.form.get("senha") or ""
    next_url = request.form.get("next") or url_for("meus_pedidos")

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ecommerce_consumidores WHERE email=%s LIMIT 1", (email,))
    user = cur.fetchone()
    cur.close()

    if not user or not check_password_hash(user["senha_hash"], senha):
        flash("E-mail ou senha inválidos.", "error")
        return redirect(url_for("consumidor_login", next=next_url))

    session.permanent = bool(request.form.get("lembrar"))
    session["consumidor_id"] = str(user["id"])
    session["consumidor_nome"] = user["nome"]
    session["consumidor_email"] = user["email"]
    session["consumidor_telefone"] = user["telefone"]
    session["consumidor_endereco"] = user.get("endereco") or ""
    session["consumidor_lat"] = user.get("endereco_lat")
    session["consumidor_lng"] = user.get("endereco_lng")
    session["email_verificado"] = bool(user.get("email_verificado"))
    return redirect(next_url)


@app.get("/criar-conta")
def consumidor_criar_conta():
    if session.get("consumidor_id"):
        return redirect(request.args.get("next") or url_for("meus_pedidos"))
    return render_template("consumidor_cadastro.html", next_url=request.args.get("next") or "")


@app.post("/criar-conta")
def consumidor_criar_conta_post():
    _ensure_consumidor_schema()
    nome = (request.form.get("nome") or "").strip()
    telefone = (request.form.get("telefone") or "").strip()
    email = _norm_email(request.form.get("email"))
    senha = request.form.get("senha") or ""
    endereco = (request.form.get("endereco") or "").strip()
    endereco_lat = _to_float_or_none(request.form.get("endereco_lat"))
    endereco_lng = _to_float_or_none(request.form.get("endereco_lng"))
    next_url = request.form.get("next") or url_for("meus_pedidos")

    if not _valid_nome(nome):
        flash("Informe nome e sobrenome reais.", "error")
        return redirect(url_for("consumidor_criar_conta", next=next_url))
    if not _valid_phone(telefone):
        flash("Informe um WhatsApp válido com DDD.", "error")
        return redirect(url_for("consumidor_criar_conta", next=next_url))
    if not _valid_email(email):
        flash("Informe um e-mail válido.", "error")
        return redirect(url_for("consumidor_criar_conta", next=next_url))
    if not _valid_endereco_completo(endereco, endereco_lat, endereco_lng):
        flash("Informe um endereço completo e selecione uma opção encontrada: rua, número, bairro, cidade, UF e CEP.", "error")
        return redirect(url_for("consumidor_criar_conta", next=next_url))
    if len(senha) < 6 or senha.isdigit() or len(set(senha)) < 4:
        flash("Crie uma senha com pelo menos 6 caracteres e variedade.", "error")
        return redirect(url_for("consumidor_criar_conta", next=next_url))

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO ecommerce_consumidores (nome, telefone, email, senha_hash, endereco, endereco_lat, endereco_lng)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, nome, telefone, email, endereco, endereco_lat, endereco_lng
            """,
            (nome, telefone, email, generate_password_hash(senha), endereco or None, endereco_lat, endereco_lng),
        )
        user = cur.fetchone()
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash("Este e-mail já tem uma conta. Faça login para continuar.", "error")
        return redirect(url_for("consumidor_login", next=next_url))
    finally:
        cur.close()

    session.permanent = bool(request.form.get("lembrar"))
    session["consumidor_id"] = str(user["id"])
    session["consumidor_nome"] = user["nome"]
    session["consumidor_email"] = user["email"]
    session["consumidor_telefone"] = user["telefone"]
    session["consumidor_endereco"] = user.get("endereco") or ""
    session["consumidor_lat"] = user.get("endereco_lat")
    session["consumidor_lng"] = user.get("endereco_lng")
    session["email_verificado"] = False

    _enviar_email_verificacao(str(user["id"]), user["email"])
    return redirect(next_url)


@app.get("/sair")
def consumidor_logout():
    for key in ("consumidor_id", "consumidor_nome", "consumidor_email", "consumidor_telefone", "consumidor_endereco", "consumidor_lat", "consumidor_lng"):
        session.pop(key, None)
    return redirect(url_for("index"))


@app.get("/meus-pedidos")
@_consumer_required
def meus_pedidos():
    _ensure_consumidor_schema()
    _ensure_reclamacao_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.id, p.cnpjloja, p.forma_pagamento, p.status, p.total, p.criado_em,
               u.razao
        FROM ecommerce_pedidos p
        JOIN users u ON u.cnpjloja = p.cnpjloja
        WHERE p.consumidor_id = %s
        ORDER BY p.criado_em DESC
        LIMIT 100
        """,
        (session["consumidor_id"],),
    )
    pedidos = cur.fetchall()
    # Mapa pedido_id → reclamação ativa
    rec_map = {}
    if pedidos:
        ids = tuple(str(p["id"]) for p in pedidos)
        placeholders = ",".join(["%s"] * len(ids))
        cur.execute(
            f"SELECT pedido_id, id, status FROM ecommerce_reclamacoes WHERE pedido_id IN ({placeholders}) AND consumidor_id=%s ORDER BY aberta_em DESC",
            (*ids, session["consumidor_id"]),
        )
        for row in cur.fetchall():
            pid = str(row["pedido_id"])
            if pid not in rec_map:
                rec_map[pid] = dict(row)
    cur.close()
    return render_template("meus_pedidos.html", pedidos=pedidos, rec_map=rec_map, motivos=_MOTIVOS_RECLAMACAO)


@app.get("/meus-pedidos/<pedido_id>")
@_consumer_required
def meu_pedido_detalhe(pedido_id):
    _ensure_receita_schema()
    _ensure_reclamacao_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.*, u.razao, u.telefone,
               c.whatsapp_pedidos, c.pix_chave, c.pix_nome
        FROM ecommerce_pedidos p
        JOIN users u ON u.cnpjloja = p.cnpjloja
        LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
        WHERE p.id = %s AND p.consumidor_id = %s
        LIMIT 1
        """,
        (pedido_id, session["consumidor_id"]),
    )
    pedido = cur.fetchone()
    if not pedido:
        flash("Pedido não encontrado.", "error")
        return redirect(url_for("meus_pedidos"))
    cur.execute("SELECT * FROM ecommerce_pedido_itens WHERE pedido_id=%s ORDER BY id", (pedido_id,))
    itens = [dict(i) for i in cur.fetchall()]
    # Reclamação ativa (se houver)
    cur.execute(
        "SELECT id, status FROM ecommerce_reclamacoes WHERE pedido_id=%s AND consumidor_id=%s ORDER BY aberta_em DESC LIMIT 1",
        (pedido_id, session["consumidor_id"]),
    )
    reclamacao = cur.fetchone()
    cur.close()
    return render_template(
        "meu_pedido_detalhe.html",
        pedido=dict(pedido),
        itens=itens,
        reclamacao=dict(reclamacao) if reclamacao else None,
        motivos=_MOTIVOS_RECLAMACAO,
    )


# ─── RECLAMAÇÕES: CONSUMIDOR ─────────────────────────────────────────────────

_MOTIVOS_RECLAMACAO = {
    "produto_vencido":  "Produto vencido",
    "produto_errado":   "Produto errado / diferente do pedido",
    "nao_entregue":     "Produto não entregue",
    "entrega_danificada": "Produto chegou danificado",
    "outro":            "Outro motivo",
}

_STATUS_RECLAMACAO_LABEL = {
    "aberta":             "Aberta — aguardando resposta da farmácia",
    "em_andamento":       "Em andamento",
    "aguardando_cliente": "Aguardando sua confirmação",
    "finalizada":         "Finalizada",
}


@app.get("/minhas-reclamacoes")
@_consumer_required
def minhas_reclamacoes():
    _ensure_reclamacao_schema()
    consumidor_id = session["consumidor_id"]
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.id, r.motivo, r.status, r.aberta_em, r.finalizada_em,
               r.prazo_loja_responder, r.prazo_cliente_confirmar,
               u.razao,
               p.id AS pedido_id
        FROM ecommerce_reclamacoes r
        JOIN users u ON u.cnpjloja = r.cnpjloja
        JOIN ecommerce_pedidos p ON p.id = r.pedido_id
        WHERE r.consumidor_id = %s
        ORDER BY r.aberta_em DESC
        LIMIT 100
        """,
        (consumidor_id,),
    )
    reclamacoes = cur.fetchall()
    cur.close()
    return render_template(
        "minhas_reclamacoes.html",
        reclamacoes=reclamacoes,
        motivos=_MOTIVOS_RECLAMACAO,
        status_label=_STATUS_RECLAMACAO_LABEL,
    )


@app.post("/meus-pedidos/<pedido_id>/reclamacao")
@_consumer_required
def abrir_reclamacao(pedido_id):
    _ensure_reclamacao_schema()
    consumidor_id = session["consumidor_id"]
    conn = db()
    cur = conn.cursor()

    # Garante que o pedido pertence ao consumidor e está em status adequado
    cur.execute(
        "SELECT id, cnpjloja, status FROM ecommerce_pedidos WHERE id=%s AND consumidor_id=%s LIMIT 1",
        (pedido_id, consumidor_id),
    )
    pedido = cur.fetchone()
    if not pedido:
        flash("Pedido não encontrado.", "error")
        cur.close()
        return redirect(url_for("meus_pedidos"))
    if pedido["status"] not in ("pago", "enviado", "entregue"):
        flash("Só é possível abrir reclamação em pedidos pagos, enviados ou entregues.", "error")
        cur.close()
        return redirect(url_for("meu_pedido_detalhe", pedido_id=pedido_id))

    # Verifica se já existe reclamação aberta para este pedido
    cur.execute(
        "SELECT id FROM ecommerce_reclamacoes WHERE pedido_id=%s AND status NOT IN ('finalizada') LIMIT 1",
        (pedido_id,),
    )
    existente = cur.fetchone()
    if existente:
        cur.close()
        return redirect(url_for("minha_reclamacao", reclamacao_id=existente["id"]))

    motivo    = (request.form.get("motivo") or "").strip()
    descricao = (request.form.get("descricao") or "").strip()

    if motivo not in _MOTIVOS_RECLAMACAO:
        flash("Selecione um motivo válido.", "error")
        cur.close()
        return redirect(url_for("meu_pedido_detalhe", pedido_id=pedido_id))
    if len(descricao) < 20:
        flash("Descreva o problema com pelo menos 20 caracteres.", "error")
        cur.close()
        return redirect(url_for("meu_pedido_detalhe", pedido_id=pedido_id))

    prazo_loja = datetime.now(timezone.utc) + timedelta(hours=48)
    cur.execute(
        """
        INSERT INTO ecommerce_reclamacoes
            (pedido_id, cnpjloja, consumidor_id, motivo, descricao, status, prazo_loja_responder)
        VALUES (%s, %s, %s, %s, %s, 'aberta', %s)
        RETURNING id
        """,
        (pedido_id, pedido["cnpjloja"], consumidor_id, motivo, descricao, prazo_loja),
    )
    rec_id = cur.fetchone()["id"]
    # Mensagem inicial automática com a descrição
    cur.execute(
        "INSERT INTO ecommerce_reclamacao_msgs (reclamacao_id, autor, mensagem) VALUES (%s, 'cliente', %s)",
        (rec_id, descricao),
    )
    conn.commit()
    cur.close()
    flash("Reclamação aberta. A farmácia tem 48 horas para responder.", "success")
    return redirect(url_for("minha_reclamacao", reclamacao_id=rec_id))


@app.get("/minha-reclamacao/<reclamacao_id>")
@_consumer_required
def minha_reclamacao(reclamacao_id):
    _ensure_reclamacao_schema()
    consumidor_id = session["consumidor_id"]
    _processar_prazos_reclamacao(reclamacao_id)
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.*, u.razao
        FROM ecommerce_reclamacoes r
        JOIN users u ON u.cnpjloja = r.cnpjloja
        WHERE r.id=%s AND r.consumidor_id=%s
        LIMIT 1
        """,
        (reclamacao_id, consumidor_id),
    )
    rec = cur.fetchone()
    if not rec:
        flash("Reclamação não encontrada.", "error")
        cur.close()
        return redirect(url_for("meus_pedidos"))
    cur.execute(
        "SELECT * FROM ecommerce_reclamacao_msgs WHERE reclamacao_id=%s ORDER BY enviada_em",
        (reclamacao_id,),
    )
    msgs = cur.fetchall()
    cur.close()
    return render_template(
        "minha_reclamacao.html",
        rec=dict(rec),
        msgs=msgs,
        motivos=_MOTIVOS_RECLAMACAO,
        status_label=_STATUS_RECLAMACAO_LABEL,
    )


@app.post("/minha-reclamacao/<reclamacao_id>/mensagem")
@_consumer_required
def reclamacao_cliente_mensagem(reclamacao_id):
    _ensure_reclamacao_schema()
    consumidor_id = session["consumidor_id"]
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, status FROM ecommerce_reclamacoes WHERE id=%s AND consumidor_id=%s LIMIT 1",
        (reclamacao_id, consumidor_id),
    )
    rec = cur.fetchone()
    if not rec or rec["status"] == "finalizada":
        cur.close()
        return redirect(url_for("meus_pedidos"))

    mensagem = (request.form.get("mensagem") or "").strip()
    if len(mensagem) < 2:
        flash("Digite uma mensagem.", "error")
        cur.close()
        return redirect(url_for("minha_reclamacao", reclamacao_id=reclamacao_id))

    cur.execute(
        "INSERT INTO ecommerce_reclamacao_msgs (reclamacao_id, autor, mensagem) VALUES (%s, 'cliente', %s)",
        (reclamacao_id, mensagem),
    )
    # Se estava aguardando cliente, volta para em_andamento
    if rec["status"] == "aguardando_cliente":
        cur.execute(
            "UPDATE ecommerce_reclamacoes SET status='em_andamento', prazo_cliente_confirmar=NULL WHERE id=%s",
            (reclamacao_id,),
        )
    conn.commit()
    cur.close()
    return redirect(url_for("minha_reclamacao", reclamacao_id=reclamacao_id))


@app.post("/minha-reclamacao/<reclamacao_id>/confirmar-resolucao")
@_consumer_required
def reclamacao_confirmar_resolucao(reclamacao_id):
    _ensure_reclamacao_schema()
    consumidor_id = session["consumidor_id"]
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, status FROM ecommerce_reclamacoes WHERE id=%s AND consumidor_id=%s LIMIT 1",
        (reclamacao_id, consumidor_id),
    )
    rec = cur.fetchone()
    if not rec or rec["status"] != "aguardando_cliente":
        flash("Esta ação não está disponível para esta reclamação.", "error")
        cur.close()
        return redirect(url_for("minha_reclamacao", reclamacao_id=reclamacao_id))

    cur.execute(
        "UPDATE ecommerce_reclamacoes SET status='finalizada', finalizada_em=NOW() WHERE id=%s",
        (reclamacao_id,),
    )
    cur.execute(
        "INSERT INTO ecommerce_reclamacao_msgs (reclamacao_id, autor, mensagem) VALUES (%s, 'cliente', %s)",
        (reclamacao_id, "✅ Cliente confirmou que o problema foi resolvido. Reclamação finalizada."),
    )
    conn.commit()
    cur.close()
    flash("Reclamação finalizada. Obrigado pelo retorno!", "success")
    return redirect(url_for("meus_pedidos"))


@app.get("/perfil")
@_consumer_required
def consumidor_perfil():
    _ensure_consumidor_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT nome, telefone, email, endereco, endereco_lat, endereco_lng FROM ecommerce_consumidores WHERE id=%s LIMIT 1", (session["consumidor_id"],))
    user = cur.fetchone()
    cur.close()
    return render_template("consumidor_perfil.html", user=user)


@app.post("/perfil")
@_consumer_required
def consumidor_perfil_post():
    _ensure_consumidor_schema()
    nome = (request.form.get("nome") or "").strip()
    telefone = (request.form.get("telefone") or "").strip()
    email = _norm_email(request.form.get("email"))
    senha = request.form.get("senha") or ""
    endereco = (request.form.get("endereco") or "").strip()
    endereco_lat = _to_float_or_none(request.form.get("endereco_lat"))
    endereco_lng = _to_float_or_none(request.form.get("endereco_lng"))

    if not _valid_nome(nome):
        flash("Informe nome e sobrenome reais.", "error")
        return redirect(url_for("consumidor_perfil"))
    if not _valid_phone(telefone):
        flash("Informe um WhatsApp válido com DDD.", "error")
        return redirect(url_for("consumidor_perfil"))
    if not _valid_email(email):
        flash("Informe um e-mail válido.", "error")
        return redirect(url_for("consumidor_perfil"))
    if not _valid_endereco_completo(endereco, endereco_lat, endereco_lng):
        flash("Informe um endereço completo e selecione uma opção encontrada: rua, número, bairro, cidade, UF e CEP.", "error")
        return redirect(url_for("consumidor_perfil"))
    if senha and (len(senha) < 6 or senha.isdigit() or len(set(senha)) < 4):
        flash("A nova senha precisa ter pelo menos 6 caracteres e variedade.", "error")
        return redirect(url_for("consumidor_perfil"))

    conn = db()
    cur = conn.cursor()
    try:
        if senha:
            cur.execute(
                """
                UPDATE ecommerce_consumidores
                SET nome=%s, telefone=%s, email=%s, senha_hash=%s, endereco=%s, endereco_lat=%s, endereco_lng=%s, atualizado_em=NOW()
                WHERE id=%s
                """,
                (nome, telefone, email, generate_password_hash(senha), endereco or None, endereco_lat, endereco_lng, session["consumidor_id"]),
            )
        else:
            cur.execute(
                """
                UPDATE ecommerce_consumidores
                SET nome=%s, telefone=%s, email=%s, endereco=%s, endereco_lat=%s, endereco_lng=%s, atualizado_em=NOW()
                WHERE id=%s
                """,
                (nome, telefone, email, endereco or None, endereco_lat, endereco_lng, session["consumidor_id"]),
            )
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash("Este e-mail já está em uso.", "error")
        return redirect(url_for("consumidor_perfil"))
    finally:
        cur.close()

    session["consumidor_nome"] = nome
    session["consumidor_email"] = email
    session["consumidor_telefone"] = telefone
    session["consumidor_endereco"] = endereco
    session["consumidor_lat"] = endereco_lat
    session["consumidor_lng"] = endereco_lng
    flash("Perfil atualizado.", "success")
    return redirect(url_for("consumidor_perfil"))


@app.post("/api/receita/upload")
def api_receita_upload():
    """Upload de uma receita médica (PDF) via Cloudinary. Retorna a URL segura."""
    if not session.get("consumidor_id"):
        return jsonify({"error": "Login necessário."}), 401
    files = request.files.getlist("receita")
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify({"error": "Arquivo não enviado."}), 400
    # Aceita somente o primeiro arquivo neste endpoint (um por chamada)
    f = files[0]
    raw = f.read()
    if len(raw) > 12 * 1024 * 1024:
        return jsonify({"error": "Arquivo muito grande (máximo 12 MB)."}), 400
    ext = (f.filename.rsplit(".", 1)[-1].lower()) if "." in f.filename else ""
    if ext != "pdf":
        return jsonify({"error": "Envie apenas PDF original da receita digital. Foto, print ou receita escaneada não são aceitos."}), 400
    import uuid as _uuid
    safe_name = f"receita_{session['consumidor_id']}_{_uuid.uuid4().hex[:12]}.pdf"
    url = _upload_receita_cloudinary(raw, safe_name)
    if not url:
        return jsonify({"error": "Erro ao fazer upload. Tente novamente."}), 500
    return jsonify({"url": url})


@app.post("/api/checkout")
def api_checkout():
    if not session.get("consumidor_id"):
        return jsonify({"error": "Faça login ou crie sua conta para enviar o pedido.", "login_required": True}), 401
    _ensure_consumidor_schema()
    _ensure_payment_schema()
    _ensure_receita_schema()
    data     = request.get_json(force=True) or {}
    fp_list  = data.get("farmaciasPedidos", [])

    if not fp_list:
        return jsonify({"error": "Carrinho vazio."}), 400

    cliente = {
        "nome": session.get("consumidor_nome") or "",
        "telefone": session.get("consumidor_telefone") or "",
        "email": session.get("consumidor_email") or "",
        "endereco": session.get("consumidor_endereco") or "",
        "lat": session.get("consumidor_lat"),
        "lng": session.get("consumidor_lng"),
    }

    conn = db()
    cur  = conn.cursor()

    # Garante que o email está presente (sessões antigas podem não ter o campo)
    if not cliente["email"] or "@" not in cliente["email"]:
        cur.execute(
            "SELECT nome, email, telefone, endereco, endereco_lat, endereco_lng FROM ecommerce_consumidores WHERE id=%s LIMIT 1",
            (session["consumidor_id"],),
        )
        _c = cur.fetchone()
        if _c:
            cliente["email"]    = _c["email"] or ""
            cliente["nome"]     = cliente["nome"] or _c["nome"] or ""
            cliente["telefone"] = cliente["telefone"] or _c["telefone"] or ""
            cliente["endereco"] = cliente["endereco"] or _c.get("endereco") or ""
            cliente["lat"] = cliente["lat"] or _c.get("endereco_lat")
            cliente["lng"] = cliente["lng"] or _c.get("endereco_lng")
    pedidos_result = []
    _itens_por_loja: dict = {}

    for fp in fp_list:
        cnpjloja   = (fp.get("cnpjloja") or "").strip()
        pagamento  = (fp.get("pagamento") or "whatsapp").strip()
        itens      = fp.get("itens", [])
        _itens_por_loja[cnpjloja] = itens
        _receita_urls = [u for u in (fp.get("receita_urls") or []) if u and isinstance(u, str)]
        receita_url = json.dumps(_receita_urls) if _receita_urls else None
        receita_declaracao_ok = fp.get("receita_declaracao_digital_valida") is True
        if not cnpjloja or not itens:
            continue

        cur.execute(
            """
            SELECT u.razao, u.telefone,
                   c.whatsapp_pedidos, c.whatsapp_receita, c.pix_chave, c.pix_nome, c.mp_access_token,
                   COALESCE(c.aceita_entrega, FALSE) AS aceita_entrega,
                   COALESCE(c.raio_entrega_km, 0) AS raio_entrega_km,
                   COALESCE(c.cobra_frete, FALSE) AS cobra_frete,
                   COALESCE(c.valor_frete, 0) AS valor_frete,
                   COALESCE(c.pedido_minimo_entrega, 0) AS pedido_minimo_entrega,
                   g.lat AS loja_lat, g.lng AS loja_lng
            FROM users u
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
            LEFT JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
            WHERE u.cnpjloja = %s LIMIT 1
            """,
            (cnpjloja,),
        )
        loja = cur.fetchone()
        if not loja:
            continue

        produtos_total = sum(float(i.get("preco", 0)) * int(i.get("qty", 1)) for i in itens)
        tipo_entrega = (fp.get("tipo_entrega") or "retirada").strip()
        entrega_lat = _to_float_or_none(fp.get("entrega_lat")) or _to_float_or_none(cliente.get("lat"))
        entrega_lng = _to_float_or_none(fp.get("entrega_lng")) or _to_float_or_none(cliente.get("lng"))
        endereco_entrega = (fp.get("endereco_entrega") or cliente.get("endereco") or "").strip()
        entrega_dist = None
        if entrega_lat is not None and entrega_lng is not None and loja.get("loja_lat") is not None and loja.get("loja_lng") is not None:
            entrega_dist = round(haversine(entrega_lat, entrega_lng, float(loja["loja_lat"]), float(loja["loja_lng"])), 1)
        entrega_ok = bool(loja.get("aceita_entrega")) and entrega_dist is not None and entrega_dist <= float(loja.get("raio_entrega_km") or 0)
        if tipo_entrega == "entrega" and not entrega_ok:
            tipo_entrega = "retirada"
        # Validate minimum order for delivery
        if tipo_entrega == "entrega":
            pedido_minimo = float(loja.get("pedido_minimo_entrega") or 0)
            if pedido_minimo > 0 and produtos_total < pedido_minimo:
                return jsonify({
                    "error": f"Pedido mínimo para entrega é R$ {pedido_minimo:.2f}. Adicione mais itens ou escolha retirada.",
                    "pedido_minimo_entrega": pedido_minimo,
                }), 400
        frete_valor = float(loja.get("valor_frete") or 0) if tipo_entrega == "entrega" and loja.get("cobra_frete") else 0.0
        total = produtos_total + frete_valor

        # Apply coupon if provided
        _ensure_cupons_schema()
        cupom_id_aplicado = None
        desconto_cupom = 0.0
        cupom_codigo = (fp.get("cupom_codigo") or "").strip().upper()
        if cupom_codigo:
            consumidor_id_checkout = session.get("consumidor_id")
            cur.execute(
                """
                SELECT * FROM ecommerce_cupons c
                WHERE c.cnpjloja=%(cnpjloja)s AND upper(c.codigo)=%(codigo)s AND c.ativo=TRUE
                  AND (c.valido_ate IS NULL OR c.valido_ate >= CURRENT_DATE)
                  AND (c.uso_maximo = 0 OR c.usos_count < c.uso_maximo)
                  AND (
                    c.publico = 'todos'
                    OR (c.publico = 'especifico' AND EXISTS (
                        SELECT 1 FROM ecommerce_cupons_clientes cc
                        WHERE cc.cupom_id = c.id AND cc.consumidor_id = %(consumidor_id)s
                    ))
                    OR (c.publico = 'primeira_compra' AND NOT EXISTS (
                        SELECT 1 FROM ecommerce_pedidos prev
                        WHERE prev.cnpjloja = c.cnpjloja AND prev.consumidor_id = %(consumidor_id)s
                        AND prev.status NOT IN ('cancelado')
                    ))
                    OR (c.publico = 'frequente' AND (
                        SELECT COUNT(*) FROM ecommerce_pedidos prev
                        WHERE prev.cnpjloja = c.cnpjloja AND prev.consumidor_id = %(consumidor_id)s
                        AND prev.status NOT IN ('cancelado')
                    ) >= c.min_compras)
                  )
                LIMIT 1
                """,
                {"cnpjloja": cnpjloja, "codigo": cupom_codigo, "consumidor_id": consumidor_id_checkout},
            )
            cupom = cur.fetchone()
            if cupom:
                escopo_c = (cupom.get("escopo") or "todos")
                escopo_cats = set(x.strip() for x in (cupom.get("escopo_categorias") or "").split(",") if x.strip())
                escopo_eans_set = set(x.strip() for x in (cupom.get("escopo_eans") or "").split(",") if x.strip())
                if escopo_c == "categoria" and escopo_cats:
                    applicable_total = sum(
                        float(item.get("preco", 0)) * int(item.get("qty", 1))
                        for item in itens
                        if _classificar_produto(item.get("nome", "")) in escopo_cats
                    )
                elif escopo_c == "produto" and escopo_eans_set:
                    applicable_total = sum(
                        float(item.get("preco", 0)) * int(item.get("qty", 1))
                        for item in itens
                        if (item.get("ean") or "").strip() in escopo_eans_set
                    )
                else:
                    applicable_total = produtos_total
                if cupom["desconto_tipo"] == "pct":
                    desconto_cupom = round(applicable_total * float(cupom["desconto_valor"]) / 100, 2)
                else:
                    desconto_cupom = min(float(cupom["desconto_valor"]), applicable_total)
                cupom_id_aplicado = str(cupom["id"])

        # Desconto automático por quantidade comprada
        total_itens_qty = sum(int(i.get("qty", 1)) for i in itens)
        cur.execute("""
            SELECT id, desconto_tipo, desconto_valor, escopo,
                   COALESCE(escopo_categorias,'') AS escopo_categorias,
                   COALESCE(escopo_eans,'') AS escopo_eans,
                   COALESCE(qtd_minima,0) AS qtd_minima
            FROM ecommerce_cupons
            WHERE cnpjloja=%s AND ativo=TRUE
              AND COALESCE(tipo_regra,'codigo')='quantidade'
              AND qtd_minima > 0 AND %s >= qtd_minima
              AND (valido_ate IS NULL OR valido_ate >= CURRENT_DATE)
              AND (uso_maximo = 0 OR usos_count < uso_maximo)
            ORDER BY qtd_minima DESC, desconto_valor DESC
        """, (cnpjloja, total_itens_qty))
        desconto_qtd = 0.0
        cupom_qtd_id = None
        for qr in cur.fetchall():
            escopo_q = qr.get("escopo") or "todos"
            eans_q = set(x.strip() for x in (qr.get("escopo_eans") or "").split(",") if x.strip())
            cats_q = set(x.strip() for x in (qr.get("escopo_categorias") or "").split(",") if x.strip())
            if escopo_q == "categoria" and cats_q:
                app_q = sum(float(i.get("preco", 0)) * int(i.get("qty", 1)) for i in itens if _classificar_produto(i.get("nome", "")) in cats_q)
            elif escopo_q == "produto" and eans_q:
                app_q = sum(float(i.get("preco", 0)) * int(i.get("qty", 1)) for i in itens if (i.get("ean") or "").strip() in eans_q)
            else:
                app_q = produtos_total
            calc_q = round(app_q * float(qr["desconto_valor"]) / 100, 2) if qr["desconto_tipo"] == "pct" else min(float(qr["desconto_valor"]), app_q)
            if calc_q > desconto_qtd:
                desconto_qtd = calc_q
                cupom_qtd_id = str(qr["id"])
        # Melhor desconto base: código ou quantidade
        if desconto_qtd > desconto_cupom:
            desconto_cupom = desconto_qtd
            cupom_id_aplicado = cupom_qtd_id

        # Desconto automático por forma de pagamento (sempre adicional ao base)
        desconto_pag = 0.0
        cupom_pag_id = None
        cur.execute("""
            SELECT id, desconto_tipo, desconto_valor, escopo,
                   COALESCE(escopo_categorias,'') AS escopo_categorias,
                   COALESCE(escopo_eans,'') AS escopo_eans
            FROM ecommerce_cupons
            WHERE cnpjloja=%s AND ativo=TRUE
              AND COALESCE(tipo_regra,'codigo')='pagamento'
              AND (forma_pagamento = %s OR forma_pagamento = 'todos')
              AND (valido_ate IS NULL OR valido_ate >= CURRENT_DATE)
              AND (uso_maximo = 0 OR usos_count < uso_maximo)
            ORDER BY desconto_valor DESC LIMIT 1
        """, (cnpjloja, pagamento))
        pag_rule = cur.fetchone()
        if pag_rule:
            escopo_p = pag_rule.get("escopo") or "todos"
            eans_p = set(x.strip() for x in (pag_rule.get("escopo_eans") or "").split(",") if x.strip())
            cats_p = set(x.strip() for x in (pag_rule.get("escopo_categorias") or "").split(",") if x.strip())
            if escopo_p == "categoria" and cats_p:
                app_p = sum(float(i.get("preco", 0)) * int(i.get("qty", 1)) for i in itens if _classificar_produto(i.get("nome", "")) in cats_p)
            elif escopo_p == "produto" and eans_p:
                app_p = sum(float(i.get("preco", 0)) * int(i.get("qty", 1)) for i in itens if (i.get("ean") or "").strip() in eans_p)
            else:
                app_p = produtos_total
            desconto_pag = round(app_p * float(pag_rule["desconto_valor"]) / 100, 2) if pag_rule["desconto_tipo"] == "pct" else min(float(pag_rule["desconto_valor"]), app_p)
            cupom_pag_id = str(pag_rule["id"])

        desconto_total_aplicado = desconto_cupom + desconto_pag
        total = max(0, total - desconto_total_aplicado)

        # Tarja + entrega: aceita somente receita digital original declarada pelo cliente.
        tem_tarja_entrega = tipo_entrega == "entrega" and any(i.get("requer_receita") for i in itens)
        if tem_tarja_entrega and not receita_url:
            return jsonify({
                "error": "Carrinho contém medicamentos com tarja que exigem receita médica. Envie o PDF original da receita digital para finalizar com entrega.",
            }), 400
        if tem_tarja_entrega and not receita_declaracao_ok:
            return jsonify({
                "error": "Confirme que a receita enviada é digital original, com assinatura eletrônica verificável, e não foto, print ou receita escaneada.",
            }), 400
        # Checagem de extensão: receita_url é JSON array; valida cada URL individualmente
        if tem_tarja_entrega and receita_url:
            try:
                _urls_check = json.loads(receita_url) if receita_url.startswith("[") else [receita_url]
            except Exception:
                _urls_check = [receita_url]
            if any(not u.lower().split("?", 1)[0].endswith(".pdf") for u in _urls_check if u):
                return jsonify({
                    "error": "Para entrega com receita, o sistema aceita apenas PDF original da receita digital assinada.",
                }), 400

        receita_status = "pendente" if (tem_tarja_entrega and receita_url) else None
        codigo_entrega = _novo_codigo_entrega() if tipo_entrega == "entrega" else None

        cur.execute(
            """
            INSERT INTO ecommerce_pedidos
              (cnpjloja, consumidor_id, cliente_nome, cliente_telefone, cliente_email, forma_pagamento, total,
               tipo_entrega, endereco_entrega, entrega_lat, entrega_lng, entrega_distancia_km, codigo_entrega, frete_valor,
               cupom_id, desconto_cupom, receita_url, receita_status, receita_declaracao_digital_valida)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                cnpjloja,
                session.get("consumidor_id"),
                (cliente.get("nome") or "").strip(),
                (cliente.get("telefone") or "").strip(),
                (cliente.get("email")    or "").strip(),
                pagamento,
                round(total, 2),
                tipo_entrega,
                endereco_entrega or None,
                entrega_lat,
                entrega_lng,
                entrega_dist,
                codigo_entrega,
                frete_valor,
                cupom_id_aplicado,
                round(desconto_total_aplicado, 2),
                receita_url,
                receita_status,
                bool(receita_declaracao_ok),
            ),
        )
        pedido_id = str(cur.fetchone()["id"])

        for item in itens:
            cur.execute(
                """
                INSERT INTO ecommerce_pedido_itens (pedido_id, ean, nome, qty, preco_unitario, imagem)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    pedido_id,
                    item.get("ean", ""),
                    item.get("nome", "Produto"),
                    int(item.get("qty", 1)),
                    float(item.get("preco", 0)),
                    item.get("imagem") or None,
                ),
            )

        # Incrementa contagem de uso para cada regra aplicada
        if cupom_id_aplicado:
            cur.execute(
                "UPDATE ecommerce_cupons SET usos_count = usos_count + 1 WHERE id=%s",
                (cupom_id_aplicado,),
            )
        if cupom_pag_id:
            cur.execute(
                "UPDATE ecommerce_cupons SET usos_count = usos_count + 1 WHERE id=%s",
                (cupom_pag_id,),
            )

        conn.commit()

        mp_init = None
        mp_payment = {}
        payment_status = "pending"
        pix_erro = None
        mp_erro = None
        if receita_status == "pendente":
            pass  # pagamento criado apenas após aprovação da receita
        elif pagamento == "mercadopago" and loja.get("mp_access_token"):
            mp_pref = _criar_preferencia_mp(
                loja["mp_access_token"], pedido_id, itens, total, cliente
            )
            mp_erro = (mp_pref or {}).get("_erro")
            if mp_pref and not mp_erro:
                mp_init = mp_pref.get("init_point")
                cur.execute(
                    """
                    UPDATE ecommerce_pedidos
                    SET mp_preference_id=%s, mp_init_point=%s, pagamento_status=%s
                    WHERE id=%s
                    """,
                    (mp_pref.get("id"), mp_init, payment_status, pedido_id),
                )
                conn.commit()
        elif pagamento == "pix" and loja.get("mp_access_token"):
            mp_payment = _criar_pagamento_pix_mp(
                loja["mp_access_token"], pedido_id, itens, total, cliente
            ) or {}
            pix_erro = mp_payment.pop("_erro", None)
            if mp_payment and not pix_erro:
                payment_status = mp_payment.get("status") or "pending"
                pedido_status = _status_pedido_por_pagamento(payment_status)
                cur.execute(
                    """
                    UPDATE ecommerce_pedidos
                    SET mp_payment_id=%s,
                        pix_qr_code=%s,
                        pix_qr_base64=%s,
                        pagamento_status=%s,
                        pagamento_status_detail=%s,
                        status=%s,
                        pagamento_confirmado_em=CASE WHEN %s='pago' THEN NOW() ELSE pagamento_confirmado_em END
                    WHERE id=%s
                    """,
                    (
                        str(mp_payment.get("id") or ""),
                        mp_payment.get("qr_code"),
                        mp_payment.get("qr_code_base64"),
                        payment_status,
                        mp_payment.get("status_detail"),
                        pedido_status,
                        pedido_status,
                        pedido_id,
                    ),
                )
                conn.commit()

        # Monta URL de notificação WhatsApp para receita pendente
        _wpp_receita_url = None
        if receita_status == "pendente":
            import re as _re
            _wpp_num = _re.sub(r"\D", "", loja.get("whatsapp_receita") or loja.get("whatsapp_pedidos") or loja.get("telefone") or "")
            if _wpp_num:
                _msg = (
                    f"Olá! Enviei minha receita médica para o pedido #{pedido_id[:8].upper()} "
                    f"pela Poupaqui. Aguardo a avaliação para confirmar a entrega. Obrigado(a)!"
                )
                _wpp_receita_url = f"https://wa.me/55{_wpp_num}?text={urllib.parse.quote(_msg)}"

        pedidos_result.append({
            "id":           pedido_id,
            "cnpjloja":     cnpjloja,
            "razao":        loja["razao"],
            "total":        round(total, 2),
            "pagamento":    pagamento,
            "tipo_entrega": tipo_entrega,
            "entrega_distancia_km": entrega_dist,
            "frete_valor": frete_valor,
            "desconto_cupom": round(desconto_total_aplicado, 2),
            "pix_chave":    loja["pix_chave"] or "",
            "pix_nome":     loja["pix_nome"]  or "",
            "mp_init_point":mp_init,
            "mp_erro":      mp_erro,
            "pix_qr_code":  mp_payment.get("qr_code", ""),
            "pix_qr_base64":mp_payment.get("qr_code_base64", ""),
            "pagamento_status": payment_status,
            "pix_erro":     pix_erro,
            "whatsapp_tel": loja["whatsapp_pedidos"] or loja["telefone"] or "",
            "receita_pendente": receita_status == "pendente",
            "receita_wpp_url": _wpp_receita_url,
        })

    if pedidos_result:
        session["checkout_pedido_ids"] = [p["id"] for p in pedidos_result]
    cur.close()

    # E-mail de confirmação para o consumidor e notificação para cada loja
    _disparar_emails_novos_pedidos(pedidos_result, _itens_por_loja, cliente)

    return jsonify({"pedidos": pedidos_result})


def _ensure_payment_schema():
    _load_db_migrations()
    if "payment" in _schema_ready:
        return
    with _schema_lock:
        if "payment" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS mp_preference_id TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS mp_payment_id TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS mp_init_point TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS pix_qr_code TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS pix_qr_base64 TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS pagamento_status TEXT DEFAULT 'pending'")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS pagamento_status_detail TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS pagamento_confirmado_em TIMESTAMPTZ")
        conn.commit()
        cur.close()
        _schema_ready.add("payment")
        _mark_migration_done("payment")


def _public_base_url():
    base = (os.getenv("PUBLIC_BASE_URL") or os.getenv("APP_URL") or "").strip().rstrip("/")
    return base or None


def _mp_notification_url():
    base = _public_base_url()
    return f"{base}/api/mercadopago/webhook" if base else None


def _mp_request(access_token, path, payload=None, method=None, idempotency_key=None):
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"https://api.mercadopago.com{path}",
        data=data,
        headers=headers,
        method=method or ("POST" if data else "GET"),
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"MP {e.code} {path}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"MP URLError {path}: {e.reason}") from e


def _status_pedido_por_pagamento(status):
    status = (status or "").lower()
    if status == "approved":
        return "pago"
    if status in {"rejected", "cancelled", "refunded", "charged_back"}:
        return "cancelado"
    return "pendente"


def _nome_cliente_partes(nome):
    partes = (nome or "Cliente Poupaqui").strip().split()
    if not partes:
        return "Cliente", "Poupaqui"
    return partes[0], " ".join(partes[1:]) or "Poupaqui"


def _payer_payload(cliente):
    first, last = _nome_cliente_partes((cliente or {}).get("nome"))
    email = ((cliente or {}).get("email") or "").strip()
    if not email or "@" not in email or email.endswith(".local"):
        email = f"comprador{int(time.time())}@poupaqui.com.br"
    return {"email": email, "first_name": first, "last_name": last}


def _criar_preferencia_mp(access_token, pedido_id, itens, total, cliente):
    try:
        payload = {
            "items": [
                {
                    "title":      (i.get("nome") or "Produto")[:255],
                    "quantity":   int(i.get("qty", 1)),
                    "unit_price": float(i.get("preco", 0)),
                    "currency_id": "BRL",
                }
                for i in itens
            ],
            "external_reference": pedido_id,
            "payer": _payer_payload(cliente),
        }
        notification_url = _mp_notification_url()
        if notification_url:
            payload["notification_url"] = notification_url
        data = _mp_request(access_token, "/checkout/preferences", payload, idempotency_key=f"{pedido_id}-checkout")
        init_point = data.get("init_point") or data.get("sandbox_init_point")
        if not init_point:
            return {"_erro": "Mercado Pago nao retornou link de pagamento."}
        return {"id": data.get("id"), "init_point": init_point}
    except Exception as exc:
        import logging
        logging.error("Mercado Pago cartao falhou para pedido %s: %s", pedido_id, exc)
        return {"_erro": str(exc)}


def _criar_pagamento_pix_mp(access_token, pedido_id, itens, total, cliente):
    try:
        payload = {
            "transaction_amount": round(float(total), 2),
            "description": f"Pedido Poupaqui #{pedido_id[:8].upper()}",
            "payment_method_id": "pix",
            "external_reference": pedido_id,
            "payer": _payer_payload(cliente),
        }
        notification_url = _mp_notification_url()
        if notification_url:
            payload["notification_url"] = notification_url
        data = _mp_request(access_token, "/v1/payments", payload, idempotency_key=f"{pedido_id}-pix")
        tx = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
        return {
            "id": data.get("id"),
            "status": data.get("status"),
            "status_detail": data.get("status_detail"),
            "qr_code": tx.get("qr_code"),
            "qr_code_base64": tx.get("qr_code_base64"),
        }
    except Exception as exc:
        import logging
        logging.error("Mercado Pago PIX falhou para pedido %s: %s", pedido_id, exc)
        return {"_erro": str(exc)}


def _sincronizar_pagamento_mp_para_pedido(pedido_id, access_token=None, payment_id=None):
    try:
        _ensure_payment_schema()
        conn = db()
        cur = conn.cursor()
        if not access_token or not payment_id:
            cur.execute(
                """
                SELECT p.id, p.cnpjloja, p.mp_payment_id, c.mp_access_token
                FROM ecommerce_pedidos p
                LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
                WHERE p.id=%s LIMIT 1
                """,
                (pedido_id,),
            )
            row = cur.fetchone()
            if not row:
                cur.close()
                return None
            access_token = access_token or row.get("mp_access_token")
            payment_id = payment_id or row.get("mp_payment_id")
        if not access_token or not payment_id:
            cur.close()
            return None
        try:
            data = _mp_request(access_token, f"/v1/payments/{payment_id}", method="GET")
        except Exception:
            cur.close()
            return None
        status = data.get("status") or "pending"
        detail = data.get("status_detail")
        pedido_status = _status_pedido_por_pagamento(status)
        cur.execute(
            """
            UPDATE ecommerce_pedidos
            SET pagamento_status=%s,
                pagamento_status_detail=%s,
                status=%s,
                pagamento_confirmado_em=CASE WHEN %s='pago' THEN COALESCE(pagamento_confirmado_em, NOW()) ELSE pagamento_confirmado_em END,
                atualizado_em=NOW()
            WHERE id=%s
            RETURNING status, pagamento_status, pagamento_status_detail, pagamento_confirmado_em
            """,
            (status, detail, pedido_status, pedido_status, pedido_id),
        )
        updated = cur.fetchone()
        conn.commit()
        cur.close()
        return dict(updated) if updated else None
    except (psycopg2.InterfaceError, psycopg2.OperationalError):
        reset_db_conn()
        return None
    except Exception:
        return None


def _aplicar_webhook_pagamento(payment_id):
    _ensure_payment_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.id, c.mp_access_token
        FROM ecommerce_pedidos p
        JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
        WHERE p.mp_payment_id=%s
        LIMIT 1
        """,
        (str(payment_id),),
    )
    row = cur.fetchone()
    cur.close()
    if row:
        return _sincronizar_pagamento_mp_para_pedido(row["id"], row["mp_access_token"], str(payment_id))

    cur = conn.cursor()
    cur.execute("SELECT cnpjloja, mp_access_token FROM ecommerce_config_loja WHERE COALESCE(mp_access_token,'')<>''")
    configs = cur.fetchall()
    cur.close()
    for cfg in configs:
        try:
            data = _mp_request(cfg["mp_access_token"], f"/v1/payments/{payment_id}", method="GET")
        except Exception:
            continue
        pedido_id = data.get("external_reference")
        if not pedido_id:
            continue
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE ecommerce_pedidos
            SET mp_payment_id=%s
            WHERE id=%s AND cnpjloja=%s
            RETURNING id
            """,
            (str(payment_id), pedido_id, cfg["cnpjloja"]),
        )
        found = cur.fetchone()
        conn.commit()
        cur.close()
        if found:
            return _sincronizar_pagamento_mp_para_pedido(pedido_id, cfg["mp_access_token"], str(payment_id))
    return None


@app.route("/api/mercadopago/webhook", methods=["GET", "POST"])
def mercado_pago_webhook():
    body = request.get_json(silent=True) or {}
    event_type = request.args.get("type") or body.get("type") or body.get("topic")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    payment_id = (
        request.args.get("data.id")
        or request.args.get("id")
        or data.get("id")
        or body.get("id")
    )
    if event_type and event_type not in {"payment", "merchant_order"}:
        return jsonify({"ok": True})
    if payment_id:
        _aplicar_webhook_pagamento(str(payment_id))
    return jsonify({"ok": True})


@app.get("/api/pedido/<pedido_id>/pagamento-status")
def api_pagamento_status(pedido_id):
    try:
        _ensure_payment_schema()
        conn = db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT p.id, p.cnpjloja, p.consumidor_id, p.status, p.pagamento_status,
                   p.pagamento_status_detail, p.pagamento_confirmado_em, p.mp_payment_id,
                   c.mp_access_token
            FROM ecommerce_pedidos p
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
            WHERE p.id=%s LIMIT 1
            """,
            (pedido_id,),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Pedido nao encontrado."}), 404

        owns_as_customer = session.get("consumidor_id") and str(row.get("consumidor_id")) == str(session.get("consumidor_id"))
        owns_as_store = session.get("cnpjloja") and str(row.get("cnpjloja")) == str(session.get("cnpjloja"))
        owns_recent_checkout = str(pedido_id) in {str(x) for x in session.get("checkout_pedido_ids", [])}
        if not owns_as_customer and not owns_as_store and not owns_recent_checkout:
            return jsonify({"error": "Acesso negado."}), 403

        status = (row.get("pagamento_status") or "").lower()
        if row.get("mp_payment_id") and status not in {"approved", "rejected", "cancelled", "refunded", "charged_back"}:
            synced = _sincronizar_pagamento_mp_para_pedido(
                pedido_id, row.get("mp_access_token"), row.get("mp_payment_id")
            )
            if synced:
                row.update(synced)

        return jsonify({
            "pedido_status": row.get("status"),
            "pagamento_status": row.get("pagamento_status") or "pending",
            "pagamento_status_detail": row.get("pagamento_status_detail") or "",
            "pagamento_confirmado_em": row.get("pagamento_confirmado_em").isoformat() if row.get("pagamento_confirmado_em") else None,
        })
    except (psycopg2.InterfaceError, psycopg2.OperationalError):
        reset_db_conn()
        return jsonify({"error": "Falha temporaria ao consultar pagamento.", "retry": True}), 200
    except Exception as exc:
        import logging
        logging.exception("Falha ao consultar pagamento %s: %s", pedido_id, exc)
        return jsonify({"error": "Falha temporaria ao consultar pagamento.", "retry": True}), 200


@app.get("/pedido/confirmacao")
def pedido_confirmacao():
    _ensure_receita_schema()
    ids = [i.strip() for i in (request.args.get("ids") or "").split(",") if i.strip()]
    if not ids:
        return redirect(url_for("index"))

    conn = db()
    cur  = conn.cursor()
    pedidos = []
    for pid in ids:
        try:
            cur.execute(
                """
                SELECT p.id, p.cnpjloja, p.cliente_nome, p.forma_pagamento,
                       p.status, p.total, p.criado_em,
                       p.mp_preference_id, p.mp_payment_id, p.mp_init_point,
                       p.pix_qr_code, p.pix_qr_base64,
                       p.pagamento_status, p.pagamento_status_detail, p.pagamento_confirmado_em,
                       p.tipo_entrega, p.codigo_entrega, p.endereco_entrega,
                       p.receita_status,
                       u.razao, u.telefone,
                       c.whatsapp_pedidos, c.pix_chave, c.pix_nome, c.mp_access_token
                FROM ecommerce_pedidos p
                JOIN users u ON u.cnpjloja = p.cnpjloja
                LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
                WHERE p.id = %s LIMIT 1
                """,
                (pid,),
            )
            p = cur.fetchone()
            if p:
                if p.get("mp_payment_id") and (p.get("pagamento_status") or "").lower() not in {"approved", "rejected", "cancelled", "refunded", "charged_back"}:
                    synced = _sincronizar_pagamento_mp_para_pedido(pid, p.get("mp_access_token"), p.get("mp_payment_id"))
                    if synced:
                        p.update(synced)
                cur.execute(
                    "SELECT * FROM ecommerce_pedido_itens WHERE pedido_id=%s ORDER BY id",
                    (pid,),
                )
                itens = [dict(i) for i in cur.fetchall()]
                pedido_dict = dict(p)
                pedido_dict.pop("mp_access_token", None)
                pedidos.append({"pedido": pedido_dict, "itens": itens})
        except Exception:
            pass

    cur.close()
    return render_template("pedido_confirmacao.html", pedidos=pedidos)


# ─── PAINEL: PEDIDOS ─────────────────────────────────────────────────────────

@app.get("/painel/pedidos")
@painel_required
def painel_pedidos():
    _ensure_payment_schema()
    _ensure_receita_schema()
    _ml_flush_queue()
    cnpjloja = session.get("cnpjloja")
    sf = (request.args.get("status") or "").strip()
    sf_receita = request.args.get("receita_pendente") == "1"
    conn = db()
    cur  = conn.cursor()
    sql  = """
        SELECT id, cliente_nome, cliente_telefone, forma_pagamento,
               status, total, criado_em, pagamento_status, pagamento_status_detail,
               receita_status, tipo_entrega,
               COALESCE(origem, 'ecommerce') AS origem, ml_order_id
        FROM ecommerce_pedidos
        WHERE cnpjloja = %s
    """
    args = [cnpjloja]
    if sf_receita:
        sql += " AND receita_status = 'pendente'"
    elif sf:
        sql += " AND status = %s"
        args.append(sf)
    sql += " ORDER BY criado_em DESC LIMIT 200"
    cur.execute(sql, args)
    pedidos = cur.fetchall()
    cur.execute(
        "SELECT COUNT(*) FROM ecommerce_pedidos WHERE cnpjloja=%s AND receita_status='pendente'",
        (cnpjloja,),
    )
    receitas_pendentes = (cur.fetchone() or {}).get("count", 0) or 0
    cur.close()
    return render_template("painel_pedidos.html", pedidos=pedidos, sf=sf,
                           sf_receita=sf_receita, receitas_pendentes=receitas_pendentes)


@app.get("/painel/pedidos/<pedido_id>")
@painel_required
def painel_pedido_detalhe(pedido_id):
    _ensure_payment_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT * FROM ecommerce_pedidos WHERE id=%s AND cnpjloja=%s LIMIT 1",
        (pedido_id, cnpjloja),
    )
    pedido = cur.fetchone()
    if not pedido:
        flash("Pedido não encontrado.", "error")
        return redirect(url_for("painel_pedidos"))
    cur.execute("SELECT * FROM ecommerce_pedido_itens WHERE pedido_id=%s ORDER BY id", (pedido_id,))
    itens = [dict(i) for i in cur.fetchall()]
    cur.close()
    pedido_dict = dict(pedido)
    # Parseia receita_url que pode ser JSON array ["url1","url2"] ou URL simples (legado)
    _ru = pedido_dict.get("receita_url") or ""
    try:
        _parsed = json.loads(_ru) if _ru.startswith("[") else None
    except Exception:
        _parsed = None
    if _parsed and isinstance(_parsed, list):
        receita_urls = [u for u in _parsed if u]
    else:
        receita_urls = [_ru] if _ru else []
    return render_template("painel_pedido_detalhe.html", pedido=pedido_dict, itens=itens, receita_urls=receita_urls)


@app.post("/painel/pedidos/<pedido_id>/sincronizar-pagamento")
@painel_required
def painel_sincronizar_pagamento(pedido_id):
    cnpjloja = session.get("cnpjloja")
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM ecommerce_pedidos WHERE id=%s AND cnpjloja=%s LIMIT 1", (pedido_id, cnpjloja))
    pedido = cur.fetchone()
    cur.close()
    if not pedido:
        flash("Pedido nÃ£o encontrado.", "error")
        return redirect(url_for("painel_pedidos"))
    synced = _sincronizar_pagamento_mp_para_pedido(pedido_id)
    if synced:
        flash("Pagamento sincronizado com o Mercado Pago.", "success")
    else:
        flash("NÃ£o foi possÃ­vel sincronizar. Verifique se o pedido tem pagamento automÃ¡tico e token configurado.", "error")
    return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))


@app.post("/painel/pedidos/<pedido_id>/status")
@painel_required
def painel_pedido_status(pedido_id):
    cnpjloja   = session.get("cnpjloja")
    novo_status = (request.form.get("status") or "").strip()
    if novo_status not in {"pendente", "pago", "enviado", "entregue", "cancelado"}:
        flash("Status inválido.", "error")
        return redirect(url_for("painel_pedidos"))
    conn = db()
    cur  = conn.cursor()
    ml_order_id = None
    if novo_status == "entregue":
        cur.execute(
            "SELECT tipo_entrega, origem, ml_order_id FROM ecommerce_pedidos WHERE id=%s AND cnpjloja=%s LIMIT 1",
            (pedido_id, cnpjloja),
        )
        row = cur.fetchone()
        is_ml = row and row.get("origem") == "mercado_livre"
        if row and row.get("tipo_entrega") == "entrega" and not is_ml:
            cur.close()
            flash(
                "Para confirmar a entrega, use o formulário de confirmação informando o código do cliente.",
                "error",
            )
            return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))
        if is_ml:
            ml_order_id = row.get("ml_order_id")
    cur.execute(
        "UPDATE ecommerce_pedidos SET status=%s, entregue_em=CASE WHEN %s='entregue' THEN NOW() ELSE entregue_em END, atualizado_em=NOW() WHERE id=%s AND cnpjloja=%s",
        (novo_status, novo_status, pedido_id, cnpjloja),
    )
    conn.commit()
    cur.close()
    if novo_status == "entregue" and ml_order_id:
        _ml_feedback_entregue(ml_order_id)
    _email_status_pedido(pedido_id, novo_status)
    flash(f"Pedido marcado como {novo_status}.", "success")
    return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))


@app.post("/painel/pedidos/<pedido_id>/confirmar-entrega")
@painel_required
def painel_confirmar_entrega(pedido_id):
    _ensure_delivery_schema()
    cnpjloja = session.get("cnpjloja")
    codigo = (request.form.get("codigo_entrega") or "").strip()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT origem, ml_order_id FROM ecommerce_pedidos WHERE id=%s AND cnpjloja=%s LIMIT 1",
        (pedido_id, cnpjloja),
    )
    pedido_row = cur.fetchone()
    is_ml = pedido_row and pedido_row.get("origem") == "mercado_livre"
    if is_ml:
        cur.execute(
            """
            UPDATE ecommerce_pedidos
            SET status='entregue', entregue_em=NOW(), atualizado_em=NOW()
            WHERE id=%s AND cnpjloja=%s AND tipo_entrega='entrega'
            RETURNING id, ml_order_id
            """,
            (pedido_id, cnpjloja),
        )
    else:
        cur.execute(
            """
            UPDATE ecommerce_pedidos
            SET status='entregue', entregue_em=NOW(), atualizado_em=NOW()
            WHERE id=%s AND cnpjloja=%s AND codigo_entrega=%s AND tipo_entrega='entrega'
            RETURNING id, ml_order_id
            """,
            (pedido_id, cnpjloja, codigo),
        )
    ok = cur.fetchone()
    conn.commit()
    cur.close()
    if ok and is_ml and ok.get("ml_order_id"):
        _ml_feedback_entregue(ok["ml_order_id"])
    if ok:
        _email_status_pedido(pedido_id, "entregue")
    flash("Entrega confirmada." if ok else "Código de entrega inválido.", "success" if ok else "error")
    return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))


@app.post("/painel/pedidos/<pedido_id>/avisar-entrega-ml")
@painel_required
def painel_avisar_entrega_ml(pedido_id):
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute(
        "SELECT ml_order_id FROM ecommerce_pedidos WHERE id=%s AND cnpjloja=%s AND origem='mercado_livre' LIMIT 1",
        (pedido_id, cnpjloja),
    )
    row = cur.fetchone(); cur.close()
    if not row or not row.get("ml_order_id"):
        flash("Pedido não encontrado ou não é do Mercado Livre.", "error")
        return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))
    ok, err = _ml_feedback_entregue(row["ml_order_id"])
    if ok:
        flash("Entrega avisada no Mercado Livre com sucesso.", "success")
    else:
        flash(f"Erro ao avisar entrega no ML: {err}", "error")
    return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))


@app.post("/painel/pedidos/<pedido_id>/avaliar-receita")
@painel_required
def painel_avaliar_receita(pedido_id):
    _ensure_receita_schema()
    cnpjloja = session.get("cnpjloja")
    acao   = (request.form.get("acao")   or "").strip()
    motivo = (request.form.get("motivo") or "").strip()
    if acao not in {"aprovar", "reprovar"}:
        flash("Ação inválida.", "error")
        return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))
    novo_status = "aprovada" if acao == "aprovar" else "reprovada"
    validou_assinatura = request.form.get("validou_assinatura") == "on"
    validou_prescritor = request.form.get("validou_prescritor") == "on"
    validou_uso_unico = request.form.get("validou_uso_unico") == "on"
    validou_regras_sanitarias = request.form.get("validou_regras_sanitarias") == "on"
    if novo_status == "aprovada" and not all([
        validou_assinatura,
        validou_prescritor,
        validou_uso_unico,
        validou_regras_sanitarias,
    ]):
        flash("Para aprovar, confirme todas as validações obrigatórias da receita digital.", "error")
        return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        """
        UPDATE ecommerce_pedidos
           SET receita_status = %s,
               receita_reprovada_motivo = %s,
               receita_validou_assinatura = %s,
               receita_validou_prescritor = %s,
               receita_validou_uso_unico = %s,
               receita_validou_regras_sanitarias = %s,
               receita_avaliada_em = NOW(),
               atualizado_em = NOW()
         WHERE id = %s AND cnpjloja = %s AND receita_status = 'pendente'
         RETURNING id
        """,
        (
            novo_status,
            motivo if novo_status == "reprovada" else None,
            validou_assinatura if novo_status == "aprovada" else False,
            validou_prescritor if novo_status == "aprovada" else False,
            validou_uso_unico if novo_status == "aprovada" else False,
            validou_regras_sanitarias if novo_status == "aprovada" else False,
            pedido_id,
            cnpjloja,
        ),
    )
    ok = cur.fetchone()
    conn.commit()

    # Se aprovada, cria o pagamento PIX/MP agora
    if ok and novo_status == "aprovada":
        cur.execute(
            """
            SELECT p.forma_pagamento, p.total,
                   p.cliente_nome, p.cliente_telefone, p.cliente_email,
                   c.mp_access_token
            FROM ecommerce_pedidos p
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
            WHERE p.id = %s LIMIT 1
            """,
            (pedido_id,),
        )
        ped = cur.fetchone()
        if ped and ped.get("mp_access_token"):
            cur.execute(
                "SELECT ean, nome, qty, preco_unitario AS preco, imagem FROM ecommerce_pedido_itens WHERE pedido_id=%s ORDER BY id",
                (pedido_id,),
            )
            itens_ped = [dict(i) for i in cur.fetchall()]
            cliente_ped = {
                "nome": ped.get("cliente_nome") or "",
                "telefone": ped.get("cliente_telefone") or "",
                "email": ped.get("cliente_email") or "",
            }
            total_ped = float(ped.get("total") or 0)
            pagamento_ped = ped.get("forma_pagamento") or ""
            if pagamento_ped == "mercadopago":
                mp_pref = _criar_preferencia_mp(ped["mp_access_token"], pedido_id, itens_ped, total_ped, cliente_ped)
                if mp_pref and not mp_pref.get("_erro"):
                    cur.execute(
                        "UPDATE ecommerce_pedidos SET mp_preference_id=%s, mp_init_point=%s WHERE id=%s",
                        (mp_pref.get("id"), mp_pref.get("init_point"), pedido_id),
                    )
                    conn.commit()
            elif pagamento_ped == "pix":
                mp_pay = _criar_pagamento_pix_mp(ped["mp_access_token"], pedido_id, itens_ped, total_ped, cliente_ped) or {}
                mp_pay.pop("_erro", None)
                if mp_pay.get("qr_code"):
                    cur.execute(
                        """
                        UPDATE ecommerce_pedidos
                           SET mp_payment_id=%s, pix_qr_code=%s, pix_qr_base64=%s,
                               pagamento_status=%s, pagamento_status_detail=%s
                         WHERE id=%s
                        """,
                        (str(mp_pay.get("id") or ""), mp_pay.get("qr_code"),
                         mp_pay.get("qr_code_base64"), mp_pay.get("status") or "pending",
                         mp_pay.get("status_detail"), pedido_id),
                    )
                    conn.commit()

    cur.close()
    if ok:
        flash("Receita aprovada! Pagamento liberado para o cliente." if novo_status == "aprovada" else "Receita reprovada.", "success")
    else:
        flash("Não foi possível avaliar a receita (já avaliada ou não encontrada).", "error")
    return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))


# ─── PAINEL: RECLAMAÇÕES ─────────────────────────────────────────────────────

@app.get("/painel/reclamacoes")
@painel_required
def painel_reclamacoes():
    _ensure_reclamacao_schema()
    cnpjloja = session.get("cnpjloja")
    sf = (request.args.get("status") or "").strip()

    # Processa prazos de todas as reclamações abertas desta loja (lazy)
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id FROM ecommerce_reclamacoes
        WHERE cnpjloja=%s AND status NOT IN ('finalizada')
        """,
        (cnpjloja,),
    )
    for row in cur.fetchall():
        _processar_prazos_reclamacao(row["id"])

    query = """
        SELECT r.*, c.nome AS consumidor_nome, c.telefone AS consumidor_tel
        FROM ecommerce_reclamacoes r
        JOIN ecommerce_consumidores c ON c.id = r.consumidor_id
        WHERE r.cnpjloja=%s
    """
    params = [cnpjloja]
    if sf:
        query += " AND r.status=%s"
        params.append(sf)
    query += " ORDER BY r.aberta_em DESC LIMIT 200"
    cur.execute(query, params)
    reclamacoes = cur.fetchall()

    # Conta advertências desta loja
    cur.execute(
        "SELECT COUNT(*) AS total FROM ecommerce_loja_advertencias WHERE cnpjloja=%s",
        (cnpjloja,),
    )
    total_adv = (cur.fetchone() or {}).get("total", 0) or 0

    # Conta abertas urgentes (prazo vencendo em < 12h ou já vencido)
    cur.execute(
        """
        SELECT COUNT(*) AS urgentes FROM ecommerce_reclamacoes
        WHERE cnpjloja=%s AND status='aberta'
          AND prazo_loja_responder < NOW() + INTERVAL '12 hours'
        """,
        (cnpjloja,),
    )
    urgentes = (cur.fetchone() or {}).get("urgentes", 0) or 0
    cur.close()

    return render_template(
        "painel_reclamacoes.html",
        reclamacoes=reclamacoes,
        sf=sf,
        total_adv=total_adv,
        urgentes=urgentes,
        motivos=_MOTIVOS_RECLAMACAO,
    )


@app.get("/painel/reclamacoes/<reclamacao_id>")
@painel_required
def painel_reclamacao_detalhe(reclamacao_id):
    _ensure_reclamacao_schema()
    cnpjloja = session.get("cnpjloja")
    _processar_prazos_reclamacao(reclamacao_id)
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.*, c.nome AS consumidor_nome, c.telefone AS consumidor_tel, c.email AS consumidor_email
        FROM ecommerce_reclamacoes r
        JOIN ecommerce_consumidores c ON c.id = r.consumidor_id
        WHERE r.id=%s AND r.cnpjloja=%s
        LIMIT 1
        """,
        (reclamacao_id, cnpjloja),
    )
    rec = cur.fetchone()
    if not rec:
        flash("Reclamação não encontrada.", "error")
        cur.close()
        return redirect(url_for("painel_reclamacoes"))
    cur.execute(
        "SELECT * FROM ecommerce_reclamacao_msgs WHERE reclamacao_id=%s ORDER BY enviada_em",
        (reclamacao_id,),
    )
    msgs = cur.fetchall()

    # Pedido relacionado
    cur.execute(
        "SELECT id, status, total, criado_em FROM ecommerce_pedidos WHERE id=%s LIMIT 1",
        (rec["pedido_id"],),
    )
    pedido = cur.fetchone()

    # Advertências desta loja
    cur.execute(
        "SELECT * FROM ecommerce_loja_advertencias WHERE cnpjloja=%s ORDER BY criada_em DESC LIMIT 10",
        (cnpjloja,),
    )
    advertencias = cur.fetchall()
    cur.close()

    return render_template(
        "painel_reclamacao_detalhe.html",
        rec=dict(rec),
        msgs=msgs,
        pedido=dict(pedido) if pedido else None,
        advertencias=advertencias,
        motivos=_MOTIVOS_RECLAMACAO,
        status_label=_STATUS_RECLAMACAO_LABEL,
    )


@app.post("/painel/reclamacoes/<reclamacao_id>/mensagem")
@painel_required
def painel_reclamacao_mensagem(reclamacao_id):
    _ensure_reclamacao_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, status FROM ecommerce_reclamacoes WHERE id=%s AND cnpjloja=%s LIMIT 1",
        (reclamacao_id, cnpjloja),
    )
    rec = cur.fetchone()
    if not rec or rec["status"] == "finalizada":
        cur.close()
        return redirect(url_for("painel_reclamacoes"))

    mensagem = (request.form.get("mensagem") or "").strip()
    if len(mensagem) < 2:
        flash("Digite uma mensagem.", "error")
        cur.close()
        return redirect(url_for("painel_reclamacao_detalhe", reclamacao_id=reclamacao_id))

    cur.execute(
        "INSERT INTO ecommerce_reclamacao_msgs (reclamacao_id, autor, mensagem) VALUES (%s, 'loja', %s)",
        (reclamacao_id, mensagem),
    )
    # Se estava aberta (sem resposta), passa para em_andamento
    if rec["status"] == "aberta":
        cur.execute(
            "UPDATE ecommerce_reclamacoes SET status='em_andamento' WHERE id=%s",
            (reclamacao_id,),
        )
    conn.commit()
    cur.close()
    return redirect(url_for("painel_reclamacao_detalhe", reclamacao_id=reclamacao_id))


@app.post("/painel/reclamacoes/<reclamacao_id>/marcar-resolvido")
@painel_required
def painel_reclamacao_marcar_resolvido(reclamacao_id):
    _ensure_reclamacao_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, status FROM ecommerce_reclamacoes WHERE id=%s AND cnpjloja=%s LIMIT 1",
        (reclamacao_id, cnpjloja),
    )
    rec = cur.fetchone()
    if not rec or rec["status"] in ("finalizada", "aguardando_cliente"):
        flash("Ação inválida para o status atual.", "error")
        cur.close()
        return redirect(url_for("painel_reclamacao_detalhe", reclamacao_id=reclamacao_id))

    prazo_cliente = datetime.now(timezone.utc) + timedelta(hours=72)
    cur.execute(
        """
        UPDATE ecommerce_reclamacoes
        SET status='aguardando_cliente', prazo_cliente_confirmar=%s
        WHERE id=%s
        """,
        (prazo_cliente, reclamacao_id),
    )
    cur.execute(
        "INSERT INTO ecommerce_reclamacao_msgs (reclamacao_id, autor, mensagem) VALUES (%s, 'loja', %s)",
        (reclamacao_id, "🔔 A farmácia marcou esta reclamação como resolvida. Por favor, confirme se o problema foi solucionado."),
    )
    conn.commit()
    cur.close()
    flash("Reclamação marcada como resolvida. O cliente tem 72 horas para confirmar.", "success")
    return redirect(url_for("painel_reclamacao_detalhe", reclamacao_id=reclamacao_id))


# ─── PAINEL: CONFIGURAÇÕES ────────────────────────────────────────────────────

# ─── PAINEL: RELATÓRIOS ───────────────────────────────────────────────────────

def _parse_report_date(value, fallback):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return fallback


@app.get("/painel/relatorios")
@painel_required
def painel_relatorios():
    _ensure_payment_schema()
    _ensure_receita_schema()
    _ensure_reclamacao_schema()
    _ensure_ml_schema()

    cnpjloja = session.get("cnpjloja")
    hoje = datetime.now().date()
    inicio_default = hoje - timedelta(days=29)
    data_inicio = _parse_report_date(request.args.get("inicio"), inicio_default)
    data_fim = _parse_report_date(request.args.get("fim"), hoje)
    if data_inicio > data_fim:
        data_inicio, data_fim = data_fim, data_inicio

    origem = (request.args.get("origem") or "todos").strip()
    if origem not in ("todos", "ecommerce", "mercado_livre"):
        origem = "todos"

    status = (request.args.get("status") or "validos").strip()
    status_options = {
        "validos": "Válidos",
        "todos": "Todos",
        "pendente": "Pendente",
        "pago": "Pago",
        "enviado": "Enviado",
        "entregue": "Entregue",
        "cancelado": "Cancelado",
    }
    if status not in status_options:
        status = "validos"

    inicio_dt = datetime.combine(data_inicio, datetime.min.time())
    fim_dt = datetime.combine(data_fim + timedelta(days=1), datetime.min.time())

    order_where = ["p.cnpjloja=%s", "p.criado_em >= %s", "p.criado_em < %s"]
    order_args = [cnpjloja, inicio_dt, fim_dt]
    if origem != "todos":
        order_where.append("COALESCE(p.origem, 'ecommerce') = %s")
        order_args.append(origem)
    if status == "validos":
        order_where.append("p.status <> 'cancelado'")
    elif status != "todos":
        order_where.append("p.status = %s")
        order_args.append(status)
    order_filter = " AND ".join(order_where)

    conn = db()
    cur = conn.cursor()

    cur.execute(f"""
        SELECT
          COUNT(*) AS pedidos,
          COALESCE(SUM(p.total), 0) AS receita,
          COUNT(DISTINCT COALESCE(
            NULLIF(p.consumidor_id::TEXT, ''),
            NULLIF(p.cliente_email, ''),
            NULLIF(p.cliente_telefone, ''),
            NULLIF(p.cliente_nome, '')
          )) AS clientes,
          COALESCE(AVG(p.total), 0) AS ticket_medio,
          COALESCE(SUM(CASE WHEN COALESCE(p.origem, 'ecommerce')='mercado_livre' THEN 1 ELSE 0 END), 0) AS pedidos_ml,
          COALESCE(SUM(CASE WHEN COALESCE(p.origem, 'ecommerce')='ecommerce' THEN 1 ELSE 0 END), 0) AS pedidos_ecommerce,
          COALESCE(SUM(CASE WHEN COALESCE(p.origem, 'ecommerce')='mercado_livre' THEN p.total ELSE 0 END), 0) AS receita_ml,
          COALESCE(SUM(CASE WHEN COALESCE(p.origem, 'ecommerce')='ecommerce' THEN p.total ELSE 0 END), 0) AS receita_ecommerce
        FROM ecommerce_pedidos p
        WHERE {order_filter}
    """, order_args)
    resumo = dict(cur.fetchone() or {})

    cur.execute(f"""
        SELECT COALESCE(SUM(i.qty), 0) AS itens
        FROM ecommerce_pedido_itens i
        JOIN ecommerce_pedidos p ON p.id = i.pedido_id
        WHERE {order_filter}
    """, order_args)
    resumo["itens"] = (cur.fetchone() or {}).get("itens", 0) or 0

    cur.execute(f"""
        SELECT p.id, p.cliente_nome, p.cliente_telefone, p.cliente_email,
               p.total, p.status, p.criado_em,
               COALESCE(p.origem, 'ecommerce') AS origem,
               COUNT(i.id) AS itens,
               COALESCE(SUM(i.qty), 0) AS unidades
        FROM ecommerce_pedidos p
        LEFT JOIN ecommerce_pedido_itens i ON i.pedido_id = p.id
        WHERE {order_filter}
        GROUP BY p.id, p.cliente_nome, p.cliente_telefone, p.cliente_email,
                 p.total, p.status, p.criado_em, COALESCE(p.origem, 'ecommerce')
        ORDER BY p.criado_em DESC
        LIMIT 30
    """, order_args)
    vendas_resumo = [dict(r) for r in cur.fetchall()]

    cur.execute(f"""
        SELECT COALESCE(
                 NULLIF(p.consumidor_id::TEXT, ''),
                 NULLIF(p.cliente_email, ''),
                 NULLIF(p.cliente_telefone, ''),
                 NULLIF(p.cliente_nome, '')
               ) AS cliente_key,
               MAX(NULLIF(p.cliente_nome, '')) AS nome,
               MAX(NULLIF(p.cliente_telefone, '')) AS telefone,
               MAX(NULLIF(p.cliente_email, '')) AS email,
               COUNT(*) AS pedidos,
               COALESCE(SUM(p.total), 0) AS total,
               MAX(p.criado_em) AS ultima_compra,
               COALESCE(SUM(CASE WHEN COALESCE(p.origem, 'ecommerce')='mercado_livre' THEN 1 ELSE 0 END), 0) AS pedidos_ml,
               COALESCE(SUM(CASE WHEN COALESCE(p.origem, 'ecommerce')='ecommerce' THEN 1 ELSE 0 END), 0) AS pedidos_ecommerce
        FROM ecommerce_pedidos p
        WHERE {order_filter}
        GROUP BY COALESCE(
                 NULLIF(p.consumidor_id::TEXT, ''),
                 NULLIF(p.cliente_email, ''),
                 NULLIF(p.cliente_telefone, ''),
                 NULLIF(p.cliente_nome, '')
               )
        ORDER BY total DESC, pedidos DESC
        LIMIT 30
    """, order_args)
    clientes_resumo = [dict(r) for r in cur.fetchall()]

    cur.execute(f"""
        SELECT COALESCE(p.origem, 'ecommerce') AS origem,
               COUNT(*) AS pedidos,
               COALESCE(SUM(p.total), 0) AS receita,
               COUNT(DISTINCT COALESCE(
                 NULLIF(p.consumidor_id::TEXT, ''),
                 NULLIF(p.cliente_email, ''),
                 NULLIF(p.cliente_telefone, ''),
                 NULLIF(p.cliente_nome, '')
               )) AS clientes
        FROM ecommerce_pedidos p
        WHERE {order_filter}
        GROUP BY COALESCE(p.origem, 'ecommerce')
    """, order_args)
    canais = {r["origem"]: dict(r) for r in cur.fetchall()}

    cur.execute(f"""
        SELECT DATE(p.criado_em) AS dia,
               COUNT(*) AS pedidos,
               COALESCE(SUM(p.total), 0) AS receita
        FROM ecommerce_pedidos p
        WHERE {order_filter}
        GROUP BY DATE(p.criado_em)
        ORDER BY dia
    """, order_args)
    vendas_por_dia = [dict(r) for r in cur.fetchall()]

    cur.execute(f"""
        SELECT COALESCE(p.origem, 'ecommerce') AS origem,
               COUNT(*) AS pedidos,
               COALESCE(SUM(p.total), 0) AS receita
        FROM ecommerce_pedidos p
        WHERE {order_filter}
        GROUP BY COALESCE(p.origem, 'ecommerce')
        ORDER BY origem
    """, order_args)
    vendas_por_canal = [dict(r) for r in cur.fetchall()]

    cur.execute(f"""
        SELECT COALESCE(NULLIF(i.ean, ''), 'sem-ean') AS ean,
               MAX(i.nome) AS nome,
               COALESCE(SUM(i.qty), 0) AS qtd,
               COALESCE(SUM(i.qty * i.preco_unitario), 0) AS receita
        FROM ecommerce_pedido_itens i
        JOIN ecommerce_pedidos p ON p.id = i.pedido_id
        WHERE {order_filter}
        GROUP BY COALESCE(NULLIF(i.ean, ''), 'sem-ean')
        ORDER BY qtd DESC, receita DESC
        LIMIT 8
    """, order_args)
    top_produtos = [dict(r) for r in cur.fetchall()]

    estoque_cte = f"""
        WITH vendas AS (
          SELECT COALESCE(NULLIF(i.ean, ''), 'sem-ean') AS ean,
                 COALESCE(SUM(i.qty), 0) AS vendidos,
                 COALESCE(SUM(i.qty * i.preco_unitario), 0) AS receita
          FROM ecommerce_pedido_itens i
          JOIN ecommerce_pedidos p ON p.id = i.pedido_id
          WHERE {order_filter}
          GROUP BY COALESCE(NULLIF(i.ean, ''), 'sem-ean')
        ),
        estoque_unificado AS (
          SELECT barras AS ean, descricao AS nome, CAST(estoque AS INTEGER) AS estoque
          FROM estoque
          WHERE cnpj=%s AND estoque > 0
          UNION ALL
          SELECT ean, descricao_produto AS nome, CAST(quantidade_estoque AS INTEGER) AS estoque
          FROM automatiza_estoque
          WHERE cnpj_loja=%s AND quantidade_estoque > 0
        ),
        estoque_loja AS (
          SELECT ean, MAX(nome) AS nome, SUM(estoque) AS estoque
          FROM estoque_unificado
          WHERE COALESCE(ean, '') <> ''
          GROUP BY ean
        )
    """
    estoque_args = order_args + [cnpjloja, cnpjloja]

    cur.execute(estoque_cte + """
        SELECT e.ean, e.nome, e.estoque,
               COALESCE(v.vendidos, 0) AS vendidos,
               COALESCE(v.receita, 0) AS receita
        FROM estoque_loja e
        LEFT JOIN vendas v ON v.ean = e.ean
        ORDER BY COALESCE(v.vendidos, 0) ASC, e.estoque DESC, e.nome
        LIMIT 8
    """, estoque_args)
    produtos_menos_vendidos = [dict(r) for r in cur.fetchall()]

    cur.execute(estoque_cte + """
        SELECT e.ean, e.nome, e.estoque,
               COALESCE(v.vendidos, 0) AS vendidos,
               COALESCE(v.receita, 0) AS receita,
               CASE
                 WHEN COALESCE(v.vendidos, 0) >= 10 AND e.estoque <= 5 THEN 'Alta prioridade'
                 WHEN COALESCE(v.vendidos, 0) >= 5 AND e.estoque <= 8 THEN 'Prioridade média'
                 ELSE 'Monitorar'
               END AS prioridade
        FROM estoque_loja e
        JOIN vendas v ON v.ean = e.ean
        WHERE e.estoque <= 10
        ORDER BY v.vendidos DESC, e.estoque ASC, v.receita DESC
        LIMIT 8
    """, estoque_args)
    reposicao = [dict(r) for r in cur.fetchall()]

    rec_args = [cnpjloja, inicio_dt, fim_dt]
    cur.execute("""
        SELECT
          COUNT(*) AS total,
          COALESCE(SUM(CASE WHEN status <> 'finalizada' THEN 1 ELSE 0 END), 0) AS abertas,
          COALESCE(SUM(CASE WHEN status = 'finalizada' THEN 1 ELSE 0 END), 0) AS finalizadas,
          COALESCE(SUM(CASE WHEN status = 'finalizada' AND (prazo_loja_responder IS NULL OR finalizada_em <= prazo_loja_responder) THEN 1 ELSE 0 END), 0) AS dentro_prazo,
          COALESCE(SUM(CASE WHEN status = 'finalizada' AND prazo_loja_responder IS NOT NULL AND finalizada_em > prazo_loja_responder THEN 1 ELSE 0 END), 0) AS fora_prazo,
          COALESCE(SUM(CASE WHEN status <> 'finalizada' AND prazo_loja_responder IS NOT NULL AND NOW() > prazo_loja_responder THEN 1 ELSE 0 END), 0) AS pendentes_atrasadas,
          COALESCE(SUM(CASE WHEN advertencia_loja THEN 1 ELSE 0 END), 0) AS advertencias
        FROM ecommerce_reclamacoes
        WHERE cnpjloja=%s AND aberta_em >= %s AND aberta_em < %s
    """, rec_args)
    reclamacoes = dict(cur.fetchone() or {})

    cur.close()

    def _num(v):
        try:
            return float(v or 0)
        except Exception:
            return 0.0

    dias_map = {r["dia"].isoformat(): r for r in vendas_por_dia}
    labels = []
    receita_series = []
    pedidos_series = []
    cursor_dia = data_inicio
    while cursor_dia <= data_fim:
        key = cursor_dia.isoformat()
        row = dias_map.get(key, {})
        labels.append(cursor_dia.strftime("%d/%m"))
        receita_series.append(round(_num(row.get("receita")), 2))
        pedidos_series.append(int(row.get("pedidos") or 0))
        cursor_dia += timedelta(days=1)

    canal_labels = {"ecommerce": "Ecommerce", "mercado_livre": "Mercado Livre"}
    canal_chart = {
        "labels": [canal_labels.get(r["origem"], r["origem"]) for r in vendas_por_canal],
        "receita": [round(_num(r.get("receita")), 2) for r in vendas_por_canal],
        "pedidos": [int(r.get("pedidos") or 0) for r in vendas_por_canal],
    }

    relatorio = {
        "resumo": {k: _num(v) for k, v in resumo.items()},
        "canais": canais,
        "top_produtos": top_produtos,
        "vendas_resumo": vendas_resumo,
        "clientes_resumo": clientes_resumo,
        "produtos_menos_vendidos": produtos_menos_vendidos,
        "reposicao": reposicao,
        "reclamacoes": {k: _num(v) for k, v in reclamacoes.items()},
        "chart_vendas": {"labels": labels, "receita": receita_series, "pedidos": pedidos_series},
        "chart_canais": canal_chart,
    }

    filtros = {
        "inicio": data_inicio.isoformat(),
        "fim": data_fim.isoformat(),
        "origem": origem,
        "status": status,
        "status_options": status_options,
    }
    # ── Vitrine cliques (WhatsApp / Maps) ─────────────────────────────────────
    _ensure_lojas_vitrine_schema()
    vitrine_stats = {"whatsapp": 0, "maps": 0, "total": 0, "por_dia": []}
    try:
        cur2 = conn.cursor()
        cur2.execute(
            """
            SELECT tipo, COUNT(*) AS total
            FROM ecommerce_lojas_vitrine_cliques
            WHERE cnpjloja = %s
              AND created_at::date BETWEEN %s AND %s
            GROUP BY tipo
            """,
            (cnpjloja, data_inicio, data_fim),
        )
        for r in cur2.fetchall():
            vitrine_stats[r["tipo"]] = int(r["total"])
        vitrine_stats["total"] = vitrine_stats["whatsapp"] + vitrine_stats["maps"]

        cur2.execute(
            """
            SELECT created_at::date AS dia, tipo, COUNT(*) AS cnt
            FROM ecommerce_lojas_vitrine_cliques
            WHERE cnpjloja = %s
              AND created_at::date BETWEEN %s AND %s
            GROUP BY dia, tipo
            ORDER BY dia
            """,
            (cnpjloja, data_inicio, data_fim),
        )
        vitrine_stats["por_dia"] = [
            {"dia": str(r["dia"]), "tipo": r["tipo"], "cnt": int(r["cnt"])}
            for r in cur2.fetchall()
        ]
        cur2.close()
    except Exception:
        pass

    return render_template("painel_relatorios.html", relatorio=relatorio, filtros=filtros,
                           vitrine_stats=vitrine_stats)


@app.get("/painel/config")
@painel_required
def painel_config():
    _ensure_receita_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM ecommerce_config_loja WHERE cnpjloja=%s LIMIT 1", (cnpjloja,))
    config = cur.fetchone() or {}
    cur.close()
    return render_template("painel_config.html", config=config)


@app.get("/painel/config/mp-test")
@painel_required
def painel_mp_test():
    """Testa o token do Mercado Pago — conta + criação de PIX real."""
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT mp_access_token FROM ecommerce_config_loja WHERE cnpjloja=%s", (cnpjloja,))
    row = cur.fetchone(); cur.close()
    token = (row or {}).get("mp_access_token") or ""
    if not token:
        return jsonify({"ok": False, "erro": "Token não configurado."})

    resultado = {"token_prefixo": token[:20] + "..."}

    # 1) Testa conta
    try:
        me = _mp_request(token, "/users/me", method="GET")
        resultado["conta"] = {
            "ok": True,
            "id": me.get("id"),
            "email": me.get("email"),
            "site_id": me.get("site_id"),
            "pix_habilitado": me.get("site_id") == "MLB",
        }
    except Exception as exc:
        resultado["conta"] = {"ok": False, "erro": str(exc)}

    # 2) Testa criação de pagamento PIX de R$1,00
    try:
        pix_payload = {
            "transaction_amount": 1.00,
            "description": "Teste PIX Poupaqui",
            "payment_method_id": "pix",
            "external_reference": f"mp-test-{cnpjloja}",
            "payer": {"email": f"teste.{int(time.time())}@poupaqui.com.br",
                      "first_name": "Teste", "last_name": "Poupaqui"},
        }
        pix_data = _mp_request(token, "/v1/payments", pix_payload,
                               idempotency_key=f"mp-test-pix-{cnpjloja}-{int(time.time())}")
        tx = (pix_data.get("point_of_interaction") or {}).get("transaction_data") or {}
        resultado["pix_teste"] = {
            "ok": True,
            "payment_id": pix_data.get("id"),
            "status": pix_data.get("status"),
            "tem_qr_code": bool(tx.get("qr_code")),
            "tem_qr_base64": bool(tx.get("qr_code_base64")),
        }
    except Exception as exc:
        resultado["pix_teste"] = {"ok": False, "erro": str(exc)}

    resultado["ok"] = (resultado.get("conta", {}).get("ok") and
                       resultado.get("pix_teste", {}).get("ok"))
    return jsonify(resultado)


@app.post("/painel/config")
@painel_required
def painel_config_salvar():
    _ensure_receita_schema()
    cnpjloja = session.get("cnpjloja")
    f = request.form
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        """
        INSERT INTO ecommerce_config_loja
          (cnpjloja, whatsapp_pedidos, whatsapp_receita, email_notificacao, aceita_whatsapp, aceita_pix, aceita_mp,
           aceita_entrega, raio_entrega_km, cobra_frete, valor_frete,
           pedido_minimo_entrega, pix_chave, pix_nome, mp_access_token, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (cnpjloja) DO UPDATE SET
          whatsapp_pedidos       = EXCLUDED.whatsapp_pedidos,
          whatsapp_receita       = EXCLUDED.whatsapp_receita,
          email_notificacao      = EXCLUDED.email_notificacao,
          aceita_whatsapp        = EXCLUDED.aceita_whatsapp,
          aceita_pix             = EXCLUDED.aceita_pix,
          aceita_mp              = EXCLUDED.aceita_mp,
          aceita_entrega         = EXCLUDED.aceita_entrega,
          raio_entrega_km        = EXCLUDED.raio_entrega_km,
          cobra_frete            = EXCLUDED.cobra_frete,
          valor_frete            = EXCLUDED.valor_frete,
          pedido_minimo_entrega  = EXCLUDED.pedido_minimo_entrega,
          pix_chave              = EXCLUDED.pix_chave,
          pix_nome               = EXCLUDED.pix_nome,
          mp_access_token        = EXCLUDED.mp_access_token,
          updated_at             = NOW()
        """,
        (
            cnpjloja,
            (f.get("whatsapp_pedidos") or "").strip(),
            (f.get("whatsapp_receita") or "").strip() or None,
            (f.get("email_notificacao") or "").strip() or None,
            "aceita_whatsapp" in f,
            "aceita_pix"      in f,
            "aceita_mp"       in f,
            "aceita_entrega"  in f,
            _to_float_or_none(f.get("raio_entrega_km")) or 0,
            "cobra_frete"     in f,
            _to_float_or_none(f.get("valor_frete")) or 0,
            _to_float_or_none(f.get("pedido_minimo_entrega")) or 0,
            (f.get("pix_chave")       or "").strip(),
            (f.get("pix_nome")        or "").strip(),
            (f.get("mp_access_token") or "").strip(),
        ),
    )
    conn.commit()
    cur.close()
    flash("Configurações salvas com sucesso.", "success")
    return redirect(url_for("painel_config"))


# ─── PAINEL: UPLOAD IMAGEM DO PRODUTO ────────────────────────────────────────

@app.post("/painel/produto/<ean>/imagem")
@painel_required
def painel_produto_imagem(ean):
    cnpjloja = session.get("cnpjloja")
    if "imagem" not in request.files or not request.files["imagem"].filename:
        flash("Selecione uma imagem.", "error")
        return redirect(url_for("precificador"))

    f   = request.files["imagem"]
    raw = f.read()
    ext = (f.filename.rsplit(".", 1)[-1].lower()) if "." in f.filename else "jpg"
    ct  = {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png",
           "webp":"image/webp","gif":"image/gif"}.get(ext, "image/jpeg")
    path = f"{cnpjloja}/{ean}.{ext}"

    img_url = upload_to_supabase_storage(raw, path, ct)
    if not img_url:
        flash("Erro no upload da imagem. Verifique o arquivo e tente novamente.", "error")
        return redirect(url_for("precificador"))

    conn = db()
    cur  = conn.cursor()
    cur.execute(
        """
        INSERT INTO ecommerce_produto_imagens (cnpjloja, ean, imagem_url)
        VALUES (%s,%s,%s)
        ON CONFLICT (cnpjloja, ean) DO UPDATE
          SET imagem_url=EXCLUDED.imagem_url, updated_at=NOW()
        """,
        (cnpjloja, ean, img_url),
    )
    conn.commit()
    cur.close()
    flash("Imagem do produto atualizada.", "success")
    return redirect(url_for("precificador"))


@app.get("/loja/<cnpjloja>")
def catalogo_loja(cnpjloja):
    _ensure_catalog_admin_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.cnpjloja, u.razao, u.endereco, u.uf, u.telefone,
               COALESCE(c.catalogo_publico, TRUE) AS catalogo_publico
        FROM users u
        LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
        WHERE u.cnpjloja = %s AND u.is_admin = FALSE
        LIMIT 1
        """,
        (cnpjloja,),
    )
    loja = cur.fetchone()
    cur.close()

    if not loja:
        flash("Loja não encontrada.", "error")
        return redirect(url_for("index"))

    loja = dict(loja)
    loja["razao"] = _public_store_name(loja)

    q = (request.args.get("q") or "").strip()
    if loja.get("catalogo_publico") is False:
        produtos = []
    else:
        produtos = get_dns_products(cnpjloja, q or None)
        produtos, _bloqueados_sem_imagem = _split_catalog_image_status(produtos)

    conn2 = db()
    _marcar_tarja_batch(produtos, conn2)
    conn2.close()

    return render_template("catalogo_loja.html", loja=loja, produtos=produtos, q=q)


# ─── PAINEL DA LOJA (login) ───────────────────────────────────────────────────

@app.get("/painel/login")
def painel_login():
    if session.get("painel_ok"):
        return redirect(url_for("precificador"))
    return render_template("painel_login.html")


@app.post("/painel/login")
def painel_login_post():
    usuario = (request.form.get("usuario") or "").strip()
    senha   = (request.form.get("senha")   or "").strip()

    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, cnpjloja, usuario, senha, razao, uf, endereco, is_admin
        FROM users WHERE usuario = %s LIMIT 1
        """,
        (usuario,),
    )
    u = cur.fetchone()
    cur.close()

    if not u or (u.get("senha") or "") != senha:
        flash("Usuário ou senha inválidos.", "error")
        return redirect(url_for("painel_login"))

    session.clear()
    session["painel_ok"]  = True
    session["user_id"]    = str(u["id"])
    session["cnpjloja"]   = u["cnpjloja"]
    session["razao"]      = u["razao"]
    session["uf"]         = u["uf"]
    session["endereco"]   = u["endereco"]
    session["is_admin"]   = bool(u.get("is_admin"))

    if session["is_admin"]:
        return redirect(url_for("admin_lojas"))
    return redirect(url_for("precificador"))


@app.get("/painel/logout")
def painel_logout():
    session.clear()
    return redirect(url_for("painel_login"))


@app.get("/painel")
@painel_required
def painel_home():
    return redirect(url_for("precificador"))


# ─── PRECIFICADOR ─────────────────────────────────────────────────────────────

@app.get("/painel/precificador")
@painel_required
def precificador():
    _ensure_competitor_schema()
    cnpjloja = session.get("cnpjloja")
    q = (request.args.get("q") or "").strip()
    produtos = get_dns_products(cnpjloja, q or None)
    _publicados, bloqueados_sem_imagem = _split_catalog_image_status(produtos)

    # Carrega preços concorrentes cacheados no banco
    eans = [p["ean"] for p in produtos if p.get("ean")]
    competitor_map = {}   # {ean: {slug: {preco, preco_original, disponivel, url, consultado_em}}}
    competitor_last_update = None
    if eans:
        conn = db()
        cur = conn.cursor()
        placeholders = ",".join(["%s"] * len(eans))
        cur.execute(
            f"SELECT * FROM ecommerce_competitor_prices WHERE ean IN ({placeholders}) ORDER BY consultado_em DESC",
            eans,
        )
        for row in cur.fetchall():
            d = dict(row)
            ean = d["ean"]
            slug = d["concorrente"]
            competitor_map.setdefault(ean, {})[slug] = d
            if competitor_last_update is None or (d.get("consultado_em") and d["consultado_em"] > competitor_last_update):
                competitor_last_update = d.get("consultado_em")
        cur.close()

    return render_template(
        "precificador.html",
        produtos=produtos,
        bloqueados_sem_imagem=bloqueados_sem_imagem,
        q=q,
        razao=session.get("razao"),
        is_admin=session.get("is_admin"),
        competitor_map=competitor_map,
        competitor_last_update=competitor_last_update,
        concorrentes=_CONCORRENTES,
    )


@app.get("/painel/precificador/testar-ean")
@painel_required
def precificador_testar_ean():
    """Debug: testa a busca de preço de concorrente para um EAN específico e retorna o resultado bruto."""
    ean = (request.args.get("ean") or "").strip()
    slug = (request.args.get("concorrente") or "drogaraia").strip()
    if not ean:
        return jsonify({"erro": "Informe ?ean=<codigo>"})
    info = _CONCORRENTES.get(slug)
    if not info:
        return jsonify({"erro": f"Concorrente '{slug}' desconhecido. Use: drogaraia ou drogariasaopaulo"})

    host_override = info.get("vtex_host")
    all_bases = [b for b in [info["base"]] + info.get("base_fallbacks", []) if b]
    results = []
    for base in all_bases:
        for url_tpl, _ in _build_vtex_attempts(base):
            url = url_tpl.format(ean=ean)
            entry = {"base": base, "host_override": host_override, "url": url,
                     "ok": False, "status": None, "corpo_preview": None, "json_ok": False, "amostra": None}
            try:
                from curl_cffi import requests as cffi_requests
                referer = url.split("/_v")[0].split("/api")[0] + "/"
                req_headers = {**_VTEX_HEADERS, "Referer": referer}
                if host_override:
                    req_headers["Host"] = host_override
                r = cffi_requests.get(
                    url,
                    headers=req_headers,
                    impersonate="chrome124",
                    timeout=12,
                    allow_redirects=True,
                )
                entry["status"] = r.status_code
                entry["corpo_preview"] = (r.text or "")[:800]
                if r.status_code == 200 and r.content:
                    try:
                        parsed = r.json()
                        entry["ok"] = True
                        entry["json_ok"] = True
                        entry["tipo"] = type(parsed).__name__
                        entry["tamanho"] = len(parsed) if isinstance(parsed, list) else (len((parsed or {}).get("products", [])) if isinstance(parsed, dict) else 0)
                        entry["amostra"] = str(parsed)[:600]
                    except Exception as je:
                        entry["json_erro"] = str(je)
            except ImportError:
                entry["erro"] = "curl_cffi não instalado"
                raw = _vtex_get_json(url, timeout=12, host_override=host_override)
                if raw is not None:
                    entry["ok"] = True
                    entry["amostra"] = str(raw)[:600]
            except Exception as e:
                entry["erro"] = str(e)[:300]
            results.append(entry)

    resultado_final = None
    primary_base = info["base"]
    if primary_base:
        fallbacks = info.get("base_fallbacks", [])
        resultado_final = _fetch_vtex_price(ean, primary_base, fallbacks=fallbacks, host_override=host_override)

    scrape_result = None
    if info.get("scrape_search"):
        scrape_result = _fetch_raia_via_search_page(ean)
        if resultado_final is None:
            resultado_final = scrape_result

    return jsonify({
        "ean": ean,
        "concorrente": slug,
        "bases_testadas": [b for b in all_bases if b],
        "scrape_search_result": scrape_result,
        "resultado_final": resultado_final,
        "tentativas": results,
    })


@app.get("/painel/precificador/debug-raia-html")
@painel_required
def precificador_debug_raia_html():
    """Debug: busca a página de busca da Raia e retorna status + primeiros 4000 chars de HTML."""
    import re
    ean = (request.args.get("ean") or "").strip()
    if not ean:
        return jsonify({"erro": "Informe ?ean=<codigo>"})
    url = f"https://www.drogaraia.com.br/search?w={urllib.parse.quote(str(ean))}"
    try:
        from curl_cffi import requests as cffi_requests
        r = cffi_requests.get(
            url,
            headers={
                "User-Agent": _VTEX_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                "Referer": "https://www.drogaraia.com.br/",
            },
            impersonate="chrome124",
            timeout=18,
            allow_redirects=True,
        )
        html = r.text or ""
        has_next_data = bool(re.search(r'__NEXT_DATA__', html, re.IGNORECASE))
        has_json_ld   = bool(re.search(r'application/ld\+json', html, re.IGNORECASE))
        has_price_kw  = bool(re.search(r'"(?:priceService|lowPrice|spotPrice|sellingPrice)"\s*:', html))
        next_data_preview = None
        m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
        if m:
            next_data_preview = m.group(1)[:3000]
        return jsonify({
            "url": url,
            "status": r.status_code,
            "html_len": len(html),
            "html_preview": html[:3000],
            "has_next_data": has_next_data,
            "has_json_ld": has_json_ld,
            "has_price_keyword": has_price_kw,
            "next_data_preview": next_data_preview,
        })
    except Exception as e:
        return jsonify({"erro": str(e)[:500], "url": url})


@app.post("/painel/precificador/buscar-concorrentes")
@painel_required
def precificador_buscar_concorrentes():
    """Dispara busca de preços em background para todos os EANs do catálogo desta loja."""
    _ensure_competitor_schema()
    cnpjloja = session.get("cnpjloja")
    produtos = get_dns_products(cnpjloja, None)
    eans = [p["ean"] for p in produtos if p.get("ean")]
    if not eans:
        return jsonify({"ok": False, "msg": "Nenhum produto encontrado."})

    # Limpa registros de erro da Raia (URL antiga www.drogaraia.com.br salvou "sem_resultado")
    try:
        conn2 = db(); cur2 = conn2.cursor()
        placeholders2 = ",".join(["%s"] * len(eans))
        cur2.execute(
            f"DELETE FROM ecommerce_competitor_prices WHERE concorrente='drogaraia' AND preco IS NULL AND ean IN ({placeholders2})",
            eans,
        )
        conn2.commit(); cur2.close()
    except Exception:
        pass

    # Retorna lista de EANs para que o JS processe em lotes síncronos
    return jsonify({"ok": True, "eans": eans, "total": len(eans)})


@app.post("/painel/precificador/buscar-lote")
@painel_required
def precificador_buscar_lote():
    """Processa um lote de EANs de forma síncrona. O JS chama em sequência para cobrir todos os produtos."""
    _ensure_competitor_schema()
    data = request.get_json(silent=True) or {}
    eans_lote = [str(e).strip() for e in (data.get("eans") or []) if e][:20]
    if not eans_lote:
        return jsonify({"ok": False, "msg": "Sem EANs"})
    stats = _fetch_and_store_competitor_prices(eans_lote) or {}
    return jsonify({"ok": True, "processados": len(eans_lote), "stats": stats})


@app.get("/painel/precificador/status-concorrentes")
@painel_required
def precificador_status_concorrentes():
    """Retorna preços atuais em cache para todos os EANs da loja (polling)."""
    _ensure_competitor_schema()
    cnpjloja = session.get("cnpjloja")
    produtos = get_dns_products(cnpjloja, None)
    eans = [p["ean"] for p in produtos if p.get("ean")]
    if not eans:
        return jsonify({"precos": {}, "last_update": None})
    conn = db()
    cur = conn.cursor()
    placeholders = ",".join(["%s"] * len(eans))
    cur.execute(
        f"SELECT ean, concorrente, preco, preco_original, disponivel, url, consultado_em FROM ecommerce_competitor_prices WHERE ean IN ({placeholders})",
        eans,
    )
    precos: dict = {}
    last_update = None
    for row in cur.fetchall():
        d = dict(row)
        ean = d["ean"]
        slug = d["concorrente"]
        precos.setdefault(ean, {})[slug] = {
            "preco": float(d["preco"]) if d.get("preco") is not None else None,
            "preco_original": float(d["preco_original"]) if d.get("preco_original") is not None else None,
            "disponivel": d.get("disponivel"),
            "url": d.get("url"),
        }
        ts = d.get("consultado_em")
        if ts and (last_update is None or ts > last_update):
            last_update = ts
    cur.close()
    return jsonify({
        "precos": precos,
        "last_update": last_update.strftime("%d/%m/%Y %H:%M") if last_update else None,
    })


@app.post("/painel/precificador/salvar")
@painel_required
def precificador_salvar():
    cnpjloja = session.get("cnpjloja")
    conn = db()
    cur = conn.cursor()
    saved = 0

    for key, val in request.form.items():
        if not key.startswith("preco_"):
            continue
        ean = key[6:]
        try:
            preco = float(val.replace(",", ".").replace("R$", "").strip())
            if preco <= 0:
                continue
        except Exception:
            continue

        cur.execute(
            """
            INSERT INTO ecommerce_precos (cnpjloja, ean, preco_customizado)
            VALUES (%s, %s, %s)
            ON CONFLICT (cnpjloja, ean) DO UPDATE
              SET preco_customizado = EXCLUDED.preco_customizado,
                  updated_at = NOW()
            """,
            (cnpjloja, ean, preco),
        )
        saved += 1

    conn.commit()
    cur.close()
    flash(f"{saved} preço(s) atualizado(s).", "success")
    return redirect(url_for("precificador"))


@app.post("/painel/precificador/ocultar")
@painel_required
def precificador_ocultar():
    cnpjloja = session.get("cnpjloja")
    ean = (request.form.get("ean") or "").strip()
    if not ean:
        return jsonify({"ok": False, "msg": "EAN inválido"})
    _ensure_precificador_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ecommerce_catalogo_oculto (cnpjloja, ean) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (cnpjloja, ean),
    )
    conn.commit()
    cur.close()
    return jsonify({"ok": True})


@app.post("/painel/precificador/restaurar")
@painel_required
def precificador_restaurar():
    cnpjloja = session.get("cnpjloja")
    ean = (request.form.get("ean") or "").strip()
    if not ean:
        return jsonify({"ok": False, "msg": "EAN inválido"})
    _ensure_precificador_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM ecommerce_catalogo_oculto WHERE cnpjloja = %s AND ean = %s",
        (cnpjloja, ean),
    )
    conn.commit()
    cur.close()
    return jsonify({"ok": True})


@app.get("/api/painel/buscar-estoque")
@painel_required
def api_painel_buscar_estoque():
    cnpjloja = session.get("cnpjloja")
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])

    _ensure_precificador_schema()
    conn = db()
    cur = conn.cursor()

    # EANs já visíveis (DNS/Vitnatu) para excluir da busca
    cur.execute("SELECT ean FROM ecommerce_catalogo_extra WHERE cnpjloja = %s", (cnpjloja,))
    ja_extras = {r["ean"] for r in cur.fetchall()}

    like = f"%{q.lower()}%"
    resultados = []
    seen = set()

    cur.execute("""
        SELECT e.barras AS ean, e.descricao AS nome, CAST(e.estoque AS INTEGER) AS qty,
               e.preco_referencial AS preco_ref
        FROM estoque e
        WHERE e.cnpj = %s AND e.estoque > 0 AND LOWER(e.descricao) LIKE %s
        ORDER BY e.descricao LIMIT 30
    """, (cnpjloja, like))
    for r in cur.fetchall():
        ean = (r["ean"] or "").strip()
        if ean and ean not in seen:
            seen.add(ean)
            resultados.append({
                "ean": ean, "nome": r["nome"],
                "qty": r["qty"], "preco_ref": float(r["preco_ref"] or 0),
                "ja_incluido": ean in ja_extras,
            })

    cur.execute("""
        SELECT ae.ean, ae.descricao_produto AS nome,
               CAST(ae.quantidade_estoque AS INTEGER) AS qty,
               ae.valor_final_produto AS preco_ref
        FROM automatiza_estoque ae
        WHERE ae.cnpj_loja = %s AND ae.quantidade_estoque > 0
          AND LOWER(ae.descricao_produto) LIKE %s
        ORDER BY ae.descricao_produto LIMIT 30
    """, (cnpjloja, like))
    for r in cur.fetchall():
        ean = (r["ean"] or "").strip()
        if ean and ean not in seen:
            seen.add(ean)
            resultados.append({
                "ean": ean, "nome": r["nome"],
                "qty": r["qty"], "preco_ref": float(r["preco_ref"] or 0),
                "ja_incluido": ean in ja_extras,
            })

    cur.close()
    return jsonify(resultados[:40])


@app.post("/painel/precificador/incluir-extra")
@painel_required
def precificador_incluir_extra():
    cnpjloja = session.get("cnpjloja")
    ean = (request.form.get("ean") or "").strip()
    if not ean:
        return jsonify({"ok": False, "msg": "EAN inválido"})
    _ensure_precificador_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ecommerce_catalogo_extra (cnpjloja, ean) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (cnpjloja, ean),
    )
    # Remove do oculto caso estivesse lá
    cur.execute(
        "DELETE FROM ecommerce_catalogo_oculto WHERE cnpjloja = %s AND ean = %s",
        (cnpjloja, ean),
    )
    conn.commit()
    cur.close()
    return jsonify({"ok": True})


@app.post("/painel/precificador/remover-extra")
@painel_required
def precificador_remover_extra():
    cnpjloja = session.get("cnpjloja")
    ean = (request.form.get("ean") or "").strip()
    if not ean:
        return jsonify({"ok": False, "msg": "EAN inválido"})
    _ensure_precificador_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM ecommerce_catalogo_extra WHERE cnpjloja = %s AND ean = %s",
        (cnpjloja, ean),
    )
    conn.commit()
    cur.close()
    return jsonify({"ok": True})


@app.post("/painel/precificador/ajuste-percentual")
@painel_required
def precificador_ajuste_percentual():
    cnpjloja = session.get("cnpjloja")
    try:
        pct = float(
            (request.form.get("percentual") or "0")
            .replace(",", ".")
            .replace("%", "")
            .strip()
        )
    except Exception:
        flash("Percentual inválido.", "error")
        return redirect(url_for("precificador"))

    if pct == 0:
        flash("Informe um percentual diferente de zero.", "error")
        return redirect(url_for("precificador"))

    fator = 1 + pct / 100
    produtos = get_dns_products(cnpjloja)
    if not produtos:
        flash("Nenhum produto encontrado.", "error")
        return redirect(url_for("precificador"))

    conn = db()
    cur = conn.cursor()
    atualizados = 0
    for p in produtos:
        base = p.get("preco_custom") or p.get("preco_ref")
        if not base:
            continue
        novo = round(float(base) * fator, 2)
        if novo <= 0:
            continue
        cur.execute(
            """
            INSERT INTO ecommerce_precos (cnpjloja, ean, preco_customizado)
            VALUES (%s, %s, %s)
            ON CONFLICT (cnpjloja, ean) DO UPDATE
              SET preco_customizado = EXCLUDED.preco_customizado,
                  updated_at = NOW()
            """,
            (cnpjloja, p["ean"], novo),
        )
        atualizados += 1

    conn.commit()
    cur.close()
    sinal = "+" if pct > 0 else ""
    flash(f"Ajuste de {sinal}{pct}% aplicado em {atualizados} produto(s).", "success")
    return redirect(url_for("precificador"))


@app.post("/painel/precificador/importar")
@painel_required
def precificador_importar():
    cnpjloja = session.get("cnpjloja")

    if "arquivo" not in request.files or not request.files["arquivo"].filename:
        flash("Selecione um arquivo para importar.", "error")
        return redirect(url_for("precificador"))

    f = request.files["arquivo"]
    content = f.read()
    fname = f.filename.lower()

    rows = []
    try:
        if fname.endswith((".xlsx", ".xls")):
            import io, openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
            ws = wb.active
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row[0] and row[1]:
                    rows.append((str(row[0]).strip(), str(row[1]).strip()))
        else:
            import csv, io
            text = content.decode("utf-8-sig", errors="replace")
            sep = ";" if ";" in text.splitlines()[0] else ","
            for row in csv.reader(io.StringIO(text), delimiter=sep):
                if len(row) >= 2 and row[0].strip():
                    rows.append((row[0].strip(), row[1].strip()))
    except Exception as e:
        flash(f"Erro ao ler arquivo: {e}", "error")
        return redirect(url_for("precificador"))

    conn = db()
    cur = conn.cursor()
    saved = errors = 0

    for ean, preco_raw in rows:
        try:
            preco = float(preco_raw.replace(",", ".").replace("R$", "").strip())
            if preco <= 0:
                continue
        except Exception:
            errors += 1
            continue

        cur.execute(
            """
            INSERT INTO ecommerce_precos (cnpjloja, ean, preco_customizado)
            VALUES (%s, %s, %s)
            ON CONFLICT (cnpjloja, ean) DO UPDATE
              SET preco_customizado = EXCLUDED.preco_customizado,
                  updated_at = NOW()
            """,
            (cnpjloja, ean, preco),
        )
        saved += 1

    conn.commit()
    cur.close()
    msg = f"Importação OK: {saved} preço(s) atualizados."
    if errors:
        msg += f" ({errors} linhas ignoradas por erro de formato)"
    flash(msg, "success")
    return redirect(url_for("precificador"))


# ─── ADMIN ────────────────────────────────────────────────────────────────────

_ADMIN_CATEGORIAS_PUBLICACAO = {
    "todos": "Todos",
    "medicamento": "Medicamentos",
    "nao_medicamento": "Não medicamentos",
    "cosmetico": "Cosméticos",
    "suplemento": "Suplementos",
    "desconhecido": "Outros/sem categoria",
}


def _admin_parse_categorias(raw):
    cats = [c.strip() for c in (raw or "").split(",") if c.strip()]
    cats = [c for c in cats if c in _ADMIN_CATEGORIAS_PUBLICACAO]
    return cats or ["todos"]


def _admin_categoria_permitida(nome, categorias):
    if "todos" in categorias:
        return True
    cat = _classificar_produto(nome or "") or "desconhecido"
    if "nao_medicamento" in categorias and cat != "medicamento":
        return True
    return cat in categorias


def _sync_catalogo_loja_admin(cnpjloja, min_estoque, categorias_raw):
    _ensure_catalog_admin_schema()
    categorias = _admin_parse_categorias(categorias_raw)
    min_estoque = max(0, int(min_estoque or 0))
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT e.barras AS ean,
               COALESCE(m.descricao, e.descricao) AS nome,
               CAST(e.estoque AS INTEGER) AS qty,
               COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
        FROM estoque e
        LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = e.cnpj AND epi.ean = e.barras
        WHERE e.cnpj=%s AND e.estoque > %s
          AND COALESCE(e.barras, e.barras_norm, '') <> ''

        UNION ALL

        SELECT ae.ean,
               COALESCE(m.descricao, ae.descricao_produto) AS nome,
               CAST(ae.quantidade_estoque AS INTEGER) AS qty,
               COALESCE(epi.imagem_url, mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem
        FROM automatiza_estoque ae
        LEFT JOIN medicamentos m ON m.barra_norm = ae.ean
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = ae.cnpj_loja AND epi.ean = ae.ean
        WHERE ae.cnpj_loja=%s AND ae.quantidade_estoque > %s
          AND COALESCE(ae.ean, '') <> ''
    """, (cnpjloja, min_estoque, cnpjloja, min_estoque))
    raw_rows = [dict(r) for r in cur.fetchall()]

    seen = set()
    produtos = []
    for row in raw_rows:
        ean = (row.get("ean") or "").strip()
        if not ean or ean in seen:
            continue
        nome = row.get("nome") or ""
        if not _admin_categoria_permitida(nome, categorias):
            continue
        seen.add(ean)
        produtos.append({"cnpjloja": cnpjloja, "ean": ean, "nome": nome, "qty": row.get("qty") or 0, "imagem": row.get("imagem") or ""})

    _apply_safe_catalog_images(produtos, cur=cur)
    _fill_missing_catalog_images(produtos, cnpjloja=cnpjloja, max_sync=len(produtos))
    _apply_safe_catalog_images(produtos, cur=cur)

    publicados = 0
    sem_imagem = 0
    for p in produtos:
        if not _has_catalog_image(p):
            sem_imagem += 1
            continue
        cur.execute(
            "INSERT INTO ecommerce_catalogo_extra (cnpjloja, ean) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (cnpjloja, p["ean"]),
        )
        cur.execute("DELETE FROM ecommerce_catalogo_oculto WHERE cnpjloja=%s AND ean=%s", (cnpjloja, p["ean"]))
        publicados += 1

    cur.execute("""
        INSERT INTO ecommerce_config_loja
          (cnpjloja, catalogo_publico, estoque_min_publicacao, categorias_publicacao,
           catalogo_sync_em, catalogo_sync_total, catalogo_sync_publicados, catalogo_sync_sem_imagem)
        VALUES (%s, TRUE, %s, %s, NOW(), %s, %s, %s)
        ON CONFLICT (cnpjloja) DO UPDATE SET
          catalogo_publico=TRUE,
          estoque_min_publicacao=EXCLUDED.estoque_min_publicacao,
          categorias_publicacao=EXCLUDED.categorias_publicacao,
          catalogo_sync_em=NOW(),
          catalogo_sync_total=EXCLUDED.catalogo_sync_total,
          catalogo_sync_publicados=EXCLUDED.catalogo_sync_publicados,
          catalogo_sync_sem_imagem=EXCLUDED.catalogo_sync_sem_imagem
    """, (cnpjloja, min_estoque, ",".join(categorias), len(produtos), publicados, sem_imagem))
    conn.commit()
    cur.close()
    _batch_cache_clear()
    return {"total": len(produtos), "publicados": publicados, "sem_imagem": sem_imagem}


@app.get("/painel/admin/lojas")
@admin_required
def admin_lojas():
    _ensure_catalog_admin_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.cnpjloja, u.razao, u.endereco, u.uf,
               g.lat, g.lng, g.geocoded_at,
               COALESCE(c.catalogo_publico, TRUE) AS catalogo_publico,
               COALESCE(c.estoque_min_publicacao, 5) AS estoque_min_publicacao,
               COALESCE(c.categorias_publicacao, 'todos') AS categorias_publicacao,
               c.catalogo_sync_em,
               COALESCE(c.catalogo_sync_total, 0) AS catalogo_sync_total,
               COALESCE(c.catalogo_sync_publicados, 0) AS catalogo_sync_publicados,
               COALESCE(c.catalogo_sync_sem_imagem, 0) AS catalogo_sync_sem_imagem
        FROM users u
        LEFT JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
        LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
        WHERE u.is_admin = FALSE
        ORDER BY u.razao
        """
    )
    lojas = cur.fetchall()
    cur.close()
    return render_template("admin_lojas.html", lojas=lojas, categorias_publicacao=_ADMIN_CATEGORIAS_PUBLICACAO)


@app.post("/painel/admin/lojas/catalogo-config")
@admin_required
def admin_loja_catalogo_config():
    _ensure_catalog_admin_schema()
    cnpjloja = (request.form.get("cnpjloja") or "").strip()
    if not cnpjloja:
        return redirect(url_for("admin_lojas"))
    min_estoque = max(0, int(request.form.get("estoque_min_publicacao") or 5))
    categorias = request.form.getlist("categorias_publicacao")
    if not categorias:
        categorias = ["todos"]
    categorias = _admin_parse_categorias(",".join(categorias))
    publico = request.form.get("catalogo_publico") == "1"
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO ecommerce_config_loja
          (cnpjloja, catalogo_publico, estoque_min_publicacao, categorias_publicacao)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (cnpjloja) DO UPDATE SET
          catalogo_publico=EXCLUDED.catalogo_publico,
          estoque_min_publicacao=EXCLUDED.estoque_min_publicacao,
          categorias_publicacao=EXCLUDED.categorias_publicacao,
          updated_at=NOW()
    """, (cnpjloja, publico, min_estoque, ",".join(categorias)))
    conn.commit()
    cur.close()
    _batch_cache_clear()
    flash("ConfiguraÃ§Ã£o do catÃ¡logo atualizada.", "success")
    return redirect(url_for("admin_lojas"))


@app.post("/painel/admin/lojas/catalogo-toggle")
@admin_required
def admin_loja_catalogo_toggle():
    _ensure_catalog_admin_schema()
    cnpjloja = (request.form.get("cnpjloja") or "").strip()
    ativo = request.form.get("ativo") == "1"
    if cnpjloja:
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ecommerce_config_loja (cnpjloja, catalogo_publico)
            VALUES (%s, %s)
            ON CONFLICT (cnpjloja) DO UPDATE SET catalogo_publico=EXCLUDED.catalogo_publico, updated_at=NOW()
        """, (cnpjloja, ativo))
        cur.execute(
            "SELECT COALESCE(estoque_min_publicacao, 5) AS min, COALESCE(categorias_publicacao, 'todos') AS categorias FROM ecommerce_config_loja WHERE cnpjloja=%s",
            (cnpjloja,),
        )
        cfg = cur.fetchone() or {"min": 5, "categorias": "todos"}
        conn.commit()
        cur.close()
        _batch_cache_clear()
        if ativo:
            stats = _sync_catalogo_loja_admin(cnpjloja, cfg.get("min") or 5, cfg.get("categorias") or "todos")
            flash(
                f"CatÃ¡logo habilitado. Sincronizados: {stats['publicados']} publicados, {stats['sem_imagem']} bloqueados por imagem.",
                "success",
            )
        else:
            flash("CatÃ¡logo ocultado dos consumidores.", "success")
    return redirect(url_for("admin_lojas"))


@app.post("/painel/admin/lojas/catalogo-sync")
@admin_required
def admin_loja_catalogo_sync():
    _ensure_catalog_admin_schema()
    cnpjloja = (request.form.get("cnpjloja") or "").strip()
    if not cnpjloja:
        return redirect(url_for("admin_lojas"))
    min_estoque = int(request.form.get("estoque_min_publicacao") or 5)
    categorias = request.form.get("categorias_publicacao") or "todos"
    stats = _sync_catalogo_loja_admin(cnpjloja, min_estoque, categorias)
    flash(
        f"SincronizaÃ§Ã£o concluÃ­da: {stats['publicados']} publicados, {stats['sem_imagem']} bloqueados por imagem.",
        "success",
    )
    return redirect(url_for("admin_lojas"))


@app.post("/painel/admin/geocodificar")
@admin_required
def admin_geocodificar():
    cnpjloja = (request.form.get("cnpjloja") or "").strip()
    if not cnpjloja:
        return redirect(url_for("admin_lojas"))

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT endereco, uf FROM users WHERE cnpjloja = %s LIMIT 1", (cnpjloja,)
    )
    u = cur.fetchone()
    cur.close()

    if not u:
        flash("Loja não encontrada.", "error")
        return redirect(url_for("admin_lojas"))

    lat, lng = nominatim_geocode(u["endereco2"] or u["endereco"], u["uf"])
    if lat:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ecommerce_lojas_geo (cnpjloja, lat, lng)
            VALUES (%s, %s, %s)
            ON CONFLICT (cnpjloja) DO UPDATE
              SET lat = EXCLUDED.lat, lng = EXCLUDED.lng, geocoded_at = NOW()
            """,
            (cnpjloja, lat, lng),
        )
        conn.commit()
        cur.close()
        flash(f"Geocodificado: {lat:.4f}, {lng:.4f}", "success")
    else:
        flash("Não foi possível geocodificar este endereço.", "error")

    return redirect(url_for("admin_lojas"))


@app.post("/painel/admin/geocodificar-todos")
@admin_required
def admin_geocodificar_todos():
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.cnpjloja, u.endereco, u.endereco2, u.uf
        FROM users u
        LEFT JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
        WHERE u.is_admin = FALSE AND (g.lat IS NULL OR g.cnpjloja IS NULL)
        LIMIT 50
        """
    )
    pendentes = cur.fetchall()
    cur.close()

    ok = fail = 0
    for u in pendentes:
        lat, lng = nominatim_geocode(u["endereco2"] or u["endereco"], u["uf"])
        if lat:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO ecommerce_lojas_geo (cnpjloja, lat, lng)
                VALUES (%s, %s, %s)
                ON CONFLICT (cnpjloja) DO UPDATE
                  SET lat = EXCLUDED.lat, lng = EXCLUDED.lng, geocoded_at = NOW()
                """,
                (u["cnpjloja"], lat, lng),
            )
            conn.commit()
            cur.close()
            ok += 1
        else:
            fail += 1

    flash(f"Geocodificados: {ok} OK, {fail} falhas.", "success")
    return redirect(url_for("admin_lojas"))


# ─── ADMIN: LOJAS VITRINE (imagens públicas das lojas) ────────────────────────

@app.post("/painel/admin/lojas-vitrine/auto-vincular")
@admin_required
def admin_lojas_vitrine_auto_vincular():
    """Vincula automaticamente cidades da vitrine às lojas cadastradas pelo endereço."""
    import unicodedata, re

    def _norm(txt):
        """Minúsculo sem acentos."""
        if not txt:
            return ""
        nfkd = unicodedata.normalize("NFKD", txt.lower())
        return "".join(c for c in nfkd if not unicodedata.combining(c))

    _ensure_lojas_vitrine_schema()
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT cnpjloja, razao, endereco, uf FROM users WHERE is_admin = FALSE ORDER BY razao")
    lojas = cur.fetchall()

    cur.execute("SELECT DISTINCT cidade FROM ecommerce_lojas_vitrine WHERE cidade IS NOT NULL")
    cidades_cloudinary = [r["cidade"] for r in cur.fetchall()]
    lojas_ik = _load_imagekit_lojas()
    cidades_ik = [l["cidade"] for l in lojas_ik if l.get("cidade")]
    todas_cidades = list({c for c in cidades_cloudinary + cidades_ik if c})

    def _salvar(cidade_raw, cnpjloja):
        cur.execute(
            "INSERT INTO ecommerce_vitrine_cnpj_map (cidade, cnpjloja) VALUES (%s,%s) ON CONFLICT (cidade) DO UPDATE SET cnpjloja=EXCLUDED.cnpjloja",
            (cidade_raw, cnpjloja),
        )
        cur.execute(
            "UPDATE ecommerce_lojas_vitrine SET cnpjloja=%s WHERE cidade=%s AND (cnpjloja IS NULL OR cnpjloja!=%s)",
            (cnpjloja, cidade_raw, cnpjloja),
        )
        cur.execute(
            "UPDATE ecommerce_lojas_vitrine_cliques SET cnpjloja=%s WHERE cidade=%s AND cnpjloja IS NULL",
            (cnpjloja, cidade_raw),
        )

    vinculados = 0
    sem_match = []

    for cidade_raw in todas_cidades:
        # "São Paulo/SP Arpoador" → nome="São Paulo", uf="SP"
        partes = cidade_raw.split("/")
        nome_completo = partes[0].strip()                        # "Itatiba 02"
        uf_str        = partes[1].strip()[:2].upper() if len(partes) > 1 else None

        # separa número do final: "Itatiba 02" → base="Itatiba", num=2
        m = re.match(r"^(.*?)\s+(\d+)$", nome_completo)
        nome_base = m.group(1).strip() if m else nome_completo
        num_idx   = int(m.group(2)) if m else 1            # 1-based

        nome_norm = _norm(nome_base)

        # acha TODAS as lojas que batem na cidade (sem acentos)
        candidatos = []
        for loja in lojas:
            end_norm = _norm(loja["endereco"] or "")
            uf_loja  = (loja["uf"] or "").strip().upper()  # strip: alguns têm "  " em vez de "SP"
            if nome_norm in end_norm:
                if not uf_str or not uf_loja or uf_str == uf_loja:
                    candidatos.append(loja)

        if not candidatos:
            sem_match.append(cidade_raw)
            continue

        # para cidade numerada escolhe pelo índice; se só 1 candidato, usa ele
        idx = min(num_idx, len(candidatos)) - 1
        match = candidatos[idx]
        _salvar(cidade_raw, match["cnpjloja"])
        vinculados += 1

    conn.commit()
    cur.close()

    msg = f"{vinculados} cidade(s) vinculada(s) automaticamente."
    if sem_match:
        msg += f" Sem match: {', '.join(sem_match)}."
    flash(msg, "success" if not sem_match else "info")
    return redirect(url_for("admin_lojas_vitrine"))

@app.route("/painel/admin/lojas-vitrine", methods=["GET", "POST"])
@admin_required
def admin_lojas_vitrine():
    _ensure_lojas_vitrine_schema()
    conn = db()
    cur = conn.cursor()

    if request.method == "POST":
        cidade    = (request.form.get("cidade") or "").strip()
        endereco  = (request.form.get("endereco") or "").strip()
        telefone  = (request.form.get("telefone") or "").strip()
        whatsapp  = (request.form.get("whatsapp") or "").strip()
        ordem     = int(request.form.get("ordem") or 0)
        cnpjloja  = (request.form.get("cnpjloja") or "").strip() or None
        imagem_url = None

        if not cidade or not endereco:
            flash("Cidade e endereço são obrigatórios.", "danger")
            return redirect(url_for("admin_lojas_vitrine"))

        file = request.files.get("file")
        if file and file.filename:
            if not _CLOUDINARY_OK:
                flash("Cloudinary não configurado — sem upload de imagem.", "warning")
            else:
                try:
                    result = cloudinary.uploader.upload(
                        file,
                        folder="lojas_vitrine",
                        resource_type="image",
                    )
                    imagem_url = result.get("secure_url")
                except Exception as e:
                    flash(f"Erro no upload da imagem: {e}", "danger")
                    return redirect(url_for("admin_lojas_vitrine"))

        cur.execute(
            "INSERT INTO ecommerce_lojas_vitrine (cidade, endereco, telefone, whatsapp, imagem_url, ordem, cnpjloja) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (cidade, endereco, telefone, whatsapp, imagem_url, ordem, cnpjloja),
        )
        conn.commit()
        flash("Loja adicionada com sucesso.", "success")
        return redirect(url_for("admin_lojas_vitrine"))

    cur.execute("SELECT id, cidade, endereco, telefone, whatsapp, imagem_url, ordem, cnpjloja FROM ecommerce_lojas_vitrine ORDER BY ordem, cidade")
    lojas_sb = cur.fetchall()
    cur.execute("SELECT cnpjloja, razao FROM users WHERE is_admin = FALSE ORDER BY razao")
    todas_lojas = cur.fetchall()
    cur.close()

    lojas_ik = _load_imagekit_lojas(include_file_id=True)

    return render_template("admin_lojas_vitrine.html", lojas_sb=lojas_sb, lojas_ik=lojas_ik, todas_lojas=todas_lojas)


@app.route("/painel/admin/lojas-vitrine/edit/<int:loja_id>", methods=["GET", "POST"])
@admin_required
def admin_lojas_vitrine_edit(loja_id):
    _ensure_lojas_vitrine_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT cnpjloja, razao FROM users WHERE is_admin = FALSE ORDER BY razao")
    todas_lojas = cur.fetchall()

    if request.method == "POST":
        cidade    = (request.form.get("cidade") or "").strip()
        endereco  = (request.form.get("endereco") or "").strip()
        telefone  = (request.form.get("telefone") or "").strip()
        whatsapp  = (request.form.get("whatsapp") or "").strip()
        ordem     = int(request.form.get("ordem") or 0)
        cnpjloja  = (request.form.get("cnpjloja") or "").strip() or None

        imagem_url = request.form.get("imagem_url_atual") or None
        file = request.files.get("file")
        if file and file.filename and _CLOUDINARY_OK:
            try:
                result = cloudinary.uploader.upload(file, folder="lojas_vitrine", resource_type="image")
                imagem_url = result.get("secure_url")
            except Exception as e:
                flash(f"Erro no upload: {e}", "danger")
                return redirect(url_for("admin_lojas_vitrine_edit", loja_id=loja_id))

        cur.execute(
            "UPDATE ecommerce_lojas_vitrine SET cidade=%s, endereco=%s, telefone=%s, whatsapp=%s, imagem_url=%s, ordem=%s, cnpjloja=%s WHERE id=%s",
            (cidade, endereco, telefone, whatsapp, imagem_url, ordem, cnpjloja, loja_id),
        )
        # retroativamente vincula cliques já gravados sem cnpjloja para esta cidade
        if cnpjloja and cidade:
            cur.execute(
                "UPDATE ecommerce_lojas_vitrine_cliques SET cnpjloja=%s WHERE cidade=%s AND cnpjloja IS NULL",
                (cnpjloja, cidade),
            )
        conn.commit()
        flash("Loja atualizada.", "success")
        return redirect(url_for("admin_lojas_vitrine"))

    cur.execute("SELECT id, cidade, endereco, telefone, whatsapp, imagem_url, ordem, cnpjloja FROM ecommerce_lojas_vitrine WHERE id=%s", (loja_id,))
    loja = cur.fetchone()
    cur.close()
    if not loja:
        flash("Loja não encontrada.", "danger")
        return redirect(url_for("admin_lojas_vitrine"))
    return render_template("admin_lojas_vitrine_edit.html", loja=loja, todas_lojas=todas_lojas)


@app.post("/painel/admin/lojas-vitrine/delete/<int:loja_id>")
@admin_required
def admin_lojas_vitrine_delete(loja_id):
    _ensure_lojas_vitrine_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM ecommerce_lojas_vitrine WHERE id=%s", (loja_id,))
    conn.commit()
    cur.close()
    flash("Loja removida.", "success")
    return redirect(url_for("admin_lojas_vitrine"))


@app.route("/painel/admin/lojas-vitrine/ik-edit/<file_id>", methods=["GET", "POST"])
@admin_required
def admin_lojas_vitrine_ik_edit(file_id):
    """Edita metadados de uma loja do ImageKit."""
    _ensure_lojas_vitrine_schema()
    file_id = (file_id or "").strip()
    if not file_id:
        return redirect(url_for("admin_lojas_vitrine"))

    if request.method == "POST":
        cidade   = (request.form.get("cidade") or "").strip()
        endereco = (request.form.get("endereco") or "").strip()
        telefone = (request.form.get("telefone") or "").strip()
        whatsapp = (request.form.get("whatsapp") or "").strip()
        cnpjloja = (request.form.get("cnpjloja") or "").strip() or None
        try:
            _ik_update_metadata(file_id, cidade, endereco, telefone, whatsapp)
            flash("Loja atualizada no ImageKit.", "success")
        except Exception as e:
            flash(f"Erro ao atualizar: {e}", "danger")
        # salva/atualiza mapeamento cidade→cnpjloja para rastreio de cliques
        if cidade:
            conn2 = db()
            cur2 = conn2.cursor()
            if cnpjloja:
                cur2.execute(
                    """
                    INSERT INTO ecommerce_vitrine_cnpj_map (cidade, cnpjloja)
                    VALUES (%s, %s)
                    ON CONFLICT (cidade) DO UPDATE SET cnpjloja = EXCLUDED.cnpjloja
                    """,
                    (cidade, cnpjloja),
                )
                # retroativamente vincula cliques gravados sem cnpjloja
                cur2.execute(
                    "UPDATE ecommerce_lojas_vitrine_cliques SET cnpjloja=%s WHERE cidade=%s AND cnpjloja IS NULL",
                    (cnpjloja, cidade),
                )
            else:
                cur2.execute("DELETE FROM ecommerce_vitrine_cnpj_map WHERE cidade=%s", (cidade,))
            conn2.commit()
            cur2.close()
        return redirect(url_for("admin_lojas_vitrine"))

    # GET — busca dados atuais
    lojas_ik = _load_imagekit_lojas(include_file_id=True)
    loja = next((l for l in lojas_ik if l.get("fileId") == file_id), None)
    if not loja:
        flash("Loja não encontrada no ImageKit.", "danger")
        return redirect(url_for("admin_lojas_vitrine"))
    # cnpjloja já vinculado (se existir)
    conn2 = db()
    cur2 = conn2.cursor()
    cur2.execute("SELECT cnpjloja FROM ecommerce_vitrine_cnpj_map WHERE cidade=%s", (loja.get("cidade", ""),))
    mapa = cur2.fetchone()
    cur2.execute("SELECT cnpjloja, razao FROM users WHERE is_admin = FALSE ORDER BY razao")
    todas_lojas = cur2.fetchall()
    cur2.close()
    loja_cnpjloja = mapa["cnpjloja"] if mapa else None
    return render_template("admin_lojas_vitrine_ik_edit.html", loja=loja, file_id=file_id,
                           todas_lojas=todas_lojas, loja_cnpjloja=loja_cnpjloja)


@app.post("/painel/admin/lojas-vitrine/ik-delete/<file_id>")
@admin_required
def admin_lojas_vitrine_ik_delete(file_id):
    """Remove uma loja do ImageKit."""
    file_id = (file_id or "").strip()
    if file_id:
        try:
            _ik_delete_file(file_id)
            flash("Loja removida do ImageKit.", "success")
        except Exception as e:
            flash(f"Erro ao excluir: {e}", "danger")
    return redirect(url_for("admin_lojas_vitrine"))


@app.post("/painel/admin/operar-loja")
@admin_required
def admin_operar_loja():
    cnpjloja = (request.form.get("cnpjloja") or "").strip()
    if cnpjloja:
        conn = db()
        cur = conn.cursor()
        cur.execute(
            "SELECT cnpjloja, razao, uf, endereco FROM users WHERE cnpjloja = %s LIMIT 1",
            (cnpjloja,),
        )
        u = cur.fetchone()
        cur.close()
        if u:
            session["cnpjloja"] = u["cnpjloja"]
            session["razao"]    = u["razao"]
            session["uf"]       = u["uf"]
            session["endereco"] = u["endereco"]
    return redirect(url_for("precificador"))


# ─── HEALTH ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return jsonify({"ok": True, "ts": datetime.now(timezone.utc).isoformat()})


# ─── ANVISA INTEGRATION ───────────────────────────────────────────────────────
#
# Fluxo:
#   1. Admin clica "Sincronizar ANVISA" → POST /painel/admin/anvisa-sync
#   2. Worker em thread busca todos os produtos em estoque na ANVISA (lote)
#   3. Salva em anvisa_cache (TTL 90 dias)
#   4. produto_detalhe lê do cache → passa var `anvisa` ao template
#   5. /bula/<chave> faz proxy do PDF com Authorization: Guest

_TIPO_COSMETICO = re.compile(
    r"\b(fps|spf|protetor|solar|bb.?cream|cc.?cream|hidratante|clareador|"
    r"base\b|sérum|serum|loção|locao|tônico|tonico|esfoliante|mascara.facial|"
    r"shampoo|condicionador|sabonete|creme.facial|antiacne|antiidade|"
    r"demaquilante|primer|blush|batom|bronzeador|autobronzeador|"
    r"anasol|unispray)\b",
    re.IGNORECASE,
)
_TIPO_SUPLEMENTO = re.compile(
    r"\b(whey|proteina|creatina|bcaa|glutamina|albumina|colageno|colágeno|"
    r"termogenico|termogênico|pre.treino|omega|ômega|probiotico|probiótico|"
    r"fibras?\b|maltodextrina|dextrose|aminoacido|aminoácido|"
    r"pronabol|ricosol|goodvit|vit.?natu|vitnatu)\b",
    re.IGNORECASE,
)
_TIPO_MEDICAMENTO = re.compile(
    r"\b(\d+\s*mg|\d+\s*mcg|\d+\s*ui|comprimido|capsula|cápsula|"
    r"xarope|ampola|injetavel|injetável|solucao|solução|sublingual|"
    r"colirio|colírio|supositório|supositorio)\b",
    re.IGNORECASE,
)


def _classificar_produto(nome: str) -> str:
    """Retorna 'cosmetico', 'suplemento', 'medicamento' ou '' (desconhecido)."""
    if not nome:
        return ""
    if _TIPO_COSMETICO.search(nome):
        return "cosmetico"
    if _TIPO_SUPLEMENTO.search(nome):
        return "suplemento"
    if _TIPO_MEDICAMENTO.search(nome):
        return "medicamento"
    return ""

def _anvisa_schema():
    conn = db()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS anvisa_cache (
            id              SERIAL PRIMARY KEY,
            chave           TEXT UNIQUE NOT NULL,
            encontrado      BOOLEAN NOT NULL DEFAULT FALSE,
            nome_anvisa     TEXT,
            laboratorio     TEXT,
            situacao        TEXT,
            principio_ativo TEXT,
            url_bula        TEXT,
            serve_para      TEXT,
            como_usar       TEXT,
            alertas         TEXT,
            id_produto      INTEGER,
            criado_em       TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS id_produto INTEGER")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS tarja TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS jwt_bula TEXT")
    conn.commit()
    cur.close()


_ANVISA_STOP_WORDS = {
    "com","de","do","da","dos","das","para","por","em","e","ou",
    "mg","mcg","ml","ui","gr","cp","caps","comp","tab","un","und",
    "sol","solucao","injetavel","oral","topico","cutaneo","subl",
    "cpr","drg","amp","fco","bsa","gel","crem","pom","sup","xpe",
    "rev","retard","ret","iny","inf","efervescente","spray",
    "comprimido","comprimidos","capsula","capsulas","softgel","gelcap",
    "dragea","drageias","xarope","pomada","creme","supositorio",
    "injecao","injetavel","solucao","suspensao","emulsao","granulado",
    "pastilha","pastilhas","sublingual","transdermico","inalacao",
    "revestido","revestidos","liberacao","prolongada","retardada",
    "efervescente","mastigavel","dispersivel","orodisp","orodispersivel",
    # Prefixos de sal farmacológico (nunca são o nome ANVISA)
    "cloridrato","bromidrato","dicloridrato","hemitartarato","hemifumarato",
    "maleato","fumarato","succinato","besilato","tartarato",
    "monoidratado","monoidratada","hemif","succ",
    # Nomes de laboratório que aparecem como 2ª palavra no estoque
    "germed","vitamedic","biolab","globo","greenbios","uniphar",
    "farmax","quimica","bellaphytus","rioquimica","medley","sandoz",
    "torrent","teuto","eurofarma","prati","donaduzzi","neo","geolab",
}

def _anvisa_chave(nome):
    """First 3 significant words of product name, uppercase (cache key)."""
    words = []
    for w in re.sub(r"[^\w\s]", " ", nome or "").upper().split():
        if (w.lower() in _ANVISA_STOP_WORDS
                or any(c.isdigit() for c in w)
                or len(w) < 4):
            continue
        words.append(w)
        if len(words) >= 2:
            break
    return " ".join(words)


def _anvisa_strip_html(frag):
    t = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", frag, flags=re.IGNORECASE)
    t = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"<[^>]+>", " ", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&apos;", "'"))
    return re.sub(r"\s+", " ", t).strip()[:2000]


_ANVISA_SECOES = {
    "serve_para": [
        r"PARA\s+QUE\s+ESTE\s+MEDICAMENTO\s+[EÉ]\s+INDICAD[OA]",
        r"PARA\s+QUE\s+[EÉ]\s+INDICAD[OA]",
        r"INDICA[CÇ][OÃ][EO]S?\b",
    ],
    "como_usar": [
        r"COMO\s+(?:DEVO\s+)?USAR\s+ESTE\s+MEDICAMENTO",
        r"POSOLOGIA\s+E\s+MODO\s+DE\s+(?:USAR|USO)",
        r"COMO\s+(?:USAR|USO)\s+ESTE",
    ],
    "alertas": [
        r"QUANDO\s+N[ÃA]O\s+DEVO\s+USAR",
        r"QUAIS\s+OS\s+(?:RISCOS|MALES|CUIDADOS)",
        r"CONTRAINDICA[CÇ][OÃ][EO]S?\b",
        r"ADVERTÊNCIAS?\s+E\s+PRECAU[CÇ][OÃ][EO]S",
    ],
}


def _anvisa_extrair_secao(html, padroes):
    for pat in padroes:
        m = re.search(
            pat + r"[^<]{0,300}</[^>]+>([\s\S]*?)(?=<(?:h[1-6]|p[^>]*class)|\Z)",
            html, re.IGNORECASE,
        )
        if m:
            text = _anvisa_strip_html(m.group(1))
            if len(text) > 25:
                return text
    return None


# Tarja Preta — controle especial (Portaria 344/98 listas A/B/C)
_TARJA_PRETA_RE = re.compile(
    r"controle\s+especial"
    r"|notifica[cç][aã]o\s+de\s+receita"
    r"|receita\s+de\s+controle"
    r"|(?:lista|port(?:aria)?\s*)\s*(?:344|[AB]\d)"
    r"|tarja\s+preta"
    r"|psicotr[oó]pico",
    re.IGNORECASE,
)

# Tarja Vermelha — exige prescrição simples
_TARJA_VERMELHA_RE = re.compile(
    r"venda\s+sob\s+prescri[cç][aã]o\s+m[eé]dica"
    r"|uso\s+sob\s+prescri[cç][aã]o\s+m[eé]dica"
    r"|somente\s+(?:com|sob)\s+prescri[cç][aã]o"
    r"|tarja\s+vermelha"
    r"|medicamento\s+sujeito\s+a\s+prescri[cç][aã]o",
    re.IGNORECASE,
)

# Fallback: nomes de princípios ativos/classes que são sempre tarja vermelha no Brasil
# Usado quando anvisa_cache não tem o produto cadastrado
_NOME_TARJA_VERMELHA_RE = re.compile(
    # Antidiabéticos orais
    r"\bglibenclamida\b|\bgliclazida\b|\bglimepirida\b|\bglipizida\b"
    r"|\bmetformina\b|\binsulina\b|\bsitagliptina\b|\bempagliflozina\b|\bdapagliflozina\b"
    # Anti-hipertensivos / cardiovasculares
    r"|\batenolol\b|\bmetoprolol\b|\bpropranolol\b|\bcarvedilol\b|\bbisoprolol\b"
    r"|\blosartana\b|\bvalsartana\b|\birbesartana\b|\bolmesartana\b|\bcandesartana\b"
    r"|\benalapril\b|\bcaptopril\b|\bramipril\b|\blisinopril\b|\bperindopril\b"
    r"|\bamlodipino\b|\bnifedipino\b|\bdiltiazem\b|\bverapamil\b"
    r"|\bhidroclorotiazida\b|\bfurosemida\b|\bespironolactona\b|\bindapamida\b"
    r"|\batorvastatina\b|\bsinvastatina\b|\brosuvastatina\b|\bpravastatina\b|\bfluvastatina\b"
    r"|\bdigoxina\b|\bamiodarona\b|\bwarfarina\b|\bclopidogrel\b"
    # Antibióticos (todos precisam de receita no BR)
    r"|\bamoxicilina\b|\bampicilina\b|\bcefalexina\b|\bcefadroxila\b|\bcefaclor\b"
    r"|\bazitromicina\b|\bclaritromicina\b|\beritromicina\b"
    r"|\bciprofloxacino\b|\blevofloxacino\b|\bnorfloxacino\b|\bofloxacino\b"
    r"|\bmetronidazol\b|\btinidazol\b|\bsulfametoxazol\b|\btrimetoprim\b"
    r"|\btetraciclina\b|\bdoxiciclina\b|\bminociclina\b"
    # Antidepressivos / ansiolíticos (não controlados)
    r"|\bfluoxetina\b|\bsertralina\b|\bescitalopram\b|\bcitalopram\b"
    r"|\bparoxetina\b|\bvenlafaxina\b|\bdesvenlafaxina\b|\bduloxetina\b"
    r"|\bamitriptilina\b|\bnortriptilina\b|\bimipramina\b"
    r"|\bbuspirona\b|\bhydroxizina\b|\bhidroxizina\b"
    # Tireóide
    r"|\blevotiroxina\b|\bmetimazol\b|\bpropiltiouracil\b"
    # Corticoides (uso sistêmico)
    r"|\bprednisona\b|\bprednisolona\b|\bdexametasona\b|\bbetametasona\b"
    r"|\bmetilprednisolona\b|\btriancinolona\b"
    # Antiulcerosos de prescrição
    r"|\bomeprazol\b|\bpantoprazol\b|\blansoprazol\b|\besomeprazol\b|\brabeprazol\b"
    # Anticonvulsivantes
    r"|\bcarbamazepina\b|\bfenitoina\b|\bvalproato\b|\btopiramate?\b|\blamotrigina\b"
    # Broncodilatadores sistêmicos
    r"|\bsalbutamol\b|\bformoterol\b|\bsalmeterol\b|\btiotropio\b|\bbudesonida\b"
    # Outros comuns
    r"|\bisossorbida\b|\bnitroglicerina\b|\btrimetazidina\b"
    r"|\balopurinol\b|\bcolchicina\b",
    re.IGNORECASE,
)


def _detectar_tarja(anvisa: dict) -> str | None:
    """Retorna 'preta', 'vermelha' ou None (sem tarja / OTC)."""
    if not anvisa:
        return None
    # Campo direto salvo pelo worker (API ANVISA)
    tarja_bd = (anvisa.get("tarja") or "").strip().lower()
    if tarja_bd in ("preta", "vermelha"):
        return tarja_bd
    # Detecta do texto da bula
    textos = " ".join(filter(None, [
        anvisa.get("alertas") or "",
        anvisa.get("como_usar") or "",
        anvisa.get("nome_anvisa") or "",
        anvisa.get("principio_ativo") or "",
    ]))
    if _TARJA_PRETA_RE.search(textos):
        return "preta"
    if _TARJA_VERMELHA_RE.search(textos):
        return "vermelha"
    return None


def _requer_receita(anvisa: dict) -> bool:
    return _detectar_tarja(anvisa) is not None


def _marcar_tarja_batch(produtos: list, conn) -> list:
    """Adiciona requer_receita=True/False a cada produto da lista (in-place + retorna)."""
    if not produtos:
        return produtos
    nomes = [p.get("nome") or "" for p in produtos]
    chaves_map: dict[str, list[int]] = {}   # chave → índices na lista
    for i, nome in enumerate(nomes):
        ch = _anvisa_chave(nome)
        if ch:
            chaves_map.setdefault(ch, []).append(i)

    # Inicializa tudo como False
    for p in produtos:
        p["requer_receita"] = False

    if not chaves_map:
        return produtos

    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT chave, alertas, como_usar, nome_anvisa, principio_ativo, tarja "
            "FROM anvisa_cache WHERE chave = ANY(%s) AND encontrado = TRUE",
            (list(chaves_map.keys()),),
        )
        for row in cur.fetchall():
            tarja = _detectar_tarja(dict(row))
            for idx in chaves_map.get(row["chave"], []):
                produtos[idx]["tarja"] = tarja
                produtos[idx]["requer_receita"] = tarja is not None
    except Exception:
        pass
    finally:
        cur.close()

    # Fallback: produtos que o anvisa_cache não detectou — checar pelo nome
    for p in produtos:
        if not p.get("requer_receita"):
            nome = p.get("nome") or ""
            if _NOME_TARJA_VERMELHA_RE.search(nome):
                p["tarja"] = "vermelha"
                p["requer_receita"] = True

    return produtos


# Remove apenas ruído claro linha a linha — preserva o máximo de conteúdo
_BULA_LINHA_RUIDO = re.compile(
    r"^\s*\d{2}/\d{2}/\d{4}"           # começa com data dd/mm/aaaa
    r"|^\s*\d{6,}/\d{2}-\d"            # código regulatório 0350699/21-1
    r"|^\s*\(\d{4,}\)\s*$"             # só código numérico (10452)
    r"|^\s*(?:N/A[\s,]*){2,}\s*$"      # só N/As repetidos
    r"|^\s*\d+[\s.]*$"                 # só número solto
    r"|^\s*\d+\s*/\s*\d+\s*$",         # só X/Y
    re.IGNORECASE,
)

# Substitui tokens de ruído inline mas mantém o resto da linha
_BULA_TOKEN_RUIDO = re.compile(
    r"\b(VPS|VP)\b"
    r"|\bRDC\s*\d+"
    r"|Altera[cç][aã]o\s+de\s+(?:Texto|Bula)\b"
    r"|Notifica[cç][aã]o\s+de\b"
    r"|Dizeres\s+Legais\b"
    r"|\(\d{5,}\)"
    r"|(?:\bN/A\b[\s,]*){3,}",
    re.IGNORECASE,
)


def _normalizar_bula(texto: str) -> str:
    """Remove ruído regulatório do texto de bula preservando o máximo de conteúdo."""
    if not texto:
        return ""

    linhas = texto.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    linhas_ok = []
    for linha in linhas:
        s = linha.strip()
        if not s:
            linhas_ok.append("")
            continue
        if _BULA_LINHA_RUIDO.match(s):
            continue                          # descarta a linha inteira
        s = _BULA_TOKEN_RUIDO.sub("", s).strip()
        # Remove prefixo "9. " ou "4. " que sobra no início da linha
        s = re.sub(r"^\d+\s*[.\)]\s+(?=[A-ZÁÉÍÓÚÃÕA-Za-z])", "", s).strip()
        if s:
            linhas_ok.append(s)

    # Normaliza quebras múltiplas e junta parágrafos
    texto_limpo = "\n".join(linhas_ok)
    texto_limpo = re.sub(r"\n{3,}", "\n\n", texto_limpo).strip()

    # Quebra em parágrafos (linhas duplas) e limpa cada um
    paragrafos = []
    for bloco in texto_limpo.split("\n\n"):
        p = re.sub(r"\n", " ", bloco).strip()
        p = re.sub(r"\s{2,}", " ", p)
        # Remove cabeçalhos de seção que ficam colados ao conteúdo como prefixo (artefato de PDF)
        p = re.sub(
            r"^(?:ESTE\s+MEDICAMENTO\s*[?!]?\s*"
            r"|O\s+QUE\s+[EÉ]\s+ESTE\s+MEDICAMENTO\s*[?!]?\s*"
            r"|COMO\s+(?:DEVO\s+)?USAR\s+ESTE\s+MEDICAMENTO\s*[?!]?\s*"
            r"|QUANDO\s+N[AÃ]O\s+DEVO\s+USAR\s+ESTE\s+MEDICAMENTO\s*[?!]?\s*"
            r"|QUAIS\s+OS\s+MALES\s+QUE\s+ESTE\s+MEDICAMENTO\s*[?!]?\s*)",
            "", p, flags=re.IGNORECASE,
        ).strip()
        # Remove ? ! : residuais do início (artefatos de PDF)
        p = re.sub(r"^[?!\s:]+", "", p).strip()
        if len(p) >= 20:
            paragrafos.append(p)

    resultado = "\n\n".join(paragrafos)
    limite = 4000
    if len(resultado) <= limite:
        return resultado
    # Corta no último ponto final antes do limite para não quebrar frase no meio
    corte = resultado.rfind(". ", 0, limite)
    if corte > limite // 2:
        return resultado[: corte + 1]
    return resultado[:limite]


def _anvisa_salvar(chave, dados):
    try:
        conn = db()
        cur  = conn.cursor()
        cur.execute(
            """
            INSERT INTO anvisa_cache
              (chave, encontrado, nome_anvisa, laboratorio, situacao,
               principio_ativo, url_bula, serve_para, como_usar, alertas,
               id_produto, tarja, jwt_bula, criado_em)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (chave) DO UPDATE SET
              encontrado=EXCLUDED.encontrado, nome_anvisa=EXCLUDED.nome_anvisa,
              laboratorio=EXCLUDED.laboratorio, situacao=EXCLUDED.situacao,
              principio_ativo=EXCLUDED.principio_ativo, url_bula=EXCLUDED.url_bula,
              serve_para=EXCLUDED.serve_para, como_usar=EXCLUDED.como_usar,
              alertas=EXCLUDED.alertas, id_produto=EXCLUDED.id_produto,
              tarja=EXCLUDED.tarja, jwt_bula=EXCLUDED.jwt_bula,
              criado_em=NOW()
            """,
            (
                chave,
                dados.get("encontrado", False),
                dados.get("nome_anvisa"),
                dados.get("laboratorio"),
                dados.get("situacao"),
                dados.get("principio_ativo"),
                dados.get("url_bula"),
                dados.get("serve_para"),
                dados.get("como_usar"),
                dados.get("alertas"),
                dados.get("id_produto"),
                dados.get("tarja"),
                dados.get("jwt_bula"),
            ),
        )
        conn.commit()
        cur.close()
    except Exception:
        pass


@app.get("/api/anvisa-info")
def api_anvisa_info():
    """Return ANVISA product info by product name. Uses 30-day cache."""
    _anvisa_schema()
    nome = (request.args.get("nome") or "").strip()
    if len(nome) < 3:
        return jsonify({"ok": False, "erro": "nome muito curto"})

    chave = _anvisa_chave(nome)
    if not chave:
        return jsonify({"ok": False, "erro": "sem palavras significativas"})

    conn = db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT * FROM anvisa_cache WHERE chave=%s AND criado_em > NOW() - INTERVAL '30 days'",
        (chave,),
    )
    cached = cur.fetchone()
    cur.close()

    if cached:
        d = dict(cached)
        if not d["encontrado"]:
            return jsonify({"ok": False, "nao_encontrado": True, "chave": chave})
        return jsonify({"ok": True, **{
            k: d.get(k) for k in
            ("nome_anvisa", "laboratorio", "situacao", "principio_ativo",
             "url_bula", "serve_para", "como_usar", "alertas")
        }})

    # Busca via subprocess com arquivos temporários — evita WinError 5 no Windows
    import subprocess as _sp
    import tempfile
    import uuid as _uuid
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_anvisa_pw_worker.py")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        inp = f.name
        f.write(chave + "\n")
    out = os.path.join(tempfile.gettempdir(), f"anvisa_{_uuid.uuid4().hex}.jsonl")
    dados = {}
    try:
        proc = _sp.Popen(
            [sys.executable, worker, "--input", inp, "--output", out],
            stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        proc.wait(timeout=90)
        if os.path.exists(out):
            lines = [l.strip() for l in open(out, encoding="utf-8") if l.strip()]
            if lines:
                dados = json.loads(lines[-1]).get("dados") or {}
    except Exception:
        dados = {}
    finally:
        for p in (inp, out):
            try:
                os.unlink(p)
            except Exception:
                pass

    if dados:
        threading.Thread(target=_anvisa_salvar, args=(chave, dados), daemon=True).start()

    if not dados.get("encontrado"):
        return jsonify({"ok": False, "nao_encontrado": True, "chave": chave})

    return jsonify({"ok": True, **{
        k: dados.get(k) for k in
        ("nome_anvisa", "laboratorio", "situacao", "principio_ativo",
         "url_bula", "serve_para", "como_usar", "alertas")
    }})


_ANVISA_PDF_HEADERS = {
    "Authorization": "Guest",
    "Accept": "application/pdf,*/*",
    "Referer": "https://consultas.anvisa.gov.br/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


def _anvisa_fetch_jwt(chave, id_produto):
    """Busca JWT (idBulaPacienteProtegido) da ANVISA para produto já conhecido."""
    try:
        import requests as _rq
        q = urllib.parse.quote(chave)
        r = _rq.get(
            f"https://consultas.anvisa.gov.br/api/consulta/bulario"
            f"?column=PRODUTO&count=20&filter[nomeProduto]={q}&order=asc&page=1",
            timeout=15,
            headers={**_ANVISA_PDF_HEADERS, "Accept": "application/json"},
        )
        if not r.ok:
            return None
        items = r.json().get("content") or r.json().get("data") or []
        for item in items:
            if item.get("idProduto") == id_produto:
                return item.get("idBulaPacienteProtegido") or None
        if items:
            return items[0].get("idBulaPacienteProtegido") or None
    except Exception:
        pass
    return None


@app.get("/bula/<chave>")
def bula_download(chave):
    """Download bula PDF direto da ANVISA (proxy) ou redireciona para página do produto."""
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT url_bula, jwt_bula, id_produto FROM anvisa_cache WHERE chave=%s AND encontrado=TRUE",
        (chave,),
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return redirect(
            "https://consultas.anvisa.gov.br/#/bulario?nomeProduto="
            + urllib.parse.quote(chave)
        )

    jwt_bula = row["jwt_bula"]

    # jwt_bula ausente (sync antigo não salvava) — busca da ANVISA e guarda no cache
    if not jwt_bula and row["id_produto"]:
        jwt_bula = _anvisa_fetch_jwt(chave, row["id_produto"])
        if jwt_bula:
            try:
                c2 = db(); cu2 = c2.cursor()
                cu2.execute("UPDATE anvisa_cache SET jwt_bula=%s WHERE chave=%s",
                            (jwt_bula, chave))
                c2.commit(); cu2.close()
            except Exception:
                pass

    if jwt_bula:
        pdf_url = (
            "https://consultas.anvisa.gov.br/api/consulta/medicamentos"
            f"/arquivo/bula/parecer/{jwt_bula}/?Authorization="
        )
        try:
            import requests as _rq
            r = _rq.get(pdf_url, timeout=30, headers=_ANVISA_PDF_HEADERS)
            if r.ok and len(r.content) > 1000:
                safe = re.sub(r"[^a-z0-9_-]", "_", chave.lower())
                return Response(
                    r.content,
                    mimetype="application/pdf",
                    headers={
                        "Content-Disposition": f'inline; filename="bula_{safe}.pdf"',
                        "Content-Length": str(len(r.content)),
                    },
                )
        except Exception:
            pass

    # Fallback: redireciona para a página do produto no site ANVISA
    url_bula = row["url_bula"]
    if not url_bula:
        return redirect(
            "https://consultas.anvisa.gov.br/#/bulario?nomeProduto="
            + urllib.parse.quote(chave)
        )
    return redirect(url_bula)


# ─── ADMIN: ANVISA BATCH SYNC ─────────────────────────────────────────────────

_anvisa_sync_prog: dict = {"rodando": False, "total": 0, "feitos": 0, "ok": 0, "falha": 0}


def _anvisa_sync_worker(nomes: list):
    """Worker de sincronização em batch. Usa arquivos temporários para IPC com o
    subprocess Playwright — evita WinError 5 (named pipe denied) no Windows."""
    import subprocess as _sp
    import tempfile
    import uuid as _uuid

    global _anvisa_sync_prog
    _anvisa_sync_prog.update({"rodando": True, "total": len(nomes), "feitos": 0, "ok": 0, "falha": 0})

    worker_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_anvisa_pw_worker.py")

    # Filtra chaves já no cache (90 dias)
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("SELECT chave FROM anvisa_cache WHERE criado_em > NOW() - INTERVAL '90 days'")
        cached = {r["chave"] for r in cur.fetchall()}
        cur.close()
    except Exception:
        cached = set()

    chaves_pendentes: list = []
    for nome in nomes:
        ch = _anvisa_chave(nome)
        if ch and ch not in cached and ch not in chaves_pendentes:
            chaves_pendentes.append(ch)

    _anvisa_sync_prog["total"] = len(chaves_pendentes)

    if not chaves_pendentes:
        _anvisa_sync_prog["rodando"] = False
        return

    # Arquivos temporários para IPC (evita herança de handles de pipe que causam WinError 5)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        inp = f.name
        for ch in chaves_pendentes:
            f.write(ch + "\n")
    out = os.path.join(tempfile.gettempdir(), f"anvisa_{_uuid.uuid4().hex}.jsonl")
    open(out, "w").close()

    try:
        proc = _sp.Popen(
            [sys.executable, worker_script, "--input", inp, "--output", out],
            stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )

        # Polling do arquivo de saída para atualizar progresso em tempo real
        seen = 0
        while True:
            time.sleep(2)
            try:
                with open(out, encoding="utf-8") as f:
                    lines = [l.strip() for l in f if l.strip()]
                for line in lines[seen:]:
                    rec   = json.loads(line)
                    dados = rec.get("dados") or {}
                    _anvisa_salvar(rec["chave"], dados)
                    _anvisa_sync_prog["feitos"] += 1
                    if dados.get("encontrado"):
                        _anvisa_sync_prog["ok"] += 1
                    else:
                        _anvisa_sync_prog["falha"] += 1
                seen = len(lines)
            except Exception:
                pass
            if proc.poll() is not None:
                break

        # Leitura final após o processo terminar
        try:
            with open(out, encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
            for line in lines[seen:]:
                rec   = json.loads(line)
                dados = rec.get("dados") or {}
                _anvisa_salvar(rec["chave"], dados)
                _anvisa_sync_prog["feitos"] += 1
                if dados.get("encontrado"):
                    _anvisa_sync_prog["ok"] += 1
                else:
                    _anvisa_sync_prog["falha"] += 1
        except Exception:
            pass

        proc.wait()
    except Exception:
        pass
    finally:
        for p in (inp, out):
            try:
                os.unlink(p)
            except Exception:
                pass
        _anvisa_sync_prog["rodando"] = False


@app.post("/painel/admin/anvisa-sync")
@admin_required
def admin_anvisa_sync():
    if _anvisa_sync_prog["rodando"]:
        return jsonify({"ok": False, "msg": "Sincronização já em andamento.", **_anvisa_sync_prog})

    _anvisa_schema()
    _ensure_precificador_schema()

    forcar = request.form.get("forcar") == "1"

    conn = db()
    cur  = conn.cursor()

    # When forcing re-sync, delete failed cache entries so worker doesn't skip them
    if forcar:
        cur.execute("DELETE FROM anvisa_cache WHERE encontrado=FALSE")
        conn.commit()

    # Produtos DNS/Vitnatu visíveis no catálogo ativo + extras adicionados manualmente
    cur.execute("""
        SELECT DISTINCT COALESCE(m.descricao, e.descricao) AS nome
        FROM estoque e
        LEFT JOIN omie_estoque_dns dns ON dns.ean_norm = COALESCE(e.barras_norm, e.barras)
        LEFT JOIN medicamentos m       ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        WHERE e.estoque > 0
          AND COALESCE(m.descricao, e.descricao) IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM ecommerce_catalogo_oculto co
              WHERE co.cnpjloja = e.cnpj AND co.ean = e.barras
          )
          AND (dns.ean_norm IS NOT NULL
               OR mi.cloudinary_url IS NOT NULL
               OR NULLIF(TRIM(m.imagem), '') IS NOT NULL
               OR e.descricao ILIKE ANY(ARRAY[
                    '%%anasol%%','%%vit natu%%','%%vitnatu%%',
                    '%%pronabol%%','%%ricosol%%','%%unispray%%','%%goodvit%%'
                  ])
               OR EXISTS (
                    SELECT 1 FROM ecommerce_catalogo_extra ex
                    WHERE ex.cnpjloja = e.cnpj AND ex.ean = e.barras
               ))

        UNION

        SELECT DISTINCT COALESCE(m.descricao, ae.descricao_produto) AS nome
        FROM automatiza_estoque ae
        LEFT JOIN omie_estoque_dns dns ON dns.ean_norm = ae.ean
        LEFT JOIN medicamentos m       ON m.barra_norm = ae.ean
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        WHERE ae.quantidade_estoque > 0
          AND COALESCE(m.descricao, ae.descricao_produto) IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM ecommerce_catalogo_oculto co
              WHERE co.cnpjloja = ae.cnpj_loja AND co.ean = ae.ean
          )
          AND (dns.ean_norm IS NOT NULL
               OR mi.cloudinary_url IS NOT NULL
               OR NULLIF(TRIM(m.imagem), '') IS NOT NULL
               OR ae.descricao_produto ILIKE ANY(ARRAY[
                    '%%anasol%%','%%vit natu%%','%%vitnatu%%',
                    '%%pronabol%%','%%ricosol%%','%%unispray%%','%%goodvit%%'
                  ])
               OR EXISTS (
                    SELECT 1 FROM ecommerce_catalogo_extra ex
                    WHERE ex.cnpjloja = ae.cnpj_loja AND ex.ean = ae.ean
               ))
    """)
    # Deduplicate by ANVISA search key — many product variants map to the same key
    nomes_raw = [r["nome"] for r in cur.fetchall() if r["nome"]]
    cur.close()

    chaves_vistas: set = set()
    nomes: list = []
    for nome in nomes_raw:
        ch = _anvisa_chave(nome)
        if ch and ch not in chaves_vistas:
            chaves_vistas.add(ch)
            nomes.append(nome)

    threading.Thread(target=_anvisa_sync_worker, args=(nomes,), daemon=True).start()
    return jsonify({
        "ok": True,
        "msg": f"Sincronizando {len(nomes)} chaves únicas em background.",
        "total": len(nomes),
    })


@app.get("/painel/admin/anvisa-sync-status")
@admin_required
def admin_anvisa_sync_status():
    return jsonify(_anvisa_sync_prog)


@app.get("/painel/admin/anvisa-debug")
@admin_required
def admin_anvisa_debug():
    """Test ANVISA API via Playwright for a single product name."""
    nome  = (request.args.get("nome") or "DIPIRONA").strip()
    chave = _anvisa_chave(nome)
    result: dict = {"nome_entrada": nome, "chave": chave, "erro": None, "dados": None}
    try:
        import subprocess as _sp
        import tempfile
        import uuid as _uuid
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_anvisa_pw_worker.py")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            inp = f.name
            f.write(chave + "\n")
        out = os.path.join(tempfile.gettempdir(), f"anvisa_{_uuid.uuid4().hex}.jsonl")
        try:
            proc = _sp.Popen(
                [sys.executable, worker, "--input", inp, "--output", out],
                stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
            proc.wait(timeout=90)
            result["returncode"] = proc.returncode
            if os.path.exists(out):
                lines = [l.strip() for l in open(out, encoding="utf-8") if l.strip()]
                if lines:
                    rec   = json.loads(lines[-1])
                    dados = rec.get("dados") or {}
                    result["dados"] = {k: v for k, v in dados.items()
                                       if k not in ("serve_para", "como_usar", "alertas")}
                    result["dados"]["serve_para_len"] = len(dados.get("serve_para") or "")
                    result["dados"]["como_usar_len"]  = len(dados.get("como_usar") or "")
                    result["dados"]["alertas_len"]    = len(dados.get("alertas") or "")
                else:
                    result["erro"] = "worker retornou arquivo de saída vazio"
            else:
                result["erro"] = "arquivo de saída não foi criado pelo worker"
        finally:
            for p in (inp, out):
                try:
                    os.unlink(p)
                except Exception:
                    pass
    except Exception as ex:
        result["erro"] = f"{type(ex).__name__}: {ex}"
    return jsonify(result)


@app.get("/painel/admin/anvisa")
@admin_required
def admin_anvisa_cache():
    _anvisa_schema()
    filtro = (request.args.get("f") or "todos").strip()
    conn = db()
    cur  = conn.cursor()

    if filtro == "encontrado":
        cur.execute("SELECT * FROM anvisa_cache WHERE encontrado=TRUE ORDER BY criado_em DESC LIMIT 500")
    elif filtro == "nao_encontrado":
        cur.execute("SELECT * FROM anvisa_cache WHERE encontrado=FALSE ORDER BY criado_em DESC LIMIT 500")
    else:
        cur.execute("SELECT * FROM anvisa_cache ORDER BY criado_em DESC LIMIT 500")

    rows = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT COUNT(*) AS t, SUM(CASE WHEN encontrado THEN 1 ELSE 0 END) AS ok FROM anvisa_cache")
    stats = cur.fetchone()
    cur.close()

    return render_template("admin_anvisa.html",
        rows=rows, filtro=filtro,
        total=stats["t"] or 0,
        total_ok=stats["ok"] or 0,
    )


# ─── PRODUTO CANON ────────────────────────────────────────────────────────────

def _ensure_produto_canon_schema():
    key = "produto_canon"
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS produto_canon (
                ean               TEXT PRIMARY KEY,
                descricao_original TEXT,
                descricao_canon   TEXT NOT NULL,
                laboratorio       TEXT,
                fonte             TEXT DEFAULT 'manual',
                criado_em         TIMESTAMPTZ DEFAULT NOW(),
                atualizado_em     TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE produto_canon ADD COLUMN IF NOT EXISTS descricao_original TEXT")
        cur.execute("ALTER TABLE produto_canon ADD COLUMN IF NOT EXISTS laboratorio TEXT")
        cur.execute("ALTER TABLE produto_canon ADD COLUMN IF NOT EXISTS categoria TEXT")
        cur.execute("ALTER TABLE produto_canon ADD COLUMN IF NOT EXISTS imagem_cosmos TEXT")
        conn.commit()
        cur.close()
        _schema_ready.add(key)


@app.get("/painel/admin/produto-canon")
@admin_required
def admin_produto_canon():
    _ensure_produto_canon_schema()
    q = (request.args.get("q") or "").strip()
    fonte = (request.args.get("fonte") or "todos").strip()
    conn = db()
    cur = conn.cursor()

    where_clauses = []
    params = []
    if q:
        where_clauses.append("(ean ILIKE %s OR descricao_canon ILIKE %s OR descricao_original ILIKE %s)")
        like = f"%{q}%"
        params += [like, like, like]
    if fonte != "todos":
        where_clauses.append("fonte = %s")
        params.append(fonte)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    cur.execute(
        f"SELECT * FROM produto_canon {where_sql} ORDER BY atualizado_em DESC LIMIT 500",
        params,
    )
    rows = [dict(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT fonte, COUNT(*) AS cnt FROM produto_canon GROUP BY fonte ORDER BY cnt DESC
    """)
    stats = {r["fonte"]: r["cnt"] for r in cur.fetchall()}
    total = sum(stats.values())
    cur.close()

    return render_template("admin_produto_canon.html",
        rows=rows, q=q, fonte=fonte, stats=stats, total=total,
    )


@app.post("/painel/admin/produto-canon/editar")
@admin_required
def admin_produto_canon_editar():
    _ensure_produto_canon_schema()
    ean = (request.form.get("ean") or "").strip()
    novo_nome = (request.form.get("descricao_canon") or "").strip()
    if not ean or not novo_nome:
        return ("EAN e nome são obrigatórios", 400)
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO produto_canon (ean, descricao_canon, fonte, criado_em, atualizado_em)
        VALUES (%s, %s, 'manual', NOW(), NOW())
        ON CONFLICT (ean) DO UPDATE SET
            descricao_canon = EXCLUDED.descricao_canon,
            fonte           = 'manual',
            atualizado_em   = NOW()
    """, (ean, novo_nome))
    conn.commit()
    cur.close()
    return redirect(url_for("admin_produto_canon", q=ean))


# ─── CUPONS ───────────────────────────────────────────────────────────────────

def _ensure_cupons_schema():
    if "cupons" in _schema_ready:
        return
    with _schema_lock:
        if "cupons" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_cupons (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                cnpjloja TEXT NOT NULL,
                codigo TEXT NOT NULL,
                desconto_tipo TEXT DEFAULT 'pct',
                desconto_valor NUMERIC(10,2) DEFAULT 0,
                ativo BOOLEAN DEFAULT TRUE,
                valido_ate DATE,
                uso_maximo INTEGER DEFAULT 0,
                usos_count INTEGER DEFAULT 0,
                criado_em TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_cupons_cnpj_codigo
            ON ecommerce_cupons (cnpjloja, upper(codigo))
        """)
        cur.execute("ALTER TABLE ecommerce_cupons ADD COLUMN IF NOT EXISTS publico TEXT DEFAULT 'todos'")
        cur.execute("ALTER TABLE ecommerce_cupons ADD COLUMN IF NOT EXISTS min_compras INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE ecommerce_cupons ADD COLUMN IF NOT EXISTS escopo TEXT DEFAULT 'todos'")
        cur.execute("ALTER TABLE ecommerce_cupons ADD COLUMN IF NOT EXISTS escopo_categorias TEXT DEFAULT ''")
        cur.execute("ALTER TABLE ecommerce_cupons ADD COLUMN IF NOT EXISTS escopo_eans TEXT DEFAULT ''")
        cur.execute("ALTER TABLE ecommerce_cupons ADD COLUMN IF NOT EXISTS tipo_regra TEXT DEFAULT 'codigo'")
        cur.execute("ALTER TABLE ecommerce_cupons ADD COLUMN IF NOT EXISTS forma_pagamento TEXT DEFAULT ''")
        cur.execute("ALTER TABLE ecommerce_cupons ADD COLUMN IF NOT EXISTS qtd_minima INTEGER DEFAULT 0")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_cupons_clientes (
                id SERIAL PRIMARY KEY,
                cupom_id UUID NOT NULL,
                consumidor_id UUID NOT NULL,
                UNIQUE(cupom_id, consumidor_id)
            )
        """)
        conn.commit()
        cur.close()
        _schema_ready.add("cupons")


@app.get("/painel/cupons")
@painel_required
def painel_cupons():
    _ensure_cupons_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT * FROM ecommerce_cupons WHERE cnpjloja=%s ORDER BY criado_em DESC", (cnpjloja,))
    cupons = cur.fetchall(); cur.close()
    return render_template("painel_cupons.html", cupons=cupons)


@app.post("/painel/cupons/novo")
@painel_required
def painel_cupons_novo():
    _ensure_cupons_schema()
    cnpjloja = session.get("cnpjloja")
    f = request.form
    tipo_regra = f.get("tipo_regra") or "codigo"
    if tipo_regra not in ("codigo", "pagamento", "quantidade"):
        tipo_regra = "codigo"
    codigo = (f.get("codigo") or "").strip().upper()
    desconto_tipo = f.get("desconto_tipo") or "pct"
    desconto_valor = _to_float_or_none(f.get("desconto_valor")) or 0
    valido_ate = f.get("valido_ate") or None
    uso_maximo = int(f.get("uso_maximo") or 0)
    forma_pagamento = (f.get("forma_pagamento") or "").strip()
    qtd_minima = int(f.get("qtd_minima") or 0)
    if tipo_regra == "codigo" and not codigo:
        flash("Informe o código do cupom.", "error")
        return redirect(url_for("painel_cupons"))
    if desconto_valor <= 0:
        flash("Informe o valor do desconto.", "error")
        return redirect(url_for("painel_cupons"))
    if desconto_tipo == "pct" and desconto_valor > 100:
        flash("Desconto percentual não pode passar de 100%.", "error")
        return redirect(url_for("painel_cupons"))
    if tipo_regra == "pagamento" and not forma_pagamento:
        flash("Selecione a forma de pagamento.", "error")
        return redirect(url_for("painel_cupons"))
    if tipo_regra == "quantidade" and qtd_minima < 1:
        flash("Informe a quantidade mínima (mínimo 1).", "error")
        return redirect(url_for("painel_cupons"))
    if tipo_regra != "codigo":
        codigo = ""
    publico = f.get("publico") or "todos"
    if publico not in ("todos", "especifico", "primeira_compra", "frequente"):
        publico = "todos"
    min_compras = int(f.get("min_compras") or 1)
    escopo = f.get("escopo") or "todos"
    if escopo not in ("todos", "categoria", "produto"):
        escopo = "todos"
    escopo_categorias = ""
    escopo_eans = ""
    if escopo == "categoria":
        cats = request.form.getlist("escopo_categorias")
        escopo_categorias = ",".join(c.strip() for c in cats if c.strip())
    elif escopo == "produto":
        eans = request.form.getlist("escopo_eans")
        escopo_eans = ",".join(e.strip() for e in eans if e.strip())
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO ecommerce_cupons (cnpjloja, codigo, desconto_tipo, desconto_valor, valido_ate, uso_maximo,
              publico, min_compras, escopo, escopo_categorias, escopo_eans,
              tipo_regra, forma_pagamento, qtd_minima)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (cnpjloja, codigo, desconto_tipo, desconto_valor, valido_ate, uso_maximo,
              publico, min_compras, escopo, escopo_categorias, escopo_eans,
              tipo_regra, forma_pagamento, qtd_minima))
        cupom_row = cur.fetchone()
        cupom_id = str(cupom_row["id"])
        consumidores_ids = request.form.getlist("consumidores_ids")
        if publico == "especifico" and consumidores_ids:
            for cid in consumidores_ids:
                try:
                    cur.execute("INSERT INTO ecommerce_cupons_clientes (cupom_id, consumidor_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (cupom_id, cid))
                except Exception:
                    pass
        conn.commit()
        labels = {"codigo": f"Cupom {codigo}", "pagamento": "Desconto por pagamento", "quantidade": "Desconto por quantidade"}
        flash(f"{labels.get(tipo_regra,'Regra')} criado com sucesso.", "success")
    except Exception:
        conn.rollback()
        flash("Código de cupom já existe." if tipo_regra == "codigo" else "Erro ao criar regra de desconto.", "error")
    cur.close()
    return redirect(url_for("painel_cupons"))


@app.post("/painel/cupons/<cupom_id>/editar")
@painel_required
def painel_cupom_editar(cupom_id):
    _ensure_cupons_schema()
    cnpjloja = session.get("cnpjloja")
    f = request.form
    tipo_regra = f.get("tipo_regra") or "codigo"
    if tipo_regra not in ("codigo", "pagamento", "quantidade"):
        tipo_regra = "codigo"
    codigo = (f.get("codigo") or "").strip().upper()
    desconto_tipo = f.get("desconto_tipo") or "pct"
    desconto_valor = _to_float_or_none(f.get("desconto_valor")) or 0
    valido_ate = f.get("valido_ate") or None
    uso_maximo = int(f.get("uso_maximo") or 0)
    forma_pagamento = (f.get("forma_pagamento") or "").strip()
    qtd_minima = int(f.get("qtd_minima") or 0)
    if tipo_regra == "codigo" and not codigo:
        flash("Informe o código do cupom.", "error")
        return redirect(url_for("painel_cupons"))
    if desconto_valor <= 0:
        flash("Informe o valor do desconto.", "error")
        return redirect(url_for("painel_cupons"))
    if desconto_tipo == "pct" and desconto_valor > 100:
        flash("Desconto percentual não pode passar de 100%.", "error")
        return redirect(url_for("painel_cupons"))
    if tipo_regra != "codigo":
        codigo = ""
    publico = f.get("publico") or "todos"
    if publico not in ("todos", "especifico", "primeira_compra", "frequente"):
        publico = "todos"
    min_compras = int(f.get("min_compras") or 1)
    escopo = f.get("escopo") or "todos"
    if escopo not in ("todos", "categoria", "produto"):
        escopo = "todos"
    escopo_categorias = ""
    escopo_eans = ""
    if escopo == "categoria":
        cats = request.form.getlist("escopo_categorias")
        escopo_categorias = ",".join(c.strip() for c in cats if c.strip())
    elif escopo == "produto":
        eans = request.form.getlist("escopo_eans")
        escopo_eans = ",".join(e.strip() for e in eans if e.strip())
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE ecommerce_cupons SET
              codigo=%s, desconto_tipo=%s, desconto_valor=%s, valido_ate=%s,
              uso_maximo=%s, publico=%s, min_compras=%s,
              escopo=%s, escopo_categorias=%s, escopo_eans=%s,
              tipo_regra=%s, forma_pagamento=%s, qtd_minima=%s
            WHERE id=%s AND cnpjloja=%s
        """, (codigo, desconto_tipo, desconto_valor, valido_ate, uso_maximo,
              publico, min_compras, escopo, escopo_categorias, escopo_eans,
              tipo_regra, forma_pagamento, qtd_minima,
              cupom_id, cnpjloja))
        if cur.rowcount == 0:
            flash("Regra não encontrada.", "error")
        else:
            conn.commit()
            flash("Regra de desconto atualizada.", "success")
    except Exception:
        conn.rollback()
        flash("Código já existe em outro cupom.", "error")
    cur.close()
    return redirect(url_for("painel_cupons"))


@app.post("/painel/cupons/<cupom_id>/toggle")
@painel_required
def painel_cupom_toggle(cupom_id):
    _ensure_cupons_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute("UPDATE ecommerce_cupons SET ativo = NOT ativo WHERE id=%s AND cnpjloja=%s", (cupom_id, cnpjloja))
    conn.commit(); cur.close()
    return redirect(url_for("painel_cupons"))


@app.post("/painel/cupons/<cupom_id>/excluir")
@painel_required
def painel_cupom_excluir(cupom_id):
    _ensure_cupons_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute("DELETE FROM ecommerce_cupons WHERE id=%s AND cnpjloja=%s", (cupom_id, cnpjloja))
    conn.commit(); cur.close()
    flash("Cupom removido.", "success")
    return redirect(url_for("painel_cupons"))


@app.post("/painel/cupons/<cupom_id>/clientes")
@painel_required
def painel_cupom_clientes(cupom_id):
    _ensure_cupons_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    # Verify ownership
    cur.execute("SELECT id FROM ecommerce_cupons WHERE id=%s AND cnpjloja=%s LIMIT 1", (cupom_id, cnpjloja))
    if not cur.fetchone():
        flash("Cupom não encontrado.", "error")
        cur.close(); return redirect(url_for("painel_cupons"))
    # Replace all assigned consumers
    cur.execute("DELETE FROM ecommerce_cupons_clientes WHERE cupom_id=%s", (cupom_id,))
    consumidores_ids = request.form.getlist("consumidores_ids")
    for cid in consumidores_ids:
        cur.execute("INSERT INTO ecommerce_cupons_clientes (cupom_id, consumidor_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (cupom_id, cid))
    conn.commit(); cur.close()
    flash("Clientes do cupom atualizados.", "success")
    return redirect(url_for("painel_cupons"))


@app.get("/api/cupom/validar")
def api_cupom_validar():
    _ensure_cupons_schema()
    cnpjloja = (request.args.get("cnpj") or "").strip()
    codigo   = (request.args.get("codigo") or "").strip().upper()
    total    = _to_float_or_none(request.args.get("total")) or 0
    if not cnpjloja or not codigo:
        return jsonify({"valido": False, "msg": "Dados incompletos."})
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT * FROM ecommerce_cupons
        WHERE cnpjloja=%s AND upper(codigo)=%s AND ativo=TRUE
          AND COALESCE(tipo_regra,'codigo')='codigo'
          AND (valido_ate IS NULL OR valido_ate >= CURRENT_DATE)
          AND (uso_maximo = 0 OR usos_count < uso_maximo)
        LIMIT 1
    """, (cnpjloja, codigo))
    cupom = cur.fetchone(); cur.close()
    if not cupom:
        return jsonify({"valido": False, "msg": "Cupom inválido ou expirado."})
    desconto = 0.0
    if cupom["desconto_tipo"] == "pct":
        desconto = round(total * float(cupom["desconto_valor"]) / 100, 2)
    else:
        desconto = min(float(cupom["desconto_valor"]), total)
    tipo_label = (
        f"-{int(cupom['desconto_valor'])}%" if cupom["desconto_tipo"] == "pct"
        else f"R$ {float(cupom['desconto_valor']):.2f}"
    )
    return jsonify({
        "valido": True,
        "cupom_id": str(cupom["id"]),
        "desconto": desconto,
        "tipo": cupom["desconto_tipo"],
        "valor": float(cupom["desconto_valor"]),
        "msg": f"Cupom aplicado: {tipo_label} de desconto",
        "escopo": cupom.get("escopo") or "todos",
        "escopo_categorias": cupom.get("escopo_categorias") or "",
        "escopo_eans": cupom.get("escopo_eans") or "",
    })


@app.get("/api/descontos-auto")
def api_descontos_auto():
    """Retorna regras automáticas de desconto (pagamento e quantidade) ativas para uma loja."""
    _ensure_cupons_schema()
    cnpjloja = (request.args.get("cnpj") or "").strip()
    if not cnpjloja:
        return jsonify({"pagamento": [], "quantidade": []})
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT tipo_regra, COALESCE(forma_pagamento,'') AS forma_pagamento,
               desconto_tipo, desconto_valor,
               COALESCE(qtd_minima,0) AS qtd_minima,
               COALESCE(escopo,'todos') AS escopo,
               COALESCE(escopo_categorias,'') AS escopo_categorias,
               COALESCE(escopo_eans,'') AS escopo_eans
        FROM ecommerce_cupons
        WHERE cnpjloja=%s AND ativo=TRUE
          AND COALESCE(tipo_regra,'codigo') IN ('pagamento','quantidade')
          AND (valido_ate IS NULL OR valido_ate >= CURRENT_DATE)
          AND (uso_maximo = 0 OR usos_count < uso_maximo)
        ORDER BY desconto_valor DESC
    """, (cnpjloja,))
    rows = cur.fetchall(); cur.close()
    pagamento_rules = []
    quantidade_rules = []
    for r in rows:
        entry = {
            "desconto_tipo": r["desconto_tipo"],
            "desconto_valor": float(r["desconto_valor"]),
            "escopo": r["escopo"],
            "escopo_categorias": r["escopo_categorias"],
            "escopo_eans": r["escopo_eans"],
        }
        if r["tipo_regra"] == "pagamento":
            entry["forma_pagamento"] = r["forma_pagamento"]
            pagamento_rules.append(entry)
        else:
            entry["qtd_minima"] = int(r["qtd_minima"])
            quantidade_rules.append(entry)
    return jsonify({"pagamento": pagamento_rules, "quantidade": quantidade_rules})


@app.get("/api/cupons/disponiveis")
def api_cupons_disponiveis():
    if os.getenv("CUPONS_ENABLED", "1") == "0":
        return jsonify({"cupons": []})
    _ensure_cupons_schema()
    cnpjloja = (request.args.get("cnpj") or "").strip()
    if not cnpjloja:
        return jsonify({"cupons": []})
    consumidor_id = session.get("consumidor_id")
    if not consumidor_id:
        return jsonify({"cupons": []})
    conn = db(); cur = conn.cursor()
    # Count consumer's orders from this store
    cur.execute(
        "SELECT COUNT(*) AS n FROM ecommerce_pedidos WHERE cnpjloja=%s AND consumidor_id=%s AND status NOT IN ('cancelado')",
        (cnpjloja, consumidor_id)
    )
    n_pedidos = (cur.fetchone() or {}).get("n") or 0
    cur.execute("""
        SELECT c.id, c.codigo, c.desconto_tipo, c.desconto_valor, c.valido_ate, c.publico, c.min_compras,
               COALESCE(c.escopo,'todos') AS escopo,
               COALESCE(c.escopo_categorias,'') AS escopo_categorias,
               COALESCE(c.escopo_eans,'') AS escopo_eans
        FROM ecommerce_cupons c
        WHERE c.cnpjloja = %s
          AND c.ativo = TRUE
          AND COALESCE(c.tipo_regra,'codigo') = 'codigo'
          AND (c.valido_ate IS NULL OR c.valido_ate >= CURRENT_DATE)
          AND (c.uso_maximo = 0 OR c.usos_count < c.uso_maximo)
          AND (
            c.publico = 'todos'
            OR (c.publico = 'especifico' AND EXISTS (
                SELECT 1 FROM ecommerce_cupons_clientes cc
                WHERE cc.cupom_id = c.id AND cc.consumidor_id = %s
            ))
            OR (c.publico = 'primeira_compra' AND %s = 0)
            OR (c.publico = 'frequente' AND %s >= c.min_compras AND c.min_compras > 0)
          )
        ORDER BY c.desconto_valor DESC
    """, (cnpjloja, consumidor_id, n_pedidos, n_pedidos))
    rows = cur.fetchall(); cur.close()
    result = []
    for r in rows:
        result.append({
            "id": str(r["id"]),
            "codigo": r["codigo"],
            "desconto_tipo": r["desconto_tipo"],
            "desconto_valor": float(r["desconto_valor"]),
            "valido_ate": r["valido_ate"].strftime("%d/%m/%Y") if r["valido_ate"] else None,
            "publico": r["publico"],
            "escopo": r.get("escopo") or "todos",
            "escopo_categorias": r.get("escopo_categorias") or "",
            "escopo_eans": r.get("escopo_eans") or "",
        })
    return jsonify({"cupons": result})


@app.get("/meus-cupons")
@_consumer_required
def consumidor_cupons():
    _ensure_cupons_schema()
    consumidor_id = session.get("consumidor_id")
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT u.razao, u.cnpjloja,
               COUNT(DISTINCT p.id) AS n_pedidos
        FROM ecommerce_pedidos p
        JOIN users u ON u.cnpjloja = p.cnpjloja
        WHERE p.consumidor_id = %s AND p.status NOT IN ('cancelado')
        GROUP BY u.razao, u.cnpjloja
    """, (consumidor_id,))
    lojas = cur.fetchall()
    cupons_por_loja = []
    for loja in lojas:
        n_pedidos = loja["n_pedidos"] or 0
        cur.execute("""
            SELECT c.id, c.codigo, c.desconto_tipo, c.desconto_valor, c.valido_ate, c.publico, c.min_compras,
                   COALESCE(c.escopo,'todos') AS escopo,
                   COALESCE(c.escopo_categorias,'') AS escopo_categorias,
                   COALESCE(c.escopo_eans,'') AS escopo_eans
            FROM ecommerce_cupons c
            WHERE c.cnpjloja = %s
              AND c.ativo = TRUE
              AND COALESCE(c.tipo_regra,'codigo') = 'codigo'
              AND (c.valido_ate IS NULL OR c.valido_ate >= CURRENT_DATE)
              AND (c.uso_maximo = 0 OR c.usos_count < c.uso_maximo)
              AND (
                c.publico = 'todos'
                OR (c.publico = 'especifico' AND EXISTS (
                    SELECT 1 FROM ecommerce_cupons_clientes cc
                    WHERE cc.cupom_id = c.id AND cc.consumidor_id = %s
                ))
                OR (c.publico = 'primeira_compra' AND %s = 0)
                OR (c.publico = 'frequente' AND %s >= c.min_compras AND c.min_compras > 0)
              )
        """, (loja["cnpjloja"], consumidor_id, n_pedidos, n_pedidos))
        cupons = cur.fetchall()
        if cupons:
            cupons_por_loja.append({"loja": dict(loja), "cupons": [dict(c) for c in cupons]})
    cur.close()
    return render_template("consumidor_cupons.html", cupons_por_loja=cupons_por_loja)


@app.get("/api/painel/consumidores")
@painel_required
def api_painel_consumidores():
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ec.consumidor_id, ec.cliente_nome, ec.cliente_email,
               COUNT(*) AS n_pedidos
        FROM ecommerce_pedidos ec
        WHERE ec.cnpjloja = %s AND ec.status NOT IN ('cancelado')
        GROUP BY ec.consumidor_id, ec.cliente_nome, ec.cliente_email
        ORDER BY n_pedidos DESC
        LIMIT 200
    """, (cnpjloja,))
    rows = cur.fetchall(); cur.close()
    return jsonify([dict(r) for r in rows])


@app.get("/api/painel/produtos-loja")
@painel_required
def api_painel_produtos_loja():
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT e.barras AS ean, e.descricao AS nome
        FROM estoque e
        WHERE e.cnpj = %s AND e.estoque > 0
        ORDER BY e.descricao
        LIMIT 600
    """, (cnpjloja,))
    rows = cur.fetchall()
    if not rows:
        cur.execute("""
            SELECT DISTINCT ae.ean, ae.descricao_produto AS nome
            FROM automatiza_estoque ae
            WHERE ae.cnpj_loja = %s AND ae.quantidade_estoque > 0
            ORDER BY ae.descricao_produto
            LIMIT 600
        """, (cnpjloja,))
        rows = cur.fetchall()
    cur.close()
    return jsonify([{"ean": r["ean"], "nome": r["nome"]} for r in rows if r.get("ean")])


# ═══════════════════════════════════════════════════════════════════════════════
# MERCADO LIVRE — integração completa (Modelo C: conta única PoupáQui)
# ═══════════════════════════════════════════════════════════════════════════════

# ── helpers de token ──────────────────────────────────────────────────────────

def _ml_get_token():
    """Retorna access_token válido, renovando automaticamente se necessário."""
    _ensure_ml_schema()
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT access_token, refresh_token, expires_at FROM ml_tokens WHERE id=1")
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    if row["expires_at"] <= datetime.now(timezone.utc) + timedelta(minutes=5):
        return _ml_refresh_token(row["refresh_token"])
    return row["access_token"]


def _ml_refresh_token(refresh_token):
    """Usa o refresh_token para obter um novo access_token e salva no banco."""
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": ML_APP_ID,
        "client_secret": ML_SECRET,
        "refresh_token": refresh_token,
    }).encode()
    req = urllib.request.Request(ML_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            token_data = json.loads(resp.read())
    except Exception as e:
        print(f"[ML] Erro ao renovar token: {e}")
        return None
    if "access_token" not in token_data:
        return None
    access_token  = token_data["access_token"]
    new_refresh   = token_data.get("refresh_token", refresh_token)
    expires_in    = token_data.get("expires_in", 21600)
    expires_at    = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    conn = db(); cur = conn.cursor()
    cur.execute("""
        UPDATE ml_tokens
        SET access_token=%s, refresh_token=%s, expires_at=%s, updated_at=NOW()
        WHERE id=1
    """, (access_token, new_refresh, expires_at))
    conn.commit(); cur.close()
    return access_token


def _ml_api_get(path, token=None):
    """GET autenticado na API do ML. Retorna dict ou None."""
    if token is None:
        token = _ml_get_token()
    if not token:
        return None
    req = urllib.request.Request(ML_API_BASE + path)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    req.add_header("Accept", "application/json")
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        print(f"[ML] Erro GET {path}: HTTP {e.code} — {body}")
        return None
    except Exception as e:
        print(f"[ML] Erro GET {path}: {e}")
        return None


def _ml_is_connected():
    """Verifica se há token válido armazenado."""
    _ensure_ml_schema()
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT expires_at FROM ml_tokens WHERE id=1")
    row = cur.fetchone()
    cur.close()
    if not row:
        return False
    return row["expires_at"] > datetime.now(timezone.utc) + timedelta(minutes=5)


# ── atribuição de loja para pedido ML ─────────────────────────────────────────

def _ml_assign_loja(eans, cep):
    """
    Encontra a loja mais próxima do CEP do comprador que tenha estoque dos EANs.
    Retorna cnpjloja ou None.
    """
    lat, lng = None, None
    if cep:
        try:
            url_cep = f"https://viacep.com.br/ws/{cep}/json/"
            req_cep = urllib.request.Request(url_cep,
                headers={"User-Agent": "PoupaquiEcommerce/1.0"})
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req_cep, timeout=5, context=ctx) as r:
                cep_data = json.loads(r.read())
            query = f"{cep_data.get('logradouro','')}, {cep_data.get('localidade','')}, {cep_data.get('uf','')}, Brasil"
            q = urllib.parse.quote(query)
            url_geo = f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1"
            req_geo = urllib.request.Request(url_geo,
                headers={"User-Agent": "PoupaquiEcommerce/1.0 (emano4775@gmail.com)"})
            with urllib.request.urlopen(req_geo, timeout=5) as r2:
                geo = json.loads(r2.read())
            if geo:
                lat = float(geo[0]["lat"])
                lng = float(geo[0]["lon"])
        except Exception:
            pass

    conn = db(); cur = conn.cursor()
    # Lojas com coordenadas
    cur.execute("SELECT cnpjloja, lat, lng FROM ecommerce_lojas_geo WHERE lat IS NOT NULL AND lng IS NOT NULL")
    lojas_geo = {r["cnpjloja"]: (r["lat"], r["lng"]) for r in cur.fetchall()}
    if not lojas_geo:
        cur.close()
        return None

    # Lojas com estoque dos EANs
    cnpjs_com_estoque = set()
    if eans:
        ph = ",".join(["%s"] * len(eans))
        cur.execute(f"SELECT DISTINCT cnpj FROM estoque WHERE barras IN ({ph}) AND estoque > 0", eans)
        cnpjs_com_estoque = {r["cnpj"] for r in cur.fetchall()}
        if not cnpjs_com_estoque:
            cur.execute(f"SELECT DISTINCT cnpj_loja FROM automatiza_estoque WHERE ean IN ({ph}) AND quantidade_estoque > 0", eans)
            cnpjs_com_estoque = {r["cnpj_loja"] for r in cur.fetchall()}
    cur.close()

    candidatos = [(cnpj, lojas_geo[cnpj]) for cnpj in (cnpjs_com_estoque or lojas_geo) if cnpj in lojas_geo]
    if not candidatos:
        return next(iter(lojas_geo), None)

    if lat and lng:
        def _hav(c):
            R = 6371
            la2, lo2 = c[1]
            dlat = math.radians(la2 - lat)
            dlon = math.radians(lo2 - lng)
            a = math.sin(dlat/2)**2 + math.cos(math.radians(lat)) * math.cos(math.radians(la2)) * math.sin(dlon/2)**2
            return R * 2 * math.asin(math.sqrt(a))
        candidatos.sort(key=_hav)

    return candidatos[0][0]


# ── processamento assíncrono de pedido ML ─────────────────────────────────────

def _process_ml_order(order_id):
    """Busca detalhes do pedido no ML e cria em ecommerce_pedidos. Roda em thread."""
    try:
        import uuid as _uuid
        token = _ml_get_token()
        if not token:
            print(f"[ML] Sem token para processar pedido {order_id}")
            return

        order = _ml_api_get(f"/orders/{order_id}", token)
        if not order:
            raise RuntimeError(f"API ML retornou vazio para /orders/{order_id} — PolicyAgent ou token inválido")

        conn = db(); cur = conn.cursor()

        # Evita duplicata
        cur.execute("SELECT id FROM ecommerce_pedidos WHERE ml_order_id=%s", (str(order_id),))
        if cur.fetchone():
            cur.close()
            return

        # Dados do comprador
        buyer       = order.get("buyer", {})
        first_name  = buyer.get("first_name", "")
        last_name   = buyer.get("last_name", "")
        cliente_nome = (f"{first_name} {last_name}").strip() or buyer.get("nickname", "Comprador ML")
        cliente_email = buyer.get("email", "")
        phone_obj   = buyer.get("phone", {})
        raw_phone   = f"{phone_obj.get('area_code','')}{phone_obj.get('number','')}"
        cliente_telefone = re.sub(r"\D+", "", raw_phone)[:11] or "00000000000"

        total       = float(order.get("total_amount", 0))
        status_ml   = order.get("status", "confirmed")
        status_ped  = "pago" if status_ml in ("paid", "confirmed") else "pendente"
        pag_status  = "approved" if status_ped == "pago" else "pending"

        # Itens
        order_items = order.get("order_items", [])
        itens_pedido = []
        eans = []
        cnpjloja_publicou = None  # loja que publicou o item no ML
        for oi in order_items:
            item_obj  = oi.get("item") or {}
            item_id   = item_obj.get("id", "")
            nome_item = item_obj.get("title", "")
            qty       = int(oi.get("quantity", 1))
            preco_u   = float(oi.get("unit_price", 0))
            cur.execute("SELECT ean, cnpjloja FROM ml_items WHERE ml_item_id=%s", (item_id,))
            row_ean = cur.fetchone()
            ean = row_ean["ean"] if row_ean else item_id
            if row_ean and row_ean.get("cnpjloja"):
                cnpjloja_publicou = row_ean["cnpjloja"]
            eans.append(ean)
            itens_pedido.append({"ean": ean, "nome": nome_item, "preco": preco_u, "qty": qty})

        # Endereço de envio
        shipping_obj  = order.get("shipping") or {}
        shipping_id   = shipping_obj.get("id")
        endereco_entrega = ""
        cep_comprador = ""
        if shipping_id:
            shipping = _ml_api_get(f"/shipments/{shipping_id}", token)
            if shipping:
                addr   = shipping.get("receiver_address") or {}
                cep_comprador = re.sub(r"\D+", "", addr.get("zip_code", ""))
                street = addr.get("street_name", "")
                number = addr.get("street_number", "")
                city   = (addr.get("city") or {}).get("name", "")
                state  = (addr.get("state") or {}).get("name", "")
                endereco_entrega = f"{street}, {number} — {city}/{state} CEP {cep_comprador}"

        cur.close()

        # Atribui loja — prioridade: loja que publicou o item → primeira loja cadastrada
        # A loja que publicou é sempre a correta; CEP apenas serviria se o item
        # estivesse em múltiplas lojas, o que não ocorre nesta arquitetura.
        cnpjloja = cnpjloja_publicou
        if not cnpjloja:
            # Fallback: item não está em ml_items (publicado fora do painel) — usa primeira loja
            conn_fb = db(); cur_fb = conn_fb.cursor()
            cur_fb.execute(
                "SELECT cnpjloja FROM users WHERE is_admin=FALSE ORDER BY razao LIMIT 1"
            )
            row_fb = cur_fb.fetchone(); cur_fb.close()
            cnpjloja = row_fb["cnpjloja"] if row_fb else None
        if not cnpjloja:
            raise RuntimeError("Nenhuma loja cadastrada no sistema.")

        # Insere pedido
        conn2 = db(); cur2 = conn2.cursor()
        pedido_id = str(_uuid.uuid4())
        cur2.execute("""
            INSERT INTO ecommerce_pedidos (
                id, cnpjloja, consumidor_id, cliente_nome, cliente_telefone, cliente_email,
                forma_pagamento, total, status, pagamento_status,
                tipo_entrega, endereco_entrega,
                origem, ml_order_id, criado_em, atualizado_em
            ) VALUES (
                %s,%s,NULL,%s,%s,%s,
                'mercado_livre',%s,%s,%s,
                'entrega',%s,
                'mercado_livre',%s,NOW(),NOW()
            )
        """, (
            pedido_id, cnpjloja, cliente_nome, cliente_telefone, cliente_email,
            total, status_ped, pag_status,
            endereco_entrega, str(order_id)
        ))
        for it in itens_pedido:
            cur2.execute("""
                INSERT INTO ecommerce_pedido_itens (pedido_id, ean, nome, preco_unitario, qty)
                VALUES (%s,%s,%s,%s,%s)
            """, (pedido_id, it["ean"], it["nome"], it["preco"], it["qty"]))
        conn2.commit(); cur2.close()
        print(f"[ML] Pedido {order_id} criado → {pedido_id} (loja {cnpjloja})")
    except Exception as e:
        print(f"[ML] Erro ao processar pedido {order_id}: {e}")
        raise  # propaga para _ml_flush_queue gravar o erro real na fila


# ── OAuth: iniciar autenticação ───────────────────────────────────────────────

@app.get("/ml/auth")
@painel_required
def ml_auth():
    _ensure_ml_schema()
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": ML_APP_ID,
        "redirect_uri": ML_REDIRECT,
    })
    return redirect(f"{ML_AUTH_URL}?{params}")


# ── OAuth: callback ───────────────────────────────────────────────────────────

@app.get("/ml/callback")
def ml_callback():
    _ensure_ml_schema()
    code = request.args.get("code", "")
    if not code:
        flash("Autorização ML cancelada ou inválida.", "error")
        return redirect(url_for("painel_ml"))

    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": ML_APP_ID,
        "client_secret": ML_SECRET,
        "code": code,
        "redirect_uri": ML_REDIRECT,
    }).encode()
    req = urllib.request.Request(ML_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            token_data = json.loads(resp.read())
    except Exception as e:
        flash(f"Erro ao obter token ML: {e}", "error")
        return redirect(url_for("painel_ml"))

    if "access_token" not in token_data:
        flash(f"Resposta inválida do ML: {token_data}", "error")
        return redirect(url_for("painel_ml"))

    access_token  = token_data["access_token"]
    refresh_token = token_data.get("refresh_token", "")
    expires_in    = token_data.get("expires_in", 21600)
    expires_at    = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    ml_user_id    = str(token_data.get("user_id", ""))

    conn = db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO ml_tokens (id, access_token, refresh_token, expires_at, ml_user_id, updated_at)
        VALUES (1, %s, %s, %s, %s, NOW())
        ON CONFLICT (id) DO UPDATE
          SET access_token=%s, refresh_token=%s, expires_at=%s, ml_user_id=%s, updated_at=NOW()
    """, (access_token, refresh_token, expires_at, ml_user_id,
          access_token, refresh_token, expires_at, ml_user_id))
    conn.commit(); cur.close()

    flash("Conta Mercado Livre conectada com sucesso!", "success")
    return redirect(url_for("painel_ml"))


# ── Entrega confirmada: notifica ML ──────────────────────────────────────────

def _ml_post(path, body_dict, token):
    """POST autenticado para a API do ML. Retorna (http_status, resposta_dict_ou_None)."""
    body = json.dumps(body_dict).encode("utf-8")
    req = urllib.request.Request(ML_API_BASE + path, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
        resp_body = resp.read().decode("utf-8", errors="replace")
        return resp.status, json.loads(resp_body) if resp_body else {}


def _ml_feedback_entregue(ml_order_id):
    """
    Notifica o ML que o pedido foi entregue.
    - Com Mercado Envios (tem shipment_id): POST /shipments/{id}/fulfillment
    - Entrega a combinar (sem shipment_id): POST /orders/{id}/feedback com fulfilled=true
    Retorna (True, "") em sucesso ou (False, mensagem_erro) em falha.
    """
    try:
        token = _ml_get_token()
        if not token:
            return False, "Sem token ML válido."
        order_data = _ml_api_get(f"/orders/{ml_order_id}", token)
        if not order_data:
            return False, f"Não foi possível buscar pedido {ml_order_id} na API ML."
        shipping_id = (order_data.get("shipping") or {}).get("id")
        print(f"[ML] Pedido {ml_order_id}: shipping_id={shipping_id}")

        if shipping_id:
            # Mercado Envios Flex / Entrega por sua conta com shipment
            status, resp = _ml_post(
                f"/shipments/{shipping_id}/fulfillment",
                {"order_id": int(ml_order_id)},
                token,
            )
            print(f"[ML] fulfillment shipment {shipping_id}: HTTP {status}, {resp}")
            return True, ""
        else:
            # Entrega a combinar (sem Mercado Envios) — feedback de entrega fulfillment
            status, resp = _ml_post(
                f"/orders/{ml_order_id}/feedback",
                {"fulfilled": True, "rating": "neutral"},
                token,
            )
            print(f"[ML] feedback pedido {ml_order_id}: HTTP {status}, {resp}")
            return True, ""
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")[:400]
        msg = f"HTTP {e.code}: {body_err}"
        print(f"[ML] Erro ao confirmar entrega pedido {ml_order_id}: {msg}")
        return False, msg
    except Exception as e:
        print(f"[ML] Erro ao confirmar entrega pedido {ml_order_id}: {e}")
        return False, str(e)


# ── Webhook: recebe notificações do ML ───────────────────────────────────────

def _ml_queue_order(order_id_str):
    """Enfileira um order_id ML para processamento. Cria a tabela se não existir."""
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ml_order_queue (
                order_id TEXT PRIMARY KEY,
                received_at TIMESTAMPTZ DEFAULT NOW(),
                processed BOOLEAN DEFAULT FALSE,
                error TEXT
            )
        """)
        cur.execute(
            "INSERT INTO ml_order_queue (order_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (order_id_str,)
        )
        conn.commit(); cur.close()
    except Exception as e:
        print(f"[ML] Erro ao enfileirar {order_id_str}: {e}")


def _ml_flush_queue():
    """Processa pedidos ML pendentes na fila. Chamado automaticamente ao abrir Pedidos."""
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ml_order_queue (
                order_id TEXT PRIMARY KEY,
                received_at TIMESTAMPTZ DEFAULT NOW(),
                processed BOOLEAN DEFAULT FALSE,
                error TEXT
            )
        """)
        cur.execute(
            "SELECT order_id FROM ml_order_queue WHERE processed=FALSE ORDER BY received_at LIMIT 20"
        )
        pendentes = [r["order_id"] for r in cur.fetchall()]
        cur.close()
    except Exception:
        return

    for oid in pendentes:
        try:
            # Pula se já foi criado (webhook pode ter processado antes)
            conn2 = db(); cur2 = conn2.cursor()
            cur2.execute("SELECT id FROM ecommerce_pedidos WHERE ml_order_id=%s LIMIT 1", (oid,))
            ja_existe = cur2.fetchone(); cur2.close()
            if ja_existe:
                conn3 = db(); cur3 = conn3.cursor()
                cur3.execute("UPDATE ml_order_queue SET processed=TRUE WHERE order_id=%s", (oid,))
                conn3.commit(); cur3.close()
                continue

            _process_ml_order(int(oid))

            # Só marca como processado se o pedido foi realmente criado no banco
            conn4 = db(); cur4 = conn4.cursor()
            cur4.execute("SELECT id FROM ecommerce_pedidos WHERE ml_order_id=%s LIMIT 1", (oid,))
            criado = cur4.fetchone(); cur4.close()
            if criado:
                conn4b = db(); cur4b = conn4b.cursor()
                cur4b.execute("UPDATE ml_order_queue SET processed=TRUE, error=NULL WHERE order_id=%s", (oid,))
                conn4b.commit(); cur4b.close()
            else:
                conn4b = db(); cur4b = conn4b.cursor()
                cur4b.execute("UPDATE ml_order_queue SET processed=FALSE, error=%s WHERE order_id=%s",
                              ("Processado mas pedido não encontrado no banco", oid))
                conn4b.commit(); cur4b.close()
        except Exception as e:
            try:
                conn5 = db(); cur5 = conn5.cursor()
                cur5.execute(
                    "UPDATE ml_order_queue SET processed=FALSE, error=%s WHERE order_id=%s",
                    (str(e)[:500], oid)
                )
                conn5.commit(); cur5.close()
            except Exception:
                pass


@app.post("/ml/webhook")
def ml_webhook():
    """Recebe notificações de novos pedidos do Mercado Livre."""
    _ensure_ml_schema()
    try:
        payload  = request.get_json(force=True, silent=True) or {}
        topic    = payload.get("topic", "") or payload.get("type", "")
        resource = payload.get("resource", "")

        if ("orders" in topic or "orders" in resource) and resource:
            order_id = resource.strip("/").split("/")[-1]
            if order_id.isdigit():
                _ml_queue_order(order_id)
                # Tenta processar imediatamente; se falhar, fila garante reprocessamento
                try:
                    _process_ml_order(int(order_id))
                    conn = db(); cur = conn.cursor()
                    cur.execute("UPDATE ml_order_queue SET processed=TRUE WHERE order_id=%s", (order_id,))
                    conn.commit(); cur.close()
                except Exception as ep:
                    print(f"[ML] Webhook: processamento adiado para {order_id}: {ep}")
    except Exception as e:
        print(f"[ML] Erro no webhook: {e}")

    return "", 200  # ML exige resposta 200 rápida


@app.post("/api/painel/ml/importar-pedido")
@painel_required
def api_ml_importar_pedido():
    """Importa manualmente um pedido ML pelo order_id."""
    body = request.get_json(force=True) or {}
    order_id_raw = str(body.get("order_id") or "").strip()
    if not order_id_raw.isdigit():
        return jsonify({"ok": False, "erro": "ID inválido — informe apenas números."}), 400
    order_id = int(order_id_raw)
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT id FROM ecommerce_pedidos WHERE ml_order_id=%s LIMIT 1", (order_id_raw,))
    if cur.fetchone():
        cur.close()
        return jsonify({"ok": False, "erro": "Pedido já importado anteriormente."})
    cur.close()
    try:
        _process_ml_order(order_id)
    except Exception as e:
        erro = str(e)
        if "403" in erro or "UNAUTHORIZED" in erro or "PolicyAgent" in erro:
            return jsonify({"ok": False, "erro": "ML bloqueou a consulta do pedido (PolicyAgent). Tente reconectar a conta ML em Mercado Livre → Reconectar."}), 400
        return jsonify({"ok": False, "erro": erro}), 500
    conn2 = db(); cur2 = conn2.cursor()
    cur2.execute("SELECT id FROM ecommerce_pedidos WHERE ml_order_id=%s LIMIT 1", (order_id_raw,))
    row = cur2.fetchone(); cur2.close()
    if row:
        return jsonify({"ok": True, "mensagem": f"Pedido {order_id} importado com sucesso!", "pedido_id": str(row["id"])})
    return jsonify({"ok": False, "erro": "Não foi possível buscar os detalhes do pedido na API do ML. Tente importar manualmente os dados abaixo."})


@app.post("/api/painel/ml/sincronizar-fila")
@painel_required
def api_ml_sincronizar_fila():
    """Processa pedidos ML enfileirados que ainda não foram criados."""
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ml_order_queue (
                order_id TEXT PRIMARY KEY,
                received_at TIMESTAMPTZ DEFAULT NOW(),
                processed BOOLEAN DEFAULT FALSE
            )
        """)
        cur.execute("SELECT order_id FROM ml_order_queue WHERE processed=FALSE ORDER BY received_at LIMIT 20")
        pendentes = [r["order_id"] for r in cur.fetchall()]
        cur.close()
    except Exception:
        return jsonify({"ok": False, "erro": "Erro ao acessar fila."}), 500

    importados = 0
    for oid in pendentes:
        try:
            conn2 = db(); cur2 = conn2.cursor()
            cur2.execute("SELECT id FROM ecommerce_pedidos WHERE ml_order_id=%s LIMIT 1", (oid,))
            if cur2.fetchone():
                cur2.execute("UPDATE ml_order_queue SET processed=TRUE WHERE order_id=%s", (oid,))
                conn2.commit(); cur2.close()
                continue
            cur2.close()
            _process_ml_order(int(oid))
            conn3 = db(); cur3 = conn3.cursor()
            cur3.execute("UPDATE ml_order_queue SET processed=TRUE WHERE order_id=%s", (oid,))
            conn3.commit(); cur3.close()
            importados += 1
        except Exception:
            pass

    return jsonify({"ok": True, "importados": importados, "pendentes": len(pendentes)})


# ── Painel ML: configuração e status ─────────────────────────────────────────

@app.get("/painel/ml")
@painel_required
def painel_ml():
    _ensure_ml_schema()
    connected = _ml_is_connected()

    # Conta pedidos ML desta loja
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) AS total,
               MAX(criado_em) AS ultimo
        FROM ecommerce_pedidos
        WHERE cnpjloja=%s AND origem='mercado_livre'
    """, (cnpjloja,))
    ml_stats = dict(cur.fetchone() or {})

    # Token info
    cur.execute("SELECT ml_user_id, updated_at FROM ml_tokens WHERE id=1")
    token_row = cur.fetchone()
    cur.close()

    return render_template("painel_ml.html",
        connected=connected,
        ml_stats=ml_stats,
        token_row=token_row,
        ml_app_id=ML_APP_ID,
    )


@app.post("/painel/ml/desconectar")
@painel_required
def painel_ml_desconectar():
    _ensure_ml_schema()
    conn = db(); cur = conn.cursor()
    cur.execute("DELETE FROM ml_tokens WHERE id=1")
    conn.commit(); cur.close()
    flash("Conta Mercado Livre desconectada.", "success")
    return redirect(url_for("painel_ml"))


# ── Publicar produto no ML ────────────────────────────────────────────────────

def _ml_api_post(path, body, token=None):
    """POST autenticado na API do ML. Retorna (dict_resp, status_code)."""
    if token is None:
        token = _ml_get_token()
    if not token:
        return None, 401
    data = json.dumps(body).encode()
    req = urllib.request.Request(ML_API_BASE + path, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()), e.code
        except Exception:
            return {"error": str(e)}, e.code
    except Exception as e:
        return {"error": str(e)}, 500


def _ml_api_put(path, body, token=None):
    """PUT autenticado na API do ML."""
    if token is None:
        token = _ml_get_token()
    if not token:
        return None, 401
    data = json.dumps(body).encode()
    req = urllib.request.Request(ML_API_BASE + path, data=data, method="PUT")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()), e.code
        except Exception:
            return {"error": str(e)}, e.code
    except Exception as e:
        return {"error": str(e)}, 500


@app.get("/api/painel/ml/status-produtos")
@painel_required
def api_ml_status_produtos():
    """Retorna status real dos itens do vendedor no ML (consulta a API do ML para status atualizado)."""
    _ensure_ml_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT mi.ml_item_id, mi.ean, mi.titulo, mi.preco, mi.status
        FROM ml_items mi
        WHERE mi.ean IN (
            SELECT barras FROM estoque WHERE cnpj=%s
            UNION
            SELECT ean FROM automatiza_estoque WHERE cnpj_loja=%s
        )
    """, (cnpjloja, cnpjloja))
    rows = cur.fetchall()
    cur.close()

    if not rows:
        return jsonify({})

    token = _ml_get_token()
    result = {}

    # Consulta status real via API do ML em lote (até 20 por vez)
    if token:
        ids = [r["ml_item_id"] for r in rows]
        ctx = ssl.create_default_context()
        for i in range(0, len(ids), 20):
            batch = ids[i:i+20]
            try:
                url = f"{ML_API_BASE}/items?ids={','.join(batch)}&attributes=id,status,sub_status,health,permalink"
                req = urllib.request.Request(url)
                req.add_header("Authorization", f"Bearer {token}")
                req.add_header("User-Agent", "Mozilla/5.0")
                with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                    data = json.loads(r.read())
                for entry in data:
                    body_item = entry.get("body") or {}
                    ml_id = body_item.get("id") or entry.get("id", "")
                    if ml_id:
                        result[ml_id] = {
                            "status":     body_item.get("status", "unknown"),
                            "sub_status": body_item.get("sub_status", []),
                            "health":     body_item.get("health"),
                            "permalink":  body_item.get("permalink", ""),
                        }
            except Exception:
                pass

    out = {}
    for r in rows:
        ml_info = result.get(r["ml_item_id"], {})
        real_status = ml_info.get("status") or r["status"]
        out[r["ean"]] = {
            "item_id":    r["ml_item_id"],
            "status":     real_status,
            "sub_status": ml_info.get("sub_status", []),
            "health":     ml_info.get("health"),
            "permalink":  ml_info.get("permalink", ""),
            "preco":      float(r["preco"] or 0),
        }

    # Atualiza status local no banco se mudou
    if result:
        conn2 = db(); cur2 = conn2.cursor()
        for r in rows:
            ml_info = result.get(r["ml_item_id"])
            if ml_info and ml_info.get("status") and ml_info["status"] != r["status"]:
                cur2.execute("UPDATE ml_items SET status=%s WHERE ml_item_id=%s",
                             (ml_info["status"], r["ml_item_id"]))
        conn2.commit(); cur2.close()

    return jsonify(out)


@app.post("/api/painel/ml/publicar")
@painel_required
def api_ml_publicar():
    """Publica ou atualiza um produto no Mercado Livre."""
    _ensure_ml_schema()
    if not _ml_is_connected():
        return jsonify({"ok": False, "erro": "Conta ML não conectada. Vá em Mercado Livre > Conectar."}), 400

    body = request.get_json(force=True) or {}
    ean         = (body.get("ean") or "").strip()
    titulo      = (body.get("titulo") or "").strip()[:60]
    preco       = float(body.get("preco") or 0)
    quantidade  = int(body.get("quantidade") or 1)
    descricao   = (body.get("descricao") or titulo).strip()
    imagens     = body.get("imagens") or []
    imagem_url  = (body.get("imagem_url") or "").strip()
    category_id = (body.get("category_id") or "MLB1196").strip()
    extra_attrs = body.get("atributos") or []
    descricao   = (body.get("descricao") or titulo).strip()
    shipping_cfg = body.get("shipping") or {}

    if not ean or not titulo or preco <= 0:
        return jsonify({"ok": False, "erro": "EAN, título e preço são obrigatórios."}), 400

    def _ml_transform_img(url):
        """Aplica transformações Cloudinary para requisitos de foto do ML."""
        if url and "res.cloudinary.com" in url and "/image/upload/" in url:
            parts = url.split("/image/upload/", 1)
            rest = parts[1]
            if rest.startswith("w_") or rest.startswith("c_") or rest.startswith("h_"):
                rest = rest.split("/", 1)[-1] if "/" in rest else rest
            return f"{parts[0]}/image/upload/w_1200,h_1200,c_pad,b_white,f_jpg,q_auto/{rest}"
        return url

    # Monta lista de fotos: usa imagens[] se enviado, senão imagem_url legado
    if imagens:
        pictures = [{"source": _ml_transform_img(u)} for u in imagens if u]
    elif imagem_url:
        pictures = [{"source": _ml_transform_img(imagem_url)}]
    else:
        pictures = []

    token = _ml_get_token()
    conn = db(); cur = conn.cursor()

    # Verifica se já existe item publicado para este EAN
    cur.execute("SELECT ml_item_id, status FROM ml_items WHERE ean=%s", (ean,))
    existing = cur.fetchone()

    if existing:
        ml_item_id  = existing["ml_item_id"]
        local_status = existing.get("status", "")

        # Verifica status real no ML antes de tentar atualizar
        real_status = local_status
        try:
            item_data = _ml_api_get(f"/items/{ml_item_id}?attributes=id,status,sub_status", token)
            if item_data:
                real_status = item_data.get("status", local_status)
        except Exception:
            pass

        if real_status == "under_review":
            cur.close()
            return jsonify({
                "ok": False,
                "erro": (f"O anúncio {ml_item_id} está em revisão pelo Mercado Livre (fotos em análise). "
                         "Aguarde a aprovação antes de atualizar. "
                         "Você pode acompanhar em Painel → Mercado Livre.")
            }), 400

        update_payload = {"price": preco, "available_quantity": quantidade}
        if pictures:
            update_payload["pictures"] = pictures
        resp, code = _ml_api_put(f"/items/{ml_item_id}", update_payload, token)
        if code not in (200, 201):
            cur.close()
            causes = resp.get("cause", []) if isinstance(resp, dict) else []
            erros = [c.get("message", "") for c in causes if c.get("type") == "error"]
            msg = "; ".join(erros[:3]) if erros else str(resp)
            return jsonify({"ok": False, "erro": f"Erro ML {code}: {msg}"}), 400
        cur.execute("""
            UPDATE ml_items SET preco=%s, status='active', updated_at=NOW() WHERE ml_item_id=%s
        """, (preco, ml_item_id))
        conn.commit(); cur.close()
        return jsonify({"ok": True, "ml_item_id": ml_item_id, "acao": "atualizado"})

    def _build_payload(cat_id):
        p = {
            "title": titulo,
            "category_id": cat_id,
            "price": preco,
            "currency_id": "BRL",
            "available_quantity": quantidade,
            "buying_mode": "buy_it_now",
            "listing_type_id": "gold_special",
            "condition": "new",
            "description": {"plain_text": descricao},
            "attributes": [{"id": "GTIN", "value_name": ean}] + [
                {"id": a["id"], "value_name": a["value_name"]}
                for a in extra_attrs if a.get("id") and a.get("value_name")
            ],
        }
        if pictures:
            p["pictures"] = pictures
        # Configuração de frete
        if shipping_cfg:
            p["shipping"] = {
                "mode": "me2",
                "local_pick_up": bool(shipping_cfg.get("local_pick_up")),
                "free_shipping": bool(shipping_cfg.get("free_shipping")),
            }
        return p

    resp, code = _ml_api_post("/items", _build_payload(category_id), token)

    # Se categoria foi migrada, tenta com o novo ID automaticamente
    if code not in (200, 201):
        causes = resp.get("cause", []) if isinstance(resp, dict) else []
        migrated_to = None
        blocking_errors = []
        for c in causes:
            if c.get("code") == "item.category_id.migrated":
                import re as _re
                m = _re.search(r'MLB\d+', c.get("message", ""))
                if m:
                    migrated_to = m.group(0)
            if c.get("type") == "error" and c.get("code") != "item.category_id.migrated":
                blocking_errors.append(c.get("message", c.get("code", "")))

        if migrated_to and not blocking_errors:
            # Retry com categoria migrada
            resp, code = _ml_api_post("/items", _build_payload(migrated_to), token)
            category_id = migrated_to  # atualiza para salvar correto
        elif migrated_to and blocking_errors:
            cur.close()
            err_msgs = "; ".join(blocking_errors[:2])
            return jsonify({"ok": False,
                            "erro": f"Categoria errada (migrada para {migrated_to} que exige atributos específicos). "
                                    f"Erros: {err_msgs}. Tente uma categoria diferente."}), 400

    if code not in (200, 201):
        cur.close()
        causes = resp.get("cause", []) if isinstance(resp, dict) else []
        erros = [c.get("message", "") for c in causes if c.get("type") == "error"]
        msg = "; ".join(erros[:2]) if erros else str(resp)
        return jsonify({"ok": False, "erro": f"Erro ML {code}: {msg}"}), 400

    ml_item_id = resp.get("id", "")
    permalink  = resp.get("permalink", "")
    _cnpjloja_pub = session.get("cnpjloja")
    cur.execute("""
        INSERT INTO ml_items (ml_item_id, ean, titulo, preco, category_id, cnpjloja, status, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'active', NOW())
        ON CONFLICT (ml_item_id) DO UPDATE
          SET preco=%s, category_id=%s, cnpjloja=%s, status='active', updated_at=NOW()
    """, (ml_item_id, ean, titulo, preco, category_id, _cnpjloja_pub, preco, category_id, _cnpjloja_pub))
    conn.commit(); cur.close()
    return jsonify({"ok": True, "ml_item_id": ml_item_id, "permalink": permalink, "acao": "publicado"})


@app.post("/api/painel/ml/pausar")
@painel_required
def api_ml_pausar():
    """Pausa (remove do ar) um anúncio no ML."""
    _ensure_ml_schema()
    body = request.get_json(force=True) or {}
    ean = (body.get("ean") or "").strip()
    if not ean:
        return jsonify({"ok": False, "erro": "EAN obrigatório."}), 400

    token = _ml_get_token()
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT ml_item_id FROM ml_items WHERE ean=%s", (ean,))
    row = cur.fetchone()
    if not row:
        cur.close()
        return jsonify({"ok": False, "erro": "Produto não publicado no ML."}), 404

    ml_item_id = row["ml_item_id"]
    resp, code = _ml_api_put(f"/items/{ml_item_id}", {"status": "paused"}, token)
    if code not in (200, 201):
        cur.close()
        return jsonify({"ok": False, "erro": f"Erro ML {code}: {resp}"}), 400

    cur.execute("UPDATE ml_items SET status='paused', updated_at=NOW() WHERE ml_item_id=%s", (ml_item_id,))
    conn.commit(); cur.close()
    return jsonify({"ok": True, "ml_item_id": ml_item_id})


@app.get("/api/painel/ml/debug-categoria")
@painel_required
def api_ml_debug_categoria():
    """Debug: mostra resposta bruta do category_predictor do ML."""
    titulo = (request.args.get("titulo") or "whey protein").strip()
    token = _ml_get_token()
    if not token:
        return jsonify({"erro": "sem token", "token_ok": False})
    ctx = ssl.create_default_context()
    resultados = {}
    # Testa category_predictor
    try:
        url = f"https://api.mercadolibre.com/sites/MLB/category_predictor/predict?title={urllib.parse.quote(titulo)}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            resultados["predictor"] = {"status": r.status, "body": json.loads(r.read())}
    except Exception as e:
        resultados["predictor_erro"] = str(e)
    # Testa search com access_token como query param
    try:
        url2 = (f"https://api.mercadolibre.com/sites/MLB/search"
                f"?q={urllib.parse.quote(titulo)}&limit=3&access_token={urllib.parse.quote(token)}")
        req2 = urllib.request.Request(url2)
        req2.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req2, context=ctx, timeout=10) as r2:
            body2 = json.loads(r2.read())
            resultados["search"] = {"total": body2.get("paging",{}).get("total",0),
                                     "cat_ids": [x.get("category_id") for x in body2.get("results",[])[:3]]}
    except Exception as e2:
        resultados["search_erro"] = str(e2)
    # Testa /sites/MLB/categories (publico, sem auth)
    try:
        url3 = "https://api.mercadolibre.com/sites/MLB/categories"
        req3 = urllib.request.Request(url3, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req3, context=ctx, timeout=8) as r3:
            cats = json.loads(r3.read())
            resultados["categories_tree_ok"] = True
            resultados["top_cats"] = [{"id": c["id"], "name": c["name"]} for c in cats]
    except Exception as e3:
        resultados["categories_tree_erro"] = str(e3)
    return jsonify({"token_ok": True, "titulo": titulo, **resultados})


@app.get("/api/painel/ml/categoria-de-url")
@painel_required
def api_ml_categoria_de_url():
    """Baixa HTML de página do ML e extrai category_id do estado embutido."""
    url = (request.args.get("url") or "").strip()
    if not url or "mercado" not in url.lower():
        return jsonify({"ok": False, "erro": "URL inválida"}), 400

    import gzip as _gzip
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        req.add_header("Accept", "text/html,application/xhtml+xml,*/*;q=0.9")
        req.add_header("Accept-Language", "pt-BR,pt;q=0.9")
        req.add_header("Accept-Encoding", "gzip, deflate")
        req.add_header("Referer", "https://www.mercadolivre.com.br/")
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            raw = r.read()
            enc = r.headers.get("Content-Encoding", "")
        try:
            html = _gzip.decompress(raw).decode("utf-8", errors="ignore")
        except Exception:
            html = raw.decode("utf-8", errors="ignore")
    except Exception as e:
        return jsonify({"ok": False, "erro": f"Não conseguiu acessar a página: {e}"}), 502

    # Padrões de category_id no JSON embutido do ML
    for pat in [
        r'"categoryId"\s*:\s*"(MLB\d+)"',
        r'"category_id"\s*:\s*"(MLB\d+)"',
        r'categoryId%22%3A%22(MLB\d+)',
        r'"CATEGORY_ID"\s*:\s*"(MLB\d+)"',
        r'category_id=(MLB\d+)',
    ]:
        m = re.search(pat, html)
        if m:
            cat_id = m.group(1)
            token = _ml_get_token()
            cat_name = cat_id
            if token:
                try:
                    req2 = urllib.request.Request(f"{ML_API_BASE}/categories/{cat_id}")
                    req2.add_header("Authorization", f"Bearer {token}")
                    with urllib.request.urlopen(req2, context=ctx, timeout=8) as r2:
                        cd = json.loads(r2.read())
                        path = cd.get("path_from_root", [])
                        cat_name = " › ".join(p["name"] for p in path[-3:]) if len(path) >= 2 else cd.get("name", cat_id)
                except Exception:
                    pass
            return jsonify({"ok": True, "category_id": cat_id, "category_name": cat_name})

    return jsonify({"ok": False, "erro": "category_id não encontrado no HTML da página"}), 404


@app.get("/api/painel/ml/categoria-por-item")
@painel_required
def api_ml_categoria_por_item():
    """Resolve category_id de um item ou produto ML usando o bearer token da loja."""
    item_id = (request.args.get("item_id") or "").strip().upper()
    ean     = (request.args.get("ean") or "").strip()
    token   = _ml_get_token()
    if not token:
        return jsonify({"ok": False, "erro": "Token ML não disponível."}), 401

    ctx = ssl.create_default_context()

    def _get_auth(path):
        req = urllib.request.Request(ML_API_BASE + path)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            return json.loads(r.read())

    def _resolve_cat_name(cat_id):
        try:
            cd = _get_auth(f"/categories/{cat_id}")
            path = cd.get("path_from_root", [])
            return " › ".join(p["name"] for p in path[-3:]) if len(path) >= 2 else cd.get("name", cat_id)
        except Exception:
            return cat_id

    # 1) Tenta como item listing (ex: MLB5377686510)
    if item_id.startswith("MLB"):
        try:
            data = _get_auth(f"/items/{item_id}?attributes=category_id")
            cat_id = data.get("category_id", "")
            if cat_id and cat_id.startswith("MLB"):
                return jsonify({"ok": True, "category_id": cat_id, "category_name": _resolve_cat_name(cat_id)})
        except Exception as e:
            pass

        # 2) Tenta como product (ex: MLB21776085)
        try:
            data = _get_auth(f"/products/{item_id}")
            cat_id = data.get("category_id", "")
            if cat_id and cat_id.startswith("MLB"):
                return jsonify({"ok": True, "category_id": cat_id, "category_name": _resolve_cat_name(cat_id)})
        except Exception:
            pass

        # 3) Tenta como categoria diretamente
        try:
            data = _get_auth(f"/categories/{item_id}")
            if data.get("id", "").startswith("MLB"):
                return jsonify({"ok": True, "category_id": data["id"], "category_name": _resolve_cat_name(data["id"])})
        except Exception:
            pass

    # 4) Tenta busca por EAN via products/search
    if ean and ean.isdigit():
        try:
            data = _get_auth(f"/products/search?site_id=MLB&product_identifier={ean}")
            results = data.get("results", [])
            if results:
                # category_id is a numeric MLB code; domain_id is like "MLB-SUPPLEMENTS" (NOT valid)
                cat_id = results[0].get("category_id") or ""
                # Fetch full product if category_id not in search result
                if not cat_id:
                    prod_id = results[0].get("id", "")
                    if prod_id:
                        try:
                            prod = _get_auth(f"/products/{prod_id}")
                            cat_id = prod.get("category_id") or ""
                        except Exception:
                            pass
                # domain_id like "MLB-SUPPLEMENTS" is NOT a category_id (reject strings with hyphens)
                if cat_id and re.match(r'^MLB\d+$', cat_id):
                    return jsonify({"ok": True, "category_id": cat_id, "category_name": _resolve_cat_name(cat_id)})
        except Exception:
            pass

    return jsonify({"ok": False, "erro": f"Não foi possível resolver categoria para {item_id or ean}"}), 404


@app.get("/api/painel/ml/categorias-usadas")
@painel_required
def api_ml_categorias_usadas():
    """Retorna categorias únicas já usadas em publicações ML desta loja."""
    _ensure_ml_schema()
    token = _ml_get_token()
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT DISTINCT category_id
            FROM ml_items
            WHERE category_id IS NOT NULL AND category_id != '' AND status = 'active'
            ORDER BY category_id
        """)
        cat_ids = [r["category_id"] for r in cur.fetchall()]
    except Exception:
        cat_ids = []
    finally:
        cur.close()

    if not cat_ids or not token:
        return jsonify({"ok": True, "categorias": []})

    ctx = ssl.create_default_context()
    categorias = []
    for cat_id in cat_ids[:10]:
        try:
            req = urllib.request.Request(f"{ML_API_BASE}/categories/{cat_id}")
            req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, context=ctx, timeout=6) as r:
                cd = json.loads(r.read())
                path = cd.get("path_from_root", [])
                name = " › ".join(p["name"] for p in path[-3:]) if len(path) >= 2 else cd.get("name", cat_id)
                categorias.append({"category_id": cat_id, "category_name": name})
        except Exception:
            categorias.append({"category_id": cat_id, "category_name": cat_id})
    return jsonify({"ok": True, "categorias": categorias})


@app.get("/api/painel/ml/navegar-categorias")
@painel_required
def api_ml_navegar_categorias():
    """Navega a árvore de categorias do ML via token da loja (endpoint /categories não é bloqueado pelo PolicyAgent)."""
    cat_id = (request.args.get("cat_id") or "root").strip()
    token = _ml_get_token()
    if not token:
        return jsonify({"ok": False, "erro": "Token ML não disponível."}), 401
    ctx = ssl.create_default_context()
    try:
        # "root" retorna todas as categorias de primeiro nível do ML Brasil
        if cat_id.lower() == "root":
            req = urllib.request.Request(f"{ML_API_BASE}/sites/MLB/categories")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                cats = json.loads(r.read())
            return jsonify({
                "ok": True,
                "id": "root",
                "nome": "Todas as categorias",
                "breadcrumb": [],
                "filhos": [{"id": c["id"], "name": c["name"]} for c in cats],
                "is_leaf": False,
            })

        req = urllib.request.Request(f"{ML_API_BASE}/categories/{cat_id}")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            data = json.loads(r.read())
        path = data.get("path_from_root", [])
        children = data.get("children_categories", [])
        return jsonify({
            "ok": True,
            "id": data["id"],
            "nome": data.get("name", cat_id),
            "breadcrumb": [{"id": p["id"], "name": p["name"]} for p in path],
            "filhos": [{"id": c["id"], "name": c["name"]} for c in children],
            "is_leaf": len(children) == 0,
        })
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.get("/api/painel/ml/config-entrega")
@painel_required
def api_ml_config_entrega():
    """Retorna as configurações de entrega da loja para pré-preencher o modal ML."""
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT aceita_entrega, raio_entrega_km, cobra_frete, valor_frete, pedido_minimo_entrega
        FROM ecommerce_config_loja WHERE cnpjloja=%s LIMIT 1
    """, (cnpjloja,))
    row = cur.fetchone() or {}
    cur.close()
    return jsonify({
        "ok": True,
        "aceita_entrega":         bool(row.get("aceita_entrega")),
        "raio_entrega_km":        float(row.get("raio_entrega_km") or 0),
        "cobra_frete":            bool(row.get("cobra_frete")),
        "valor_frete":            float(row.get("valor_frete") or 0),
        "pedido_minimo_entrega":  float(row.get("pedido_minimo_entrega") or 0),
    })


@app.post("/api/painel/ml/upload-imagem")
@painel_required
def api_ml_upload_imagem():
    """Faz upload de imagem: tenta Cloudinary primeiro, fallback na API de fotos do ML."""
    f = request.files.get("imagem")
    if not f:
        return jsonify({"ok": False, "erro": "Nenhum arquivo enviado"}), 400

    file_bytes = f.read()

    # Tenta Cloudinary se disponível
    if _CLOUDINARY_OK:
        try:
            import cloudinary.uploader as _cu
            result = _cu.upload(
                file_bytes,
                folder="ml_fotos",
                resource_type="image",
                transformation=[
                    {"width": 1200, "height": 1200, "crop": "pad", "background": "white"},
                    {"format": "jpg", "quality": "auto"},
                ],
            )
            return jsonify({"ok": True, "url": result["secure_url"], "via": "cloudinary"})
        except Exception:
            pass  # cai para fallback ML

    # Fallback: API de upload de fotos do ML
    token = _ml_get_token()
    if not token:
        return jsonify({"ok": False, "erro": "Token ML não disponível e Cloudinary não configurado."}), 500
    try:
        boundary = "---PoupaquiUpload"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="foto.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()

        ctx = ssl.create_default_context()
        req = urllib.request.Request(
            f"{ML_API_BASE}/pictures/items/upload",
            data=body,
            method="POST",
        )
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
            data = json.loads(r.read())
        url = data.get("secure_url") or data.get("url") or (data.get("variations") or [{}])[0].get("secure_url", "")
        if url:
            return jsonify({"ok": True, "url": url, "via": "ml"})
        return jsonify({"ok": False, "erro": f"ML não retornou URL: {data}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.get("/api/painel/ml/atributos-categoria")
@painel_required
def api_ml_atributos_categoria():
    """Retorna atributos obrigatórios de uma categoria ML para exibir no formulário."""
    cat_id = (request.args.get("cat_id") or "").strip()
    if not cat_id:
        return jsonify({"ok": False, "erro": "cat_id obrigatório"}), 400
    token = _ml_get_token()
    if not token:
        return jsonify({"ok": False, "erro": "Token ML não disponível"}), 401
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(f"{ML_API_BASE}/categories/{cat_id}/attributes")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            attrs = json.loads(r.read())
        required = []
        for a in attrs:
            tags = a.get("tags", {})
            if not (tags.get("required") or tags.get("catalog_required")):
                continue
            if a.get("id") == "GTIN":
                continue  # preenchido automaticamente com EAN
            required.append({
                "id": a["id"],
                "name": a.get("name", a["id"]),
                "value_type": a.get("value_type", "string"),
                "allowed_values": [{"id": v["id"], "name": v["name"]} for v in a.get("values", [])[:40]],
            })
        return jsonify({"ok": True, "atributos": required})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.get("/api/painel/ml/sugerir-categoria")
@painel_required
def api_ml_sugerir_categoria():
    """Usa category_predictor autenticado do ML para sugerir categoria folha."""
    titulo = (request.args.get("titulo") or request.args.get("q") or "").strip()
    if not titulo:
        return jsonify({"ok": False, "erro": "Parâmetro titulo obrigatório."}), 400
    try:
        token = _ml_get_token()
        if not token:
            return jsonify({"ok": False, "erro": "Token ML não disponível. Reconecte em Mercado Livre."}), 401

        ctx = ssl.create_default_context()

        def _ml_get_raw(url, token):
            # Tenta com access_token como query param (funciona mesmo de IPs bloqueados)
            sep = "&" if "?" in url else "?"
            full_url = url + sep + "access_token=" + urllib.parse.quote(token)
            req = urllib.request.Request(full_url)
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                return json.loads(r.read()), r.status

        # Tenta category_predictor
        pred_url = (f"https://api.mercadolibre.com/sites/MLB/category_predictor/predict"
                    f"?title={urllib.parse.quote(titulo)}")
        try:
            data, status = _ml_get_raw(pred_url, token)
            cat_id   = data.get("id", "")
            cat_name = data.get("name", "")
            if cat_id:
                # Resolve caminho completo
                try:
                    cat_data, _ = _ml_get_raw(f"https://api.mercadolibre.com/categories/{cat_id}", token)
                    path = cat_data.get("path_from_root", [])
                    if len(path) >= 2:
                        cat_name = " › ".join(p["name"] for p in path[-3:])
                except Exception:
                    pass
                return jsonify({"ok": True, "sugestoes": [{"category_id": cat_id, "category_name": cat_name}]})
        except Exception as pred_err:
            pass  # Cai para fallback abaixo

        # Fallback: products/search por GTIN se o titulo for numérico (EAN)
        if titulo.isdigit():
            try:
                prod_url = (f"https://api.mercadolibre.com/products/search"
                            f"?site_id=MLB&product_identifier={titulo}")
                prod_data, _ = _ml_get_raw(prod_url, token)
                results = prod_data.get("results", [])
                if results:
                    cat_id = results[0].get("domain_id", "") or ""
                    # Tenta pegar category_id do primeiro resultado de anuncio por EAN
                    search_url = (f"https://api.mercadolibre.com/sites/MLB/search"
                                  f"?q={titulo}&limit=5")
                    sd, _ = _ml_get_raw(search_url, token)
                    items = sd.get("results", [])
                    seen = {}
                    for it in items:
                        cid = it.get("category_id", "")
                        if cid and cid not in seen:
                            seen[cid] = True
                    sugestoes = []
                    for cid in list(seen.keys())[:3]:
                        try:
                            cd, _ = _ml_get_raw(f"https://api.mercadolibre.com/categories/{cid}", token)
                            path = cd.get("path_from_root", [])
                            name = " › ".join(p["name"] for p in path[-3:]) if len(path) >= 2 else cd.get("name", cid)
                        except Exception:
                            name = cid
                        sugestoes.append({"category_id": cid, "category_name": name})
                    if sugestoes:
                        return jsonify({"ok": True, "sugestoes": sugestoes})
            except Exception:
                pass

        return jsonify({"ok": False, "erro": "Não foi possível determinar a categoria. Digite o ID manualmente."}), 404
    except Exception as ex:
        return jsonify({"ok": False, "erro": str(ex)}), 500


@app.post("/api/painel/ml/publicar-lote")
@painel_required
def api_ml_publicar_lote():
    """Publica múltiplos produtos no ML em sequência."""
    _ensure_ml_schema()
    if not _ml_is_connected():
        return jsonify({"ok": False, "erro": "Conta ML não conectada."}), 400

    body = request.get_json(force=True) or {}
    produtos     = body.get("produtos") or []
    category_id  = (body.get("category_id") or "MLB1196").strip()
    qty_padrao   = int(body.get("quantidade_padrao") or 1)

    if not produtos:
        return jsonify({"ok": False, "erro": "Nenhum produto enviado."}), 400

    token = _ml_get_token()
    conn = db(); cur = conn.cursor()
    resultados = []

    for item in produtos:
        ean        = (item.get("ean") or "").strip()
        titulo     = (item.get("titulo") or "").strip()[:60]
        preco      = float(item.get("preco") or 0)
        imagem_url = (item.get("imagem_url") or "").strip()
        quantidade = int(item.get("quantidade") or qty_padrao)

        if not ean or not titulo or preco <= 0:
            resultados.append({"ean": ean, "ok": False, "erro": "Dados incompletos"})
            continue

        try:
            cur.execute("SELECT ml_item_id, status FROM ml_items WHERE ean=%s", (ean,))
            existing = cur.fetchone()

            if existing:
                ml_item_id = existing["ml_item_id"]
                resp, code = _ml_api_put(f"/items/{ml_item_id}",
                                         {"price": preco, "available_quantity": quantidade}, token)
                if code not in (200, 201):
                    resultados.append({"ean": ean, "ok": False, "erro": f"ML {code}"})
                    continue
                cur.execute("UPDATE ml_items SET preco=%s, status='active', updated_at=NOW() WHERE ml_item_id=%s",
                            (preco, ml_item_id))
                conn.commit()
                resultados.append({"ean": ean, "ok": True, "ml_item_id": ml_item_id, "acao": "atualizado"})
            else:
                pictures = [{"source": imagem_url}] if imagem_url else []
                payload = {
                    "title": titulo,
                    "category_id": category_id,
                    "price": preco,
                    "currency_id": "BRL",
                    "available_quantity": quantidade,
                    "buying_mode": "buy_it_now",
                    "listing_type_id": "gold_special",
                    "condition": "new",
                    "description": {"plain_text": titulo},
                }
                if pictures:
                    payload["pictures"] = pictures
                resp, code = _ml_api_post("/items", payload, token)
                if code not in (200, 201):
                    resultados.append({"ean": ean, "ok": False, "erro": f"ML {code}: {resp.get('message','')}"})
                    continue
                ml_item_id = resp.get("id", "")
                cur.execute("""
                    INSERT INTO ml_items (ml_item_id, ean, titulo, preco, status, updated_at)
                    VALUES (%s, %s, %s, %s, 'active', NOW())
                    ON CONFLICT (ml_item_id) DO UPDATE SET preco=%s, status='active', updated_at=NOW()
                """, (ml_item_id, ean, titulo, preco, preco))
                conn.commit()
                resultados.append({"ean": ean, "ok": True, "ml_item_id": ml_item_id, "acao": "publicado"})
        except Exception as ex:
            resultados.append({"ean": ean, "ok": False, "erro": str(ex)})

    cur.close()
    publicados = sum(1 for r in resultados if r["ok"])
    erros      = len(resultados) - publicados
    return jsonify({"ok": True, "resultados": resultados, "publicados": publicados, "erros": erros})


# ── Endereço do vendedor no ML ────────────────────────────────────────────────

_UF_TO_ML_STATE = {
    "AC": "BR-AC", "AL": "BR-AL", "AP": "BR-AP", "AM": "BR-AM", "BA": "BR-BA",
    "CE": "BR-CE", "DF": "BR-DF", "ES": "BR-ES", "GO": "BR-GO", "MA": "BR-MA",
    "MT": "BR-MT", "MS": "BR-MS", "MG": "BR-MG", "PA": "BR-PA", "PB": "BR-PB",
    "PR": "BR-PR", "PE": "BR-PE", "PI": "BR-PI", "RJ": "BR-RJ", "RN": "BR-RN",
    "RS": "BR-RS", "RO": "BR-RO", "RR": "BR-RR", "SC": "BR-SC", "SP": "BR-SP",
    "SE": "BR-SE", "TO": "BR-TO",
}


def _parse_endereco_banco(endereco2, endereco, uf):
    """Extrai campos de endereço a partir do texto do banco para pré-preencher o formulário."""
    texto = (endereco2 or endereco or "").strip()
    cep = ""
    m_cep = re.search(r"\b(\d{5})-?(\d{3})\b", texto)
    if m_cep:
        cep = m_cep.group(1) + m_cep.group(2)

    # Remove CEP do texto para facilitar parsing do restante
    sem_cep = re.sub(r",?\s*\d{5}-?\d{3}", "", texto).strip(" ,")

    partes = [p.strip() for p in sem_cep.split(",") if p.strip()]

    logradouro = partes[0] if len(partes) >= 1 else ""
    numero     = ""
    bairro     = ""
    cidade     = ""
    estado     = uf or ""

    # Tenta extrair número embutido no logradouro ("Rua X, 123")
    m_num = re.search(r"\s+(\d+\w*)\s*$", logradouro)
    if m_num:
        numero    = m_num.group(1)
        logradouro = logradouro[:m_num.start()].strip()

    if len(partes) >= 2:
        parte2 = partes[1]
        m_num2 = re.match(r"^(\d+\w*)\s*$", parte2)
        if m_num2:
            numero = m_num2.group(1)
        else:
            bairro = parte2

    if len(partes) >= 3 and not bairro:
        bairro = partes[2]

    # Cidade é a última parte que não seja UF (2 letras) nem CEP
    for p in reversed(partes):
        if re.match(r"^[A-Za-z]{2}$", p):
            estado = p.upper()
        elif re.match(r"^\d{5}-?\d{3}$", p):
            continue
        elif len(p) >= 3 and not re.match(r"^\d+$", p):
            cidade = p
            break

    return {
        "logradouro": logradouro,
        "numero":     numero,
        "bairro":     bairro,
        "cidade":     cidade,
        "estado":     estado,
        "cep":        cep,
    }


@app.get("/api/painel/ml/dados-vendedor")
@painel_required
def api_ml_dados_vendedor():
    """Retorna endereço atual do vendedor no ML + endereço do banco como sugestão."""
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute(
        "SELECT endereco, endereco2, uf FROM users WHERE cnpjloja=%s LIMIT 1",
        (cnpjloja,)
    )
    u = cur.fetchone()
    cur.close()

    sugestao = _parse_endereco_banco(
        u["endereco2"] if u else None,
        u["endereco"]  if u else None,
        u["uf"]        if u else None,
    )

    token = _ml_get_token()
    if not token:
        return jsonify({"ok": True, "ml_address": None, "sugestao": sugestao})

    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(f"{ML_API_BASE}/users/me")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            me = json.loads(r.read())

        addr = me.get("address") or {}
        ml_address = {
            "user_id":    me.get("id"),
            "logradouro": addr.get("address") or "",
            "numero":     addr.get("number") or "",
            "bairro":     (addr.get("neighborhood") or {}).get("name") or "",
            "cidade":     (addr.get("city") or {}).get("name") or "",
            "estado":     ((addr.get("state") or {}).get("id") or "").replace("BR-", ""),
            "cep":        (addr.get("zip_code") or "").replace("-", ""),
        }
        return jsonify({"ok": True, "ml_address": ml_address, "sugestao": sugestao})
    except Exception as e:
        return jsonify({"ok": True, "ml_address": None, "sugestao": sugestao, "aviso": str(e)})


@app.post("/api/painel/ml/salvar-endereco")
@painel_required
def api_ml_salvar_endereco():
    """Salva endereço do vendedor via API do ML (PUT /users/{id})."""
    token = _ml_get_token()
    if not token:
        return jsonify({"ok": False, "erro": "Token ML não disponível."}), 401

    body = request.get_json(force=True) or {}
    logradouro = (body.get("logradouro") or "").strip()
    numero     = (body.get("numero") or "").strip()
    bairro     = (body.get("bairro") or "").strip()
    cidade     = (body.get("cidade") or "").strip()
    estado_uf  = (body.get("estado") or "").strip().upper()
    cep        = re.sub(r"\D", "", body.get("cep") or "")

    if not logradouro or not cidade or not cep or len(cep) != 8:
        return jsonify({"ok": False, "erro": "Preencha: logradouro, cidade e CEP (8 dígitos)."}), 400

    endereco_ml = logradouro
    if numero:
        endereco_ml = f"{logradouro}, {numero}"

    state_id = _UF_TO_ML_STATE.get(estado_uf, f"BR-{estado_uf}")

    # Busca o user_id no ML
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(f"{ML_API_BASE}/users/me")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            me = json.loads(r.read())
        user_id = me.get("id")
    except Exception as e:
        return jsonify({"ok": False, "erro": f"Erro ao buscar usuário ML: {e}"}), 500

    payload = {
        "address": {
            "address":      endereco_ml,
            "zip_code":     cep,
            "city":         {"name": cidade},
            "state":        {"id": state_id},
            "neighborhood": {"name": bairro},
        }
    }

    resp, code = _ml_api_put(f"/users/{user_id}", payload, token)
    if code in (200, 201):
        return jsonify({"ok": True, "mensagem": "Endereço salvo com sucesso no Mercado Livre!"})
    erro_msg = (resp or {}).get("message") or (resp or {}).get("error") or f"Erro {code}"
    return jsonify({"ok": False, "erro": f"ML retornou: {erro_msg}"}), 400


# ─── E-MAIL TRANSACIONAL ──────────────────────────────────────────────────────

_STATUS_LABEL = {
    "pendente":  "Pendente",
    "pago":      "Pago ✓",
    "enviado":   "Enviado / Saiu para entrega",
    "entregue":  "Entregue ✓",
    "cancelado": "Cancelado",
}


def _disparar_emails_novos_pedidos(pedidos_result: list, itens_por_loja: dict, cliente: dict):
    """Envia confirmação ao consumidor e alerta para cada loja. Roda em thread daemon."""
    if not RESEND_API_KEY:
        return
    try:
        consumidor_email = (cliente.get("email") or "").strip()
        consumidor_nome  = (cliente.get("nome") or "Cliente").split()[0]

        for ped in pedidos_result:
            pedido_id = ped.get("id", "")
            cnpjloja  = ped.get("cnpjloja", "")
            razao     = ped.get("razao") or "Farmácia"
            total_fmt = f"R$ {ped.get('total', 0):.2f}".replace(".", ",")
            tipo      = "Entrega" if ped.get("tipo_entrega") == "entrega" else "Retirada na loja"
            pagamento = ped.get("pagamento", "").replace("_", " ").title()
            itens     = itens_por_loja.get(cnpjloja, [])

            itens_html = "".join(
                f"<tr><td style='padding:6px 0;border-bottom:1px solid #eee'>{html.escape(str(i.get('nome','Produto')))}</td>"
                f"<td style='padding:6px 0;border-bottom:1px solid #eee;text-align:right'>{i.get('qty',1)}×</td>"
                f"<td style='padding:6px 0;border-bottom:1px solid #eee;text-align:right'>R$ {float(i.get('preco',0)):.2f}</td></tr>"
                for i in itens
            )
            itens_table = (
                f"<table width='100%' cellspacing='0' style='margin:12px 0'><thead>"
                f"<tr><th style='text-align:left;font-size:.8rem;color:#888'>Produto</th>"
                f"<th style='text-align:right;font-size:.8rem;color:#888'>Qtd</th>"
                f"<th style='text-align:right;font-size:.8rem;color:#888'>Preço</th></tr></thead>"
                f"<tbody>{itens_html}</tbody></table>"
            ) if itens_html else ""

            # — Consumidor
            if consumidor_email and "@" in consumidor_email:
                corpo_consumidor = (
                    f"<p>Olá, <b>{html.escape(consumidor_nome)}</b>!</p>"
                    f"<p>Seu pedido <b>#{pedido_id[:8].upper()}</b> foi recebido com sucesso.</p>"
                    f"<div class='info-box'>"
                    f"<b>Farmácia:</b> {html.escape(razao)}<br>"
                    f"<b>Total:</b> {total_fmt}<br>"
                    f"<b>Forma de pagamento:</b> {html.escape(pagamento)}<br>"
                    f"<b>Modalidade:</b> {html.escape(tipo)}"
                    f"</div>"
                    f"{itens_table}"
                    f"<p>Acompanhe seus pedidos acessando <b>Meus Pedidos</b> no site.</p>"
                )
                _send_email(
                    consumidor_email,
                    f"✅ Pedido #{pedido_id[:8].upper()} recebido — {razao}",
                    _email_html_wrapper("Pedido recebido!", corpo_consumidor),
                )

            # — Loja: busca e-mail de notificação
            try:
                conn2 = _new_conn()
                cur2  = conn2.cursor()
                cur2.execute(
                    "SELECT email_notificacao FROM ecommerce_config_loja WHERE cnpjloja=%s LIMIT 1",
                    (cnpjloja,),
                )
                row = cur2.fetchone()
                cur2.close()
                conn2.close()
                loja_email = ((row or {}).get("email_notificacao") or "").strip()
            except Exception:
                loja_email = ""

            if loja_email and "@" in loja_email:
                corpo_loja = (
                    f"<p>Um novo pedido foi recebido!</p>"
                    f"<div class='info-box'>"
                    f"<b>Pedido:</b> #{pedido_id[:8].upper()}<br>"
                    f"<b>Cliente:</b> {html.escape(cliente.get('nome',''))} · {html.escape(cliente.get('telefone',''))}<br>"
                    f"<b>Total:</b> {total_fmt}<br>"
                    f"<b>Modalidade:</b> {html.escape(tipo)}<br>"
                    f"<b>Pagamento:</b> {html.escape(pagamento)}"
                    f"</div>"
                    f"{itens_table}"
                    f"<p>Acesse o <b>Painel da Loja</b> para ver os detalhes e atualizar o status.</p>"
                )
                _send_email(
                    loja_email,
                    f"🛒 Novo pedido #{pedido_id[:8].upper()} — {razao}",
                    _email_html_wrapper("Novo pedido recebido", corpo_loja),
                )
    except Exception as exc:
        app.logger.warning("_disparar_emails_novos_pedidos error: %s", exc)


def _email_status_pedido(pedido_id: str, novo_status: str):
    """Avisa o consumidor quando o status do pedido muda. Roda em thread daemon."""
    if not RESEND_API_KEY:
        return
    try:
        conn2 = _new_conn()
        cur2  = conn2.cursor()
        cur2.execute(
            """
            SELECT p.cliente_nome, p.cliente_email, p.total, p.tipo_entrega, u.razao
            FROM ecommerce_pedidos p
            JOIN users u ON u.cnpjloja = p.cnpjloja
            WHERE p.id = %s LIMIT 1
            """,
            (pedido_id,),
        )
        row = cur2.fetchone()
        cur2.close()
        conn2.close()
        if not row:
            return
        email = (row.get("cliente_email") or "").strip()
        if not email or "@" not in email:
            return
        nome     = (row.get("cliente_nome") or "Cliente").split()[0]
        razao    = row.get("razao") or "Farmácia"
        total_fmt = f"R$ {float(row.get('total', 0)):.2f}".replace(".", ",")
        label    = _STATUS_LABEL.get(novo_status, novo_status.title())
        tipo     = "Entrega" if (row.get("tipo_entrega") or "") == "entrega" else "Retirada na loja"

        corpo = (
            f"<p>Olá, <b>{html.escape(nome)}</b>!</p>"
            f"<p>Seu pedido <b>#{pedido_id[:8].upper()}</b> teve o status atualizado.</p>"
            f"<div class='info-box'>"
            f"<b>Farmácia:</b> {html.escape(razao)}<br>"
            f"<b>Novo status:</b> <span style='color:#c8102e;font-weight:bold'>{html.escape(label)}</span><br>"
            f"<b>Total:</b> {total_fmt}<br>"
            f"<b>Modalidade:</b> {html.escape(tipo)}"
            f"</div>"
            f"<p>Acesse <b>Meus Pedidos</b> para acompanhar todas as atualizações.</p>"
        )
        _send_email(
            email,
            f"📦 Pedido #{pedido_id[:8].upper()} — {label}",
            _email_html_wrapper(f"Status: {label}", corpo),
        )
    except Exception as exc:
        app.logger.warning("_email_status_pedido error: %s", exc)


# ─── RECUPERAÇÃO DE SENHA ─────────────────────────────────────────────────────

@app.get("/recuperar-senha")
def recuperar_senha():
    if session.get("consumidor_id"):
        return redirect(url_for("index"))
    return render_template("consumidor_recuperar_senha.html")


@app.post("/recuperar-senha")
def recuperar_senha_post():
    _ensure_consumidor_auth_columns()
    email = _norm_email(request.form.get("email"))
    if not _valid_email(email):
        flash("Informe um e-mail válido.", "error")
        return redirect(url_for("recuperar_senha"))

    conn = db(); cur = conn.cursor()
    cur.execute("SELECT id FROM ecommerce_consumidores WHERE email=%s LIMIT 1", (email,))
    user = cur.fetchone()

    if user:
        token = secrets.token_urlsafe(32)
        expira = datetime.now(timezone.utc) + timedelta(hours=2)
        cur.execute(
            "UPDATE ecommerce_consumidores SET reset_token=%s, reset_token_expira=%s WHERE id=%s",
            (token, expira, user["id"]),
        )
        conn.commit()
        base = _public_base_url() or request.host_url.rstrip("/")
        link = f"{base}/recuperar-senha/{token}"
        corpo = (
            f"<p>Você solicitou a redefinição de senha na Poupáqui.</p>"
            f"<p>Clique no botão abaixo para criar uma nova senha. O link expira em <b>2 horas</b>.</p>"
            f"<p><a class='btn' href='{link}'>Redefinir minha senha</a></p>"
            f"<p style='font-size:.82rem;color:#888'>Se você não solicitou isso, ignore este e-mail.</p>"
        )
        _send_email(email, "🔑 Redefinição de senha — Poupáqui", _email_html_wrapper("Redefina sua senha", corpo))
    cur.close()
    flash("Se este e-mail estiver cadastrado, você receberá as instruções em instantes.", "success")
    return redirect(url_for("recuperar_senha"))


@app.get("/recuperar-senha/<token>")
def recuperar_senha_token(token):
    _ensure_consumidor_auth_columns()
    conn = db(); cur = conn.cursor()
    cur.execute(
        "SELECT id FROM ecommerce_consumidores WHERE reset_token=%s AND reset_token_expira > NOW() LIMIT 1",
        (token,),
    )
    user = cur.fetchone()
    cur.close()
    if not user:
        flash("Link inválido ou expirado. Solicite novamente.", "error")
        return redirect(url_for("recuperar_senha"))
    return render_template("consumidor_nova_senha.html", token=token)


@app.post("/recuperar-senha/<token>")
def recuperar_senha_token_post(token):
    _ensure_consumidor_auth_columns()
    senha = request.form.get("senha") or ""
    confirma = request.form.get("confirma") or ""
    if senha != confirma:
        flash("As senhas não coincidem.", "error")
        return redirect(url_for("recuperar_senha_token", token=token))
    if len(senha) < 6 or senha.isdigit() or len(set(senha)) < 4:
        flash("Crie uma senha com pelo menos 6 caracteres e variedade.", "error")
        return redirect(url_for("recuperar_senha_token", token=token))

    conn = db(); cur = conn.cursor()
    cur.execute(
        """
        UPDATE ecommerce_consumidores
        SET senha_hash=%s, reset_token=NULL, reset_token_expira=NULL
        WHERE reset_token=%s AND reset_token_expira > NOW()
        RETURNING id
        """,
        (generate_password_hash(senha), token),
    )
    ok = cur.fetchone()
    conn.commit(); cur.close()
    if not ok:
        flash("Link inválido ou expirado. Solicite novamente.", "error")
        return redirect(url_for("recuperar_senha"))
    flash("Senha redefinida com sucesso! Faça login.", "success")
    return redirect(url_for("consumidor_login"))


# ─── VERIFICAÇÃO DE E-MAIL ────────────────────────────────────────────────────

@app.get("/verificar-email/<token>")
def verificar_email(token):
    _ensure_consumidor_auth_columns()
    conn = db(); cur = conn.cursor()
    cur.execute(
        "UPDATE ecommerce_consumidores SET email_verificado=TRUE, email_token=NULL WHERE email_token=%s RETURNING id",
        (token,),
    )
    ok = cur.fetchone()
    conn.commit(); cur.close()
    if ok:
        session["email_verificado"] = True
        flash("E-mail verificado com sucesso!", "success")
    else:
        flash("Link de verificação inválido ou já utilizado.", "error")
    return redirect(url_for("index"))


@app.post("/api/reenviar-verificacao")
def api_reenviar_verificacao():
    if not session.get("consumidor_id"):
        return jsonify({"error": "Não autenticado."}), 401
    _ensure_consumidor_auth_columns()
    conn = db(); cur = conn.cursor()
    cur.execute(
        "SELECT email, email_verificado, email_token_enviado_em FROM ecommerce_consumidores WHERE id=%s LIMIT 1",
        (session["consumidor_id"],),
    )
    user = cur.fetchone()
    if not user or user.get("email_verificado"):
        cur.close()
        return jsonify({"ok": True, "ja_verificado": True, "msg": "E-mail já verificado."})
    # Rate limit: 1 reenvio a cada 2 minutos
    ultimo = user.get("email_token_enviado_em")
    if ultimo:
        import datetime as _dt
        agora = _dt.datetime.now(_dt.timezone.utc)
        if ultimo.tzinfo is None:
            ultimo = ultimo.replace(tzinfo=_dt.timezone.utc)
        segundos = (agora - ultimo).total_seconds()
        if segundos < 120:
            cur.close()
            espera = int(120 - segundos)
            return jsonify({"ok": False, "rate_limited": True, "espera": espera,
                            "msg": f"Aguarde {espera}s antes de reenviar."}), 429
    token = secrets.token_urlsafe(32)
    cur.execute(
        "UPDATE ecommerce_consumidores SET email_token=%s, email_token_enviado_em=NOW() WHERE id=%s",
        (token, session["consumidor_id"]),
    )
    conn.commit(); cur.close()
    base = _public_base_url() or request.host_url.rstrip("/")
    link = f"{base}/verificar-email/{token}"
    corpo = (
        f"<p>Clique abaixo para confirmar seu e-mail na Poupáqui:</p>"
        f"<p><a class='btn' href='{link}'>Confirmar meu e-mail</a></p>"
        f"<p style='font-size:.82rem;color:#888'>Se não foi você, ignore este e-mail.</p>"
    )
    _send_email(user["email"], "✉️ Confirme seu e-mail — Poupáqui", _email_html_wrapper("Confirme seu e-mail", corpo))
    return jsonify({"ok": True, "msg": "E-mail de verificação reenviado."})


@app.get("/api/verificar-email-existe")
def api_verificar_email_existe():
    """Verifica em tempo real se o e-mail já tem cadastro — usado no formulário de criação de conta."""
    email = _norm_email(request.args.get("email"))
    if not _valid_email(email):
        return jsonify({"existe": False})
    _ensure_consumidor_schema()
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM ecommerce_consumidores WHERE email=%s LIMIT 1", (email,))
    existe = cur.fetchone() is not None
    cur.close()
    return jsonify({"existe": existe})


# ─── LGPD — POLÍTICA DE PRIVACIDADE ──────────────────────────────────────────

@app.get("/politica-de-privacidade")
def politica_privacidade():
    return render_template("politica_privacidade.html")


@app.get("/api/dbg-email")
def api_dbg_email():
    if request.args.get("t") != os.getenv("SECRET_KEY", ""):
        return jsonify({"error": "forbidden"}), 403
    to = request.args.get("to", "emano4775@gmail.com")
    payload = json.dumps({"from": RESEND_FROM, "to": [to], "subject": "DBG Test", "html": "<p>ok</p>"}).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload,
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json", "User-Agent": "PoupaquiApp/1.0"}, method="POST",
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            return jsonify({"ok": True, "status": resp.status, "from": RESEND_FROM, "key": RESEND_API_KEY[:12]})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return jsonify({"ok": False, "status": exc.code, "body": body, "from": RESEND_FROM, "key": RESEND_API_KEY[:12]}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "from": RESEND_FROM, "key": RESEND_API_KEY[:12]}), 200


if __name__ == "__main__":
    app.run(debug=True, port=5001)
