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
import hashlib
import threading
import urllib.request
import urllib.parse
import urllib.error
import html
from datetime import datetime, timezone, timedelta
from functools import wraps
import re
import ssl
import unicodedata
import base64

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from werkzeug.security import generate_password_hash, check_password_hash
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, send_from_directory, Response
)

try:
    import alpha_sync
except Exception:
    alpha_sync = None

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "poupaqui-ecommerce-dev-2026")
app.permanent_session_lifetime = timedelta(days=30)

_BRT = timezone(timedelta(hours=-3))

@app.template_filter('brt')
def _filter_brt(dt, fmt='%d/%m/%Y %H:%M'):
    if not dt:
        return '—'
    if hasattr(dt, 'tzinfo') and dt.tzinfo:
        dt = dt.astimezone(_BRT)
    else:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(_BRT)
    return dt.strftime(fmt)

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

# ─── GOOGLE OAUTH (CONSUMIDOR) ───────────────────────────────────────────────
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

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

def _cloudinary_sign(params_str: str) -> str:
    secret = os.getenv("CLOUDINARY_API_SECRET", "")
    return hashlib.sha1(f"{params_str}{secret}".encode()).hexdigest()


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
WASENDER_API_KEY = os.getenv("WASENDER_API_KEY", "")


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
    """Envolve conteúdo em layout HTML de e-mail simples no estilo Poupaqui."""
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
  <div class="header"><h1>Poupaqui</h1></div>
  <div class="body"><h2 style="margin-top:0;color:#c8102e">{titulo}</h2>{conteudo}</div>
  <div class="footer">Poupaqui — Sua farmácia de confiança · <a href="https://drogariaspoupaqui.com.br" style="color:#c8102e">drogariaspoupaqui.com.br</a></div>
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
        f"<p>Clique abaixo para confirmar seu e-mail na Poupaqui:</p>"
        f"<p><a class='btn' href='{link}'>Confirmar meu e-mail</a></p>"
        f"<p style='font-size:.82rem;color:#888'>Se não foi você, ignore este e-mail.</p>"
    )
    return _send_email(email, "✉️ Confirme seu e-mail — Poupaqui", _email_html_wrapper("Confirme seu e-mail", corpo))


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

def _alpha_enabled():
    try:
        return bool(alpha_sync and alpha_sync.alpha_enabled())
    except Exception:
        return False


def _catalogo_alpha_exclusivo():
    return _alpha_enabled()


def _ensure_alpha_schema():
    if not _alpha_enabled():
        return
    # Roda o DDL do schema Alpha uma única vez (por instância/migração). Antes
    # isso era executado a cada chamada — e _preco_catalogo_atual chama por item
    # do carrinho —, abrindo nova conexão e disparando ~15 ALTER TABLE com lock
    # exclusivo em ecommerce_pedidos. Com Alpha habilitado, isso serializava as
    # requisições de carrinho e estourava o timeout de 15s da Vercel (504).
    key = "alpha_local_schema_v1"
    if key in _schema_ready:
        return
    _load_db_migrations()
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        try:
            alpha_sync.ensure_local_schema()
            _schema_ready.add(key)
            _mark_migration_done(key)
        except Exception as exc:
            app.logger.warning("alpha ensure schema error: %s", exc)


def _alpha_sync_products_safe(limit=5000):
    if not _alpha_enabled():
        return {"ok": False, "erro": "alpha_nao_configurado"}
    try:
        result = alpha_sync.sync_products(limit=limit)
        _batch_cache_clear()
        return result
    except Exception as exc:
        app.logger.warning("alpha sync products error: %s", exc)
        return {"ok": False, "erro": str(exc)}


def _alpha_export_paid_order_safe(pedido_id):
    if not _alpha_enabled() or not pedido_id:
        return {"ok": False, "erro": "alpha_nao_configurado"}
    try:
        return alpha_sync.export_paid_order(str(pedido_id))
    except Exception as exc:
        app.logger.warning("alpha export order %s error: %s", pedido_id, exc)
        try:
            conn = db()
            cur = conn.cursor()
            cur.execute(
                "UPDATE ecommerce_pedidos SET alpha_status='erro', alpha_erro=%s, alpha_status_atualizado_em=NOW() WHERE id=%s",
                (str(exc)[:800], str(pedido_id)),
            )
            conn.commit()
            cur.close()
        except Exception:
            pass
        return {"ok": False, "erro": str(exc)}


def _finalizar_pos_pagamento_aprovado(pedido_id, notificar=True):
    # Exporta para o Alpha ANTES de avancar o status: _auto_pronto_retirada
    # move o pedido para 'pronto_retirada', e o export so deve depender de o
    # pagamento estar aprovado (a verificacao em export_paid_order ja aceita
    # ambos os casos, mas manter esta ordem evita qualquer corrida).
    try:
        _alpha_export_paid_order_safe(str(pedido_id))
    except Exception as exc:
        app.logger.warning("pos pagamento alpha %s error: %s", pedido_id, exc)
    try:
        _auto_pronto_retirada(str(pedido_id))
    except Exception as exc:
        app.logger.warning("pos pagamento pronto retirada %s error: %s", pedido_id, exc)
    if notificar:
        try:
            _notificar_pedido_evento(
                str(pedido_id),
                "pagamento",
                "Pagamento aprovado",
                f"O pagamento do pedido #{str(pedido_id)[:8].upper()} foi confirmado.",
            )
        except Exception as exc:
            app.logger.warning("pos pagamento notificar %s error: %s", pedido_id, exc)
        try:
            _pid = str(pedido_id)
            _ok_wa = _wa_notif_pedido_loja(
                _pid,
                f'✅ Pedido pago!\n'
                f'Pedido #{_pid[:8].upper()} foi confirmado.\n'
                f'Clique para preparar:\n'
                f'{_wa_base_url()}/painel/pedidos/{_pid}'
            )
            if _ok_wa:
                try:
                    _wc = db(); _wcu = _wc.cursor()
                    _wcu.execute("UPDATE ecommerce_pedidos SET wa_loja_notificado_em=NOW() WHERE id=%s", (_pid,))
                    _wc.commit(); _wcu.close()
                except Exception:
                    pass
        except Exception:
            pass


def _finalizar_pos_pagamento_aprovado_async(pedido_id, notificar=True):
    # Em ambiente serverless (Vercel) a instância é congelada assim que a
    # resposta HTTP é enviada, então uma daemon thread iniciada aqui não chega
    # a executar — e o pedido pago nunca é exportado para o Alpha. Nesse caso
    # roda de forma síncrona antes de retornar (cabe no maxDuration de 60s e
    # cada etapa já é protegida por try/except). Localmente mantém o
    # comportamento assíncrono para não travar a requisição.
    if os.environ.get("VERCEL"):
        _finalizar_pos_pagamento_aprovado(str(pedido_id), notificar)
        return
    try:
        threading.Thread(
            target=_finalizar_pos_pagamento_aprovado,
            args=(str(pedido_id), notificar),
            daemon=True,
        ).start()
    except Exception:
        pass


def _alpha_sync_statuses_safe():
    if not _alpha_enabled():
        return {"ok": False, "erro": "alpha_nao_configurado"}
    try:
        return alpha_sync.sync_order_statuses()
    except Exception as exc:
        app.logger.warning("alpha sync statuses error: %s", exc)
        return {"ok": False, "erro": str(exc)}


_alpha_catalog_sync_lock = threading.Lock()
_alpha_catalog_last_attempt = 0.0


def _alpha_catalog_has_products(cur, cnpjloja=None):
    try:
        _ensure_alpha_schema()
        if cnpjloja:
            cur.execute(
                """
                SELECT 1
                FROM ecommerce_alpha_produtos
                WHERE cnpjloja=%s
                  AND COALESCE(inativo,false)=false
                  AND COALESCE(estoque,0)>0
                LIMIT 1
                """,
                (cnpjloja,),
            )
        else:
            cur.execute(
                """
                SELECT 1
                FROM ecommerce_alpha_produtos
                WHERE COALESCE(inativo,false)=false
                  AND COALESCE(estoque,0)>0
                LIMIT 1
                """
            )
        return cur.fetchone() is not None
    except Exception as exc:
        app.logger.warning("alpha catalog has products check error: %s", exc)
        return False


def _alpha_catalog_sync_if_needed(cur=None, cnpjloja=None, force=False):
    if not _catalogo_alpha_exclusivo():
        return {"ok": False, "erro": "alpha_nao_configurado"}
    own_conn = None
    try:
        if cur is None:
            own_conn = db()
            cur = own_conn.cursor()
        has_products = _alpha_catalog_has_products(cur, cnpjloja=cnpjloja)
        now_ts = time.time()
        global _alpha_catalog_last_attempt
        if not force and now_ts - _alpha_catalog_last_attempt < 300:
            return {"ok": True, "skipped": "tentativa_recente", "has_products": has_products}
        if not _alpha_catalog_sync_lock.acquire(blocking=False):
            return {"ok": False, "skipped": "sync_em_andamento"}
        try:
            _alpha_catalog_last_attempt = now_ts
            # PG advisory lock serializes o sync entre instâncias Vercel concorrentes.
            # Sem isso, múltiplos cold-starts disparam ensure_local_schema() ao mesmo
            # tempo → ALTER TABLE ecommerce_pedidos concorrente → deadlock → 504.
            _lk_conn = db()
            _lk_cur = _lk_conn.cursor()
            _lk_cur.execute("SELECT pg_try_advisory_lock(20260707)")
            _lk_row = _lk_cur.fetchone()
            _has_pg_lock = bool((_lk_row or {}).get("pg_try_advisory_lock", False))
            _lk_cur.close()
            if not _has_pg_lock:
                return {"ok": True, "skipped": "sync_serializado_outro_processo"}
            try:
                result = _alpha_sync_products_safe(limit=5000)
                app.logger.warning("alpha catalog autosync result: %s", result)
                return result
            finally:
                try:
                    _ul_cur = _lk_conn.cursor()
                    _ul_cur.execute("SELECT pg_advisory_unlock(20260707)")
                    _ul_cur.fetchone()
                    _ul_cur.close()
                except Exception:
                    pass
        finally:
            _alpha_catalog_sync_lock.release()
    finally:
        if own_conn is not None:
            try:
                cur.close()
            except Exception:
                pass


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
    consumidor_notif_nao_lidas = 0
    consumidor_encomendas_abertas = 0
    consumidor_tem_assinatura = False
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
            try:
                _ensure_notificacoes_schema()
                cur.execute(
                    "SELECT COUNT(*) AS n FROM ecommerce_notificacoes_consumidor WHERE consumidor_id=%s AND lida_em IS NULL",
                    (session["consumidor_id"],),
                )
                consumidor_notif_nao_lidas = (cur.fetchone() or {}).get("n", 0) or 0
            except Exception:
                consumidor_notif_nao_lidas = 0
            try:
                _ensure_encomenda_schema()
                cur.execute(
                    "SELECT COUNT(*) AS n FROM ecommerce_encomendas WHERE consumidor_id=%s AND status NOT IN ('finalizada')",
                    (session["consumidor_id"],),
                )
                consumidor_encomendas_abertas = (cur.fetchone() or {}).get("n", 0) or 0
            except Exception:
                consumidor_encomendas_abertas = 0
            try:
                _ensure_assinatura_schema()
                cur.execute(
                    """SELECT 1 FROM ecommerce_assinantes
                       WHERE consumidor_id=%s AND status='ativo' AND pagamento_status='aprovado'
                         AND (data_fim IS NULL OR data_fim > NOW()) LIMIT 1""",
                    (session["consumidor_id"],),
                )
                consumidor_tem_assinatura = cur.fetchone() is not None
            except Exception:
                consumidor_tem_assinatura = False
            cur.close()
        except Exception:
            consumidor_rec_abertas = 0
            consumidor_notif_nao_lidas = 0
            consumidor_encomendas_abertas = 0
    return {
        "money": fmt_brl,
        "now": datetime.now(timezone.utc),
        "consumidor": consumidor,
        "consumidor_rec_abertas": consumidor_rec_abertas,
        "consumidor_notif_nao_lidas": consumidor_notif_nao_lidas,
        "consumidor_encomendas_abertas": consumidor_encomendas_abertas,
        "consumidor_tem_assinatura": consumidor_tem_assinatura,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_ANON": SUPABASE_ANON,
        "GOOGLE_MAPS_KEY": os.getenv("GOOGLE_MAPS_KEY", ""),
    }

def _consumidor_from_session():
    cid = session.get("consumidor_id")
    if not cid:
        return None
    return {
        "id": cid,
        "nome": session.get("consumidor_nome"),
        "email": session.get("consumidor_email"),
        "telefone": session.get("consumidor_telefone"),
        "endereco": session.get("consumidor_endereco"),
        "lat": session.get("consumidor_lat"),
        "lng": session.get("consumidor_lng"),
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
        cur.execute("ALTER TABLE ecommerce_consumidores ADD COLUMN IF NOT EXISTS documento TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS cliente_documento TEXT")
        conn.commit()
        cur.close()
        _schema_ready.add("consumidor")
        _mark_migration_done("consumidor")
    _ensure_consumidor_auth_columns()


def _ensure_consumidor_profile_columns():
    key = "consumidor_profile_v2"
    _load_db_migrations()
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("ALTER TABLE ecommerce_consumidores ADD COLUMN IF NOT EXISTS documento TEXT")
        cur.execute("ALTER TABLE ecommerce_consumidores ADD COLUMN IF NOT EXISTS endereco TEXT")
        cur.execute("ALTER TABLE ecommerce_consumidores ADD COLUMN IF NOT EXISTS endereco_lat DOUBLE PRECISION")
        cur.execute("ALTER TABLE ecommerce_consumidores ADD COLUMN IF NOT EXISTS endereco_lng DOUBLE PRECISION")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS cliente_documento TEXT")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_notificacoes_schema():
    key = "consumidor_notificacoes_v2"
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_notificacoes_consumidor (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                consumidor_id UUID NOT NULL,
                tipo TEXT NOT NULL DEFAULT 'sistema',
                titulo TEXT NOT NULL,
                mensagem TEXT,
                imagem_url TEXT,
                url TEXT,
                pedido_id UUID,
                lida_em TIMESTAMPTZ,
                criada_em TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE ecommerce_notificacoes_consumidor ADD COLUMN IF NOT EXISTS imagem_url TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_notif_cons_criada ON ecommerce_notificacoes_consumidor(consumidor_id, criada_em DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_notif_cons_lida ON ecommerce_notificacoes_consumidor(consumidor_id, lida_em)")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_aviso_chegada_schema():
    key = "aviso_chegada_v1"
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_avisos_chegada (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                consumidor_id UUID NOT NULL,
                cidade TEXT NOT NULL,
                uf TEXT,
                lat DOUBLE PRECISION,
                lng DOUBLE PRECISION,
                criado_em TIMESTAMPTZ DEFAULT NOW(),
                notificado_em TIMESTAMPTZ,
                UNIQUE (consumidor_id, cidade, uf)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_aviso_chegada_pendente ON ecommerce_avisos_chegada(cidade, uf) WHERE notificado_em IS NULL")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _notificar_consumidor(consumidor_id, tipo, titulo, mensagem="", url=None, pedido_id=None, conn=None, imagem_url=None):
    if not consumidor_id or not titulo:
        return
    try:
        _ensure_notificacoes_schema()
        own = conn is None
        conn = conn or db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ecommerce_notificacoes_consumidor
              (consumidor_id, tipo, titulo, mensagem, imagem_url, url, pedido_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (consumidor_id, tipo or "sistema", titulo[:160], (mensagem or "")[:600], imagem_url, url, pedido_id),
        )
        if own:
            conn.commit()
        cur.close()
    except Exception as exc:
        try:
            app.logger.warning("_notificar_consumidor error: %s", exc)
        except Exception:
            pass


def _notificar_pedido_evento(pedido_id, tipo, titulo, mensagem=""):
    try:
        _ensure_notificacoes_schema()
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT consumidor_id FROM ecommerce_pedidos WHERE id=%s LIMIT 1", (pedido_id,))
        row = cur.fetchone()
        if row and row.get("consumidor_id"):
            try:
                url = url_for("meu_pedido_detalhe", pedido_id=pedido_id)
            except RuntimeError:
                url = f"/meus-pedidos/{pedido_id}"
            _notificar_consumidor(
                row["consumidor_id"], tipo, titulo, mensagem,
                url=url,
                pedido_id=pedido_id, conn=conn,
            )
            conn.commit()
        cur.close()
    except Exception:
        pass


def _notificar_todos_consumidores(titulo, mensagem="", url=None, tipo="sistema", limite=5000, imagem_url=None):
    if not titulo:
        return 0
    try:
        _ensure_notificacoes_schema()
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM ecommerce_consumidores ORDER BY criado_em DESC LIMIT %s", (int(limite),))
        consumidores = [r["id"] for r in cur.fetchall()]
        if not consumidores:
            cur.close()
            return 0
        rows = [(cid, tipo or "sistema", titulo[:160], (mensagem or "")[:600], imagem_url, url) for cid in consumidores]
        execute_values(
            cur,
            """
            INSERT INTO ecommerce_notificacoes_consumidor
              (consumidor_id, tipo, titulo, mensagem, imagem_url, url)
            VALUES %s
            """,
            rows,
        )
        conn.commit()
        cur.close()
        return len(rows)
    except Exception as exc:
        try:
            app.logger.warning("_notificar_todos_consumidores error: %s", exc)
        except Exception:
            pass
    return 0


# ─── WA SENDER (notificações WhatsApp para a loja) ───────────────────────────

def _wa_send(numero: str, msg: str) -> bool:
    """Envia mensagem WhatsApp via WA Sender API. Normaliza número para +55DD9XXXXXXXX."""
    if not WASENDER_API_KEY or not numero:
        app.logger.warning("wa_send: WASENDER_API_KEY vazia ou numero vazio (key=%r, num=%r)", bool(WASENDER_API_KEY), numero)
        return False
    d = re.sub(r'\D', '', numero)
    if not d or len(d) < 8:
        app.logger.warning("wa_send: numero invalido apos normalizar: %r", numero)
        return False
    to = ('+55' + d) if not d.startswith('55') else ('+' + d)
    try:
        req = urllib.request.Request(
            'https://wasenderapi.com/api/send-message',
            data=json.dumps({'to': to, 'text': msg}).encode(),
            headers={
                'Authorization': f'Bearer {WASENDER_API_KEY}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            app.logger.warning("wa_send ok: to=%s status=%s", to, resp.status)
            return True
    except urllib.error.HTTPError as e:
        body = e.read(300).decode(errors='replace')
        app.logger.warning("wa_send HTTPError %s to=%s: %s", e.code, to, body)
        return False
    except Exception as exc:
        app.logger.warning("wa_send error to=%s: %s", to, exc)
        return False


def _wa_notif_pedido_loja(pedido_id: str, msg: str):
    """Busca whatsapp_pedidos da loja do pedido e envia notificação WA."""
    if not WASENDER_API_KEY:
        return False
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT c.whatsapp_pedidos AS wpp
            FROM ecommerce_pedidos p
            JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
            WHERE p.id = %s LIMIT 1
            """,
            (pedido_id,),
        )
        row = cur.fetchone()
        cur.close()
        wpp = (row or {}).get('wpp') or ""
        if wpp:
            return _wa_send(wpp, msg)
        app.logger.warning("wa_notif_pedido_loja: sem numero para pedido %s", pedido_id)
        return False
    except Exception as e:
        app.logger.warning("wa_notif_pedido_loja error pedido=%s: %s", pedido_id, e)
        return False


def _wa_base_url():
    return os.getenv('PUBLIC_BASE_URL', 'https://ecommerce-2-rosy.vercel.app').rstrip('/')


# ─── FIM WA SENDER ───────────────────────────────────────────────────────────


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


def _ensure_horario_schema():
    if "horario" not in _schema_ready:
        with _schema_lock:
            if "horario" not in _schema_ready:
                conn = db()
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ecommerce_config_horario (
                        cnpjloja      TEXT     NOT NULL,
                        dia_semana    SMALLINT NOT NULL,
                        hora_abertura TIME,
                        hora_fechamento TIME,
                        fechado       BOOLEAN  DEFAULT FALSE,
                        PRIMARY KEY (cnpjloja, dia_semana)
                    )
                """)
                # Horario de entrega — pode ser um subconjunto do horario de
                # funcionamento (loja aberta pra retirada nao implica entrega
                # disponivel no mesmo horario). NULL nos horarios de entrega
                # = usa o mesmo horario da loja nesse dia.
                cur.execute("ALTER TABLE ecommerce_config_horario ADD COLUMN IF NOT EXISTS entrega_habilitada BOOLEAN DEFAULT TRUE")
                cur.execute("ALTER TABLE ecommerce_config_horario ADD COLUMN IF NOT EXISTS entrega_hora_abertura TIME")
                cur.execute("ALTER TABLE ecommerce_config_horario ADD COLUMN IF NOT EXISTS entrega_hora_fechamento TIME")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ecommerce_config_feriado (
                        id              SERIAL PRIMARY KEY,
                        cnpjloja        TEXT NOT NULL,
                        data            DATE NOT NULL,
                        descricao       TEXT,
                        fechado         BOOLEAN DEFAULT TRUE,
                        hora_abertura   TIME,
                        hora_fechamento TIME,
                        criado_em       TIMESTAMPTZ DEFAULT NOW(),
                        UNIQUE(cnpjloja, data)
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_config_feriado_cnpj_data ON ecommerce_config_feriado(cnpjloja, data)")
                conn.commit()
                cur.close()
                _schema_ready.add("horario")


def _ensure_delivery_schema():
    _ensure_consumidor_schema()
    if "delivery" not in _schema_ready:
        with _schema_lock:
            if "delivery" not in _schema_ready:
                conn = db()
                cur = conn.cursor()
                cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS aceita_entrega BOOLEAN DEFAULT FALSE")
                cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS raio_entrega_km NUMERIC DEFAULT 0")
                cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS cobra_frete BOOLEAN DEFAULT FALSE")
                cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS valor_frete NUMERIC DEFAULT 0")
                cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS pedido_minimo_entrega NUMERIC DEFAULT 0")
                cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS todos_prontos_retirada BOOLEAN DEFAULT FALSE")
                cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS mp_public_key TEXT")
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
    _ensure_loja_email_column()          # sempre chamado, independente do delivery já estar marcado
    _ensure_codigo_retirada_column()     # idem
    _ensure_previsao_entrega_column()    # idem
    _ensure_entrega_agendada_column()    # idem
    _ensure_agendamento_entrega_column() # idem
    _ensure_previsao_entrega_em_column() # idem
    _ensure_pedidos_status_historico_schema() # idem


def _ensure_codigo_retirada_column():
    """Migração separada para codigo_retirada em ecommerce_pedidos."""
    key = "codigo_retirada_v1"
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
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS codigo_retirada TEXT")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_previsao_entrega_column():
    key = "previsao_entrega_v1"
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
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS previsao_entrega TEXT")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_entrega_agendada_column():
    """Data agendada de entrega — quando o cliente pede entrega num dia em que
    ela nao esta disponivel agora (feriado/fora do horario de entrega), mas
    esta disponivel num proximo dia, ele pode agendar pra essa data."""
    key = "entrega_agendada_v1"
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
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS data_entrega_agendada DATE")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_agendamento_entrega_column():
    """Opt-in da loja pra oferecer 'agendar entrega pra outro dia' quando a
    entrega nao esta disponivel hoje (feriado/fora do horario de entrega)."""
    key = "agendamento_entrega_v1"
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
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS permite_agendamento_entrega BOOLEAN DEFAULT FALSE")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_previsao_entrega_em_column():
    """Versao com data/hora real da previsao (previsao_entrega e so texto
    livre, nao da pra comparar com agora() pra saber se atrasou)."""
    key = "previsao_entrega_em_v1"
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
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS previsao_entrega_em TIMESTAMPTZ")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_pedidos_status_historico_schema():
    """Registra o horario exato de cada mudanca de status do pedido, pra
    timeline do consumidor mostrar hora real de cada etapa (nao so o status
    atual). Pedidos antigos (antes desta migracao) nao tem historico
    completo — a timeline cai pra uma versao aproximada usando os
    timestamps legados (criado_em/pagamento_confirmado_em/entregue_em)."""
    key = "pedidos_status_historico_v1"
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_pedidos_status_historico (
                id        SERIAL PRIMARY KEY,
                pedido_id UUID NOT NULL,
                status    TEXT NOT NULL,
                criado_em TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_status_hist_pedido ON ecommerce_pedidos_status_historico(pedido_id)")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _registrar_status_pedido(pedido_id, status):
    """Grava no historico a mudanca de status — best-effort, nunca deve
    quebrar o fluxo principal do pedido se falhar."""
    try:
        _ensure_pedidos_status_historico_schema()
        conn = db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO ecommerce_pedidos_status_historico (pedido_id, status) VALUES (%s, %s)",
            (pedido_id, status),
        )
        conn.commit()
        cur.close()
    except Exception:
        pass


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


def _ensure_mp_public_key_column():
    key = "loja_mp_public_key_v1"
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
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS mp_public_key TEXT")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_logo_url_column():
    key = "loja_logo_url_v1"
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
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS logo_url TEXT")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_gateway_alt_columns():
    key = "gateway_alt_v2"
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
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS gateway_alternativo TEXT DEFAULT 'mercadopago'")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS asaas_api_key TEXT")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS pagbank_token TEXT")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS pagbank_public_key TEXT")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS asaas_taxa_pct NUMERIC DEFAULT 0")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS asaas_taxa_fixa NUMERIC DEFAULT 0")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS pagbank_taxa_pct NUMERIC DEFAULT 0")
        cur.execute("ALTER TABLE ecommerce_config_loja ADD COLUMN IF NOT EXISTS pagbank_taxa_fixa NUMERIC DEFAULT 0")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_asaas_clientes (
                id SERIAL PRIMARY KEY,
                consumidor_id TEXT NOT NULL,
                cnpjloja TEXT NOT NULL,
                asaas_customer_id TEXT NOT NULL,
                criado_em TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(consumidor_id, cnpjloja)
            )
        """)
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_cartoes_schema():
    key = "cartoes_salvos_v1"
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_mp_clientes (
                id SERIAL PRIMARY KEY,
                consumidor_id TEXT NOT NULL,
                cnpjloja TEXT NOT NULL,
                mp_customer_id TEXT NOT NULL,
                criado_em TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(consumidor_id, cnpjloja)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_cartoes_salvos (
                id SERIAL PRIMARY KEY,
                consumidor_id TEXT NOT NULL,
                cnpjloja TEXT NOT NULL,
                mp_card_id TEXT NOT NULL,
                ultimos_quatro TEXT,
                mes_vencimento INT,
                ano_vencimento INT,
                payment_method_id TEXT,
                criado_em TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(consumidor_id, cnpjloja, mp_card_id)
            )
        """)
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_banner_schema():
    if "banners" in _schema_ready:
        return
    with _schema_lock:
        if "banners" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_banners (
                id         SERIAL PRIMARY KEY,
                cnpjloja   TEXT NOT NULL,
                imagem_url TEXT NOT NULL,
                link_url   TEXT,
                titulo     TEXT,
                ativo      BOOLEAN DEFAULT TRUE,
                ordem      INTEGER DEFAULT 0,
                criado_em  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ecommerce_banners_cnpj ON ecommerce_banners(cnpjloja)")
        conn.commit()
        cur.close()
        _schema_ready.add("banners")
        _mark_migration_done("banners")


# Cache simples para a API de banners (evita query a cada requisição)
_banner_cache: dict = {}
_banner_cache_lock = threading.Lock()
_BANNER_CACHE_TTL = 300  # 5 minutos


def _banner_cache_get(key: str):
    with _banner_cache_lock:
        e = _banner_cache.get(key)
        if e and time.time() - e["ts"] < _BANNER_CACHE_TTL:
            return e["data"]
    return None


def _banner_cache_set(key: str, data):
    with _banner_cache_lock:
        _banner_cache[key] = {"ts": time.time(), "data": data}


_home_api_cache: dict = {}
_home_api_cache_lock = threading.Lock()


def _home_api_cache_get(key: tuple, ttl_seconds: int):
    now = time.time()
    with _home_api_cache_lock:
        e = _home_api_cache.get(key)
        if not e:
            return None
        if now - float(e.get("ts") or 0) > ttl_seconds:
            _home_api_cache.pop(key, None)
            return None
        return e.get("data")


def _home_api_cache_set(key: tuple, data, ttl_seconds: int = 180):
    now = time.time()
    with _home_api_cache_lock:
        if len(_home_api_cache) >= 200:
            old = [k for k, v in _home_api_cache.items() if now - float(v.get("ts") or 0) > ttl_seconds]
            for k in old[:80]:
                _home_api_cache.pop(k, None)
            while len(_home_api_cache) >= 180:
                oldest = min(_home_api_cache, key=lambda k: _home_api_cache[k]["ts"])
                _home_api_cache.pop(oldest, None)
        _home_api_cache[key] = {"ts": now, "data": data}

def _ensure_promo_schema():
    if "promocoes" in _schema_ready:
        return
    with _schema_lock:
        if "promocoes" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_promocoes (
                id            SERIAL PRIMARY KEY,
                cnpjloja      TEXT NOT NULL,
                ean           TEXT NOT NULL,
                nome          TEXT,
                preco_promo   NUMERIC(10,2) NOT NULL,
                data_inicio   TIMESTAMPTZ DEFAULT NOW(),
                data_fim      TIMESTAMPTZ,
                so_assinantes BOOLEAN DEFAULT FALSE,
                ativo         BOOLEAN DEFAULT TRUE,
                criado_em     TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ecommerce_promocoes_cnpj ON ecommerce_promocoes(cnpjloja)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ecommerce_promocoes_ean  ON ecommerce_promocoes(ean)")
        conn.commit()
        cur.close()
        _schema_ready.add("promocoes")
        _mark_migration_done("promocoes")


def _ensure_assinatura_schema():
    if "assinaturas" not in _schema_ready:
        _ensure_assinatura_schema_impl()
    _ensure_repasses_admin_schema()  # sempre chamado, independente de "assinaturas" já estar marcado


def _ensure_assinatura_schema_impl():
    with _schema_lock:
        if "assinaturas" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_planos_assinatura (
                id            SERIAL PRIMARY KEY,
                cnpjloja      TEXT NOT NULL UNIQUE,
                nome          TEXT NOT NULL DEFAULT 'Clube Fidelidade',
                descricao     TEXT,
                preco_mensal  NUMERIC(10,2) NOT NULL DEFAULT 19.90,
                beneficios    TEXT,
                ativo         BOOLEAN DEFAULT FALSE,
                criado_em     TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_assinantes (
                id                  SERIAL PRIMARY KEY,
                consumidor_id       TEXT NOT NULL,
                cnpjloja            TEXT NOT NULL,
                plano_id            INTEGER REFERENCES ecommerce_planos_assinatura(id),
                status              TEXT DEFAULT 'aguardando_pagamento',
                mp_payment_id       TEXT,
                mp_preference_id    TEXT,
                pagamento_status    TEXT DEFAULT 'pendente',
                data_inicio         TIMESTAMPTZ,
                data_fim            TIMESTAMPTZ,
                criado_em           TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(consumidor_id, cnpjloja)
            )
        """)
        # Fix: se tabela já existia com INTEGER, migra para TEXT
        try:
            cur.execute(
                "ALTER TABLE ecommerce_assinantes ALTER COLUMN consumidor_id TYPE TEXT USING consumidor_id::TEXT"
            )
            conn.commit()
        except Exception:
            conn.rollback()
        # Adiciona colunas de pagamento se não existirem
        for col_sql in [
            "ALTER TABLE ecommerce_assinantes ADD COLUMN IF NOT EXISTS mp_payment_id TEXT",
            "ALTER TABLE ecommerce_assinantes ADD COLUMN IF NOT EXISTS mp_preference_id TEXT",
            "ALTER TABLE ecommerce_assinantes ADD COLUMN IF NOT EXISTS mp_init_point TEXT",
            "ALTER TABLE ecommerce_assinantes ADD COLUMN IF NOT EXISTS mp_preapproval_id TEXT",
            "ALTER TABLE ecommerce_assinantes ADD COLUMN IF NOT EXISTS mp_preapproval_init_point TEXT",
            "ALTER TABLE ecommerce_assinantes ADD COLUMN IF NOT EXISTS assinatura_recorrente BOOLEAN DEFAULT FALSE",
            "ALTER TABLE ecommerce_assinantes ADD COLUMN IF NOT EXISTS pagamento_status TEXT DEFAULT 'pendente'",
        ]:
            try:
                cur.execute(col_sql)
            except Exception:
                conn.rollback()
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ecommerce_assinantes_cons ON ecommerce_assinantes(consumidor_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ecommerce_assinantes_cnpj ON ecommerce_assinantes(cnpjloja)")
        conn.commit()
        cur.close()
        _schema_ready.add("assinaturas")
        _mark_migration_done("assinaturas")


def _ensure_repasses_admin_schema():
    """Beneficios financiados pelo admin (frete gratis da 1a entrega do
    assinante, cupons administrados pelo admin) — registra quanto o admin
    "deve" repassar pra loja, pra ela sempre receber o valor que configurou
    mesmo quando o cliente pagou menos por causa de um beneficio do admin."""
    key = "repasses_admin_v1"
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
        cur.execute("ALTER TABLE ecommerce_planos_assinatura ADD COLUMN IF NOT EXISTS frete_gratis_primeira_entrega BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE ecommerce_assinantes ADD COLUMN IF NOT EXISTS frete_gratis_primeira_usado BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS frete_gratis_assinante BOOLEAN DEFAULT FALSE")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_repasses_admin (
                id          SERIAL PRIMARY KEY,
                cnpjloja    TEXT NOT NULL,
                pedido_id   UUID,
                tipo        TEXT NOT NULL,
                valor       NUMERIC(10,2) NOT NULL,
                descricao   TEXT,
                criado_em   TIMESTAMPTZ DEFAULT NOW(),
                pago        BOOLEAN DEFAULT FALSE,
                pago_em     TIMESTAMPTZ
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_repasses_admin_cnpj ON ecommerce_repasses_admin(cnpjloja)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_repasses_admin_pago ON ecommerce_repasses_admin(pago)")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_config_admin_schema():
    """Config global do admin — usada para cobrar TODAS as assinaturas de TODAS
    as lojas na mesma conta Mercado Pago (dinheiro de assinatura nao vai mais
    pra conta MP de cada loja individual)."""
    if "config_admin" in _schema_ready:
        return
    with _schema_lock:
        if "config_admin" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_config_admin (
                id              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                mp_access_token TEXT,
                mp_public_key   TEXT,
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        _schema_ready.add("config_admin")
        _mark_migration_done("config_admin")


def _admin_mp_config():
    """Retorna {mp_access_token, mp_public_key} da conta MP central do admin,
    usada para cobrar assinaturas (independente da loja assinada)."""
    _ensure_config_admin_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT mp_access_token, mp_public_key FROM ecommerce_config_admin WHERE id=1")
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else {}


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


_STATUS_ENCOMENDA_LABEL = {
    "aberta":       "Aguardando resposta da farmácia",
    "em_andamento": "Em andamento",
    "disponivel":   "Produto disponível!",
    "finalizada":   "Finalizada",
}


def _ensure_encomenda_schema():
    _ensure_reclamacao_schema()
    if "encomenda_v1" in _schema_ready:
        return
    _load_db_migrations()
    if "encomenda_v1" in _schema_ready:
        return
    with _schema_lock:
        if "encomenda_v1" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_encomendas (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                cnpjloja TEXT NOT NULL,
                consumidor_id UUID NOT NULL,
                produto_nome TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'aberta',
                criado_em TIMESTAMPTZ DEFAULT NOW(),
                atualizado_em TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_encomenda_msgs (
                id SERIAL PRIMARY KEY,
                encomenda_id UUID NOT NULL,
                autor TEXT NOT NULL,
                mensagem TEXT NOT NULL,
                enviada_em TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        _schema_ready.add("encomenda_v1")
        _mark_migration_done("encomenda_v1")


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
                cnpjloja TEXT,
                status TEXT DEFAULT 'active',
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE ml_items ADD COLUMN IF NOT EXISTS category_id TEXT")
        cur.execute("ALTER TABLE ml_items ADD COLUMN IF NOT EXISTS cnpjloja TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS origem TEXT DEFAULT 'ecommerce'")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS ml_order_id TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS cliente_documento TEXT")
        conn.commit(); cur.close()
        _schema_ready.add("ml")
        _mark_migration_done("ml")


def _ensure_ml_accounts_schema():
    key = "ml_accounts_v1"
    _ensure_ml_schema()
    _load_db_migrations()
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        conn = db(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ml_tokens_loja (
                cnpjloja TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                ml_user_id TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ml_tokens_loja_user ON ml_tokens_loja (ml_user_id)")
        cur.execute("ALTER TABLE ml_items ADD COLUMN IF NOT EXISTS cnpjloja TEXT")
        conn.commit(); cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_ml_shipping_schema():
    key = "ml_shipping_v1"
    _ensure_ml_accounts_schema()
    _load_db_migrations()
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        conn = db(); cur = conn.cursor()
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS ml_shipping_id TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS ml_shipping_status TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS ml_shipping_substatus TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS ml_shipping_mode TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS ml_logistic_type TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS ml_tracking_number TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS ml_tracking_method TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS ml_shipping_updated_at TIMESTAMPTZ")
        conn.commit(); cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _norm_email(email):
    return (email or "").strip().lower()


def _digits(s):
    return re.sub(r"\D+", "", s or "")


def _placeholder_for_tarja(tarja: str | None):
    if tarja == "preta":
        return GENERIC_TARJA_PRETA_IMG
    if tarja == "vermelha":
        return GENERIC_TARJA_VERMELHA_IMG
    return None


def _valid_nome(nome):
    nome = (nome or "").strip()
    parts = [p for p in nome.split() if len(p) >= 2]
    return len(nome) >= 6 and len(parts) >= 2 and bool(re.fullmatch(r"[A-Za-zÀ-ÿ' ]+", nome))


def _valid_email(email):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""))


def _valid_phone(phone):
    d = _digits(phone)
    return len(d) in (10, 11) and len(set(d)) > 2


def _valid_cpf(cpf):
    d = _digits(cpf)
    if len(d) != 11 or len(set(d)) == 1:
        return False
    nums = [int(x) for x in d]
    s1 = sum(nums[i] * (10 - i) for i in range(9))
    v1 = (s1 * 10) % 11
    if v1 == 10:
        v1 = 0
    s2 = sum(nums[i] * (11 - i) for i in range(10))
    v2 = (s2 * 10) % 11
    if v2 == 10:
        v2 = 0
    return nums[9] == v1 and nums[10] == v2


def _valid_cnpj(cnpj):
    d = _digits(cnpj)
    if len(d) != 14 or len(set(d)) == 1:
        return False
    nums = [int(x) for x in d]
    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos2 = [6] + pesos1
    s1 = sum(nums[i] * pesos1[i] for i in range(12))
    v1 = 0 if s1 % 11 < 2 else 11 - (s1 % 11)
    s2 = sum(nums[i] * pesos2[i] for i in range(13))
    v2 = 0 if s2 % 11 < 2 else 11 - (s2 % 11)
    return nums[12] == v1 and nums[13] == v2


def _valid_documento(documento):
    d = _digits(documento)
    return _valid_cpf(d) if len(d) == 11 else _valid_cnpj(d) if len(d) == 14 else False


def _google_oauth_ready():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _google_redirect_uri():
    if GOOGLE_REDIRECT_URI:
        return GOOGLE_REDIRECT_URI
    return url_for("consumidor_google_callback", _external=True)


def _google_post_json(url, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _google_get_json(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    text = _norm_text(f"{endereco or ''} {endereco2 or ''}")
    uf = (uf or "").upper()
    if uf == "SP" and "são pedro" in text:
        return -22.5483, -47.9139
    if "djair jose marques" in text and ("mirassol" in text or "15133" in text or "regissol" in text):
        return -20.8029, -49.5202
    return None, None


def _endereco_geo_hash(endereco, uf=""):
    base = _norm_text(f"{endereco or ''} {uf or ''}")
    return hashlib.sha1(base.encode("utf-8")).hexdigest() if base else None


def _ensure_lojas_geo_address_hash():
    key = "lojas_geo_endereco_hash_v1"
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
        cur.execute("ALTER TABLE ecommerce_lojas_geo ADD COLUMN IF NOT EXISTS endereco_hash TEXT")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


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
    _ensure_lojas_geo_address_hash()
    endereco_hash = _endereco_geo_hash(endereco, uf)
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT lat, lng, endereco_hash FROM ecommerce_lojas_geo WHERE cnpjloja = %s", (cnpjloja,)
    )
    row = cur.fetchone()
    cur.close()
    if row and row["lat"] and row.get("endereco_hash") == endereco_hash:
        return float(row["lat"]), float(row["lng"])
    lat, lng = _geo_override(endereco, None, uf)
    if lat:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ecommerce_lojas_geo (cnpjloja, lat, lng, endereco_hash)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (cnpjloja) DO UPDATE
              SET lat = EXCLUDED.lat, lng = EXCLUDED.lng, endereco_hash = EXCLUDED.endereco_hash, geocoded_at = NOW()
            """,
            (cnpjloja, lat, lng, endereco_hash),
        )
        conn.commit()
        cur.close()
        return lat, lng
    lat, lng = nominatim_geocode(endereco, uf)
    if lat:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ecommerce_lojas_geo (cnpjloja, lat, lng, endereco_hash)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (cnpjloja) DO UPDATE
              SET lat = EXCLUDED.lat, lng = EXCLUDED.lng, endereco_hash = EXCLUDED.endereco_hash, geocoded_at = NOW()
            """,
            (cnpjloja, lat, lng, endereco_hash),
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
_catalogo_loja_cache: dict = {}
_catalogo_loja_cache_lock = threading.Lock()
_CATALOGO_LOJA_CACHE_TTL = 300


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
    with _catalogo_loja_cache_lock:
        _catalogo_loja_cache.clear()


def _catalogo_loja_cache_get(key: tuple):
    with _catalogo_loja_cache_lock:
        e = _catalogo_loja_cache.get(key)
        if e and time.time() - e["ts"] < _CATALOGO_LOJA_CACHE_TTL:
            return e["data"]
    return None


def _catalogo_loja_cache_set(key: tuple, data: list):
    with _catalogo_loja_cache_lock:
        if len(_catalogo_loja_cache) >= 50:
            oldest = min(_catalogo_loja_cache, key=lambda k: _catalogo_loja_cache[k]["ts"])
            del _catalogo_loja_cache[oldest]
        _catalogo_loja_cache[key] = {"data": data, "ts": time.time()}


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


_SYMPTOM_SEARCH_TERMS = {
    "febre": [
        "febre", "antitermico", "antitermica", "dipirona", "paracetamol",
        "ibuprofeno", "termometro",
    ],
    "dor": [
        "dor", "analgesico", "paracetamol", "dipirona", "ibuprofeno",
        "naproxeno", "dorflex", "neosaldina",
    ],
    "dor cabeca": ["dor de cabeca", "cefaleia", "analgesico", "dipirona", "paracetamol", "neosaldina"],
    "cabeca": ["dor de cabeca", "cefaleia", "analgesico", "dipirona", "paracetamol", "neosaldina"],
    "gripe": [
        "gripe", "resfriado", "antigripal", "benegrip", "cimegripe",
        "multigrip", "paracetamol", "dipirona", "vitamina c", "soro nasal",
    ],
    "resfriado": ["resfriado", "gripe", "antigripal", "soro nasal", "pastilha", "vitamina c"],
    "tosse": ["tosse", "xarope", "expectorante", "antitussigeno", "acetilcisteina", "ambroxol", "guaco"],
    "catarro": ["catarro", "expectorante", "acetilcisteina", "ambroxol", "xarope"],
    "garganta": ["dor de garganta", "garganta", "pastilha", "spray", "mel", "propolis"],
    "nariz": ["nariz", "congestao nasal", "soro nasal", "descongestionante", "rinossoro", "maresis"],
    "rinite": ["rinite", "antialergico", "loratadina", "cetirizina", "fexofenadina", "soro nasal"],
    "alergia": ["alergia", "antialergico", "loratadina", "cetirizina", "fexofenadina", "desloratadina"],
    "azia": ["azia", "queimacao", "antiacido", "omeprazol", "pantoprazol", "esomeprazol", "hidroxido"],
    "queimacao": ["queimacao", "azia", "antiacido", "omeprazol", "pantoprazol"],
    "enjoo": ["enjoo", "nausea", "antiemetico", "dimenidrinato", "dramin", "meclizina"],
    "nausea": ["nausea", "enjoo", "dimenidrinato", "dramin"],
    "diarreia": ["diarreia", "soro reidratacao", "probiótico", "probiotico", "loperamida", "floratil"],
    "prisao ventre": ["prisao de ventre", "constipacao", "laxante", "lactulose", "supositorio", "fibra"],
    "constipacao": ["constipacao", "prisao de ventre", "laxante", "lactulose", "fibra"],
    "colica": ["colica", "escopolamina", "buscopan", "simeticona", "analgesico"],
    "gases": ["gases", "simeticona", "dimeticona", "luftal"],
    "assadura": ["assadura", "pomada", "dexpantenol", "bepantol", "hipoglos", "nistatina"],
    "machucado": ["machucado", "ferimento", "curativo", "gaze", "antisseptico", "clorexidina", "agua oxigenada"],
    "ferimento": ["ferimento", "machucado", "curativo", "gaze", "antisseptico", "clorexidina"],
    "acne": ["acne", "antiacne", "gel limpeza", "sabonete facial", "peroxido benzoila"],
    "pele seca": ["pele seca", "hidratante", "creme hidratante", "loção hidratante", "dexpantenol"],
    "queimadura": ["queimadura", "pos sol", "dexpantenol", "aloe vera", "hidratante"],
    "insônia": ["insonia", "melatonina", "sono"],
    "insonia": ["insonia", "melatonina", "sono"],
}


def _search_terms_for_query(query):
    base = _norm_text(query)
    if not base:
        return []
    terms = [base]
    try:
        _ensure_produto_sintomas_schema()
        conn = db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT termos_busca, principio_ativo
            FROM ecommerce_produto_sintomas
            WHERE to_tsvector('portuguese',
                COALESCE(sintomas,'') || ' ' || COALESCE(termos_busca,'') || ' ' ||
                COALESCE(principio_ativo,'') || ' ' || COALESCE(classe_terapeutica,'')
            ) @@ plainto_tsquery('portuguese', %s)
               OR LOWER(COALESCE(sintomas,'') || ' ' || COALESCE(termos_busca,'')) LIKE %s
            LIMIT 20
            """,
            (base, f"%{base}%"),
        )
        for row in cur.fetchall():
            for field in (row.get("termos_busca") or "", row.get("principio_ativo") or ""):
                for t in re.split(r"[,;\s]+", field):
                    t = _norm_text(t)
                    if t and len(t) > 2 and t not in terms:
                        terms.append(t)
        cur.close()
    except Exception:
        pass
    # fallback ao dicionário hardcoded se a tabela não retornou expansão
    if len(terms) <= 1:
        for symptom, mapped in _SYMPTOM_SEARCH_TERMS.items():
            if symptom in base or base in symptom:
                terms.extend(_norm_text(m) for m in mapped)
    seen = set()
    result = []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _product_excluded_for_symptom_query(query, haystack):
    q = _norm_text(query)
    hay = _norm_text(haystack)
    if ("febre" in q or "gripe" in q or "resfriado" in q) and re.search(r"\b(buscopan|hioscina|escopolamina|tramadol|codeina|morfina|oxicodona)\b", hay):
        return True
    if ("diarreia" in q or "intestino" in q) and re.search(r"\b(shampoo|condicionador|creme para pentear|sabonete)\b", hay):
        return True
    return False


def _ensure_produto_sintomas_schema():
    _load_db_migrations()
    if "produto_sintomas" in _schema_ready:
        return
    with _schema_lock:
        if "produto_sintomas" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_produto_sintomas (
                ean TEXT PRIMARY KEY,
                nome_ref TEXT,
                sintomas TEXT,
                termos_busca TEXT,
                principio_ativo TEXT,
                classe_terapeutica TEXT,
                fonte TEXT DEFAULT 'regras',
                confianca TEXT DEFAULT 'media',
                atualizado_em TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_produto_sintomas_termos ON ecommerce_produto_sintomas USING gin (to_tsvector('portuguese', COALESCE(sintomas,'') || ' ' || COALESCE(termos_busca,'')))")
        conn.commit()
        cur.close()
        _schema_ready.add("produto_sintomas")


def _attach_product_symptoms(produtos):
    if not produtos:
        return produtos
    try:
        _ensure_produto_sintomas_schema()
        eans = sorted({_digits(p.get("ean")).lstrip("0") for p in produtos if _digits(p.get("ean"))})
        if not eans:
            return produtos
        conn = db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT LTRIM(COALESCE(ean,''), '0') AS ean_key,
                   COALESCE(sintomas,'') AS sintomas,
                   COALESCE(termos_busca,'') AS termos_busca,
                   COALESCE(principio_ativo,'') AS principio_ativo,
                   COALESCE(classe_terapeutica,'') AS classe_terapeutica
            FROM ecommerce_produto_sintomas
            WHERE LTRIM(COALESCE(ean,''), '0') = ANY(%s)
            """,
            (eans,),
        )
        by_ean = {r["ean_key"]: dict(r) for r in cur.fetchall()}
        cur.close()
        for produto in produtos:
            row = by_ean.get(_digits(produto.get("ean")).lstrip("0"))
            if not row:
                continue
            produto["sintomas"] = row.get("sintomas") or ""
            produto["termos_busca"] = row.get("termos_busca") or ""
            produto["principio_ativo"] = row.get("principio_ativo") or produto.get("principio_ativo") or ""
            produto["classe_terapeutica"] = row.get("classe_terapeutica") or produto.get("classe_terapeutica") or ""
    except Exception:
        pass
    return produtos


def _attach_product_promos(produtos):
    """Adiciona campo 'promo' a cada produto com promoção vigente em batch."""
    if not produtos:
        return
    try:
        _ensure_promo_schema()
        eans = sorted({_digits(p.get("ean")).lstrip("0") for p in produtos if _digits(p.get("ean"))})
        cnpjs = sorted({_digits(p.get("cnpjloja")) for p in produtos if _digits(p.get("cnpjloja"))})
        if not eans or not cnpjs:
            return
        conn = db(); cur = conn.cursor()
        cur.execute(
            """SELECT LTRIM(COALESCE(ean,''), '0') AS ean_key,
                      regexp_replace(COALESCE(cnpjloja,''), '\\D', '', 'g') AS cnpj_key,
                      cnpjloja, preco_promo, so_assinantes
                FROM ecommerce_promocoes
                WHERE ativo=TRUE AND (data_fim IS NULL OR data_fim > NOW())
                  AND LTRIM(COALESCE(ean,''), '0') = ANY(%s)
                  AND regexp_replace(COALESCE(cnpjloja,''), '\\D', '', 'g') = ANY(%s)""",
            (eans, cnpjs),
        )
        promo_map = {(r["ean_key"], r["cnpj_key"]): r for r in cur.fetchall()}
        if not promo_map:
            cur.close()
            return
        # Verifica assinaturas ativas do consumidor logado
        assinados = set()
        cid = str(session.get("consumidor_id") or "")
        if cid:
            cnpjs_promo = list({r["cnpj_key"] for r in promo_map.values()})
            ph2 = ",".join(["%s"] * len(cnpjs_promo))
            cur.execute(
                f"""SELECT regexp_replace(COALESCE(cnpjloja,''), '\\D', '', 'g') AS cnpj_key
                    FROM ecommerce_assinantes
                    WHERE consumidor_id=%s
                      AND regexp_replace(COALESCE(cnpjloja,''), '\\D', '', 'g') IN ({ph2})
                      AND status='ativo' AND pagamento_status='aprovado'
                      AND (data_fim IS NULL OR data_fim > NOW())""",
                [cid] + cnpjs_promo,
            )
            assinados = {r["cnpj_key"] for r in cur.fetchall()}
        cur.close()
        for p in produtos:
            cnpj_key = _digits(p.get("cnpjloja"))
            row = promo_map.get((_digits(p.get("ean")).lstrip("0"), cnpj_key))
            if not row:
                continue
            is_sub = cnpj_key in assinados
            preco_original = float(p.get("preco_original") or p.get("preco") or 0)
            preco_promo = float(row["preco_promo"])
            p["promo"] = {
                "so_assinantes": bool(row["so_assinantes"]),
                "preco_promo": preco_promo,
                "assinante_ativo": bool(is_sub),
                "aplicada": bool((not row["so_assinantes"]) or is_sub),
                "preco_original": preco_original,
            }
            if p["promo"]["aplicada"] and preco_promo > 0 and (not preco_original or preco_promo < preco_original):
                p["preco_original"] = preco_original
                p["preco"] = preco_promo
    except Exception:
        pass


def _assinante_ativo(cnpjloja: str, consumidor_id: str | None = None) -> bool:
    consumidor_id = str(consumidor_id or session.get("consumidor_id") or "")
    if not consumidor_id or not cnpjloja:
        return False
    try:
        _ensure_assinatura_schema()
        conn = db()
        cur = conn.cursor()
        cur.execute(
            """SELECT 1 FROM ecommerce_assinantes
               WHERE consumidor_id=%s
                 AND regexp_replace(COALESCE(cnpjloja,''), '\\D', '', 'g') = regexp_replace(%s, '\\D', '', 'g')
                 AND status='ativo' AND pagamento_status='aprovado'
                 AND (data_fim IS NULL OR data_fim > NOW())
               LIMIT 1""",
            (consumidor_id, cnpjloja),
        )
        ok = cur.fetchone() is not None
        cur.close()
        return ok
    except Exception:
        return False


def _assinatura_vigente_row(row) -> bool:
    if not row:
        return False
    if row.get("status") != "ativo" or row.get("pagamento_status") != "aprovado":
        return False
    fim = row.get("data_fim")
    if fim is None:
        return True
    now = datetime.now(fim.tzinfo) if getattr(fim, "tzinfo", None) else datetime.now()
    return fim > now


def _consumidor_e_assinante(consumidor_id, cnpjloja) -> bool:
    """Checagem ao vivo (sem cache) — corta benefício de assinante no instante
    em que status/pagamento/data_fim deixam de valer, sem depender de nenhum
    job rodar antes."""
    if not consumidor_id or not cnpjloja:
        return False
    try:
        _ensure_assinatura_schema()
        conn = db(); cur = conn.cursor()
        cur.execute(
            """SELECT 1 FROM ecommerce_assinantes
               WHERE consumidor_id=%s AND cnpjloja=%s
                 AND status='ativo' AND pagamento_status='aprovado'
                 AND (data_fim IS NULL OR data_fim > NOW())
               LIMIT 1""",
            (str(consumidor_id), cnpjloja),
        )
        return bool(cur.fetchone())
    except Exception:
        return False


def _preco_produto_com_promocao(cnpjloja: str, ean: str, preco_base, consumidor_id: str | None = None) -> tuple[float, dict | None]:
    """Aplica, por cima do preco_base (que pode vir do Alpha ou de qualquer
    outra fonte), a promocao so-assinantes lancada em ecommerce_promocoes —
    e uma camada de desconto independente do preco base, igual ao cupom."""
    preco = float(preco_base or 0)
    if not cnpjloja or not ean:
        return preco, None
    try:
        _ensure_promo_schema()
        conn = db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT preco_promo, so_assinantes
            FROM ecommerce_promocoes
            WHERE regexp_replace(COALESCE(cnpjloja,''), '\\D', '', 'g') = regexp_replace(%s, '\\D', '', 'g')
              AND LTRIM(COALESCE(ean, ''), '0') = LTRIM(%s, '0')
              AND ativo=TRUE
              AND (data_fim IS NULL OR data_fim > NOW())
            ORDER BY data_inicio DESC, id DESC
            LIMIT 1
            """,
            (cnpjloja, ean),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return preco, None
        assinante = _assinante_ativo(cnpjloja, consumidor_id)
        promo = {
            "so_assinantes": bool(row["so_assinantes"]),
            "preco_promo": float(row["preco_promo"]),
            "assinante_ativo": assinante,
            "aplicada": bool((not row["so_assinantes"]) or assinante),
            "preco_original": preco,
        }
        if promo["aplicada"] and promo["preco_promo"] > 0 and (not preco or promo["preco_promo"] < preco):
            return promo["preco_promo"], promo
        return preco, promo
    except Exception:
        return preco, None


def _preco_catalogo_atual(cnpjloja: str, ean: str, fallback=0) -> float:
    preco_fallback = float(fallback or 0)
    if not cnpjloja or not ean:
        return preco_fallback
    try:
        conn = db()
        cur = conn.cursor()
        if _alpha_enabled():
            try:
                _ensure_alpha_schema()
                cur.execute(
                    """
                    SELECT preco_atual AS preco
                    FROM ecommerce_alpha_produtos
                    WHERE cnpjloja=%s
                      AND LTRIM(COALESCE(ean, ''), '0') = LTRIM(%s, '0')
                      AND COALESCE(inativo, false) = false
                      AND COALESCE(estoque, 0) > 0
                    LIMIT 1
                    """,
                    (cnpjloja, ean),
                )
                row_alpha = cur.fetchone()
                if row_alpha and row_alpha.get("preco") is not None:
                    cur.close()
                    return float(row_alpha["preco"] or 0)
            except Exception:
                pass
            cur.close()
            return 0.0
        cur.execute(
            """
            SELECT COALESCE(ep.preco_customizado, vg.preco_venda, vg_market.preco_venda, e.preco_referencial) AS preco
            FROM estoque e
            LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = e.cnpj AND ep.ean = e.barras
            LEFT JOIN LATERAL (
                SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
                FROM vendageral
                WHERE cnpj = e.cnpj AND ean = e.barras
                  AND total_vendasgeral > 0 AND itens > 0
                ORDER BY id DESC LIMIT 1
            ) vg ON TRUE
            LEFT JOIN LATERAL (
                SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
                FROM vendageral
                WHERE ean = e.barras
                  AND total_vendasgeral > 0 AND itens > 0
                ORDER BY id DESC LIMIT 1
            ) vg_market ON TRUE
            WHERE e.cnpj=%s
              AND (e.barras=%s OR LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0') = LTRIM(%s, '0'))
              AND e.estoque > 0
            LIMIT 1
            """,
            (cnpjloja, ean, ean),
        )
        row = cur.fetchone()
        if row and row.get("preco") is not None:
            cur.close()
            return float(row["preco"] or 0)
        cur.execute(
            """
            SELECT COALESCE(ep.preco_customizado, av.preco_venda, ae.valor_final_produto) AS preco
            FROM automatiza_estoque ae
            LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = ae.cnpj_loja AND ep.ean = ae.ean
            LEFT JOIN LATERAL (
                SELECT ROUND(valor_final_vendido / NULLIF(quantidade_vendida, 0), 2) AS preco_venda
                FROM automatiza_vendas
                WHERE cnpj_loja = ae.cnpj_loja AND ean = ae.ean
                  AND valor_final_vendido > 0 AND quantidade_vendida > 0
                ORDER BY id DESC LIMIT 1
            ) av ON TRUE
            WHERE ae.cnpj_loja=%s
              AND LTRIM(COALESCE(ae.ean, ''), '0') = LTRIM(%s, '0')
              AND ae.quantidade_estoque > 0
            LIMIT 1
            """,
            (cnpjloja, ean),
        )
        row = cur.fetchone()
        cur.close()
        if row and row.get("preco") is not None:
            return float(row["preco"] or 0)
    except Exception:
        pass
    return preco_fallback


def _symptom_index_eans_for_query(query, limit=250):
    base = _norm_text(query)
    if not base:
        return []
    try:
        _ensure_produto_sintomas_schema()
        conn = db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ean
            FROM ecommerce_produto_sintomas
            WHERE to_tsvector('portuguese',
                COALESCE(sintomas,'') || ' ' || COALESCE(termos_busca,'') || ' ' ||
                COALESCE(principio_ativo,'') || ' ' || COALESCE(classe_terapeutica,'')
            ) @@ plainto_tsquery('portuguese', %s)
               OR LOWER(COALESCE(sintomas,'') || ' ' || COALESCE(termos_busca,'') || ' ' ||
                        COALESCE(principio_ativo,'') || ' ' || COALESCE(classe_terapeutica,'')) LIKE %s
            ORDER BY
                -- EANs brasileiros reais (789xxxxxxxxxx) primeiro
                CASE WHEN ean ~ '^789[0-9]{10}$' THEN 0 ELSE 1 END,
                ean
            LIMIT %s
            """,
            (base, f"%{base}%", limit),
        )
        rows = [r["ean"] for r in cur.fetchall()]
        cur.close()
        return rows
    except Exception:
        return []


_BUSCA_CACHE_VERSION = "v2"  # incrementar para invalidar cache quando o prompt do Claude mudar

def _norm_query_cache(query):
    """Normaliza query para chave de cache: lowercase, sem acentos, espaços simples."""
    value = (query or "").strip().lower()
    value = unicodedata.normalize("NFD", value)
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    norm = re.sub(r"\s+", " ", value).strip()
    return f"{_BUSCA_CACHE_VERSION}:{norm}" if norm else norm


_busca_cache_schema_ok = False
_busca_cache_schema_lock = threading.Lock()


def _ensure_busca_cache_schema():
    global _busca_cache_schema_ok
    if _busca_cache_schema_ok:
        return
    with _busca_cache_schema_lock:
        if _busca_cache_schema_ok:
            return
        try:
            conn = db()
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS busca_cache (
                    query_normalizada TEXT PRIMARY KEY,
                    resultado_ia      JSONB NOT NULL,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at        TIMESTAMPTZ NOT NULL
                )
            """)
            conn.commit()
            cur.close()
            _busca_cache_schema_ok = True
        except Exception:
            pass


_BUSCA_IA_MEM_CACHE: dict = {}   # {query_norm: (resultado, expires_ts)}
_BUSCA_IA_MEM_MAX  = 300
_BUSCA_IA_MEM_TTL  = 1800        # 30 min — Vercel pode reiniciar workers a qualquer hora


def _busca_cache_get(query_norm):
    """Retorna resultado_ia do cache (memória primeiro, depois DB)."""
    # 1. Memória
    entry = _BUSCA_IA_MEM_CACHE.get(query_norm)
    if entry:
        resultado, exp = entry
        if time.time() < exp:
            return resultado
        _BUSCA_IA_MEM_CACHE.pop(query_norm, None)
    # 2. Banco
    try:
        _ensure_busca_cache_schema()
        conn = db()
        cur = conn.cursor()
        cur.execute(
            "SELECT resultado_ia FROM busca_cache WHERE query_normalizada = %s AND expires_at > NOW()",
            (query_norm,),
        )
        row = cur.fetchone()
        cur.close()
        if row:
            resultado = row["resultado_ia"]
            # Promove para memória para evitar roundtrip na próxima vez
            _BUSCA_IA_MEM_CACHE[query_norm] = (resultado, time.time() + _BUSCA_IA_MEM_TTL)
            return resultado
    except Exception:
        pass
    return None


def _busca_cache_set(query_norm, resultado_ia):
    """Salva resultado da IA no cache (memória + DB, TTL 3 dias).

    O cache é global por query normalizada — a mesma busca reaproveita o
    resultado para todos os usuários (logados e anônimos), então a IA roda
    no máximo uma vez por busca distinta a cada 3 dias."""
    # Memória imediata
    if len(_BUSCA_IA_MEM_CACHE) >= _BUSCA_IA_MEM_MAX:
        try:
            oldest = min(_BUSCA_IA_MEM_CACHE, key=lambda k: _BUSCA_IA_MEM_CACHE[k][1])
            _BUSCA_IA_MEM_CACHE.pop(oldest, None)
        except Exception:
            pass
    _BUSCA_IA_MEM_CACHE[query_norm] = (resultado_ia, time.time() + _BUSCA_IA_MEM_TTL)
    # Banco em background para persistir entre restarts
    def _persist():
        try:
            _ensure_busca_cache_schema()
            conn2 = _new_conn()
            cur2 = conn2.cursor()
            cur2.execute(
                """
                INSERT INTO busca_cache (query_normalizada, resultado_ia, created_at, expires_at)
                VALUES (%s, %s::jsonb, NOW(), NOW() + INTERVAL '3 days')
                ON CONFLICT (query_normalizada) DO UPDATE
                    SET resultado_ia = EXCLUDED.resultado_ia,
                        created_at   = NOW(),
                        expires_at   = NOW() + INTERVAL '3 days'
                """,
                (query_norm, json.dumps(resultado_ia)),
            )
            conn2.commit()
            cur2.close()
            conn2.close()
        except Exception:
            pass
    threading.Thread(target=_persist, daemon=True).start()


_NL_SYMPTOM_WORDS = {
    "dor", "febre", "tosse", "gripe", "resfriado", "nariz", "garganta",
    "pressao", "diabetes", "colesterol", "ansiedade", "depressao", "insonia",
    "gastrite", "azia", "refluxo", "alergia", "infeccao", "inflamacao",
    "enjoo", "tontura", "enxaqueca", "sinusite", "bronquite", "asma",
    "anemia", "tireoide", "reumatismo", "artrite", "artrose",
    "hemorroida", "prisao", "diarreia", "nausea", "vomito", "hipertensao",
    "colica", "gases", "catarro", "rinite", "queimacao", "constipacao",
    "acne", "assadura", "ferimento", "queimadura", "machucado",
    "intestino", "estomago", "figado", "rim", "coracao", "pulmao",
    "cabeca", "costas", "perna", "joelho", "ombro", "pescoco",
}


def _is_natural_language_query(query):
    """True para qualquer busca textual — a IA interpreta sintomas, marcas e princípios ativos.
    Exceção: EANs puros (8-14 dígitos) vão direto para busca por código."""
    q = _norm_query_cache(query)
    if not q:
        return False
    # EAN puro → busca direta
    if re.match(r'^\d{8,14}$', q.replace(' ', '')):
        return False
    return True


def _busca_fuzzy_pg_trgm(term, limit=60):
    """Busca fuzzy via pg_trgm em medicamentos; retorna lista de EANs."""
    norm = _norm_text(term)
    if not norm or len(norm) < 3:
        return []
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT barra_norm AS ean
            FROM medicamentos
            WHERE similarity(LOWER(COALESCE(descricao, '')), %s) > 0.2
               OR LOWER(COALESCE(descricao, '')) LIKE %s
            ORDER BY similarity(LOWER(COALESCE(descricao, '')), %s) DESC
            LIMIT %s
            """,
            (norm, f"%{norm}%", norm, limit),
        )
        eans = [r["ean"] for r in cur.fetchall() if r.get("ean")]
        cur.close()
        return eans
    except Exception:
        return []


def _claude_busca_interpret(query):
    """
    Interpreta query de linguagem natural via Claude Haiku.
    Checa cache antes de chamar a API e salva resultado após chamada bem-sucedida.
    Retorna dict {principios_ativos, nomes_tecnicos, categorias, termos_busca} ou None.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    query_norm = _norm_query_cache(query)
    cached = _busca_cache_get(query_norm)
    if cached is not None:
        return cached
    prompt = (
        "Você é Poupinha, assistente simpática da Drogarias Poupaqui. "
        "Dado uma busca de produto, retorne um JSON com:\n"
        "1. Uma saudação curta e amigável (campo 'saudacao') — 1 frase NEUTRA, sem mencionar "
        "sintomas, doenças, condições médicas ou indicações terapêuticas. "
        "Diga apenas que vai mostrar o que está disponível. Sem emojis.\n"
        "2. Os medicamentos/substâncias para buscar no catálogo.\n"
        "Retorne SOMENTE um JSON válido sem markdown:\n"
        '{"saudacao":"Veja o que encontramos disponível nas farmácias próximas:",'
        '"principios_ativos":["escopolamina","simeticona"],"nomes_tecnicos":["butilescopolamina"],'
        '"categorias":["antiesp"],"termos_busca":["buscopan"]}\n'
        "REGRAS OBRIGATÓRIAS:\n"
        "- saudacao: NUNCA mencione doenças, sintomas, condições ou indicações (ex: PROIBIDO dizer 'alergia', 'dor', 'pressão', 'diabetes' etc). Diga apenas 'Veja o que encontramos!' ou similar\n"
        "- principios_ativos: nomes exatos das substâncias ativas que aparecem em bulas\n"
        "- nomes_tecnicos: outros princípios ativos ou nomes farmacológicos alternativos\n"
        "- categorias: classe terapêutica sem hifens e sem acentos\n"
        "- termos_busca: palavras curtas que aparecem literalmente em nomes de produtos no estoque\n"
        "- Use somente termos em português SEM acentos e SEM hifens (exceto na saudacao)\n"
        "- Prefira nomes de substâncias ativas a nomes de condições (losartana, não hipertensão)\n"
        "- Se não souber, retorne listas vazias\n"
        f"Busca: {query}"
    )
    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 400,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
        method="POST",
    )
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = (data.get("content") or [{}])[0].get("text", "").strip()
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
        resultado = json.loads(text)
        # Só cacheia se a IA retornou pelo menos um termo útil
        if any(resultado.get(k) for k in ("principios_ativos", "nomes_tecnicos", "categorias", "termos_busca")):
            _busca_cache_set(query_norm, resultado)
        return resultado
    except Exception:
        return None


def _catalog_product_key(nome):
    text = _norm_text(nome)
    text = re.sub(r"\b(capsulas|capsula|caps|cps|comprimidos|comprimido|comp|cp)\b", "cp", text)
    text = re.sub(r"\b(fr|frasco)\b", "fr", text)
    text = re.sub(r"\b(c|com)\s*(\d+)\b", r"c \1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or ""


def _prefer_display_product(current, produto):
    cur_img = bool((current.get("imagem") or "").strip())
    new_img = bool((produto.get("imagem") or "").strip())
    cur_dist = current.get("distancia_km")
    new_dist = produto.get("distancia_km")
    if new_img and not cur_img:
        return True
    if new_img != cur_img:
        return False
    cur_placeholder = bool(current.get("imagem_padrao_poupaqui"))
    new_placeholder = bool(produto.get("imagem_padrao_poupaqui"))
    if cur_placeholder and not new_placeholder:
        return True
    if new_placeholder and not cur_placeholder:
        return False
    if new_dist is not None and (cur_dist is None or new_dist < cur_dist):
        return True
    if new_dist == cur_dist:
        try:
            return float(produto.get("preco") or 0) < float(current.get("preco") or 0)
        except Exception:
            return False
    return False


def _dedupe_products_by_store_ean(produtos):
    best = {}
    no_ean = []
    for produto in produtos:
        ean_key = _digits(produto.get("ean"))
        if not ean_key:
            no_ean.append(produto)
            continue
        store_key = _digits(produto.get("cnpjloja")) or (produto.get("cnpjloja") or "").strip()
        key = (store_key, ean_key)
        current = best.get(key)
        if current is None or _prefer_display_product(current, produto):
            best[key] = produto
    return list(best.values()) + no_ean


def _dedupe_products_for_display(produtos):
    produtos = _dedupe_products_by_store_ean(produtos)
    best = {}
    for produto in produtos:
        product_key = _catalog_product_key(produto.get("nome") or "") or (produto.get("ean") or "").strip()
        if not product_key:
            continue
        store_key = _digits(produto.get("cnpjloja")) or (produto.get("cnpjloja") or "").strip()
        key = (store_key, product_key)
        current = best.get(key)
        if current is None:
            best[key] = produto
            continue
        if _prefer_display_product(current, produto):
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
    r"drogaria\s+s[aã]o\s+paulo|drogaria[\s_-]?sp\b|drogariasp\b|drogariasaopaulo"
    r"|drogaria\s+s[aã]o\s+jo[aã]o|saojoao|s[aã]o\s+jo[aã]o"
    r"|droga\s*raia|drogasil|pague\s*menos|panvel|nissei|venancio"
    r"|ultrafarma|drogaria\s+araujo|drogaria\s+minas|farm[aá]cia\s+brito"
    r"|drogaria\s+santa|drogariasantaterezinha|farmacias?\s+heroos|farmaciasheroos|farmais|nova\s*farmais"
    r"|meu\s+mundo\s+fit|formosa|farmasesi|drogaria\s+canabrava"
    r"|farmalan|avante\s+farm[aá]cia"
    r"|drogarias?|farm[aá]cias?|(?<!consulta)remedios|(?<!farma)c[eê]utic",
    # Removido o token genérico "farma": casava com nomes de fornecedores/pastas
    # legítimos (ex: MAXIFARMA no caminho da imagem do Alpha) e bloqueava fotos
    # válidas no detalhe do produto. Marcas concorrentes específicas continuam
    # cobertas acima (ultrafarma, farmalan, farmais, farm[aá]cias? etc).
    re.IGNORECASE,
)

_OCR_TEXT_CACHE = {}


def _fetch_cosmos_api_image_url(ean):
    """Chama a API Bluesoft Cosmos diretamente pelo EAN e retorna a URL da thumbnail.

    Compartilha a mesma cota diária do cosmos_sync.py. Usar como fallback apenas
    quando o produto não está em produto_canon.imagem_cosmos.
    """
    token = os.getenv("COSMOS_TOKEN", "").strip()
    if not token:
        return None
    ean_digits = _digits(ean)
    if len(ean_digits) < 8:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.cosmos.bluesoft.com.br/gtins/{ean_digits}",
            headers={
                "X-Cosmos-Token": token,
                "User-Agent": "Cosmos-API-Request",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return None
    thumb = (data.get("thumbnail") or "").strip()
    return thumb if thumb.startswith("http") else None


_NON_PRODUCT_IMAGE_RE = re.compile(
    r"sua\s+sa[uú]de|f[aá]cil\s+e\s+acess[ií]vel|tempo\s+e\s+dinheiro"
    r"|delivery|entrega|frete|promo[cç][aã]o|oferta|desconto"
    r"|banner|hero|rem[eé]dios|drogarias?\s+online"
    # Imagens de pessoa/lifestyle exibindo produto
    r"|clique\s+aqui|compre\s+agora|saiba\s+mais|aproveite"
    r"|consulte\s+seu\s+m[eé]dico|sob\s+prescri[cç][aã]o"
    r"|imagem\s+(meramente\s+)?ilustrativa|foto\s+ilustrativa",
    re.IGNORECASE,
)


def _looks_like_other_pharmacy_brand(*values):
    blob = " ".join(v or "" for v in values)
    return bool(_OTHER_PHARMACY_BRANDS_RE.search(blob))


def _ocr_space_api_key():
    return os.getenv("OCR_SPACE_API_KEY", "").strip()


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
        timeout = float(os.getenv("OCR_SPACE_TIMEOUT", "2"))
        with urllib.request.urlopen(req, timeout=timeout) as r:
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
    """Busca imagem via Serper (Google Images).

    Tenta 3 variações de query em ordem crescente de abrangência:
    1. EAN entre aspas (exato)
    2. EAN sem aspas (mais resultados)
    3. EAN + primeiras palavras do nome (quando EAN sozinho não tem resultados)

    A verificação de EAN é garantida pela query — os filtros de farmácia/banner
    protegem contra imagens inadequadas.
    """
    api_key = _serper_api_key()
    ean_digits = _digits(ean)
    if not api_key or len(ean_digits) < 8:
        return None

    # monta as queries em ordem de prioridade
    nome_curto = " ".join((nome or "").split()[:4])
    queries = [
        f'{ean_digits} {nome_curto}'.strip() if nome_curto else ean_digits,
        f'"{ean_digits}" {nome_curto}'.strip() if nome_curto else f'"{ean_digits}"',
        f'"{ean_digits}"',
        ean_digits,
    ]
    # remove duplicatas mantendo ordem
    seen_q: set = set()
    queries = [q for q in queries if not (q in seen_q or seen_q.add(q))]

    for q in queries:
        try:
            data = _post_json(
                "https://google.serper.dev/images",
                {"q": q, "num": 10, "gl": "br", "hl": "pt-br"},
                headers={"X-API-KEY": api_key},
                timeout=12,
            )
        except Exception:
            return None
        images = data.get("images") or []
        if not images:
            continue
        for item in images[:10]:
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
            return image_url
        # se ainda há queries restantes, tenta a próxima
    return None


def _download_image_for_storage(image_url):
    try:
        req = urllib.request.Request(
            image_url,
            headers={"User-Agent": "PoupaquiEcommerce/1.0 (catalog-image-fill)"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            content_type = r.headers.get("Content-Type", "")
            raw = r.read(4 * 1024 * 1024)
        if not content_type.lower().startswith("image/"):
            if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                content_type = "image/png"
            elif raw.startswith(b"\xff\xd8\xff"):
                content_type = "image/jpeg"
            elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
                content_type = "image/webp"
            else:
                return None, None, None
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
        placeholder = None
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
        cached_url = _first_valid_url(cached["imagem_url"] if cached else None)
        # Ignorar placeholder de medicamento salvo para produto não-medicamento
        if cached_url in (GENERIC_TARJA_VERMELHA_IMG, GENERIC_TARJA_PRETA_IMG):
            _tipo_fill = _TIPO_ALIAS.get(_classificar_produto(nome or ""), _classificar_produto(nome or ""))
            if _tipo_fill in _TIPOS_NAO_MEDICAMENTO:
                cached_url = None
        image_url = image_url or cached_url
        # Imagem própria Vitnatu (upload manual do fabricante) — mais confiável que busca externa
        if not image_url:
            cur.execute(
                "SELECT imagem_url FROM vitnatu_imagens WHERE LTRIM(ean, '0') = LTRIM(%s, '0') "
                "AND imagem_url IS NOT NULL AND TRIM(imagem_url) <> '' LIMIT 1",
                (ean_digits,),
            )
            vitnatu_row = cur.fetchone()
            if vitnatu_row:
                image_url = _first_valid_url(vitnatu_row["imagem_url"])
        # Tenta imagem do cosmos já indexada pelo script de sincronização
        if not image_url:
            cur.execute(
                "SELECT imagem_cosmos FROM produto_canon WHERE ean = %s AND imagem_cosmos IS NOT NULL AND TRIM(imagem_cosmos) <> '' LIMIT 1",
                (ean_digits,),
            )
            cosmos_row = cur.fetchone()
            if cosmos_row:
                image_url = _first_valid_url(cosmos_row["imagem_cosmos"])
        if not image_url:
            source_urls = [_fetch_exact_barcode_image_url(ean_digits)]
            if nome:
                source_urls.append(_fetch_verified_serper_image_url(ean_digits, nome))
                source_urls.append(_fetch_serper_image_result_url(ean_digits, nome))
            seen_sources = set()
            for source_url in source_urls:
                if not source_url or source_url in seen_sources:
                    continue
                seen_sources.add(source_url)
                if (
                    _looks_like_other_pharmacy_brand(source_url)
                    or _image_has_other_pharmacy_text(source_url)
                    or _image_looks_non_product(source_url)
                ):
                    continue
                raw, ext, content_type = _download_image_for_storage(source_url)
                if raw:
                    image_url = upload_to_supabase_storage(
                        raw,
                        f"auto-ean/{ean_digits}.{ext}",
                        content_type,
                    )
                    if image_url:
                        break
        if image_url:
            _upsert_catalog_image(cur, cnpjloja, ean_key, image_url)
            _upsert_catalog_image_all_stores(cur, ean_digits, image_url)
            conn.commit()
            cur.close()
            return image_url
        # Salva fallback genérico apenas se o produto merece a caixinha (tem tarja ou "genérico" no nome)
        if not placeholder:
            cur.close()
            return None  # produto sem tarja/genérico não recebe a caixinha de medicamento
        fallback = GENERIC_TARJA_VERMELHA_IMG
        _upsert_catalog_image(cur, cnpjloja, ean_key, fallback)
        conn.commit()
        cur.close()
        return fallback
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


_MEDICINE_PLACEHOLDER_URLS = frozenset({GENERIC_TARJA_VERMELHA_IMG, GENERIC_TARJA_PRETA_IMG})

def _is_alpha_product(produto):
    return (produto.get("fonte_estoque") or "").strip().lower() == "alpha_a7"


def _has_catalog_image(produto):
    img = (produto.get("imagem") or "").strip()
    if not img:
        return False
    if _is_alpha_product(produto) and (img in _MEDICINE_PLACEHOLDER_URLS or produto.get("imagem_padrao_poupaqui")):
        return bool(produto.get("imagem_bloqueada_anvisa") and produto.get("anvisa_cache_encontrado"))
    if img in _MEDICINE_PLACEHOLDER_URLS:
        tarja = (produto.get("tarja") or "").strip().lower()
        if tarja in ("vermelha", "preta"):
            return True
        # exibir=False confirma produto tarjado mesmo sem tarja explícita
        if produto.get("exibir_imagem_publica") is False:
            return True
        if produto.get("imagem_bloqueada_anvisa"):
            return True
        return False
    return True


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


def _apply_safe_catalog_images(produtos, cur=None, persist_placeholders=True):
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
                       descricao, marca, classe, laboratorio
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

    to_persist: list = []
    to_cleanup: list = []
    _MED_PLACEHOLDERS = {GENERIC_TARJA_VERMELHA_IMG, GENERIC_TARJA_PRETA_IMG}

    # Cosmos batch lookup for products that currently have no image
    _sem_img_eans = [_digits(p.get("ean")) for p in produtos if not (p.get("imagem") or "").strip() or (p.get("imagem") or "") in _MED_PLACEHOLDERS]
    _cosmos_by_ean: dict = {}
    if _sem_img_eans:
        try:
            _own_cosmos_cur = cur is None
            _cc = db().cursor() if _own_cosmos_cur else cur
            _cc.execute(
                "SELECT ean, imagem_cosmos FROM produto_canon WHERE ean = ANY(%s)"
                " AND imagem_cosmos IS NOT NULL AND TRIM(imagem_cosmos) <> ''"
                " AND fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')"
                " AND imagem_cosmos NOT LIKE '%12466%'",
                (_sem_img_eans,),
            )
            _cosmos_by_ean = {r["ean"]: r["imagem_cosmos"] for r in _cc.fetchall()}
            if _own_cosmos_cur:
                _cc.close()
        except Exception:
            pass

    for produto in produtos:
        ean_key = _digits(produto.get("ean")).lstrip("0")
        med = med_by_ean.get(ean_key, {})
        if med.get("laboratorio") and not (produto.get("laboratorio") or "").strip():
            produto["laboratorio"] = med["laboratorio"]
        if med.get("marca") and not (produto.get("marca") or "").strip():
            produto["marca"] = med["marca"]
        anvisa = {"tarja": produto.get("tarja") or ""}
        placeholder = _placeholder_for_tarja(anvisa.get("tarja"))
        imagem_atual = produto.get("imagem") or ""
        _tipo_p_raw = produto.get("categoria") or med.get("tipo_ia") or _classificar_produto(produto.get("nome") or "")
        _tipo_p = _TIPO_ALIAS.get(_tipo_p_raw, _tipo_p_raw)
        _exibir_publicamente = produto.get("exibir_imagem_publica")
        _alpha_item = _is_alpha_product(produto)
        if _alpha_item and imagem_atual in _MED_PLACEHOLDERS:
            produto["imagem"] = ""
            produto.pop("imagem_padrao_poupaqui", None)
            produto.pop("imagem_bloqueada_anvisa", None)
            imagem_atual = ""
            cnpj_c = produto.get("cnpjloja")
            ean_c  = (produto.get("ean") or "").strip()
            if cnpj_c and ean_c:
                to_cleanup.append((cnpj_c, ean_c))
        # Decisao explicita da fonte oficial prevalece sobre o fallback por tarja.
        if imagem_atual in _MED_PLACEHOLDERS and (
            _exibir_publicamente is True
            or not placeholder
            or _tipo_p in _TIPOS_NAO_MEDICAMENTO
        ):
            produto["imagem"] = ""
            imagem_atual = ""
            cnpj_c = produto.get("cnpjloja")
            ean_c  = (produto.get("ean") or "").strip()
            if cnpj_c and ean_c:
                to_cleanup.append((cnpj_c, ean_c))
        # Aplica imagem do cosmos quando produto não tem imagem
        if not imagem_atual:
            _cosmos_img = _cosmos_by_ean.get(_digits(produto.get("ean")))
            if _cosmos_img:
                produto["imagem"] = _cosmos_img
                imagem_atual = _cosmos_img
        if _alpha_item:
            continue
        if (
            _tipo_p not in _TIPOS_NAO_MEDICAMENTO
            and _exibir_publicamente is not True
            and placeholder
            and anvisa.get("tarja") in ("preta", "vermelha")
        ):
            produto["imagem"] = placeholder
            produto["imagem_padrao_poupaqui"] = True
            produto["imagem_bloqueada_anvisa"] = True
        elif _looks_like_other_pharmacy_brand(imagem_atual) or (placeholder and _is_untrusted_scraped_image(imagem_atual) and _image_has_other_pharmacy_text(imagem_atual)):
            produto["imagem"] = placeholder
            produto["imagem_padrao_poupaqui"] = bool(placeholder)
            produto["imagem_bloqueada_marca_farmacia"] = True
        elif not imagem_atual and placeholder and _tipo_p not in _TIPOS_NAO_MEDICAMENTO:
            produto["imagem"] = placeholder
            produto["imagem_padrao_poupaqui"] = True
            cnpj = produto.get("cnpjloja")
            ean  = (produto.get("ean") or "").strip()
            if cnpj and ean:
                to_persist.append((cnpj, ean, placeholder))

    # Persiste placeholders em lote para que requisições futuras os encontrem via JOIN direto
    if persist_placeholders and to_persist:
        try:
            conn2 = _new_conn()
            wc = conn2.cursor()
            wc.executemany(
                """
                INSERT INTO ecommerce_produto_imagens (cnpjloja, ean, imagem_url)
                VALUES (%s, %s, %s)
                ON CONFLICT (cnpjloja, ean) DO NOTHING
                """,
                to_persist,
            )
            conn2.commit()
            wc.close()
            conn2.close()
        except Exception:
            pass

    # Remove placeholders de medicamento indevidamente salvos para produtos não-medicamento
    if persist_placeholders and to_cleanup:
        try:
            conn3 = _new_conn()
            wc3 = conn3.cursor()
            for cnpj_cl, ean_cl in to_cleanup:
                wc3.execute(
                    """
                    UPDATE ecommerce_produto_imagens
                       SET imagem_url = NULL
                     WHERE cnpjloja = %s AND ean = %s
                       AND imagem_url IN (%s, %s)
                    """,
                    (cnpj_cl, ean_cl, GENERIC_TARJA_VERMELHA_IMG, GENERIC_TARJA_PRETA_IMG),
                )
            conn3.commit()
            wc3.close()
            conn3.close()
        except Exception:
            pass

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

_IMAGEM_FILTER_ALPHA = """AND (
            COALESCE(e.barras_norm, e.barras) IN (
                SELECT barra_norm FROM medicamentos
                WHERE barra_norm IS NOT NULL
                  AND (
                    NULLIF(TRIM(imagem), '') IS NOT NULL
                    OR id IN (SELECT medicamento_id FROM medicamentos_imagens
                              WHERE cloudinary_url IS NOT NULL)
                  )
            )
            OR COALESCE(e.barras_norm, e.barras) IN (
                SELECT ean FROM produto_canon
                WHERE imagem_cosmos IS NOT NULL AND TRIM(imagem_cosmos) <> ''
                  AND fonte NOT IN ('cosmos_miss', 'ia_miss')
            )
            OR EXISTS (
                SELECT 1
                FROM ecommerce_produto_imagens epi0
                WHERE epi0.cnpjloja = e.cnpj
                  AND LTRIM(COALESCE(epi0.ean, ''), '0') = LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0')
                  AND epi0.imagem_url IS NOT NULL
                  AND TRIM(epi0.imagem_url) <> ''
            )
            OR EXISTS (
                SELECT 1 FROM medicamentos5 m5x
                WHERE m5x.barra = e.barras
                  AND NULLIF(TRIM(m5x.imagem), '') IS NOT NULL
            )
          )"""

_IMAGEM_FILTER_ALPHA_A7 = """AND (
            ap.imagem_url IS NOT NULL
            OR ap.ean IN (
                SELECT barra_norm FROM medicamentos
                WHERE barra_norm IS NOT NULL
                  AND (
                    NULLIF(TRIM(imagem), '') IS NOT NULL
                    OR id IN (SELECT medicamento_id FROM medicamentos_imagens
                              WHERE cloudinary_url IS NOT NULL)
                  )
            )
            OR ap.ean IN (
                SELECT ean FROM produto_canon
                WHERE imagem_cosmos IS NOT NULL AND TRIM(imagem_cosmos) <> ''
                  AND fonte NOT IN ('cosmos_miss', 'ia_miss')
            )
            OR EXISTS (
                SELECT 1
                FROM ecommerce_produto_imagens epi0
                WHERE epi0.cnpjloja = ap.cnpjloja
                  AND LTRIM(COALESCE(epi0.ean, ''), '0') = LTRIM(COALESCE(ap.ean, ''), '0')
                  AND epi0.imagem_url IS NOT NULL
                  AND TRIM(epi0.imagem_url) <> ''
            )
            OR EXISTS (
                SELECT 1 FROM medicamentos5 m5x
                WHERE m5x.barra = ap.ean
                  AND NULLIF(TRIM(m5x.imagem), '') IS NOT NULL
            )
          )"""

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
          {imagem_filter}
        ORDER BY e.descricao
        LIMIT {limite}
    )
    SELECT
        el.barras                                                            AS ean,
        COALESCE(m.descricao, pc.descricao_canon, el.descricao)             AS nome,
        COALESCE(elab.laboratorio, pc.laboratorio, m.laboratorio)            AS laboratorio,
        m.marca                                                              AS marca,
        el.qty,
        COALESCE(vg.preco_venda, vg_market.preco_venda, el.preco_referencial)                       AS preco_ref,
        ep.preco_customizado                                                                        AS preco_custom,
        COALESCE(ep.preco_customizado, vg.preco_venda, vg_market.preco_venda, el.preco_referencial) AS preco,
        el.custo_medio                                                                              AS custo,
        COALESCE(epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem
    FROM eligible el
    LEFT JOIN LATERAL (
        SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
        FROM vendageral
        WHERE cnpj = el.cnpjloja AND ean = el.barras
          AND total_vendasgeral > 0 AND itens > 0
        ORDER BY id DESC
        LIMIT 1
    ) vg ON TRUE
    LEFT JOIN LATERAL (
        SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
        FROM vendageral
        WHERE ean = el.barras
          AND total_vendasgeral > 0 AND itens > 0
        ORDER BY id DESC
        LIMIT 1
    ) vg_market ON TRUE
    LEFT JOIN medicamentos m          ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(COALESCE(el.ean_join, ''), '0')
    LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
    LEFT JOIN produto_canon pc        ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(COALESCE(el.ean_join, ''), '0') AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
    LEFT JOIN ecommerce_lab_ean elab  ON LTRIM(COALESCE(elab.ean, ''), '0') = LTRIM(COALESCE(el.ean_join, ''), '0')
    LEFT JOIN ecommerce_precos ep     ON ep.cnpjloja = el.cnpjloja AND ep.ean = el.barras
    LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = el.cnpjloja AND epi.ean = el.barras
    LEFT JOIN medicamentos5 m5        ON m5.barra = el.barras
"""

_IMAGEM_FILTER_AUTO = """AND (
            ae.ean IN (
                SELECT barra_norm FROM medicamentos
                WHERE barra_norm IS NOT NULL
                  AND (
                    NULLIF(TRIM(imagem), '') IS NOT NULL
                    OR id IN (SELECT medicamento_id FROM medicamentos_imagens
                              WHERE cloudinary_url IS NOT NULL)
                  )
            )
            OR ae.ean IN (
                SELECT ean FROM produto_canon
                WHERE imagem_cosmos IS NOT NULL AND TRIM(imagem_cosmos) <> ''
                  AND fonte NOT IN ('cosmos_miss', 'ia_miss')
            )
            OR EXISTS (
                SELECT 1
                FROM ecommerce_produto_imagens epi0
                WHERE epi0.cnpjloja = ae.cnpj_loja
                  AND LTRIM(COALESCE(epi0.ean, ''), '0') = LTRIM(COALESCE(ae.ean, ''), '0')
                  AND epi0.imagem_url IS NOT NULL
                  AND TRIM(epi0.imagem_url) <> ''
            )
            OR EXISTS (
                SELECT 1 FROM medicamentos5 m5x
                WHERE m5x.barra = ae.ean
                  AND NULLIF(TRIM(m5x.imagem), '') IS NOT NULL
            )
          )"""

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
          {imagem_filter}
        ORDER BY ae.descricao_produto
        LIMIT {limite}
    )
    SELECT
        el.ean,
        COALESCE(m.descricao, pc.descricao_canon, el.descricao_produto)           AS nome,
        COALESCE(elab.laboratorio, pc.laboratorio, m.laboratorio)                 AS laboratorio,
        m.marca                                                                   AS marca,
        el.qty,
        COALESCE(av.preco_venda, el.valor_final_produto)                          AS preco_ref,
        ep.preco_customizado                                                       AS preco_custom,
        COALESCE(ep.preco_customizado, av.preco_venda, el.valor_final_produto)    AS preco,
        el.custo,
        COALESCE(epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), ''))  AS imagem
    FROM eligible el
    LEFT JOIN LATERAL (
        SELECT ROUND(valor_final_vendido / NULLIF(quantidade_vendida, 0), 2) AS preco_venda
        FROM automatiza_vendas
        WHERE cnpj_loja = el.cnpjloja AND ean = el.ean
          AND valor_final_vendido > 0 AND quantidade_vendida > 0
        ORDER BY id DESC
        LIMIT 1
    ) av ON TRUE
    LEFT JOIN medicamentos m          ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(COALESCE(el.ean, ''), '0')
    LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
    LEFT JOIN produto_canon pc        ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(COALESCE(el.ean, ''), '0') AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
    LEFT JOIN ecommerce_lab_ean elab  ON LTRIM(COALESCE(elab.ean, ''), '0') = LTRIM(COALESCE(el.ean, ''), '0')
    LEFT JOIN ecommerce_precos ep     ON ep.cnpjloja = el.cnpjloja AND ep.ean = el.ean
    LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = el.cnpjloja AND epi.ean = el.ean
    LEFT JOIN medicamentos5 m5        ON m5.barra = el.ean
"""

_SQL_ALPHA_FAST = """
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
          {imagem_filter}
        ORDER BY e.descricao
        LIMIT {limite}
    )
    SELECT
        el.barras                                                            AS ean,
        COALESCE(m.descricao, pc.descricao_canon, el.descricao)             AS nome,
        COALESCE(elab.laboratorio, pc.laboratorio, m.laboratorio)            AS laboratorio,
        m.marca                                                              AS marca,
        el.qty,
        el.preco_referencial                                                  AS preco_ref,
        ep.preco_customizado                                                  AS preco_custom,
        COALESCE(ep.preco_customizado, el.preco_referencial)                 AS preco,
        el.custo_medio                                                        AS custo,
        COALESCE(epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem,
        'alpha'                                                               AS fonte_estoque
    FROM eligible el
    LEFT JOIN medicamentos m          ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(COALESCE(el.ean_join, ''), '0')
    LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
    LEFT JOIN produto_canon pc        ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(COALESCE(el.ean_join, ''), '0') AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
    LEFT JOIN ecommerce_lab_ean elab  ON LTRIM(COALESCE(elab.ean, ''), '0') = LTRIM(COALESCE(el.ean_join, ''), '0')
    LEFT JOIN ecommerce_precos ep     ON ep.cnpjloja = el.cnpjloja AND ep.ean = el.barras
    LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = el.cnpjloja AND epi.ean = el.barras
    LEFT JOIN medicamentos5 m5        ON m5.barra = el.barras
"""

_SQL_AUTO_FAST = """
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
          {imagem_filter}
        ORDER BY ae.descricao_produto
        LIMIT {limite}
    )
    SELECT
        el.ean,
        COALESCE(m.descricao, pc.descricao_canon, el.descricao_produto)           AS nome,
        COALESCE(elab.laboratorio, pc.laboratorio, m.laboratorio)                 AS laboratorio,
        m.marca                                                                   AS marca,
        el.qty,
        el.valor_final_produto                                                     AS preco_ref,
        ep.preco_customizado                                                       AS preco_custom,
        COALESCE(ep.preco_customizado, el.valor_final_produto)                    AS preco,
        el.custo,
        COALESCE(epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem,
        'auto'                                                                     AS fonte_estoque
    FROM eligible el
    LEFT JOIN medicamentos m          ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(COALESCE(el.ean, ''), '0')
    LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
    LEFT JOIN produto_canon pc        ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(COALESCE(el.ean, ''), '0') AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
    LEFT JOIN ecommerce_lab_ean elab  ON LTRIM(COALESCE(elab.ean, ''), '0') = LTRIM(COALESCE(el.ean, ''), '0')
    LEFT JOIN ecommerce_precos ep     ON ep.cnpjloja = el.cnpjloja AND ep.ean = el.ean
    LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = el.cnpjloja AND epi.ean = el.ean
    LEFT JOIN medicamentos5 m5        ON m5.barra = el.ean
"""


_SQL_ALPHA_A7 = """
    WITH eligible AS (
        SELECT
            ap.cnpjloja,
            ap.ean,
            ap.nome,
            CAST(ap.estoque AS INTEGER) AS qty,
            ap.preco_venda,
            ap.preco_atual,
            ap.fabricante,
            ap.principio_ativo,
            ap.imagem_url,
            ap.alpha_o_id
        FROM ecommerce_alpha_produtos ap
        WHERE ap.cnpjloja = %s
          AND COALESCE(ap.inativo, false) = false
          AND COALESCE(ap.estoque, 0) > 0
          AND COALESCE(ap.ean, '') <> ''
          {busca}
          {imagem_filter}
        ORDER BY ap.nome
        LIMIT {limite}
    )
    SELECT
        el.ean,
        COALESCE(m.descricao, pc.descricao_canon, el.nome) AS nome,
        COALESCE(elab.laboratorio, pc.laboratorio, m.laboratorio, el.fabricante) AS laboratorio,
        m.marca AS marca,
        el.qty,
        el.preco_venda AS preco_ref,
        NULL::numeric AS preco_custom,
        el.preco_venda AS preco,
        NULL::numeric AS custo,
        COALESCE(el.imagem_url, epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem,
        'alpha_a7' AS fonte_estoque,
        el.alpha_o_id
    FROM eligible el
    LEFT JOIN medicamentos m          ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(COALESCE(el.ean, ''), '0')
    LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
    LEFT JOIN produto_canon pc        ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(COALESCE(el.ean, ''), '0') AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
    LEFT JOIN ecommerce_lab_ean elab  ON LTRIM(COALESCE(elab.ean, ''), '0') = LTRIM(COALESCE(el.ean, ''), '0')
    LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = el.cnpjloja AND LTRIM(COALESCE(epi.ean, ''), '0') = LTRIM(COALESCE(el.ean, ''), '0')
    LEFT JOIN medicamentos5 m5        ON m5.barra = el.ean
"""


def _apply_latest_sales_prices(produtos, cnpjloja, cur):
    alpha_eans = [p["ean"] for p in produtos if p.get("ean") and p.get("fonte_estoque") == "alpha" and p.get("preco_custom") is None]
    auto_eans = [p["ean"] for p in produtos if p.get("ean") and p.get("fonte_estoque") == "auto" and p.get("preco_custom") is None]

    alpha_prices = {}
    if alpha_eans:
        cur.execute(
            """
            SELECT DISTINCT ON (ean)
                   ean, ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
            FROM vendageral
            WHERE cnpj = %s
              AND ean = ANY(%s)
              AND total_vendasgeral > 0
              AND itens > 0
            ORDER BY ean, id DESC
            """,
            (cnpjloja, alpha_eans),
        )
        alpha_prices = {r["ean"]: r["preco_venda"] for r in cur.fetchall() if r.get("preco_venda") is not None}

    auto_prices = {}
    if auto_eans:
        cur.execute(
            """
            SELECT DISTINCT ON (ean)
                   ean, ROUND(valor_final_vendido / NULLIF(quantidade_vendida, 0), 2) AS preco_venda
            FROM automatiza_vendas
            WHERE cnpj_loja = %s
              AND ean = ANY(%s)
              AND valor_final_vendido > 0
              AND quantidade_vendida > 0
            ORDER BY ean, id DESC
            """,
            (cnpjloja, auto_eans),
        )
        auto_prices = {r["ean"]: r["preco_venda"] for r in cur.fetchall() if r.get("preco_venda") is not None}

    # EANs sem preço local — buscar fallback em qualquer loja
    eans_sem_preco = [
        p["ean"] for p in produtos
        if p.get("preco_custom") is None
        and p.get("ean")
        and (alpha_prices.get(p["ean"]) is None and auto_prices.get(p["ean"]) is None)
    ]
    market_prices: dict = {}
    if eans_sem_preco:
        try:
            cur.execute(
                """
                SELECT DISTINCT ON (ean)
                       ean, ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
                FROM vendageral
                WHERE ean = ANY(%s)
                  AND total_vendasgeral > 0 AND itens > 0
                ORDER BY ean, id DESC
                """,
                (eans_sem_preco,),
            )
            market_prices = {r["ean"]: r["preco_venda"] for r in cur.fetchall() if r.get("preco_venda") is not None}
        except Exception:
            pass

    for produto in produtos:
        if produto.get("preco_custom") is not None:
            continue
        ean = produto.get("ean")
        preco_venda = alpha_prices.get(ean) if produto.get("fonte_estoque") == "alpha" else auto_prices.get(ean)
        if preco_venda is None:
            preco_venda = market_prices.get(ean)
        if preco_venda is not None:
            produto["preco_ref"] = preco_venda
            produto["preco"] = preco_venda


def get_dns_products(
    cnpjloja,
    q=None,
    include_hidden=False,
    skip_image_filter=False,
    schedule_fill=True,
    persist_image_updates=True,
    ensure_anvisa_schema=True,
    ensure_precificador_schema=True,
    batch_sales_prices=False,
    dedupe_display=True,
):
    conn = db()
    cur = conn.cursor()

    if ensure_precificador_schema:
        _ensure_precificador_schema()
    if _catalogo_alpha_exclusivo():
        _alpha_catalog_sync_if_needed(cur=cur, cnpjloja=cnpjloja)

    # EANs ocultos por esta loja. No catálogo Alpha/A7 exclusivo essa regra antiga
    # não se aplica: a publicação passa a ser controlada pelo próprio Alpha.
    if _catalogo_alpha_exclusivo():
        ocultos = set()
    else:
        cur.execute("SELECT ean FROM ecommerce_catalogo_oculto WHERE cnpjloja = %s", (cnpjloja,))
        ocultos = {r["ean"] for r in cur.fetchall()}

    busca_alpha = busca_auto = ""
    busca_alpha_a7 = ""
    args_alpha = [cnpjloja]
    args_alpha_a7 = [cnpjloja]
    args_auto  = [cnpjloja]
    if q:
        like = f"%{q.lower()}%"
        busca_alpha = "AND (LOWER(e.descricao) LIKE %s OR COALESCE(e.barras_norm, e.barras, '') LIKE %s)"
        busca_alpha_a7 = "AND (LOWER(ap.nome) LIKE %s OR COALESCE(ap.ean, '') LIKE %s)"
        busca_auto  = "AND (LOWER(ae.descricao_produto) LIKE %s OR COALESCE(ae.ean, '') LIKE %s)"
        args_alpha.extend([like, f"%{q}%"])
        args_alpha_a7.extend([like, f"%{q}%"])
        args_auto.extend([like, f"%{q}%"])

    if skip_image_filter:
        imagem_alpha_a7 = ""
        imagem_alpha = ""
        imagem_auto  = ""
        limite = 9999  # sem limite prático — mostra todos com estoque
    else:
        imagem_alpha_a7 = _IMAGEM_FILTER_ALPHA_A7
        imagem_alpha = _IMAGEM_FILTER_ALPHA
        imagem_auto  = _IMAGEM_FILTER_AUTO
        limite = 9999  # idem: todos com imagem e estoque

    sql_alpha = _SQL_ALPHA_FAST if batch_sales_prices else _SQL_ALPHA
    sql_auto = _SQL_AUTO_FAST if batch_sales_prices else _SQL_AUTO

    alpha_a7 = []
    if _alpha_enabled():
        try:
            _ensure_alpha_schema()
            cur.execute(_SQL_ALPHA_A7.format(busca=busca_alpha_a7, imagem_filter=imagem_alpha_a7, limite=limite), args_alpha_a7)
            alpha_a7 = cur.fetchall()
        except Exception as exc:
            app.logger.warning("catalogo alpha a7 indisponivel: %s", exc)

    alpha = []
    auto = []
    if not _catalogo_alpha_exclusivo():
        cur.execute(sql_alpha.format(busca=busca_alpha, imagem_filter=imagem_alpha, limite=limite), args_alpha)
        alpha = cur.fetchall()

        cur.execute(sql_auto.format(busca=busca_auto, imagem_filter=imagem_auto, limite=limite), args_auto)
        auto = cur.fetchall()

    seen, combined = set(), []
    for row in list(alpha_a7) + list(alpha) + list(auto):
        ean = (row["ean"] or "").strip()
        if ean not in seen and (include_hidden or ean not in ocultos):
            seen.add(ean)
            d = dict(row)
            d["is_extra"] = False
            d["oculto"] = ean in ocultos
            combined.append(d)

    if batch_sales_prices:
        _apply_latest_sales_prices(combined, cnpjloja, cur)

    # Produtos extras incluídos manualmente pela loja
    extra_eans = []
    if not _catalogo_alpha_exclusivo():
        cur.execute("SELECT ean FROM ecommerce_catalogo_extra WHERE cnpjloja = %s", (cnpjloja,))
        extra_eans = [r["ean"] for r in cur.fetchall() if r["ean"] not in seen]

    if extra_eans:
        cur.execute("""
            SELECT e.barras AS ean, e.descricao AS nome,
                   CAST(e.estoque AS INTEGER) AS qty,
                   COALESCE(vg.preco_venda, vg_market.preco_venda, e.preco_referencial) AS preco_ref,
                   ep.preco_customizado AS preco_custom,
                   COALESCE(ep.preco_customizado, vg.preco_venda, vg_market.preco_venda, e.preco_referencial) AS preco,
                   e.custo_medio AS custo,
                   COALESCE(pc.laboratorio, m.laboratorio) AS laboratorio,
                   m.marca AS marca,
                   COALESCE(epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem
            FROM estoque e
            LEFT JOIN LATERAL (
                SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
                FROM vendageral
                WHERE cnpj = e.cnpj AND ean = e.barras
                  AND total_vendasgeral > 0 AND itens > 0
                ORDER BY id DESC LIMIT 1
            ) vg ON TRUE
            LEFT JOIN LATERAL (
                SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
                FROM vendageral
                WHERE ean = e.barras
                  AND total_vendasgeral > 0 AND itens > 0
                ORDER BY id DESC LIMIT 1
            ) vg_market ON TRUE
            LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            LEFT JOIN produto_canon pc ON pc.ean = COALESCE(e.barras_norm, e.barras) AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
            LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = e.cnpj AND ep.ean = e.barras
            LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = e.cnpj AND epi.ean = e.barras
            LEFT JOIN medicamentos5 m5 ON m5.barra = e.barras
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
                       COALESCE(pc.laboratorio, m.laboratorio) AS laboratorio,
                       m.marca AS marca,
                       COALESCE(epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem
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
                LEFT JOIN produto_canon pc ON pc.ean = ae.ean AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
                LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = ae.cnpj_loja AND ep.ean = ae.ean
                LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = ae.cnpj_loja AND epi.ean = ae.ean
                LEFT JOIN medicamentos5 m5 ON m5.barra = ae.ean
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

    _apply_safe_catalog_images(combined, cur=cur, persist_placeholders=persist_image_updates)
    _marcar_tarja_batch(combined, cur.connection, ensure_schema=ensure_anvisa_schema)
    # Remove imagens de farmácias concorrentes que escaparam dos filtros anteriores
    for _p in combined:
        _img = (_p.get("imagem") or "").strip()
        if _img and _looks_like_other_pharmacy_brand(_img):
            if _is_alpha_product(_p):
                continue
            else:
                _p["imagem"] = _placeholder_for_tarja(_p.get("tarja")) or GENERIC_TARJA_VERMELHA_IMG
                _p["imagem_padrao_poupaqui"] = True
                _p["imagem_bloqueada_anvisa"] = True
    combined = (
        _dedupe_products_for_display(combined)
        if dedupe_display
        else _dedupe_products_by_store_ean(combined)
    )
    cur.close()
    if schedule_fill:
        _schedule_fill_images(combined, cnpjloja=cnpjloja)
    return sorted(combined, key=lambda x: (x.get("nome") or "").lower())


# ─── BATCH DNS PRODUCTS (home page) ──────────────────────────────────────────

_SQL_ALPHA_A7_BATCH = """
    WITH eligible AS (
        SELECT
            ap.cnpjloja,
            ap.ean,
            ap.nome,
            CAST(ap.estoque AS INTEGER) AS qty,
            ap.preco_venda,
            ap.preco_atual,
            ap.fabricante,
            ap.imagem_url
        FROM ecommerce_alpha_produtos ap
        WHERE ap.cnpjloja = ANY(%s)
          AND COALESCE(ap.inativo, false) = false
          AND COALESCE(ap.estoque, 0) > 0
          AND COALESCE(ap.ean, '') <> ''
          AND (
            ap.imagem_url IS NOT NULL
            OR ap.ean IN (
                SELECT barra_norm FROM medicamentos
                WHERE barra_norm IS NOT NULL
                  AND (
                    NULLIF(TRIM(imagem), '') IS NOT NULL
                    OR id IN (SELECT medicamento_id FROM medicamentos_imagens
                              WHERE cloudinary_url IS NOT NULL)
                  )
            )
            OR ap.ean IN (
                SELECT ean FROM produto_canon
                WHERE imagem_cosmos IS NOT NULL AND TRIM(imagem_cosmos) <> ''
                  AND fonte NOT IN ('cosmos_miss', 'ia_miss')
            )
            OR EXISTS (
                SELECT 1
                FROM ecommerce_produto_imagens epi0
                WHERE epi0.cnpjloja = ap.cnpjloja
                  AND LTRIM(COALESCE(epi0.ean, ''), '0') = LTRIM(COALESCE(ap.ean, ''), '0')
                  AND epi0.imagem_url IS NOT NULL
                  AND TRIM(epi0.imagem_url) <> ''
            )
            OR EXISTS (
                SELECT 1 FROM medicamentos5 m5x
                WHERE m5x.barra = ap.ean
                  AND NULLIF(TRIM(m5x.imagem), '') IS NOT NULL
            )
          )
        ORDER BY ap.nome
        LIMIT 9999
    )
    SELECT
        el.cnpjloja,
        el.ean,
        COALESCE(m.descricao, pc.descricao_canon, el.nome) AS nome,
        COALESCE(elab.laboratorio, pc.laboratorio, m.laboratorio, el.fabricante) AS laboratorio,
        m.marca AS marca,
        COALESCE(m.tipo_ia, pc.categoria) AS categoria,
        el.qty,
        el.preco_venda AS preco,
        COALESCE(el.imagem_url, epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem,
        'alpha_a7' AS fonte_estoque
    FROM eligible el
    LEFT JOIN medicamentos m          ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(COALESCE(el.ean, ''), '0')
    LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
    LEFT JOIN produto_canon pc        ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(COALESCE(el.ean, ''), '0') AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
    LEFT JOIN ecommerce_lab_ean elab  ON LTRIM(COALESCE(elab.ean,''),'0') = LTRIM(COALESCE(el.ean,''),'0')
    LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = el.cnpjloja AND LTRIM(COALESCE(epi.ean, ''), '0') = LTRIM(COALESCE(el.ean, ''), '0')
    LEFT JOIN medicamentos5 m5        ON m5.barra = el.ean
"""

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
        LEFT JOIN ecommerce_config_loja cfg_e ON cfg_e.cnpjloja = e.cnpj
        WHERE e.cnpj = ANY(%s)
          AND CAST(e.estoque AS INTEGER) >= COALESCE(cfg_e.estoque_min_publicacao, 1)
          AND (
            COALESCE(e.barras_norm, e.barras) IN (
                SELECT barra_norm FROM medicamentos
                WHERE barra_norm IS NOT NULL
                  AND (
                    NULLIF(TRIM(imagem), '') IS NOT NULL
                    OR id IN (SELECT medicamento_id FROM medicamentos_imagens
                              WHERE cloudinary_url IS NOT NULL)
                  )
            )
            OR COALESCE(e.barras_norm, e.barras) IN (
                SELECT ean FROM produto_canon
                WHERE imagem_cosmos IS NOT NULL AND TRIM(imagem_cosmos) <> ''
                  AND fonte NOT IN ('cosmos_miss', 'ia_miss')
            )
            OR EXISTS (
                SELECT 1
                FROM ecommerce_produto_imagens epi0
                WHERE epi0.cnpjloja = e.cnpj
                  AND LTRIM(COALESCE(epi0.ean, ''), '0') = LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0')
                  AND epi0.imagem_url IS NOT NULL
                  AND TRIM(epi0.imagem_url) <> ''
            )
            OR EXISTS (
                SELECT 1 FROM medicamentos5 m5x
                WHERE m5x.barra = e.barras
                  AND NULLIF(TRIM(m5x.imagem), '') IS NOT NULL
            )
          )
        ORDER BY e.descricao
        LIMIT 9999
    ),
    vg_precos AS (
        SELECT DISTINCT ON (cnpj, ean)
               cnpj, ean,
               ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
        FROM vendageral
        WHERE cnpj = ANY(%s) AND total_vendasgeral > 0 AND itens > 0
        ORDER BY cnpj, ean, id DESC
    )
    SELECT
        el.cnpjloja,
        el.barras                                                             AS ean,
        COALESCE(m.descricao, pc.descricao_canon, el.descricao)              AS nome,
        COALESCE(elab.laboratorio, pc.laboratorio, m.laboratorio)            AS laboratorio,
        m.marca                                                              AS marca,
        COALESCE(m.tipo_ia, pc.categoria)  AS categoria,
        el.qty,
        COALESCE(ep.preco_customizado, vg.preco_venda, el.preco_referencial) AS preco,
        COALESCE(epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem
    FROM eligible el
    LEFT JOIN vg_precos vg             ON vg.cnpj = el.cnpjloja AND vg.ean = el.barras
    LEFT JOIN medicamentos m          ON m.barra_norm = el.ean_join
    LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
    LEFT JOIN produto_canon pc        ON pc.ean = el.ean_join AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
    LEFT JOIN ecommerce_lab_ean elab  ON LTRIM(COALESCE(elab.ean,''),'0') = LTRIM(COALESCE(el.ean_join,''),'0')
    LEFT JOIN ecommerce_precos ep     ON ep.cnpjloja = el.cnpjloja AND ep.ean = el.barras
    LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = el.cnpjloja AND epi.ean = el.barras
    LEFT JOIN medicamentos5 m5        ON m5.barra = el.barras
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
        LEFT JOIN ecommerce_config_loja cfg_ae ON cfg_ae.cnpjloja = ae.cnpj_loja
        WHERE ae.cnpj_loja = ANY(%s)
          AND CAST(ae.quantidade_estoque AS INTEGER) >= COALESCE(cfg_ae.estoque_min_publicacao, 1)
          AND (
            ae.ean IN (
                SELECT barra_norm FROM medicamentos
                WHERE barra_norm IS NOT NULL
                  AND (
                    NULLIF(TRIM(imagem), '') IS NOT NULL
                    OR id IN (SELECT medicamento_id FROM medicamentos_imagens
                              WHERE cloudinary_url IS NOT NULL)
                  )
            )
            OR ae.ean IN (
                SELECT ean FROM produto_canon
                WHERE imagem_cosmos IS NOT NULL AND TRIM(imagem_cosmos) <> ''
                  AND fonte NOT IN ('cosmos_miss', 'ia_miss')
            )
            OR EXISTS (
                SELECT 1
                FROM ecommerce_produto_imagens epi0
                WHERE epi0.cnpjloja = ae.cnpj_loja
                  AND LTRIM(COALESCE(epi0.ean, ''), '0') = LTRIM(COALESCE(ae.ean, ''), '0')
                  AND epi0.imagem_url IS NOT NULL
                  AND TRIM(epi0.imagem_url) <> ''
            )
            OR EXISTS (
                SELECT 1 FROM medicamentos5 m5x
                WHERE m5x.barra = ae.ean
                  AND NULLIF(TRIM(m5x.imagem), '') IS NOT NULL
            )
          )
        ORDER BY ae.descricao_produto
        LIMIT 9999
    )
    SELECT
        el.cnpjloja,
        el.ean,
        COALESCE(m.descricao, pc.descricao_canon, el.descricao)                   AS nome,
        COALESCE(elab.laboratorio, pc.laboratorio, m.laboratorio)               AS laboratorio,
        m.marca                                                                   AS marca,
        COALESCE(m.tipo_ia, pc.categoria)        AS categoria,
        el.qty,
        COALESCE(ep.preco_customizado, el.valor_final_produto)                    AS preco,
        COALESCE(epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), ''))  AS imagem
    FROM eligible el
    LEFT JOIN medicamentos m          ON m.barra_norm = el.ean
    LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
    LEFT JOIN produto_canon pc        ON pc.ean = el.ean AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
    LEFT JOIN ecommerce_lab_ean elab  ON LTRIM(COALESCE(elab.ean,''),'0') = LTRIM(COALESCE(el.ean,''),'0')
    LEFT JOIN ecommerce_precos ep     ON ep.cnpjloja = el.cnpjloja AND ep.ean = el.ean
    LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = el.cnpjloja AND epi.ean = el.ean
    LEFT JOIN medicamentos5 m5        ON m5.barra = el.ean
"""


def _apply_saved_categories(produtos, cur=None):
    """Reaproveita as categorias ja definidas em ecommerce_classificacao_ean.

    O app nunca lia essa tabela — a categoria vinha so de medicamentos.tipo_ia /
    produto_canon. Produtos do fluxo novo (Alpha) que nao estao em medicamentos
    ficavam sem categoria. Aqui, quando existe uma classificacao salva para o EAN,
    ela prevalece (mesmo vocabulario de tipo_ia: generico, similar, suplemento,
    perfumaria, nutricao, varejo, dermocosmetico, etc.)."""
    if not produtos:
        return produtos
    por_key: dict = {}
    for p in produtos:
        ean = (p.get("ean") or "").strip()
        key = ean.lstrip("0") or ean
        if key:
            por_key.setdefault(key, []).append(p)
    if not por_key:
        return produtos
    own = cur is None
    try:
        if own:
            cur = db().cursor()
        cur.execute(
            "SELECT ean, tipo FROM ecommerce_classificacao_ean "
            "WHERE ean = ANY(%s) AND COALESCE(tipo, '') <> ''",
            (list(por_key.keys()),),
        )
        for r in cur.fetchall():
            for p in por_key.get(r["ean"], []):
                p["categoria"] = r["tipo"]
    except Exception as exc:
        app.logger.warning("apply saved categories error: %s", exc)
    finally:
        if own and cur:
            try:
                cur.close()
            except Exception:
                pass
    return produtos


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
    if _catalogo_alpha_exclusivo():
        _alpha_catalog_sync_if_needed(cur=cur)
    alpha_a7 = []
    if _alpha_enabled():
        try:
            _ensure_alpha_schema()
            cur.execute(_SQL_ALPHA_A7_BATCH, (cnpjs,))
            alpha_a7 = cur.fetchall()
        except Exception as exc:
            app.logger.warning("batch catalogo alpha a7 indisponivel: %s", exc)
    alpha = []
    auto = []
    if not _catalogo_alpha_exclusivo():
        cur.execute(_SQL_ALPHA_BATCH, (cnpjs, cnpjs))
        alpha = cur.fetchall()
        cur.execute(_SQL_AUTO_BATCH, (cnpjs,))
        auto = cur.fetchall()

    # Ocultos por loja. Ignorado no modo Alpha/A7 exclusivo.
    if _catalogo_alpha_exclusivo():
        ocultos = set()
    else:
        cur.execute(
            "SELECT cnpjloja, ean FROM ecommerce_catalogo_oculto WHERE cnpjloja = ANY(%s)",
            (cnpjs,),
        )
        ocultos = {(r["cnpjloja"], r["ean"]) for r in cur.fetchall()}

    # Extras adicionados manualmente pelas lojas
    extra_pairs = []
    if not _catalogo_alpha_exclusivo():
        cur.execute(
            "SELECT cnpjloja, ean FROM ecommerce_catalogo_extra WHERE cnpjloja = ANY(%s)",
            (cnpjs,),
        )
        extra_pairs = [(r["cnpjloja"], r["ean"]) for r in cur.fetchall()]

    seen, combined = set(), []
    for row in list(alpha_a7) + list(alpha) + list(auto):
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
                       COALESCE(pc.laboratorio, m.laboratorio) AS laboratorio,
                       m.marca AS marca,
                       COALESCE(m.tipo_ia, pc.categoria) AS categoria,
                       CAST(e.estoque AS INTEGER) AS qty,
                       COALESCE(ep.preco_customizado, vg.preco_venda, vg_market.preco_venda, e.preco_referencial) AS preco,
                       COALESCE(epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem
                FROM estoque e
                LEFT JOIN LATERAL (
                    SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
                    FROM vendageral
                    WHERE cnpj = e.cnpj AND ean = e.barras
                      AND total_vendasgeral > 0 AND itens > 0
                    ORDER BY id DESC LIMIT 1
                ) vg ON TRUE
                LEFT JOIN LATERAL (
                    SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
                    FROM vendageral
                    WHERE ean = e.barras
                      AND total_vendasgeral > 0 AND itens > 0
                    ORDER BY id DESC LIMIT 1
                ) vg_market ON TRUE
                LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
                LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
                LEFT JOIN produto_canon pc ON pc.ean = COALESCE(e.barras_norm, e.barras) AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
                LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = e.cnpj AND ep.ean = e.barras
                LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = e.cnpj AND epi.ean = e.barras
                LEFT JOIN medicamentos5 m5 ON m5.barra = e.barras
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
                       COALESCE(pc.laboratorio, m.laboratorio) AS laboratorio,
                       m.marca AS marca,
                       COALESCE(m.tipo_ia, pc.categoria) AS categoria,
                       CAST(ae.quantidade_estoque AS INTEGER) AS qty,
                       COALESCE(ep.preco_customizado, av.preco_venda, ae.valor_final_produto) AS preco,
                       COALESCE(epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem
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
                LEFT JOIN produto_canon pc ON pc.ean = ae.ean AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
                LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = ae.cnpj_loja AND ep.ean = ae.ean
                LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = ae.cnpj_loja AND epi.ean = ae.ean
                LEFT JOIN medicamentos5 m5 ON m5.barra = ae.ean
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
    _marcar_tarja_batch(combined, conn)
    for _p in combined:
        _img = (_p.get("imagem") or "").strip()
        if _img and _looks_like_other_pharmacy_brand(_img):
            if _is_alpha_product(_p):
                continue
            else:
                _p["imagem"] = _placeholder_for_tarja(_p.get("tarja")) or GENERIC_TARJA_VERMELHA_IMG
                _p["imagem_padrao_poupaqui"] = True
                _p["imagem_bloqueada_anvisa"] = True
    _apply_saved_categories(combined, cur)
    cur.close()
    try:
        conn.close()
    except Exception:
        pass
    _batch_cache_set(cache_key, combined)
    _schedule_fill_images(combined)
    return combined


def get_dns_products_batch_by_eans(cnpjs, eans):
    if not cnpjs or not eans:
        return []
    _ensure_catalog_admin_schema()
    _ensure_precificador_schema()
    _ensure_produto_canon_schema()
    _ensure_alpha_schema()
    ean_keys = sorted({_digits(e).lstrip("0") for e in eans if _digits(e)})
    if not ean_keys:
        return []
    conn = _new_conn_batch()
    cur = conn.cursor()
    if _catalogo_alpha_exclusivo():
        _alpha_catalog_sync_if_needed(cur=cur)
    cur.execute(
        """
        WITH alvo AS (SELECT unnest(%s::text[]) AS ean_key),
        alpha_a7 AS (
            SELECT ap.cnpjloja,
                   ap.ean AS ean,
                   ap.ean AS ean_join,
                   ap.nome AS nome_raw,
                   CAST(ap.estoque AS INTEGER) AS qty,
                   ap.preco_venda AS preco_base,
                   'alpha_a7' AS fonte_estoque
            FROM ecommerce_alpha_produtos ap
            JOIN alvo a ON LTRIM(COALESCE(ap.ean, ''), '0') = a.ean_key
            WHERE ap.cnpjloja = ANY(%s)
              AND COALESCE(ap.inativo, false) = false
              AND COALESCE(ap.estoque, 0) > 0
        ),
        alpha AS (
            SELECT e.cnpj AS cnpjloja,
                   e.barras AS ean,
                   COALESCE(e.barras_norm, e.barras) AS ean_join,
                   e.descricao AS nome_raw,
                   CAST(e.estoque AS INTEGER) AS qty,
                   e.preco_referencial AS preco_base,
                   'alpha' AS fonte_estoque
            FROM estoque e
            JOIN alvo a ON LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0') = a.ean_key
            WHERE e.cnpj = ANY(%s) AND e.estoque > 0
        ),
        auto AS (
            SELECT ae.cnpj_loja AS cnpjloja,
                   ae.ean AS ean,
                   ae.ean AS ean_join,
                   ae.descricao_produto AS nome_raw,
                   CAST(ae.quantidade_estoque AS INTEGER) AS qty,
                   ae.valor_final_produto AS preco_base,
                   'auto' AS fonte_estoque
            FROM automatiza_estoque ae
            JOIN alvo a ON LTRIM(COALESCE(ae.ean, ''), '0') = a.ean_key
            WHERE ae.cnpj_loja = ANY(%s) AND ae.quantidade_estoque > 0
        ),
        base AS (
            SELECT * FROM alpha_a7
            UNION ALL
            SELECT * FROM alpha
            UNION ALL
            SELECT * FROM auto
        )
        SELECT
            b.cnpjloja,
            b.ean,
            COALESCE(m.descricao, pc.descricao_canon, b.nome_raw) AS nome,
            COALESCE(pc.laboratorio, m.laboratorio) AS laboratorio,
            m.marca AS marca,
            COALESCE(m.tipo_ia, pc.categoria) AS categoria,
            b.qty,
            CASE WHEN b.fonte_estoque = 'alpha_a7' THEN b.preco_base ELSE COALESCE(ep.preco_customizado, vg.preco_venda, av.preco_venda, b.preco_base) END AS preco,
            COALESCE(apimg.imagem_url, epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem,
            b.fonte_estoque
        FROM base b
        LEFT JOIN medicamentos m ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(COALESCE(b.ean_join, b.ean, ''), '0')
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        LEFT JOIN produto_canon pc ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(COALESCE(b.ean_join, b.ean, ''), '0') AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
        LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = b.cnpjloja AND ep.ean = b.ean
        LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = b.cnpjloja AND epi.ean = b.ean
        LEFT JOIN medicamentos5 m5 ON m5.barra = b.ean
        LEFT JOIN ecommerce_alpha_produtos apimg ON apimg.cnpjloja = b.cnpjloja AND LTRIM(COALESCE(apimg.ean, ''), '0') = LTRIM(COALESCE(b.ean_join, b.ean, ''), '0')
        LEFT JOIN LATERAL (
            SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
            FROM vendageral
            WHERE cnpj = b.cnpjloja AND ean = b.ean
              AND total_vendasgeral > 0 AND itens > 0
            ORDER BY total_vendasgeral DESC LIMIT 1
        ) vg ON TRUE
        LEFT JOIN LATERAL (
            SELECT ROUND(valor_final_vendido / NULLIF(quantidade_vendida, 0), 2) AS preco_venda
            FROM automatiza_vendas
            WHERE cnpj_loja = b.cnpjloja AND ean = b.ean
              AND valor_final_vendido > 0 AND quantidade_vendida > 0
            ORDER BY valor_final_vendido DESC LIMIT 1
        ) av ON TRUE
        """,
        (ean_keys, cnpjs, cnpjs, cnpjs),
    )
    rows = [dict(r) for r in cur.fetchall()]
    if _catalogo_alpha_exclusivo():
        rows = [r for r in rows if r.get("fonte_estoque") == "alpha_a7"]

    # Filtra itens ocultos apenas no catálogo legado; no Alpha/A7 exclusivo
    # a publicação é controlada no ERP.
    if not _catalogo_alpha_exclusivo():
        try:
            cur.execute(
                "SELECT cnpjloja, ean FROM ecommerce_catalogo_oculto WHERE cnpjloja = ANY(%s)",
                (cnpjs,),
            )
            ocultos_by = {(r["cnpjloja"], r["ean"]) for r in cur.fetchall()}
            if ocultos_by:
                rows = [r for r in rows if (r.get("cnpjloja"), r.get("ean")) not in ocultos_by]
        except Exception:
            pass

    cur.close()
    try:
        conn.close()
    except Exception:
        pass
    _apply_safe_catalog_images(rows)
    try:
        conn2 = _new_conn()
        _marcar_tarja_batch(rows, conn2)
        conn2.close()
    except Exception:
        pass
    # Remove produtos sem imagem: não devem aparecer no catálogo público
    # (depois de _marcar_tarja_batch para que tarjados recebam placeholder antes de filtrar)
    if _catalogo_alpha_exclusivo():
        rows = [r for r in rows if r.get("fonte_estoque") == "alpha_a7" and _has_catalog_image(r)]
    else:
        rows = [r for r in rows if _has_catalog_image(r)]
    for _p in rows:
        _img = (_p.get("imagem") or "").strip()
        if _img and _looks_like_other_pharmacy_brand(_img):
            if _is_alpha_product(_p):
                continue
            else:
                _p["imagem"] = _placeholder_for_tarja(_p.get("tarja")) or GENERIC_TARJA_VERMELHA_IMG
                _p["imagem_padrao_poupaqui"] = True
                _p["imagem_bloqueada_anvisa"] = True
    _attach_product_promos(rows)
    _schedule_fill_images(rows)
    return _dedupe_products_for_display(rows)


def get_dns_products_batch_by_name(cnpjs, terms, limit=400):
    """Batch name search — finds products matching any term without loading full catalog."""
    if not cnpjs or not terms:
        return []
    _ensure_catalog_admin_schema()
    _ensure_precificador_schema()
    _ensure_produto_canon_schema()
    _ensure_alpha_schema()
    patterns = [f"%{t.lower()}%" for t in terms[:4] if t]
    if not patterns:
        return []
    conn = _new_conn_batch()
    cur = conn.cursor()
    if _catalogo_alpha_exclusivo():
        _alpha_catalog_sync_if_needed(cur=cur)
    cur.execute(
        """
        WITH alpha_a7 AS (
            SELECT ap.cnpjloja,
                   ap.ean AS ean,
                   ap.ean AS ean_join,
                   ap.nome AS nome_raw,
                   CAST(ap.estoque AS INTEGER) AS qty,
                   ap.preco_venda AS preco_base,
                   'alpha_a7' AS fonte_estoque
            FROM ecommerce_alpha_produtos ap
            WHERE ap.cnpjloja = ANY(%s)
              AND COALESCE(ap.inativo, false) = false
              AND COALESCE(ap.estoque, 0) > 0
              AND (
                LOWER(ap.nome) LIKE ANY(%s)
                OR COALESCE(ap.ean,'') IN (
                    SELECT barra_norm FROM medicamentos
                    WHERE barra_norm IS NOT NULL
                      AND (LOWER(descricao) LIKE ANY(%s) OR LOWER(COALESCE(marca,'')) LIKE ANY(%s))
                )
                OR COALESCE(ap.ean,'') IN (
                    SELECT ean FROM produto_canon
                    WHERE ean IS NOT NULL AND fonte NOT IN ('cosmos_miss','ia_miss','placeholder_broken')
                      AND LOWER(descricao_canon) LIKE ANY(%s)
                )
              )
            LIMIT %s
        ),
        alpha AS (
            SELECT e.cnpj AS cnpjloja,
                   e.barras AS ean,
                   COALESCE(e.barras_norm, e.barras) AS ean_join,
                   e.descricao AS nome_raw,
                   CAST(e.estoque AS INTEGER) AS qty,
                   e.preco_referencial AS preco_base,
                   'alpha' AS fonte_estoque
            FROM estoque e
            WHERE e.cnpj = ANY(%s) AND e.estoque > 0
              AND (
                LOWER(e.descricao) LIKE ANY(%s)
                OR COALESCE(e.barras_norm, e.barras) IN (
                    SELECT barra_norm FROM medicamentos
                    WHERE barra_norm IS NOT NULL
                      AND (LOWER(descricao) LIKE ANY(%s) OR LOWER(COALESCE(marca,'')) LIKE ANY(%s))
                )
                OR COALESCE(e.barras_norm, e.barras) IN (
                    SELECT ean FROM produto_canon
                    WHERE ean IS NOT NULL AND fonte NOT IN ('cosmos_miss','ia_miss','placeholder_broken')
                      AND LOWER(descricao_canon) LIKE ANY(%s)
                )
              )
            LIMIT %s
        ),
        auto AS (
            SELECT ae.cnpj_loja AS cnpjloja,
                   ae.ean AS ean,
                   ae.ean AS ean_join,
                   ae.descricao_produto AS nome_raw,
                   CAST(ae.quantidade_estoque AS INTEGER) AS qty,
                   ae.valor_final_produto AS preco_base,
                   'auto' AS fonte_estoque
            FROM automatiza_estoque ae
            WHERE ae.cnpj_loja = ANY(%s) AND ae.quantidade_estoque > 0
              AND (
                LOWER(ae.descricao_produto) LIKE ANY(%s)
                OR COALESCE(ae.ean,'') IN (
                    SELECT barra_norm FROM medicamentos
                    WHERE barra_norm IS NOT NULL
                      AND (LOWER(descricao) LIKE ANY(%s) OR LOWER(COALESCE(marca,'')) LIKE ANY(%s))
                )
                OR COALESCE(ae.ean,'') IN (
                    SELECT ean FROM produto_canon
                    WHERE ean IS NOT NULL AND fonte NOT IN ('cosmos_miss','ia_miss','placeholder_broken')
                      AND LOWER(descricao_canon) LIKE ANY(%s)
                )
              )
            LIMIT %s
        ),
        base AS (
            SELECT * FROM alpha_a7
            UNION ALL
            SELECT * FROM alpha
            UNION ALL
            SELECT * FROM auto
        )
        SELECT
            b.cnpjloja,
            b.ean,
            COALESCE(m.descricao, pc.descricao_canon, b.nome_raw) AS nome,
            COALESCE(pc.laboratorio, m.laboratorio) AS laboratorio,
            m.marca AS marca,
            COALESCE(m.tipo_ia, pc.categoria) AS categoria,
            b.qty,
            CASE WHEN b.fonte_estoque = 'alpha_a7' THEN b.preco_base ELSE COALESCE(ep.preco_customizado, vg.preco_venda, av.preco_venda, b.preco_base) END AS preco,
            COALESCE(apimg.imagem_url, epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem,
            b.fonte_estoque
        FROM base b
        LEFT JOIN medicamentos m           ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(COALESCE(b.ean_join, b.ean, ''), '0')
        LEFT JOIN medicamentos_imagens mi  ON mi.medicamento_id = m.id
        LEFT JOIN produto_canon pc         ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(COALESCE(b.ean_join, b.ean, ''), '0')
                                          AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
        LEFT JOIN ecommerce_precos ep      ON ep.cnpjloja = b.cnpjloja AND ep.ean = b.ean
        LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = b.cnpjloja AND epi.ean = b.ean
        LEFT JOIN medicamentos5 m5 ON m5.barra = b.ean
        LEFT JOIN ecommerce_alpha_produtos apimg ON apimg.cnpjloja = b.cnpjloja AND LTRIM(COALESCE(apimg.ean, ''), '0') = LTRIM(COALESCE(b.ean_join, b.ean, ''), '0')
        LEFT JOIN LATERAL (
            SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
            FROM vendageral
            WHERE cnpj = b.cnpjloja AND ean = b.ean
              AND total_vendasgeral > 0 AND itens > 0
            ORDER BY total_vendasgeral DESC LIMIT 1
        ) vg ON TRUE
        LEFT JOIN LATERAL (
            SELECT ROUND(valor_final_vendido / NULLIF(quantidade_vendida, 0), 2) AS preco_venda
            FROM automatiza_vendas
            WHERE cnpj_loja = b.cnpjloja AND ean = b.ean
              AND valor_final_vendido > 0 AND quantidade_vendida > 0
            ORDER BY valor_final_vendido DESC LIMIT 1
        ) av ON TRUE
        """,
        (cnpjs, patterns, patterns, patterns, patterns, limit,
         cnpjs, patterns, patterns, patterns, patterns, limit,
         cnpjs, patterns, patterns, patterns, patterns, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    if _catalogo_alpha_exclusivo():
        rows = [r for r in rows if r.get("fonte_estoque") == "alpha_a7"]
    if not _catalogo_alpha_exclusivo():
        try:
            cur.execute(
                "SELECT cnpjloja, ean FROM ecommerce_catalogo_oculto WHERE cnpjloja = ANY(%s)",
                (cnpjs,),
            )
            ocultos_by = {(r["cnpjloja"], r["ean"]) for r in cur.fetchall()}
            if ocultos_by:
                rows = [r for r in rows if (r.get("cnpjloja"), r.get("ean")) not in ocultos_by]
        except Exception:
            pass
    cur.close()
    try:
        conn.close()
    except Exception:
        pass
    _apply_safe_catalog_images(rows)
    try:
        conn2 = _new_conn()
        _marcar_tarja_batch(rows, conn2)
        conn2.close()
    except Exception:
        pass
    if _catalogo_alpha_exclusivo():
        rows = [r for r in rows if r.get("fonte_estoque") == "alpha_a7" and _has_catalog_image(r)]
    else:
        rows = [r for r in rows if _has_catalog_image(r)]
    for _p in rows:
        _img = (_p.get("imagem") or "").strip()
        if _img and _looks_like_other_pharmacy_brand(_img):
            if _is_alpha_product(_p):
                continue
            else:
                _p["imagem"] = _placeholder_for_tarja(_p.get("tarja")) or GENERIC_TARJA_VERMELHA_IMG
                _p["imagem_padrao_poupaqui"] = True
                _p["imagem_bloqueada_anvisa"] = True
    _attach_product_promos(rows)
    _schedule_fill_images(rows)
    return _dedupe_products_for_display(rows)


def get_alpha_products_direct_by_query(cnpjs, query, limit=120):
    if not cnpjs or not query or not _catalogo_alpha_exclusivo():
        return []
    q_norm = _norm_text(query)
    patterns = []
    if q_norm:
        patterns.append(f"%{q_norm}%")
    q_raw = (query or "").strip().lower()
    if q_raw and f"%{q_raw}%" not in patterns:
        patterns.append(f"%{q_raw}%")
    ean_q = _digits(query)
    if not patterns and not ean_q:
        return []
    conn = _new_conn_batch()
    cur = conn.cursor()
    _alpha_catalog_sync_if_needed(cur=cur)
    cur.execute(
        """
        SELECT
            ap.cnpjloja,
            ap.ean,
            COALESCE(m.descricao, pc.descricao_canon, ap.nome) AS nome,
            COALESCE(elab.laboratorio, pc.laboratorio, m.laboratorio, ap.fabricante) AS laboratorio,
            m.marca AS marca,
            COALESCE(m.tipo_ia, pc.categoria) AS categoria,
            CAST(ap.estoque AS INTEGER) AS qty,
            ap.preco_venda AS preco,
            COALESCE(ap.imagem_url, epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem,
            'alpha_a7' AS fonte_estoque
        FROM ecommerce_alpha_produtos ap
        LEFT JOIN medicamentos m          ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(COALESCE(ap.ean, ''), '0')
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        LEFT JOIN produto_canon pc        ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(COALESCE(ap.ean, ''), '0') AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
        LEFT JOIN ecommerce_lab_ean elab  ON LTRIM(COALESCE(elab.ean,''),'0') = LTRIM(COALESCE(ap.ean,''),'0')
        LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = ap.cnpjloja AND LTRIM(COALESCE(epi.ean, ''), '0') = LTRIM(COALESCE(ap.ean, ''), '0')
        LEFT JOIN medicamentos5 m5        ON m5.barra = ap.ean
        WHERE ap.cnpjloja = ANY(%s)
          AND COALESCE(ap.inativo, false) = false
          AND COALESCE(ap.estoque, 0) > 0
          AND (
            LOWER(ap.nome) LIKE ANY(%s)
            OR COALESCE(ap.ean, '') LIKE %s
          )
        ORDER BY ap.nome
        LIMIT %s
        """,
        (cnpjs, patterns or ["__sem_match__"], f"%{ean_q}%" if ean_q else "__sem_match__", int(limit)),
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    try:
        conn.close()
    except Exception:
        pass
    _apply_safe_catalog_images(rows)
    try:
        conn2 = _new_conn()
        _marcar_tarja_batch(rows, conn2)
        conn2.close()
    except Exception:
        pass
    _apply_saved_categories(rows)
    _schedule_fill_images(rows)
    return _dedupe_products_for_display(rows)


def get_alpha_products_direct(cnpjs, limit=200):
    if not cnpjs or not _catalogo_alpha_exclusivo():
        return []
    conn = _new_conn_batch()
    cur = conn.cursor()
    _alpha_catalog_sync_if_needed(cur=cur)
    cur.execute(
        """
        SELECT
            ap.cnpjloja,
            ap.ean,
            COALESCE(m.descricao, pc.descricao_canon, ap.nome) AS nome,
            COALESCE(elab.laboratorio, pc.laboratorio, m.laboratorio, ap.fabricante) AS laboratorio,
            m.marca AS marca,
            COALESCE(m.tipo_ia, pc.categoria) AS categoria,
            CAST(ap.estoque AS INTEGER) AS qty,
            ap.preco_venda AS preco,
            COALESCE(ap.imagem_url, epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem,
            'alpha_a7' AS fonte_estoque
        FROM ecommerce_alpha_produtos ap
        LEFT JOIN medicamentos m          ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(COALESCE(ap.ean, ''), '0')
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        LEFT JOIN produto_canon pc        ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(COALESCE(ap.ean, ''), '0') AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
        LEFT JOIN ecommerce_lab_ean elab  ON LTRIM(COALESCE(elab.ean,''),'0') = LTRIM(COALESCE(ap.ean,''),'0')
        LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = ap.cnpjloja AND LTRIM(COALESCE(epi.ean, ''), '0') = LTRIM(COALESCE(ap.ean, ''), '0')
        LEFT JOIN medicamentos5 m5        ON m5.barra = ap.ean
        WHERE ap.cnpjloja = ANY(%s)
          AND COALESCE(ap.inativo, false) = false
          AND COALESCE(ap.estoque, 0) > 0
          AND COALESCE(ap.ean, '') <> ''
        ORDER BY ap.nome
        LIMIT %s
        """,
        (cnpjs, int(limit)),
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    try:
        conn.close()
    except Exception:
        pass
    _apply_safe_catalog_images(rows)
    try:
        conn2 = _new_conn()
        _marcar_tarja_batch(rows, conn2)
        conn2.close()
    except Exception:
        pass
    _apply_saved_categories(rows)
    rows = [r for r in rows if _has_catalog_image(r)]
    _schedule_fill_images(rows)
    return _dedupe_products_for_display(rows)


# ─── AUTH ─────────────────────────────────────────────────────────────────────

def painel_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("painel_ok"):
            return redirect(url_for("painel_login"))
        role = session.get("painel_role")
        if role in ("motoboy", "lojista"):
            allowed = {
                "painel_home",
                "painel_pedidos",
                "painel_pedido_detalhe",
                "painel_confirmar_entrega",
                "painel_confirmar_retirada",
                "painel_logout",
                "api_painel_novos_alertas",
            }
            if role == "lojista":
                # lojista também pode atualizar status do pedido
                allowed.add("painel_pedidos_status")
                allowed.add("painel_avaliar_receita")
                allowed.add("api_alpha_exportar_pedido")
            if request.endpoint not in allowed:
                flash("Acesso restrito aos pedidos.", "error")
                return redirect(url_for("painel_pedidos"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("painel_ok") or not session.get("is_admin"):
            return redirect(url_for("painel_login"))
        return fn(*args, **kwargs)
    return wrapper


def _motoboy_logged():
    return session.get("painel_role") == "motoboy"


def _lojista_logged():
    return session.get("painel_role") == "lojista"


def _ensure_lojista_schema():
    key = "lojistas_v1"
    _load_db_migrations()
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ecommerce_lojistas (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                cnpjloja TEXT NOT NULL,
                nome TEXT NOT NULL,
                usuario TEXT NOT NULL UNIQUE,
                senha_hash TEXT NOT NULL,
                ativo BOOLEAN DEFAULT TRUE,
                criado_em TIMESTAMPTZ DEFAULT NOW(),
                atualizado_em TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lojistas_cnpj ON ecommerce_lojistas(cnpjloja)")
        conn.commit()
        cur.close()
        _schema_ready.add(key)
        _mark_migration_done(key)


def _ensure_motoboy_schema():
    conn = db()
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ecommerce_motoboys (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            cnpjloja TEXT NOT NULL,
            nome TEXT NOT NULL,
            usuario TEXT NOT NULL UNIQUE,
            senha_hash TEXT NOT NULL,
            ativo BOOLEAN DEFAULT TRUE,
            criado_em TIMESTAMPTZ DEFAULT NOW(),
            atualizado_em TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_motoboys_cnpj ON ecommerce_motoboys(cnpjloja)")
    conn.commit()
    cur.close()


# ─── BANNERS ─────────────────────────────────────────────────────────────────

@app.get("/api/banners")
def api_banners():
    """
    Retorna banners de lojas próximas. Só exibido para consumidores logados.
    Query params: lat, lng, raio (km, default 80)
    Cacheado por grade de ~5km (evita query a cada abertura da home).
    """
    if not session.get("consumidor_id"):
        return jsonify({"banners": []})
    _ensure_banner_schema()
    try:
        lat  = float(request.args.get("lat", 0))
        lng  = float(request.args.get("lng", 0))
        raio = min(float(request.args.get("raio", 80)), 200)
    except (TypeError, ValueError):
        lat = lng = 0.0
        raio = 80.0

    # Chave de cache por grade de 0.05° (~5 km)
    if lat and lng:
        cache_key = f"banners:{round(lat/0.05)*50}:{round(lng/0.05)*50}"
    else:
        cache_key = "banners:global"

    cached = _banner_cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    conn = db()
    cur  = conn.cursor()

    if not (lat and lng):
        cur.close()
        return jsonify({"banners": []})

    cur.execute("""
        SELECT b.id, b.cnpjloja, b.imagem_url, b.link_url, b.titulo,
               b.ordem, u.razao,
               (6371 * acos(
                   cos(radians(%s)) * cos(radians(g.lat)) *
                   cos(radians(g.lng) - radians(%s)) +
                   sin(radians(%s)) * sin(radians(g.lat))
               )) AS distancia_km
        FROM ecommerce_banners b
        JOIN users u ON u.cnpjloja = b.cnpjloja
        JOIN ecommerce_lojas_geo g ON g.cnpjloja = b.cnpjloja
        WHERE b.ativo = TRUE
          AND (6371 * acos(
                   cos(radians(%s)) * cos(radians(g.lat)) *
                   cos(radians(g.lng) - radians(%s)) +
                   sin(radians(%s)) * sin(radians(g.lat))
               )) <= %s
        ORDER BY distancia_km, b.ordem, b.criado_em DESC
        LIMIT 20
    """, (lat, lng, lat, lat, lng, lat, raio))

    rows = cur.fetchall()
    cur.close()

    banners = [
        {
            "id":           r["id"],
            "cnpjloja":     r["cnpjloja"],
            "imagem_url":   r["imagem_url"],
            "link_url":     r["link_url"] or "",
            "titulo":       r["titulo"]   or "",
            "razao":        r["razao"]    or "",
            "distancia_km": round(float(r["distancia_km"] or 0), 1),
        }
        for r in rows
    ]
    result = {"banners": banners}
    # Só faz cache se há resultado — lista vazia não vale guardar
    if banners:
        _banner_cache_set(cache_key, result)
    return jsonify(result)


@app.get("/painel/banners")
@painel_required
def painel_banners():
    _ensure_banner_schema()
    cnpjloja = session["cnpjloja"]
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT id, imagem_url, link_url, titulo, ativo, ordem, criado_em "
        "FROM ecommerce_banners WHERE cnpjloja=%s ORDER BY ordem, criado_em DESC",
        (cnpjloja,),
    )
    banners = cur.fetchall()
    cur.close()
    return render_template("painel_banners.html", banners=banners)


@app.post("/painel/banners/upload")
@painel_required
def painel_banners_upload():
    _ensure_banner_schema()
    cnpjloja = session["cnpjloja"]
    f = request.files.get("imagem")
    if not f or not f.filename:
        flash("Selecione uma imagem.", "danger")
        return redirect(url_for("painel_banners"))

    ext = (f.filename.rsplit(".", 1)[-1] or "jpg").lower()
    if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
        flash("Formato inválido. Use JPG, PNG ou WEBP.", "danger")
        return redirect(url_for("painel_banners"))

    raw = f.read(4 * 1024 * 1024)
    ct_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
              "webp": "image/webp", "gif": "image/gif"}
    content_type = ct_map.get(ext, "image/jpeg")

    path = f"banners/{cnpjloja}/{secrets.token_hex(8)}.{ext}"
    url  = upload_to_supabase_storage(raw, path, content_type)
    if not url:
        flash("Erro ao enviar imagem. Tente novamente.", "danger")
        return redirect(url_for("painel_banners"))

    titulo   = (request.form.get("titulo")   or "").strip()[:120]
    link_url = (request.form.get("link_url") or "").strip()[:300]

    conn = db()
    cur  = conn.cursor()
    cur.execute(
        "INSERT INTO ecommerce_banners (cnpjloja, imagem_url, link_url, titulo) VALUES (%s,%s,%s,%s)",
        (cnpjloja, url, link_url or None, titulo or None),
    )
    conn.commit()
    cur.close()

    # Invalida cache de banners
    with _banner_cache_lock:
        _banner_cache.clear()

    flash("Banner enviado com sucesso.", "success")
    return redirect(url_for("painel_banners"))


@app.post("/painel/banners/<int:banner_id>/toggle")
@painel_required
def painel_banners_toggle(banner_id):
    _ensure_banner_schema()
    cnpjloja = session["cnpjloja"]
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE ecommerce_banners SET ativo = NOT ativo "
        "WHERE id=%s AND cnpjloja=%s",
        (banner_id, cnpjloja),
    )
    conn.commit()
    cur.close()
    with _banner_cache_lock:
        _banner_cache.clear()
    return redirect(url_for("painel_banners"))


@app.post("/painel/banners/<int:banner_id>/delete")
@painel_required
def painel_banners_delete(banner_id):
    _ensure_banner_schema()
    cnpjloja = session["cnpjloja"]
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        "DELETE FROM ecommerce_banners WHERE id=%s AND cnpjloja=%s",
        (banner_id, cnpjloja),
    )
    conn.commit()
    cur.close()
    with _banner_cache_lock:
        _banner_cache.clear()
    flash("Banner excluído.", "success")
    return redirect(url_for("painel_banners"))


# ─── ROTAS PÚBLICAS ───────────────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template("home.html")


@app.get("/catalogo")
def catalogo_publico():
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.cnpjloja, u.razao, u.endereco, u.uf, u.telefone,
               g.lat, g.lng
        FROM users u
        JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
        LEFT JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
        WHERE u.is_admin = FALSE
          AND c.catalogo_publico = TRUE
        ORDER BY u.razao
        """
    )
    lojas = cur.fetchall()
    cur.close()
    return render_template("index.html", lojas=lojas)


@app.get("/vitnatu")
def vitnatu_page():
    return render_template("vitnatu.html")


@app.get("/api/vitnatu-produtos")
@_rate_limited_api(max_calls=40, window_secs=60)
def api_vitnatu_produtos():
    try:
        lat_usr = float(request.args.get("lat", 0))
        lng_usr = float(request.args.get("lng", 0))
    except (ValueError, TypeError):
        lat_usr, lng_usr = 0.0, 0.0

    sem_loc = (lat_usr == 0.0 and lng_usr == 0.0)
    conn = db()
    cur  = conn.cursor()
    proximas, loja_info = [], {}

    if not sem_loc:
        cur.execute("""
            SELECT u.cnpjloja, u.razao, u.endereco, u.endereco2, u.uf, g.lat, g.lng
            FROM users u
            JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
            WHERE u.is_admin = FALSE AND g.lat IS NOT NULL
              AND COALESCE(c.catalogo_publico, TRUE) = TRUE
        """)
        geo = cur.fetchall()
        if geo:
            dists = []
            for l in geo:
                lat_l, lng_l = _geo_override(l.get("endereco"), l.get("endereco2"), l.get("uf"))
                if not lat_l:
                    lat_l, lng_l = float(l["lat"]), float(l["lng"])
                dists.append({**dict(l), "lat": lat_l, "lng": lng_l,
                               "distancia_km": round(haversine(lat_usr, lng_usr, lat_l, lng_l), 2)})
            dists.sort(key=lambda x: x["distancia_km"])
            proximas = [l for l in dists if l["distancia_km"] <= 60] or dists[:5]
            loja_info = {l["cnpjloja"]: l for l in proximas}

    if not proximas:
        cur.execute("""
            SELECT u.cnpjloja, u.razao, u.endereco
            FROM users u
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
            WHERE u.is_admin = FALSE AND COALESCE(c.catalogo_publico, TRUE) = TRUE
        """)
        proximas = list(cur.fetchall())
        loja_info = {l["cnpjloja"]: {"razao": _public_store_name(l), "distancia_km": None} for l in proximas}

    cnpjs = [l["cnpjloja"] for l in proximas]
    if not cnpjs:
        cur.close()
        return jsonify({"produtos": []})

    if _catalogo_alpha_exclusivo():
        produtos_base = get_dns_products_batch(cnpjs)
        cur.execute("SELECT * FROM vitnatu_produtos WHERE ativo=TRUE")
        vp_all = {r["nome"].upper(): dict(r) for r in cur.fetchall()}
        cur.close()

        stop = {"com","de","do","da","dos","das","para","por","em","e","ou","cp","ml","mg","un","gr","caps","comp","tab"}

        def _enrich_vitnatu_alpha(nome):
            words = [w for w in (nome or "").upper().split() if len(w) >= 4 and w.lower() not in stop][:3]
            for vp_nome, vp in vp_all.items():
                if words and all(w in vp_nome for w in words):
                    return vp
            return None

        produtos = []
        seen = set()
        for row in produtos_base:
            hay = " ".join(str(row.get(k) or "") for k in ("nome", "marca", "laboratorio")).upper()
            if "VITNATU" not in hay and "VIT NATU" not in hay:
                continue
            ean = (row.get("ean") or "").strip()
            ean_norm = ean.lstrip("0") or ean
            if not ean_norm or ean_norm in seen:
                continue
            seen.add(ean_norm)
            p = dict(row)
            vp = _enrich_vitnatu_alpha(p.get("nome") or "")
            if vp:
                p["serve_para"] = vp.get("serve_para") or ""
                p["porque_comprar"] = vp.get("porque_comprar") or ""
                p["como_usar"] = vp.get("como_usar") or ""
            info = loja_info.get(p["cnpjloja"], {})
            p["razao"] = _public_store_name(info) if info else ""
            p["distancia_km"] = info.get("distancia_km")
            produtos.append(p)
        produtos.sort(key=lambda x: (x.get("distancia_km") is None, x.get("distancia_km") or 0, (x.get("nome") or "").lower()))
        return jsonify({"produtos": produtos, "n_lojas": len({p["cnpjloja"] for p in produtos})})

    _VITNATU_FILTER = """
        AND (
            UPPER(COALESCE(m.laboratorio,'')) LIKE '%%VITNATU%%'
            OR UPPER(COALESCE(m.laboratorio,'')) LIKE '%%VIT NATU%%'
            OR UPPER(COALESCE(m.marca,''))      LIKE '%%VITNATU%%'
            OR UPPER(COALESCE(m.marca,''))      LIKE '%%VIT NATU%%'
            OR UPPER(COALESCE(m.descricao,''))  LIKE '%%VITNATU%%'
            OR UPPER(COALESCE(m.descricao,''))  LIKE '%%VIT NATU%%'
            OR UPPER(COALESCE(e.descricao,''))  LIKE '%%VITNATU%%'
            OR UPPER(COALESCE(e.descricao,''))  LIKE '%%VIT NATU%%'
        )
        AND LTRIM(e.barras,'0') NOT IN ('7898638342004','7898722820623')
    """

    cur.execute(f"""
        SELECT DISTINCT ON (LTRIM(e.barras,'0'))
               e.cnpj AS cnpjloja,
               e.barras AS ean,
               COALESCE(m.descricao, e.descricao) AS nome,
               m.marca, m.laboratorio,
               CAST(e.estoque AS INTEGER) AS qty,
               COALESCE(ep.preco_customizado, vg.preco_venda, e.preco_referencial) AS preco,
               vi.imagem_url AS imagem
        FROM estoque e
        LEFT JOIN medicamentos m
               ON LTRIM(COALESCE(m.barra_norm, m.barra,''),'0') = LTRIM(e.barras,'0')
              AND m.barra_norm IS NOT NULL
        LEFT JOIN LATERAL (
            SELECT ROUND(total_vendasgeral / NULLIF(itens,0),2) AS preco_venda
            FROM vendageral WHERE cnpj=e.cnpj AND ean=e.barras
              AND total_vendasgeral>0 AND itens>0
            ORDER BY id DESC LIMIT 1
        ) vg ON TRUE
        LEFT JOIN vitnatu_imagens vi ON vi.ean = LTRIM(e.barras,'0')
        LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = e.cnpj AND ep.ean = e.barras
        WHERE e.cnpj = ANY(%s) AND e.estoque > 0
          AND LENGTH(e.barras) >= 8
          {_VITNATU_FILTER}
        ORDER BY LTRIM(e.barras,'0'), array_position(%s::text[], e.cnpj)
    """, (cnpjs, cnpjs))
    alpha = cur.fetchall()

    _VF_AUTO = _VITNATU_FILTER.replace('e.descricao', 'ae.descricao_produto').replace('e.barras', 'ae.ean')
    cur.execute(f"""
        SELECT DISTINCT ON (LTRIM(ae.ean,'0'))
               ae.cnpj_loja AS cnpjloja,
               ae.ean,
               COALESCE(m.descricao, ae.descricao_produto) AS nome,
               m.marca, m.laboratorio,
               CAST(ae.quantidade_estoque AS INTEGER) AS qty,
               COALESCE(ep.preco_customizado, ae.valor_final_produto) AS preco,
               vi.imagem_url AS imagem
        FROM automatiza_estoque ae
        LEFT JOIN medicamentos m
               ON LTRIM(COALESCE(m.barra_norm, m.barra,''),'0') = LTRIM(ae.ean,'0')
              AND m.barra_norm IS NOT NULL
        LEFT JOIN vitnatu_imagens vi ON vi.ean = LTRIM(ae.ean,'0')
        LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = ae.cnpj_loja AND ep.ean = ae.ean
        WHERE ae.cnpj_loja = ANY(%s) AND ae.quantidade_estoque > 0
          AND LENGTH(ae.ean) >= 8
          {_VF_AUTO}
        ORDER BY LTRIM(ae.ean,'0'), array_position(%s::text[], ae.cnpj_loja)
    """, (cnpjs, cnpjs))
    auto = cur.fetchall()

    # Carregar todos os dados de vitnatu_produtos para enriquecer
    cur.execute("SELECT * FROM vitnatu_produtos WHERE ativo=TRUE")
    vp_all = {r["nome"].upper(): dict(r) for r in cur.fetchall()}

    # Fallback: vitnatu_imagens já tem tudo mapeado por EAN — consulta direta
    all_eans_raw = list({(row["ean"] or "").strip() for row in list(alpha) + list(auto) if row.get("ean")})
    _img_fallback: dict = {}
    if all_eans_raw:
        try:
            cur.execute(
                "SELECT ean, imagem_url FROM vitnatu_imagens WHERE ean = ANY(%s)",
                ([e.lstrip("0") or e for e in all_eans_raw],),
            )
            _img_fallback = {r["ean"]: r["imagem_url"] for r in cur.fetchall()}
        except Exception:
            pass

    cur.close()

    stop = {"com","de","do","da","dos","das","para","por","em","e","ou","cp","ml","mg","un","gr","caps","comp","tab"}

    def _enrich_vitnatu(nome):
        words = [w for w in nome.upper().split() if len(w) >= 4 and w.lower() not in stop][:3]
        best = None
        for vp_nome, vp in vp_all.items():
            if all(w in vp_nome for w in words):
                best = vp
                break
        return best

    seen, produtos = set(), []
    for row in list(alpha) + list(auto):
        ean = (row["ean"] or "").strip()
        if not ean:
            continue
        ean_norm = ean.lstrip("0") or ean
        if ean_norm in seen:
            continue
        seen.add(ean_norm)
        p = dict(row)
        # Imagem: filtrar placeholders e usar fallback se necessário
        img = (p.get("imagem") or "").strip()
        if img in _MEDICINE_PLACEHOLDER_URLS:
            img = ""
        if not img:
            img = _img_fallback.get(ean_norm, "")
        p["imagem"] = img
        # Enriquecer com vitnatu_produtos
        vp = _enrich_vitnatu(p.get("nome") or "")
        if vp:
            p["serve_para"]     = vp.get("serve_para") or ""
            p["porque_comprar"] = vp.get("porque_comprar") or ""
            p["como_usar"]      = vp.get("como_usar") or ""
        # Info da loja mais próxima
        info = loja_info.get(p["cnpjloja"], {})
        p["razao"]        = _public_store_name(info) if info else ""
        p["distancia_km"] = info.get("distancia_km")
        produtos.append(p)

    produtos.sort(key=lambda x: (x.get("distancia_km") is None, x.get("distancia_km") or 0, (x.get("nome") or "").lower()))
    return jsonify({"produtos": produtos, "n_lojas": len({p["cnpjloja"] for p in produtos})})


@app.get("/ofertas")
def ofertas_publicas():
    return redirect(url_for("catalogo_publico", ofertas="1"))


@app.get("/lojas")
def lojas_vitrine():
    _ensure_lojas_vitrine_schema()

    # Busca do ImageKit (lojas legadas do poupaqui-admin)
    lojas_ik = _load_imagekit_lojas()

    # Busca do Supabase (lojas novas adicionadas pelo painel dns-ecommerce)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT cidade, endereco, telefone, whatsapp, imagem_url, cnpjloja FROM ecommerce_lojas_vitrine ORDER BY ordem, cidade")
    rows = cur.fetchall()

    # Mapa cidade → cnpjloja para lojas legadas (ImageKit) que não têm cnpj na vitrine
    cur.execute("SELECT cidade, cnpjloja FROM ecommerce_vitrine_cnpj_map")
    cidade_cnpj_map = {r["cidade"]: r["cnpjloja"] for r in cur.fetchall()}
    cur.close()

    lojas_sb = [
        {
            "cidade": r["cidade"],
            "endereco": r["endereco"],
            "telefone": r["telefone"],
            "whatsapp": r["whatsapp"],
            "imagem_url": r["imagem_url"],
            "cnpjloja": r["cnpjloja"] or cidade_cnpj_map.get(r["cidade"]),
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
            "cnpjloja": cidade_cnpj_map.get(l.get("cidade", "")),
        }
        for l in lojas_ik
    ]

    # Combina: ImageKit primeiro (já vem ordenado por cidade), depois Supabase
    from itertools import chain
    todas = sorted(
        chain(lojas_ik_norm, lojas_sb),
        key=lambda x: (x.get("cidade") or "").lower()
    )

    # Lojas com plano de assinatura ativo (vem dos users reais do sistema)
    lojas_com_plano = []
    try:
        _ensure_assinatura_schema()
        cur2 = conn.cursor()
        cur2.execute("""
            SELECT u.cnpjloja, u.razao, u.endereco, u.uf,
                   p.nome AS plano_nome, p.descricao, p.preco_mensal, p.beneficios
            FROM ecommerce_planos_assinatura p
            JOIN users u ON u.cnpjloja = p.cnpjloja
            WHERE p.ativo = TRUE AND u.is_admin = FALSE
            ORDER BY u.razao
        """)
        lojas_com_plano = cur2.fetchall()
        cur2.close()
    except Exception:
        pass

    # Verifica assinaturas do consumidor logado
    assinados_cnpj = set()
    consumidor_id = str(session.get("consumidor_id") or "")
    if consumidor_id and lojas_com_plano:
        try:
            cur3 = conn.cursor()
            cnpjs_plano = [r["cnpjloja"] for r in lojas_com_plano]
            ph = ",".join(["%s"] * len(cnpjs_plano))
            cur3.execute(
                f"""SELECT cnpjloja FROM ecommerce_assinantes
                    WHERE consumidor_id=%s AND cnpjloja IN ({ph})
                      AND status='ativo' AND pagamento_status='aprovado'
                      AND (data_fim IS NULL OR data_fim > NOW())""",
                [consumidor_id] + cnpjs_plano,
            )
            assinados_cnpj = {r["cnpjloja"] for r in cur3.fetchall()}
            cur3.close()
        except Exception:
            pass

    return render_template("lojas_vitrine.html", lojas=todas,
                           lojas_com_plano=lojas_com_plano,
                           assinados_cnpj=assinados_cnpj)


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


def _ensure_avaliacoes_schema():
    key = "avaliacoes_loja"
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        conn = db(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_avaliacoes_loja (
                id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                cnpjloja     TEXT NOT NULL,
                pedido_id    TEXT NOT NULL,
                consumidor_id UUID,
                estrelas     SMALLINT NOT NULL CHECK (estrelas BETWEEN 1 AND 5),
                comentario   TEXT,
                criado_em    TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_avaliacao_pedido UNIQUE (pedido_id)
            );
            CREATE INDEX IF NOT EXISTS idx_aval_cnpj ON ecommerce_avaliacoes_loja(cnpjloja);
        """)
        conn.commit(); cur.close()
        _schema_ready.add(key)


def _calcular_reputacao_loja(avaliacoes):
    """
    Regra: 5 avaliações positivas (4-5★) cancelam 1 negativa (1-3★).
    Mínimo 5 avaliações para exibir publicamente.
    Retorna dict com média, totais e comentários, ou None se insuficiente.
    """
    if len(avaliacoes) < 5:
        return None
    positivas = [a for a in avaliacoes if a["estrelas"] >= 4]
    negativas  = sorted([a for a in avaliacoes if a["estrelas"] < 4], key=lambda x: x["estrelas"])
    n_cancel   = min(len(negativas), len(positivas) // 5)
    negativas_efetivas = negativas[n_cancel:]
    efetivas   = list(positivas) + negativas_efetivas
    media      = sum(a["estrelas"] for a in efetivas) / len(efetivas)
    comentarios = [a for a in sorted(avaliacoes, key=lambda x: x.get("criado_em") or "", reverse=True)
                   if (a.get("comentario") or "").strip()][:6]
    return {
        "media":              round(media, 1),
        "total":              len(avaliacoes),
        "positivas":          len(positivas),
        "negativas_total":    len(negativas),
        "negativas_canceladas": n_cancel,
        "comentarios":        [dict(c) for c in comentarios],
    }


def _reputacao_loja(cnpjloja):
    """Carrega avaliações do banco e calcula reputação."""
    _ensure_avaliacoes_schema()
    try:
        conn = db(); cur = conn.cursor()
        cur.execute(
            "SELECT estrelas, comentario, criado_em FROM ecommerce_avaliacoes_loja WHERE cnpjloja=%s ORDER BY criado_em DESC",
            (cnpjloja,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return _calcular_reputacao_loja(rows)
    except Exception:
        return None


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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ecommerce_interesses_regiao (
            id                    SERIAL PRIMARY KEY,
            anon_id               TEXT NOT NULL,
            consumidor_id          UUID,
            cidade_interesse       TEXT,
            uf_interesse           TEXT,
            localizacao_label      TEXT,
            lat                    DOUBLE PRECISION,
            lng                    DOUBLE PRECISION,
            raio_km                NUMERIC,
            raio_fallback_km       NUMERIC,
            motivo                 TEXT NOT NULL DEFAULT 'sem_produtos',
            cidades_disponiveis    JSONB DEFAULT '[]'::jsonb,
            contador               INTEGER DEFAULT 1,
            primeiro_registro_em   TIMESTAMPTZ DEFAULT NOW(),
            ultimo_registro_em     TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_eir_anon_regiao_motivo
        ON ecommerce_interesses_regiao (
            anon_id,
            COALESCE(cidade_interesse, ''),
            COALESCE(uf_interesse, ''),
            motivo
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_eir_regiao
        ON ecommerce_interesses_regiao(cidade_interesse, uf_interesse)
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


@app.post("/api/sugestao-regiao")
@_rate_limited_api(max_calls=10, window_secs=60)
def api_sugestao_regiao():
    """Salva sugestão de cidade/produto enviada pelo usuário na tela de área sem cobertura."""
    data = request.get_json(silent=True) or {}
    cidade  = (data.get("cidade_sugerida") or "").strip()[:200]
    produto = (data.get("produto_desejado") or "").strip()[:300]
    anon_id = (data.get("anon_id") or "").strip()[:80]
    label   = (data.get("localizacao_label") or "").strip()[:300]
    try:
        lat = float(data.get("lat") or 0) or None
        lng = float(data.get("lng") or 0) or None
    except (TypeError, ValueError):
        lat = lng = None

    if not cidade:
        return jsonify({"ok": False, "erro": "cidade obrigatória"}), 400

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ecommerce_sugestoes_regiao (
            id               SERIAL PRIMARY KEY,
            anon_id          TEXT,
            cidade_sugerida  TEXT NOT NULL,
            produto_desejado TEXT,
            localizacao_label TEXT,
            lat              DOUBLE PRECISION,
            lng              DOUBLE PRECISION,
            criado_em        TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        INSERT INTO ecommerce_sugestoes_regiao
            (anon_id, cidade_sugerida, produto_desejado, localizacao_label, lat, lng)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (anon_id, cidade, produto or None, label or None, lat, lng))
    conn.commit()
    cur.close()
    return jsonify({"ok": True})


@app.get("/painel/admin/sugestoes-regiao")
@admin_required
def admin_sugestoes_regiao():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ecommerce_sugestoes_regiao (
            id               SERIAL PRIMARY KEY,
            anon_id          TEXT,
            cidade_sugerida  TEXT NOT NULL,
            produto_desejado TEXT,
            localizacao_label TEXT,
            lat              DOUBLE PRECISION,
            lng              DOUBLE PRECISION,
            criado_em        TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        SELECT cidade_sugerida,
               produto_desejado,
               localizacao_label,
               COUNT(*)                              AS total,
               COUNT(DISTINCT anon_id)               AS usuarios,
               MAX(criado_em)                        AS ultima_vez,
               array_agg(DISTINCT produto_desejado
                         ORDER BY produto_desejado)
                 FILTER (WHERE produto_desejado IS NOT NULL) AS produtos_lista
        FROM ecommerce_sugestoes_regiao
        GROUP BY cidade_sugerida, produto_desejado, localizacao_label
        ORDER BY total DESC, ultima_vez DESC
        LIMIT 500
    """)
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(*) AS total, COUNT(DISTINCT anon_id) AS usuarios FROM ecommerce_sugestoes_regiao")
    stats = cur.fetchone()
    cur.close()
    return render_template("admin_sugestoes_regiao.html", rows=rows, stats=stats)


@app.post("/api/interesse-regiao")
@_rate_limited_api(max_calls=30, window_secs=60)
def api_interesse_regiao():
    """Registra regiões onde o usuário procurou e não encontrou cobertura no raio atual."""
    _ensure_lojas_vitrine_schema()
    data = request.get_json(silent=True) or {}
    anon_id = (data.get("anon_id") or "").strip()[:80]
    if not anon_id:
        return jsonify({"ok": False, "erro": "anon_id obrigatório"}), 400

    label = (data.get("localizacao_label") or "").strip()[:240]
    cidade = (data.get("cidade") or "").strip()[:120]
    uf = (data.get("uf") or "").strip().upper()[:2]
    if not cidade and label:
        cidade_parse, uf_parse = _parse_cidade_uf(label)
        cidade = (cidade_parse or "").strip()[:120]
        uf = uf or (uf_parse or "").strip().upper()[:2]

    motivo = (data.get("motivo") or "sem_produtos").strip()[:40]
    if motivo not in ("sem_produtos", "fora_raio"):
        motivo = "sem_produtos"

    def _num_or_none(value):
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    cidades = data.get("cidades_disponiveis") or []
    if not isinstance(cidades, list):
        cidades = []
    cidades = [
        {
            "cidade": str(c.get("cidade") or c.get("nome") or "")[:120],
            "uf": str(c.get("uf") or "")[:2].upper(),
        }
        for c in cidades[:30]
        if isinstance(c, dict) and (c.get("cidade") or c.get("nome"))
    ]

    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ecommerce_interesses_regiao (
            anon_id, consumidor_id, cidade_interesse, uf_interesse, localizacao_label,
            lat, lng, raio_km, raio_fallback_km, motivo, cidades_disponiveis
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (
            anon_id,
            COALESCE(cidade_interesse, ''),
            COALESCE(uf_interesse, ''),
            motivo
        )
        DO UPDATE SET
            consumidor_id = COALESCE(EXCLUDED.consumidor_id, ecommerce_interesses_regiao.consumidor_id),
            localizacao_label = COALESCE(NULLIF(EXCLUDED.localizacao_label, ''), ecommerce_interesses_regiao.localizacao_label),
            lat = COALESCE(EXCLUDED.lat, ecommerce_interesses_regiao.lat),
            lng = COALESCE(EXCLUDED.lng, ecommerce_interesses_regiao.lng),
            raio_km = COALESCE(EXCLUDED.raio_km, ecommerce_interesses_regiao.raio_km),
            raio_fallback_km = COALESCE(EXCLUDED.raio_fallback_km, ecommerce_interesses_regiao.raio_fallback_km),
            cidades_disponiveis = CASE
                WHEN EXCLUDED.cidades_disponiveis <> '[]'::jsonb THEN EXCLUDED.cidades_disponiveis
                ELSE ecommerce_interesses_regiao.cidades_disponiveis
            END,
            contador = ecommerce_interesses_regiao.contador + 1,
            ultimo_registro_em = NOW()
        """,
        (
            anon_id,
            session.get("consumidor_id"),
            cidade or None,
            uf or None,
            label or None,
            _num_or_none(data.get("lat")),
            _num_or_none(data.get("lng")),
            _num_or_none(data.get("raio_km")),
            _num_or_none(data.get("raio_fallback_km")),
            motivo,
            json.dumps(cidades, ensure_ascii=False),
        ),
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
    cur.execute("""
        SELECT u.cnpjloja, u.endereco2, g.lat, g.lng
        FROM users u
        JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
        WHERE g.lat IS NOT NULL AND g.lng IS NOT NULL
    """)
    geo_por_cnpj = {
        r["cnpjloja"]: {"lat": r["lat"], "lng": r["lng"], "endereco": r.get("endereco2")}
        for r in cur.fetchall()
        if r.get("cnpjloja")
    }
    cur.execute("SELECT cidade, cnpjloja FROM ecommerce_vitrine_cnpj_map")
    cidade_cnpj_map = {r["cidade"]: r["cnpjloja"] for r in cur.fetchall()}
    cur.execute(
        "SELECT cidade, endereco, telefone, whatsapp, imagem_url, cnpjloja "
        "FROM ecommerce_lojas_vitrine ORDER BY ordem, cidade"
    )
    lojas_sb = []
    for r in cur.fetchall():
        d = dict(r)
        d["cnpjloja"] = d.get("cnpjloja") or cidade_cnpj_map.get(d["cidade"])
        lojas_sb.append(d)
    cur.close()

    lojas_ik = _load_imagekit_lojas()
    lojas_ik_norm = [
        {"cidade": l.get("cidade",""), "endereco": l.get("endereco",""),
         "telefone": l.get("telefone",""), "whatsapp": l.get("whatsapp",""),
         "imagem_url": l.get("url",""),
         "cnpjloja": cidade_cnpj_map.get(l.get("cidade",""))}
        for l in lojas_ik
    ]

    from itertools import chain
    resultado = []
    vistos = set()
    for loja in sorted(chain(lojas_ik_norm, lojas_sb),
                       key=lambda x: (x.get("cidade") or "").lower()):
        c = loja.get("cidade","")
        cnpj = loja.get("cnpjloja")
        chave = cnpj or f"{c}|{loja.get('endereco','')}|{loja.get('imagem_url','')}"
        if chave in vistos:
            continue
        vistos.add(chave)
        geo = geo_por_cnpj.get(cnpj) if cnpj else None
        if geo:
            loja["lat"], loja["lng"] = geo["lat"], geo["lng"]
            if geo.get("endereco"):
                loja["endereco"] = geo["endereco"]
        else:
            loja["lat"], loja["lng"] = coords.get(c, (None, None))
        resultado.append(loja)
    cur = conn.cursor()
    cur.execute("""
        SELECT u.cnpjloja, u.razao, u.endereco AS apelido, u.endereco2 AS endereco, u.telefone, g.lat, g.lng
        FROM users u
        JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
        WHERE u.cnpjloja IS NOT NULL
          AND g.lat IS NOT NULL
          AND g.lng IS NOT NULL
        ORDER BY u.razao, u.cnpjloja
    """)
    for row in cur.fetchall():
        cnpj = row.get("cnpjloja")
        if not cnpj or cnpj in vistos:
            continue
        loja = dict(row)
        apelido = re.sub(r"\s*-\s*\d{14}\s*$", "", (loja.get("apelido") or "")).strip()
        loja["cidade"] = apelido or _public_store_name(loja)
        loja["imagem_url"] = ""
        loja["whatsapp"] = ""
        resultado.append(loja)
        vistos.add(cnpj)
    cur.close()
    _espalhar_marcadores_sobrepostos(resultado)
    return jsonify(resultado)


def _espalhar_marcadores_sobrepostos(lojas):
    grupos = {}
    for loja in lojas:
        if loja.get("lat") is None or loja.get("lng") is None:
            continue
        key = (round(float(loja["lat"]), 6), round(float(loja["lng"]), 6))
        grupos.setdefault(key, []).append(loja)
    for grupo in grupos.values():
        if len(grupo) <= 1:
            continue
        passo = 0.00018
        total = len(grupo)
        for idx, loja in enumerate(grupo):
            ang = (2 * math.pi * idx) / total
            loja["lat"] = float(loja["lat"]) + math.sin(ang) * passo
            loja["lng"] = float(loja["lng"]) + math.cos(ang) * passo


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


@app.get("/api/lojas/ativas")
@_rate_limited_api(max_calls=60, window_secs=60)
def api_lojas_ativas():
    """Lojas com catalogo_publico=true, opcionalmente ordenadas por proximidade.
    Retorna sem_farmacia_proxima=true quando nenhuma está dentro de 60km."""
    try:
        lat = float(request.args.get("lat", 0))
        lng = float(request.args.get("lng", 0))
    except (ValueError, TypeError):
        lat = lng = 0.0

    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT u.cnpjloja, u.razao, u.endereco, u.endereco2, u.uf, u.telefone,
               g.lat, g.lng
        FROM users u
        JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
        LEFT JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
        WHERE u.is_admin = FALSE
          AND c.catalogo_publico = TRUE
        ORDER BY u.razao
    """)
    rows = cur.fetchall()
    cur.close()

    lojas = []
    tem_proxima = False
    for r in rows:
        cidade = re.sub(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b", "", r["endereco"] or "")
        cidade = re.sub(r"\b\d{14}\b", "", cidade)
        cidade = re.split(r"\s+-\s+|\s+-\s*$|-\s*$", cidade, maxsplit=1)[0]
        cidade = re.sub(r"\s+", " ", cidade).strip(" -,.")
        dist = None
        if lat != 0.0 or lng != 0.0:
            if r["lat"] and r["lng"]:
                dist = round(haversine(lat, lng, float(r["lat"]), float(r["lng"])), 1)
                if dist <= 60:
                    tem_proxima = True
        lojas.append({
            "cnpjloja":    r["cnpjloja"],
            "razao":       _public_store_name(r),
            "cidade":      cidade or _public_store_name(r),
            "uf":          (r["uf"] or "").strip().upper(),
            "endereco":    r["endereco2"] or "",
            "lat":         float(r["lat"]) if r["lat"] is not None else None,
            "lng":         float(r["lng"]) if r["lng"] is not None else None,
            "distancia_km": dist,
        })

    if lat != 0.0 or lng != 0.0:
        lojas.sort(key=lambda x: (x["distancia_km"] is None, x["distancia_km"] or 9999))

    return jsonify({
        "lojas": lojas,
        "sem_farmacia_proxima": bool((lat != 0.0 or lng != 0.0) and not tem_proxima and lojas),
    })


@app.get("/api/produtos-destaque")
def api_produtos_destaque():
    """Produtos em destaque sem necessidade de localização (fallback home)."""
    try:
        conn = db()
        cur  = conn.cursor()
        cur.execute(
            """
            SELECT u.cnpjloja, u.razao
            FROM users u
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
            WHERE u.is_admin = FALSE
              AND COALESCE(c.catalogo_publico, TRUE) = TRUE
            ORDER BY u.razao
            """
        )
        lojas = cur.fetchall()
        cur.close()
        cnpjs = [l["cnpjloja"] for l in lojas]
        loja_nome = {l["cnpjloja"]: _public_store_name(l) for l in lojas}
        produtos = []
        fonte_produtos = (
            get_alpha_products_direct(cnpjs, limit=200)
            if _catalogo_alpha_exclusivo()
            else get_dns_products_batch(cnpjs)
        )
        for p in fonte_produtos:
            d = dict(p)
            d["razao"] = d.get("razao") or loja_nome.get(d.get("cnpjloja"), "")
            produtos.append(d)
        random.shuffle(produtos)
        return jsonify({"produtos": produtos[:20]})
    except Exception:
        return jsonify({"produtos": []})


@app.get("/api/produtos-proximos")
@_rate_limited_api(max_calls=40, window_secs=60)
def api_produtos_proximos():
    """Wrapper fino: qualquer excecao nao prevista no pipeline de busca (que e
    longo e tem varios caminhos — NL/IA, direto, fuzzy, complementos por loja)
    vira uma resposta vazia normal em vez de um 500 cru, que o front-end
    mostra como "Erro ao carregar. Recarregue a pagina."."""
    try:
        return _api_produtos_proximos_impl()
    except Exception:
        app.logger.exception("Erro em /api/produtos-proximos")
        raio = float(request.args.get("raio", 30))
        raio_fallback = max(raio, float(request.args.get("raio_fallback", 60)))
        return jsonify({
            "produtos": [], "cnpjs_proximos": [], "lojas_proximas": [],
            "fora_raio": False, "sem_geocode": False,
            "raio_km": raio, "raio_fallback_km": raio_fallback, "n_lojas": 0,
            "saudacao": None, "alternativa_para": None, "principio_ativo_ia": None,
        })


def _api_produtos_proximos_impl():
    _ensure_delivery_schema()
    _ensure_catalog_admin_schema()
    _ensure_logo_url_column()
    _busca_inicio = time.monotonic()
    _entrega_horario_cache: dict = {}
    def _entrega_disponivel_horario_cached(cnpj):
        if cnpj not in _entrega_horario_cache:
            _entrega_horario_cache[cnpj] = _status_horario_entrega(cnpj).get("entrega_disponivel_horario", True)
        return _entrega_horario_cache[cnpj]
    try:
        lat_usr = float(request.args["lat"])
        lng_usr = float(request.args["lng"])
    except (KeyError, ValueError):
        return jsonify({"error": "lat/lng inválidos"}), 400

    raio    = float(request.args.get("raio", 30))
    raio_fallback = max(raio, float(request.args.get("raio_fallback", 60)))
    busca_q = (request.args.get("q") or "").strip()
    cat_filter = (request.args.get("cat") or "").strip().lower()
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
                   COALESCE(c.valor_frete, 0) AS valor_frete,
                   c.logo_url
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
            SELECT u.cnpjloja, u.razao, u.endereco, c.logo_url
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
            l["cnpjloja"]: {"razao": _public_store_name(l), "distancia_km": None, "aceita_entrega": False, "raio_entrega_km": 0, "cobra_frete": False, "valor_frete": 0, "logo_url": l.get("logo_url")}
            for l in todas
        }

    cur.close()

    if not proximas:
        return jsonify({"produtos": [], "fora_raio": False, "raio_km": raio, "raio_fallback_km": raio_fallback, "n_lojas": 0})

    cnpjs = [l["cnpjloja"] for l in proximas]
    produtos_raw = []
    ia_filter_terms = None  # preenchido pelo caminho NL; usado no filtro final
    _nl_ean_src = set()    # EANs do índice de sintomas; base limpa para fallback NL sem IA
    _alternativa_para   = None  # nome da marca buscada quando não encontrada diretamente
    _principio_ativo_ia = None  # genérico/PA encontrado pela IA em substituição

    is_nl = _is_natural_language_query(busca_q) if busca_q else False

    if busca_q:
        if is_nl:
            # --- Caminho NL: IA + busca direta em paralelo ---
            query_norm_nl = _norm_query_cache(busca_q)
            cached_ia = _busca_cache_get(query_norm_nl)

            if cached_ia:
                ia_result = cached_ia
            else:
                # Dispara chamada IA em thread paralela enquanto busca direta já roda
                _ia_holder = [None]
                def _run_ia():
                    _ia_holder[0] = _claude_busca_interpret(busca_q)
                _ia_thread = threading.Thread(target=_run_ia, daemon=True)
                _ia_thread.start()

                # Busca direta simultânea (não espera a IA)
                _st_parallel = _search_terms_for_query(busca_q) or [_norm_text(busca_q)]
                _ie_parallel  = [e for e in _symptom_index_eans_for_query(busca_q, limit=200)
                                  if re.match(r"^789\d{10}$", e)]
                if _ie_parallel:
                    produtos_raw = get_dns_products_batch_by_eans(cnpjs, _ie_parallel[:100])
                    _nl_ean_src.update(_ie_parallel)
                if not produtos_raw:
                    produtos_raw = get_dns_products_batch_by_name(cnpjs, _st_parallel[:4])

                # Aguarda IA no máximo 1s (já temos resultados do DB enquanto isso)
                _ia_thread.join(timeout=1.0)
                ia_result = _ia_holder[0]
                # Quando o banco não retornou nada ainda, vale esperar mais pelo Claude
                # (cobre buscas de marca como "allegra" onde só a IA sabe o genérico)
                if not produtos_raw and ia_result is None:
                    _ia_thread.join(timeout=4.0)
                    ia_result = _ia_holder[0]

            if ia_result:
                ia_terms = []
                for _field in ("principios_ativos", "nomes_tecnicos", "categorias", "termos_busca"):
                    for _t in (ia_result.get(_field) or []):
                        _t = _norm_text(_t)
                        # >=4 chars: termos de 2-3 letras geram falso-positivo em LIKE
                        # contra o catalogo inteiro quando a IA "alucina" um termo vago.
                        if _t and len(_t) >= 4 and _t not in ia_terms:
                            ia_terms.append(_t)
                if ia_terms:
                    ia_filter_terms = ia_terms
                    _nl_antes_ia = len(produtos_raw)
                    # Complementa com produtos por nome via termos IA (merge com busca direta)
                    _ia_seen = {(p.get("cnpjloja"), p.get("ean")) for p in produtos_raw}
                    for p in get_dns_products_batch_by_name(cnpjs, ia_terms[:6]):
                        key = (p.get("cnpjloja"), p.get("ean"))
                        if key not in _ia_seen:
                            produtos_raw.append(p)
                            _ia_seen.add(key)
                    ia_eans = []
                    seen_ia_eans = {p.get("ean") for p in produtos_raw}
                    for _ia_term in ia_terms[:5]:
                        for ean in _symptom_index_eans_for_query(_ia_term, limit=60):
                            if re.match(r"^789\d{10}$", ean) and ean not in seen_ia_eans:
                                ia_eans.append(ean)
                                seen_ia_eans.add(ean)
                    if ia_eans:
                        _ia_seen2 = {(p.get("cnpjloja"), p.get("ean")) for p in produtos_raw}
                        for p in get_dns_products_batch_by_eans(cnpjs, ia_eans[:80]):
                            key = (p.get("cnpjloja"), p.get("ean"))
                            if key not in _ia_seen2:
                                produtos_raw.append(p)
                                _ia_seen2.add(key)
                    # Detecta busca de marca que retornou apenas genérico (sem produto original)
                    if len(produtos_raw) > _nl_antes_ia and _nl_antes_ia == 0:
                        _principio_ativo_ia = (ia_result.get("principios_ativos") or ia_terms)[:1]
                        _principio_ativo_ia = _principio_ativo_ia[0] if _principio_ativo_ia else None
                        _bq_norm = _norm_text(busca_q)
                        # Só marca como "alternativa" se o nome buscado não aparece nos produtos encontrados
                        _nomes_encontrados = " ".join(_norm_text(p.get("nome") or "") for p in produtos_raw)
                        if _bq_norm not in _nomes_encontrados:
                            _alternativa_para = busca_q
            # Fallback: sem resultado nenhum
            if not produtos_raw:
                _st = _search_terms_for_query(busca_q)
                _ie = _symptom_index_eans_for_query(busca_q, limit=300)
                _re = [e for e in _ie if re.match(r"^789\d{10}$", e)]
                if _re:
                    produtos_raw = get_dns_products_batch_by_eans(cnpjs, _re[:120])
                if not produtos_raw:
                    produtos_raw = get_dns_products_batch_by_name(cnpjs, _st or [busca_q])

            # Complementa com índice de sintomas direto para o termo original —
            # captura marcas comerciais indexadas pelo sintoma (ex: Anador está sob "febre")
            # mas cujo nome não contém os termos químicos que a IA retornou.
            _d_eans = [e for e in _symptom_index_eans_for_query(busca_q, limit=200)
                       if re.match(r"^789\d{10}$", e)]
            if _d_eans:
                _nl_ean_src.update(_d_eans)
                _seen_nl = {(p.get("cnpjloja"), p.get("ean")) for p in produtos_raw}
                _seen_nl_eans = {p.get("ean") for p in produtos_raw}
                _new_d = [e for e in _d_eans if e not in _seen_nl_eans]
                if _new_d:
                    for p in get_dns_products_batch_by_eans(cnpjs, _new_d[:100]):
                        key = (p.get("cnpjloja"), p.get("ean"))
                        if key not in _seen_nl:
                            produtos_raw.append(p)
                            _seen_nl.add(key)
            # Não estende ia_filter_terms com termos genéricos do dicionário —
            # "dor" bateria em "condor", "cortador" etc por substring.

            # Safety net: caminho NL sem resultado — tenta busca por nome direto da query
            # (cobre casos onde IA retornou termos que não batem com nenhum produto no estoque)
            if not produtos_raw:
                ia_filter_terms = None  # libera filtro para o q_terms mais amplo
                _fb_terms = _search_terms_for_query(busca_q) or [_norm_text(busca_q)]
                produtos_raw = get_dns_products_batch_by_name(cnpjs, _fb_terms[:4])
        else:
            # --- Caminho direto: waterfall passo 1 → 2 → 3 ---
            search_terms = _search_terms_for_query(busca_q)
            index_eans = _symptom_index_eans_for_query(busca_q, limit=300)

            # Passo 1: EAN index + nome
            real_eans = [e for e in index_eans if re.match(r"^789\d{10}$", e)]
            if real_eans:
                produtos_raw = get_dns_products_batch_by_eans(cnpjs, real_eans[:120])
            if not produtos_raw:
                produtos_raw = get_dns_products_batch_by_name(cnpjs, search_terms or [busca_q])

            # Passo 2: fuzzy pg_trgm se menos de 3 resultados
            if len(produtos_raw) < 3:
                fuzzy_eans = _busca_fuzzy_pg_trgm(busca_q)
                real_fuzzy = [e for e in fuzzy_eans if re.match(r"^789\d{10}$", e)]
                if real_fuzzy:
                    seen_p2 = {(p.get("cnpjloja"), p.get("ean")) for p in produtos_raw}
                    for p in get_dns_products_batch_by_eans(cnpjs, real_fuzzy[:80]):
                        if (p.get("cnpjloja"), p.get("ean")) not in seen_p2:
                            produtos_raw.append(p)

            # Passo 3: IA apenas quando banco não encontrou nada suficiente
            _alternativa_para  = None  # marca quando encontrou genérico/similar em vez do produto original
            _principio_ativo_ia = None
            # Essa chamada e sincrona (bloqueia a request, sem paralelismo com o
            # caminho NL) — se a busca ja esta demorando muito, pula a IA em vez
            # de arriscar estourar o timeout da funcao serverless.
            if len(produtos_raw) < 3 and (time.monotonic() - _busca_inicio) < 6.0:
                ia_result = _claude_busca_interpret(busca_q)
                if ia_result:
                    ia_terms = []
                    for _field in ("principios_ativos", "nomes_tecnicos", "categorias", "termos_busca"):
                        for _t in (ia_result.get(_field) or []):
                            _t = _norm_text(_t)
                            # >=4 chars: termos de 2-3 letras (ex: "cr", "gel") geram falso-positivo
                            # em LIKE contra o catalogo inteiro quando a IA "alucina" um termo vago.
                            if _t and len(_t) >= 4 and _t not in ia_terms:
                                ia_terms.append(_t)
                    if ia_terms:
                        _antes_ia = len(produtos_raw)
                        seen_ia = {(p.get("cnpjloja"), p.get("ean")) for p in produtos_raw}
                        for p in get_dns_products_batch_by_name(cnpjs, ia_terms[:6]):
                            key = (p.get("cnpjloja"), p.get("ean"))
                            if key not in seen_ia:
                                produtos_raw.append(p)
                                seen_ia.add(key)
                        ia_eans = []
                        seen_ia_eans = {p.get("ean") for p in produtos_raw}
                        for _ia_term in ia_terms[:3]:
                            for ean in _symptom_index_eans_for_query(_ia_term, limit=60):
                                if re.match(r"^789\d{10}$", ean) and ean not in seen_ia_eans:
                                    ia_eans.append(ean)
                                    seen_ia_eans.add(ean)
                        if ia_eans:
                            for p in get_dns_products_batch_by_eans(cnpjs, ia_eans[:80]):
                                key = (p.get("cnpjloja"), p.get("ean"))
                                if key not in seen_ia:
                                    produtos_raw.append(p)
                                    seen_ia.add(key)
                        # Produtos adicionados via genérico/IA: filtro final deve aceitar esses termos
                        if len(produtos_raw) > _antes_ia:
                            orig_norm = _norm_text(busca_q)
                            ia_filter_terms = ([orig_norm] if orig_norm not in ia_terms else []) + ia_terms
                            # Sinaliza que exibimos alternativa genérica (não o produto exato buscado)
                            _principio_ativo_ia = (ia_result.get("principios_ativos") or ia_terms)[:1]
                            _principio_ativo_ia = _principio_ativo_ia[0] if _principio_ativo_ia else None
                            if _antes_ia == 0:
                                _alternativa_para = busca_q

            # Complementa busca direta nos estoques individuais das lojas
            low_value_terms = {
                "febre", "dor", "antitermico", "antitermica", "analgesico",
                "gripe", "resfriado", "tosse", "nariz", "garganta",
            }
            extra_lookup_terms = search_terms[:1]
            if len(search_terms) > 1:
                extra_lookup_terms = [t for t in search_terms[1:] if t not in low_value_terms][:1]
                if not extra_lookup_terms:
                    extra_lookup_terms = search_terms[1:2]
            seen_search = {(p.get("cnpjloja"), p.get("ean")) for p in produtos_raw}
            # Sempre complementa com busca por nome — o índice de sintomas cobre sintomas/INN
            # mas não cobre todos os nomes comerciais presentes no estoque de cada loja.
            for cnpj in cnpjs[:8]:
                for term in extra_lookup_terms:
                    try:
                        for p in get_dns_products(cnpj, term, skip_image_filter=True)[:80]:
                            key = (p.get("cnpjloja") or cnpj, p.get("ean"))
                            if key in seen_search:
                                continue
                            p = {**p, "cnpjloja": cnpj}
                            produtos_raw.append(p)
                            seen_search.add(key)
                    except Exception:
                        continue
    else:
        produtos_raw = get_dns_products_batch(cnpjs)

    if _catalogo_alpha_exclusivo() and busca_q:
        seen_alpha_direct = {(p.get("cnpjloja"), p.get("ean")) for p in produtos_raw}
        for p in get_alpha_products_direct_by_query(cnpjs, busca_q):
            key = (p.get("cnpjloja"), p.get("ean"))
            if key not in seen_alpha_direct:
                produtos_raw.append(p)
                seen_alpha_direct.add(key)

    # Filtro server-side de categoria (mesmos aliases que o JS usa)
    if cat_filter:
        _CAT_ALIAS_SRV = {
            "cosmetico": "perfumaria", "higiene": "perfumaria",
            "correlato": "varejo", "outros": "varejo",
            "alimento": "nutricao",
        }
        # "medicamento" é superset dos subtipos de remédio; os subtipos
        # (generico/similar/referencia) continuam filtrando de forma exata.
        _MED_TIPOS = {"medicamento", "generico", "similar", "referencia"}
        def _cat_ok(p):
            c = (p.get("categoria") or "").lower()
            c = _CAT_ALIAS_SRV.get(c, c)
            if cat_filter == "medicamento":
                return c in _MED_TIPOS
            return c == cat_filter
        produtos_raw = [p for p in produtos_raw if _cat_ok(p)]

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
        entrega_disponivel = bool(
            aceita_entrega and dist is not None and dist <= raio_entrega
            and _entrega_disponivel_horario_cached(p["cnpjloja"])
        )
        frete_valor = float(info.get("valor_frete") or 0) if entrega_disponivel and info.get("cobra_frete") else 0.0
        entrega_meta = {
            "aceita_entrega": aceita_entrega,
            "raio_entrega_km": raio_entrega,
            "entrega_disponivel": entrega_disponivel,
            "cobra_frete": bool(info.get("cobra_frete")),
            "valor_frete": frete_valor,
        }

        categoria = p.get("categoria") or _classificar_produto(p.get("nome") or "")
        produto_view = {**p, "razao": razao, "logo_url": info.get("logo_url"), "distancia_km": dist, "categoria": categoria, **entrega_meta}

        produtos_view.append(produto_view)

    _attach_product_promos(produtos_view)
    produtos_view = _dedupe_products_for_display(produtos_view)
    _attach_product_symptoms(produtos_view)
    _attach_product_promos(produtos_view)

    if busca_q:
        if is_nl and not ia_filter_terms:
            # IA não retornou a tempo: exibe apenas produtos vindos do índice de sintomas.
            # Evita que a busca ampla por texto mostre produtos irrelevantes (ex: sabonetes para "pressão alta").
            _bq_norm_direct = _norm_text(busca_q)
            produtos_view = [
                p for p in produtos_view
                if p.get("ean") in _nl_ean_src
                or (
                    p.get("fonte_estoque") == "alpha_a7"
                    and _bq_norm_direct
                    and _bq_norm_direct in _norm_text(p.get("nome") or "")
                )
            ]
        else:
            # NL: usa termos da IA (específicos); Direto: usa q_terms expandidos
            filter_terms = ia_filter_terms if ia_filter_terms else _search_terms_for_query(busca_q)
            # Pré-compila padrões: termos curtos (≤4 chars) usam word boundary para não
            # bater em "condor"/"cortador" com "dor", "cor" etc.
            _ft_patterns = []
            for t in filter_terms:
                if len(t) <= 4:
                    _ft_patterns.append(re.compile(r'(?<![a-z])' + re.escape(t) + r'(?![a-z])'))
                else:
                    _ft_patterns.append(t)  # string → substring simples
            filtrados = []
            for p in produtos_view:
                hay_raw = " ".join([
                    p.get("ean") or "",
                    p.get("nome") or "",
                    p.get("razao") or "",
                    p.get("laboratorio") or "",
                    p.get("marca") or "",
                    p.get("categoria") or "",
                    p.get("sintomas") or "",
                    p.get("termos_busca") or "",
                    p.get("principio_ativo") or "",
                    p.get("classe_terapeutica") or "",
                ])
                hay = _norm_text(hay_raw)
                if _product_excluded_for_symptom_query(busca_q, hay_raw):
                    continue
                if any(
                    (pat.search(hay) if hasattr(pat, 'search') else pat in hay)
                    for pat in _ft_patterns
                ):
                    filtrados.append(p)
            produtos_view = filtrados

    # Busca NL (sintomas): remove medicamentos tarjados — eles só aparecem em busca direta por nome.
    # EXCEÇÃO: busca de marca/produto específico (palavra única ou sem palavras de sintoma)
    # ex: "allegra", "dipirona", "losartana" → não remover; "dor de cabeça" → remover
    if is_nl and busca_q:
        _bq_words = _norm_text(busca_q).split()
        _e_busca_sintoma = (
            len(_bq_words) >= 3
            or any(w in _NL_SYMPTOM_WORDS for w in _bq_words)
        )
        if _e_busca_sintoma:
            produtos_view = [
                p for p in produtos_view
                if (p.get("tarja") or "").lower() not in ("vermelha", "preta")
            ]

    result = sorted(produtos_view, key=lambda x: (x.get("distancia_km") is None, x.get("distancia_km") or 0, (x.get("nome") or "").lower()))
    if _catalogo_alpha_exclusivo() and busca_q and not result:
        try:
            q_norm_direct = _norm_text(busca_q)
            ean_direct = _digits(busca_q)
            conn_alpha_direct = db()
            cur_alpha_direct = conn_alpha_direct.cursor()
            cur_alpha_direct.execute(
                """
                SELECT cnpjloja, ean, nome, preco_venda AS preco,
                       CAST(estoque AS INTEGER) AS qty,
                       imagem_url AS imagem,
                       fabricante AS laboratorio,
                       'alpha_a7' AS fonte_estoque
                FROM ecommerce_alpha_produtos
                WHERE cnpjloja = ANY(%s)
                  AND COALESCE(inativo,false)=false
                  AND COALESCE(estoque,0)>0
                  AND (
                    LOWER(COALESCE(nome,'')) LIKE %s
                    OR COALESCE(ean,'') LIKE %s
                  )
                ORDER BY nome
                LIMIT 20
                """,
                (cnpjs, f"%{q_norm_direct}%" if q_norm_direct else "__sem_match__", f"%{ean_direct}%" if ean_direct else "__sem_match__"),
            )
            alpha_rows = [dict(r) for r in cur_alpha_direct.fetchall()]
            cur_alpha_direct.close()
            for p in alpha_rows:
                info = loja_info.get(p["cnpjloja"], {})
                dist = info.get("distancia_km")
                aceita_entrega = bool(info.get("aceita_entrega"))
                raio_entrega = float(info.get("raio_entrega_km") or 0)
                entrega_disponivel = bool(
                    aceita_entrega and dist is not None and dist <= raio_entrega
                    and _entrega_disponivel_horario_cached(p["cnpjloja"])
                )
                result.append({
                    **p,
                    "razao": _public_store_name(info),
                    "distancia_km": dist,
                    "categoria": _classificar_produto(p.get("nome") or ""),
                    "requer_receita": False,
                    "aceita_entrega": aceita_entrega,
                    "raio_entrega_km": raio_entrega,
                    "entrega_disponivel": entrega_disponivel,
                    "cobra_frete": bool(info.get("cobra_frete")),
                    "valor_frete": float(info.get("valor_frete") or 0) if entrega_disponivel and info.get("cobra_frete") else 0.0,
                })
        except Exception as exc:
            app.logger.warning("alpha direct final fallback error: %s", exc)
    saudacao = None
    if is_nl and ia_result:
        saudacao = (ia_result.get("saudacao") or "").strip() or None
    # Quando encontrou alternativa genérica, substitui qualquer saudação do Claude por mensagem
    # neutra — a saudação do Claude tende a mencionar condição médica ("alergia", "dor" etc.)
    # o que caracteriza indicação terapêutica e não pode aparecer no e-commerce de farmácia.
    if _alternativa_para and result:
        _pa_label = (_principio_ativo_ia or "").title() or "genérico"
        saudacao = f"Não encontramos {_alternativa_para.title()} disponível. Exibindo o equivalente genérico encontrado nas farmácias próximas."
    return jsonify({
        "produtos":          result[:500],
        "cnpjs_proximos":    [l["cnpjloja"] for l in proximas],
        "lojas_proximas":    [{"cnpjloja": l["cnpjloja"], "razao": _public_store_name(l)} for l in proximas],
        "fora_raio":         fora_raio,
        "sem_geocode":       sem_geocode,
        "raio_km":           raio,
        "raio_fallback_km":  raio_fallback,
        "n_lojas":           len(proximas),
        "saudacao":          saudacao,
        "alternativa_para":  _alternativa_para,
        "principio_ativo_ia": _principio_ativo_ia,
    })


@app.get("/api/mais-comprados")
@_rate_limited_api(max_calls=30, window_secs=60)
def api_mais_comprados():
    """Produtos mais comprados nas lojas próximas (sem login necessário)."""
    _ensure_logo_url_column()
    try:
        lat_usr = float(request.args.get("lat", 0))
        lng_usr = float(request.args.get("lng", 0))
    except ValueError:
        lat_usr = lng_usr = 0.0

    conn = db(); cur = conn.cursor()
    if lat_usr != 0.0 or lng_usr != 0.0:
        cur.execute("""
            SELECT u.cnpjloja
            FROM users u
            JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
            WHERE u.is_admin = FALSE AND g.lat IS NOT NULL
              AND COALESCE(c.catalogo_publico, TRUE) = TRUE
        """)
        rows_lojas = cur.fetchall()
        cnpjs = [r["cnpjloja"] for r in rows_lojas
                 if haversine(lat_usr, lng_usr, float(r.get("lat") or 0), float(r.get("lng") or 0)) <= 60
                ] if rows_lojas else []
    else:
        cur.execute("""
            SELECT u.cnpjloja FROM users u
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
            WHERE u.is_admin = FALSE AND COALESCE(c.catalogo_publico, TRUE) = TRUE
        """)
        cnpjs = [r["cnpjloja"] for r in cur.fetchall()]

    if not cnpjs:
        cur.close()
        return jsonify({"produtos": []})

    cur.execute("""
        SELECT pi.ean,
               MAX(pi.nome)  AS nome,
               MAX(pi.imagem) AS imagem,
               p.cnpjloja,
               MAX(u.razao) AS razao,
               MAX(c.logo_url) AS logo_url,
               AVG(pi.preco_unitario) AS preco,
               COUNT(*) AS total_vendas
        FROM ecommerce_pedido_itens pi
        JOIN ecommerce_pedidos p ON p.id = pi.pedido_id
        JOIN users u ON u.cnpjloja = p.cnpjloja
        LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
        WHERE p.cnpjloja = ANY(%s)
          AND p.status NOT IN ('cancelado', 'pendente')
          AND pi.imagem IS NOT NULL AND TRIM(pi.imagem) <> ''
          AND COALESCE(pi.ean, '') <> ''
        GROUP BY pi.ean, p.cnpjloja
        ORDER BY total_vendas DESC, MAX(pi.nome)
        LIMIT 20
    """, (cnpjs,))
    produtos = [dict(r) for r in cur.fetchall()]
    cur.close()
    return jsonify({"produtos": produtos})


# ─────────────────────────────────────────────────────────────────────────────
# Home personalizada: lembretes + para você + cross-sell + trending físico
# ─────────────────────────────────────────────────────────────────────────────

_HOME_INSIGHTS_SCHEMA_READY = False

def _ensure_home_insights_schema():
    global _HOME_INSIGHTS_SCHEMA_READY
    if _HOME_INSIGHTS_SCHEMA_READY:
        return
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_home_insights (
                id            SERIAL PRIMARY KEY,
                consumidor_id TEXT NOT NULL,
                tipo          TEXT NOT NULL,
                payload       JSONB NOT NULL DEFAULT '{}',
                criado_em     TIMESTAMPTZ DEFAULT NOW(),
                expira_em     TIMESTAMPTZ
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hins_consumidor_tipo
            ON ecommerce_home_insights(consumidor_id, tipo)
        """)
        conn.commit(); cur.close()
        _HOME_INSIGHTS_SCHEMA_READY = True
    except Exception:
        pass

# Mapeamento de classe farmacológica → lembrete
import re as _med_re

_DRUG_REMINDERS = [
    # (tipo, pattern, dias_lembrete, titulo, msg_template)
    ("anticoncepcional", _med_re.compile(
        r"levonorgestrel|etinilestradiol|desogestrel|gestodeno|dienogeste|drospirenona|"
        r"noretisterona|nogestimato|etonogestrel|acetato de ciproterona",
        _med_re.IGNORECASE,
    ), 26, "Hora de reabastecer",
       "Você comprou {produto} há {dias} dias. Confira se está na hora de repor!"),

    ("antibiotico", _med_re.compile(
        r"amoxicilina|azitromicina|ciprofloxacino|cefalexina|metronidazol|doxiciclina|"
        r"claritromicina|levofloxacino|norfloxacino|ampicilina|sulfametoxazol|nitrofurantoina|"
        r"cefadroxila|tetraciclina|clindamicina|ceftriaxona|moxifloxacino",
        _med_re.IGNORECASE,
    ), 7, "Compra recente",
       "Você comprou {produto} há {dias} dias. Precisando de mais alguma coisa?"),

    ("anti_hipertensivo", _med_re.compile(
        r"losartana|enalapril|anlodipina|amlodipina|atenolol|metoprolol|valsartana|olmesartana|"
        r"hidroclorotiazida|ramipril|lisinopril|carvedilol|bisoprolol|captopril|irbesartana",
        _med_re.IGNORECASE,
    ), 25, "Hora de reabastecer",
       "Você comprou {produto} há {dias} dias. Verifique se está na hora de repor!"),

    ("hipoglicemiante", _med_re.compile(
        r"metformina|glibenclamida|glipizida|glicazida|sitagliptina|empagliflozina|"
        r"dapagliflozina|glimepirida|saxagliptina|canagliflozina",
        _med_re.IGNORECASE,
    ), 25, "Hora de reabastecer",
       "Você comprou {produto} há {dias} dias. Confira se precisa reabastecer!"),

    ("ibp", _med_re.compile(
        r"omeprazol|pantoprazol|esomeprazol|lansoprazol|rabeprazol",
        _med_re.IGNORECASE,
    ), 28, "Hora de reabastecer",
       "Você comprou {produto} há {dias} dias. Hora de verificar o estoque!"),

    ("estatina", _med_re.compile(
        r"sinvastatina|atorvastatina|rosuvastatina|pravastatina|fluvastatina|pitavastatina",
        _med_re.IGNORECASE,
    ), 28, "Hora de reabastecer",
       "Você comprou {produto} há {dias} dias. Confira se precisa repor!"),

    ("tireoide", _med_re.compile(
        r"levotiroxina|levothyroxine",
        _med_re.IGNORECASE,
    ), 28, "Hora de reabastecer",
       "Você comprou {produto} há {dias} dias. Verifique se está na hora de reabastecer!"),

    ("vermifugo", _med_re.compile(
        r"albendazol|mebendazol|tiabendazol",
        _med_re.IGNORECASE,
    ), 180, "Hora de reabastecer",
       "Você comprou {produto} há {dias} dias. Confira se precisa repor!"),
]


def _detectar_tipo_med(nome, principio_ativo):
    texto = f"{nome or ''} {principio_ativo or ''}"
    for tipo, pattern, *_ in _DRUG_REMINDERS:
        if pattern.search(texto):
            return tipo
    return None


def _calcular_lembretes(consumidor_id, conn):
    from datetime import datetime, timezone
    cur = conn.cursor()
    # Busca compras + principio_ativo via anvisa_cache (por chave derivada do nome)
    cur.execute("""
        SELECT DISTINCT ON (pi.ean)
            pi.ean, pi.nome, pi.imagem, pi.preco_unitario AS preco,
            p.criado_em, p.cnpjloja,
            ac.principio_ativo AS pa
        FROM ecommerce_pedido_itens pi
        JOIN ecommerce_pedidos p ON p.id = pi.pedido_id
        LEFT JOIN anvisa_cache ac ON ac.chave = UPPER(REGEXP_REPLACE(
            SPLIT_PART(pi.nome, ' ', 1), '[^A-Za-z]', '', 'g'))
        WHERE p.consumidor_id = %s
          AND p.status NOT IN ('cancelado')
          AND p.criado_em >= NOW() - INTERVAL '7 months'
        ORDER BY pi.ean, p.criado_em DESC
    """, (consumidor_id,))
    compras = [dict(r) for r in cur.fetchall()]
    cur.close()

    agora = datetime.now(timezone.utc)
    lembretes = []
    for compra in compras:
        nome = compra["nome"] or ""
        pa = compra.get("pa") or ""
        tipo = _detectar_tipo_med(nome, pa)
        if not tipo:
            continue
        cfg = next((c for c in _DRUG_REMINDERS if c[0] == tipo), None)
        if not cfg:
            continue
        _, _, dias_lembrete, titulo, msg_tmpl = cfg
        data_compra = compra["criado_em"]
        if data_compra.tzinfo is None:
            data_compra = data_compra.replace(tzinfo=timezone.utc)
        dias = (agora - data_compra).days
        janela_min = int(dias_lembrete * 0.75)
        janela_max = int(dias_lembrete * 2.8)
        if janela_min <= dias <= janela_max:
            nome_curto = " ".join(nome.split()[:4])
            lembretes.append({
                "tipo": tipo,
                "titulo": titulo,
                "mensagem": msg_tmpl.format(produto=nome_curto, dias=dias),
                "ean": compra["ean"],
                "nome": nome,
                "imagem": compra.get("imagem") or "",
                "preco": float(compra.get("preco") or 0),
                "cnpjloja": compra.get("cnpjloja") or "",
                "dias": dias,
            })
    # Um lembrete por tipo (evita duplicatas de genéricos diferentes do mesmo princípio ativo)
    seen_tipo: set = set()
    lembretes_dedup = []
    for _l in lembretes:
        if _l["tipo"] not in seen_tipo:
            seen_tipo.add(_l["tipo"])
            lembretes_dedup.append(_l)
    return lembretes_dedup[:3]


def _recomendacoes_pessoais(consumidor_id, conn, cnpjs_proximos=None):
    """EANs mais comprados pelo consumidor que ainda estão no catálogo das lojas próximas."""
    _ensure_logo_url_column()
    cur = conn.cursor()
    cur.execute("""
        SELECT pi.ean,
               MAX(pi.nome)     AS nome,
               MAX(pi.imagem)   AS imagem,
               p.cnpjloja,
               MAX(u.razao)     AS razao,
               AVG(pi.preco_unitario) AS preco,
               COUNT(*)         AS vezes
        FROM ecommerce_pedido_itens pi
        JOIN ecommerce_pedidos p ON p.id = pi.pedido_id
        JOIN users u ON u.cnpjloja = p.cnpjloja
        WHERE p.consumidor_id = %s
          AND p.status NOT IN ('cancelado')
        GROUP BY pi.ean, p.cnpjloja
        ORDER BY vezes DESC, MAX(p.criado_em) DESC
        LIMIT 20
    """, (consumidor_id,))
    historico = [dict(r) for r in cur.fetchall()]

    if not historico:
        cur.close()
        return []

    eans_norm = list({(r["ean"] or "").lstrip("0") for r in historico if r.get("ean")})
    # Verifica quais estão no estoque atual (prefere registros com imagem)
    cur.execute(f"""
        SELECT DISTINCT ON (LTRIM(e.barras,'0'))
               e.barras AS ean,
               COALESCE(m.descricao, e.descricao) AS nome,
               COALESCE(mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem),'')) AS imagem,
               e.cnpj AS cnpjloja, u.razao, cl.logo_url,
               COALESCE(ep.preco_customizado, e.preco_referencial) AS preco
        FROM estoque e
        LEFT JOIN medicamentos m ON LTRIM(COALESCE(m.barra_norm,m.barra,''),'0') = LTRIM(e.barras,'0')
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        LEFT JOIN produto_canon pc ON LTRIM(COALESCE(pc.ean,''),'0') = LTRIM(e.barras,'0')
            AND pc.fonte NOT IN ('cosmos_miss','ia_miss','placeholder_broken')
        LEFT JOIN users u ON u.cnpjloja = e.cnpj
        LEFT JOIN ecommerce_config_loja cl ON cl.cnpjloja = e.cnpj
        LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = e.cnpj AND ep.ean = e.barras
        WHERE LTRIM(e.barras,'0') = ANY(%s)
          AND e.estoque > 0 AND u.is_admin = FALSE
          {'AND e.cnpj = ANY(%s)' if cnpjs_proximos else ''}
        ORDER BY LTRIM(e.barras,'0'),
                 (COALESCE(mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem),'')) IS NOT NULL) DESC,
                 e.estoque DESC
    """, [eans_norm] + ([cnpjs_proximos] if cnpjs_proximos else []))
    em_catalogo = {dict(r)["ean"].lstrip("0"): dict(r) for r in cur.fetchall()}

    # Também checa automatiza_estoque
    faltam = [e for e in eans_norm if e not in em_catalogo]
    if faltam:
        cur.execute(f"""
            SELECT DISTINCT ON (LTRIM(ae.ean,'0'))
                   ae.ean AS ean,
                   COALESCE(m.descricao, ae.descricao_produto) AS nome,
                   COALESCE(mi.cloudinary_url, NULLIF(TRIM(m.imagem),'')) AS imagem,
                   ae.cnpj_loja AS cnpjloja, u.razao, cl.logo_url,
                   COALESCE(ep.preco_customizado, ae.valor_final_produto) AS preco
            FROM automatiza_estoque ae
            LEFT JOIN medicamentos m ON LTRIM(COALESCE(m.barra_norm,m.barra,''),'0') = LTRIM(ae.ean,'0')
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            LEFT JOIN users u ON u.cnpjloja = ae.cnpj_loja
            LEFT JOIN ecommerce_config_loja cl ON cl.cnpjloja = ae.cnpj_loja
            LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = ae.cnpj_loja AND ep.ean = ae.ean
            WHERE LTRIM(ae.ean,'0') = ANY(%s)
              AND ae.quantidade_estoque > 0 AND u.is_admin = FALSE
              {'AND ae.cnpj_loja = ANY(%s)' if cnpjs_proximos else ''}
            ORDER BY LTRIM(ae.ean,'0'),
                     (COALESCE(mi.cloudinary_url, NULLIF(TRIM(m.imagem),'')) IS NOT NULL) DESC,
                     ae.quantidade_estoque DESC
        """, [faltam] + ([cnpjs_proximos] if cnpjs_proximos else []))
        for r in cur.fetchall():
            r = dict(r)
            ean_k = r["ean"].lstrip("0")
            if ean_k not in em_catalogo:
                em_catalogo[ean_k] = r
    cur.close()

    resultado = []
    for item in historico:
        ean_n = (item["ean"] or "").lstrip("0")
        if ean_n in em_catalogo:
            prod = em_catalogo[ean_n].copy()
            prod["vezes"] = item["vezes"]
            resultado.append(prod)
    return resultado[:10]


def _cross_sell_ia(consumidor_id, historico_nomes, conn, api_key, cnpjs_proximos=None):
    """Sugestões de cross-sell via Claude, cacheadas 7 dias por usuário."""
    from datetime import datetime, timezone, timedelta
    _ensure_home_insights_schema()
    cur = conn.cursor()

    # Verifica cache
    cur.execute("""
        SELECT payload FROM ecommerce_home_insights
        WHERE consumidor_id = %s AND tipo = 'cross_sell_v3' AND expira_em > NOW()
        ORDER BY criado_em DESC LIMIT 1
    """, (consumidor_id,))
    row = cur.fetchone()
    if row:
        _pl = row["payload"]
        if _pl.get("v") == 2:  # v2: objetos completos com imagem/preço
            cur.close()
            return {"mensagem": _pl.get("mensagem", ""), "produtos": _pl.get("produtos", [])}
        # v1 (só nomes) — descarta e refaz para salvar v2

    if not api_key or not historico_nomes:
        cur.close()
        return []

    # Amostra de produtos do catálogo filtrada por lojas próximas
    cnpj_cond = "AND e.cnpj = ANY(%s)" if cnpjs_proximos else ""
    try:
        cur.execute(f"""
            SELECT COALESCE(m.descricao, e.descricao) AS nome
            FROM estoque e
            LEFT JOIN medicamentos m ON LTRIM(COALESCE(m.barra_norm,m.barra,''),'0') = LTRIM(e.barras,'0')
            WHERE e.estoque > 0 AND COALESCE(m.descricao, e.descricao) IS NOT NULL {cnpj_cond}
            ORDER BY CAST(e.estoque AS INTEGER) DESC NULLS LAST
            LIMIT 120
        """, [cnpjs_proximos] if cnpjs_proximos else [])
        catalogo_sample = [r["nome"] for r in cur.fetchall() if r["nome"]]
    except Exception:
        catalogo_sample = []

    if not catalogo_sample:
        cur.close()
        return []

    prompt = (
        f"Histórico de compras do cliente: {', '.join(historico_nomes[:8])}\n\n"
        f"Catálogo disponível:\n" + "\n".join(catalogo_sample[:80]) + "\n\n"
        f"Sugira EXATAMENTE 4 produtos do catálogo mais relevantes para este cliente "
        f"(itens que ele provavelmente compra com frequência ou pode precisar repor). "
        f"Não sugira medicamentos tarjados, controlados ou que exijam receita médica.\n"
        f"Crie também uma frase curta (máx 15 palavras) em português, amigável, sobre CONVENIÊNCIA "
        f"(reposição, economia, praticidade) — NUNCA sobre saúde, tratamento, prevenção ou efeito terapêutico. "
        f"Exemplos aceitos: 'Hora de reabastecer? Separamos o que você já conhece e confia!' "
        f"ou 'Notamos que pode ser hora de repor esses itens do seu carrinho habitual.' "
        f"Exemplos PROIBIDOS: qualquer frase com saúde, bem-estar, potencializar, tratar, prevenir.\n"
        f"Responda APENAS com JSON sem markdown: "
        f'{{\"mensagem\": \"frase aqui\", \"sugestoes\": [\"nome exato 1\", ...]}} '
        f"Use nomes EXATAMENTE como no catálogo."
    )
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={"content-type": "application/json",
                 "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"},
        method="POST",
    )
    mensagem_ia = ""
    sugestoes_nomes = []
    try:
        with urllib.request.urlopen(req, timeout=9) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = "".join(p.get("text","") for p in data.get("content",[]) if p.get("type")=="text").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        _parsed = json.loads(raw.strip())
        mensagem_ia = (_parsed.get("mensagem") or "").strip()
        sugestoes_nomes = _parsed.get("sugestoes", [])[:4]
    except Exception as exc:
        app.logger.warning(f"cross_sell claude error: {exc}")
        cur.close()
        return []

    # Busca produtos reais no catálogo pelos nomes sugeridos
    produtos_cross = []
    for nome_s in sugestoes_nomes:
        palavras = [w for w in nome_s.upper().split() if len(w) >= 4][:3]
        if not palavras:
            continue
        conds = " AND ".join([f"UPPER(COALESCE(m.descricao, e.descricao,'')) LIKE %s"] * len(palavras))
        cnpj_filt = "AND e.cnpj = ANY(%s)" if cnpjs_proximos else ""
        params = [f"%{w}%" for w in palavras] + ([cnpjs_proximos] if cnpjs_proximos else [])
        try:
            cur.execute(f"""
                SELECT DISTINCT ON (LTRIM(e.barras,'0'))
                    e.barras AS ean,
                    COALESCE(m.descricao, e.descricao) AS nome,
                    COALESCE(mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem),'')) AS imagem,
                    e.cnpj AS cnpjloja, u.razao,
                    COALESCE(ep.preco_customizado, e.preco_referencial) AS preco
                FROM estoque e
                LEFT JOIN medicamentos m ON LTRIM(COALESCE(m.barra_norm,m.barra,''),'0') = LTRIM(e.barras,'0')
                LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
                LEFT JOIN produto_canon pc ON LTRIM(COALESCE(pc.ean,''),'0') = LTRIM(e.barras,'0')
                    AND pc.fonte NOT IN ('cosmos_miss','ia_miss','placeholder_broken')
                LEFT JOIN users u ON u.cnpjloja = e.cnpj
                LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = e.cnpj AND ep.ean = e.barras
                WHERE {conds} AND e.estoque > 0 AND u.is_admin = FALSE {cnpj_filt}
                ORDER BY LTRIM(e.barras,'0') LIMIT 1
            """, params)
            r = cur.fetchone()
            if r:
                produtos_cross.append(dict(r))
        except Exception:
            pass

    # Salva cache por 7 dias — v2: objetos completos com imagem e preço.
    # TTL longo para a IA de cross-sell rodar no máximo ~1x por usuário por
    # semana (controle de custo): o histórico de compras muda devagar, então
    # não compensa regenerar a cada dia/login.
    try:
        cur.execute("DELETE FROM ecommerce_home_insights WHERE consumidor_id=%s AND tipo='cross_sell_v3'",
                    (consumidor_id,))
        expira = datetime.now(timezone.utc) + timedelta(days=7)
        _payload_v2 = {
            "v": 2, "mensagem": mensagem_ia,
            "produtos": [
                {"ean": p.get("ean",""), "nome": p.get("nome",""), "imagem": p.get("imagem",""),
                 "preco": float(p.get("preco") or 0), "cnpjloja": p.get("cnpjloja",""),
                 "razao": p.get("razao","")}
                for p in produtos_cross
            ]
        }
        cur.execute("""
            INSERT INTO ecommerce_home_insights (consumidor_id, tipo, payload, expira_em)
            VALUES (%s, 'cross_sell_v3', %s, %s)
        """, (consumidor_id, json.dumps(_payload_v2), expira))
        conn.commit()
    except Exception:
        pass
    cur.close()
    return {"mensagem": mensagem_ia, "produtos": produtos_cross}


def _banner_semana_ia(consumidor_id, nome, historico_nomes, conn, api_key):
    """Gera frase personalizada para o hero da home. Cache 7 dias por usuário."""
    if not api_key or not consumidor_id:
        return ""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT payload FROM ecommerce_home_insights
            WHERE consumidor_id = %s AND tipo = 'banner_semana_v1' AND expira_em > NOW()
            ORDER BY criado_em DESC LIMIT 1
        """, (consumidor_id,))
        row = cur.fetchone()
        if row:
            cur.close()
            return row["payload"].get("frase", "")
    except Exception:
        return ""

    mes = datetime.now().month
    estacoes = {12:"verão", 1:"verão", 2:"verão", 3:"outono", 4:"outono", 5:"outono",
                6:"inverno", 7:"inverno", 8:"inverno", 9:"primavera", 10:"primavera", 11:"primavera"}
    meses_nomes = {1:"janeiro",2:"fevereiro",3:"março",4:"abril",5:"maio",6:"junho",
                   7:"julho",8:"agosto",9:"setembro",10:"outubro",11:"novembro",12:"dezembro"}
    estacao = estacoes.get(mes, "")
    mes_nome = meses_nomes.get(mes, "")
    nome_curto = (nome or "").split()[0] if nome else "cliente"

    historico_str = ", ".join(historico_nomes[:5]) if historico_nomes else ""
    prompt = (
        f"Crie UMA frase de saudação personalizada (máx 18 palavras) para o banner de uma farmácia online.\n"
        f"Cliente: {nome_curto}. Estação: {estacao} ({mes_nome}).\n"
        + (f"Categorias recentes do cliente: {historico_str}.\n" if historico_str else "")
        + f"Fale APENAS sobre: conveniência, economia, praticidade, boas-vindas, ofertas, novidades.\n"
        f"NUNCA mencione: saúde, medicamento, doença, tratamento, remédio, cura, prevenção, terapêutico.\n"
        f"Exemplos aceitos: 'Olá {nome_curto}! Que bom ter você de volta — as melhores ofertas da semana estão aqui.' "
        f"ou 'Bom ver você por aqui, {nome_curto}! Novidades chegaram perto de você nesse {estacao}.'\n"
        f"Responda APENAS com a frase, sem aspas, sem explicação."
    )
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 80,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={"content-type": "application/json",
                 "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"},
        method="POST",
    )
    frase = ""
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        frase = "".join(p.get("text", "") for p in data.get("content", []) if p.get("type") == "text").strip()
        frase = frase.strip('"').strip("'").strip()
    except Exception as exc:
        app.logger.warning(f"banner_ia error: {exc}")
        cur.close()
        return ""

    try:
        cur.execute("DELETE FROM ecommerce_home_insights WHERE consumidor_id=%s AND tipo='banner_semana_v1'",
                    (consumidor_id,))
        expira = datetime.now(timezone.utc) + timedelta(days=7)
        cur.execute("""
            INSERT INTO ecommerce_home_insights (consumidor_id, tipo, payload, expira_em)
            VALUES (%s, 'banner_semana_v1', %s, %s)
        """, (consumidor_id, json.dumps({"frase": frase}), expira))
        conn.commit()
    except Exception:
        pass
    cur.close()
    return frase


def _trending_lojas_fisicas(conn, lat=0.0, lng=0.0, limit=12, cnpjs_proximos=None):
    """Top EANs das lojas físicas (vendageral + automatiza_vendas) que estão no catálogo das lojas próximas."""
    _ensure_logo_url_column()
    cur = conn.cursor()

    # Se não recebeu cnpjs_proximos pré-calculados, resolve aqui —
    # sempre filtra por catalogo_publico=true; 60km quando tiver coord,
    # fallback para todas as ativas se nenhuma estiver próxima.
    if cnpjs_proximos is None:
        try:
            cur.execute("""
                SELECT g.cnpjloja, g.lat, g.lng
                FROM ecommerce_lojas_geo g
                JOIN users u ON u.cnpjloja = g.cnpjloja
                LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = g.cnpjloja
                WHERE u.is_admin = FALSE
                  AND COALESCE(c.catalogo_publico, FALSE) = TRUE
            """)
            lojas_ativas = cur.fetchall()
            todos_ativos = [r["cnpjloja"] for r in lojas_ativas]
            if todos_ativos:
                if lat != 0.0 or lng != 0.0:
                    proximas = [r["cnpjloja"] for r in lojas_ativas
                                if haversine(lat, lng,
                                             float(r["lat"] or 0),
                                             float(r["lng"] or 0)) <= 60]
                    cnpjs_proximos = proximas if proximas else todos_ativos
                else:
                    cnpjs_proximos = todos_ativos
        except Exception:
            cnpjs_proximos = None

    scores = {}
    vg_filter  = "AND cnpj = ANY(%s)"      if cnpjs_proximos else ""
    av_filter  = "AND cnpj_loja = ANY(%s)" if cnpjs_proximos else ""
    vg_params  = [cnpjs_proximos] if cnpjs_proximos else []
    av_params  = [cnpjs_proximos] if cnpjs_proximos else []

    try:
        cur.execute(f"""
            SELECT ean, SUM(CAST(itens AS BIGINT)) AS total
            FROM vendageral
            WHERE ean IS NOT NULL AND ean <> '' AND itens IS NOT NULL {vg_filter}
            GROUP BY ean ORDER BY total DESC LIMIT 600
        """, vg_params)
        for r in cur.fetchall():
            scores[r["ean"]] = scores.get(r["ean"], 0) + int(r["total"] or 0)
    except Exception:
        pass

    try:
        cur.execute(f"""
            SELECT ean, SUM(CAST(quantidade_vendida AS BIGINT)) AS total
            FROM automatiza_vendas
            WHERE ean IS NOT NULL AND ean <> '' AND quantidade_vendida IS NOT NULL {av_filter}
            GROUP BY ean ORDER BY total DESC LIMIT 600
        """, av_params)
        for r in cur.fetchall():
            scores[r["ean"]] = scores.get(r["ean"], 0) + int(r["total"] or 0)
    except Exception:
        pass

    if not scores:
        cur.close()
        return []

    top_eans_norm = [e.lstrip("0") for e in sorted(scores, key=scores.get, reverse=True)[:300]]

    # Busca no catálogo apenas de lojas próximas (com imagem obrigatória)
    cnpj_estoque_cond = "AND e.cnpj = ANY(%s)" if cnpjs_proximos else ""
    estoque_params    = [top_eans_norm, cnpjs_proximos, limit * 4] if cnpjs_proximos else [top_eans_norm, limit * 4]
    try:
        cur.execute(f"""
            SELECT DISTINCT ON (LTRIM(e.barras,'0'))
                e.barras AS ean,
                COALESCE(m.descricao, e.descricao) AS nome,
                COALESCE(mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem),'')) AS imagem,
                e.cnpj AS cnpjloja, u.razao, cl.logo_url,
                COALESCE(ep.preco_customizado, e.preco_referencial) AS preco
            FROM estoque e
            LEFT JOIN medicamentos m ON LTRIM(COALESCE(m.barra_norm,m.barra,''),'0') = LTRIM(e.barras,'0')
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            LEFT JOIN produto_canon pc ON LTRIM(COALESCE(pc.ean,''),'0') = LTRIM(e.barras,'0')
                AND pc.fonte NOT IN ('cosmos_miss','ia_miss','placeholder_broken')
            LEFT JOIN users u ON u.cnpjloja = e.cnpj
            LEFT JOIN ecommerce_config_loja cl ON cl.cnpjloja = e.cnpj
            LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = e.cnpj AND ep.ean = e.barras
            WHERE LTRIM(e.barras,'0') = ANY(%s)
              AND e.estoque > 0 AND u.is_admin = FALSE {cnpj_estoque_cond}
            ORDER BY LTRIM(e.barras,'0'),
                     (COALESCE(mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem),'')) IS NOT NULL) DESC,
                     e.estoque DESC
            LIMIT %s
        """, estoque_params)
        rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        cur.close()
        return []

    cur.close()
    # Filtra apenas com imagem e reordena por score
    rows = [r for r in rows if r.get("imagem")]
    rows.sort(key=lambda r: scores.get(r["ean"], scores.get((r["ean"] or "").lstrip("0"), 0)), reverse=True)
    return rows[:limit]


_cidade_uf_cache: dict = {}


def _reverse_geocode_cidade_uf(lat, lng):
    """Resolve cidade/UF a partir de lat/lng — Google Maps (se GOOGLE_MAPS_KEY
    configurada) com fallback Nominatim. Usado para exibir a cidade do
    consumidor no aviso de "sem parceria aqui" e para casar com o aviso de
    chegada quando uma loja e ativada nessa mesma cidade."""
    try:
        lat_f, lng_f = round(float(lat or 0), 3), round(float(lng or 0), 3)
    except (TypeError, ValueError):
        return None, None
    if not lat_f and not lng_f:
        return None, None
    cache_key = (lat_f, lng_f)
    if cache_key in _cidade_uf_cache:
        return _cidade_uf_cache[cache_key]
    cidade = uf = None
    gm_key = os.getenv("GOOGLE_MAPS_KEY", "").strip()
    if gm_key:
        try:
            url = (f"https://maps.googleapis.com/maps/api/geocode/json?latlng={lat_f},{lng_f}"
                   f"&key={gm_key}&language=pt-BR&result_type=administrative_area_level_2|locality")
            with urllib.request.urlopen(url, timeout=5, context=ssl.create_default_context()) as r:
                data = json.loads(r.read().decode("utf-8"))
            if data.get("status") == "OK" and data.get("results"):
                comps = data["results"][0].get("address_components", [])
                cidade = next((c["long_name"] for c in comps if "administrative_area_level_2" in c["types"]), None) \
                      or next((c["long_name"] for c in comps if "locality" in c["types"]), None)
                uf = next((c["short_name"] for c in comps if "administrative_area_level_1" in c["types"]), None)
        except Exception:
            pass
    if not cidade:
        try:
            url = f"https://nominatim.openstreetmap.org/reverse?lat={lat_f}&lon={lng_f}&format=json&zoom=10&addressdetails=1"
            req = urllib.request.Request(url, headers={"User-Agent": "poupaqui/1.0", "Accept-Language": "pt-BR"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
            addr = data.get("address", {})
            cidade = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("county")
            uf = (addr.get("state_code") or "").replace("BR-", "") or None
        except Exception:
            pass
    _cidade_uf_cache[cache_key] = (cidade, uf)
    return cidade, uf


@app.get("/api/home/insights")
@_rate_limited_api(max_calls=20, window_secs=60)
def api_home_insights():
    """Lembretes de tratamento, recomendações pessoais, cross-sell IA e trending lojas físicas."""
    consumidor_id = session.get("consumidor_id")
    try:
        lat = float(request.args.get("lat", 0))
        lng = float(request.args.get("lng", 0))
    except (ValueError, TypeError):
        lat = lng = 0.0
    home_insights_cache_key = (
        "home_insights",
        str(consumidor_id or "anon"),
        round(lat or 0, 2),
        round(lng or 0, 2),
        bool(_catalogo_alpha_exclusivo()),
    )
    cached_home_insights = _home_api_cache_get(home_insights_cache_key, 180 if consumidor_id else 300)
    if cached_home_insights is not None:
        return jsonify(cached_home_insights)
    resultado = {
        "logado": bool(consumidor_id),
        "lembretes": [],
        "para_voce": [],
        "cross_sell": [],
        "cross_sell_mensagem": "",
        "banner_ia": "",
        "trending_lojas": [],
        "sem_farmacia_proxima": False,
        "cidade": None,
        "uf": None,
    }
    conn = db()

    # Resolve lojas ativas — sempre filtra por catalogo_publico=true.
    # Com localização: prioriza lojas dentro de 60km; se nenhuma estiver
    # próxima, usa todas as ativas como fallback e sinaliza ao frontend.
    cnpjs_proximos = None
    try:
        _cur = conn.cursor()
        _cur.execute("""
            SELECT g.cnpjloja, g.lat, g.lng
            FROM ecommerce_lojas_geo g
            JOIN users u ON u.cnpjloja = g.cnpjloja
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = g.cnpjloja
            WHERE u.is_admin = FALSE
              AND COALESCE(c.catalogo_publico, FALSE) = TRUE
        """)
        lojas_ativas = _cur.fetchall()
        _cur.close()
        todos_ativos = [r["cnpjloja"] for r in lojas_ativas]
        if todos_ativos:
            if lat != 0.0 or lng != 0.0:
                proximas = [r["cnpjloja"] for r in lojas_ativas
                            if haversine(lat, lng, float(r["lat"] or 0), float(r["lng"] or 0)) <= 60]
                if proximas:
                    cnpjs_proximos = proximas
                else:
                    cnpjs_proximos = todos_ativos
                    resultado["sem_farmacia_proxima"] = True
                    try:
                        cidade, uf = _reverse_geocode_cidade_uf(lat, lng)
                        resultado["cidade"] = cidade
                        resultado["uf"] = uf
                    except Exception:
                        pass
            else:
                cnpjs_proximos = todos_ativos
    except Exception as e:
        app.logger.warning(f"cnpjs_proximos: {e}")

    try:
        if _catalogo_alpha_exclusivo():
            resultado["trending_lojas"] = get_alpha_products_direct(cnpjs_proximos or [], limit=24)
        else:
            resultado["trending_lojas"] = _trending_lojas_fisicas(conn, lat, lng, cnpjs_proximos=cnpjs_proximos)
    except Exception as e:
        app.logger.warning(f"trending_lojas: {e}")

    if consumidor_id:
        try:
            resultado["lembretes"] = _calcular_lembretes(consumidor_id, conn)
        except Exception as e:
            app.logger.warning(f"lembretes: {e}")

        try:
            resultado["para_voce"] = _recomendacoes_pessoais(consumidor_id, conn, cnpjs_proximos)
            if resultado["para_voce"]:
                _marcar_tarja_batch(resultado["para_voce"], conn, ensure_schema=False)
        except Exception as e:
            app.logger.warning(f"para_voce: {e}")

        try:
            api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
            if api_key and resultado["para_voce"]:
                nomes = [p.get("nome", "") for p in resultado["para_voce"][:6] if p.get("nome")]
                _cs = _cross_sell_ia(consumidor_id, nomes, conn, api_key, cnpjs_proximos)
                if isinstance(_cs, dict):
                    resultado["cross_sell"] = _cs.get("produtos", [])
                    resultado["cross_sell_mensagem"] = _cs.get("mensagem", "")
                else:
                    resultado["cross_sell"] = _cs or []
        except Exception as e:
            app.logger.warning(f"cross_sell: {e}")

        try:
            api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
            if api_key:
                nome_consumidor = session.get("consumidor_nome", "")
                nomes_hist = [p.get("nome", "") for p in (resultado["para_voce"] or [])[:5] if p.get("nome")]
                resultado["banner_ia"] = _banner_semana_ia(consumidor_id, nome_consumidor, nomes_hist, conn, api_key)
        except Exception as e:
            app.logger.warning(f"banner_ia: {e}")

    try:
        conn.close()
    except Exception:
        pass
    _home_api_cache_set(home_insights_cache_key, resultado, ttl_seconds=180 if consumidor_id else 300)
    return jsonify(resultado)


# ── Kits: mapeamento tema → regex de busca ─────────────────────────────────────
_KITS_DEF = {
    "gripe": {
        "label": "Kit gripe e resfriado",
        "icon": "fa-head-side-cough",
        "regex": r"gripe|resfriado|antigripal|tosse|ambroxol|descongestionante|soro nasal|paracetamol|dipirona",
    },
    "bebe": {
        "label": "Kit bebê",
        "icon": "fa-baby",
        "regex": r"fralda|lenco umedecid|pomada.*frald|frald.*pomada|termometro|alcool.*beb|beb.*alcool|talco.*beb|shampoo.*beb|sabonete.*beb",
    },
    "pele": {
        "label": "Kit cuidados com a pele",
        "icon": "fa-spa",
        "regex": r"protetor solar|hidratante facial|vitamina e |colageno|sabonete facial|retinol|antissinais|acido.*hialur",
    },
}


@app.get("/api/kit-produtos")
@_rate_limited_api(max_calls=30, window_secs=60)
def api_kit_produtos():
    """Retorna produtos disponíveis nas lojas próximas para montagem de kit."""
    tema = request.args.get("tema", "").strip().lower()
    kit = _KITS_DEF.get(tema)
    if not kit:
        return jsonify({"erro": "tema inválido", "produtos": []}), 400

    try:
        lat = float(request.args.get("lat", 0))
        lng = float(request.args.get("lng", 0))
    except (ValueError, TypeError):
        lat = lng = 0.0

    conn = db()
    cur = conn.cursor()

    # Resolve lojas ativas; 60km quando tiver coord, fallback para todas ativas
    cnpjs_proximos = None
    try:
        cur.execute("""
            SELECT g.cnpjloja, g.lat, g.lng
            FROM ecommerce_lojas_geo g
            JOIN users u ON u.cnpjloja = g.cnpjloja
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = g.cnpjloja
            WHERE u.is_admin = FALSE
              AND COALESCE(c.catalogo_publico, FALSE) = TRUE
        """)
        lojas_ativas = cur.fetchall()
        todos_ativos = [r["cnpjloja"] for r in lojas_ativas]
        if todos_ativos:
            if lat != 0.0 or lng != 0.0:
                proximas = [r["cnpjloja"] for r in lojas_ativas
                            if haversine(lat, lng, float(r["lat"] or 0), float(r["lng"] or 0)) <= 60]
                cnpjs_proximos = proximas if proximas else todos_ativos
            else:
                cnpjs_proximos = todos_ativos
    except Exception as e:
        app.logger.warning(f"kit_produtos cnpjs: {e}")

    cnpj_cond   = "AND e.cnpj = ANY(%s)" if cnpjs_proximos else ""
    params_list = [kit["regex"]]
    if cnpjs_proximos:
        params_list.append(cnpjs_proximos)
    params_list.append(40)

    try:
        cur.execute(f"""
            SELECT DISTINCT ON (LTRIM(e.barras,'0'))
                e.barras AS ean,
                COALESCE(m.descricao, e.descricao) AS nome,
                COALESCE(mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem),'')) AS imagem,
                e.cnpj AS cnpjloja,
                u.razao,
                COALESCE(ep.preco_customizado, e.preco_referencial) AS preco
            FROM estoque e
            LEFT JOIN medicamentos m
                ON LTRIM(COALESCE(m.barra_norm, m.barra,''),'0') = LTRIM(e.barras,'0')
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            LEFT JOIN produto_canon pc
                ON LTRIM(COALESCE(pc.ean,''),'0') = LTRIM(e.barras,'0')
               AND pc.fonte NOT IN ('cosmos_miss','ia_miss','placeholder_broken')
            LEFT JOIN users u ON u.cnpjloja = e.cnpj
            LEFT JOIN ecommerce_precos ep ON ep.cnpjloja = e.cnpj AND ep.ean = e.barras
            WHERE LOWER(COALESCE(m.descricao, e.descricao)) ~ %s
              AND e.estoque > 0 AND u.is_admin = FALSE
              AND COALESCE(mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem),'')) IS NOT NULL
              {cnpj_cond}
            ORDER BY LTRIM(e.barras,'0'),
                     e.estoque DESC
            LIMIT %s
        """, params_list)
        rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        app.logger.warning(f"kit_produtos query: {e}")
        cur.close()
        return jsonify({"label": kit["label"], "icon": kit["icon"], "produtos": []})

    cur.close()
    rows = [r for r in rows if r.get("imagem") and float(r.get("preco") or 0) > 0]
    return jsonify({"label": kit["label"], "icon": kit["icon"], "produtos": rows[:10]})


@app.get("/api/comprar-novamente")
def api_comprar_novamente():
    """Produtos de pedidos anteriores do consumidor logado."""
    consumidor_id = session.get("consumidor_id")
    if not consumidor_id:
        return jsonify({"produtos": [], "logado": False})

    try:
        lat_usr = float(request.args.get("lat", 0))
        lng_usr = float(request.args.get("lng", 0))
    except ValueError:
        lat_usr = lng_usr = 0.0

    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ON (pi.ean)
               pi.ean, pi.nome, pi.imagem, pi.preco_unitario AS preco,
               p.cnpjloja, u.razao, p.criado_em
        FROM ecommerce_pedido_itens pi
        JOIN ecommerce_pedidos p ON p.id = pi.pedido_id
        JOIN users u ON u.cnpjloja = p.cnpjloja
        WHERE p.consumidor_id = %s
          AND p.status NOT IN ('cancelado')
          AND pi.imagem IS NOT NULL AND TRIM(pi.imagem) <> ''
          AND COALESCE(pi.ean, '') <> ''
        ORDER BY pi.ean, p.criado_em DESC
        LIMIT 20
    """, (consumidor_id,))
    produtos = [dict(r) for r in cur.fetchall()]
    cur.close()
    return jsonify({"produtos": produtos, "logado": True})


@app.get("/api/loja/<cnpj>/reputacao")
@_rate_limited_api(max_calls=60, window_secs=60)
def api_reputacao_loja(cnpj):
    """Reputação pública de uma loja."""
    rep = _reputacao_loja(cnpj)
    if rep is None:
        return jsonify({"suficiente": False})
    return jsonify({"suficiente": True, **rep})


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
        placeholder_generico = None
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

    if nome_busca:
        try:
            _anvisa_schema()
            chave_anv = _anvisa_chave(nome_busca)
            if chave_anv:
                cur.execute(
                    "SELECT alertas, como_usar, nome_anvisa, principio_ativo, tarja, receita_retida, "
                    "exibir_imagem_publica, dizeres_receita, dizeres_imagem "
                    "FROM anvisa_cache WHERE chave=%s AND encontrado=TRUE LIMIT 1",
                    (chave_anv,),
                )
                anvisa_img = dict(cur.fetchone() or {})
                tarja_img = _detectar_tarja(anvisa_img)
                _tipo_busca = _classificar_produto(nome_busca)
                _is_med_busca = _tipo_busca not in _TIPOS_NAO_MEDICAMENTO
                if tarja_img in ("preta", "vermelha") and _is_med_busca:
                    result["imagem_med"] = _placeholder_for_tarja(tarja_img) or result.get("imagem_med")
                result["tarja"] = tarja_img
                result["receita_retida"] = _exige_receita_digital_entrega(anvisa_img, nome_busca)
        except Exception:
            pass

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
    _ensure_gateway_alt_columns()
    _ensure_assinatura_schema()
    _ensure_logo_url_column()
    cnpjs = [c.strip() for c in request.args.get("cnpjs", "").split(",") if c.strip()]
    if not cnpjs:
        return jsonify({})
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.cnpjloja, u.razao, u.endereco, u.telefone, c.logo_url,
               COALESCE(c.whatsapp_pedidos, u.telefone) AS whatsapp_pedidos,
               COALESCE(c.aceita_whatsapp, FALSE)       AS aceita_whatsapp,
               COALESCE(c.aceita_pix, TRUE)             AS aceita_pix,
               COALESCE(c.aceita_mp, FALSE)             AS aceita_mp,
               COALESCE(c.gateway_alternativo, 'mercadopago') AS gateway_alternativo,
               (COALESCE(c.gateway_alternativo, 'mercadopago')='mercadopago' AND COALESCE(c.mp_access_token,'')<>'')
                OR (COALESCE(c.gateway_alternativo, 'mercadopago')='asaas' AND COALESCE(c.asaas_api_key,'')<>'')
                OR (COALESCE(c.gateway_alternativo, 'mercadopago')='pagbank' AND COALESCE(c.pagbank_token,'')<>'' AND COALESCE(c.pagbank_public_key,'')<>'')
                AS gateway_configurado,
               COALESCE(c.aceita_entrega, FALSE)        AS aceita_entrega,
               COALESCE(c.raio_entrega_km, 0)           AS raio_entrega_km,
               COALESCE(c.cobra_frete, FALSE)           AS cobra_frete,
               COALESCE(c.valor_frete, 0)               AS valor_frete,
               COALESCE(c.pedido_minimo_entrega, 0)     AS pedido_minimo_entrega,
               (c.pix_chave IS NOT NULL AND c.pix_chave <> '') AS tem_pix,
               g.lat AS loja_lat, g.lng AS loja_lng,
               p.nome AS plano_nome, p.descricao AS plano_descricao,
               p.preco_mensal AS plano_preco_mensal, p.beneficios AS plano_beneficios,
               COALESCE(p.ativo, FALSE) AS plano_ativo
        FROM users u
        LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
        LEFT JOIN ecommerce_lojas_geo g   ON g.cnpjloja = u.cnpjloja
        LEFT JOIN ecommerce_planos_assinatura p ON p.cnpjloja = u.cnpjloja
        WHERE u.cnpjloja = ANY(%s)
        """,
        (cnpjs,),
    )
    rows = cur.fetchall()

    consumidor_id = session.get("consumidor_id")
    frete_gratis_disponivel = set()
    assinados = set()
    pendentes = set()
    if consumidor_id:
        cur.execute(
            """
            SELECT a.cnpjloja FROM ecommerce_assinantes a
            JOIN ecommerce_planos_assinatura p ON p.cnpjloja = a.cnpjloja
            WHERE a.consumidor_id=%s AND a.cnpjloja = ANY(%s)
              AND a.status='ativo' AND a.pagamento_status='aprovado'
              AND COALESCE(p.frete_gratis_primeira_entrega, FALSE) = TRUE
              AND COALESCE(a.frete_gratis_primeira_usado, FALSE) = FALSE
            """,
            (consumidor_id, cnpjs),
        )
        frete_gratis_disponivel = {r["cnpjloja"] for r in cur.fetchall()}

        cur.execute(
            """
            SELECT cnpjloja, status FROM ecommerce_assinantes
            WHERE consumidor_id=%s AND cnpjloja = ANY(%s)
              AND (data_fim IS NULL OR data_fim > NOW())
            """,
            (consumidor_id, cnpjs),
        )
        for r in cur.fetchall():
            if r["status"] == "ativo":
                assinados.add(r["cnpjloja"])
            elif r["status"] == "aguardando_pagamento":
                pendentes.add(r["cnpjloja"])
    cur.close()

    data = {}
    for r in rows:
        item = dict(r)
        item["razao"] = _public_store_name(item)
        _hs = _status_horario_entrega(r["cnpjloja"])
        item["entrega_disponivel_horario"] = _hs.get("entrega_disponivel_horario", True)
        item["loja_aberta"] = _hs.get("aberta", True)
        item["feriado_hoje"] = _hs.get("feriado_hoje")
        item["proximo_dia_entrega"] = _hs.get("proximo_dia_entrega")
        item["frete_gratis_assinante_disponivel"] = r["cnpjloja"] in frete_gratis_disponivel
        item["ja_assina"] = r["cnpjloja"] in assinados
        item["assinatura_pendente"] = r["cnpjloja"] in pendentes
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


def _build_recommendations(itens, cnpjlojas, limit=8, offset=0):
    _ensure_catalog_admin_schema()
    limit = max(1, min(int(limit or 8), 20))
    offset = max(0, int(offset or 0))
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
    skipped = 0
    for _, from_history, p in ranked:
        ean = p.get("ean")
        if ean in seen:
            continue
        seen.add(ean)
        if skipped < offset:
            skipped += 1
            continue
        item = dict(p)
        item["razao"] = loja_info.get(item.get("cnpjloja"), item.get("razao") or "Drogaria Poupaqui")
        item["categoria"] = item.get("categoria") or _classificar_produto(item.get("nome") or "")
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


def _claude_haiku(prompt: str, max_tokens: int = 300, timeout: int = 5) -> str | None:
    """Chama Claude Haiku e retorna o texto da resposta ou None em caso de erro."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
        method="POST",
    )
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            data_resp = json.loads(r.read().decode("utf-8"))
        return (data_resp.get("content") or [{}])[0].get("text", "").strip()
    except Exception:
        return None


@app.get("/api/home/economia-ia")
def api_home_economia_ia():
    """Retorna insight gerado por Claude + top produtos com maior economia da região."""
    try:
        lat = float(request.args.get("lat", 0))
        lng = float(request.args.get("lng", 0))
    except (ValueError, TypeError):
        lat, lng = 0.0, 0.0

    conn = db()
    cur = conn.cursor()
    try:
        if lat != 0.0 and lng != 0.0:
            cur.execute(
                """
                SELECT u.cnpjloja FROM users u
                JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
                LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
                WHERE u.is_admin = FALSE AND g.lat IS NOT NULL
                  AND COALESCE(c.catalogo_publico, TRUE) = TRUE
                  AND (
                    6371 * acos(LEAST(1.0, cos(radians(%s))*cos(radians(g.lat::float))*cos(radians(g.lng::float)-radians(%s))+sin(radians(%s))*sin(radians(g.lat::float))))
                  ) <= 60
                LIMIT 20
                """,
                (lat, lng, lat),
            )
        else:
            cur.execute(
                """SELECT u.cnpjloja FROM users u
                   LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
                   WHERE u.is_admin = FALSE AND COALESCE(c.catalogo_publico, TRUE) = TRUE
                   LIMIT 10"""
            )
        cnpjs = [r["cnpjloja"] for r in cur.fetchall()]
    except Exception:
        cnpjs = []
    finally:
        cur.close()
        conn.close()

    if not cnpjs:
        return jsonify({"insight_ia": "Compare preços e economize na sua saúde.", "produtos": [], "economia_total": 0})

    todos = get_dns_products_batch(cnpjs)

    _nomes_lixo = {"sem descr", "sem descrição", "sem nome", "produto", "item", ""}
    validos = [
        p for p in todos
        if p.get("imagem")
        and (float(p.get("preco") or 0)) > 0
        and (float(p.get("preco") or 0)) <= 300       # filtra preços absurdos
        and (p.get("nome") or "").strip().lower() not in _nomes_lixo
        and len((p.get("nome") or "").strip()) >= 5
    ]

    # Agrupa por EAN para encontrar maior diferença de preço entre lojas
    por_ean: dict = {}
    for p in validos:
        ean = str(p.get("ean") or "").strip()
        if not ean:
            continue
        por_ean.setdefault(ean, []).append(p)

    with_savings = []
    for ean, lista in por_ean.items():
        if len(lista) < 2:
            continue
        lista.sort(key=lambda x: float(x.get("preco") or 0))
        melhor = lista[0]
        pior   = lista[-1]
        eco = float(pior.get("preco") or 0) - float(melhor.get("preco") or 0)
        if eco > 0.5:
            with_savings.append({**melhor, "economia": round(eco, 2)})

    with_savings.sort(key=lambda x: -x["economia"])
    top = with_savings[:3]

    # Fallback: produtos populares (mais estoque) em faixa de preço razoável (R$ 10–150)
    if len(top) < 3:
        eans_top = {t.get("ean") for t in top}
        resto = [
            p for p in validos
            if p.get("ean") not in eans_top
            and 10 <= float(p.get("preco") or 0) <= 150
        ]
        resto.sort(key=lambda x: -(int(x.get("qty") or 0)))
        for p in resto:
            if len(top) >= 3:
                break
            top.append({**p, "economia": 0.0})

    economia_total = sum(t.get("economia", 0) for t in top)

    # Claude gera o insight
    nomes_str = "; ".join(t.get("nome", "") for t in top[:3] if t.get("nome"))
    insight_ia = None
    if nomes_str:
        # Cache compartilhado por conjunto de produtos (mem + DB, 3 dias). O
        # insight é o mesmo para todos os visitantes daquela região, então a
        # IA roda no máximo uma vez a cada 3 dias por combinação de produtos —
        # antes chamava o Claude a cada carregamento da home (logado ou não).
        _eco_cache_key = f"economia_ia:{_norm_query_cache(nomes_str)}"
        _eco_cached = _busca_cache_get(_eco_cache_key)
        if isinstance(_eco_cached, dict) and _eco_cached.get("insight"):
            insight_ia = _eco_cached["insight"]
        else:
            prompt = (
                "Você é Poupinha, a IA da rede de farmácias Poupaqui. "
                f"Você encontrou estes produtos com bom preço na região do cliente: {nomes_str}. "
                f"A economia potencial é de R$ {economia_total:.2f}. "
                "Gere UMA frase curta e animada (máximo 120 caracteres) dizendo que o cliente pode economizar "
                "comprando esses produtos no Poupaqui. Seja direto, simpático e em português. Sem emojis. "
                "Retorne SOMENTE a frase, sem aspas nem explicações."
            )
            insight_ia = _claude_haiku(prompt, max_tokens=80, timeout=5)
            if insight_ia:
                _busca_cache_set(_eco_cache_key, {"insight": insight_ia})

    if not insight_ia:
        insight_ia = f"Compare preços e economize até {_format_brl(economia_total)} nos produtos mais procurados da sua região." if economia_total > 0 else "Confira os melhores preços nas farmácias perto de você."

    result = [
        {
            "ean": t.get("ean"),
            "nome": t.get("nome"),
            "imagem": t.get("imagem"),
            "preco": t.get("preco"),
            "razao": t.get("razao") or "Drogaria Poupaqui",
            "cnpjloja": t.get("cnpjloja"),
            "economia": t.get("economia", 0),
        }
        for t in top
    ]
    return jsonify({"insight_ia": insight_ia, "produtos": result, "economia_total": round(economia_total, 2)})


def _format_brl(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


_BULA_GARBAGE_RE = re.compile(
    r"GENÉRICO\s*[–\-]\s*GENÉRICO|RDC\s+de\s+Bula|de\s+Bula\s*[–\-]\s*RDC|"
    r"\b\d{4,}\s*[–\-]\s*\d{4,}\b|Atualização\s+do\s+texto\s+de\s+bula|"
    r"REAÇÕES\s+ADVERSAS\s+III|Instrução\s+Normativa\s+n",
    re.IGNORECASE,
)


def _is_bula_garbage(text: str | None) -> bool:
    if not text or len(text.strip()) < 20:
        return True
    return bool(_BULA_GARBAGE_RE.search(text))


def _enriquecer_descricao_ia(chave_anvisa: str, nome: str, principio_ativo: str,
                              serve_para: str, como_usar: str, alertas: str,
                              conn, api_key: str, anvisa_tarja: str = "") -> dict:
    """Gera/atualiza para_que_serve_ia e como_tomar_ia no anvisa_cache. Cache 1 ano."""
    from datetime import datetime, timezone, timedelta
    import json as _json

    # Verifica cache (1 ano)
    cur = conn.cursor()
    cur.execute(
        "SELECT para_que_serve_ia, como_tomar_ia, ia_descricao_gerado_em, "
        "tarja_ia, exibir_imagem_publica "
        "FROM anvisa_cache WHERE chave=%s",
        (chave_anvisa,),
    )
    row = cur.fetchone()

    _serve_ia        = row["para_que_serve_ia"]     if row else None
    _usar_ia         = row["como_tomar_ia"]         if row else None
    _gerado          = row["ia_descricao_gerado_em"] if row else None
    _tarja_ia_cached = row["tarja_ia"]              if row else None
    _exibir_cached   = row["exibir_imagem_publica"] if row else None

    _tarja_validado  = _tarja_ia_cached is not None
    _exibir_validado = _exibir_cached   is not None

    # Se cache válido (menos de 1 ano) e TODOS os campos já estão preenchidos, retorna direto
    if _serve_ia and _usar_ia and _tarja_validado and _exibir_validado and _gerado:
        if _gerado.tzinfo is None:
            _gerado = _gerado.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - _gerado) < timedelta(days=365):
            cur.close()
            return {
                "para_que_serve_ia": _serve_ia,
                "como_tomar_ia": _usar_ia,
                "tarja_ia": _tarja_ia_cached,
                "exibir_imagem_ia": _exibir_cached,
            }

    # Decide se as descrições precisam ser melhoradas
    serve_ok  = not _is_bula_garbage(serve_para)
    usar_ok   = not _is_bula_garbage(como_usar)

    # Só pula IA se descrições boas E tarja/exibir já foram validados
    if serve_ok and usar_ok and _tarja_validado and _exibir_validado:
        cur.close()
        return {}

    # Tem IA já gerada e cache válido (só chegou aqui porque 1 campo ainda é garbage)
    # retorna o que já existe para não perder dados de um campo que foi corrigido
    if _serve_ia or _usar_ia:
        serve_ainda_ruim = not serve_ok and not _serve_ia
        usar_ainda_ruim  = not usar_ok  and not _usar_ia
        if not serve_ainda_ruim and not usar_ainda_ruim:
            cur.close()
            return {"para_que_serve_ia": _serve_ia or "", "como_tomar_ia": _usar_ia or ""}

    # Monta contexto para a IA
    serve_ctx = serve_para if serve_ok else "(dado inválido no sistema)"
    usar_ctx  = como_usar  if usar_ok  else "(dado inválido no sistema)"

    tarja_anvisa = (anvisa_tarja or "").strip().lower()

    prompt = (
        f"Produto de farmácia: {nome}\n"
        f"Princípio ativo / substância: {principio_ativo or 'não informado'}\n"
        f"Tarja registrada no sistema: {tarja_anvisa or 'não informada'}\n\n"
        f"Dados existentes (podem estar incorretos ou vazios):\n"
        f"- Para que serve: {serve_ctx[:400]}\n"
        f"- Como usar/tomar: {usar_ctx[:400]}\n\n"
        f"Com base no nome e princípio ativo, gere descrições úteis para o consumidor.\n"
        f"Responda SOMENTE em JSON com as chaves:\n"
        f"  \"para_que_serve\": 2-3 frases objetivas sobre para que o produto é indicado\n"
        f"  \"como_tomar\": 2-3 frases sobre modo de uso geral (sem posologia exata)\n"
        f"  \"tarja\": \"preta\", \"vermelha\" ou \"sem_tarja\" (OTC/suplemento/isento)\n"
        f"  \"tarja_confianca\": \"alta\", \"media\" ou \"baixa\"\n"
        f"  \"exibir_imagem\": true se é marca amplamente conhecida com imagem pública "
        f"(ex: Advil, Buscopan, Engov, Centrum, marcas OTC famosas), false caso contrário\n"
        f"Use linguagem simples e direta para o consumidor final. "
        f"Não faça promessas de cura ou diagnóstico."
    )

    try:
        # Usa o mesmo cliente HTTP das demais chamadas Claude (api.anthropic.com).
        # Antes usava o SDK `anthropic`, que não está no requirements e por isso
        # falhava silenciosamente em produção (e gerava o aviso de import).
        raw = (_claude_haiku(prompt, max_tokens=300) or "").strip()
        # Extrai JSON mesmo se vier com markdown
        _m = re.search(r"\{[\s\S]+\}", raw)
        data = _json.loads(_m.group(0)) if _m else {}
        novo_serve      = (data.get("para_que_serve") or "").strip() or None
        novo_usar       = (data.get("como_tomar") or "").strip() or None
        novo_tarja      = (data.get("tarja") or "").strip().lower() or None
        novo_conf       = (data.get("tarja_confianca") or "").strip().lower() or None
        _exibir_raw     = data.get("exibir_imagem")
        # IA só confirma que PODE exibir (true) — nunca usa false para bloquear imagem
        novo_exibir     = True if _exibir_raw is True else None
        if novo_tarja not in ("preta", "vermelha", "sem_tarja"):
            novo_tarja = None
        if novo_conf not in ("alta", "media", "baixa"):
            novo_conf = None
    except Exception as e:
        app.logger.warning(f"_enriquecer_descricao_ia: {e}")
        cur.close()
        return {}

    if novo_serve or novo_usar or novo_tarja or novo_exibir is not None:
        try:
            cur.execute(
                """INSERT INTO anvisa_cache (chave, encontrado, para_que_serve_ia, como_tomar_ia,
                       tarja_ia, tarja_ia_confianca, exibir_imagem_publica, ia_descricao_gerado_em)
                   VALUES (%s, FALSE, %s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (chave) DO UPDATE SET
                       para_que_serve_ia      = COALESCE(%s, anvisa_cache.para_que_serve_ia),
                       como_tomar_ia          = COALESCE(%s, anvisa_cache.como_tomar_ia),
                       tarja_ia               = COALESCE(%s, anvisa_cache.tarja_ia),
                       tarja_ia_confianca     = COALESCE(%s, anvisa_cache.tarja_ia_confianca),
                       exibir_imagem_publica  = COALESCE(anvisa_cache.exibir_imagem_publica, %s),
                       ia_descricao_gerado_em = NOW()""",
                (chave_anvisa, novo_serve, novo_usar, novo_tarja, novo_conf, novo_exibir,
                 novo_serve, novo_usar, novo_tarja, novo_conf, novo_exibir),
            )
            conn.commit()
        except Exception as e:
            app.logger.warning(f"_enriquecer_descricao_ia save: {e}")
            try: conn.rollback()
            except Exception: pass

    cur.close()
    return {
        "para_que_serve_ia": novo_serve,
        "como_tomar_ia": novo_usar,
        "tarja_ia": novo_tarja,
        "tarja_ia_confianca": novo_conf,
        "exibir_imagem_ia": novo_exibir,
    }


@app.get("/produto/<ean>")
def produto_detalhe(ean):
    cnpjloja  = (request.args.get("cnpj")  or "").strip()
    nome_hint = (request.args.get("nome") or "").strip()
    conn = db()
    cur  = conn.cursor()

    cur.execute(
        """
        SELECT m.descricao, m.marca, m.laboratorio, m.classe,
               COALESCE(epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem
        FROM medicamentos m
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        LEFT JOIN produto_canon pc ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0')
                                  AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
        LEFT JOIN LATERAL (
            SELECT imagem_url
            FROM ecommerce_produto_imagens
            WHERE LTRIM(COALESCE(ean, ''), '0') = LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0')
              AND imagem_url IS NOT NULL AND TRIM(imagem_url) <> ''
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 1
        ) epi ON TRUE
        LEFT JOIN medicamentos5 m5 ON m5.barra = COALESCE(m.barra_norm, m.barra)
        WHERE LTRIM(COALESCE(m.barra_norm,''),'0') = LTRIM(%s,'0')
           OR LTRIM(COALESCE(m.barra,''),'0')      = LTRIM(%s,'0')
        ORDER BY (m.barra_norm IS NOT NULL) DESC, m.id
        LIMIT 1
        """,
        (ean, ean),
    )
    med = cur.fetchone()

    # Fallback: produto sem entrada em medicamentos pode ter descricao/imagem em produto_canon
    _descricao_canon = None
    if not med:
        cur.execute(
            "SELECT descricao_canon FROM produto_canon "
            "WHERE LTRIM(COALESCE(ean,''),'0') = LTRIM(%s,'0') "
            "  AND descricao_canon IS NOT NULL "
            "  AND fonte NOT IN ('cosmos_miss','ia_miss','placeholder_broken') "
            "LIMIT 1",
            (ean,),
        )
        _row_canon = cur.fetchone()
        _descricao_canon = _row_canon["descricao_canon"] if _row_canon else None

    nome_busca = nome_hint or (med["descricao"] if med else _descricao_canon or "")
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
        _ensure_logo_url_column()
        cur.execute(
            """SELECT u.cnpjloja, u.razao, u.endereco, u.uf, u.telefone, c.logo_url
               FROM users u LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
               WHERE u.cnpjloja=%s AND u.is_admin=FALSE LIMIT 1""",
            (cnpjloja,),
        )
        loja = cur.fetchone()
        if loja:
            loja = dict(loja)
            loja["razao"] = _public_store_name(loja)

        cur.execute(
            """
            SELECT imagem_url
            FROM ecommerce_produto_imagens
            WHERE cnpjloja=%s
              AND LTRIM(COALESCE(ean, ''), '0') = LTRIM(%s, '0')
              AND imagem_url IS NOT NULL AND TRIM(imagem_url) <> ''
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 1
            """,
            (cnpjloja, ean),
        )
        row_img = cur.fetchone()
        imagem_custom = row_img["imagem_url"] if row_img else None
        # Placeholder genérico salvo como imagem_custom é dado obsoleto — ignorar
        if imagem_custom in _MEDICINE_PLACEHOLDER_URLS:
            imagem_custom = None

        if _alpha_enabled():
            try:
                _ensure_alpha_schema()
                cur.execute(
                    """
                    SELECT ap.ean, COALESCE(m.descricao, pc.descricao_canon, ap.nome) AS nome,
                           CAST(ap.estoque AS INTEGER) AS qty,
                           ap.preco_venda AS preco,
                           COALESCE(%s, ap.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), epi.imagem_url) AS imagem,
                           'alpha_a7' AS fonte_estoque,
                           ap.alpha_o_id
                    FROM ecommerce_alpha_produtos ap
                    LEFT JOIN medicamentos m          ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(COALESCE(ap.ean, ''), '0')
                    LEFT JOIN medicamentos_imagens mi  ON mi.medicamento_id = m.id
                    LEFT JOIN produto_canon pc ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(COALESCE(ap.ean, ''), '0') AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
                    LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = ap.cnpjloja AND LTRIM(COALESCE(epi.ean, ''), '0') = LTRIM(COALESCE(ap.ean, ''), '0')
                    WHERE ap.cnpjloja = %s
                      AND LTRIM(COALESCE(ap.ean, ''), '0') = LTRIM(%s, '0')
                      AND COALESCE(ap.inativo, false) = false
                      AND COALESCE(ap.estoque, 0) > 0
                    LIMIT 1
                    """,
                    (imagem_custom, cnpjloja, ean),
                )
                produto = cur.fetchone()
            except Exception as exc:
                app.logger.warning("produto detalhe alpha a7 indisponivel: %s", exc)

        if not produto and not _catalogo_alpha_exclusivo():
            cur.execute(
                """
                SELECT e.barras AS ean, e.descricao AS nome,
                       CAST(e.estoque AS INTEGER) AS qty,
                       COALESCE(ep.preco_customizado, vg.preco_venda, vg_market.preco_venda, e.preco_referencial) AS preco,
                       COALESCE(%s, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), epi.imagem_url) AS imagem
                FROM estoque e
                LEFT JOIN medicamentos m          ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0')
                LEFT JOIN medicamentos_imagens mi  ON mi.medicamento_id = m.id
                LEFT JOIN produto_canon pc ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0') AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
                LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = e.cnpj AND LTRIM(COALESCE(epi.ean, ''), '0') = LTRIM(COALESCE(e.barras_norm, e.barras, ''), '0')
                LEFT JOIN ecommerce_precos ep      ON ep.cnpjloja = e.cnpj AND ep.ean = e.barras
                LEFT JOIN LATERAL (
                    SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
                    FROM vendageral
                    WHERE cnpj = e.cnpj AND ean = e.barras
                      AND total_vendasgeral > 0 AND itens > 0
                    ORDER BY id DESC LIMIT 1
                ) vg ON TRUE
                LEFT JOIN LATERAL (
                    SELECT ROUND(total_vendasgeral / NULLIF(itens, 0), 2) AS preco_venda
                    FROM vendageral
                    WHERE ean = e.barras
                      AND total_vendasgeral > 0 AND itens > 0
                    ORDER BY id DESC LIMIT 1
                ) vg_market ON TRUE
                WHERE e.cnpj = %s AND (e.barras = %s OR e.barras_norm = %s) AND e.estoque > 0
                LIMIT 1
                """,
                (imagem_custom, cnpjloja, ean, ean),
            )
            produto = cur.fetchone()

        if not produto and not _catalogo_alpha_exclusivo():
            cur.execute(
                """
                SELECT ae.ean, ae.descricao_produto AS nome,
                       CAST(ae.quantidade_estoque AS INTEGER) AS qty,
                       COALESCE(ep.preco_customizado, av.preco_venda, ae.valor_final_produto) AS preco,
                       COALESCE(%s, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), epi.imagem_url) AS imagem
                FROM automatiza_estoque ae
                LEFT JOIN medicamentos m          ON LTRIM(COALESCE(m.barra_norm, m.barra, ''), '0') = LTRIM(COALESCE(ae.ean, ''), '0')
                LEFT JOIN medicamentos_imagens mi  ON mi.medicamento_id = m.id
                LEFT JOIN produto_canon pc ON LTRIM(COALESCE(pc.ean, ''), '0') = LTRIM(COALESCE(ae.ean, ''), '0') AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
                LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = ae.cnpj_loja AND LTRIM(COALESCE(epi.ean, ''), '0') = LTRIM(COALESCE(ae.ean, ''), '0')
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
    if _chave_anvisa and _chave_anvisa not in _CHAVES_OTC_ISENTO:
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

    # Enriquece descrição com IA para qualquer produto com chave conhecida
    # (funciona mesmo para OTC/suplementos sem entrada ANVISA)
    if _chave_anvisa:
        try:
            _api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
            if _api_key:
                _ia_desc = _enriquecer_descricao_ia(
                    _chave_anvisa, nome_busca,
                    anvisa.get("principio_ativo") or "",
                    anvisa.get("serve_para") or "",
                    anvisa.get("como_usar") or "",
                    anvisa.get("alertas") or "",
                    conn, _api_key,
                    anvisa_tarja=anvisa.get("tarja") or "",
                )
                if _ia_desc.get("para_que_serve_ia"):
                    anvisa["para_que_serve_ia"] = _ia_desc["para_que_serve_ia"]
                if _ia_desc.get("como_tomar_ia"):
                    anvisa["como_tomar_ia"] = _ia_desc["como_tomar_ia"]
                if _ia_desc.get("tarja_ia"):
                    anvisa["tarja_ia"] = _ia_desc["tarja_ia"]
                    anvisa["tarja_ia_confianca"] = _ia_desc.get("tarja_ia_confianca") or ""
                if _ia_desc.get("exibir_imagem_ia") is True:
                    # IA só confirma exibição (true) — nunca bloqueia via false
                    if anvisa.get("exibir_imagem_publica") is None:
                        anvisa["exibir_imagem_publica"] = True
        except Exception as _e:
            app.logger.warning(f"enriquecer_descricao_ia: {_e}")

    if not produto and not med and not nome_hint:
        flash("Produto não encontrado.", "error")
        return redirect(url_for("index"))

    if produto:
        produto = dict(produto)
        preco_corrigido, promo_produto = _preco_produto_com_promocao(
            cnpjloja,
            ean,
            produto.get("preco"),
            session.get("consumidor_id"),
        )
        if promo_produto:
            produto["preco_original"] = promo_produto.get("preco_original")
            produto["promo"] = promo_produto
            produto["preco"] = preco_corrigido

    # Ignorar placeholders genéricos que podem ter sido salvos erroneamente em medicamentos.imagem
    _prod_img = (produto.get("imagem") if produto else "") or ""
    _med_img  = (med.get("imagem")    if med    else "") or ""
    if _prod_img in _MEDICINE_PLACEHOLDER_URLS: _prod_img = ""
    if _med_img  in _MEDICINE_PLACEHOLDER_URLS: _med_img  = ""
    imagem = imagem_custom or _prod_img or _med_img or None
    if not imagem and cnpjloja:
        imagem = _fill_one_catalog_image(cnpjloja, ean, nome_busca)
    nome   = (med["descricao"] if med else None) or _descricao_canon or (produto["nome"] if produto else None) or nome_hint or "Produto"
    tipo_produto = _classificar_produto(nome)
    _is_med = tipo_produto not in _TIPOS_NAO_MEDICAMENTO
    tarja = _detectar_tarja(anvisa) if _is_med else None
    placeholder_generico = None
    if not _is_med:
        # Produto claramente não-medicamento: limpar dados farmacêuticos que poderiam vir
        # de uma correspondência incorreta do EAN na base ANVISA
        anvisa = {}
    # Mesma lógica do _marcar_tarja_batch usado no card:
    # substitui imagem de farmácia concorrente e aplica caixa genérica quando tarja ou exibir=False
    if imagem and _looks_like_other_pharmacy_brand(imagem):
        imagem = placeholder_generico
    elif imagem and placeholder_generico and _is_untrusted_scraped_image(imagem) and _image_has_other_pharmacy_text(imagem):
        imagem = placeholder_generico
    if _is_med:
        _exibir = anvisa.get("exibir_imagem_publica")
        _nao_exibir = _exibir is False
        _exibir_confirmado = _exibir is True  # IA ou ANVISA confirmou explicitamente
        # Fonte oficial explicita prevalece; tarja e fallback apenas quando a regra e desconhecida.
        _bloquear_img = _nao_exibir or (
            _exibir is None and tarja in ("preta", "vermelha")
        )
        if not _bloquear_img and not _exibir_confirmado and imagem and _looks_like_other_pharmacy_brand(imagem):
            _bloquear_img = True
        if _bloquear_img:
            # Caixinha de tarja só para produtos realmente tarjados — OTC/suplemento sem imagem = sem imagem
            if tarja in ("preta", "vermelha"):
                imagem = _placeholder_for_tarja(tarja) or imagem
            else:
                imagem = None
    requer_receita = _exige_receita_digital_entrega(anvisa, nome)
    reputacao = _reputacao_loja(loja.get("cnpjloja")) if loja else None

    # Plano de assinatura da loja exibida
    plano_assinatura = None
    ja_assina = False
    assinatura_pendente = False
    if cnpjloja:
        try:
            _ensure_assinatura_schema()
            cur2 = conn.cursor()
            cur2.execute(
                "SELECT id, nome, descricao, preco_mensal, beneficios, frete_gratis_primeira_entrega "
                "FROM ecommerce_planos_assinatura WHERE cnpjloja=%s AND ativo=TRUE LIMIT 1",
                (cnpjloja,),
            )
            plano_assinatura = cur2.fetchone()
            if plano_assinatura:
                consumidor_id = str(session.get("consumidor_id") or "")
                if consumidor_id:
                    cur2.execute(
                        """SELECT status, pagamento_status, data_fim FROM ecommerce_assinantes
                           WHERE consumidor_id=%s AND cnpjloja=%s LIMIT 1""",
                        (consumidor_id, cnpjloja),
                    )
                    row_assin = cur2.fetchone()
                    if row_assin:
                        ja_assina = _assinatura_vigente_row(row_assin)
                        assinatura_pendente = row_assin["status"] == "aguardando_pagamento"
            cur2.close()
        except Exception:
            pass

    status_entrega = None
    if loja and loja.get("cnpjloja"):
        try:
            status_entrega = _status_horario_entrega(loja["cnpjloja"])
        except Exception:
            status_entrega = None

    return render_template(
        "produto_detalhe.html",
        ean=ean, nome=nome, imagem=imagem,
        med=dict(med) if med else {},
        vitnatu=dict(vitnatu) if vitnatu else {},
        produto=produto if produto else {},
        loja=dict(loja) if loja else {},
        anvisa=anvisa,
        tipo_produto=tipo_produto,
        tarja=tarja,
        requer_receita=requer_receita,
        reputacao=reputacao,
        plano_assinatura=plano_assinatura,
        ja_assina=ja_assina,
        assinatura_pendente=assinatura_pendente,
        status_entrega=status_entrega,
    )


# ─── BUSCA POR RECEITA MÉDICA ─────────────────────────────────────────────────

_MED_DOSAGE_RE = re.compile(
    r"\b([A-ZÁÉÍÓÚÂÊÔÃÕÇÀÜ][A-Za-záéíóúâêôãõçàüÁÉÍÓÚÂÊÔÃÕÇÀÜ\s]{2,40}?)"
    r"\s+((?:\d+(?:[,\.]\d+)?\s*"
    r"(?:mg|mcg|µg|ui|g\b|ml\b|%|comprimido|cápsula|capsula|frasco|ampola|bisnaga|pct\b)"
    r".{0,60}))",
    re.IGNORECASE | re.MULTILINE,
)


def _claude_vision_receita(image_b64: str, media_type: str = "image/jpeg"):
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    prompt = (
        "Analise esta receita médica brasileira e extraia todos os medicamentos prescritos. "
        "Retorne SOMENTE um JSON válido, sem markdown, no formato:\n"
        '{"medicamentos":[{"nome":"Nome","concentracao":"ex:500mg",'
        '"forma":"ex:comprimido","posologia":"ex:1cp 3x/dia por 7 dias"}]}\n'
        "Se não for receita ou não tiver medicamentos, retorne: {\"medicamentos\":[]}"
    )
    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": prompt},
        ]}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
        method="POST",
    )
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = (data.get("content") or [{}])[0].get("text", "").strip()
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
        return json.loads(text)
    except Exception:
        return None


def _claude_vision_caixa(image_b64: str, media_type: str = "image/jpeg"):
    """Identifica medicamento a partir de foto de embalagem. Retorna dict com
    nome/laboratorio/dosagem/ean, ou {"erro":"imagem_invalida"}, ou None se falhar."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    prompt = (
        "Analise a imagem de embalagem de medicamento e extraia as informacoes visiveis. "
        "Retorne SOMENTE um JSON valido sem markdown:\n"
        '{"nome":"Nome do medicamento","laboratorio":"Laboratorio ou null","dosagem":"ex:500mg ou null","ean":"13 digitos ou null"}\n'
        "REGRAS:\n"
        "- nome: nome comercial OU principio ativo que aparece na caixa (obrigatorio)\n"
        "- laboratorio: fabricante se visivel, null caso contrario\n"
        "- dosagem: concentracao/dosagem visivel ex '500mg' '10mg/ml', null se ausente\n"
        "- ean: somente os 13 digitos do codigo de barras se claramente legivel, null caso contrario\n"
        "- Valores sem acentos e sem caracteres especiais para facilitar a busca\n"
        "- Se a imagem NAO for embalagem de medicamento retorne: {\"erro\":\"imagem_invalida\"}"
    )
    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 200,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": prompt},
        ]}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
        method="POST",
    )
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            resp_data = json.loads(r.read().decode("utf-8"))
        text = (resp_data.get("content") or [{}])[0].get("text", "").strip()
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
        return json.loads(text)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        app.logger.error("_claude_vision_caixa HTTPError %s: %s", e.code, body)
        return None
    except Exception as e:
        app.logger.error("_claude_vision_caixa error: %s", e)
        return None


def _ocr_receita_b64(image_b64: str, media_type: str = "image/jpeg"):
    api_key = _ocr_space_api_key()
    if not api_key:
        return None
    data_uri = f"data:{media_type};base64,{image_b64}"
    try:
        payload = urllib.parse.urlencode({
            "apikey": api_key,
            "base64Image": data_uri,
            "language": "por",
            "scale": "true",
            "OCREngine": "2",
            "isOverlayRequired": "false",
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.ocr.space/parse/image",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        return " ".join(
            item.get("ParsedText", "") for item in (data.get("ParsedResults") or [])
            if isinstance(item, dict)
        )
    except Exception:
        return None


def _parse_receita_text(text: str):
    _SKIP_WORDS = re.compile(
        r"\b(dr|dra|cid|data|nome|paciente|medic[oa]|receita|assinatura|carimbo|tel|cpf|crm|rg|rua|av|bairro|cidade|estado|cep|codigo)\b",
        re.IGNORECASE,
    )
    meds, seen = [], set()
    for m in _MED_DOSAGE_RE.finditer(text):
        nome = m.group(1).strip().title()
        conc = (m.group(2) or "").strip()[:80]
        key  = _norm_text(nome)
        if len(key) < 4 or key in seen or _SKIP_WORDS.search(nome):
            continue
        seen.add(key)
        meds.append({"nome": nome, "concentracao": conc, "forma": "", "posologia": ""})
    return meds


def _buscar_med_catalogo(nome_med: str, cnpjs: list):
    from concurrent.futures import ThreadPoolExecutor
    q = _norm_text(nome_med)
    if not q or len(q) < 3:
        return []
    resultados, seen = [], set()

    def _search_cnpj(cnpj):
        try:
            return get_dns_products(cnpj, q[:35], skip_image_filter=True)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=min(len(cnpjs), 6)) as exc:
        for prods in exc.map(_search_cnpj, cnpjs[:15], timeout=15):
            for p in prods:
                ean = (p.get("ean") or "").strip()
                if not ean or ean in seen:
                    continue
                if q not in _norm_text(p.get("nome") or ""):
                    continue
                seen.add(ean)
                resultados.append(p)
    return resultados


@app.get("/receita-medica")
def receita_medica():
    return render_template("receita.html")


@app.post("/api/receita/analisar")
@_rate_limited_api(max_calls=10, window_secs=60)
def api_receita_analisar():
    data = request.get_json(silent=True) or {}
    image_b64  = (data.get("imagem") or "").strip()
    media_type = (data.get("tipo") or "image/jpeg").strip()
    if not image_b64:
        return jsonify({"ok": False, "erro": "Imagem não recebida"}), 400
    if media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        media_type = "image/jpeg"

    resultado = _claude_vision_receita(image_b64, media_type)
    if resultado is not None:
        meds = resultado.get("medicamentos") or []
        return jsonify({"ok": True, "medicamentos": meds, "metodo": "vision"})

    texto = _ocr_receita_b64(image_b64, media_type)
    if texto:
        meds = _parse_receita_text(texto)
        return jsonify({"ok": True, "medicamentos": meds, "metodo": "ocr"})

    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    has_ocr       = bool(_ocr_space_api_key())
    if not has_anthropic and not has_ocr:
        return jsonify({
            "ok": False,
            "erro": "Configure ANTHROPIC_API_KEY ou OCR_SPACE_API_KEY para habilitar leitura de receitas.",
        }), 503
    return jsonify({"ok": False, "erro": "Não foi possível ler a receita. Tente uma imagem com melhor iluminação."}), 422


@app.post("/api/receita/buscar")
@_rate_limited_api(max_calls=20, window_secs=60)
def api_receita_buscar():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    data        = request.get_json(silent=True) or {}
    medicamentos = data.get("medicamentos") or []
    lat_raw     = data.get("lat")
    lng_raw     = data.get("lng")
    if not medicamentos:
        return jsonify({"ok": False, "erro": "Lista de medicamentos vazia"}), 400

    conn = db()
    cur  = conn.cursor()
    try:
        if lat_raw is not None and lng_raw is not None:
            try:
                lat_f, lng_f = float(lat_raw), float(lng_raw)
            except (TypeError, ValueError):
                lat_f = lng_f = None
        else:
            lat_f = lng_f = None

        if lat_f and lng_f:
            cur.execute("""
                SELECT u.cnpjloja, u.razao, u.endereco, u.uf,
                       g.lat AS glat, g.lng AS glng,
                       COALESCE(c.aceita_entrega, FALSE)  AS aceita_entrega,
                       COALESCE(c.raio_entrega_km, 0)     AS raio_entrega_km,
                       COALESCE(c.cobra_frete, FALSE)     AS cobra_frete,
                       COALESCE(c.valor_frete, 0)         AS valor_frete
                FROM users u
                JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
                LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
                WHERE u.is_admin = FALSE
                  AND COALESCE(c.catalogo_publico, TRUE) = TRUE
                  AND g.lat IS NOT NULL
            """)
            lojas_raw = cur.fetchall()
            lojas_dist = sorted(
                [{**dict(l), "distancia_km": round(haversine(lat_f, lng_f, float(l["glat"]), float(l["glng"])), 2)}
                 for l in lojas_raw],
                key=lambda x: x["distancia_km"],
            )
            cnpjs     = [l["cnpjloja"] for l in lojas_dist[:12]]
            loja_info = {l["cnpjloja"]: l for l in lojas_dist[:12]}
        else:
            cur.execute("""
                SELECT u.cnpjloja, u.razao, u.endereco, u.uf
                FROM users u
                LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
                WHERE u.is_admin = FALSE
                  AND COALESCE(c.catalogo_publico, TRUE) = TRUE
                ORDER BY u.razao
            """)
            lojas_raw = cur.fetchall()
            cnpjs     = [l["cnpjloja"] for l in lojas_raw]
            loja_info = {l["cnpjloja"]: {"razao": _public_store_name(l), "distancia_km": None}
                         for l in lojas_raw}
    finally:
        cur.close()

    def _buscar_um(med):
        nome = (med.get("nome") or "").strip()
        if not nome:
            return nome, []
        prods = _buscar_med_catalogo(nome, cnpjs)
        enriched = []
        for p in prods:
            info  = loja_info.get(p.get("cnpjloja"), {})
            dist  = info.get("distancia_km")
            razao = _public_store_name(info) if info else ""
            categoria = _classificar_produto(p.get("nome") or "")
            imagem = p.get("imagem") or None
            if imagem and _looks_like_other_pharmacy_brand(imagem):
                imagem = None
            enriched.append({
                "ean":        p.get("ean") or "",
                "nome":       p.get("nome") or "",
                "laboratorio": p.get("laboratorio") or "",
                "preco":      p.get("preco"),
                "qty":        p.get("qty"),
                "imagem":     imagem,
                "cnpjloja":   p.get("cnpjloja") or "",
                "razao":      razao,
                "distancia_km": dist,
                "categoria":  categoria,
            })
        enriched.sort(key=lambda x: (x["distancia_km"] is None, x["distancia_km"] or 0))
        return nome, enriched[:20]

    resultados = {}
    with ThreadPoolExecutor(max_workers=min(len(medicamentos), 5)) as exc:
        futures = {exc.submit(_buscar_um, med): med for med in medicamentos[:10]}
        for future in as_completed(futures, timeout=25):
            try:
                nome, prods = future.result()
                if nome:
                    resultados[nome] = prods
            except Exception:
                med = futures[future]
                resultados[med.get("nome", "") or ""] = []

    return jsonify({"ok": True, "resultados": resultados})


@app.get("/api/busca/sugestoes")
@_rate_limited_api(max_calls=120, window_secs=60)
def api_busca_sugestoes():
    """Autocomplete leve: retorna nomes de produtos + 'você quis dizer?' para typos."""
    q = (request.args.get("q") or "").strip()
    cnpjs_param = (request.args.get("cnpjs") or "").strip()
    if len(q) < 2:
        return jsonify({"sugestoes": [], "voce_quis_dizer": None})

    cnpjs = [c.strip() for c in cnpjs_param.split(",") if c.strip()]

    # Se não há CNPJs, resolve pelo lat/lng ou usa todas as farmácias
    if not cnpjs:
        try:
            lat_usr = float(request.args.get("lat") or 0)
            lng_usr = float(request.args.get("lng") or 0)
            _conn = db()
            _cur = _conn.cursor()
            _cur.execute(
                "SELECT u.cnpjloja, g.lat, g.lng FROM users u "
                "LEFT JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja "
                "WHERE u.is_admin = FALSE"
            )
            for _r in _cur.fetchall():
                if lat_usr and lng_usr and _r["lat"] and _r["lng"]:
                    if haversine(lat_usr, lng_usr, float(_r["lat"]), float(_r["lng"])) <= 60:
                        cnpjs.append(_r["cnpjloja"])
                else:
                    # sem geo → inclui todas
                    cnpjs.append(_r["cnpjloja"])
            _cur.close()
            _conn.close()
        except Exception:
            pass

    if not cnpjs:
        return jsonify({"sugestoes": [], "voce_quis_dizer": None})

    q_norm = _norm_text(q)
    tokens = [t for t in q_norm.split() if len(t) >= 2]
    if not tokens:
        return jsonify({"sugestoes": [], "voce_quis_dizer": None})

    patterns = [f"%{t}%" for t in tokens[:3]]
    token_cond_e  = " AND ".join("LOWER(e.descricao) LIKE %s"              for _ in patterns)
    token_cond_ae = " AND ".join("LOWER(ae.descricao_produto) LIKE %s"     for _ in patterns)

    try:
        conn = _new_conn_batch()
        cur = conn.cursor()

        # ── busca direta ────────────────────────────────────────
        cur.execute(
            f"""
            SELECT nome FROM (
                (SELECT UPPER(TRIM(e.descricao)) AS nome FROM estoque e
                WHERE e.cnpj = ANY(%s) AND e.estoque > 0 AND {token_cond_e}
                LIMIT 30)
                UNION ALL
                (SELECT UPPER(TRIM(ae.descricao_produto)) AS nome FROM automatiza_estoque ae
                WHERE ae.cnpj_loja = ANY(%s) AND ae.quantidade_estoque > 0 AND {token_cond_ae}
                LIMIT 30)
            ) sub
            WHERE nome IS NOT NULL AND nome <> ''
            GROUP BY nome ORDER BY MIN(LENGTH(nome))
            LIMIT 8
            """,
            [cnpjs] + patterns + [cnpjs] + patterns,
        )
        rows = [r["nome"] for r in cur.fetchall() if r.get("nome")]

        voce_quis_dizer = None

        # ── "você quis dizer?" quando direto retorna vazio ──────
        if not rows and len(q_norm) >= 4:
            # tenta prefixos progressivamente mais curtos (remove até 3 letras)
            # ex: "dipirina" → "dipirin" → "dipiri" → "dipir" que bate em "dipirona"
            sugestoes_fuzzy = []
            prefixos = []
            cond_f_e  = "LOWER(e.descricao) LIKE %s"
            cond_f_ae = "LOWER(ae.descricao_produto) LIKE %s"
            for token in tokens[:2]:
                max_remove = min(3, len(token) - 5)  # mantém ao menos 5 chars
                if max_remove < 1:
                    continue
                prefixos = [token[: len(token) - i] for i in range(1, max_remove + 1)
                            if len(token) - i >= 5]
                for prefixo in prefixos:
                    pat_f = f"%{prefixo}%"
                    cur.execute(
                        f"""
                        SELECT nome FROM (
                            (SELECT UPPER(TRIM(e.descricao)) AS nome FROM estoque e
                            WHERE e.cnpj = ANY(%s) AND e.estoque > 0 AND {cond_f_e}
                            LIMIT 10)
                            UNION ALL
                            (SELECT UPPER(TRIM(ae.descricao_produto)) AS nome FROM automatiza_estoque ae
                            WHERE ae.cnpj_loja = ANY(%s) AND ae.quantidade_estoque > 0 AND {cond_f_ae}
                            LIMIT 10)
                        ) sub
                        WHERE nome IS NOT NULL
                        GROUP BY nome ORDER BY MIN(LENGTH(nome))
                        LIMIT 4
                        """,
                        [cnpjs, pat_f, cnpjs, pat_f],
                    )
                    for r in cur.fetchall():
                        n = r.get("nome")
                        if n and n not in sugestoes_fuzzy:
                            sugestoes_fuzzy.append(n)
                    if sugestoes_fuzzy:
                        break
                if sugestoes_fuzzy:
                    break

            if sugestoes_fuzzy:
                rows = sugestoes_fuzzy[:8]
                # encontra a palavra do resultado que contém o prefixo (ex: "FRANCIS" não "SABONETE")
                corrigido = None
                for resultado_nome in sugestoes_fuzzy[:1]:
                    for palavra in resultado_nome.split():
                        p_norm = _norm_text(palavra)
                        if any(p_norm.startswith(pref) or pref in p_norm
                               for pref in prefixos):
                            corrigido = palavra.title()
                            break
                    if corrigido:
                        break
                if corrigido and _norm_text(corrigido) != tokens[0]:
                    voce_quis_dizer = " ".join(
                        [corrigido] + [t.title() for t in tokens[1:]]
                    )

        cur.close()
        conn.close()
        return jsonify({"sugestoes": rows, "voce_quis_dizer": voce_quis_dizer})

    except Exception as _e:
        import traceback; traceback.print_exc()
        return jsonify({"sugestoes": [], "voce_quis_dizer": None})


@app.post("/api/busca/foto")
@_rate_limited_api(max_calls=10, window_secs=60)
def api_busca_foto():
    imagem_file = request.files.get("imagem")
    if not imagem_file:
        return jsonify({"ok": False, "erro": "Nenhuma imagem enviada"}), 400

    raw = imagem_file.read()
    if not raw:
        return jsonify({"ok": False, "erro": "Imagem vazia"}), 400

    # Detecta tipo real pelos magic bytes — ignora content_type do browser
    if raw[:3] == b'\xff\xd8\xff':
        media_type = "image/jpeg"
    elif raw[:4] == b'\x89PNG':
        media_type = "image/png"
    elif raw[:4] in (b'GIF8', b'GIF9'):
        media_type = "image/gif"
    elif raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        media_type = "image/webp"
    else:
        media_type = "image/jpeg"

    cache_key = "foto_" + hashlib.md5(raw).hexdigest()
    identificado = _busca_cache_get(cache_key)

    if not identificado:
        img_b64 = base64.b64encode(raw).decode("utf-8")
        identificado = _claude_vision_caixa(img_b64, media_type)
        if not identificado:
            return jsonify({"ok": False, "erro": "Não foi possível processar a imagem"}), 503
        if identificado.get("erro") == "imagem_invalida":
            return jsonify({"ok": False, "erro": "A imagem não parece ser uma embalagem de remédio"}), 422
        if identificado.get("nome"):
            _busca_cache_set(cache_key, identificado)

    nome    = (identificado.get("nome") or "").strip()
    dosagem = (identificado.get("dosagem") or "").strip()
    ean     = (identificado.get("ean") or "").strip()

    if not nome:
        return jsonify({"ok": False, "erro": "Não foi possível identificar o medicamento na imagem"}), 422

    lat_raw = request.form.get("lat")
    lng_raw = request.form.get("lng")
    lat_f = lng_f = None
    if lat_raw and lng_raw:
        try:
            lat_f, lng_f = float(lat_raw), float(lng_raw)
        except (TypeError, ValueError):
            pass

    if not (lat_f and lng_f):
        return jsonify({
            "ok": True,
            "identificado": identificado,
            "produtos": [],
            "aviso": "Ative a localização para ver as farmácias próximas.",
        })

    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT u.cnpjloja, u.razao, u.endereco, u.uf,
                   g.lat AS glat, g.lng AS glng,
                   COALESCE(c.aceita_entrega, FALSE) AS aceita_entrega,
                   COALESCE(c.raio_entrega_km, 0)   AS raio_entrega_km
            FROM users u
            JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
            WHERE u.is_admin = FALSE
              AND COALESCE(c.catalogo_publico, TRUE) = TRUE
              AND g.lat IS NOT NULL
        """)
        lojas_raw = cur.fetchall()
    finally:
        cur.close()

    lojas_dist = sorted(
        [{**dict(l), "distancia_km": round(haversine(lat_f, lng_f, float(l["glat"]), float(l["glng"])), 2)}
         for l in lojas_raw],
        key=lambda x: x["distancia_km"],
    )
    proximas = [l for l in lojas_dist if l["distancia_km"] <= 30]
    if not proximas:
        proximas = [l for l in lojas_dist if l["distancia_km"] <= 60][:3]
    if not proximas:
        proximas = lojas_dist[:3]

    cnpjs     = [l["cnpjloja"] for l in proximas]
    loja_info = {l["cnpjloja"]: l for l in proximas}

    def _enrich(prods):
        enriched = []
        for p in prods:
            info  = loja_info.get(p.get("cnpjloja"), {})
            dist  = info.get("distancia_km")
            razao = _public_store_name(info) if info else ""
            imagem = p.get("imagem") or None
            if imagem and _looks_like_other_pharmacy_brand(imagem):
                imagem = None
            enriched.append({
                "ean":          p.get("ean") or "",
                "nome":         p.get("nome") or "",
                "laboratorio":  p.get("laboratorio") or "",
                "preco":        p.get("preco"),
                "qty":          p.get("qty"),
                "imagem":       imagem,
                "cnpjloja":     p.get("cnpjloja") or "",
                "razao":        razao,
                "distancia_km": dist,
            })
        enriched.sort(key=lambda x: (x["distancia_km"] is None, x["distancia_km"] or 0))
        return enriched

    produtos = []

    if ean:
        prods_ean = get_dns_products_batch_by_eans(cnpjs, [ean])
        produtos = _enrich(prods_ean)

    if len(produtos) < 5:
        # Extrai palavras-chave do nome identificado (sem palavras curtas/genéricas)
        _stop = {'de','da','do','das','dos','e','com','para','em','a','o','as','os',
                 'por','um','uma','mg','ml','mcg','un','cp','comp','cap','cps','comprimido',
                 'capsula','solucao','xarope','gotas','pomada','creme','gel','spray'}
        palavras = [w for w in re.split(r'\s+', nome.lower())
                    if len(w) >= 4 and w not in _stop and not re.match(r'^\d', w)]
        if dosagem:
            palavras.append(dosagem.lower())
        # Usa no máximo 3 palavras mais longas (mais distintivas)
        kw_termos = sorted(set(palavras), key=len, reverse=True)[:3]
        if not kw_termos:
            kw_termos = [nome]
        prods_nome = get_dns_products_batch_by_name(cnpjs, kw_termos)
        seen_eans = {p["ean"] for p in produtos if p["ean"]}
        for p in _enrich(prods_nome):
            if p["ean"] not in seen_eans:
                produtos.append(p)
                seen_eans.add(p["ean"])

    return jsonify({
        "ok": True,
        "identificado": identificado,
        "produtos": produtos[:30],
    })


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
    _anvisa_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT ean, cnpjloja, nome, preco, qty, imagem, razao, COALESCE(requer_receita,FALSE) AS requer_receita FROM ecommerce_carrinho WHERE consumidor_id=%s ORDER BY atualizado_em",
        (cid,),
    )
    items = []
    for r in cur.fetchall():
        nome = r["nome"] or ""
        preco_base_atual = _preco_catalogo_atual(r["cnpjloja"], r["ean"], r["preco"])
        if float(preco_base_atual or 0) <= 0:
            continue
        preco_item, promo_item = _preco_produto_com_promocao(
            r["cnpjloja"], r["ean"], preco_base_atual, cid
        )
        anvisa = {}
        tarja = None
        chave = _anvisa_chave(nome)
        if chave:
            cur.execute(
                "SELECT alertas, como_usar, nome_anvisa, principio_ativo, tarja, receita_retida, "
                "venda_online_permitida, exibir_imagem_publica, dizeres_receita, dizeres_imagem "
                "FROM anvisa_cache WHERE chave=%s AND encontrado=TRUE LIMIT 1",
                (chave,),
            )
            anvisa = dict(cur.fetchone() or {})
            tarja = _detectar_tarja(anvisa)
        requer_receita = bool(r["requer_receita"])
        if tarja is not None or anvisa:
            requer_receita = _exige_receita_digital_entrega(anvisa, nome)
        items.append({
            "ean": r["ean"], "cnpjloja": r["cnpjloja"], "nome": nome,
            "preco": preco_item, "preco_original": preco_base_atual,
            "promo": promo_item, "qty": r["qty"] or 1,
            "imagem": r["imagem"] or "", "razao": r["razao"] or "",
            "tarja": tarja, "requer_receita": requer_receita,
            "receita_retida": requer_receita,
        })
    cur.close()
    return jsonify({"items": items})


@app.post("/api/carrinho/recalcular")
def api_carrinho_recalcular():
    cid = session.get("consumidor_id")
    data = request.get_json(force=True) or {}
    items_in = data.get("items") or []
    items = []
    for item in items_in:
        ean = (item.get("ean") or "").strip()
        cnpjloja = (item.get("cnpjloja") or "").strip()
        if not ean or not cnpjloja:
            continue
        preco_base = _preco_catalogo_atual(cnpjloja, ean, item.get("preco", 0))
        if _catalogo_alpha_exclusivo() and float(preco_base or 0) <= 0:
            continue
        preco, promo = _preco_produto_com_promocao(cnpjloja, ean, preco_base, cid)
        novo = dict(item)
        novo["preco"] = preco
        novo["preco_original"] = preco_base
        if promo:
            novo["promo"] = promo
        else:
            novo.pop("promo", None)
        items.append(novo)
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
        return redirect(request.args.get("next") or url_for("index"))
    return render_template(
        "consumidor_login.html",
        next_url=request.args.get("next") or "",
        google_auth_available=_google_oauth_ready(),
    )


@app.get("/auth/google")
def consumidor_google_start():
    if not _google_oauth_ready():
        flash("Login com Google ainda não está configurado.", "info")
        return redirect(request.referrer or url_for("consumidor_login", next=request.args.get("next") or ""))

    state = secrets.token_urlsafe(24)
    session["google_oauth_state"] = state
    session["google_oauth_next"] = request.args.get("next") or url_for("index")
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": _google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return redirect(GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params))


@app.get("/auth/google/callback")
def consumidor_google_callback():
    if request.args.get("state") != session.get("google_oauth_state"):
        flash("Não foi possível validar o login com Google. Tente novamente.", "error")
        return redirect(url_for("consumidor_login"))
    code = request.args.get("code") or ""
    next_url = session.get("google_oauth_next") or url_for("index")
    if not code:
        flash("Login com Google cancelado.", "info")
        return redirect(url_for("consumidor_login", next=next_url))

    try:
        token_data = _google_post_json(GOOGLE_TOKEN_URL, {
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": _google_redirect_uri(),
            "grant_type": "authorization_code",
        })
        profile = _google_get_json(GOOGLE_USERINFO_URL, token_data.get("access_token") or "")
    except Exception as exc:
        app.logger.error("google oauth error: %s", exc)
        flash("Não foi possível entrar com Google agora.", "error")
        return redirect(url_for("consumidor_login", next=next_url))
    finally:
        session.pop("google_oauth_state", None)

    email = _norm_email(profile.get("email"))
    nome = (profile.get("name") or "").strip()
    if not email or not profile.get("email_verified"):
        flash("Use uma conta Google com e-mail verificado.", "error")
        return redirect(url_for("consumidor_login", next=next_url))

    _ensure_consumidor_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ecommerce_consumidores WHERE email=%s LIMIT 1", (email,))
    user = cur.fetchone()
    cur.close()

    if user:
        session.permanent = True
        session["consumidor_id"] = str(user["id"])
        session["consumidor_nome"] = user["nome"]
        session["consumidor_email"] = user["email"]
        session["consumidor_telefone"] = user["telefone"]
        session["consumidor_documento"] = user.get("documento") or ""
        session["consumidor_endereco"] = user.get("endereco") or ""
        session["consumidor_lat"] = user.get("endereco_lat")
        session["consumidor_lng"] = user.get("endereco_lng")
        session["email_verificado"] = True
        return redirect(next_url)

    session["google_signup"] = {"email": email, "nome": nome}
    flash("Complete CPF, WhatsApp e endereço para finalizar sua conta Google.", "info")
    return redirect(url_for("consumidor_criar_conta", next=next_url))


@app.post("/entrar")
def consumidor_login_post():
    _ensure_consumidor_schema()
    email = _norm_email(request.form.get("email"))
    senha = request.form.get("senha") or ""
    next_url = request.form.get("next") or url_for("index")

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
    session["consumidor_documento"] = user.get("documento") or ""
    session["consumidor_endereco"] = user.get("endereco") or ""
    session["consumidor_lat"] = user.get("endereco_lat")
    session["consumidor_lng"] = user.get("endereco_lng")
    session["email_verificado"] = bool(user.get("email_verificado"))
    return redirect(next_url)


@app.get("/criar-conta")
def consumidor_criar_conta():
    if session.get("consumidor_id"):
        return redirect(request.args.get("next") or url_for("index"))
    google_signup = session.get("google_signup") or {}
    return render_template(
        "consumidor_cadastro.html",
        next_url=request.args.get("next") or "",
        google_auth_available=_google_oauth_ready(),
        google_signup=google_signup,
    )


@app.post("/criar-conta")
def consumidor_criar_conta_post():
    _ensure_consumidor_schema()
    nome = (request.form.get("nome") or "").strip()
    telefone = (request.form.get("telefone") or "").strip()
    documento = _digits(request.form.get("documento") or "")
    email = _norm_email(request.form.get("email"))
    senha = request.form.get("senha") or ""
    endereco = (request.form.get("endereco") or "").strip()
    endereco_lat = _to_float_or_none(request.form.get("endereco_lat"))
    endereco_lng = _to_float_or_none(request.form.get("endereco_lng"))
    next_url = request.form.get("next") or url_for("index")
    google_signup = session.get("google_signup") or {}
    google_email = _norm_email(google_signup.get("email"))
    is_google_signup = bool(google_email and google_email == email)

    if not _valid_nome(nome):
        flash("Informe nome e sobrenome reais.", "error")
        return redirect(url_for("consumidor_criar_conta", next=next_url))
    if not _valid_phone(telefone):
        flash("Informe um WhatsApp válido com DDD.", "error")
        return redirect(url_for("consumidor_criar_conta", next=next_url))
    if not _valid_documento(documento):
        flash("Informe CPF ou CNPJ válido.", "error")
        return redirect(url_for("consumidor_criar_conta", next=next_url))
    if not _valid_email(email):
        flash("Informe um e-mail válido.", "error")
        return redirect(url_for("consumidor_criar_conta", next=next_url))
    if not _valid_endereco_completo(endereco, endereco_lat, endereco_lng):
        flash("Informe um endereço completo e selecione uma opção encontrada: rua, número, bairro, cidade, UF e CEP.", "error")
        return redirect(url_for("consumidor_criar_conta", next=next_url))
    if not is_google_signup and (len(senha) < 6 or senha.isdigit() or len(set(senha)) < 4):
        flash("Crie uma senha com pelo menos 6 caracteres e variedade.", "error")
        return redirect(url_for("consumidor_criar_conta", next=next_url))
    if is_google_signup:
        senha = secrets.token_urlsafe(24)

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO ecommerce_consumidores (nome, telefone, documento, email, senha_hash, endereco, endereco_lat, endereco_lng, email_verificado)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, nome, telefone, documento, email, endereco, endereco_lat, endereco_lng
            """,
            (nome, telefone, documento, email, generate_password_hash(senha), endereco or None, endereco_lat, endereco_lng, is_google_signup),
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
    session["consumidor_documento"] = user.get("documento") or ""
    session["consumidor_endereco"] = user.get("endereco") or ""
    session["consumidor_lat"] = user.get("endereco_lat")
    session["consumidor_lng"] = user.get("endereco_lng")
    session["email_verificado"] = bool(is_google_signup)

    if is_google_signup:
        session.pop("google_signup", None)
    else:
        _enviar_email_verificacao(str(user["id"]), user["email"])
    return redirect(next_url)


@app.get("/sair")
def consumidor_logout():
    for key in ("consumidor_id", "consumidor_nome", "consumidor_email", "consumidor_telefone", "consumidor_documento", "consumidor_endereco", "consumidor_lat", "consumidor_lng"):
        session.pop(key, None)
    return redirect(url_for("index"))


@app.get("/meus-pedidos")
@_consumer_required
def meus_pedidos():
    _ensure_consumidor_schema()
    _ensure_reclamacao_schema()
    _ensure_previsao_entrega_em_column()
    _ensure_logo_url_column()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.id, p.cnpjloja, p.forma_pagamento, p.status, p.total, p.criado_em,
               p.tipo_entrega, p.desconto_cupom, p.previsao_entrega_em,
               u.razao, u.endereco, c.logo_url
        FROM ecommerce_pedidos p
        JOIN users u ON u.cnpjloja = p.cnpjloja
        LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
        WHERE p.consumidor_id = %s
        ORDER BY p.criado_em DESC
        LIMIT 100
        """,
        (session["consumidor_id"],),
    )
    pedidos = [dict(p) for p in cur.fetchall()]
    for p in pedidos:
        p["razao"] = _public_store_name(p)
    economia_total = sum(float(p.get("desconto_cupom") or 0) for p in pedidos)
    agora = datetime.now(timezone.utc)
    for p in pedidos:
        prev_em = p.get("previsao_entrega_em")
        p["atrasado"] = bool(
            prev_em and p.get("status") not in ("entregue", "cancelado") and agora > prev_em
        )

    # Mapa pedido_id → reclamação ativa + itens (preview de imagens)
    rec_map = {}
    itens_map = {}
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

        cur.execute(
            f"SELECT pedido_id, ean, nome, qty, preco_unitario, imagem FROM ecommerce_pedido_itens WHERE pedido_id IN ({placeholders}) ORDER BY id",
            ids,
        )
        for row in cur.fetchall():
            pid = str(row["pedido_id"])
            itens_map.setdefault(pid, []).append(dict(row))
    cur.close()
    for p in pedidos:
        _itens_p = itens_map.get(str(p["id"]), [])
        p["itens_count"] = len(_itens_p)
        p["itens_preview"] = _itens_p[:3]
        p["primeiro_item_nome"] = _itens_p[0]["nome"] if _itens_p else ""
    return render_template(
        "meus_pedidos.html", pedidos=pedidos, rec_map=rec_map,
        motivos=_MOTIVOS_RECLAMACAO, economia_total=economia_total,
    )


@app.post("/api/pedido/<pedido_id>/repetir")
@_consumer_required
def api_pedido_repetir(pedido_id):
    """Recoloca os itens de um pedido anterior no carrinho — mas so os que
    ainda tem estoque na loja agora (nunca deixa 'comprar' item indisponivel).
    Reusa _preco_catalogo_atual, que ja filtra estoque>0 em todas as fontes
    (Alpha, estoque, automatiza_estoque) e devolve preco atualizado."""
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT p.cnpjloja, u.razao, u.endereco FROM ecommerce_pedidos p JOIN users u ON u.cnpjloja = p.cnpjloja "
        "WHERE p.id=%s AND p.consumidor_id=%s LIMIT 1",
        (pedido_id, session["consumidor_id"]),
    )
    pedido = cur.fetchone()
    if not pedido:
        cur.close()
        return jsonify({"error": "Pedido não encontrado."}), 404
    cnpjloja = pedido["cnpjloja"]
    razao_publica = _public_store_name(pedido)
    cur.execute(
        "SELECT ean, nome, qty, imagem FROM ecommerce_pedido_itens WHERE pedido_id=%s ORDER BY id",
        (pedido_id,),
    )
    itens = cur.fetchall()
    cur.close()

    disponiveis = []
    indisponiveis = []
    for item in itens:
        preco_atual = _preco_catalogo_atual(cnpjloja, item.get("ean") or "", 0)
        if preco_atual and preco_atual > 0:
            disponiveis.append({
                "ean": item.get("ean") or "",
                "nome": item.get("nome") or "",
                "qty": int(item.get("qty") or 1),
                "preco": preco_atual,
                "imagem": item.get("imagem") or "",
                "cnpjloja": cnpjloja,
                "razao": razao_publica,
            })
        else:
            indisponiveis.append(item.get("nome") or "Item")
    return jsonify({"disponiveis": disponiveis, "indisponiveis": indisponiveis})


@app.get("/notificacoes")
@_consumer_required
def consumidor_notificacoes():
    _ensure_notificacoes_schema()
    consumidor_id = session["consumidor_id"]
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT id, tipo, titulo, mensagem, imagem_url, url, pedido_id, lida_em, criada_em
        FROM ecommerce_notificacoes_consumidor
        WHERE consumidor_id=%s
        ORDER BY criada_em DESC
        LIMIT 100
    """, (consumidor_id,))
    notificacoes = cur.fetchall()
    cur.close()
    return render_template("consumidor_notificacoes.html", notificacoes=notificacoes)


@app.post("/api/notificacoes/<notif_id>/ler")
@_consumer_required
def api_notificacao_ler(notif_id):
    _ensure_notificacoes_schema()
    conn = db(); cur = conn.cursor()
    cur.execute(
        "UPDATE ecommerce_notificacoes_consumidor SET lida_em=COALESCE(lida_em, NOW()) WHERE id=%s AND consumidor_id=%s",
        (notif_id, session["consumidor_id"]),
    )
    conn.commit(); cur.close()
    return jsonify({"ok": True})


@app.post("/api/notificacoes/ler-todas")
@_consumer_required
def api_notificacoes_ler_todas():
    _ensure_notificacoes_schema()
    conn = db(); cur = conn.cursor()
    cur.execute(
        "UPDATE ecommerce_notificacoes_consumidor SET lida_em=COALESCE(lida_em, NOW()) WHERE consumidor_id=%s AND lida_em IS NULL",
        (session["consumidor_id"],),
    )
    conn.commit(); cur.close()
    return jsonify({"ok": True})


@app.post("/admin/notificacoes/sistema")
@admin_required
def admin_notificacoes_sistema():
    data = request.get_json(silent=True) or {}
    titulo = (data.get("titulo") or request.form.get("titulo") or "").strip()
    mensagem = (data.get("mensagem") or request.form.get("mensagem") or "").strip()
    url = (data.get("url") or request.form.get("url") or "").strip() or None
    if not titulo:
        return jsonify({"ok": False, "msg": "Titulo obrigatorio."}), 400
    enviados = _notificar_todos_consumidores(titulo, mensagem, url=url, tipo="sistema")
    return jsonify({"ok": True, "enviados": enviados})


@app.get("/favoritos")
@_consumer_required
def consumidor_favoritos():
    _ensure_favoritos_schema()
    consumidor_id = session["consumidor_id"]
    _ensure_logo_url_column()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT f.*, c.logo_url
        FROM ecommerce_favoritos f
        LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = f.cnpjloja
        WHERE f.consumidor_id=%s
        ORDER BY f.atualizado_em DESC
        LIMIT 200
        """,
        (consumidor_id,),
    )
    favoritos = [dict(r) for r in cur.fetchall()]
    cur.close()
    return render_template("consumidor_favoritos.html", favoritos=favoritos)


@app.get("/api/favoritos")
def api_favoritos_listar():
    consumidor_id = session.get("consumidor_id")
    if not consumidor_id:
        return jsonify({"favoritos": []})
    _ensure_favoritos_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT ean, cnpjloja FROM ecommerce_favoritos WHERE consumidor_id=%s",
        (consumidor_id,),
    )
    rows = cur.fetchall()
    cur.close()
    return jsonify({"favoritos": [{"ean": r["ean"], "cnpjloja": r["cnpjloja"]} for r in rows]})


@app.post("/api/favoritos")
@_consumer_required
def api_favoritos_toggle():
    _ensure_favoritos_schema()
    data = request.get_json(silent=True) or {}
    consumidor_id = session["consumidor_id"]
    ean = (data.get("ean") or "").strip()
    cnpjloja = (data.get("cnpjloja") or "").strip()
    if not ean or not cnpjloja:
        return jsonify({"ok": False, "erro": "Produto inválido."}), 400
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM ecommerce_favoritos WHERE consumidor_id=%s AND ean=%s AND cnpjloja=%s LIMIT 1",
        (consumidor_id, ean, cnpjloja),
    )
    row = cur.fetchone()
    if row:
        cur.execute("DELETE FROM ecommerce_favoritos WHERE id=%s", (row["id"],))
        conn.commit()
        cur.close()
        return jsonify({"ok": True, "favorito": False})
    nome = (data.get("nome") or "Produto Poupaqui").strip()[:300]
    preco = _to_float_or_none(data.get("preco"))
    imagem = (data.get("imagem") or "").strip()[:800] or None
    razao = (data.get("razao") or "").strip()[:250] or None
    categoria = (data.get("categoria") or _classificar_produto(nome) or "").strip()[:80] or None
    cur.execute(
        """
        INSERT INTO ecommerce_favoritos
          (consumidor_id, ean, cnpjloja, nome, preco, imagem, razao, categoria,
           alerta_preco, alerta_estoque, preco_referencia)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE,TRUE,%s)
        ON CONFLICT (consumidor_id, ean, cnpjloja) DO UPDATE
          SET nome=EXCLUDED.nome,
              preco=EXCLUDED.preco,
              imagem=EXCLUDED.imagem,
              razao=EXCLUDED.razao,
              categoria=EXCLUDED.categoria,
              atualizado_em=NOW()
        """,
        (consumidor_id, ean, cnpjloja, nome, preco, imagem, razao, categoria, preco),
    )
    conn.commit()
    cur.close()
    return jsonify({"ok": True, "favorito": True})


@app.get("/api/aviso-chegada")
def api_aviso_chegada_status():
    """Diz se o consumidor logado ja pediu aviso para a cidade/uf informada."""
    consumidor_id = session.get("consumidor_id")
    cidade = (request.args.get("cidade") or "").strip()
    uf = (request.args.get("uf") or "").strip()
    if not consumidor_id or not cidade:
        return jsonify({"registrado": False})
    _ensure_aviso_chegada_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM ecommerce_avisos_chegada WHERE consumidor_id=%s AND cidade=%s AND COALESCE(uf,'')=%s LIMIT 1",
        (consumidor_id, cidade, uf),
    )
    registrado = cur.fetchone() is not None
    cur.close()
    return jsonify({"registrado": registrado})


@app.post("/api/aviso-chegada")
@_consumer_required
def api_aviso_chegada_criar():
    """Consumidor pede para ser avisado (via notificacoes) quando uma farmacia
    parceira for ativada na cidade dele. Casamento por cidade/uf acontece em
    admin_loja_catalogo_toggle quando o admin liga o catalogo publico da loja."""
    _ensure_aviso_chegada_schema()
    data = request.get_json(silent=True) or {}
    consumidor_id = session["consumidor_id"]
    cidade = (data.get("cidade") or "").strip()[:120]
    uf = (data.get("uf") or "").strip()[:2].upper() or None
    lat = _to_float_or_none(data.get("lat"))
    lng = _to_float_or_none(data.get("lng"))
    if not cidade:
        return jsonify({"ok": False, "erro": "Cidade não identificada."}), 400
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ecommerce_avisos_chegada (consumidor_id, cidade, uf, lat, lng)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (consumidor_id, cidade, uf) DO NOTHING
        """,
        (consumidor_id, cidade, uf, lat, lng),
    )
    conn.commit()
    cur.close()
    return jsonify({"ok": True})


@app.post("/api/favoritos/alertas")
@_consumer_required
def api_favoritos_alertas():
    _ensure_favoritos_schema()
    data = request.get_json(silent=True) or {}
    consumidor_id = session["consumidor_id"]
    ean = (data.get("ean") or "").strip()
    cnpjloja = (data.get("cnpjloja") or "").strip()
    campo = data.get("campo")
    valor = bool(data.get("valor"))
    if campo not in ("alerta_preco", "alerta_estoque") or not ean or not cnpjloja:
        return jsonify({"ok": False}), 400
    conn = db()
    cur = conn.cursor()
    cur.execute(
        f"UPDATE ecommerce_favoritos SET {campo}=%s, atualizado_em=NOW() WHERE consumidor_id=%s AND ean=%s AND cnpjloja=%s",
        (valor, consumidor_id, ean, cnpjloja),
    )
    conn.commit()
    cur.close()
    return jsonify({"ok": True})


def _montar_timeline_pedido(pedido):
    """Monta a timeline vertical do pedido com horario real de cada etapa,
    usando o historico de status quando existe (pedidos criados depois da
    migracao) e caindo pros 3 timestamps legados (criado_em/pagamento_
    confirmado_em/entregue_em) pra pedidos antigos sem historico."""
    status_atual = pedido.get("status")
    if status_atual == "cancelado":
        return None
    fluxo = (
        ["pendente", "pago", "pronto_retirada", "entregue"]
        if (pedido.get("tipo_entrega") or "retirada") == "retirada"
        else ["pendente", "pago", "enviado", "entregue"]
    )
    fluxo_label = {
        "pendente": "Pedido recebido",
        "pago": "Pagamento confirmado",
        "pronto_retirada": "Pronto para retirada",
        "enviado": "Saiu para entrega",
        "entregue": "Retirado na loja" if (pedido.get("tipo_entrega") or "retirada") == "retirada" else "Entregue",
    }
    atual_idx = fluxo.index(status_atual) if status_atual in fluxo else 0

    _ensure_pedidos_status_historico_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT status, criado_em FROM ecommerce_pedidos_status_historico WHERE pedido_id=%s ORDER BY criado_em ASC",
        (pedido["id"],),
    )
    historico = cur.fetchall()
    cur.close()
    primeiro_por_status = {}
    for row in historico:
        primeiro_por_status.setdefault(row["status"], row["criado_em"])

    if not primeiro_por_status:
        if pedido.get("criado_em"):
            primeiro_por_status["pendente"] = pedido["criado_em"]
        if pedido.get("pagamento_confirmado_em"):
            primeiro_por_status["pago"] = pedido["pagamento_confirmado_em"]
        if pedido.get("entregue_em"):
            primeiro_por_status["entregue"] = pedido["entregue_em"]

    passos = []
    for idx, st in enumerate(fluxo):
        ts = primeiro_por_status.get(st)
        passos.append({
            "status": st,
            "label": fluxo_label[st],
            "done": idx <= atual_idx,
            "atual": idx == atual_idx,
            "timestamp_label": ts.strftime("%d/%m · %H:%M") if ts else None,
        })
    return passos


@app.get("/meus-pedidos/<pedido_id>")
@_consumer_required
def meu_pedido_detalhe(pedido_id):
    _ensure_receita_schema()
    _ensure_reclamacao_schema()
    _ensure_previsao_entrega_em_column()
    _ensure_logo_url_column()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.*, u.razao, u.telefone, u.endereco,
               u.endereco2 AS loja_endereco,
               c.whatsapp_pedidos, c.pix_chave, c.pix_nome, c.logo_url
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
    if (
        pedido.get("status") == "pago"
        and (pedido.get("tipo_entrega") or "retirada") != "entrega"
        and not pedido.get("codigo_retirada")
    ):
        _auto_pronto_retirada(pedido_id)
        cur.execute(
            """
            SELECT p.*, u.razao, u.telefone,
                   u.endereco2 AS loja_endereco,
                   c.whatsapp_pedidos, c.pix_chave, c.pix_nome, c.logo_url
            FROM ecommerce_pedidos p
            JOIN users u ON u.cnpjloja = p.cnpjloja
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
            WHERE p.id = %s AND p.consumidor_id = %s
            LIMIT 1
            """,
            (pedido_id, session["consumidor_id"]),
        )
        pedido = cur.fetchone() or pedido
    cur.execute("SELECT * FROM ecommerce_pedido_itens WHERE pedido_id=%s ORDER BY id", (pedido_id,))
    itens = [dict(i) for i in cur.fetchall()]
    # Reclamação ativa (se houver)
    cur.execute(
        "SELECT id, status FROM ecommerce_reclamacoes WHERE pedido_id=%s AND consumidor_id=%s ORDER BY aberta_em DESC LIMIT 1",
        (pedido_id, session["consumidor_id"]),
    )
    reclamacao = cur.fetchone()
    # Verifica se já avaliou este pedido
    _ensure_avaliacoes_schema()
    cur2 = conn.cursor()
    cur2.execute("SELECT estrelas, comentario FROM ecommerce_avaliacoes_loja WHERE pedido_id=%s LIMIT 1", (pedido_id,))
    avaliacao_feita = cur2.fetchone()
    cur2.close()
    cur.close()
    pedido = dict(pedido)
    pedido["razao"] = _public_store_name(pedido)
    if pedido.get("data_entrega_agendada"):
        _data_ag = pedido["data_entrega_agendada"]
        _dia_ag = (_data_ag.weekday() + 1) % 7
        _nome_dia_ag = next((n for d, n in _DIAS_SEMANA if d == _dia_ag), "")
        pedido["data_entrega_agendada_label"] = f"{_nome_dia_ag}, {_data_ag.strftime('%d/%m/%Y')}"

    timeline = _montar_timeline_pedido(pedido)

    previsao_em = pedido.get("previsao_entrega_em")
    pedido["atrasado"] = bool(
        previsao_em and pedido.get("status") not in ("entregue", "cancelado") and datetime.now(timezone.utc) > previsao_em
    )
    if previsao_em:
        pedido["previsao_entrega_em_label"] = previsao_em.strftime("%d/%m às %H:%M")

    return render_template(
        "meu_pedido_detalhe.html",
        pedido=pedido,
        itens=itens,
        reclamacao=dict(reclamacao) if reclamacao else None,
        motivos=_MOTIVOS_RECLAMACAO,
        avaliacao_feita=dict(avaliacao_feita) if avaliacao_feita else None,
        timeline=timeline,
    )


def _alpha_nota_xml_por_pedido(pedido_id):
    if not alpha_sync:
        return None, None, None
    schema = alpha_sync._alpha_schema()
    with alpha_sync._alpha_connect(timeout_ms=30000) as aconn:
        acur = aconn.cursor()
        acur.execute(
            alpha_sync.sql.SQL(
                """
                SELECT o_xml, o_chaveacesso, o_numero
                  FROM {}.out_documentofiscalpedido
                 WHERE o_codigopedidointegracao = %s
                   AND COALESCE(NULLIF(o_xml, ''), '') <> ''
                 ORDER BY COALESCE(o_datahoraemissao, do_id) DESC NULLS LAST
                 LIMIT 1
                """
            ).format(alpha_sync.sql.Identifier(schema)),
            (str(pedido_id),),
        )
        row = acur.fetchone()
    if not row:
        return None, None, None
    return row.get("o_xml"), row.get("o_chaveacesso"), row.get("o_numero")


def _xml_text(root, path, ns):
    node = root.find(path, ns)
    return (node.text or "").strip() if node is not None and node.text is not None else ""


@app.get("/meus-pedidos/<pedido_id>/nota-fiscal.pdf")
@_consumer_required
def meu_pedido_nota_fiscal_pdf(pedido_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM ecommerce_pedidos WHERE id=%s AND consumidor_id=%s LIMIT 1",
        (pedido_id, session["consumidor_id"]),
    )
    pedido = cur.fetchone()
    cur.close()
    if not pedido:
        return jsonify({"ok": False, "erro": "pedido_nao_encontrado"}), 404

    xml_text, chave_db, numero_db = _alpha_nota_xml_por_pedido(pedido_id)
    if not xml_text:
        return jsonify({"ok": False, "erro": "nota_nao_disponivel"}), 404

    from io import BytesIO
    from xml.etree import ElementTree as ET
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except Exception:
        return jsonify({"ok": False, "erro": "xml_invalido"}), 422

    ns = {"n": "http://www.portalfiscal.inf.br/nfe"}
    inf = root.find(".//n:infNFe", ns)
    if inf is None:
        return jsonify({"ok": False, "erro": "xml_sem_nfe"}), 422

    def txt(path):
        return _xml_text(inf, path, ns)

    chave = (chave_db or (inf.attrib.get("Id") or "").replace("NFe", "")).strip()
    numero = txt("n:ide/n:nNF") or str(numero_db or "")
    serie = txt("n:ide/n:serie")
    emissao = txt("n:ide/n:dhEmi") or txt("n:ide/n:dEmi")
    emit_nome = txt("n:emit/n:xNome")
    emit_cnpj = txt("n:emit/n:CNPJ") or txt("n:emit/n:CPF")
    dest_nome = txt("n:dest/n:xNome")
    dest_doc = txt("n:dest/n:CNPJ") or txt("n:dest/n:CPF")
    valor_prod = txt("n:total/n:ICMSTot/n:vProd")
    valor_nf = txt("n:total/n:ICMSTot/n:vNF")

    itens = []
    for det in inf.findall("n:det", ns):
        prod = det.find("n:prod", ns)
        if prod is None:
            continue
        get = lambda name: _xml_text(prod, f"n:{name}", ns)
        itens.append({
            "codigo": get("cProd"),
            "ean": get("cEAN") or get("cEANTrib"),
            "descricao": get("xProd"),
            "qtd": get("qCom") or get("qTrib"),
            "unit": get("vUnCom") or get("vUnTrib"),
            "total": get("vProd"),
        })

    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    left = 1.2 * cm
    right = width - 1.2 * cm
    y = height - 1.1 * cm

    def draw_text(x, yy, value, max_width, font="Helvetica", size=8):
        value = str(value or "")
        pdf.setFont(font, size)
        if stringWidth(value, font, size) <= max_width:
            pdf.drawString(x, yy, value)
            return
        while value and stringWidth(value + "...", font, size) > max_width:
            value = value[:-1]
        pdf.drawString(x, yy, value + "...")

    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(left, y, "DANFE / Documento Auxiliar da Nota Fiscal")
    pdf.setFont("Helvetica", 8)
    pdf.drawRightString(right, y, "Gerado a partir do XML retornado pelo Alpha/A7")
    y -= 0.7 * cm

    pdf.setLineWidth(0.8)
    pdf.rect(left, y - 2.3 * cm, right - left, 2.3 * cm)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(left + 0.2 * cm, y - 0.35 * cm, f"NF-e/NFC-e numero {numero or '-'}")
    pdf.drawString(left + 7.2 * cm, y - 0.35 * cm, f"Serie {serie or '-'}")
    pdf.setFont("Helvetica", 8)
    pdf.drawString(left + 0.2 * cm, y - 0.85 * cm, f"Chave de acesso: {chave or '-'}")
    pdf.drawString(left + 0.2 * cm, y - 1.25 * cm, f"Emissao: {emissao or '-'}")
    pdf.drawString(left + 0.2 * cm, y - 1.65 * cm, f"Valor produtos: R$ {valor_prod or '-'}")
    pdf.drawString(left + 7.2 * cm, y - 1.65 * cm, f"Valor total: R$ {valor_nf or '-'}")
    y -= 2.7 * cm

    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(left, y, "Emitente")
    pdf.drawString(left + 9.2 * cm, y, "Destinatario")
    y -= 0.35 * cm
    draw_text(left, y, emit_nome, 8.4 * cm)
    draw_text(left + 9.2 * cm, y, dest_nome, 8.4 * cm)
    y -= 0.35 * cm
    pdf.setFont("Helvetica", 8)
    pdf.drawString(left, y, f"CNPJ/CPF: {emit_cnpj or '-'}")
    pdf.drawString(left + 9.2 * cm, y, f"CNPJ/CPF: {dest_doc or '-'}")
    y -= 0.7 * cm

    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(left, y, "Itens")
    y -= 0.35 * cm
    pdf.line(left, y, right, y)
    y -= 0.35 * cm

    cols = {
        "codigo": left,
        "ean": left + 2.2 * cm,
        "desc": left + 5.0 * cm,
        "qtd": right - 4.2 * cm,
        "unit": right - 2.8 * cm,
        "total": right - 1.2 * cm,
    }
    pdf.setFont("Helvetica-Bold", 7)
    pdf.drawString(cols["codigo"], y, "Cod")
    pdf.drawString(cols["ean"], y, "EAN")
    pdf.drawString(cols["desc"], y, "Descricao")
    pdf.drawRightString(cols["qtd"], y, "Qtd")
    pdf.drawRightString(cols["unit"], y, "Unit")
    pdf.drawRightString(cols["total"], y, "Total")
    y -= 0.25 * cm
    pdf.line(left, y, right, y)
    y -= 0.3 * cm

    for item in itens:
        if y < 1.6 * cm:
            pdf.showPage()
            y = height - 1.2 * cm
        draw_text(cols["codigo"], y, item["codigo"], 2.0 * cm, size=7)
        draw_text(cols["ean"], y, item["ean"], 2.6 * cm, size=7)
        draw_text(cols["desc"], y, item["descricao"], 8.2 * cm, size=7)
        pdf.setFont("Helvetica", 7)
        pdf.drawRightString(cols["qtd"], y, item["qtd"] or "-")
        pdf.drawRightString(cols["unit"], y, item["unit"] or "-")
        pdf.drawRightString(cols["total"], y, item["total"] or "-")
        y -= 0.32 * cm

    pdf.showPage()
    pdf.save()
    buf.seek(0)

    filename = f"danfe_{chave or numero or pedido_id}.pdf"
    return Response(
        buf.getvalue(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.post("/meus-pedidos/<pedido_id>/avaliar")
@_consumer_required
def avaliar_pedido(pedido_id):
    """Submete avaliação da loja após pedido entregue."""
    _ensure_avaliacoes_schema()
    conn = db(); cur = conn.cursor()
    # Verifica pedido é do consumidor e status entregue
    cur.execute(
        "SELECT cnpjloja, status, consumidor_id FROM ecommerce_pedidos WHERE id=%s LIMIT 1",
        (pedido_id,),
    )
    pedido = cur.fetchone()
    if not pedido or str(pedido["consumidor_id"]) != str(session.get("consumidor_id")):
        cur.close()
        flash("Pedido não encontrado.", "error")
        return redirect(url_for("meus_pedidos"))
    if pedido["status"] not in ("entregue", "pronto_retirada", "pago"):
        cur.close()
        flash("Avaliação disponível apenas após a conclusão do pedido.", "warning")
        return redirect(url_for("meu_pedido_detalhe", pedido_id=pedido_id))

    try:
        estrelas = int(request.form.get("estrelas", 0))
    except ValueError:
        estrelas = 0
    if estrelas < 1 or estrelas > 5:
        cur.close()
        flash("Selecione entre 1 e 5 estrelas.", "warning")
        return redirect(url_for("meu_pedido_detalhe", pedido_id=pedido_id))

    comentario = (request.form.get("comentario") or "").strip()
    if estrelas < 5 and len(comentario) < 10:
        cur.close()
        flash("Por favor, descreva o que poderia ter sido melhor (mínimo 10 caracteres).", "warning")
        return redirect(url_for("meu_pedido_detalhe", pedido_id=pedido_id))

    try:
        cur.execute("""
            INSERT INTO ecommerce_avaliacoes_loja (cnpjloja, pedido_id, consumidor_id, estrelas, comentario)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (pedido_id) DO NOTHING
        """, (pedido["cnpjloja"], pedido_id, session["consumidor_id"], estrelas, comentario or None))
        conn.commit()
        flash("Avaliação enviada! Obrigado pelo feedback.", "success")
    except Exception:
        conn.rollback()
        flash("Não foi possível registrar a avaliação. Tente novamente.", "error")
    cur.close()
    return redirect(url_for("meu_pedido_detalhe", pedido_id=pedido_id))


# ─── ENCOMENDAS: CONSUMIDOR ──────────────────────────────────────────────────

@app.post("/encomenda/criar")
@_consumer_required
def encomenda_criar():
    _ensure_encomenda_schema()
    cnpjloja = (request.form.get("cnpjloja") or "").strip()
    produto_nome = (request.form.get("produto_nome") or "").strip()
    mensagem_texto = (request.form.get("mensagem") or "").strip()
    if not cnpjloja or not produto_nome:
        flash("Informe a farmácia e o produto.", "error")
        return redirect(url_for("index"))
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT cnpjloja, razao FROM users WHERE cnpjloja=%s AND is_admin=FALSE LIMIT 1", (cnpjloja,))
    loja = cur.fetchone()
    if not loja:
        cur.close()
        flash("Farmácia não encontrada.", "error")
        return redirect(url_for("index"))
    consumidor_id = session["consumidor_id"]
    cur.execute(
        "INSERT INTO ecommerce_encomendas (cnpjloja, consumidor_id, produto_nome) VALUES (%s,%s,%s) RETURNING id",
        (cnpjloja, consumidor_id, produto_nome),
    )
    encomenda_id = cur.fetchone()["id"]
    texto = mensagem_texto or f"Olá! Procuro {produto_nome}. Vocês têm ou podem encomendá-lo?"
    cur.execute(
        "INSERT INTO ecommerce_encomenda_msgs (encomenda_id, autor, mensagem) VALUES (%s,'cliente',%s)",
        (encomenda_id, texto),
    )
    conn.commit()
    cur.close()
    flash("Solicitação de encomenda enviada!", "success")
    return redirect(url_for("minha_encomenda_detalhe", encomenda_id=encomenda_id))


@app.get("/minhas-encomendas")
@_consumer_required
def minhas_encomendas():
    _ensure_encomenda_schema()
    _ensure_logo_url_column()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.*, u.razao, c.logo_url,
               (SELECT COUNT(*) FROM ecommerce_encomenda_msgs m WHERE m.encomenda_id=e.id) AS n_msgs
        FROM ecommerce_encomendas e
        JOIN users u ON u.cnpjloja = e.cnpjloja
        LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = e.cnpjloja
        WHERE e.consumidor_id = %s
        ORDER BY e.atualizado_em DESC
        """,
        (session["consumidor_id"],),
    )
    encomendas = [dict(r) for r in cur.fetchall()]
    cur.close()
    return render_template("minhas_encomendas.html", encomendas=encomendas, status_label=_STATUS_ENCOMENDA_LABEL)


@app.get("/minhas-encomendas/<encomenda_id>")
@_consumer_required
def minha_encomenda_detalhe(encomenda_id):
    _ensure_encomenda_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.*, u.razao, u.telefone
        FROM ecommerce_encomendas e
        JOIN users u ON u.cnpjloja = e.cnpjloja
        WHERE e.id = %s AND e.consumidor_id = %s
        LIMIT 1
        """,
        (encomenda_id, session["consumidor_id"]),
    )
    enc = cur.fetchone()
    if not enc:
        cur.close()
        flash("Encomenda não encontrada.", "error")
        return redirect(url_for("minhas_encomendas"))
    cur.execute(
        "SELECT * FROM ecommerce_encomenda_msgs WHERE encomenda_id=%s ORDER BY enviada_em",
        (encomenda_id,),
    )
    msgs = [dict(m) for m in cur.fetchall()]
    cur.close()
    return render_template(
        "minha_encomenda.html",
        enc=dict(enc),
        msgs=msgs,
        status_label=_STATUS_ENCOMENDA_LABEL,
    )


@app.post("/minhas-encomendas/<encomenda_id>/mensagem")
@_consumer_required
def minha_encomenda_mensagem(encomenda_id):
    _ensure_encomenda_schema()
    mensagem = (request.form.get("mensagem") or "").strip()
    if not mensagem:
        return redirect(url_for("minha_encomenda_detalhe", encomenda_id=encomenda_id))
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, status FROM ecommerce_encomendas WHERE id=%s AND consumidor_id=%s LIMIT 1",
        (encomenda_id, session["consumidor_id"]),
    )
    enc = cur.fetchone()
    if not enc or enc["status"] == "finalizada":
        cur.close()
        return redirect(url_for("minhas_encomendas"))
    cur.execute(
        "INSERT INTO ecommerce_encomenda_msgs (encomenda_id, autor, mensagem) VALUES (%s,'cliente',%s)",
        (encomenda_id, mensagem),
    )
    if enc["status"] == "aberta":
        cur.execute(
            "UPDATE ecommerce_encomendas SET status='em_andamento', atualizado_em=NOW() WHERE id=%s",
            (encomenda_id,),
        )
    else:
        cur.execute("UPDATE ecommerce_encomendas SET atualizado_em=NOW() WHERE id=%s", (encomenda_id,))
    conn.commit()
    cur.close()
    return redirect(url_for("minha_encomenda_detalhe", encomenda_id=encomenda_id))


# ─── RECLAMAÇÕES: CONSUMIDOR ─────────────────────────────────────────────────

_MOTIVOS_RECLAMACAO = {
    "produto_vencido":  "Produto vencido",
    "produto_errado":   "Produto errado / diferente do pedido",
    "nao_entregue":     "Produto não entregue",
    "entrega_danificada": "Produto chegou danificado",
    "pedido_atrasado":  "Pedido atrasado",
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
    cur.execute("SELECT nome, telefone, documento, email, endereco, endereco_lat, endereco_lng FROM ecommerce_consumidores WHERE id=%s LIMIT 1", (session["consumidor_id"],))
    user = cur.fetchone()
    cur.close()
    return render_template("consumidor_perfil.html", user=user)


@app.post("/perfil")
@_consumer_required
def consumidor_perfil_post():
    _ensure_consumidor_schema()
    nome = (request.form.get("nome") or "").strip()
    telefone = (request.form.get("telefone") or "").strip()
    documento = _digits(request.form.get("documento") or "")
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
    if not _valid_documento(documento):
        flash("Informe CPF ou CNPJ válido.", "error")
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
                SET nome=%s, telefone=%s, documento=%s, email=%s, senha_hash=%s, endereco=%s, endereco_lat=%s, endereco_lng=%s, atualizado_em=NOW()
                WHERE id=%s
                """,
                (nome, telefone, documento, email, generate_password_hash(senha), endereco or None, endereco_lat, endereco_lng, session["consumidor_id"]),
            )
        else:
            cur.execute(
                """
                UPDATE ecommerce_consumidores
                SET nome=%s, telefone=%s, documento=%s, email=%s, endereco=%s, endereco_lat=%s, endereco_lng=%s, atualizado_em=NOW()
                WHERE id=%s
                """,
                (nome, telefone, documento, email, endereco or None, endereco_lat, endereco_lng, session["consumidor_id"]),
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
    session["consumidor_documento"] = documento
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
    _ensure_consumidor_profile_columns()
    _ensure_payment_schema()
    _ensure_receita_schema()
    _ensure_mp_public_key_column()
    _ensure_gateway_alt_columns()
    data     = request.get_json(force=True) or {}
    fp_list  = data.get("farmaciasPedidos", [])

    if not fp_list:
        return jsonify({"error": "Carrinho vazio."}), 400

    cliente = {
        "nome": session.get("consumidor_nome") or "",
        "telefone": session.get("consumidor_telefone") or "",
        "documento": session.get("consumidor_documento") or "",
        "email": session.get("consumidor_email") or "",
        "endereco": session.get("consumidor_endereco") or "",
        "lat": session.get("consumidor_lat"),
        "lng": session.get("consumidor_lng"),
    }

    conn = db()
    cur  = conn.cursor()

    # Garante que email/documento estão presentes (sessões antigas podem não ter esses campos)
    if (not cliente["email"] or "@" not in cliente["email"]) or len(_digits(cliente.get("documento"))) not in {11, 14}:
        cur.execute(
            "SELECT nome, email, telefone, documento, endereco, endereco_lat, endereco_lng FROM ecommerce_consumidores WHERE id=%s LIMIT 1",
            (session["consumidor_id"],),
        )
        _c = cur.fetchone()
        if _c:
            cliente["email"]    = _c["email"] or ""
            cliente["nome"]     = cliente["nome"] or _c["nome"] or ""
            cliente["telefone"] = cliente["telefone"] or _c["telefone"] or ""
            cliente["documento"] = cliente["documento"] or _c.get("documento") or ""
            cliente["endereco"] = cliente["endereco"] or _c.get("endereco") or ""
            cliente["lat"] = cliente["lat"] or _c.get("endereco_lat")
            cliente["lng"] = cliente["lng"] or _c.get("endereco_lng")
    if len(_digits(cliente.get("documento"))) not in {11, 14}:
        cur.close()
        return jsonify({
            "error": "Informe CPF ou CNPJ no perfil para finalizar o pedido.",
            "profile_required": True,
        }), 400
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
                   c.whatsapp_pedidos, c.whatsapp_receita, c.pix_chave, c.pix_nome,
                   c.mp_access_token, c.mp_public_key,
                   COALESCE(c.gateway_alternativo, 'mercadopago') AS gateway_alternativo,
                   c.asaas_api_key, c.pagbank_token, c.pagbank_public_key,
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

        for item in itens:
            preco_base_atual = _preco_catalogo_atual(
                cnpjloja,
                item.get("ean") or "",
                item.get("preco", 0),
            )
            if float(preco_base_atual or 0) <= 0:
                return jsonify({
                    "error": f"{item.get('nome') or 'Um item do carrinho'} não está mais disponível. Atualize o carrinho e tente novamente.",
                    "reload_cart": True,
                }), 400
            preco_corrigido, promo_info = _preco_produto_com_promocao(
                cnpjloja,
                item.get("ean") or "",
                preco_base_atual,
                session.get("consumidor_id"),
            )
            item["preco"] = preco_corrigido
            item["preco_original"] = preco_base_atual
            if promo_info:
                item["promo"] = promo_info
            else:
                item.pop("promo", None)
        produtos_total = sum(float(i.get("preco", 0)) * int(i.get("qty", 1)) for i in itens)
        # Promocao so-assinantes e sempre custeada pelo admin — a loja recebe
        # o preco cheio (Alpha) igual, a diferenca vira repasse (mesma logica
        # do frete gratis da 1a entrega e do cupom admin).
        desconto_promo_assinante = sum(
            (float(i["promo"]["preco_original"]) - float(i.get("preco", 0))) * int(i.get("qty", 1))
            for i in itens
            if i.get("promo") and i["promo"].get("so_assinantes") and i["promo"].get("aplicada")
            and float(i["promo"].get("preco_original") or 0) > float(i.get("preco", 0))
        )
        tipo_entrega = (fp.get("tipo_entrega") or "retirada").strip()
        entrega_lat = _to_float_or_none(fp.get("entrega_lat")) or _to_float_or_none(cliente.get("lat"))
        entrega_lng = _to_float_or_none(fp.get("entrega_lng")) or _to_float_or_none(cliente.get("lng"))
        endereco_entrega = (fp.get("endereco_entrega") or cliente.get("endereco") or "").strip()
        entrega_dist = None
        if entrega_lat is not None and entrega_lng is not None and loja.get("loja_lat") is not None and loja.get("loja_lng") is not None:
            entrega_dist = round(haversine(entrega_lat, entrega_lng, float(loja["loja_lat"]), float(loja["loja_lng"])), 1)
        entrega_ok_base = (
            bool(loja.get("aceita_entrega"))
            and entrega_dist is not None
            and entrega_dist <= float(loja.get("raio_entrega_km") or 0)
        )
        entrega_ok = entrega_ok_base and _status_horario_entrega(cnpjloja).get("entrega_disponivel_horario", True)

        data_entrega_agendada = None
        if (
            tipo_entrega == "entrega" and not entrega_ok and entrega_ok_base
            and fp.get("data_entrega_agendada")
            and _loja_permite_agendamento_entrega(cnpjloja)
        ):
            # Loja aceita entrega, habilitou agendamento e o endereço está no
            # raio — só não está disponível agora (feriado/fora do horário de
            # entrega). Cliente pediu pra agendar pra outro dia; revalida a
            # data no servidor, não confia só no que o carrinho mandou.
            try:
                _data_agendada = datetime.strptime(fp["data_entrega_agendada"], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                _data_agendada = None
            _hoje = _data_hoje_br()
            if (
                _data_agendada
                and _hoje <= _data_agendada <= _hoje + timedelta(days=14)
                and _entrega_disponivel_na_data(cnpjloja, _data_agendada)
            ):
                data_entrega_agendada = _data_agendada
                entrega_ok = True  # segue como pedido de entrega, só que agendado

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
        consumidor_id_checkout = session.get("consumidor_id")
        is_assinante_checkout = _consumidor_e_assinante(consumidor_id_checkout, cnpjloja)

        frete_valor_cheio = float(loja.get("valor_frete") or 0) if tipo_entrega == "entrega" and loja.get("cobra_frete") else 0.0
        frete_gratis_assinante = False
        assinatura_id_beneficio = None
        if tipo_entrega == "entrega" and frete_valor_cheio > 0 and is_assinante_checkout:
            cur.execute(
                """
                SELECT a.id FROM ecommerce_assinantes a
                JOIN ecommerce_planos_assinatura p ON p.cnpjloja = a.cnpjloja
                WHERE a.consumidor_id=%s AND a.cnpjloja=%s
                  AND a.status='ativo' AND a.pagamento_status='aprovado'
                  AND COALESCE(p.frete_gratis_primeira_entrega, FALSE) = TRUE
                  AND COALESCE(a.frete_gratis_primeira_usado, FALSE) = FALSE
                LIMIT 1
                """,
                (consumidor_id_checkout, cnpjloja),
            )
            _ass_beneficio = cur.fetchone()
            if _ass_beneficio:
                frete_gratis_assinante = True
                assinatura_id_beneficio = _ass_beneficio["id"]
        frete_valor = 0.0 if frete_gratis_assinante else frete_valor_cheio
        total = produtos_total + frete_valor

        # Apply coupon if provided
        _ensure_cupons_schema()
        cupom_id_aplicado = None
        desconto_cupom = 0.0
        cupom_codigo = (fp.get("cupom_codigo") or "").strip().upper()
        if cupom_codigo:
            cur.execute(
                """
                SELECT c.* FROM ecommerce_cupons c
                JOIN ecommerce_cupons_lojas cl ON cl.cupom_id = c.id AND cl.cnpjloja = %(cnpjloja)s
                WHERE upper(c.codigo)=%(codigo)s AND c.ativo=TRUE
                  AND (c.valido_ate IS NULL OR c.valido_ate >= CURRENT_DATE)
                  AND (c.uso_maximo = 0 OR cl.usos_count < c.uso_maximo)
                  AND (COALESCE(c.so_assinantes, FALSE) = FALSE OR %(is_assinante)s)
                  AND (
                    c.publico = 'todos'
                    OR (c.publico = 'especifico' AND EXISTS (
                        SELECT 1 FROM ecommerce_cupons_clientes cc
                        WHERE cc.cupom_id = c.id AND cc.consumidor_id = %(consumidor_id)s
                    ))
                    OR (c.publico = 'primeira_compra' AND NOT EXISTS (
                        SELECT 1 FROM ecommerce_pedidos prev
                        WHERE prev.cnpjloja = %(cnpjloja)s AND prev.consumidor_id = %(consumidor_id)s
                        AND prev.status NOT IN ('cancelado')
                    ))
                    OR (c.publico = 'frequente' AND (
                        SELECT COUNT(*) FROM ecommerce_pedidos prev
                        WHERE prev.cnpjloja = %(cnpjloja)s AND prev.consumidor_id = %(consumidor_id)s
                        AND prev.status NOT IN ('cancelado')
                    ) >= c.min_compras)
                  )
                LIMIT 1
                """,
                {"cnpjloja": cnpjloja, "codigo": cupom_codigo, "consumidor_id": consumidor_id_checkout,
                 "is_assinante": is_assinante_checkout},
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
            SELECT c.id, c.desconto_tipo, c.desconto_valor, c.escopo,
                   COALESCE(c.escopo_categorias,'') AS escopo_categorias,
                   COALESCE(c.escopo_eans,'') AS escopo_eans,
                   COALESCE(c.qtd_minima,0) AS qtd_minima
            FROM ecommerce_cupons c
            JOIN ecommerce_cupons_lojas cl ON cl.cupom_id = c.id AND cl.cnpjloja = %s
            WHERE c.ativo=TRUE
              AND COALESCE(c.tipo_regra,'codigo')='quantidade'
              AND c.qtd_minima > 0 AND %s >= c.qtd_minima
              AND (c.valido_ate IS NULL OR c.valido_ate >= CURRENT_DATE)
              AND (c.uso_maximo = 0 OR cl.usos_count < c.uso_maximo)
              AND (COALESCE(c.so_assinantes, FALSE) = FALSE OR %s)
            ORDER BY c.qtd_minima DESC, c.desconto_valor DESC
        """, (cnpjloja, total_itens_qty, is_assinante_checkout))
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
            SELECT c.id, c.desconto_tipo, c.desconto_valor, c.escopo,
                   COALESCE(c.escopo_categorias,'') AS escopo_categorias,
                   COALESCE(c.escopo_eans,'') AS escopo_eans
            FROM ecommerce_cupons c
            JOIN ecommerce_cupons_lojas cl ON cl.cupom_id = c.id AND cl.cnpjloja = %s
            WHERE c.ativo=TRUE
              AND COALESCE(c.tipo_regra,'codigo')='pagamento'
              AND (c.forma_pagamento = %s OR c.forma_pagamento = 'todos')
              AND (c.valido_ate IS NULL OR c.valido_ate >= CURRENT_DATE)
              AND (c.uso_maximo = 0 OR cl.usos_count < c.uso_maximo)
              AND (COALESCE(c.so_assinantes, FALSE) = FALSE OR %s)
            ORDER BY c.desconto_valor DESC LIMIT 1
        """, (cnpjloja, pagamento, is_assinante_checkout))
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

        # Recalcula no servidor; nao confia apenas no booleano enviado pelo carrinho.
        for item in itens:
            nome_item = item.get("nome") or ""
            anvisa_item = {}
            chave_item = _anvisa_chave(nome_item)
            if chave_item:
                try:
                    cur.execute(
                        "SELECT alertas, como_usar, nome_anvisa, principio_ativo, tarja, receita_retida "
                        "FROM anvisa_cache WHERE chave=%s AND encontrado=TRUE LIMIT 1",
                        (chave_item,),
                    )
                    anvisa_item = dict(cur.fetchone() or {})
                except Exception:
                    anvisa_item = {}
            tarja_item = _detectar_tarja(anvisa_item)
            item["requer_receita"] = _exige_receita_digital_entrega(anvisa_item, nome_item)

        # Receita digital no checkout apenas para retencao/controle; tarja vermelha simples nao bloqueia.
        tem_retencao_entrega = tipo_entrega == "entrega" and any(i.get("requer_receita") for i in itens)
        if tem_retencao_entrega and not receita_url:
            return jsonify({
                "error": "Carrinho contém medicamento com retenção/controle. Envie o PDF original da receita digital para finalizar com entrega.",
            }), 400
        if tem_retencao_entrega and not receita_declaracao_ok:
            return jsonify({
                "error": "Confirme que a receita enviada é digital original, com assinatura eletrônica verificável, e não foto, print ou receita escaneada.",
            }), 400
        # Checagem de extensão: receita_url é JSON array; valida cada URL individualmente
        if tem_retencao_entrega and receita_url:
            try:
                _urls_check = json.loads(receita_url) if receita_url.startswith("[") else [receita_url]
            except Exception:
                _urls_check = [receita_url]
            if any(not u.lower().split("?", 1)[0].endswith(".pdf") for u in _urls_check if u):
                return jsonify({
                    "error": "Para entrega com receita, o sistema aceita apenas PDF original da receita digital assinada.",
                }), 400

        receita_status = "pendente" if (tem_retencao_entrega and receita_url) else None
        codigo_entrega = _novo_codigo_entrega() if tipo_entrega == "entrega" else None

        cur.execute(
            """
            INSERT INTO ecommerce_pedidos
              (cnpjloja, consumidor_id, cliente_nome, cliente_telefone, cliente_documento, cliente_email, forma_pagamento, total,
               tipo_entrega, endereco_entrega, entrega_lat, entrega_lng, entrega_distancia_km, codigo_entrega, frete_valor,
               cupom_id, desconto_cupom, receita_url, receita_status, receita_declaracao_digital_valida, data_entrega_agendada,
               frete_gratis_assinante)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                cnpjloja,
                session.get("consumidor_id"),
                (cliente.get("nome") or "").strip(),
                (cliente.get("telefone") or "").strip(),
                _digits(cliente.get("documento")),
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
                data_entrega_agendada,
                frete_gratis_assinante,
            ),
        )
        pedido_id = str(cur.fetchone()["id"])

        if frete_gratis_assinante and assinatura_id_beneficio:
            cur.execute(
                "UPDATE ecommerce_assinantes SET frete_gratis_primeira_usado=TRUE WHERE id=%s",
                (assinatura_id_beneficio,),
            )
            cur.execute(
                """INSERT INTO ecommerce_repasses_admin (cnpjloja, pedido_id, tipo, valor, descricao)
                   VALUES (%s, %s, 'frete_primeira_entrega', %s, %s)""",
                (cnpjloja, pedido_id, round(frete_valor_cheio, 2),
                 f"Frete grátis da 1ª entrega do assinante — pedido #{pedido_id[:8].upper()}"),
            )
        if desconto_total_aplicado > 0:
            cur.execute(
                """INSERT INTO ecommerce_repasses_admin (cnpjloja, pedido_id, tipo, valor, descricao)
                   VALUES (%s, %s, 'cupom', %s, %s)""",
                (cnpjloja, pedido_id, round(desconto_total_aplicado, 2),
                 f"Cupom/desconto aplicado — pedido #{pedido_id[:8].upper()}"),
            )
        if desconto_promo_assinante > 0:
            cur.execute(
                """INSERT INTO ecommerce_repasses_admin (cnpjloja, pedido_id, tipo, valor, descricao)
                   VALUES (%s, %s, 'promocao', %s, %s)""",
                (cnpjloja, pedido_id, round(desconto_promo_assinante, 2),
                 f"Promoção só-assinantes aplicada — pedido #{pedido_id[:8].upper()}"),
            )

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

        # Incrementa contagem de uso para cada regra aplicada — contador e
        # limite sao por loja (ecommerce_cupons_lojas), nao global da regra.
        if cupom_id_aplicado:
            cur.execute(
                "UPDATE ecommerce_cupons_lojas SET usos_count = usos_count + 1 WHERE cupom_id=%s AND cnpjloja=%s",
                (cupom_id_aplicado, cnpjloja),
            )
        if cupom_pag_id:
            cur.execute(
                "UPDATE ecommerce_cupons_lojas SET usos_count = usos_count + 1 WHERE cupom_id=%s AND cnpjloja=%s",
                (cupom_pag_id, cnpjloja),
            )

        conn.commit()
        _notificar_consumidor(
            session.get("consumidor_id"),
            "pedido",
            "Pedido recebido",
            f"Seu pedido #{pedido_id[:8].upper()} foi registrado e ja esta com a farmacia.",
            url=url_for("meu_pedido_detalhe", pedido_id=pedido_id),
            pedido_id=pedido_id,
            conn=conn,
        )
        conn.commit()
        _registrar_status_pedido(pedido_id, "pendente")

        # Notifica loja via WhatsApp ao receber novo pedido
        try:
            _wpp_loja = re.sub(r'\D', '', loja.get('whatsapp_pedidos') or loja.get('telefone') or '')
            if _wpp_loja and WASENDER_API_KEY:
                _total_fmt = f"R${round(total, 2):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                _wa_send(
                    _wpp_loja,
                    f'🛍️ Novo pedido recebido!\n'
                    f'Pedido #{pedido_id[:8].upper()}\n'
                    f'Total: {_total_fmt}\n'
                    f'Ver pedido:\n'
                    f'{_wa_base_url()}/painel/pedidos/{pedido_id}'
                )
        except Exception:
            pass

        mp_init = None
        mp_payment = {}
        payment_status = "pending"
        pix_erro = None
        mp_erro = None
        gateway_pagamento = (loja.get("gateway_alternativo") or "mercadopago").strip()
        gateway_habilitado = (
            gateway_pagamento == "mercadopago"
            or (gateway_pagamento == "asaas" and bool(loja.get("asaas_api_key")))
            or (gateway_pagamento == "pagbank" and bool(loja.get("pagbank_token")) and bool(loja.get("pagbank_public_key")))
        )
        if receita_status == "pendente":
            pass  # pagamento criado apenas após aprovação da receita
        elif pagamento == "mercadopago" and gateway_pagamento == "asaas" and loja.get("asaas_api_key"):
            cur.execute(
                """
                UPDATE ecommerce_pedidos
                SET pagamento_status=%s, atualizado_em=NOW()
                WHERE id=%s
                """,
                ("PENDING", pedido_id),
            )
            conn.commit()
        elif pagamento == "pix" and gateway_pagamento == "asaas" and loja.get("asaas_api_key"):
            try:
                mp_payment = _criar_pagamento_pix_asaas(
                    loja["asaas_api_key"], pedido_id, total, cliente, session.get("consumidor_id"), cnpjloja
                ) or {}
                payment_status = mp_payment.get("status") or "PENDING"
                pedido_status = _asaas_status_para_pedido(payment_status)
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
                _registrar_status_pedido(pedido_id, pedido_status)
                if pedido_status == "pago":
                    _alpha_export_paid_order_safe(pedido_id)
            except Exception as exc:
                pix_erro = str(exc)
        elif pagamento == "mercadopago" and gateway_pagamento != "mercadopago":
            mp_erro = f"Gateway {gateway_pagamento} configurado, mas o checkout transparente deste gateway ainda não está ativo."
        elif pagamento == "pix" and gateway_pagamento != "mercadopago":
            pix_erro = f"Gateway {gateway_pagamento} configurado, mas o PIX automático deste gateway ainda não está ativo."
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
            elif loja.get("mp_public_key"):
                cur.execute(
                    """
                    UPDATE ecommerce_pedidos
                    SET pagamento_status=%s, mp_preference_id=NULL, mp_init_point=NULL
                    WHERE id=%s
                    """,
                    (payment_status, pedido_id),
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
                _registrar_status_pedido(pedido_id, pedido_status)
                if pedido_status == "pago":
                    _alpha_export_paid_order_safe(pedido_id)

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
            "mp_public_key": loja.get("mp_public_key") or "",
            "gateway_pagamento": gateway_pagamento,
            "gateway_habilitado": gateway_habilitado,
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


@app.post("/api/pedido/<pedido_id>/cartao-transparente")
def api_pedido_cartao_transparente(pedido_id):
    if not session.get("consumidor_id"):
        return jsonify({"error": "Faça login para pagar."}), 401
    _ensure_payment_schema()
    data = request.get_json(force=True) or {}
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.id, p.cnpjloja, p.consumidor_id, p.total, p.status, p.pagamento_status,
               p.cliente_nome, p.cliente_email, c.mp_access_token,
               COALESCE(c.gateway_alternativo, 'mercadopago') AS gateway_alternativo
        FROM ecommerce_pedidos p
        JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
        WHERE p.id=%s LIMIT 1
        """,
        (pedido_id,),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        return jsonify({"error": "Pedido não encontrado."}), 404
    if str(row.get("consumidor_id")) != str(session.get("consumidor_id")):
        cur.close()
        return jsonify({"error": "Pedido não pertence ao usuário logado."}), 403
    if not row.get("mp_access_token"):
        cur.close()
        return jsonify({"error": "Loja sem Mercado Pago configurado."}), 400
    if (row.get("gateway_alternativo") or "mercadopago") != "mercadopago":
        cur.close()
        return jsonify({
            "error": "Esta loja selecionou outro gateway. O checkout transparente dele precisa ser ativado antes de processar cartao por aqui."
        }), 400
    if (row.get("pagamento_status") or "").lower() == "approved":
        cur.close()
        return jsonify({"status": "approved"})
    payer = data.get("payer") or {}
    identification = payer.get("identification") or {}
    doc_number = _digits(identification.get("number"))
    doc_type = (identification.get("type") or ("CNPJ" if len(doc_number) == 14 else "CPF")).upper()
    if doc_type not in {"CPF", "CNPJ"} or len(doc_number) not in {11, 14}:
        cur.close()
        return jsonify({"error": "Informe CPF ou CNPJ valido do pagador."}), 400
    data["payer"] = {
        **payer,
        "identification": {"type": doc_type, "number": doc_number},
    }
    cliente = {
        "nome": row.get("cliente_nome") or session.get("consumidor_nome") or "",
        "email": row.get("cliente_email") or session.get("consumidor_email") or "",
    }
    consumidor_id = str(row.get("consumidor_id") or "")
    cnpjloja = str(row.get("cnpjloja") or "")
    mp_customer_id = None
    pay = _criar_pagamento_cartao_mp(row["mp_access_token"], pedido_id, row["total"], cliente, data, mp_customer_id)
    erro = pay.get("_erro") if isinstance(pay, dict) else None
    if erro:
        cur.close()
        return jsonify({"error": erro}), 400
    status = (pay.get("status") or "pending").lower()
    detail = pay.get("status_detail")
    pedido_status = _status_pedido_por_pagamento(status)
    cur.execute(
        """
        UPDATE ecommerce_pedidos
        SET mp_payment_id=%s,
            pagamento_status=%s,
            pagamento_status_detail=%s,
            status=%s,
            pagamento_confirmado_em=CASE WHEN %s='pago' THEN COALESCE(pagamento_confirmado_em, NOW()) ELSE pagamento_confirmado_em END,
            atualizado_em=NOW()
        WHERE id=%s
        """,
        (str(pay.get("id") or ""), status, detail, pedido_status, pedido_status, pedido_id),
    )
    conn.commit()
    cur.close()
    _registrar_status_pedido(pedido_id, pedido_status)
    if pedido_status == "pago":
        _finalizar_pos_pagamento_aprovado_async(pedido_id)
    return jsonify({
        "status": status,
        "status_detail": detail,
        "pedido_status": pedido_status,
        "payment_id": str(pay.get("id") or ""),
    })


@app.post("/api/pedido/<pedido_id>/asaas-cartao")
def api_pedido_asaas_cartao(pedido_id):
    if not session.get("consumidor_id"):
        return jsonify({"error": "Faça login para pagar."}), 401
    _ensure_payment_schema()
    _ensure_gateway_alt_columns()
    data = request.get_json(force=True) or {}
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.id, p.cnpjloja, p.consumidor_id, p.total, p.status, p.pagamento_status,
               p.cliente_nome, p.cliente_email, p.cliente_telefone,
               c.asaas_api_key, COALESCE(c.gateway_alternativo, 'mercadopago') AS gateway_alternativo
        FROM ecommerce_pedidos p
        JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
        WHERE p.id=%s LIMIT 1
        """,
        (pedido_id,),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        return jsonify({"error": "Pedido não encontrado."}), 404
    if str(row.get("consumidor_id")) != str(session.get("consumidor_id")):
        cur.close()
        return jsonify({"error": "Pedido não pertence ao usuário logado."}), 403
    if (row.get("gateway_alternativo") or "") != "asaas":
        cur.close()
        return jsonify({"error": "Esta loja não está configurada para Asaas."}), 400
    if not row.get("asaas_api_key"):
        cur.close()
        return jsonify({"error": "Loja sem API Key Asaas configurada."}), 400
    holder = data.get("holder") or {}
    doc = _digits(holder.get("cpfCnpj") or "")
    cep = _digits(holder.get("postalCode") or "")
    if len(doc) not in {11, 14}:
        cur.close()
        return jsonify({"error": "Informe CPF ou CNPJ válido do pagador."}), 400
    if len(cep) != 8:
        cur.close()
        return jsonify({"error": "Informe CEP válido do titular do cartão."}), 400
    cliente = {
        "nome": row.get("cliente_nome") or session.get("consumidor_nome") or "",
        "email": row.get("cliente_email") or session.get("consumidor_email") or "",
        "telefone": row.get("cliente_telefone") or session.get("consumidor_telefone") or "",
    }
    if not holder.get("email"):
        holder["email"] = cliente["email"]
    if not holder.get("phone"):
        holder["phone"] = cliente["telefone"]
    data["holder"] = holder
    try:
        pay = _criar_pagamento_cartao_asaas(
            row["asaas_api_key"], pedido_id, row["total"], cliente,
            str(row.get("consumidor_id") or ""), str(row.get("cnpjloja") or ""), data
        )
    except Exception as exc:
        cur.close()
        return jsonify({"error": str(exc)}), 400
    status = (pay.get("status") or "PENDING").upper()
    pedido_status = _asaas_status_para_pedido(status)
    cur.execute(
        """
        UPDATE ecommerce_pedidos
        SET mp_payment_id=%s,
            pagamento_status=%s,
            pagamento_status_detail=%s,
            status=%s,
            pagamento_confirmado_em=CASE WHEN %s='pago' THEN COALESCE(pagamento_confirmado_em, NOW()) ELSE pagamento_confirmado_em END,
            atualizado_em=NOW()
        WHERE id=%s
        """,
        (str(pay.get("id") or ""), status, status, pedido_status, pedido_status, pedido_id),
    )
    conn.commit()
    cur.close()
    _registrar_status_pedido(pedido_id, pedido_status)
    if pedido_status == "pago":
        _auto_pronto_retirada(pedido_id)
        _alpha_export_paid_order_safe(pedido_id)
        _notificar_pedido_evento(
            pedido_id, "pagamento", "Pagamento aprovado",
            f"O pagamento do pedido #{str(pedido_id)[:8].upper()} foi confirmado.",
        )
    return jsonify({"status": status, "pedido_status": pedido_status, "payment_id": str(pay.get("id") or "")})


@app.get("/api/cartoes-salvos/<cnpjloja>")
def api_listar_cartoes_salvos(cnpjloja):
    _ensure_cartoes_schema()
    consumidor_id = str(session.get("consumidor_id") or "")
    if not consumidor_id:
        return jsonify({"error": "Faça login."}), 401
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT mp_customer_id FROM ecommerce_mp_clientes WHERE consumidor_id=%s AND cnpjloja=%s LIMIT 1",
        (consumidor_id, cnpjloja)
    )
    mp_row = cur.fetchone()
    mp_customer_id = mp_row["mp_customer_id"] if mp_row else None
    cur.execute(
        "SELECT mp_card_id, ultimos_quatro, mes_vencimento, ano_vencimento, payment_method_id FROM ecommerce_cartoes_salvos WHERE consumidor_id=%s AND cnpjloja=%s ORDER BY criado_em DESC",
        (consumidor_id, cnpjloja)
    )
    cards = [
        {
            "id": r["mp_card_id"],
            "ultimos_quatro": r["ultimos_quatro"],
            "mes_vencimento": r["mes_vencimento"],
            "ano_vencimento": r["ano_vencimento"],
            "payment_method_id": r["payment_method_id"],
        }
        for r in cur.fetchall()
    ]
    cur.close()
    return jsonify({
        "mp_customer_id": mp_customer_id,
        "cards": cards,
    })


@app.delete("/api/cartoes-salvos/<cnpjloja>/<card_id>")
def api_deletar_cartao_salvo(cnpjloja, card_id):
    _ensure_cartoes_schema()
    consumidor_id = str(session.get("consumidor_id") or "")
    if not consumidor_id:
        return jsonify({"error": "Faça login."}), 401
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT c.mp_access_token FROM ecommerce_config_loja c WHERE c.cnpjloja=%s LIMIT 1",
        (cnpjloja,)
    )
    cfg_row = cur.fetchone()
    if not cfg_row or not cfg_row.get("mp_access_token"):
        cur.close()
        return jsonify({"error": "Loja não configurada."}), 400
    cur.execute(
        "SELECT mp_customer_id FROM ecommerce_mp_clientes WHERE consumidor_id=%s AND cnpjloja=%s LIMIT 1",
        (consumidor_id, cnpjloja)
    )
    mp_row = cur.fetchone()
    if not mp_row:
        cur.close()
        return jsonify({"error": "Nenhum cartão salvo."}), 404
    mp_customer_id = mp_row["mp_customer_id"]
    try:
        _mp_request(cfg_row["mp_access_token"], f"/v1/customers/{mp_customer_id}/cards/{card_id}", method="DELETE")
    except Exception:
        pass
    cur.execute(
        "DELETE FROM ecommerce_cartoes_salvos WHERE consumidor_id=%s AND cnpjloja=%s AND mp_card_id=%s",
        (consumidor_id, cnpjloja, card_id)
    )
    conn.commit()
    cur.close()
    return jsonify({"ok": True})


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
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS cliente_documento TEXT")
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


def _asaas_base_url(api_key=""):
    if os.getenv("ASAAS_SANDBOX", "").strip().lower() in {"1", "true", "yes"}:
        return "https://api-sandbox.asaas.com/v3"
    if "sandbox" in (api_key or "").lower():
        return "https://api-sandbox.asaas.com/v3"
    return "https://api.asaas.com/v3"


def _asaas_request(api_key, path, payload=None, method=None, timeout=60):
    headers = {
        "access_token": api_key,
        "accept": "application/json",
        "content-type": "application/json",
        "User-Agent": "Poupaqui/1.0",
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{_asaas_base_url(api_key)}{path}",
        data=data,
        headers=headers,
        method=method or ("POST" if data else "GET"),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Asaas {e.code} {path}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Asaas URLError {path}: {e.reason}") from e


def _asaas_remote_ip():
    raw = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP") or request.remote_addr or ""
    return raw.split(",", 1)[0].strip() or "127.0.0.1"


def _asaas_status_para_pedido(status):
    status = (status or "").upper()
    if status in {"RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"}:
        return "pago"
    if status in {"REFUNDED", "CHARGEBACK_REQUESTED", "CHARGEBACK_DISPUTE", "AWAITING_CHARGEBACK_REVERSAL"}:
        return "cancelado"
    return "pendente"


def _asaas_cliente_payload(cliente, doc_number=""):
    nome = ((cliente or {}).get("nome") or "Cliente Poupaqui").strip()
    email = ((cliente or {}).get("email") or "").strip()
    telefone = _digits((cliente or {}).get("telefone") or "")
    payload = {"name": nome[:100]}
    if email and "@" in email:
        payload["email"] = email
    if doc_number:
        payload["cpfCnpj"] = _digits(doc_number)
    if telefone:
        payload["mobilePhone"] = telefone[-11:]
    return payload


def _obter_ou_criar_asaas_customer(api_key, consumidor_id, cnpjloja, cliente, doc_number=""):
    _ensure_gateway_alt_columns()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT asaas_customer_id FROM ecommerce_asaas_clientes WHERE consumidor_id=%s AND cnpjloja=%s LIMIT 1",
        (str(consumidor_id), cnpjloja),
    )
    row = cur.fetchone()
    if row:
        cur.close()
        return row["asaas_customer_id"]
    data = _asaas_request(api_key, "/customers", _asaas_cliente_payload(cliente, doc_number))
    customer_id = data.get("id")
    if not customer_id:
        cur.close()
        raise RuntimeError("Asaas não retornou ID do cliente.")
    cur.execute(
        "INSERT INTO ecommerce_asaas_clientes(consumidor_id, cnpjloja, asaas_customer_id) VALUES(%s,%s,%s) ON CONFLICT(consumidor_id, cnpjloja) DO UPDATE SET asaas_customer_id=EXCLUDED.asaas_customer_id",
        (str(consumidor_id), cnpjloja, customer_id),
    )
    conn.commit()
    cur.close()
    return customer_id


def _asaas_due_date(days=0):
    return (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()


def _criar_pagamento_pix_asaas(api_key, pedido_id, total, cliente, consumidor_id, cnpjloja):
    customer_id = _obter_ou_criar_asaas_customer(api_key, consumidor_id, cnpjloja, cliente)
    payment = _asaas_request(api_key, "/payments", {
        "customer": customer_id,
        "billingType": "PIX",
        "value": round(float(total), 2),
        "dueDate": _asaas_due_date(0),
        "description": f"Pedido Poupaqui #{str(pedido_id)[:8].upper()}",
        "externalReference": str(pedido_id),
    })
    payment_id = payment.get("id")
    qr = _asaas_request(api_key, f"/payments/{payment_id}/pixQrCode", method="GET") if payment_id else {}
    return {
        "id": payment_id,
        "status": payment.get("status") or "PENDING",
        "status_detail": payment.get("status"),
        "qr_code": qr.get("payload"),
        "qr_code_base64": qr.get("encodedImage"),
    }


def _asaas_card_payload(form):
    doc = _digits((form.get("holder") or {}).get("cpfCnpj") or (form.get("payer") or {}).get("doc_number"))
    phone = _digits((form.get("holder") or {}).get("phone") or "")
    return {
        "creditCard": {
            "holderName": (form.get("card_holder_name") or "").strip(),
            "number": _digits(form.get("card_number")),
            "expiryMonth": str(form.get("expiry_month") or "").zfill(2),
            "expiryYear": str(form.get("expiry_year") or ""),
            "ccv": _digits(form.get("ccv")),
        },
        "creditCardHolderInfo": {
            "name": (form.get("holder") or {}).get("name") or form.get("card_holder_name") or "",
            "email": (form.get("holder") or {}).get("email") or "",
            "cpfCnpj": doc,
            "postalCode": _digits((form.get("holder") or {}).get("postalCode")),
            "addressNumber": str((form.get("holder") or {}).get("addressNumber") or "S/N"),
            "phone": phone[-11:] if phone else "",
            "mobilePhone": phone[-11:] if phone else "",
        },
    }


def _criar_pagamento_cartao_asaas(api_key, pedido_id, total, cliente, consumidor_id, cnpjloja, form):
    holder = form.get("holder") or {}
    doc = _digits(holder.get("cpfCnpj") or (form.get("payer") or {}).get("doc_number"))
    customer_id = _obter_ou_criar_asaas_customer(api_key, consumidor_id, cnpjloja, cliente, doc)
    card_payload = _asaas_card_payload(form)
    payload = {
        "customer": customer_id,
        "billingType": "CREDIT_CARD",
        "value": round(float(total), 2),
        "dueDate": _asaas_due_date(0),
        "description": f"Pedido Poupaqui #{str(pedido_id)[:8].upper()}",
        "externalReference": str(pedido_id),
        "remoteIp": _asaas_remote_ip(),
        **card_payload,
    }
    return _asaas_request(api_key, "/payments", payload, timeout=75)


def _criar_assinatura_cartao_asaas(api_key, assinatura_id, cnpjloja, plano, cliente, consumidor_id, form):
    holder = form.get("holder") or {}
    doc = _digits(holder.get("cpfCnpj") or (form.get("payer") or {}).get("doc_number"))
    customer_id = _obter_ou_criar_asaas_customer(api_key, consumidor_id, cnpjloja, cliente, doc)
    card_payload = _asaas_card_payload(form)
    payload = {
        "customer": customer_id,
        "billingType": "CREDIT_CARD",
        "value": round(float(plano.get("preco_mensal") or plano.get("preco") or 0), 2),
        "nextDueDate": _asaas_due_date(0),
        "cycle": "MONTHLY",
        "description": f"Assinatura {plano.get('plano_nome') or plano.get('nome') or 'Clube'} - {plano.get('razao') or 'Poupaqui'}"[:255],
        "externalReference": f"assinatura:{assinatura_id}",
        "remoteIp": _asaas_remote_ip(),
        **card_payload,
    }
    return _asaas_request(api_key, "/subscriptions", payload, timeout=75)


def _obter_ou_criar_mp_customer(access_token, consumidor_id, cnpjloja, cliente):
    try:
        _ensure_cartoes_schema()
        conn = db()
        cur = conn.cursor()
        cur.execute(
            "SELECT mp_customer_id FROM ecommerce_mp_clientes WHERE consumidor_id=%s AND cnpjloja=%s LIMIT 1",
            (str(consumidor_id), cnpjloja)
        )
        row = cur.fetchone()
        if row:
            cur.close()
            return str(row["mp_customer_id"])
        email = (cliente or {}).get("email") or f"cliente_{str(consumidor_id)[:8]}@poupaqui.com.br"
        nome = (cliente or {}).get("nome") or ""
        parts = (nome or "").split(None, 1)
        payload = {"email": email.strip()}
        if parts:
            payload["first_name"] = parts[0]
        if len(parts) > 1:
            payload["last_name"] = parts[1]
        data = _mp_request(access_token, "/v1/customers", payload)
        mp_customer_id = data.get("id")
        if not mp_customer_id:
            cur.close()
            return None
        cur.execute(
            "INSERT INTO ecommerce_mp_clientes(consumidor_id, cnpjloja, mp_customer_id) VALUES(%s, %s, %s) ON CONFLICT(consumidor_id, cnpjloja) DO NOTHING",
            (str(consumidor_id), cnpjloja, str(mp_customer_id))
        )
        conn.commit()
        cur.close()
        return str(mp_customer_id)
    except Exception:
        return None


def _registrar_cartao_de_pagamento(conn, payment_data, consumidor_id, cnpjloja):
    try:
        card = payment_data.get("card") or {}
        card_id = card.get("id")
        if not card_id:
            return
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO ecommerce_cartoes_salvos(consumidor_id, cnpjloja, mp_card_id, ultimos_quatro, mes_vencimento, ano_vencimento, payment_method_id) VALUES(%s, %s, %s, %s, %s, %s, %s) ON CONFLICT(consumidor_id, cnpjloja, mp_card_id) DO NOTHING",
            (
                str(consumidor_id), cnpjloja, str(card_id),
                card.get("last_four_digits") or "",
                card.get("expiration_month"),
                card.get("expiration_year"),
                payment_data.get("payment_method_id") or card.get("payment_method_id") or "",
            )
        )
        conn.commit()
        cur.close()
    except Exception:
        pass


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
            return {"_erro": "Mercado Pago não retornou link de pagamento."}
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


def _criar_pagamento_cartao_mp(access_token, pedido_id, total, cliente, form, mp_customer_id=None):
    try:
        payer = dict(form.get("payer") or {})
        payer["email"] = (
            payer.get("email")
            or (cliente or {}).get("email")
            or f"comprador{int(time.time())}@poupaqui.com.br"
        ).strip()
        if mp_customer_id:
            payer["id"] = mp_customer_id
            payer["type"] = "customer"
        payload = {
            "transaction_amount": round(float(total), 2),
            "token": form.get("token"),
            "description": f"Pedido Poupaqui #{str(pedido_id)[:8].upper()}",
            "installments": int(form.get("installments") or 1),
            "payment_method_id": form.get("payment_method_id"),
            "external_reference": str(pedido_id),
            "payer": payer,
        }
        issuer_id = form.get("issuer_id")
        if issuer_id:
            payload["issuer_id"] = issuer_id
        notification_url = _mp_notification_url()
        if notification_url:
            payload["notification_url"] = notification_url
        return _mp_request(access_token, "/v1/payments", payload, idempotency_key=f"{pedido_id}-card")
    except Exception as exc:
        return {"_erro": str(exc)}


def _criar_assinatura_recorrente_mp(access_token, assinatura_id, plano, cliente):
    try:
        preco = float(plano.get("preco_mensal") or plano.get("preco") or 0)
        email = (_payer_payload(cliente).get("email") or "").strip()
        payload = {
            "reason": f"Assinatura {plano.get('plano_nome') or plano.get('nome') or 'Clube'} - {plano.get('razao') or 'Poupaqui'}"[:255],
            "external_reference": f"assinatura:{assinatura_id}",
            "payer_email": email,
            "auto_recurring": {
                "frequency": 1,
                "frequency_type": "months",
                "transaction_amount": round(preco, 2),
                "currency_id": "BRL",
            },
        }
        notification_url = _mp_notification_url()
        if notification_url:
            payload["notification_url"] = notification_url
            payload["back_url"] = f"{_public_base_url()}/minhas-assinaturas"
        data = _mp_request(access_token, "/preapproval", payload, idempotency_key=f"assin-{assinatura_id}-preapproval")
        init_point = data.get("init_point") or data.get("sandbox_init_point")
        if not init_point:
            return {"_erro": "Mercado Pago não retornou link da assinatura recorrente."}
        return {"id": data.get("id"), "init_point": init_point, "status": data.get("status")}
    except Exception as exc:
        import logging
        logging.error("Mercado Pago recorrencia falhou para assinatura %s: %s", assinatura_id, exc)
        return {"_erro": str(exc)}


def _criar_assinatura_recorrente_cartao_mp(access_token, assinatura_id, plano, cliente, card_token_id):
    try:
        preco = float(plano.get("preco_mensal") or plano.get("preco") or 0)
        email = (_payer_payload(cliente).get("email") or "").strip()
        payload = {
            "reason": f"Assinatura {plano.get('plano_nome') or plano.get('nome') or 'Clube'} - {plano.get('razao') or 'Poupaqui'}"[:255],
            "external_reference": f"assinatura:{assinatura_id}",
            "payer_email": email,
            "card_token_id": card_token_id,
            "status": "authorized",
            "auto_recurring": {
                "frequency": 1,
                "frequency_type": "months",
                "transaction_amount": round(preco, 2),
                "currency_id": "BRL",
            },
        }
        notification_url = _mp_notification_url()
        if notification_url:
            payload["notification_url"] = notification_url
            payload["back_url"] = f"{_public_base_url()}/minhas-assinaturas"
        return _mp_request(access_token, "/preapproval", payload, idempotency_key=f"assin-{assinatura_id}-brick")
    except Exception as exc:
        return {"_erro": str(exc)}


def _ativar_assinatura_row(cur, assinatura_id, recorrente=False):
    if recorrente:
        cur.execute(
            """UPDATE ecommerce_assinantes
               SET status='ativo', pagamento_status='aprovado',
                   data_inicio=COALESCE(data_inicio, NOW()),
                   data_fim=NULL, assinatura_recorrente=TRUE
               WHERE id=%s""",
            (assinatura_id,),
        )
    else:
        cur.execute(
            """UPDATE ecommerce_assinantes
               SET status='ativo', pagamento_status='aprovado',
                   data_inicio=COALESCE(data_inicio, NOW()),
                   data_fim=NOW() + INTERVAL '30 days',
                   assinatura_recorrente=FALSE
               WHERE id=%s""",
            (assinatura_id,),
        )


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
            "SELECT status, pagamento_status FROM ecommerce_pedidos WHERE id=%s LIMIT 1",
            (pedido_id,),
        )
        old_payment_row = cur.fetchone() or {}
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
        result = dict(updated) if updated else None
        if result and result.get("status") != (old_payment_row.get("status") or ""):
            _registrar_status_pedido(pedido_id, result["status"])
        # auto-avanço para retirada com flag
        status_changed_to_paid = (
            result
            and result.get("status") == "pago"
            and (
                (old_payment_row.get("status") or "") != "pago"
                or (old_payment_row.get("pagamento_status") or "") != status
            )
        )
        if status_changed_to_paid:
            _finalizar_pos_pagamento_aprovado_async(pedido_id)
        return result
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


def _ativar_assinatura_mp(payment_id: str):
    """Ativa assinatura quando MP confirma pagamento com external_reference=assinatura:<id>.

    Assinatura usa sempre a conta MP central do admin (nao a de cada loja),
    entao basta um unico token pra consultar o pagamento."""
    try:
        _ensure_assinatura_schema()
        token = _admin_mp_config().get("mp_access_token") or ""
        if not token:
            return
        conn = db(); cur = conn.cursor()
        cur.execute(
            "SELECT id FROM ecommerce_assinantes WHERE mp_payment_id=%s AND status='aguardando_pagamento' LIMIT 1",
            (str(payment_id),),
        )
        row = cur.fetchone()
        try:
            data = _mp_request(token, f"/v1/payments/{payment_id}", method="GET")
        except Exception:
            cur.close(); return
        payment_status = (data.get("status") or "").lower()
        if not row:
            ext_ref = str(data.get("external_reference") or "")
            if not ext_ref.startswith("assinatura:"):
                cur.close(); return
            assinatura_id = ext_ref.split(":", 1)[1]
            cur.execute(
                "SELECT id FROM ecommerce_assinantes WHERE id=%s AND status='aguardando_pagamento' LIMIT 1",
                (assinatura_id,),
            )
            row = cur.fetchone()
            if not row:
                cur.close(); return
            cur.execute("UPDATE ecommerce_assinantes SET mp_payment_id=%s WHERE id=%s", (str(payment_id), row["id"]))
        if payment_status != "approved":
            cur.close(); return
        _ativar_assinatura_row(cur, row["id"], recorrente=False)
        conn.commit(); cur.close()
    except Exception:
        pass


def _sincronizar_assinatura_preapproval(preapproval_id: str):
    """Assinatura recorrente sempre criada na conta MP central do admin."""
    try:
        _ensure_assinatura_schema()
        token = _admin_mp_config().get("mp_access_token") or ""
        if not token:
            return None
        conn = db(); cur = conn.cursor()
        cur.execute(
            "SELECT id FROM ecommerce_assinantes WHERE mp_preapproval_id=%s LIMIT 1",
            (str(preapproval_id),),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            return None
        data = _mp_request(token, f"/preapproval/{preapproval_id}", method="GET")
        status = (data.get("status") or "").lower()
        if status in {"authorized", "active"}:
            _ativar_assinatura_row(cur, row["id"], recorrente=True)
        elif status in {"cancelled", "paused"}:
            cur.execute(
                "UPDATE ecommerce_assinantes SET status='cancelado', data_fim=NOW() WHERE id=%s",
                (row["id"],),
            )
        conn.commit()
        cur.close()
        return status
    except Exception:
        return None


def _sincronizar_authorized_payment(authorized_payment_id: str):
    """Busca a cobranca recorrente individual no MP pra achar o preapproval_id
    dela e resincronizar aquela assinatura (detecta cobranca recusada rapido,
    sem esperar o proximo passo manual do consumidor ou o job periodico)."""
    try:
        token = _admin_mp_config().get("mp_access_token") or ""
        if not token:
            return
        data = _mp_request(token, f"/authorized_payments/{authorized_payment_id}", method="GET")
        preapproval_id = data.get("preapproval_id")
        if preapproval_id:
            _sincronizar_assinatura_preapproval(str(preapproval_id))
    except Exception:
        pass


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
    if event_type and "preapproval" in str(event_type).lower() and payment_id:
        _sincronizar_assinatura_preapproval(str(payment_id))
        return jsonify({"ok": True})
    if event_type and "authorized_payment" in str(event_type).lower() and payment_id:
        # Cada cobranca recorrente (aprovada ou recusada) do MP gera esse evento —
        # é o jeito mais rapido de detectar quando alguem "parou de pagar":
        # se a cobranca falhar, o MP eventualmente pausa/cancela o preapproval,
        # e essa checagem propaga isso pro nosso banco na hora.
        if os.environ.get("VERCEL"):
            _sincronizar_authorized_payment(str(payment_id))
        else:
            threading.Thread(
                target=lambda: _sincronizar_authorized_payment(str(payment_id)),
                daemon=True,
            ).start()
        return jsonify({"ok": True})
    if event_type and event_type not in {"payment", "merchant_order"}:
        return jsonify({"ok": True})
    if payment_id:
        # No Vercel (serverless) a instância é congelada após retornar a resposta,
        # então threads daemon nunca chegam a executar. Rodar de forma síncrona
        # garante que o pagamento seja processado e o pedido exportado ao Alpha.
        if os.environ.get("VERCEL"):
            _ativar_assinatura_mp(str(payment_id))
            _aplicar_webhook_pagamento(str(payment_id))
        else:
            threading.Thread(
                target=lambda: (_ativar_assinatura_mp(str(payment_id)), _aplicar_webhook_pagamento(str(payment_id))),
                daemon=True,
            ).start()
    return jsonify({"ok": True})


@app.post("/api/asaas/webhook")
def asaas_webhook():
    body = request.get_json(silent=True) or {}
    event = (body.get("event") or "").upper()
    payment = body.get("payment") if isinstance(body.get("payment"), dict) else {}
    payment_id = str(payment.get("id") or "")
    ext_ref = str(payment.get("externalReference") or "")
    status = (payment.get("status") or "").upper()
    if event.startswith("PAYMENT_") and (payment_id or ext_ref):
        conn = db()
        cur = conn.cursor()
        if ext_ref.startswith("assinatura:"):
            assinatura_id = ext_ref.split(":", 1)[1]
            if status in {"RECEIVED", "CONFIRMED"} or event in {"PAYMENT_RECEIVED", "PAYMENT_CONFIRMED"}:
                _ativar_assinatura_row(cur, assinatura_id, recorrente=True)
                cur.execute("UPDATE ecommerce_assinantes SET mp_payment_id=%s WHERE id=%s", (payment_id, assinatura_id))
            elif event in {"PAYMENT_DELETED", "PAYMENT_REFUNDED"}:
                cur.execute("UPDATE ecommerce_assinantes SET status='cancelado', data_fim=NOW() WHERE id=%s", (assinatura_id,))
            conn.commit()
            cur.close()
            return jsonify({"ok": True})
        pedido_status = _asaas_status_para_pedido(status)
        if ext_ref:
            cur.execute(
                """
                UPDATE ecommerce_pedidos
                SET mp_payment_id=COALESCE(NULLIF(%s,''), mp_payment_id),
                    pagamento_status=%s,
                    pagamento_status_detail=%s,
                    status=%s,
                    pagamento_confirmado_em=CASE WHEN %s='pago' THEN COALESCE(pagamento_confirmado_em, NOW()) ELSE pagamento_confirmado_em END,
                    atualizado_em=NOW()
                WHERE id=%s
                RETURNING id
                """,
                (payment_id, status or event, event, pedido_status, pedido_status, ext_ref),
            )
        else:
            cur.execute(
                """
                UPDATE ecommerce_pedidos
                SET pagamento_status=%s,
                    pagamento_status_detail=%s,
                    status=%s,
                    pagamento_confirmado_em=CASE WHEN %s='pago' THEN COALESCE(pagamento_confirmado_em, NOW()) ELSE pagamento_confirmado_em END,
                    atualizado_em=NOW()
                WHERE mp_payment_id=%s
                RETURNING id
                """,
                (status or event, event, pedido_status, pedido_status, payment_id),
            )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        if row:
            _registrar_status_pedido(str(row["id"]), pedido_status)
            if pedido_status == "pago":
                _finalizar_pos_pagamento_aprovado_async(str(row["id"]))
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
    _ensure_logo_url_column()
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
                       p.tipo_entrega, p.codigo_entrega, p.codigo_retirada, p.endereco_entrega,
                       p.receita_status,
                       u.razao, u.telefone,
                       c.whatsapp_pedidos, c.pix_chave, c.pix_nome, c.logo_url, c.mp_access_token
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
                if (
                    p.get("status") == "pago"
                    and (p.get("tipo_entrega") or "retirada") != "entrega"
                    and not p.get("codigo_retirada")
                ):
                    _auto_pronto_retirada(pid)
                    cur.execute(
                        """
                        SELECT p.id, p.cnpjloja, p.cliente_nome, p.forma_pagamento,
                               p.status, p.total, p.criado_em,
                               p.mp_preference_id, p.mp_payment_id, p.mp_init_point,
                               p.pix_qr_code, p.pix_qr_base64,
                               p.pagamento_status, p.pagamento_status_detail, p.pagamento_confirmado_em,
                               p.tipo_entrega, p.codigo_entrega, p.codigo_retirada, p.endereco_entrega,
                               p.receita_status,
                               u.razao, u.telefone,
                               c.whatsapp_pedidos, c.pix_chave, c.pix_nome, c.logo_url, c.mp_access_token
                        FROM ecommerce_pedidos p
                        JOIN users u ON u.cnpjloja = p.cnpjloja
                        LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
                        WHERE p.id = %s LIMIT 1
                        """,
                        (pid,),
                    )
                    p = cur.fetchone() or p
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


# ─── PAINEL: ALERTAS SONOROS (polling) ───────────────────────────────────────

@app.get("/api/painel/novos-alertas")
@painel_required
def api_painel_novos_alertas():
    _ensure_encomenda_schema()
    _ensure_reclamacao_schema()
    cnpjloja = session.get("cnpjloja")
    since_raw = request.args.get("since", "")
    # Captura "now" ANTES das queries — evita race condition onde um pedido
    # inserido durante o processamento cai no gap entre dois polls consecutivos
    query_time = datetime.now(timezone.utc)
    try:
        since_dt = datetime.fromisoformat(since_raw.replace("Z", "+00:00"))
    except Exception:
        return jsonify({"pedidos": 0, "reclamacoes": 0, "encomendas": 0,
                        "now": query_time.isoformat()})
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS n FROM ecommerce_pedidos WHERE cnpjloja=%s AND criado_em > %s"
        + (" AND tipo_entrega='entrega'" if _motoboy_logged() else ""),
        (cnpjloja, since_dt),
    )
    pedidos = cur.fetchone()["n"]
    if _motoboy_logged():
        cur.close()
        return jsonify({
            "pedidos": pedidos,
            "reclamacoes": 0,
            "encomendas": 0,
            "now": query_time.isoformat(),
        })
    cur.execute(
        "SELECT COUNT(*) AS n FROM ecommerce_reclamacoes WHERE cnpjloja=%s AND aberta_em > %s",
        (cnpjloja, since_dt),
    )
    reclamacoes = cur.fetchone()["n"]
    cur.execute(
        "SELECT COUNT(*) AS n FROM ecommerce_encomendas WHERE cnpjloja=%s AND criado_em > %s",
        (cnpjloja, since_dt),
    )
    encomendas = cur.fetchone()["n"]
    cur.close()
    return jsonify({
        "pedidos": pedidos,
        "reclamacoes": reclamacoes,
        "encomendas": encomendas,
        "now": query_time.isoformat(),
    })


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
    if _motoboy_logged():
        sql += " AND tipo_entrega = 'entrega'"
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
    if _motoboy_logged():
        receitas_pendentes = 0
    cur.close()
    return render_template("painel_pedidos.html", pedidos=pedidos, sf=sf,
                           sf_receita=sf_receita, receitas_pendentes=receitas_pendentes)


@app.get("/painel/pedidos/<pedido_id>")
@painel_required
def painel_pedido_detalhe(pedido_id):
    _ensure_payment_schema()
    _ensure_ml_shipping_schema()
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
    if _motoboy_logged() and pedido.get("tipo_entrega") != "entrega":
        flash("Este acesso é restrito aos pedidos de entrega.", "error")
        return redirect(url_for("painel_pedidos"))
    cur.execute("SELECT * FROM ecommerce_pedido_itens WHERE pedido_id=%s ORDER BY id", (pedido_id,))
    itens = [dict(i) for i in cur.fetchall()]
    cur.close()
    pedido_dict = dict(pedido)
    ml_shipping_info = None
    if pedido_dict.get("origem") == "mercado_livre":
        if pedido_dict.get("ml_order_id") and (not pedido_dict.get("ml_shipping_id") or not pedido_dict.get("ml_shipping_status")):
            synced_info, _sync_err = _ml_sync_shipment_for_pedido(
                pedido_id=pedido_id,
                cnpjloja=cnpjloja,
            )
            if synced_info:
                ml_shipping_info = synced_info
                cur2 = db().cursor()
                cur2.execute(
                    "SELECT * FROM ecommerce_pedidos WHERE id=%s AND cnpjloja=%s LIMIT 1",
                    (pedido_id, cnpjloja),
                )
                pedido_dict = dict(cur2.fetchone() or pedido_dict)
                cur2.close()
        if not ml_shipping_info:
            ml_shipping_info = {
                "id": pedido_dict.get("ml_shipping_id"),
                "status": pedido_dict.get("ml_shipping_status"),
                "substatus": pedido_dict.get("ml_shipping_substatus"),
                "mode": pedido_dict.get("ml_shipping_mode"),
                "logistic_type": pedido_dict.get("ml_logistic_type"),
                "tracking_number": pedido_dict.get("ml_tracking_number"),
                "tracking_method": pedido_dict.get("ml_tracking_method"),
                "instruction": _ml_shipping_instruction(
                    pedido_dict.get("ml_logistic_type"),
                    pedido_dict.get("ml_shipping_mode"),
                    pedido_dict.get("ml_shipping_status"),
                    pedido_dict.get("ml_shipping_substatus"),
                ),
            }
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
    if pedido_dict.get("data_entrega_agendada"):
        _data_ag = pedido_dict["data_entrega_agendada"]
        _dia_ag = (_data_ag.weekday() + 1) % 7
        _nome_dia_ag = next((n for d, n in _DIAS_SEMANA if d == _dia_ag), "")
        pedido_dict["data_entrega_agendada_label"] = f"{_nome_dia_ag}, {_data_ag.strftime('%d/%m/%Y')}"
    return render_template(
        "painel_pedido_detalhe.html",
        pedido=pedido_dict,
        itens=itens,
        receita_urls=receita_urls,
        ml_shipping=ml_shipping_info,
        alpha_enabled=_alpha_enabled(),
    )


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
        flash("Pedido não encontrado.", "error")
        return redirect(url_for("painel_pedidos"))
    synced = _sincronizar_pagamento_mp_para_pedido(pedido_id)
    if synced:
        flash("Pagamento sincronizado com o Mercado Pago.", "success")
    else:
        flash("Não foi possível sincronizar. Verifique se o pedido tem pagamento automático e token configurado.", "error")
    return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))


@app.post("/painel/pedidos/<pedido_id>/ml/sincronizar-envio")
@painel_required
def painel_ml_sincronizar_envio(pedido_id):
    cnpjloja = session.get("cnpjloja")
    info, err = _ml_sync_shipment_for_pedido(pedido_id=pedido_id, cnpjloja=cnpjloja)
    if info:
        flash("Envio Mercado Livre sincronizado.", "success")
    else:
        flash(f"Não foi possível sincronizar o envio: {err}", "error")
    return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))


@app.get("/painel/pedidos/<pedido_id>/ml/etiqueta")
@painel_required
def painel_ml_baixar_etiqueta(pedido_id):
    _ensure_ml_shipping_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute(
        """
        SELECT id, ml_order_id, ml_shipping_id
        FROM ecommerce_pedidos
        WHERE id=%s AND cnpjloja=%s AND COALESCE(origem, 'ecommerce')='mercado_livre'
        LIMIT 1
        """,
        (pedido_id, cnpjloja),
    )
    row = cur.fetchone(); cur.close()
    if not row:
        flash("Pedido Mercado Livre não encontrado.", "error")
        return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))
    shipping_id = row.get("ml_shipping_id")
    if not shipping_id:
        info, err = _ml_sync_shipment_for_pedido(pedido_id=pedido_id, cnpjloja=cnpjloja)
        shipping_id = (info or {}).get("id")
        if not shipping_id:
            flash(f"Envio sem etiqueta disponível: {err}", "error")
            return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))

    path = "/shipment_labels?" + urllib.parse.urlencode({
        "shipment_ids": shipping_id,
        "response_type": "pdf",
    })
    data, status, content_type = _ml_api_get_raw(path, accept="application/pdf")
    if not data or status not in (200, 201):
        flash(f"Não foi possível baixar a etiqueta no Mercado Livre: HTTP {status} - {content_type}", "error")
        return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))
    return Response(
        data,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="etiqueta-ml-{shipping_id}.pdf"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/painel/pedidos/<pedido_id>/status")
@painel_required
def painel_pedido_status(pedido_id):
    _ensure_previsao_entrega_column()
    _ensure_previsao_entrega_em_column()
    cnpjloja   = session.get("cnpjloja")
    novo_status = (request.form.get("status") or "").strip()
    previsao_entrega = (request.form.get("previsao_entrega") or "").strip() or None
    previsao_entrega_em = None
    _previsao_raw = (request.form.get("previsao_entrega_em") or "").strip()
    if _previsao_raw:
        try:
            previsao_entrega_em = datetime.strptime(_previsao_raw, "%Y-%m-%dT%H:%M")
        except ValueError:
            previsao_entrega_em = None
    if novo_status not in {"pendente", "pago", "pronto_retirada", "enviado", "entregue", "cancelado"}:
        flash("Status inválido.", "error")
        return redirect(url_for("painel_pedidos"))
    conn = db()
    cur  = conn.cursor()
    ml_order_id = None
    is_ml_status = False
    if novo_status in {"enviado", "entregue"}:
        cur.execute(
            "SELECT tipo_entrega, origem, ml_order_id FROM ecommerce_pedidos WHERE id=%s AND cnpjloja=%s LIMIT 1",
            (pedido_id, cnpjloja),
        )
        row = cur.fetchone()
        is_ml = row and row.get("origem") == "mercado_livre"
        is_ml_status = bool(is_ml)
    if novo_status == "entregue":
        if row and not is_ml:
            cur.close()
            flash(
                "Para finalizar entrega/retirada, use o formulário de confirmação informando o código do cliente.",
                "error",
            )
            return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))
        if is_ml:
            ml_order_id = row.get("ml_order_id")
    if novo_status == "pronto_retirada":
        codigo = _novo_codigo_entrega()
        cur.execute(
            "UPDATE ecommerce_pedidos SET status='pronto_retirada', codigo_retirada=COALESCE(codigo_retirada, %s), "
            "previsao_entrega=COALESCE(%s, previsao_entrega), previsao_entrega_em=COALESCE(%s, previsao_entrega_em), "
            "atualizado_em=NOW() WHERE id=%s AND cnpjloja=%s",
            (codigo, previsao_entrega, previsao_entrega_em, pedido_id, cnpjloja),
        )
    else:
        cur.execute(
            "UPDATE ecommerce_pedidos SET status=%s, entregue_em=CASE WHEN %s='entregue' THEN NOW() ELSE entregue_em END, "
            "previsao_entrega=COALESCE(%s, previsao_entrega), previsao_entrega_em=COALESCE(%s, previsao_entrega_em), "
            "atualizado_em=NOW() WHERE id=%s AND cnpjloja=%s",
            (novo_status, novo_status, previsao_entrega, previsao_entrega_em, pedido_id, cnpjloja),
        )
    conn.commit()
    cur.close()
    _registrar_status_pedido(pedido_id, novo_status)
    if novo_status == "entregue" and ml_order_id:
        _ml_feedback_entregue(ml_order_id, cnpjloja=cnpjloja)
    elif novo_status == "enviado" and is_ml_status:
        _ml_sync_shipment_for_pedido(pedido_id=pedido_id, cnpjloja=cnpjloja)
    _email_status_pedido(pedido_id, novo_status)
    _notificar_pedido_evento(
        pedido_id,
        "pedido",
        f"Pedido {_STATUS_LABEL.get(novo_status, novo_status)}",
        f"O status do pedido #{str(pedido_id)[:8].upper()} foi atualizado para {_STATUS_LABEL.get(novo_status, novo_status)}.",
    )
    # se ficou como pago e é retirada com flag ativada, avança automaticamente
    if novo_status == "pago":
        _auto_pronto_retirada(pedido_id)
    if novo_status == "enviado" and is_ml_status:
        flash(
            "Pedido marcado como enviado no Poupaqui. No Mercado Livre, o transporte muda pela etiqueta/postagem/coleta; sincronizamos o envio pela API.",
            "success",
        )
    else:
        flash(f"Pedido marcado como {_STATUS_LABEL.get(novo_status, novo_status)}.", "success")
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
    if is_ml and not _motoboy_logged():
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
    if ok:
        _registrar_status_pedido(pedido_id, "entregue")
    if ok and is_ml and ok.get("ml_order_id"):
        _ml_feedback_entregue(ok["ml_order_id"], cnpjloja=cnpjloja)
    if ok:
        _email_status_pedido(pedido_id, "entregue")
        _notificar_pedido_evento(
            pedido_id,
            "pedido",
            "Pedido entregue",
            f"A entrega do pedido #{str(pedido_id)[:8].upper()} foi confirmada.",
        )
    flash("Entrega confirmada." if ok else "Código de entrega inválido.", "success" if ok else "error")
    return redirect(url_for("painel_pedido_detalhe", pedido_id=pedido_id))


@app.post("/painel/pedidos/<pedido_id>/confirmar-retirada")
@painel_required
def painel_confirmar_retirada(pedido_id):
    _ensure_delivery_schema()
    cnpjloja = session.get("cnpjloja")
    codigo = (request.form.get("codigo_retirada") or "").strip()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE ecommerce_pedidos
        SET status='entregue', entregue_em=NOW(), atualizado_em=NOW()
        WHERE id=%s AND cnpjloja=%s AND codigo_retirada=%s AND tipo_entrega!='entrega'
        RETURNING id
        """,
        (pedido_id, cnpjloja, codigo),
    )
    ok = cur.fetchone()
    conn.commit()
    cur.close()
    if ok:
        _registrar_status_pedido(pedido_id, "entregue")
        _email_status_pedido(pedido_id, "entregue")
        _notificar_pedido_evento(
            pedido_id,
            "pedido",
            "Pedido retirado",
            f"A retirada do pedido #{str(pedido_id)[:8].upper()} foi confirmada.",
        )
    flash(
        "Retirada confirmada com sucesso!" if ok
        else "Código inválido. Peça ao cliente o código exibido no aplicativo.",
        "success" if ok else "error",
    )
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
    ok, err = _ml_feedback_entregue(row["ml_order_id"], cnpjloja=cnpjloja)
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


# ─── PAINEL: ENCOMENDAS ──────────────────────────────────────────────────────

@app.get("/painel/encomendas")
@painel_required
def painel_encomendas():
    _ensure_encomenda_schema()
    cnpjloja = session.get("cnpjloja")
    status_filtro = request.args.get("status", "abertas")
    conn = db()
    cur = conn.cursor()
    if status_filtro == "todas":
        cur.execute(
            """
            SELECT e.*, c.nome AS consumidor_nome, c.email AS consumidor_email, c.telefone AS consumidor_telefone,
                   (SELECT COUNT(*) FROM ecommerce_encomenda_msgs m WHERE m.encomenda_id=e.id) AS n_msgs
            FROM ecommerce_encomendas e
            JOIN ecommerce_consumidores c ON c.id = e.consumidor_id
            WHERE e.cnpjloja = %s
            ORDER BY e.atualizado_em DESC
            """,
            (cnpjloja,),
        )
    else:
        cur.execute(
            """
            SELECT e.*, c.nome AS consumidor_nome, c.email AS consumidor_email, c.telefone AS consumidor_telefone,
                   (SELECT COUNT(*) FROM ecommerce_encomenda_msgs m WHERE m.encomenda_id=e.id) AS n_msgs
            FROM ecommerce_encomendas e
            JOIN ecommerce_consumidores c ON c.id = e.consumidor_id
            WHERE e.cnpjloja = %s AND e.status NOT IN ('finalizada')
            ORDER BY e.atualizado_em DESC
            """,
            (cnpjloja,),
        )
    encomendas = [dict(r) for r in cur.fetchall()]
    cur.close()
    return render_template(
        "painel_encomendas.html",
        encomendas=encomendas,
        status_label=_STATUS_ENCOMENDA_LABEL,
        status_filtro=status_filtro,
    )


@app.get("/painel/encomendas/<encomenda_id>")
@painel_required
def painel_encomenda_detalhe(encomenda_id):
    _ensure_encomenda_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.*, c.nome AS consumidor_nome, c.email AS consumidor_email, c.telefone AS consumidor_telefone
        FROM ecommerce_encomendas e
        JOIN ecommerce_consumidores c ON c.id = e.consumidor_id
        WHERE e.id = %s AND e.cnpjloja = %s
        LIMIT 1
        """,
        (encomenda_id, cnpjloja),
    )
    enc = cur.fetchone()
    if not enc:
        cur.close()
        flash("Encomenda não encontrada.", "error")
        return redirect(url_for("painel_encomendas"))
    cur.execute(
        "SELECT * FROM ecommerce_encomenda_msgs WHERE encomenda_id=%s ORDER BY enviada_em",
        (encomenda_id,),
    )
    msgs = [dict(m) for m in cur.fetchall()]
    cur.close()
    return render_template(
        "painel_encomenda_detalhe.html",
        enc=dict(enc),
        msgs=msgs,
        status_label=_STATUS_ENCOMENDA_LABEL,
    )


@app.post("/painel/encomendas/<encomenda_id>/mensagem")
@painel_required
def painel_encomenda_mensagem(encomenda_id):
    _ensure_encomenda_schema()
    cnpjloja = session.get("cnpjloja")
    mensagem = (request.form.get("mensagem") or "").strip()
    if not mensagem:
        return redirect(url_for("painel_encomenda_detalhe", encomenda_id=encomenda_id))
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, status FROM ecommerce_encomendas WHERE id=%s AND cnpjloja=%s LIMIT 1",
        (encomenda_id, cnpjloja),
    )
    enc = cur.fetchone()
    if not enc or enc["status"] == "finalizada":
        cur.close()
        return redirect(url_for("painel_encomendas"))
    cur.execute(
        "INSERT INTO ecommerce_encomenda_msgs (encomenda_id, autor, mensagem) VALUES (%s,'loja',%s)",
        (encomenda_id, mensagem),
    )
    if enc["status"] == "aberta":
        cur.execute(
            "UPDATE ecommerce_encomendas SET status='em_andamento', atualizado_em=NOW() WHERE id=%s",
            (encomenda_id,),
        )
    else:
        cur.execute("UPDATE ecommerce_encomendas SET atualizado_em=NOW() WHERE id=%s", (encomenda_id,))
    conn.commit()
    cur.close()
    return redirect(url_for("painel_encomenda_detalhe", encomenda_id=encomenda_id))


@app.post("/painel/encomendas/<encomenda_id>/status")
@painel_required
def painel_encomenda_status(encomenda_id):
    _ensure_encomenda_schema()
    cnpjloja = session.get("cnpjloja")
    novo_status = (request.form.get("status") or "").strip()
    if novo_status not in {"em_andamento", "disponivel", "finalizada"}:
        flash("Status inválido.", "error")
        return redirect(url_for("painel_encomenda_detalhe", encomenda_id=encomenda_id))
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE ecommerce_encomendas SET status=%s, atualizado_em=NOW() WHERE id=%s AND cnpjloja=%s",
        (novo_status, encomenda_id, cnpjloja),
    )
    conn.commit()
    cur.close()
    flash(f"Encomenda marcada como: {_STATUS_ENCOMENDA_LABEL.get(novo_status, novo_status)}.", "success")
    return redirect(url_for("painel_encomenda_detalhe", encomenda_id=encomenda_id))


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

    # Avaliações da loja
    _ensure_avaliacoes_schema()
    cur.execute("""
        SELECT a.estrelas, a.comentario, a.criado_em,
               p.cliente_nome
        FROM ecommerce_avaliacoes_loja a
        LEFT JOIN ecommerce_pedidos p ON p.id::text = a.pedido_id
        WHERE a.cnpjloja = %s
        ORDER BY a.criado_em DESC
        LIMIT 50
    """, (cnpjloja,))
    avaliacoes_rows = [dict(r) for r in cur.fetchall()]
    reputacao_rel = _calcular_reputacao_loja(avaliacoes_rows) or {}

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
    interesses_regiao = []
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

    if session.get("is_admin"):
        try:
            cur3 = conn.cursor()
            cur3.execute(
                """
                SELECT
                  COALESCE(NULLIF(cidade_interesse, ''), 'Não identificada') AS cidade,
                  COALESCE(NULLIF(uf_interesse, ''), '') AS uf,
                  motivo,
                  COALESCE(SUM(contador), 0) AS buscas,
                  COUNT(*) AS visitantes,
                  MAX(ultimo_registro_em) AS ultima_busca
                FROM ecommerce_interesses_regiao
                WHERE ultimo_registro_em >= %s AND ultimo_registro_em < %s
                GROUP BY COALESCE(NULLIF(cidade_interesse, ''), 'Não identificada'),
                         COALESCE(NULLIF(uf_interesse, ''), ''),
                         motivo
                ORDER BY buscas DESC, visitantes DESC, ultima_busca DESC
                LIMIT 12
                """,
                (inicio_dt, fim_dt),
            )
            interesses_regiao = [dict(r) for r in cur3.fetchall()]
            cur3.close()
        except Exception:
            interesses_regiao = []
    relatorio["interesses_regiao"] = interesses_regiao

    return render_template("painel_relatorios.html", relatorio=relatorio, filtros=filtros,
                           vitrine_stats=vitrine_stats,
                           avaliacoes=avaliacoes_rows,
                           reputacao=reputacao_rel)


@app.get("/painel/config")
@painel_required
def painel_config():
    _ensure_receita_schema()
    _ensure_mp_public_key_column()
    _ensure_gateway_alt_columns()
    _ensure_logo_url_column()
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
    _ensure_mp_public_key_column()
    _ensure_gateway_alt_columns()
    cnpjloja = session.get("cnpjloja")
    f = request.form
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        """
        INSERT INTO ecommerce_config_loja
          (cnpjloja, whatsapp_pedidos, whatsapp_receita, email_notificacao, aceita_whatsapp, aceita_pix, aceita_mp,
           aceita_entrega, raio_entrega_km, cobra_frete, valor_frete,
           pedido_minimo_entrega, pix_chave, pix_nome, mp_access_token, mp_public_key,
           gateway_alternativo, asaas_api_key, pagbank_token, pagbank_public_key,
           asaas_taxa_pct, asaas_taxa_fixa, pagbank_taxa_pct, pagbank_taxa_fixa,
           todos_prontos_retirada, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
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
          mp_public_key          = EXCLUDED.mp_public_key,
          gateway_alternativo    = EXCLUDED.gateway_alternativo,
          asaas_api_key          = EXCLUDED.asaas_api_key,
          pagbank_token          = EXCLUDED.pagbank_token,
          pagbank_public_key     = EXCLUDED.pagbank_public_key,
          asaas_taxa_pct         = EXCLUDED.asaas_taxa_pct,
          asaas_taxa_fixa        = EXCLUDED.asaas_taxa_fixa,
          pagbank_taxa_pct       = EXCLUDED.pagbank_taxa_pct,
          pagbank_taxa_fixa      = EXCLUDED.pagbank_taxa_fixa,
          todos_prontos_retirada = EXCLUDED.todos_prontos_retirada,
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
            (f.get("mp_public_key")   or "").strip(),
            (f.get("gateway_alternativo") or "mercadopago").strip(),
            (f.get("asaas_api_key") or "").strip() or None,
            (f.get("pagbank_token") or "").strip() or None,
            (f.get("pagbank_public_key") or "").strip() or None,
            _to_float_or_none(f.get("asaas_taxa_pct")) or 0,
            _to_float_or_none(f.get("asaas_taxa_fixa")) or 0,
            _to_float_or_none(f.get("pagbank_taxa_pct")) or 0,
            _to_float_or_none(f.get("pagbank_taxa_fixa")) or 0,
            "todos_prontos_retirada" in f,
        ),
    )
    conn.commit()
    cur.close()
    flash("Configurações salvas com sucesso.", "success")
    return redirect(url_for("painel_config"))


@app.post("/painel/config/logo")
@painel_required
def painel_config_logo():
    _ensure_logo_url_column()
    cnpjloja = session.get("cnpjloja")
    arquivo = request.files.get("logo")
    if not arquivo or not arquivo.filename:
        flash("Selecione uma imagem para o logo.", "error")
        return redirect(url_for("painel_config"))
    if not _CLOUDINARY_OK:
        flash("Upload de imagem não está configurado no momento.", "error")
        return redirect(url_for("painel_config"))
    try:
        result = cloudinary.uploader.upload(
            arquivo,
            resource_type="image",
            folder="logos_loja",
            public_id=cnpjloja,
            overwrite=True,
        )
        logo_url = result.get("secure_url")
    except Exception as exc:
        app.logger.error("Upload de logo da loja: %s", exc)
        logo_url = None
    if not logo_url:
        flash("Não foi possível enviar a imagem. Tente novamente.", "error")
        return redirect(url_for("painel_config"))
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ecommerce_config_loja (cnpjloja, logo_url)
        VALUES (%s, %s)
        ON CONFLICT (cnpjloja) DO UPDATE SET logo_url=EXCLUDED.logo_url, updated_at=NOW()
        """,
        (cnpjloja, logo_url),
    )
    conn.commit()
    cur.close()
    flash("Logo da loja atualizado.", "success")
    return redirect(url_for("painel_config"))


@app.get("/painel/admin/config")
@admin_required
def admin_config():
    """Conta Mercado Pago central do admin — usada para cobrar todas as assinaturas."""
    cfg = _admin_mp_config()
    return render_template("admin_config.html", config=cfg)


@app.post("/painel/admin/config")
@admin_required
def admin_config_salvar():
    _ensure_config_admin_schema()
    f = request.form
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ecommerce_config_admin (id, mp_access_token, mp_public_key, updated_at)
        VALUES (1, %s, %s, NOW())
        ON CONFLICT (id) DO UPDATE SET
          mp_access_token = EXCLUDED.mp_access_token,
          mp_public_key   = EXCLUDED.mp_public_key,
          updated_at      = NOW()
        """,
        (
            (f.get("mp_access_token") or "").strip(),
            (f.get("mp_public_key") or "").strip(),
        ),
    )
    conn.commit()
    cur.close()
    flash("Configuração Mercado Pago do admin salva com sucesso.", "success")
    return redirect(url_for("admin_config"))


@app.get("/painel/admin/config/mp-test")
@admin_required
def admin_mp_test():
    """Testa o token Mercado Pago do admin — conta + criação de PIX real."""
    cfg = _admin_mp_config()
    token = cfg.get("mp_access_token") or ""
    if not token:
        return jsonify({"ok": False, "erro": "Token não configurado."})

    resultado = {"token_prefixo": token[:20] + "..."}

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

    try:
        pix_payload = {
            "transaction_amount": 1.00,
            "description": "Teste PIX Poupaqui (admin)",
            "payment_method_id": "pix",
            "external_reference": "mp-test-admin",
            "payer": {"email": f"teste.{int(time.time())}@poupaqui.com.br",
                      "first_name": "Teste", "last_name": "Poupaqui"},
        }
        pix_data = _mp_request(token, "/v1/payments", pix_payload,
                               idempotency_key=f"mp-test-admin-pix-{int(time.time())}")
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


def _consumidores_notificacao_loja(cnpjloja: str, publico: str = "todos"):
    _ensure_notificacoes_schema()
    _ensure_assinatura_schema()
    _ensure_favoritos_schema()
    publico = publico if publico in {"todos", "compradores", "assinantes", "favoritos"} else "todos"
    conn = db()
    cur = conn.cursor()
    parts = []
    args = []
    if publico in {"todos", "compradores"}:
        parts.append("SELECT DISTINCT consumidor_id FROM ecommerce_pedidos WHERE cnpjloja=%s AND consumidor_id IS NOT NULL")
        args.append(cnpjloja)
    if publico in {"todos", "assinantes"}:
        parts.append(
            "SELECT DISTINCT consumidor_id::uuid AS consumidor_id FROM ecommerce_assinantes "
            "WHERE cnpjloja=%s AND consumidor_id ~* '^[0-9a-f-]{36}$'"
        )
        args.append(cnpjloja)
    if publico in {"todos", "favoritos"}:
        parts.append("SELECT DISTINCT consumidor_id FROM ecommerce_favoritos WHERE cnpjloja=%s AND consumidor_id IS NOT NULL")
        args.append(cnpjloja)
    if not parts:
        cur.close()
        return []
    cur.execute(" UNION ".join(parts), tuple(args))
    ids = [r["consumidor_id"] for r in cur.fetchall() if r.get("consumidor_id")]
    cur.close()
    return ids


@app.get("/painel/notificacoes")
@painel_required
def painel_notificacoes():
    cnpjloja = session.get("cnpjloja")
    publico_counts = {
        "todos": len(_consumidores_notificacao_loja(cnpjloja, "todos")),
        "compradores": len(_consumidores_notificacao_loja(cnpjloja, "compradores")),
        "assinantes": len(_consumidores_notificacao_loja(cnpjloja, "assinantes")),
        "favoritos": len(_consumidores_notificacao_loja(cnpjloja, "favoritos")),
    }
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT n.titulo, n.mensagem, n.imagem_url, n.url, n.criada_em, COUNT(*) AS total
        FROM ecommerce_notificacoes_consumidor n
        WHERE n.tipo='loja' AND n.url LIKE %s
        GROUP BY n.titulo, n.mensagem, n.imagem_url, n.url, n.criada_em
        ORDER BY n.criada_em DESC
        LIMIT 20
        """,
        (f"%/loja/{cnpjloja}%",),
    )
    historico = cur.fetchall()
    cur.close()
    return render_template("painel_notificacoes.html", publico_counts=publico_counts, historico=historico)


@app.post("/painel/notificacoes")
@painel_required
def painel_notificacoes_enviar():
    cnpjloja = session.get("cnpjloja")
    titulo = (request.form.get("titulo") or "").strip()
    mensagem = (request.form.get("mensagem") or "").strip()
    publico = (request.form.get("publico") or "todos").strip()
    url = (request.form.get("url") or "").strip() or url_for("catalogo_loja", cnpjloja=cnpjloja)
    imagem_url = None
    imagem = request.files.get("imagem")
    if imagem and imagem.filename:
        raw = imagem.read()
        if len(raw) > 3 * 1024 * 1024:
            flash("A imagem deve ter no máximo 3 MB.", "error")
            return redirect(url_for("painel_notificacoes"))
        ext = (imagem.filename.rsplit(".", 1)[-1].lower()) if "." in imagem.filename else "jpg"
        content_type = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
            "gif": "image/gif",
        }.get(ext)
        if not content_type:
            flash("Use uma imagem JPG, PNG, WEBP ou GIF.", "error")
            return redirect(url_for("painel_notificacoes"))
        storage_path = f"notificacoes/{cnpjloja}/{int(time.time())}-{secrets.token_hex(6)}.{ext}"
        imagem_url = upload_to_supabase_storage(raw, storage_path, content_type)
        if not imagem_url:
            flash("Não foi possível enviar a imagem. Tente novamente.", "error")
            return redirect(url_for("painel_notificacoes"))
    if not titulo or not mensagem:
        flash("Informe título e mensagem.", "error")
        return redirect(url_for("painel_notificacoes"))
    consumidores = _consumidores_notificacao_loja(cnpjloja, publico)
    if not consumidores:
        flash("Nenhum consumidor encontrado para esse público.", "error")
        return redirect(url_for("painel_notificacoes"))
    rows = [(cid, "loja", titulo[:160], mensagem[:600], imagem_url, url) for cid in consumidores]
    conn = db()
    cur = conn.cursor()
    execute_values(
        cur,
        """
        INSERT INTO ecommerce_notificacoes_consumidor
          (consumidor_id, tipo, titulo, mensagem, imagem_url, url)
        VALUES %s
        """,
        rows,
    )
    conn.commit()
    cur.close()
    flash(f"Notificação enviada para {len(rows)} consumidor(es).", "success")
    return redirect(url_for("painel_notificacoes"))


# ─── PAINEL: UPLOAD IMAGEM DO PRODUTO ────────────────────────────────────────

@app.post("/painel/produto/<ean>/imagem")
@painel_required
def painel_produto_imagem(ean):
    is_ajax = (request.headers.get("X-Requested-With") == "XMLHttpRequest"
               or request.accept_mimetypes.best == "application/json")
    cnpjloja = session.get("cnpjloja")

    if "imagem" not in request.files or not request.files["imagem"].filename:
        if is_ajax:
            return jsonify({"ok": False, "erro": "Selecione uma imagem."}), 400
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
        if is_ajax:
            return jsonify({"ok": False, "erro": "Erro no upload. Verifique o arquivo e tente novamente."}), 500
        flash("Erro no upload da imagem. Verifique o arquivo e tente novamente.", "error")
        return redirect(url_for("precificador"))
    # x-upsert sobrescreve o mesmo path/URL no Storage — sem isso, um reenvio
    # (ex: corrigir a foto) manteria a URL idêntica e ficaria preso em cache
    # de CDN/navegador de quem já viu o produto antes.
    img_url = f"{img_url}?v={int(time.time())}"

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
    _batch_cache_clear()

    if is_ajax:
        return jsonify({"ok": True, "imagem_url": img_url})
    flash("Imagem do produto atualizada.", "success")
    return redirect(url_for("precificador"))


@app.get("/loja/<cnpjloja>")
def catalogo_loja(cnpjloja):
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
        cache_key = ("catalogo_loja", cnpjloja, q)
        produtos = _catalogo_loja_cache_get(cache_key)
        if produtos is None:
            # A vitrine precisa aplicar a mesma regra do precificador: buscar o
            # estoque completo, normalizar/preencher imagens seguras e só depois
            # remover o que ainda ficou sem imagem publicável. No caminho público
            # não disparamos o worker de imagens para não segurar a resposta.
            produtos = get_dns_products(
                cnpjloja,
                q or None,
                skip_image_filter=True,
                schedule_fill=False,
                persist_image_updates=False,
                ensure_anvisa_schema=False,
                ensure_precificador_schema=False,
                batch_sales_prices=True,
            )
            produtos, _bloqueados_sem_imagem = _split_catalog_image_status(produtos)
            _catalogo_loja_cache_set(cache_key, produtos)

    if loja.get("catalogo_publico") is False:
        produtos, _bloqueados_sem_imagem = _split_catalog_image_status(produtos)

    _attach_product_promos(produtos)

    # Plano de assinatura da loja (se ativo)
    plano_assinatura = None
    ja_assina = False
    assinatura_pendente = False
    try:
        _ensure_assinatura_schema()
        cur2 = conn.cursor()
        cur2.execute(
            """SELECT id, nome, descricao, preco_mensal, beneficios,
                      COALESCE(frete_gratis_primeira_entrega, FALSE) AS frete_gratis_primeira_entrega
               FROM ecommerce_planos_assinatura WHERE cnpjloja=%s AND ativo=TRUE LIMIT 1""",
            (cnpjloja,),
        )
        plano_assinatura = cur2.fetchone()
        if plano_assinatura:
            consumidor_id = str(session.get("consumidor_id") or "")
            if consumidor_id:
                cur2.execute(
                    """SELECT status, pagamento_status, data_fim FROM ecommerce_assinantes
                       WHERE consumidor_id=%s AND cnpjloja=%s LIMIT 1""",
                    (consumidor_id, cnpjloja),
                )
                row_assin = cur2.fetchone()
                if row_assin:
                    ja_assina = _assinatura_vigente_row(row_assin)
                    assinatura_pendente = row_assin["status"] == "aguardando_pagamento"
        cur2.close()
    except Exception:
        pass

    return render_template("catalogo_loja.html", loja=loja, produtos=produtos, q=q,
                           plano_assinatura=plano_assinatura, ja_assina=ja_assina,
                           assinatura_pendente=assinatura_pendente)


# ─── PAINEL DA LOJA (login) ───────────────────────────────────────────────────

@app.get("/painel/login")
def painel_login():
    if session.get("painel_ok"):
        if _motoboy_logged() or _lojista_logged():
            return redirect(url_for("painel_pedidos"))
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
    if u and (u.get("senha") or "") == senha:
        session.clear()
        session["painel_ok"]  = True
        session["user_id"]    = str(u["id"])
        session["cnpjloja"]   = u["cnpjloja"]
        session["razao"]      = u["razao"]
        session["uf"]         = u["uf"]
        session["endereco"]   = u["endereco"]
        session["is_admin"]   = bool(u.get("is_admin"))
        session["painel_role"] = "loja"

        if session["is_admin"]:
            return redirect(url_for("admin_lojas"))
        return redirect(url_for("precificador"))

    try:
        _ensure_motoboy_schema()
        cur = db().cursor()
        cur.execute(
            """
            SELECT m.id, m.cnpjloja, m.nome, m.senha_hash, u.razao, u.uf, u.endereco
            FROM ecommerce_motoboys m
            JOIN users u ON u.cnpjloja = m.cnpjloja
            WHERE m.usuario=%s AND COALESCE(m.ativo, TRUE)=TRUE
            LIMIT 1
            """,
            (usuario,),
        )
        m = cur.fetchone()
        cur.close()
    except Exception:
        m = None

    if m and check_password_hash(m.get("senha_hash") or "", senha):
        session.clear()
        session["painel_ok"]   = True
        session["user_id"]     = str(m["id"])
        session["cnpjloja"]    = m["cnpjloja"]
        session["razao"]       = m["razao"]
        session["uf"]          = m["uf"]
        session["endereco"]    = m["endereco"]
        session["is_admin"]    = False
        session["painel_role"] = "motoboy"
        session["motoboy_nome"] = m["nome"]
        return redirect(url_for("painel_pedidos"))

    # Tenta login como lojista
    try:
        _ensure_lojista_schema()
        cur = db().cursor()
        cur.execute(
            """
            SELECT l.id, l.cnpjloja, l.nome, l.senha_hash, u.razao, u.uf, u.endereco
            FROM ecommerce_lojistas l
            JOIN users u ON u.cnpjloja = l.cnpjloja
            WHERE l.usuario=%s AND COALESCE(l.ativo, TRUE)=TRUE
            LIMIT 1
            """,
            (usuario,),
        )
        lj = cur.fetchone()
        cur.close()
    except Exception:
        lj = None

    if lj and check_password_hash(lj.get("senha_hash") or "", senha):
        session.clear()
        session["painel_ok"]    = True
        session["user_id"]      = str(lj["id"])
        session["cnpjloja"]     = lj["cnpjloja"]
        session["razao"]        = lj["razao"]
        session["uf"]           = lj["uf"]
        session["endereco"]     = lj["endereco"]
        session["is_admin"]     = False
        session["painel_role"]  = "lojista"
        session["lojista_nome"] = lj["nome"]
        return redirect(url_for("painel_pedidos"))

    flash("Usuário ou senha inválidos.", "error")
    return redirect(url_for("painel_login"))


@app.get("/painel/logout")
def painel_logout():
    session.clear()
    return redirect(url_for("painel_login"))


@app.get("/painel")
@painel_required
def painel_home():
    if _motoboy_logged() or _lojista_logged():
        return redirect(url_for("painel_pedidos"))
    return redirect(url_for("precificador"))


@app.get("/painel/motoboys")
@painel_required
def painel_motoboys():
    _ensure_motoboy_schema()
    cnpjloja = session.get("cnpjloja")
    cur = db().cursor()
    cur.execute(
        """
        SELECT id, nome, usuario, ativo, criado_em, atualizado_em
        FROM ecommerce_motoboys
        WHERE cnpjloja=%s
        ORDER BY ativo DESC, nome
        """,
        (cnpjloja,),
    )
    motoboys = cur.fetchall()
    cur.close()
    return render_template("painel_motoboys.html", motoboys=motoboys)


@app.post("/painel/motoboys")
@painel_required
def painel_motoboys_criar():
    _ensure_motoboy_schema()
    cnpjloja = session.get("cnpjloja")
    nome = (request.form.get("nome") or "").strip()
    usuario = (request.form.get("usuario") or "").strip().lower()
    senha = (request.form.get("senha") or "").strip()
    if len(nome) < 3:
        flash("Informe o nome do motoboy.", "error")
        return redirect(url_for("painel_motoboys"))
    if len(usuario) < 4 or not re.match(r"^[a-z0-9._-]+$", usuario):
        flash("O login deve ter pelo menos 4 caracteres e usar apenas letras, números, ponto, traço ou underline.", "error")
        return redirect(url_for("painel_motoboys"))
    if len(senha) < 6:
        flash("A senha do motoboy precisa ter pelo menos 6 caracteres.", "error")
        return redirect(url_for("painel_motoboys"))
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO ecommerce_motoboys (cnpjloja, nome, usuario, senha_hash)
            VALUES (%s, %s, %s, %s)
            """,
            (cnpjloja, nome, usuario, generate_password_hash(senha)),
        )
        conn.commit()
        flash("Login do motoboy criado com acesso restrito aos pedidos.", "success")
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash("Este login já está em uso. Escolha outro usuário.", "error")
    finally:
        cur.close()
    return redirect(url_for("painel_motoboys"))


@app.post("/painel/motoboys/<motoboy_id>/status")
@painel_required
def painel_motoboys_status(motoboy_id):
    _ensure_motoboy_schema()
    ativo = request.form.get("ativo") == "1"
    cnpjloja = session.get("cnpjloja")
    cur = db().cursor()
    cur.execute(
        "UPDATE ecommerce_motoboys SET ativo=%s, atualizado_em=NOW() WHERE id=%s AND cnpjloja=%s",
        (ativo, motoboy_id, cnpjloja),
    )
    db().commit()
    cur.close()
    flash("Acesso do motoboy atualizado.", "success")
    return redirect(url_for("painel_motoboys"))


@app.post("/painel/motoboys/<motoboy_id>/senha")
@painel_required
def painel_motoboys_senha(motoboy_id):
    _ensure_motoboy_schema()
    senha = (request.form.get("senha") or "").strip()
    if len(senha) < 6:
        flash("A nova senha precisa ter pelo menos 6 caracteres.", "error")
        return redirect(url_for("painel_motoboys"))
    cnpjloja = session.get("cnpjloja")
    cur = db().cursor()
    cur.execute(
        """
        UPDATE ecommerce_motoboys
        SET senha_hash=%s, atualizado_em=NOW()
        WHERE id=%s AND cnpjloja=%s
        """,
        (generate_password_hash(senha), motoboy_id, cnpjloja),
    )
    db().commit()
    cur.close()
    flash("Senha do motoboy atualizada.", "success")
    return redirect(url_for("painel_motoboys"))


# ─── LOJISTAS (acesso restrito só a pedidos) ──────────────────────────────────

@app.get("/painel/lojistas")
@painel_required
def painel_lojistas():
    if _motoboy_logged() or _lojista_logged():
        return redirect(url_for("painel_pedidos"))
    _ensure_lojista_schema()
    cnpjloja = session.get("cnpjloja")
    cur = db().cursor()
    cur.execute(
        """
        SELECT id, nome, usuario, ativo, criado_em, atualizado_em
        FROM ecommerce_lojistas
        WHERE cnpjloja=%s
        ORDER BY ativo DESC, nome
        """,
        (cnpjloja,),
    )
    lojistas = cur.fetchall()
    cur.close()
    return render_template("painel_lojistas.html", lojistas=lojistas)


@app.post("/painel/lojistas")
@painel_required
def painel_lojistas_criar():
    if _motoboy_logged() or _lojista_logged():
        return redirect(url_for("painel_pedidos"))
    _ensure_lojista_schema()
    cnpjloja = session.get("cnpjloja")
    nome    = (request.form.get("nome")    or "").strip()
    usuario = (request.form.get("usuario") or "").strip().lower()
    senha   = (request.form.get("senha")   or "").strip()
    if len(nome) < 3:
        flash("Informe o nome do atendente.", "error")
        return redirect(url_for("painel_lojistas"))
    if len(usuario) < 4 or not re.match(r"^[a-z0-9._-]+$", usuario):
        flash("O login deve ter pelo menos 4 caracteres e usar apenas letras, números, ponto, traço ou underline.", "error")
        return redirect(url_for("painel_lojistas"))
    if len(senha) < 6:
        flash("A senha precisa ter pelo menos 6 caracteres.", "error")
        return redirect(url_for("painel_lojistas"))
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO ecommerce_lojistas (cnpjloja, nome, usuario, senha_hash)
            VALUES (%s, %s, %s, %s)
            """,
            (cnpjloja, nome, usuario, generate_password_hash(senha)),
        )
        conn.commit()
        flash("Login criado. Este acesso só tem acesso à tela de pedidos.", "success")
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash("Este login já está em uso. Escolha outro usuário.", "error")
    finally:
        cur.close()
    return redirect(url_for("painel_lojistas"))


@app.post("/painel/lojistas/<lojista_id>/status")
@painel_required
def painel_lojistas_status(lojista_id):
    if _motoboy_logged() or _lojista_logged():
        return redirect(url_for("painel_pedidos"))
    _ensure_lojista_schema()
    ativo = request.form.get("ativo") == "1"
    cnpjloja = session.get("cnpjloja")
    cur = db().cursor()
    cur.execute(
        "UPDATE ecommerce_lojistas SET ativo=%s, atualizado_em=NOW() WHERE id=%s AND cnpjloja=%s",
        (ativo, lojista_id, cnpjloja),
    )
    db().commit()
    cur.close()
    flash("Acesso atualizado.", "success")
    return redirect(url_for("painel_lojistas"))


@app.post("/painel/lojistas/<lojista_id>/senha")
@painel_required
def painel_lojistas_senha(lojista_id):
    if _motoboy_logged() or _lojista_logged():
        return redirect(url_for("painel_pedidos"))
    _ensure_lojista_schema()
    senha = (request.form.get("senha") or "").strip()
    if len(senha) < 6:
        flash("A nova senha precisa ter pelo menos 6 caracteres.", "error")
        return redirect(url_for("painel_lojistas"))
    cnpjloja = session.get("cnpjloja")
    cur = db().cursor()
    cur.execute(
        """
        UPDATE ecommerce_lojistas
        SET senha_hash=%s, atualizado_em=NOW()
        WHERE id=%s AND cnpjloja=%s
        """,
        (generate_password_hash(senha), lojista_id, cnpjloja),
    )
    db().commit()
    cur.close()
    flash("Senha atualizada.", "success")
    return redirect(url_for("painel_lojistas"))


# ─── PRECIFICADOR ─────────────────────────────────────────────────────────────

@app.get("/painel/precificador")
@painel_required
def precificador():
    _ensure_competitor_schema()
    cnpjloja = session.get("cnpjloja")
    q = (request.args.get("q") or "").strip()
    produtos = get_dns_products(
        cnpjloja,
        q or None,
        skip_image_filter=True,
        dedupe_display=False,
    )
    _publicados, bloqueados_sem_imagem = _split_catalog_image_status(produtos)
    for _p in bloqueados_sem_imagem:
        _p["sem_imagem"] = True

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
        cnpjloja=cnpjloja,
        is_admin=session.get("is_admin"),
        competitor_map=competitor_map,
        competitor_last_update=competitor_last_update,
        concorrentes=_CONCORRENTES,
        catalogo_alpha_exclusivo=_catalogo_alpha_exclusivo(),
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
    produtos = get_dns_products(cnpjloja, None, skip_image_filter=True)
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
    produtos = get_dns_products(cnpjloja, None, skip_image_filter=True)
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
    _batch_cache_clear()
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
    _batch_cache_clear()
    return jsonify({"ok": True})


@app.post("/api/alpha/sync")
@painel_required
def api_alpha_sync():
    global _alpha_catalog_last_attempt
    _alpha_catalog_last_attempt = 0.0
    produtos = _alpha_sync_products_safe()
    statuses = _alpha_sync_statuses_safe()
    return jsonify({"ok": bool(produtos.get("ok") or statuses.get("ok")), "produtos": produtos, "status_pedidos": statuses})


_CRON_SECRET = "poupaqui-alpha-cron-7x9k2m"

@app.post("/api/cron/wa-diagnostico")
def api_cron_wa_diagnostico():
    try:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {_CRON_SECRET}":
            return jsonify({"ok": False, "erro": "unauthorized"}), 401
        key_ok = bool(WASENDER_API_KEY)
        key_preview = (WASENDER_API_KEY[:8] + "...") if WASENDER_API_KEY else "(vazia)"
        numero = ""
        try:
            conn2 = db(); cur2 = conn2.cursor()
            cur2.execute("SELECT whatsapp_pedidos AS wpp FROM ecommerce_config_loja LIMIT 1")
            row2 = cur2.fetchone(); cur2.close()
            numero = (row2 or {}).get("wpp") or ""
        except Exception as e2:
            return jsonify({"ok": False, "key_presente": key_ok, "key_preview": key_preview, "erro_db": str(e2)})
        d = re.sub(r'\D', '', numero)
        to = ('+55' + d) if d and not d.startswith('55') else ('+' + d if d else "")
        if not key_ok:
            return jsonify({"ok": False, "erro": "WASENDER_API_KEY vazia no Vercel", "key_preview": key_preview, "numero": numero})
        if not to or len(d) < 8:
            return jsonify({"ok": False, "erro": "numero invalido", "numero": numero, "to": to, "key_preview": key_preview})
        ok = _wa_send(to, "Diagnostico Poupaqui WA OK!")
        return jsonify({"ok": ok, "key_preview": key_preview, "to": to, "msg": "enviado" if ok else "falhou - ver logs Vercel"})
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)})


@app.post("/api/cron/alpha-export")
def api_cron_alpha_export():
    """Endpoint chamado pelo cron do Hostgator a cada minuto. Exporta 1 pedido por chamada."""
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {_CRON_SECRET}":
        return jsonify({"ok": False, "erro": "unauthorized"}), 401
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id FROM ecommerce_pedidos
        WHERE status IN ('pago','pronto_retirada','em_separacao','separado',
                         'em_transito','saiu_entrega','saiu_para_entrega','entregue','concluido')
          AND (alpha_status IS NULL OR alpha_status = 'erro')
          AND pagamento_confirmado_em IS NOT NULL
        ORDER BY pagamento_confirmado_em
        LIMIT 1
        """,
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return jsonify({"ok": True, "msg": "nenhum_pendente"})
    pid = str(row["id"])
    res = _alpha_export_paid_order_safe(pid)
    return jsonify({"ok": True, "id": pid[:8], "resultado": res})


@app.post("/api/cron/wa-notify")
def api_cron_wa_notify():
    """Envia WA da loja para 1 pedido pago que ainda não foi notificado."""
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {_CRON_SECRET}":
        return jsonify({"ok": False, "erro": "unauthorized"}), 401
    if not WASENDER_API_KEY:
        return jsonify({"ok": False, "erro": "WASENDER_API_KEY_nao_configurada"})
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, total FROM ecommerce_pedidos
        WHERE status IN ('pago','pronto_retirada','em_separacao','separado',
                         'em_transito','saiu_entrega','saiu_para_entrega','entregue','concluido')
          AND wa_loja_notificado_em IS NULL
          AND pagamento_confirmado_em IS NOT NULL
          AND pagamento_confirmado_em > NOW() - INTERVAL '48 hours'
        ORDER BY pagamento_confirmado_em
        LIMIT 1
        """,
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return jsonify({"ok": True, "msg": "nenhum_pendente"})
    pid = str(row["id"])
    total_fmt = f"R${float(row['total'] or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    msg = (
        f"✅ Pedido pago!\n"
        f"Pedido #{pid[:8].upper()} - {total_fmt}\n"
        f"Clique para preparar:\n"
        f"{_wa_base_url()}/painel/pedidos/{pid}"
    )
    ok = _wa_notif_pedido_loja(pid, msg)
    if ok:
        try:
            uc = db(); ucu = uc.cursor()
            ucu.execute("UPDATE ecommerce_pedidos SET wa_loja_notificado_em=NOW() WHERE id=%s", (pid,))
            uc.commit(); ucu.close()
        except Exception:
            pass
    return jsonify({"ok": ok, "id": pid[:8], "enviado": ok})


@app.post("/api/alpha/pedido/<pedido_id>/exportar")
@painel_required
def api_alpha_exportar_pedido(pedido_id):
    result = _alpha_export_paid_order_safe(str(pedido_id))
    return jsonify(result)


@app.post("/api/wa/teste")
@painel_required
def api_wa_teste():
    """Testa envio de WA com diagnóstico detalhado."""
    data = request.get_json(force=True) or {}
    numero = (data.get("numero") or "").strip()
    key_present = bool(WASENDER_API_KEY)
    key_preview = (WASENDER_API_KEY[:8] + "…") if WASENDER_API_KEY else ""
    if not numero:
        cnpjloja = session.get("cnpjloja")
        conn = db(); cur = conn.cursor()
        cur.execute("SELECT COALESCE(whatsapp_pedidos, telefone) AS wpp FROM ecommerce_config_loja WHERE cnpjloja=%s LIMIT 1", (cnpjloja,))
        row = cur.fetchone(); cur.close()
        numero = (row or {}).get("wpp") or ""
    d = re.sub(r'\D', '', numero)
    to = ('+55' + d) if d and not d.startswith('55') else ('+' + d if d else "")
    if not key_present:
        return jsonify({"ok": False, "erro": "WASENDER_API_KEY nao configurada no ambiente", "key_preview": key_preview, "to": to})
    if not to or len(d) < 8:
        return jsonify({"ok": False, "erro": f"Numero invalido: {numero!r}", "to": to})
    try:
        req = urllib.request.Request(
            'https://wasenderapi.com/api/send-message',
            data=json.dumps({'to': to, 'text': '🔧 Teste de notificação Poupaqui — tudo certo!'}).encode(),
            headers={'Authorization': f'Bearer {WASENDER_API_KEY}', 'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read(500).decode(errors='replace')
            return jsonify({"ok": True, "to": to, "status": resp.status, "body": body, "key_preview": key_preview})
    except urllib.error.HTTPError as e:
        body = e.read(500).decode(errors='replace')
        return jsonify({"ok": False, "erro": f"HTTP {e.code}", "body": body, "to": to, "key_preview": key_preview})
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc), "to": to, "key_preview": key_preview})


# ─── ADMIN ────────────────────────────────────────────────────────────────────

_ADMIN_CATEGORIAS_PUBLICACAO = {
    "todos": "Todos",
    "medicamento": "Medicamentos",
    "nao_medicamento": "Não medicamentos",
    "suplemento": "Suplementos",
    "perfumaria": "Perfumaria e Higiene",
    "dermocosmetico": "Dermocosméticos",
    "nutricao": "Nutrição",
    "varejo": "Varejo/Conveniência",
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
               COALESCE(epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem
        FROM estoque e
        LEFT JOIN medicamentos m ON m.barra_norm = COALESCE(e.barras_norm, e.barras)
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        LEFT JOIN produto_canon pc ON pc.ean = COALESCE(e.barras_norm, e.barras) AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
        LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = e.cnpj AND epi.ean = e.barras
        LEFT JOIN medicamentos5 m5 ON m5.barra = e.barras
        WHERE e.cnpj=%s AND e.estoque > %s
          AND COALESCE(e.barras, e.barras_norm, '') <> ''

        UNION ALL

        SELECT ae.ean,
               COALESCE(m.descricao, ae.descricao_produto) AS nome,
               CAST(ae.quantidade_estoque AS INTEGER) AS qty,
               COALESCE(epi.imagem_url, mi.cloudinary_url, pc.imagem_cosmos, NULLIF(TRIM(m.imagem), ''), NULLIF(TRIM(m5.imagem), '')) AS imagem
        FROM automatiza_estoque ae
        LEFT JOIN medicamentos m ON m.barra_norm = ae.ean
        LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
        LEFT JOIN produto_canon pc ON pc.ean = ae.ean AND pc.fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
        LEFT JOIN ecommerce_produto_imagens epi ON epi.cnpjloja = ae.cnpj_loja AND epi.ean = ae.ean
        LEFT JOIN medicamentos5 m5 ON m5.barra = ae.ean
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
    flash("Configuração do catálogo atualizada.", "success")
    return redirect(url_for("admin_lojas"))


def _notificar_avisos_chegada(cnpjloja):
    """Quando uma loja e ativada no catalogo publico, avisa (via notificacoes)
    quem tinha pedido para ser avisado quando chegasse uma parceria na
    cidade dela. Casamento por cidade/uf normalizados (mesma fonte de
    geocode do consumidor: Google Maps com fallback Nominatim)."""
    try:
        _ensure_aviso_chegada_schema()
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT lat, lng FROM ecommerce_lojas_geo WHERE cnpjloja=%s LIMIT 1", (cnpjloja,))
        geo = cur.fetchone()
        if not geo or not geo.get("lat") or not geo.get("lng"):
            cur.close()
            return
        cidade, uf = _reverse_geocode_cidade_uf(geo["lat"], geo["lng"])
        if not cidade:
            cur.close()
            return
        cidade_norm = _norm_text(cidade)
        uf_norm = (uf or "").strip().upper()
        cur.execute("SELECT id, consumidor_id, cidade FROM ecommerce_avisos_chegada WHERE notificado_em IS NULL AND COALESCE(uf,'')=%s", (uf_norm,))
        pendentes = [r for r in cur.fetchall() if _norm_text(r["cidade"]) == cidade_norm]
        if not pendentes:
            cur.close()
            return
        cur.execute("SELECT razao, endereco FROM users WHERE cnpjloja=%s LIMIT 1", (cnpjloja,))
        loja_row = cur.fetchone()
        razao = _public_store_name(dict(loja_row)) if loja_row else "uma nova farmácia parceira"
        for row in pendentes:
            _notificar_consumidor(
                row["consumidor_id"], "aviso_chegada",
                f"Chegamos em {cidade}!",
                f"{razao} agora faz parte do Poupaqui. Já dá pra fazer seu pedido.",
                url=url_for("index"), conn=conn,
            )
            cur.execute("UPDATE ecommerce_avisos_chegada SET notificado_em=NOW() WHERE id=%s", (row["id"],))
        conn.commit()
        cur.close()
    except Exception as exc:
        app.logger.warning("_notificar_avisos_chegada error: %s", exc)


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
            flash("Catálogo habilitado. Clique em Sincronizar para publicar os produtos.", "success")
            try:
                _notificar_avisos_chegada(cnpjloja)
            except Exception:
                pass
        else:
            flash("Catálogo ocultado dos consumidores.", "success")
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
        f"Sincronização concluída: {stats['publicados']} publicados, {stats['sem_imagem']} sem imagem (serão preenchidos automaticamente).",
        "success",
    )
    return redirect(url_for("admin_lojas"))


@app.post("/painel/admin/geocodificar")
@admin_required
def admin_geocodificar():
    _ensure_lojas_geo_address_hash()
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

    endereco_geo = u["endereco2"] or u["endereco"]
    lat, lng = _geo_override(u["endereco"], u["endereco2"], u["uf"])
    if not lat:
        lat, lng = nominatim_geocode(endereco_geo, u["uf"])
    if lat:
        endereco_hash = _endereco_geo_hash(endereco_geo, u["uf"])
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ecommerce_lojas_geo (cnpjloja, lat, lng, endereco_hash)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (cnpjloja) DO UPDATE
              SET lat = EXCLUDED.lat, lng = EXCLUDED.lng, endereco_hash = EXCLUDED.endereco_hash, geocoded_at = NOW()
            """,
            (cnpjloja, lat, lng, endereco_hash),
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
    _ensure_lojas_geo_address_hash()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.cnpjloja, u.endereco, u.endereco2, u.uf, g.endereco_hash, g.lat
        FROM users u
        LEFT JOIN ecommerce_lojas_geo g ON g.cnpjloja = u.cnpjloja
        WHERE u.is_admin = FALSE
        LIMIT 300
        """
    )
    pendentes = [
        r for r in cur.fetchall()
        if not r.get("lat")
        or r.get("endereco_hash") != _endereco_geo_hash(r.get("endereco2") or r.get("endereco"), r.get("uf"))
    ][:50]
    cur.close()

    ok = fail = 0
    for u in pendentes:
        endereco_geo = u["endereco2"] or u["endereco"]
        lat, lng = _geo_override(u["endereco"], u["endereco2"], u["uf"])
        if not lat:
            lat, lng = nominatim_geocode(endereco_geo, u["uf"])
        if lat:
            endereco_hash = _endereco_geo_hash(endereco_geo, u["uf"])
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO ecommerce_lojas_geo (cnpjloja, lat, lng, endereco_hash)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (cnpjloja) DO UPDATE
                  SET lat = EXCLUDED.lat, lng = EXCLUDED.lng, endereco_hash = EXCLUDED.endereco_hash, geocoded_at = NOW()
                """,
                (u["cnpjloja"], lat, lng, endereco_hash),
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

@app.get("/api/cloudinary-signature")
@admin_required
def api_cloudinary_signature():
    """Gera assinatura para upload direto do browser para o Cloudinary."""
    if not _CLOUDINARY_OK:
        return jsonify({"error": "Cloudinary não configurado"}), 400
    folder = request.args.get("folder", "lojas_vitrine")
    ts = int(time.time())
    params_str = f"folder={folder}&timestamp={ts}"
    return jsonify({
        "signature": _cloudinary_sign(params_str),
        "timestamp": ts,
        "api_key": os.getenv("CLOUDINARY_API_KEY", ""),
        "cloud_name": os.getenv("CLOUDINARY_CLOUD_NAME", ""),
        "folder": folder,
    })


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
        # imagem_url já vem pronta do upload direto browser→Cloudinary
        imagem_url = (request.form.get("imagem_url") or "").strip() or None

        if not cidade or not endereco:
            flash("Cidade e endereço são obrigatórios.", "danger")
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

        # nova URL vinda do upload direto browser→Cloudinary; fallback para a atual
        nova_url = (request.form.get("imagem_url") or "").strip()
        imagem_url = nova_url or request.form.get("imagem_url_atual") or None

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
#   3. Salva em anvisa_cache (TTL longo; evita reprocessar massa ANVISA)
#   4. produto_detalhe lê do cache → passa var `anvisa` ao template
#   5. /bula/<chave> faz proxy do PDF com Authorization: Guest

ANVISA_CACHE_TTL_DAYS = int(os.getenv("ANVISA_CACHE_TTL_DAYS", "3650"))

_TIPO_PERFUMARIA = re.compile(
    r"\b(sabonete|shampoo|condicionador|creme.capilar|mascara.capilar|oleo.capilar|anticaspa|"
    r"tintura.capilar|tinta.cabelo|coloracao.capilar|depilatorio|depilatório|cera.depilatoria|"
    r"pasta.dental|creme.dental|escova.dental|fio.dental|enxaguante|colutorio|antisseptico.bucal|"
    r"desodorante|antitranspirante|fralda|fraldas|absorvente|lenco.umedecido|lenco.umid\w*|toalha.umed\w*|toalha.umid\w*|toalha.beb|pano.umed\w*|protetor.diario|"
    r"algodao|cotonete|hastes.flexiveis|papel.higienico|preservativo|lubrificante.intimo|"
    r"talco|creme.assadura|oleo.corporal|creme.pes|lixa.pes|cuidado.pes|"
    r"espuma.barba|creme.barba|gel.barba|barbear|pos.barba|"
    r"protetor.labial|lipgel|carmed|labello|"
    r"perfume|colonia|eau.de|"
    r"esmalte|acetona|removedor.esmalte|"
    r"batom|blush|primer|bronzeador|autobronzeador|delineador|sombra|glitter|base.facial|rimel|mascara.cilios|"
    r"anasol|unispray)\b",
    re.IGNORECASE,
)
_TIPO_DERMOCOSMETICO = re.compile(
    r"\b(protetor.solar|bloqueador.solar|fps\b|spf\b|"
    r"serum\b|sérum\b|"
    r"hidratante.facial|creme.facial|creme.anti.?age|creme.anti.?idade|antiidade|antirrugas|anti.?age|"
    r"clareador|despigmentante|"
    r"esfoliante.facial|"
    r"mascara.facial|argila.facial|"
    r"tonico.facial|tônico.facial|agua.micelar|"
    r"antiacne|anti.?acne|tratamento.manchas|firmador.facial|"
    r"bb.?cream|cc.?cream)\b",
    re.IGNORECASE,
)
_TIPO_NUTRICAO = re.compile(
    r"\b(dieta.enteral|formula.infantil|aptamil|enfamil|nan\b|leite.sem.lactose|"
    r"alimento.diabet|isoton[io]|papinha|adocante|"
    r"sucralose|stevi[ao]|frutose|maltit|fresubin|ensure\b|nutren)\b",
    re.IGNORECASE,
)
_TIPO_VAREJO = re.compile(
    r"\b(bala\b|balas\b|chiclete|pacoca|paçoca|barrinha.cereal|chocolate\b|biscoito|agua.mineral|suco\b|"
    r"pilha\b|pilhas\b|bateria.alcalina|carregador|cabo.usb|fone.ouvido|brinquedo|"
    r"produto.limpeza|detergente|papel.sulfite|pastilha\b|"
    r"agulha|seringa|luva.descartavel|luva.procedimento|gaze|atadura|esparadrapo|curativo|band.?aid|"
    r"lanceta|tira.reagente|glicemia|glicosimetro|termometro|nebulizador|"
    r"inalador|cateter|sonda|ostomia|esfigmo|agua.oxigenada|povidine|pvpi|"
    r"clorexidina|soro.fisiologico|agua.destilada|alcool.isopropanol|compressa)\b",
    re.IGNORECASE,
)
_TIPO_SUPLEMENTO = re.compile(
    r"\b(whey|proteina|creatina|bcaa|glutamina|albumina|colageno|colágeno|"
    r"termogenico|termogênico|pre.treino|omega|ômega|probiotico|probiótico|"
    r"fibras?\b|maltodextrina|dextrose|aminoacido|aminoácido|melatonina|"
    r"pronabol|ricosol|goodvit|vit.?natu|vitnatu|vitamina|complexo.b|"
    r"zinco|calcio|ferro|magnesio|potassio|acido.folico|biotina)\b",
    re.IGNORECASE,
)
_TIPO_MEDICAMENTO = re.compile(
    r"\b(\d+\s*mg|\d+\s*mcg|\d+\s*ui|comprimido|capsula|cápsula|"
    r"xarope|ampola|injetavel|injetável|solucao|solução|sublingual|"
    r"colirio|colírio|supositório|supositorio)\b",
    re.IGNORECASE,
)

_TIPOS_NAO_MEDICAMENTO = frozenset({
    "suplemento", "perfumaria", "dermocosmetico", "nutricao", "varejo",
})

# aliases para valores antigos ainda presentes no banco
_TIPO_ALIAS = {
    "cosmetico":  "perfumaria",
    "higiene":    "perfumaria",
    "correlato":  "varejo",
    "alimento":   "nutricao",
    "outro":      "varejo",
    # dermocosmetico agora é válido — sem alias
}


def _classificar_produto(nome: str) -> str:
    """Retorna categoria do produto pelo nome (fallback regex; prefira tipo_ia do banco)."""
    if not nome:
        return ""
    if _TIPO_VAREJO.search(nome):
        return "varejo"
    if _TIPO_NUTRICAO.search(nome):
        return "nutricao"
    if _TIPO_SUPLEMENTO.search(nome):
        return "suplemento"
    if _TIPO_DERMOCOSMETICO.search(nome):
        return "dermocosmetico"
    if _TIPO_PERFUMARIA.search(nome):
        return "perfumaria"
    if _TIPO_MEDICAMENTO.search(nome):
        return "medicamento"
    return ""

_ANVISA_SCHEMA_READY = False


def _anvisa_schema():
    global _ANVISA_SCHEMA_READY
    if _ANVISA_SCHEMA_READY:
        return
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
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS receita_retida BOOLEAN")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS venda_online_permitida BOOLEAN")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS exibir_imagem_publica BOOLEAN")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS dizeres_receita TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS dizeres_imagem TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS tarja_ia TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS tarja_ia_confianca TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS classificacao_ia TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS para_que_serve_ia TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS como_tomar_ia TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS principais_cuidados_ia TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS indicado_para_ia TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS ia_descricao_gerado_em TIMESTAMPTZ")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS url_bula_fabricante TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS fonte_fabricante_url TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS fonte_fabricante_dominio TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS fonte_fabricante_confianca TEXT")
    cur.execute("ALTER TABLE anvisa_cache ADD COLUMN IF NOT EXISTS fonte_fabricante_consultada_em TIMESTAMPTZ")
    conn.commit()
    cur.close()
    _ANVISA_SCHEMA_READY = True


_ANVISA_STOP_WORDS = {
    "com","de","do","da","dos","das","para","por","em","e","ou",
    "mg","mcg","ml","ui","gr","cp","caps","comp","tab","un","und",
    "sol","solucao","injetavel","oral","topico","cutaneo","subl",
    "cpr","drg","amp","fco","bsa","gel","crem","pom","sup","xpe",
    "susp","solu","gota","gotas","soln","inj",
    "rev","retard","ret","iny","inf","efervescente","spray",
    "comprimido","comprimidos","capsula","capsulas","softgel","gelcap",
    "dragea","drageias","xarope","pomada","creme","supositorio",
    "injecao","injetavel","solucao","suspensao","emulsao","granulado",
    "pastilha","pastilhas","sublingual","transdermico","inalacao",
    "revestido","revestidos","liberacao","prolongada","retardada",
    "efervescente","mastigavel","dispersivel","orodisp","orodispersivel",
    # Embalagem / acessório dosador — nunca fazem parte do INN
    "frasco","frascos","litro","litros","copo","copinho","dosador","medidor",
    "conta","seringa","caneta","nebulizador","inalador","vaporizador",
    # Rótulos comerciais — não aparecem em registros ANVISA
    "generico","generica","similar","bioequivalente",
    # Prefixos de sal farmacológico (nunca são o nome ANVISA)
    "cloridrato","bromidrato","dicloridrato","hemitartarato","hemifumarato",
    "maleato","fumarato","succinato","besilato","tartarato",
    "monoidratado","monoidratada","hemif","succ",
    # Sufixos de forma/composição que mascaram INN quando 2ª palavra
    "hidroclor","medoxomila","flacodin",
    # Nomes de laboratório que aparecem como 2ª palavra no estoque
    "germed","vitamedic","biolab","globo","greenbios","uniphar",
    "farmax","quimica","bellaphytus","rioquimica","medley","sandoz",
    "torrent","teuto","eurofarma","prati","donaduzzi","neo","geolab",
    "pharlab","pharma","laboratorio","laboratorios",
    "natulab","multilab","airela","pharmascience","biosintetica",
}

# Mapeamento nome-comercial → INN para lookup no anvisa_cache.
# IMPORTANTE: manter sincronizado com _MARCA_TO_INN em anvisa_sync.py.
_MARCA_TO_INN = {
    # Analgésicos / AINEs
    "ALIVIUM":      "IBUPROFENO",
    "BUPROVIL":     "IBUPROFENO",
    "ARTRINID":     "INDOMETACINA",
    "NIMELIT":      "NIMESULIDA",
    "BENZIFLEX":    "CLONIXINATO LISINA",
    "CODEX":        "CODEINA",
    "COXYM":        "COLCHICINA",
    # Espasmolíticos
    "BUSCOPAN":     "BUTILBROMETO ESCOPOLAMINA",
    "BUSCOPLEX":    "BUTILBROMETO ESCOPOLAMINA",
    # Antibióticos / Antiparasitários
    "AZITROPHAR":   "AZITROMICINA",
    "BELFACTRIM":   "SULFAMETOXAZOL TRIMETOPRIMA",
    "BELMIRAX":     "MEBENDAZOL",
    "BACINA":       "NEOMICINA BACITRACINA",
    "CIPRIXIN":     "CIPROFLOXACINO",
    # Anti-hipertensivos / Cardiovascular
    "ARADOIS":      "LOSARTANA",
    "BESILAPIN":    "ANLODIPINO",
    "FEDIPINA":     "NIFEDIPINO",
    "CARBIDOL":     "CARBIDOPA LEVODOPA",
    # Corticosteroides
    "BETAPROSPAN":  "BETAMETASONA",
    "BETRICORT":    "BETAMETASONA",
    "BIOFLADEX":    "BETAMETASONA",
    "CELERGIN":     "BETAMETASONA",
    "CELESTAMINE":  "BETAMETASONA",
    "CELESTONE":    "BETAMETASONA",
    "CELESTRAT":    "BETAMETASONA",
    "CORTICORTEN":  "PREDNISONA",
    # Anti-histamínicos
    "ALLEXOFEDRIN": "FEXOFENADINA",
    "ARLIVRY":      "LORATADINA",
    "ALERADINA":    "LORATADINA",
    "BERITIN":      "CETIRIZINA",
    # Mucolíticos / Broncodilatadores
    "AMBROL":       "AMBROXOL",
    "AMBROXMEL":    "AMBROXOL",
    "BRONQTRAT":    "AMBROXOL",
    "AERODINI":     "SALBUTAMOL",
    "CELETIL":      "SALBUTAMOL",
    # Vitaminas / outros medicamentos
    "BENERVA":      "TIAMINA",
    "ANTIAZIL":     "HIDROXIDO ALUMINIO",
    "CISTEIL":      "ACETILCISTEINA",
    "CONTRACEP":    "MEDROXIPROGESTERONA",
    "BENZODERM":    "PEROXIDO BENZOILA",
    # Inibidores de bomba de prótons (IBP)
    "ELPRAZOL":     "ESOMEPRAZOL",
    "ESOP":         "ESOMEPRAZOL",
    # Mucolíticos/Expectorantes
    "EMSEXPECT":    "AMBROXOL",
    "EMSEXPECTOR":  "AMBROXOL",
    "EXPECVEM":     "GUAIFENESINA",
    "FLUCETIL":     "ACETILCISTEINA",
    # Contraceptivos
    "ETINIL":       "ETINILESTRADIOL GESTODENO",
    # Anti-histamínico / Ansiolítico
    "DROXY":        "HIDROXIZINA",
    # Colírio antiglaucoma
    "DRUSOLOL":     "DORZOLAMIDA TIMOLOL",
    # AINEs
    "FARMOXICAM":   "PIROXICAM",
    # Antiflatulento
    "FLACODIN":     "SIMETICONA",
    # Antifúngico
    "FUNOK":        "ITRACONAZOL",
    # Antiácido
    "GASTROBEM":    "HIDROXIDO ALUMINIO",
    # Antiflatulento
    "LUFTAL":       "SIMETICONA",
    # Laxativos
    "LACTUGOLD":    "LACTULOSE",
    "NATULAXE":     "BISACODILA",
    # Analgésico/Antipirético
    "TILEMAXY":     "PARACETAMOL",
    # Antifúngico oral
    "NISTAMAX":     "NISTATINA",
    # Antibióticos
    "POLICLAVUMOXIL": "AMOXICILINA CLAVULANATO",
    "NEMICINA":     "NEOMICINA",
    # Anti-inflamatório
    "NEOTAREN":     "DICLOFENACO",
    # Diurético
    "NEOSEMID":     "FUROSEMIDA",
    # Antidiarreico
    "KAOSEC":       "LOPERAMIDA",
    # Variação de grafia INN
    "LORATADIN":    "LORATADINA",
    # Antiemético / cinetose
    "DRAMIN":       "DIMENIDRINATO",
    # Antineoplásico
    "TAMISA":       "TAMOXIFENO",
    # Antidiabético (gliptina)
    "NESINA":       "ALOGLIPTINA",
    # Anticoncepcionais
    "NEOVLAR":      "NORGESTREL",
    "FOLDAN":       "NORGESTREL",
    # Antipsicótico
    "NEOZINE":      "LEVOMEPROMAZINA",
    # Estrogênio TRH
    "SYSTEN":       "ESTRADIOL",
    # Anti-histamínico
    "AVIANT":       "BILASTINA",
}

# Chaves cujo lookup no anvisa_cache deve ser ignorado tanto na escrita (csv/bulário)
# quanto na leitura (_marcar_tarja_batch).  São produtos OTC, cosméticos, higiene ou
# suplementos cujo nome gera uma chave que colide com um medicamento ANVISA tarjado.
_CHAVES_OTC_ISENTO = frozenset({
    # Antissépticos / cosméticos que colidem com versão farmacêutica ANVISA
    "AGUA OXIGENADA", "AGUA BORICADA", "AGUA DESTILADA", "AGUA MELISSA",
    "AGUA", "ALCOOL ETILICO", "ALCOOL GEL", "ALCOOL ANTISSEPTICO", "ALCOOL IODADO",
    "SORO FISIOLOGICO", "ANTISSEPTICO", "CANFORA", "AMONIA",
    # Compostos que o CMED lista em formulações hospitalares mas vende-se como OTC
    "BICARBONATO SODIO", "BICARBONATO CALCIO", "CLORETO SODIO", "CLORETO MAGNESIO",
    # Vitaminas OTC — versão injetável/farmacêutica contamina tablets comuns
    "ACIDO ASCORBICO", "ACIDO FOLICO", "VITAMINA", "VITAM",
    # OTC puros que o CMED/anvisa_sync às vezes tarjam incorretamente
    "PARACETAMOL", "DIPIRONA", "IBUPROFENO", "ACIDO ACETILSALICILICO",
    "PANCREATINA", "DIMENTICONE", "SIMETICONA",
    # Fitoterápicos sem prescrição
    "VALERIANA", "PASSIFLORA", "PANAX",
    # Chaves genéricas demais
    "NOVA", "FONT", "CARVAO VEGETAL",
    # Marcas cosméticas / higiene cujo nome coincide com entrada ANVISA tarjada
    "ASEPXIA", "ASEPXIA SECATIVO", "ASEPXIA FORTE",
    "CAREFREE", "CAREFREE PROT",
    "CIFLOGEX", "CIFLOGEX DIET", "CIFLOGEX LIMAO", "CIFLOGEX MENTA",
    "AVENE", "AVENE AGUA",
    "BEPANTOL", "BEPANTOL DERMA",
    "BIODERMA", "BIODERMA SENSIBIO",
    "VICHY", "VICHY LIFTACTIV",
    "NEUTROGENA", "NEUTROGENA HIDRATANTE",
    "NIVEA", "NIVEA HIDRATANTE",
    # Suplementos/proteínas — falso positivo com Ultracet (tramadol+paracetamol)
    "GOOD ULTRA", "GOOD GLUTAMINA", "GOOD PLUS",
    # Linha capilar — falso positivo com fitoterápicos ANVISA
    "TRUFA MARACUJA", "TRUFA MENTA", "TRUFA BRANCO", "TRUFA CEREJA",
    "TRUFA KIDS", "TRUFA LACREME", "TRUFA LEITE", "TRUFA MEZZO", "TRUFA TRADICIONAL",
})


def _anvisa_chave(nome):
    """Retorna chave de lookup no anvisa_cache: INN se nome comercial mapeado, senão 2 primeiras palavras significativas."""
    tks = re.sub(r"[^\w\s]", " ", nome or "").upper().split()
    if tks and tks[0] in _MARCA_TO_INN:
        return _MARCA_TO_INN[tks[0]]
    words = []
    for w in tks:
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


# Tarja Preta: somente sinais fortes. "Controle especial" sozinho tambem aparece
# em tarja vermelha com receita retida, entao nao deve virar preta por regex.
_TARJA_PRETA_RE = re.compile(
    r"notifica[cç][aã]o\s+de\s+receita\s+[ab]"
    r"|\blista\s+[AB]\d?\b"
    r"|tarja\s+preta",
    re.IGNORECASE,
)

# Tarja Vermelha — exige prescrição simples
_TARJA_VERMELHA_RE = re.compile(
    r"venda\s+sob\s+prescri[cç][aã]o\s+m[eé]dica"
    r"|uso\s+sob\s+prescri[cç][aã]o\s+m[eé]dica"
    r"|somente\s+(?:com|sob)\s+prescri[cç][aã]o"
    r"|tarja\s+vermelha"
    r"|medicamento\s+sujeito\s+a\s+prescri[cç][aã]o"
    r"|receita\s+de\s+controle\s+especial"
    r"|controle\s+especial",
    re.IGNORECASE,
)

_RECEITA_RETENCAO_RE = re.compile(
    r"s[oó]\s+pode\s+ser\s+vendid[oa]\s+com\s+reten[cç][aã]o\s+da\s+receita"
    r"|com\s+reten[cç][aã]o\s+da\s+receita"
    r"|reten[cç][aã]o\s+(?:de|da)\s+receita"
    r"|receita\s+de\s+controle\s+especial"
    r"|notifica[cç][aã]o\s+de\s+receita"
    r"|controle\s+especial"
    r"|sngpc"
    r"|antimicrobian[oa]s?",
    re.IGNORECASE,
)

_NOME_RECEITA_RETIDA_RE = re.compile(
    # Antimicrobianos comuns: tarja vermelha com retencao/escrituracao.
    r"\bamoxicilina\b|\bampicilina\b|\bcefalexina\b|\bcefadroxila\b|\bcefaclor\b"
    r"|\bazitromicina\b|\bclaritromicina\b|\beritromicina\b"
    r"|\bciprofloxacino\b|\blevofloxacino\b|\bnorfloxacino\b|\bofloxacino\b"
    r"|\bmetronidazol\b|\btinidazol\b|\bsulfametoxazol\b|\btrimetoprim\b"
    r"|\btetraciclina\b|\bdoxiciclina\b|\bminociclina\b"
    # Receita de controle especial em tarja vermelha (quando detectado por nome).
    r"|\bfluoxetina\b|\bsertralina\b|\bescitalopram\b|\bcitalopram\b"
    r"|\bparoxetina\b|\bvenlafaxina\b|\bdesvenlafaxina\b|\bduloxetina\b"
    r"|\bamitriptilina\b|\bnortriptilina\b|\bimipramina\b"
    r"|\bcarbamazepina\b|\bfenitoina\b|\bvalproato\b|\btopiramate?\b|\blamotrigina\b"
    r"|\bcodeina\b"
    # Contraceptivos hormonais: tarja vermelha com prescrição.
    r"|\blevonorgestrel\b|\betinilestradiol\b|\bdesogestrel\b|\bgestodeno\b"
    r"|\bnoretisterona\b|\bdrospirenona\b|\bclormadinona\b|\bdienogeste\b",
    re.IGNORECASE,
)


def _detectar_tarja(anvisa: dict) -> str | None:
    """Retorna a tarja do produto. tarja_ia (validação IA) prevalece sobre dado bruto do ANVISA."""
    if not anvisa:
        return None
    # IA validou — usa como fonte primária (corrige dados errados do ANVISA)
    tarja_ia = (anvisa.get("tarja_ia") or "").strip().lower()
    if tarja_ia == "sem_tarja":
        return None
    if tarja_ia in ("preta", "vermelha"):
        return tarja_ia
    # Fallback: dado bruto do ANVISA quando IA ainda não validou
    tarja_bd = (anvisa.get("tarja") or "").strip().lower()
    if tarja_bd in ("preta", "vermelha"):
        return tarja_bd
    return None


def _requer_receita(anvisa: dict) -> bool:
    return _exige_receita_digital_entrega(anvisa)


def _exige_receita_digital_entrega(anvisa: dict | None, nome: str = "") -> bool:
    """True apenas para receita com retencao/controle; tarja vermelha simples nao bloqueia checkout."""
    anvisa = anvisa or {}
    if anvisa.get("receita_retida") is True:
        return True
    if anvisa.get("receita_retida") is False and anvisa.get("tarja"):
        return False
    tarja = _detectar_tarja(anvisa)
    if tarja == "preta":
        return True
    textos = " ".join(filter(None, [
        anvisa.get("alertas") or "",
        anvisa.get("como_usar") or "",
        anvisa.get("nome_anvisa") or "",
        anvisa.get("principio_ativo") or "",
        anvisa.get("tarja") or "",
        nome or "",
    ]))
    return bool(_RECEITA_RETENCAO_RE.search(textos) or _NOME_RECEITA_RETIDA_RE.search(nome or textos))


def _marcar_tarja_batch(produtos: list, conn, ensure_schema=True) -> list:
    """Adiciona requer_receita=True/False a cada produto da lista (in-place + retorna)."""
    if not produtos:
        return produtos
    if ensure_schema:
        try:
            _anvisa_schema()
        except Exception:
            pass
    nomes = [p.get("nome") or "" for p in produtos]

    chaves_map: dict[str, list[int]] = {}
    for i, nome in enumerate(nomes):
        ch = _anvisa_chave(nome)
        if not ch or ch in _CHAVES_OTC_ISENTO:
            continue
        chaves_map.setdefault(ch, []).append(i)

    for p in produtos:
        p["requer_receita"] = False

    if not chaves_map:
        return produtos

    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT chave, alertas, como_usar, nome_anvisa, principio_ativo, tarja, "
            "receita_retida, venda_online_permitida, exibir_imagem_publica, dizeres_receita, dizeres_imagem "
            "FROM anvisa_cache WHERE chave = ANY(%s) AND encontrado = TRUE",
            (list(chaves_map.keys()),),
        )
        rows_by_chave = {r["chave"]: r for r in cur.fetchall()}

        def _aplicar(idx, row):
            # Cosméticos, suplementos e varejo não recebem tarja ANVISA
            _tipo = _classificar_produto(produtos[idx].get("nome") or "")
            _is_med = _tipo not in _TIPOS_NAO_MEDICAMENTO
            if not _is_med:
                return
            produtos[idx]["anvisa_cache_encontrado"] = True
            tarja = _detectar_tarja(dict(row))
            produtos[idx]["tarja"] = tarja
            produtos[idx]["receita_retida"] = bool(row.get("receita_retida")) if row.get("receita_retida") is not None else _exige_receita_digital_entrega(dict(row), produtos[idx].get("nome") or "")
            produtos[idx]["requer_receita"] = bool(produtos[idx]["receita_retida"])
            produtos[idx]["venda_online_permitida"] = row.get("venda_online_permitida")
            produtos[idx]["exibir_imagem_publica"] = row.get("exibir_imagem_publica")
            produtos[idx]["dizeres_receita"] = row.get("dizeres_receita")
            produtos[idx]["dizeres_imagem"] = row.get("dizeres_imagem")
            # Fonte oficial explicita prevalece; tarja e fallback quando a regra e desconhecida.
            _exibir = row.get("exibir_imagem_publica")
            _nao_exibir = _exibir is False
            _bloquear = _nao_exibir or (
                _exibir is None and tarja in ("preta", "vermelha")
            )
            # Tambem bloqueia se a imagem atual e de uma farmacia concorrente
            _imagem_atual = (produtos[idx].get("imagem") or "").strip()
            if not _bloquear and _imagem_atual and _looks_like_other_pharmacy_brand(_imagem_atual):
                _bloquear = True
            if _bloquear:
                # Se tarja nao definida mas imagem bloqueada, usa vermelha como padrao
                tarja_placeholder = tarja if tarja in ("preta", "vermelha") else "vermelha"
                placeholder = _placeholder_for_tarja(tarja_placeholder)
                if placeholder:
                    produtos[idx]["imagem"] = placeholder
                    produtos[idx]["imagem_padrao_poupaqui"] = True
                    produtos[idx]["imagem_bloqueada_anvisa"] = True

        for ch, indices in chaves_map.items():
            if ch in rows_by_chave:
                for idx in indices:
                    _aplicar(idx, rows_by_chave[ch])

        # Fallback: chaves de uma única palavra (ex: "LOSARTANA") que não encontraram
        # resultado — tenta prefixo "LOSARTANA %" para herdar tarja de variantes conhecidas.
        missing_single = [ch for ch in chaves_map if ch not in rows_by_chave and " " not in ch]
        if missing_single:
            try:
                cur.execute(
                    "SELECT DISTINCT ON (SPLIT_PART(chave, ' ', 1)) "
                    "SPLIT_PART(chave, ' ', 1) AS first_word, "
                    "chave, alertas, como_usar, nome_anvisa, principio_ativo, tarja, "
                    "receita_retida, venda_online_permitida, "
                    "exibir_imagem_publica, dizeres_receita, dizeres_imagem "
                    "FROM anvisa_cache "
                    "WHERE SPLIT_PART(chave, ' ', 1) = ANY(%s) AND encontrado = TRUE "
                    "  AND STRPOS(chave, ' ') > 0 "
                    "ORDER BY SPLIT_PART(chave, ' ', 1), chave",
                    (missing_single,),
                )
                prefix_by_word = {r["first_word"]: r for r in cur.fetchall()}
                for ch, indices in chaves_map.items():
                    if ch in prefix_by_word:
                        for idx in indices:
                            _aplicar(idx, prefix_by_word[ch])
            except Exception:
                pass

    except Exception:
        pass
    finally:
        cur.close()

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
               id_produto, tarja, jwt_bula, receita_retida, venda_online_permitida,
               exibir_imagem_publica, dizeres_receita, dizeres_imagem, criado_em)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (chave) DO UPDATE SET
              encontrado=EXCLUDED.encontrado, nome_anvisa=EXCLUDED.nome_anvisa,
              laboratorio=EXCLUDED.laboratorio, situacao=EXCLUDED.situacao,
              principio_ativo=EXCLUDED.principio_ativo, url_bula=EXCLUDED.url_bula,
              serve_para=EXCLUDED.serve_para, como_usar=EXCLUDED.como_usar,
              alertas=EXCLUDED.alertas, id_produto=EXCLUDED.id_produto,
              tarja=EXCLUDED.tarja, jwt_bula=EXCLUDED.jwt_bula,
              receita_retida=EXCLUDED.receita_retida,
              venda_online_permitida=EXCLUDED.venda_online_permitida,
              exibir_imagem_publica=EXCLUDED.exibir_imagem_publica,
              dizeres_receita=EXCLUDED.dizeres_receita,
              dizeres_imagem=EXCLUDED.dizeres_imagem,
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
                dados.get("receita_retida"),
                dados.get("venda_online_permitida"),
                dados.get("exibir_imagem_publica"),
                dados.get("dizeres_receita"),
                dados.get("dizeres_imagem"),
            ),
        )
        conn.commit()
        cur.close()
    except Exception:
        pass


@app.get("/api/anvisa-info")
def api_anvisa_info():
    """Return ANVISA product info by product name. Uses long-lived cache."""
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
        "SELECT * FROM anvisa_cache WHERE chave=%s AND criado_em > NOW() - (%s || ' days')::interval",
        (chave, ANVISA_CACHE_TTL_DAYS),
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
             "url_bula", "serve_para", "como_usar", "alertas", "tarja",
             "receita_retida", "venda_online_permitida", "exibir_imagem_publica",
             "dizeres_receita", "dizeres_imagem")
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
         "url_bula", "serve_para", "como_usar", "alertas", "tarja",
         "receita_retida", "venda_online_permitida", "exibir_imagem_publica",
         "dizeres_receita", "dizeres_imagem")
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
    """Abre a bula oficial do fabricante; usa o fluxo ANVISA como fallback."""
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        """
        SELECT url_bula, url_bula_fabricante, jwt_bula, id_produto
        FROM anvisa_cache
        WHERE chave=%s AND encontrado=TRUE
        """,
        (chave,),
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return redirect(
            "https://consultas.anvisa.gov.br/#/bulario?nomeProduto="
            + urllib.parse.quote(chave)
        )

    if row["url_bula_fabricante"]:
        return redirect(row["url_bula_fabricante"])

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

    # Filtra chaves já no cache dentro do TTL configurado.
    try:
        conn = db(); cur = conn.cursor()
        cur.execute(
            "SELECT chave FROM anvisa_cache WHERE criado_em >= NOW() - (%s || ' days')::interval",
            (ANVISA_CACHE_TTL_DAYS,),
        )
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
        try:
            cur.execute("SELECT to_regclass('public.produto_canon') AS tbl")
            if (cur.fetchone() or {}).get("tbl"):
                cur.close()
                _schema_ready.add(key)
                return
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
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
        cur.execute("ALTER TABLE ecommerce_cupons ADD COLUMN IF NOT EXISTS so_assinantes BOOLEAN DEFAULT FALSE")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_cupons_clientes (
                id SERIAL PRIMARY KEY,
                cupom_id UUID NOT NULL,
                consumidor_id UUID NOT NULL,
                UNIQUE(cupom_id, consumidor_id)
            )
        """)
        # Regras de desconto agora sao geridas pelo admin e podem valer para
        # varias lojas de uma vez — cnpjloja na tabela principal deixa de ser
        # obrigatorio; quem participa da regra mora em ecommerce_cupons_lojas.
        try:
            cur.execute("ALTER TABLE ecommerce_cupons ALTER COLUMN cnpjloja DROP NOT NULL")
        except Exception:
            conn.rollback()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_cupons_lojas (
                id SERIAL PRIMARY KEY,
                cupom_id UUID NOT NULL REFERENCES ecommerce_cupons(id) ON DELETE CASCADE,
                cnpjloja TEXT NOT NULL,
                codigo_upper TEXT NOT NULL DEFAULT '',
                usos_count INTEGER NOT NULL DEFAULT 0,
                criado_em TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(cupom_id, cnpjloja)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cupons_lojas_cnpj ON ecommerce_cupons_lojas(cnpjloja)")
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_cupons_lojas_cnpj_codigo
            ON ecommerce_cupons_lojas (cnpjloja, codigo_upper) WHERE codigo_upper <> ''
        """)
        # Backfill idempotente: todo cupom ja existente (1 loja) ganha sua
        # entrada na juncao automaticamente, preservando o contador de uso.
        cur.execute("""
            INSERT INTO ecommerce_cupons_lojas (cupom_id, cnpjloja, codigo_upper, usos_count)
            SELECT id, cnpjloja, upper(COALESCE(codigo,'')), COALESCE(usos_count,0)
            FROM ecommerce_cupons WHERE cnpjloja IS NOT NULL
            ON CONFLICT (cupom_id, cnpjloja) DO NOTHING
        """)
        conn.commit()
        cur.close()
        _schema_ready.add("cupons")


def _sync_cupom_lojas(cur, cupom_id, cnpjlojas, codigo_upper=""):
    """Replace-all das lojas participantes de uma regra de desconto.

    Preserva usos_count das lojas que continuam na regra (so faz UPSERT do
    codigo_upper), remove quem foi desmarcado e adiciona quem for novo."""
    cnpjlojas = list(dict.fromkeys(c for c in cnpjlojas if c))
    cur.execute(
        "DELETE FROM ecommerce_cupons_lojas WHERE cupom_id=%s AND cnpjloja <> ALL(%s)",
        (cupom_id, cnpjlojas or [""]),
    )
    for cnpj in cnpjlojas:
        cur.execute(
            """
            INSERT INTO ecommerce_cupons_lojas (cupom_id, cnpjloja, codigo_upper)
            VALUES (%s, %s, %s)
            ON CONFLICT (cupom_id, cnpjloja) DO UPDATE SET codigo_upper = EXCLUDED.codigo_upper
            """,
            (cupom_id, cnpj, codigo_upper),
        )


def _ensure_favoritos_schema():
    _load_db_migrations()
    if "favoritos" in _schema_ready:
        return
    with _schema_lock:
        if "favoritos" in _schema_ready:
            return
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_favoritos (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                consumidor_id UUID NOT NULL,
                ean TEXT NOT NULL,
                cnpjloja TEXT NOT NULL,
                nome TEXT NOT NULL,
                preco NUMERIC(10,2),
                imagem TEXT,
                razao TEXT,
                categoria TEXT,
                alerta_preco BOOLEAN DEFAULT TRUE,
                alerta_estoque BOOLEAN DEFAULT TRUE,
                preco_referencia NUMERIC(10,2),
                criado_em TIMESTAMPTZ DEFAULT NOW(),
                atualizado_em TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(consumidor_id, ean, cnpjloja)
            )
        """)
        cur.execute("ALTER TABLE ecommerce_favoritos ADD COLUMN IF NOT EXISTS categoria TEXT")
        cur.execute("ALTER TABLE ecommerce_favoritos ADD COLUMN IF NOT EXISTS alerta_preco BOOLEAN DEFAULT TRUE")
        cur.execute("ALTER TABLE ecommerce_favoritos ADD COLUMN IF NOT EXISTS alerta_estoque BOOLEAN DEFAULT TRUE")
        cur.execute("ALTER TABLE ecommerce_favoritos ADD COLUMN IF NOT EXISTS preco_referencia NUMERIC(10,2)")
        conn.commit()
        cur.close()
        _schema_ready.add("favoritos")


@app.get("/painel/admin/cupons")
@admin_required
def admin_cupons():
    _ensure_cupons_schema()
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT c.*,
               COALESCE(
                 (SELECT string_agg(u.razao, ', ' ORDER BY u.razao)
                  FROM ecommerce_cupons_lojas cl
                  JOIN users u ON u.cnpjloja = cl.cnpjloja
                  WHERE cl.cupom_id = c.id),
                 '—'
               ) AS lojas_nomes,
               COALESCE(
                 (SELECT array_agg(cl.cnpjloja) FROM ecommerce_cupons_lojas cl WHERE cl.cupom_id = c.id),
                 ARRAY[]::text[]
               ) AS lojas_cnpjs,
               (SELECT COALESCE(SUM(cl.usos_count),0) FROM ecommerce_cupons_lojas cl WHERE cl.cupom_id = c.id) AS usos_total
        FROM ecommerce_cupons c
        ORDER BY c.criado_em DESC
    """)
    cupons = cur.fetchall()
    cur.execute("SELECT cnpjloja, razao FROM users WHERE is_admin=FALSE ORDER BY razao")
    lojas = cur.fetchall()
    cur.close()
    return render_template("admin_cupons.html", cupons=cupons, lojas=lojas)


@app.post("/painel/admin/cupons/novo")
@admin_required
def admin_cupons_novo():
    _ensure_cupons_schema()
    f = request.form
    lojas_sel = request.form.getlist("lojas")
    if not lojas_sel:
        flash("Selecione ao menos uma loja.", "error")
        return redirect(url_for("admin_cupons"))
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
        return redirect(url_for("admin_cupons"))
    if desconto_valor <= 0:
        flash("Informe o valor do desconto.", "error")
        return redirect(url_for("admin_cupons"))
    if desconto_tipo == "pct" and desconto_valor > 100:
        flash("Desconto percentual não pode passar de 100%.", "error")
        return redirect(url_for("admin_cupons"))
    if tipo_regra == "pagamento" and not forma_pagamento:
        flash("Selecione a forma de pagamento.", "error")
        return redirect(url_for("admin_cupons"))
    if tipo_regra == "quantidade" and qtd_minima < 1:
        flash("Informe a quantidade mínima (mínimo 1).", "error")
        return redirect(url_for("admin_cupons"))
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
        eans_raw = request.form.getlist("escopo_eans")
        eans_split = [e for raw in eans_raw for e in re.split(r"[,\s]+", raw.strip()) if e]
        escopo_eans = ",".join(dict.fromkeys(eans_split))
    so_assinantes = f.get("so_assinantes") == "1"
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO ecommerce_cupons (cnpjloja, codigo, desconto_tipo, desconto_valor, valido_ate, uso_maximo,
              publico, min_compras, escopo, escopo_categorias, escopo_eans,
              tipo_regra, forma_pagamento, qtd_minima, so_assinantes)
            VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (codigo, desconto_tipo, desconto_valor, valido_ate, uso_maximo,
              publico, min_compras, escopo, escopo_categorias, escopo_eans,
              tipo_regra, forma_pagamento, qtd_minima, so_assinantes))
        cupom_id = str(cur.fetchone()["id"])
        _sync_cupom_lojas(cur, cupom_id, lojas_sel, codigo)
        consumidores_ids = request.form.getlist("consumidores_ids")
        if publico == "especifico" and consumidores_ids:
            for cid in consumidores_ids:
                try:
                    cur.execute("INSERT INTO ecommerce_cupons_clientes (cupom_id, consumidor_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (cupom_id, cid))
                except Exception:
                    pass
        conn.commit()
        labels = {"codigo": f"Cupom {codigo}", "pagamento": "Desconto por pagamento", "quantidade": "Desconto por quantidade"}
        flash(f"{labels.get(tipo_regra,'Regra')} criado com sucesso para {len(lojas_sel)} loja(s).", "success")
    except Exception:
        conn.rollback()
        flash("Código de cupom já existe em uma das lojas selecionadas." if tipo_regra == "codigo" else "Erro ao criar regra de desconto.", "error")
    cur.close()
    return redirect(url_for("admin_cupons"))


@app.post("/painel/admin/cupons/<cupom_id>/editar")
@admin_required
def admin_cupom_editar(cupom_id):
    _ensure_cupons_schema()
    f = request.form
    lojas_sel = request.form.getlist("lojas")
    if not lojas_sel:
        flash("Selecione ao menos uma loja.", "error")
        return redirect(url_for("admin_cupons"))
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
        return redirect(url_for("admin_cupons"))
    if desconto_valor <= 0:
        flash("Informe o valor do desconto.", "error")
        return redirect(url_for("admin_cupons"))
    if desconto_tipo == "pct" and desconto_valor > 100:
        flash("Desconto percentual não pode passar de 100%.", "error")
        return redirect(url_for("admin_cupons"))
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
        eans_raw = request.form.getlist("escopo_eans")
        eans_split = [e for raw in eans_raw for e in re.split(r"[,\s]+", raw.strip()) if e]
        escopo_eans = ",".join(dict.fromkeys(eans_split))
    so_assinantes = f.get("so_assinantes") == "1"
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE ecommerce_cupons SET
              codigo=%s, desconto_tipo=%s, desconto_valor=%s, valido_ate=%s,
              uso_maximo=%s, publico=%s, min_compras=%s,
              escopo=%s, escopo_categorias=%s, escopo_eans=%s,
              tipo_regra=%s, forma_pagamento=%s, qtd_minima=%s, so_assinantes=%s
            WHERE id=%s
        """, (codigo, desconto_tipo, desconto_valor, valido_ate, uso_maximo,
              publico, min_compras, escopo, escopo_categorias, escopo_eans,
              tipo_regra, forma_pagamento, qtd_minima, so_assinantes,
              cupom_id))
        if cur.rowcount == 0:
            flash("Regra não encontrada.", "error")
        else:
            _sync_cupom_lojas(cur, cupom_id, lojas_sel, codigo)
            conn.commit()
            flash("Regra de desconto atualizada.", "success")
    except Exception:
        conn.rollback()
        flash("Código já existe em uma das lojas selecionadas.", "error")
    cur.close()
    return redirect(url_for("admin_cupons"))


@app.post("/painel/admin/cupons/<cupom_id>/toggle")
@admin_required
def admin_cupom_toggle(cupom_id):
    _ensure_cupons_schema()
    conn = db(); cur = conn.cursor()
    cur.execute("UPDATE ecommerce_cupons SET ativo = NOT ativo WHERE id=%s", (cupom_id,))
    conn.commit(); cur.close()
    return redirect(url_for("admin_cupons"))


@app.post("/painel/admin/cupons/<cupom_id>/excluir")
@admin_required
def admin_cupom_excluir(cupom_id):
    _ensure_cupons_schema()
    conn = db(); cur = conn.cursor()
    cur.execute("DELETE FROM ecommerce_cupons WHERE id=%s", (cupom_id,))
    conn.commit(); cur.close()
    flash("Cupom removido.", "success")
    return redirect(url_for("admin_cupons"))


@app.post("/painel/admin/cupons/<cupom_id>/clientes")
@admin_required
def admin_cupom_clientes(cupom_id):
    _ensure_cupons_schema()
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT id FROM ecommerce_cupons WHERE id=%s LIMIT 1", (cupom_id,))
    if not cur.fetchone():
        flash("Cupom não encontrado.", "error")
        cur.close(); return redirect(url_for("admin_cupons"))
    # Replace all assigned consumers
    cur.execute("DELETE FROM ecommerce_cupons_clientes WHERE cupom_id=%s", (cupom_id,))
    consumidores_ids = request.form.getlist("consumidores_ids")
    for cid in consumidores_ids:
        cur.execute("INSERT INTO ecommerce_cupons_clientes (cupom_id, consumidor_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (cupom_id, cid))
    conn.commit(); cur.close()
    flash("Clientes do cupom atualizados.", "success")
    return redirect(url_for("admin_cupons"))


@app.get("/api/cupom/validar")
def api_cupom_validar():
    _ensure_cupons_schema()
    cnpjloja = (request.args.get("cnpj") or "").strip()
    codigo   = (request.args.get("codigo") or "").strip().upper()
    total    = _to_float_or_none(request.args.get("total")) or 0
    if not cnpjloja or not codigo:
        return jsonify({"valido": False, "msg": "Dados incompletos."})
    is_assinante = _consumidor_e_assinante(session.get("consumidor_id"), cnpjloja)
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT c.* FROM ecommerce_cupons c
        JOIN ecommerce_cupons_lojas cl ON cl.cupom_id = c.id AND cl.cnpjloja = %s
        WHERE upper(c.codigo)=%s AND c.ativo=TRUE
          AND COALESCE(c.tipo_regra,'codigo')='codigo'
          AND (c.valido_ate IS NULL OR c.valido_ate >= CURRENT_DATE)
          AND (c.uso_maximo = 0 OR cl.usos_count < c.uso_maximo)
          AND (COALESCE(c.so_assinantes, FALSE) = FALSE OR %s)
        LIMIT 1
    """, (cnpjloja, codigo, is_assinante))
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
    is_assinante = _consumidor_e_assinante(session.get("consumidor_id"), cnpjloja)
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT c.tipo_regra, COALESCE(c.forma_pagamento,'') AS forma_pagamento,
               c.desconto_tipo, c.desconto_valor,
               COALESCE(c.qtd_minima,0) AS qtd_minima,
               COALESCE(c.escopo,'todos') AS escopo,
               COALESCE(c.escopo_categorias,'') AS escopo_categorias,
               COALESCE(c.escopo_eans,'') AS escopo_eans
        FROM ecommerce_cupons c
        JOIN ecommerce_cupons_lojas cl ON cl.cupom_id = c.id AND cl.cnpjloja = %s
        WHERE c.ativo=TRUE
          AND COALESCE(c.tipo_regra,'codigo') IN ('pagamento','quantidade')
          AND (c.valido_ate IS NULL OR c.valido_ate >= CURRENT_DATE)
          AND (c.uso_maximo = 0 OR cl.usos_count < c.uso_maximo)
          AND (COALESCE(c.so_assinantes, FALSE) = FALSE OR %s)
        ORDER BY c.desconto_valor DESC
    """, (cnpjloja, is_assinante))
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


@app.get("/api/lojas-com-assinatura")
def api_lojas_com_assinatura():
    """Retorna quais CNPJs têm plano de assinatura ativo e se o consumidor já assina cada um."""
    cnpjs_param = (request.args.get("cnpjs") or "").strip()
    if not cnpjs_param:
        return jsonify({"planos": {}})
    cnpjs = [c.strip() for c in cnpjs_param.split(",") if c.strip()][:30]
    cnpj_keys = sorted({_digits(c) for c in cnpjs if _digits(c)})
    if not cnpjs or not cnpj_keys:
        return jsonify({"planos": {}})
    try:
        _ensure_assinatura_schema()
        conn = db(); cur = conn.cursor()
        placeholders = ",".join(["%s"] * len(cnpj_keys))
        cur.execute(
            f"""SELECT regexp_replace(COALESCE(cnpjloja,''), '\\D', '', 'g') AS cnpj_key,
                       cnpjloja, nome, preco_mensal,
                       COALESCE(frete_gratis_primeira_entrega, FALSE) AS frete_gratis_primeira_entrega
                FROM ecommerce_planos_assinatura
                WHERE regexp_replace(COALESCE(cnpjloja,''), '\\D', '', 'g') IN ({placeholders})
                  AND ativo=TRUE""",
            cnpj_keys,
        )
        rows = cur.fetchall()
        requested_by_key = {_digits(c): c for c in cnpjs if _digits(c)}
        planos = {
            requested_by_key.get(r["cnpj_key"], r["cnpjloja"]): {
                "nome": r["nome"],
                "preco": float(r["preco_mensal"] or 0),
                "cnpj_key": r["cnpj_key"],
                "frete_gratis_primeira_entrega": bool(r["frete_gratis_primeira_entrega"]),
            }
            for r in rows
        }

        # Verifica quais o consumidor já assina
        assinados = set()
        consumidor_id = str(session.get("consumidor_id") or "")
        if consumidor_id and planos:
            cnpjs_com_plano = [info["cnpj_key"] for info in planos.values()]
            ph2 = ",".join(["%s"] * len(cnpjs_com_plano))
            cur.execute(
                f"""SELECT regexp_replace(COALESCE(cnpjloja,''), '\\D', '', 'g') AS cnpj_key
                    FROM ecommerce_assinantes
                    WHERE consumidor_id=%s
                      AND regexp_replace(COALESCE(cnpjloja,''), '\\D', '', 'g') IN ({ph2})
                      AND status='ativo' AND pagamento_status='aprovado'
                      AND (data_fim IS NULL OR data_fim > NOW())""",
                [consumidor_id] + cnpjs_com_plano,
            )
            assinados = {r["cnpj_key"] for r in cur.fetchall()}
        cur.close()
        result = {
            cnpj: {
                "nome": info["nome"],
                "preco": info["preco"],
                "ja_assina": info["cnpj_key"] in assinados,
                "frete_gratis_primeira_entrega": info["frete_gratis_primeira_entrega"],
            }
            for cnpj, info in planos.items()
        }
        return jsonify({"planos": result})
    except Exception:
        return jsonify({"planos": {}})


@app.get("/api/cupons/disponiveis")
def api_cupons_disponiveis():
    if os.getenv("CUPONS_ENABLED", "1") == "0":
        return jsonify({"cupons": []})
    _ensure_cupons_schema()
    cnpjloja = (request.args.get("cnpj") or "").strip()
    if not cnpjloja:
        return jsonify({"cupons": []})
    consumidor_id = session.get("consumidor_id")
    conn = db(); cur = conn.cursor()
    # Count consumer's orders from this store
    n_pedidos = 0
    if consumidor_id:
        cur.execute(
            "SELECT COUNT(*) AS n FROM ecommerce_pedidos WHERE cnpjloja=%s AND consumidor_id=%s AND status NOT IN ('cancelado')",
            (cnpjloja, consumidor_id)
        )
        n_pedidos = (cur.fetchone() or {}).get("n") or 0
    # Verifica se consumidor é assinante desta loja
    is_assinante = False
    if consumidor_id:
        try:
            _ensure_assinatura_schema()
            cur.execute(
                """SELECT 1 FROM ecommerce_assinantes
                   WHERE consumidor_id=%s AND cnpjloja=%s
                     AND status='ativo' AND pagamento_status='aprovado'
                     AND (data_fim IS NULL OR data_fim > NOW())
                   LIMIT 1""",
                (str(consumidor_id), cnpjloja),
            )
            is_assinante = bool(cur.fetchone())
        except Exception:
            pass

    publico_extra = """
            OR (c.publico = 'especifico' AND EXISTS (
                SELECT 1 FROM ecommerce_cupons_clientes cc
                WHERE cc.cupom_id = c.id AND cc.consumidor_id = %s
            ))
            OR (c.publico = 'primeira_compra' AND %s = 0)
            OR (c.publico = 'frequente' AND %s >= c.min_compras AND c.min_compras > 0)
    """ if consumidor_id else ""
    assin_filter = "" if is_assinante else "AND COALESCE(c.so_assinantes, FALSE) = FALSE"
    params_cupom = (cnpjloja, consumidor_id, n_pedidos, n_pedidos) if consumidor_id else (cnpjloja,)
    cur.execute(f"""
        SELECT c.id, c.codigo, c.desconto_tipo, c.desconto_valor, c.valido_ate, c.publico, c.min_compras,
               COALESCE(c.escopo,'todos') AS escopo,
               COALESCE(c.escopo_categorias,'') AS escopo_categorias,
               COALESCE(c.escopo_eans,'') AS escopo_eans,
               COALESCE(c.so_assinantes, FALSE) AS so_assinantes
        FROM ecommerce_cupons c
        JOIN ecommerce_cupons_lojas cl ON cl.cupom_id = c.id AND cl.cnpjloja = %s
        WHERE c.ativo = TRUE
          AND COALESCE(c.tipo_regra,'codigo') = 'codigo'
          AND (c.valido_ate IS NULL OR c.valido_ate >= CURRENT_DATE)
          AND (c.uso_maximo = 0 OR cl.usos_count < c.uso_maximo)
          {assin_filter}
          AND (
            c.publico = 'todos'
            {publico_extra}
          )
        ORDER BY c.desconto_valor DESC
    """, params_cupom)
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
            "so_assinantes": bool(r.get("so_assinantes")),
        })
    return jsonify({"cupons": result})


@app.get("/meus-cupons")
@_consumer_required
def consumidor_cupons():
    _ensure_cupons_schema()
    _ensure_logo_url_column()
    consumidor_id = session.get("consumidor_id")
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT u.razao, u.cnpjloja, c2.logo_url,
               COUNT(DISTINCT p.id) AS n_pedidos
        FROM ecommerce_pedidos p
        JOIN users u ON u.cnpjloja = p.cnpjloja
        LEFT JOIN ecommerce_config_loja c2 ON c2.cnpjloja = u.cnpjloja
        WHERE p.consumidor_id = %s AND p.status NOT IN ('cancelado')
        GROUP BY u.razao, u.cnpjloja, c2.logo_url
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
            JOIN ecommerce_cupons_lojas cl ON cl.cupom_id = c.id AND cl.cnpjloja = %s
            WHERE c.ativo = TRUE
              AND COALESCE(c.tipo_regra,'codigo') = 'codigo'
              AND (c.valido_ate IS NULL OR c.valido_ate >= CURRENT_DATE)
              AND (c.uso_maximo = 0 OR cl.usos_count < c.uso_maximo)
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
# MERCADO LIVRE — integração completa (Modelo C: conta única Poupaqui)
# ═══════════════════════════════════════════════════════════════════════════════

# ── helpers de token ──────────────────────────────────────────────────────────

def _ml_current_cnpj():
    try:
        return session.get("cnpjloja")
    except Exception:
        return None


def _ml_get_token(cnpjloja=None, ml_user_id=None):
    """Retorna access_token válido da loja, renovando automaticamente se necessário."""
    _ensure_ml_accounts_schema()
    cnpjloja = cnpjloja or _ml_current_cnpj()
    conn = db(); cur = conn.cursor()
    if cnpjloja:
        cur.execute("""
            SELECT cnpjloja, access_token, refresh_token, expires_at
            FROM ml_tokens_loja
            WHERE cnpjloja=%s
            LIMIT 1
        """, (cnpjloja,))
    elif ml_user_id:
        cur.execute("""
            SELECT cnpjloja, access_token, refresh_token, expires_at
            FROM ml_tokens_loja
            WHERE ml_user_id=%s
            LIMIT 1
        """, (str(ml_user_id),))
    else:
        cur.execute("""
            SELECT cnpjloja, access_token, refresh_token, expires_at
            FROM ml_tokens_loja
            ORDER BY updated_at DESC
            LIMIT 1
        """)
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    if row["expires_at"] <= datetime.now(timezone.utc) + timedelta(minutes=5):
        return _ml_refresh_token(row["refresh_token"], row["cnpjloja"])
    return row["access_token"]


def _ml_refresh_token(refresh_token, cnpjloja):
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
        UPDATE ml_tokens_loja
        SET access_token=%s, refresh_token=%s, expires_at=%s, updated_at=NOW()
        WHERE cnpjloja=%s
    """, (access_token, new_refresh, expires_at, cnpjloja))
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


def _ml_api_get_raw(path, token=None, accept="application/octet-stream"):
    """GET autenticado que retorna bytes, usado para etiqueta/arquivos."""
    if token is None:
        token = _ml_get_token()
    if not token:
        return None, 401, "Sem token ML válido."
    req = urllib.request.Request(ML_API_BASE + path)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    req.add_header("Accept", accept)
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            return resp.read(), resp.status, resp.headers.get("Content-Type", accept)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:400]
        return None, e.code, body
    except Exception as e:
        return None, 500, str(e)


def _ml_flat_value(value):
    if isinstance(value, dict):
        return value.get("id") or value.get("name") or value.get("description") or json.dumps(value, ensure_ascii=False)
    if value is None:
        return None
    return str(value)


def _ml_shipping_instruction(logistic_type, mode, status=None, substatus=None):
    logistic_type = (logistic_type or "").strip()
    mode = (mode or "").strip()
    status = (status or "").strip()
    substatus = (substatus or "").strip()
    if logistic_type == "fulfillment":
        return "Produto em fulfillment do Mercado Livre. A expedição é operacionalizada pelo Mercado Livre."
    if logistic_type == "self_service":
        return "Modalidade Mercado Envios Flex/entrega própria. Prepare o produto e siga a rota/instrução liberada pelo Mercado Livre."
    if logistic_type in {"drop_off", "xd_drop_off"}:
        return "Imprima a etiqueta e leve o pacote ao ponto/agência indicado pelo Mercado Livre."
    if logistic_type == "cross_docking":
        return "Prepare o pacote com etiqueta. Se a conta tiver coleta habilitada, aguarde a coleta; caso contrário, siga o ponto de despacho indicado pelo Mercado Livre."
    if mode == "me2":
        return "Mercado Envios ativo. Consulte a etiqueta e a instrução de despacho; a modalidade exata depende da conta e da venda."
    if status or substatus:
        return "Envio vinculado ao Mercado Livre. Sincronize para acompanhar status e instruções."
    return "Ainda não há detalhe de envio disponível para este pedido."


def _ml_parse_shipment(shipping):
    shipping = shipping or {}
    receiver = shipping.get("receiver_address") or {}
    city = receiver.get("city") or {}
    state = receiver.get("state") or {}
    return {
        "id": _ml_flat_value(shipping.get("id")),
        "status": _ml_flat_value(shipping.get("status")),
        "substatus": _ml_flat_value(shipping.get("substatus")),
        "mode": _ml_flat_value(shipping.get("mode")),
        "logistic_type": _ml_flat_value(shipping.get("logistic_type")),
        "tracking_number": _ml_flat_value(shipping.get("tracking_number") or shipping.get("tracking_code")),
        "tracking_method": _ml_flat_value(shipping.get("tracking_method")),
        "date_created": _ml_flat_value(shipping.get("date_created")),
        "last_updated": _ml_flat_value(shipping.get("last_updated")),
        "estimated_delivery": _ml_flat_value(
            ((shipping.get("shipping_option") or {}).get("estimated_delivery_time") or {}).get("date")
        ),
        "address": {
            "street": receiver.get("street_name") or "",
            "number": receiver.get("street_number") or "",
            "city": city.get("name") or "",
            "state": state.get("name") or "",
            "zip_code": re.sub(r"\D+", "", receiver.get("zip_code", "") or ""),
        },
    }


def _ml_sync_shipment_for_pedido(pedido_id=None, ml_order_id=None, shipping_id=None, cnpjloja=None):
    """Busca envio no ML, salva dados principais no pedido e retorna info para UI."""
    _ensure_ml_shipping_schema()
    token = _ml_get_token(cnpjloja=cnpjloja)
    if not token:
        return None, "Sem token ML válido. Reconecte a conta Mercado Livre."

    pedido_row = None
    if pedido_id:
        conn = db(); cur = conn.cursor()
        if cnpjloja:
            cur.execute(
                "SELECT id, ml_order_id, ml_shipping_id FROM ecommerce_pedidos WHERE id=%s AND cnpjloja=%s LIMIT 1",
                (pedido_id, cnpjloja),
            )
        else:
            cur.execute(
                "SELECT id, ml_order_id, ml_shipping_id FROM ecommerce_pedidos WHERE id=%s LIMIT 1",
                (pedido_id,),
            )
        pedido_row = cur.fetchone(); cur.close()
        if not pedido_row:
            return None, "Pedido não encontrado."
        ml_order_id = ml_order_id or pedido_row.get("ml_order_id")
        shipping_id = shipping_id or pedido_row.get("ml_shipping_id")

    if not shipping_id and ml_order_id:
        order_data = _ml_api_get(f"/orders/{ml_order_id}", token)
        if not order_data:
            return None, f"Não foi possível consultar o pedido ML {ml_order_id}."
        shipping_id = _ml_flat_value((order_data.get("shipping") or {}).get("id"))

    if not shipping_id:
        return None, "Pedido Mercado Livre sem shipping_id. Pode ser venda sem Mercado Envios ou envio ainda não gerado."

    shipping = _ml_api_get(f"/shipments/{shipping_id}", token)
    if not shipping:
        return None, f"Não foi possível consultar o envio {shipping_id}."
    info = _ml_parse_shipment(shipping)
    info["instruction"] = _ml_shipping_instruction(
        info.get("logistic_type"), info.get("mode"), info.get("status"), info.get("substatus")
    )

    if pedido_id:
        conn = db(); cur = conn.cursor()
        cur.execute(
            """
            UPDATE ecommerce_pedidos
            SET ml_shipping_id=%s,
                ml_shipping_status=%s,
                ml_shipping_substatus=%s,
                ml_shipping_mode=%s,
                ml_logistic_type=%s,
                ml_tracking_number=%s,
                ml_tracking_method=%s,
                ml_shipping_updated_at=NOW()
            WHERE id=%s
            """,
            (
                info.get("id"), info.get("status"), info.get("substatus"), info.get("mode"),
                info.get("logistic_type"), info.get("tracking_number"), info.get("tracking_method"),
                pedido_id,
            ),
        )
        conn.commit(); cur.close()
    return info, ""


def _ml_is_connected(cnpjloja=None):
    """Verifica se há token válido armazenado."""
    _ensure_ml_accounts_schema()
    cnpjloja = cnpjloja or _ml_current_cnpj()
    if not cnpjloja:
        return False
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT expires_at FROM ml_tokens_loja WHERE cnpjloja=%s LIMIT 1", (cnpjloja,))
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

def _ml_tokens_candidates(cnpjloja=None, ml_user_id=None):
    _ensure_ml_accounts_schema()
    conn = db(); cur = conn.cursor()
    if cnpjloja:
        cur.execute("""
            SELECT cnpjloja, access_token, refresh_token, expires_at
            FROM ml_tokens_loja
            WHERE cnpjloja=%s
        """, (cnpjloja,))
    elif ml_user_id:
        cur.execute("""
            SELECT cnpjloja, access_token, refresh_token, expires_at
            FROM ml_tokens_loja
            WHERE ml_user_id=%s
            ORDER BY updated_at DESC
        """, (str(ml_user_id),))
    else:
        cur.execute("""
            SELECT cnpjloja, access_token, refresh_token, expires_at
            FROM ml_tokens_loja
            ORDER BY updated_at DESC
        """)
    rows = cur.fetchall(); cur.close()
    return rows


def _process_ml_order(order_id, cnpjloja_hint=None, ml_user_id=None):
    """Busca detalhes do pedido no ML e cria em ecommerce_pedidos. Roda em thread."""
    try:
        import uuid as _uuid
        _ensure_ml_shipping_schema()
        token = None
        token_cnpj = None
        order = None
        for candidate in _ml_tokens_candidates(cnpjloja_hint, ml_user_id):
            token_cnpj = candidate["cnpjloja"]
            token = _ml_get_token(cnpjloja=token_cnpj)
            if not token:
                continue
            order = _ml_api_get(f"/orders/{order_id}", token)
            if order:
                break
        if not token or not order:
            print(f"[ML] Sem token para processar pedido {order_id}")
            return

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
        billing_info = order.get("billing_info") or buyer.get("billing_info") or {}
        cliente_documento = _digits(
            billing_info.get("doc_number")
            or billing_info.get("document_number")
            or billing_info.get("docNumber")
            or buyer.get("doc_number")
            or buyer.get("document")
            or ""
        )

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
        shipping_info = {}
        endereco_entrega = ""
        cep_comprador = ""
        if shipping_id:
            shipping = _ml_api_get(f"/shipments/{shipping_id}", token)
            if shipping:
                shipping_info = _ml_parse_shipment(shipping)
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
        cnpjloja = cnpjloja_publicou or token_cnpj or cnpjloja_hint
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
                id, cnpjloja, consumidor_id, cliente_nome, cliente_telefone, cliente_documento, cliente_email,
                forma_pagamento, total, status, pagamento_status,
                tipo_entrega, endereco_entrega,
                origem, ml_order_id, ml_shipping_id, ml_shipping_status, ml_shipping_substatus,
                ml_shipping_mode, ml_logistic_type, ml_tracking_number, ml_tracking_method,
                ml_shipping_updated_at, criado_em, atualizado_em
            ) VALUES (
                %s,%s,NULL,%s,%s,%s,%s,
                'mercado_livre',%s,%s,%s,
                'entrega',%s,
                'mercado_livre',%s,%s,%s,%s,
                %s,%s,%s,%s,
                CASE WHEN %s IS NULL THEN NULL ELSE NOW() END,NOW(),NOW()
            )
        """, (
            pedido_id, cnpjloja, cliente_nome, cliente_telefone, cliente_documento or None, cliente_email,
            total, status_ped, pag_status,
            endereco_entrega, str(order_id), str(shipping_id) if shipping_id else None,
            shipping_info.get("status"), shipping_info.get("substatus"), shipping_info.get("mode"),
            shipping_info.get("logistic_type"), shipping_info.get("tracking_number"),
            shipping_info.get("tracking_method"), str(shipping_id) if shipping_id else None,
        ))
        for it in itens_pedido:
            cur2.execute("""
                INSERT INTO ecommerce_pedido_itens (pedido_id, ean, nome, preco_unitario, qty)
                VALUES (%s,%s,%s,%s,%s)
            """, (pedido_id, it["ean"], it["nome"], it["preco"], it["qty"]))
        conn2.commit(); cur2.close()
        if status_ped == "pago":
            _alpha_export_paid_order_safe(pedido_id)
        print(f"[ML] Pedido {order_id} criado → {pedido_id} (loja {cnpjloja})")
    except Exception as e:
        print(f"[ML] Erro ao processar pedido {order_id}: {e}")
        raise  # propaga para _ml_flush_queue gravar o erro real na fila


# ── OAuth: iniciar autenticação ───────────────────────────────────────────────

@app.get("/ml/auth")
@painel_required
def ml_auth():
    _ensure_ml_accounts_schema()
    if not ML_APP_ID or not ML_SECRET:
        faltando = []
        if not ML_APP_ID:
            faltando.append("ML_APP_ID")
        if not ML_SECRET:
            faltando.append("ML_CLIENT_SECRET")
        flash(
            "Integração Mercado Livre incompleta. Configure no ambiente: "
            + ", ".join(faltando)
            + ".",
            "error",
        )
        return redirect(url_for("painel_ml"))
    cnpjloja = session.get("cnpjloja")
    state = secrets.token_urlsafe(24)
    session["ml_oauth_state"] = state
    session["ml_oauth_cnpjloja"] = cnpjloja
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": ML_APP_ID,
        "redirect_uri": ML_REDIRECT,
        "state": state,
    })
    return redirect(f"{ML_AUTH_URL}?{params}")


# ── OAuth: callback ───────────────────────────────────────────────────────────

@app.get("/ml/callback")
def ml_callback():
    _ensure_ml_accounts_schema()
    if not ML_APP_ID or not ML_SECRET:
        flash("Integração Mercado Livre incompleta. Configure ML_APP_ID e ML_CLIENT_SECRET.", "error")
        return redirect(url_for("painel_ml"))
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    expected_state = session.get("ml_oauth_state")
    cnpjloja = session.get("ml_oauth_cnpjloja") or session.get("cnpjloja")
    if not code:
        flash("Autorização ML cancelada ou inválida.", "error")
        return redirect(url_for("painel_ml"))
    if not expected_state or state != expected_state or not cnpjloja:
        flash("Autorização ML expirada ou sem loja vinculada. Tente conectar novamente pelo painel da loja.", "error")
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
        INSERT INTO ml_tokens_loja (cnpjloja, access_token, refresh_token, expires_at, ml_user_id, updated_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT (cnpjloja) DO UPDATE
          SET access_token=%s, refresh_token=%s, expires_at=%s, ml_user_id=%s, updated_at=NOW()
    """, (cnpjloja, access_token, refresh_token, expires_at, ml_user_id,
          access_token, refresh_token, expires_at, ml_user_id))
    conn.commit(); cur.close()
    session.pop("ml_oauth_state", None)
    session.pop("ml_oauth_cnpjloja", None)

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


def _ml_feedback_entregue(ml_order_id, cnpjloja=None):
    """
    Notifica o ML que o pedido foi entregue.
    - Com Mercado Envios (tem shipment_id): POST /shipments/{id}/fulfillment
    - Entrega a combinar (sem shipment_id): POST /orders/{id}/feedback com fulfilled=true
    Retorna (True, "") em sucesso ou (False, mensagem_erro) em falha.
    """
    try:
        token = _ml_get_token(cnpjloja=cnpjloja)
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

def _ml_queue_order(order_id_str, ml_user_id=None):
    """Enfileira um order_id ML para processamento. Cria a tabela se não existir."""
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ml_order_queue (
                order_id TEXT PRIMARY KEY,
                received_at TIMESTAMPTZ DEFAULT NOW(),
                processed BOOLEAN DEFAULT FALSE,
                error TEXT,
                ml_user_id TEXT
            )
        """)
        cur.execute("ALTER TABLE ml_order_queue ADD COLUMN IF NOT EXISTS ml_user_id TEXT")
        cur.execute(
            """
            INSERT INTO ml_order_queue (order_id, ml_user_id)
            VALUES (%s, %s)
            ON CONFLICT (order_id) DO UPDATE
              SET ml_user_id=COALESCE(EXCLUDED.ml_user_id, ml_order_queue.ml_user_id)
            """,
            (order_id_str, str(ml_user_id) if ml_user_id else None)
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
                error TEXT,
                ml_user_id TEXT
            )
        """)
        cur.execute("ALTER TABLE ml_order_queue ADD COLUMN IF NOT EXISTS ml_user_id TEXT")
        cur.execute(
            "SELECT order_id, ml_user_id FROM ml_order_queue WHERE processed=FALSE ORDER BY received_at LIMIT 20"
        )
        pendentes = cur.fetchall()
        cur.close()
    except Exception:
        return

    for rowq in pendentes:
        oid = rowq["order_id"]
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

            _process_ml_order(int(oid), ml_user_id=rowq.get("ml_user_id"))

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
        ml_user_id = payload.get("user_id") or payload.get("application_id")

        if ("orders" in topic or "orders" in resource) and resource:
            order_id = resource.strip("/").split("/")[-1]
            if order_id.isdigit():
                _ml_queue_order(order_id, ml_user_id)
                # Tenta processar imediatamente; se falhar, fila garante reprocessamento
                try:
                    _process_ml_order(int(order_id), ml_user_id=ml_user_id)
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
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT id FROM ecommerce_pedidos WHERE ml_order_id=%s LIMIT 1", (order_id_raw,))
    if cur.fetchone():
        cur.close()
        return jsonify({"ok": False, "erro": "Pedido já importado anteriormente."})
    cur.close()
    try:
        _process_ml_order(order_id, cnpjloja_hint=cnpjloja)
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
                processed BOOLEAN DEFAULT FALSE,
                ml_user_id TEXT
            )
        """)
        cur.execute("ALTER TABLE ml_order_queue ADD COLUMN IF NOT EXISTS ml_user_id TEXT")
        cur.execute("SELECT order_id, ml_user_id FROM ml_order_queue WHERE processed=FALSE ORDER BY received_at LIMIT 20")
        pendentes = cur.fetchall()
        cur.close()
    except Exception:
        return jsonify({"ok": False, "erro": "Erro ao acessar fila."}), 500

    importados = 0
    for rowq in pendentes:
        oid = rowq["order_id"]
        try:
            conn2 = db(); cur2 = conn2.cursor()
            cur2.execute("SELECT id FROM ecommerce_pedidos WHERE ml_order_id=%s LIMIT 1", (oid,))
            if cur2.fetchone():
                cur2.execute("UPDATE ml_order_queue SET processed=TRUE WHERE order_id=%s", (oid,))
                conn2.commit(); cur2.close()
                continue
            cur2.close()
            _process_ml_order(int(oid), ml_user_id=rowq.get("ml_user_id"))
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
    _ensure_ml_accounts_schema()
    cnpjloja = session.get("cnpjloja")
    connected = _ml_is_connected(cnpjloja)

    # Conta pedidos ML desta loja
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) AS total,
               MAX(criado_em) AS ultimo
        FROM ecommerce_pedidos
        WHERE cnpjloja=%s AND origem='mercado_livre'
    """, (cnpjloja,))
    ml_stats = dict(cur.fetchone() or {})

    # Token info
    cur.execute("SELECT ml_user_id, updated_at FROM ml_tokens_loja WHERE cnpjloja=%s LIMIT 1", (cnpjloja,))
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
    _ensure_ml_accounts_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute("DELETE FROM ml_tokens_loja WHERE cnpjloja=%s", (cnpjloja,))
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
    _ensure_ml_accounts_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT mi.ml_item_id, mi.ean, mi.titulo, mi.preco, mi.status
        FROM ml_items mi
        WHERE mi.cnpjloja=%s
          AND mi.ean IN (
            SELECT barras FROM estoque WHERE cnpj=%s
            UNION
            SELECT ean FROM automatiza_estoque WHERE cnpj_loja=%s
        )
    """, (cnpjloja, cnpjloja, cnpjloja))
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
    _ensure_ml_accounts_schema()
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
    cur.execute("SELECT ml_item_id, status FROM ml_items WHERE ean=%s AND cnpjloja=%s", (ean, session.get("cnpjloja")))
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
            UPDATE ml_items SET preco=%s, status='active', updated_at=NOW() WHERE ml_item_id=%s AND cnpjloja=%s
        """, (preco, ml_item_id, session.get("cnpjloja")))
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
    _ensure_ml_accounts_schema()
    body = request.get_json(force=True) or {}
    ean = (body.get("ean") or "").strip()
    if not ean:
        return jsonify({"ok": False, "erro": "EAN obrigatório."}), 400

    token = _ml_get_token()
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT ml_item_id FROM ml_items WHERE ean=%s AND cnpjloja=%s", (ean, session.get("cnpjloja")))
    row = cur.fetchone()
    if not row:
        cur.close()
        return jsonify({"ok": False, "erro": "Produto não publicado no ML."}), 404

    ml_item_id = row["ml_item_id"]
    resp, code = _ml_api_put(f"/items/{ml_item_id}", {"status": "paused"}, token)
    if code not in (200, 201):
        cur.close()
        return jsonify({"ok": False, "erro": f"Erro ML {code}: {resp}"}), 400

    cur.execute("UPDATE ml_items SET status='paused', updated_at=NOW() WHERE ml_item_id=%s AND cnpjloja=%s", (ml_item_id, session.get("cnpjloja")))
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
    _ensure_ml_accounts_schema()
    token = _ml_get_token()
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT DISTINCT category_id
            FROM ml_items
            WHERE cnpjloja=%s AND category_id IS NOT NULL AND category_id != '' AND status = 'active'
            ORDER BY category_id
        """, (cnpjloja,))
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
    _ensure_ml_accounts_schema()
    if not _ml_is_connected():
        return jsonify({"ok": False, "erro": "Conta ML não conectada."}), 400

    body = request.get_json(force=True) or {}
    produtos     = body.get("produtos") or []
    category_id  = (body.get("category_id") or "MLB1196").strip()
    qty_padrao   = int(body.get("quantidade_padrao") or 1)

    if not produtos:
        return jsonify({"ok": False, "erro": "Nenhum produto enviado."}), 400

    token = _ml_get_token()
    cnpjloja = session.get("cnpjloja")
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
            cur.execute("SELECT ml_item_id, status FROM ml_items WHERE ean=%s AND cnpjloja=%s", (ean, cnpjloja))
            existing = cur.fetchone()

            if existing:
                ml_item_id = existing["ml_item_id"]
                resp, code = _ml_api_put(f"/items/{ml_item_id}",
                                         {"price": preco, "available_quantity": quantidade}, token)
                if code not in (200, 201):
                    resultados.append({"ean": ean, "ok": False, "erro": f"ML {code}"})
                    continue
                cur.execute("UPDATE ml_items SET preco=%s, status='active', updated_at=NOW() WHERE ml_item_id=%s AND cnpjloja=%s",
                            (preco, ml_item_id, cnpjloja))
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
                    INSERT INTO ml_items (ml_item_id, ean, titulo, preco, category_id, cnpjloja, status, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 'active', NOW())
                    ON CONFLICT (ml_item_id) DO UPDATE
                      SET preco=%s, category_id=%s, cnpjloja=%s, status='active', updated_at=NOW()
                """, (ml_item_id, ean, titulo, preco, category_id, cnpjloja, preco, category_id, cnpjloja))
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
    "pendente":        "Pendente",
    "pago":            "Pago ✓",
    "pronto_retirada": "Pronto para retirada",
    "enviado":         "Enviado / Saiu para entrega",
    "entregue":        "Entregue ✓",
    "cancelado":       "Cancelado",
}


def _auto_pronto_retirada(pedido_id: str):
    """Para retirada paga, gera codigo; se a loja configurou, avanca para pronto_retirada."""
    try:
        conn2 = _new_conn()
        cur2  = conn2.cursor()
        cur2.execute(
            """SELECT p.tipo_entrega, p.codigo_retirada, c.todos_prontos_retirada
               FROM ecommerce_pedidos p
               LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
               WHERE p.id=%s AND p.status='pago' LIMIT 1""",
            (pedido_id,),
        )
        row = cur2.fetchone()
        if row and (row.get("tipo_entrega") or "retirada") != "entrega":
            codigo = row.get("codigo_retirada") or _novo_codigo_entrega()
            if row.get("todos_prontos_retirada"):
                cur2.execute(
                    "UPDATE ecommerce_pedidos SET status='pronto_retirada', codigo_retirada=%s, atualizado_em=NOW() WHERE id=%s",
                    (codigo, pedido_id),
                )
                status_email = "pronto_retirada"
            else:
                cur2.execute(
                    "UPDATE ecommerce_pedidos SET codigo_retirada=%s, atualizado_em=NOW() WHERE id=%s AND codigo_retirada IS NULL",
                    (codigo, pedido_id),
                )
                status_email = None
            conn2.commit()
            cur2.close()
            conn2.close()
            if status_email:
                _registrar_status_pedido(pedido_id, status_email)
                _email_status_pedido(pedido_id, status_email)
                _notificar_pedido_evento(
                    pedido_id,
                    "pedido",
                    "Pedido pronto para retirada",
                    f"Seu pedido #{str(pedido_id)[:8].upper()} esta pronto. Informe o codigo de retirada na loja.",
                )
            else:
                _notificar_pedido_evento(
                    pedido_id,
                    "pedido",
                    "Codigo de retirada gerado",
                    f"Seu pedido #{str(pedido_id)[:8].upper()} ja tem codigo de retirada disponivel.",
                )
            return True
        cur2.close()
        conn2.close()
    except Exception as exc:
        app.logger.warning("_auto_pronto_retirada error: %s", exc)
    return False


def _garantir_codigo_retirada(pedido_id: str):
    try:
        conn2 = _new_conn()
        cur2 = conn2.cursor()
        codigo = _novo_codigo_entrega()
        cur2.execute(
            """
            UPDATE ecommerce_pedidos
            SET codigo_retirada=COALESCE(codigo_retirada, %s), atualizado_em=NOW()
            WHERE id=%s AND COALESCE(tipo_entrega, 'retirada') != 'entrega'
            RETURNING codigo_retirada
            """,
            (codigo, pedido_id),
        )
        row = cur2.fetchone()
        conn2.commit()
        cur2.close()
        conn2.close()
        return (row or {}).get("codigo_retirada") if row else None
    except Exception as exc:
        app.logger.warning("_garantir_codigo_retirada error: %s", exc)
    return None


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
            SELECT p.cliente_nome, p.cliente_email, p.total, p.tipo_entrega, u.razao,
                   u.endereco2 AS endereco
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
        nome      = (row.get("cliente_nome") or "Cliente").split()[0]
        razao     = row.get("razao") or "Farmácia"
        endereco  = (row.get("endereco") or "").strip()
        total_fmt = f"R$ {float(row.get('total', 0)):.2f}".replace(".", ",")
        label     = _STATUS_LABEL.get(novo_status, novo_status.title())
        tipo_ent  = (row.get("tipo_entrega") or "retirada")

        if novo_status == "pronto_retirada":
            corpo = (
                f"<p>Olá, <b>{html.escape(nome)}</b>!</p>"
                f"<p>Seu pedido <b>#{pedido_id[:8].upper()}</b> está pronto para retirada! 🎉</p>"
                f"<div class='info-box'>"
                f"<b>Farmácia:</b> {html.escape(razao)}<br>"
                f"<b>Endereço:</b> {html.escape(endereco)}<br>"
                f"<b>Total:</b> {total_fmt}"
                f"</div>"
                f"<p>Apresente seu nome ou número do pedido ao balcão.</p>"
            )
        else:
            tipo = "Entrega" if tipo_ent == "entrega" else "Retirada na loja"
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
            f"<p>Você solicitou a redefinição de senha na Poupaqui.</p>"
            f"<p>Clique no botão abaixo para criar uma nova senha. O link expira em <b>2 horas</b>.</p>"
            f"<p><a class='btn' href='{link}'>Redefinir minha senha</a></p>"
            f"<p style='font-size:.82rem;color:#888'>Se você não solicitou isso, ignore este e-mail.</p>"
        )
        _send_email(email, "🔑 Redefinição de senha — Poupaqui", _email_html_wrapper("Redefina sua senha", corpo))
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
        f"<p>Clique abaixo para confirmar seu e-mail na Poupaqui:</p>"
        f"<p><a class='btn' href='{link}'>Confirmar meu e-mail</a></p>"
        f"<p style='font-size:.82rem;color:#888'>Se não foi você, ignore este e-mail.</p>"
    )
    _send_email(user["email"], "✉️ Confirme seu e-mail — Poupaqui", _email_html_wrapper("Confirme seu e-mail", corpo))
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


# ─── PROMOÇÕES ────────────────────────────────────────────────────────────────
# A loja nao lanca mais promocao manual — quem lanca agora e so o admin (aqui
# embaixo, /painel/admin/promocoes) ou a promocao ja vem direto do caderno de
# oferta do Alpha (preco_promocional sincronizado em alpha_sync.py). A leitura
# pra exibicao (so_assinantes, checkout etc) continua via ecommerce_promocoes
# normalmente, sem mudanca.

@app.get("/painel/admin/promocoes")
@admin_required
def admin_promocoes():
    _ensure_promo_schema()
    conn = db()
    cur  = conn.cursor()
    cnpj_filtro = (request.args.get("cnpjloja") or "").strip()
    where = "WHERE p.cnpjloja = %s" if cnpj_filtro else ""
    params = (cnpj_filtro,) if cnpj_filtro else ()
    cur.execute(f"""
        SELECT p.*, u.razao,
               (p.ativo AND (p.data_fim IS NULL OR p.data_fim > NOW())) AS vigente
        FROM ecommerce_promocoes p
        JOIN users u ON u.cnpjloja = p.cnpjloja
        {where}
        ORDER BY p.criado_em DESC
        LIMIT 300
    """, params)
    promos = cur.fetchall()
    cur.execute("SELECT cnpjloja, razao FROM users WHERE is_admin=FALSE ORDER BY razao")
    lojas = cur.fetchall()
    cur.close()
    return render_template("admin_promocoes.html", promos=promos, lojas=lojas, cnpj_filtro=cnpj_filtro)


@app.get("/painel/admin/promocoes/testar-ean")
@admin_required
def admin_promocoes_testar_ean():
    """Busca o nome de um produto por EAN no catalogo de uma loja especifica
    (Alpha, ou estoque/automatiza_estoque como fallback pra loja nao-Alpha)."""
    ean = re.sub(r"\D", "", request.args.get("ean") or "")
    cnpj = (request.args.get("cnpjloja") or "").strip()
    if not ean or not cnpj:
        return jsonify({"nome": None})
    conn = db(); cur = conn.cursor()
    cur.execute(
        "SELECT nome FROM ecommerce_alpha_produtos WHERE cnpjloja=%s AND LTRIM(COALESCE(ean,''),'0')=LTRIM(%s,'0') LIMIT 1",
        (cnpj, ean),
    )
    row = cur.fetchone()
    if not row:
        cur.execute(
            "SELECT descricao AS nome FROM estoque WHERE cnpj=%s AND LTRIM(COALESCE(barras_norm, barras,''),'0')=LTRIM(%s,'0') LIMIT 1",
            (cnpj, ean),
        )
        row = cur.fetchone()
    if not row:
        cur.execute(
            "SELECT descricao_produto AS nome FROM automatiza_estoque WHERE cnpj_loja=%s AND LTRIM(COALESCE(ean,''),'0')=LTRIM(%s,'0') LIMIT 1",
            (cnpj, ean),
        )
        row = cur.fetchone()
    cur.close()
    return jsonify({"nome": row["nome"] if row else None})


@app.post("/painel/admin/promocoes/criar")
@admin_required
def admin_promocoes_criar():
    _ensure_promo_schema()
    cnpj          = (request.form.get("cnpjloja") or "").strip()
    ean           = re.sub(r"\D", "", request.form.get("ean") or "")
    nome          = request.form.get("nome", "").strip()
    preco_promo   = request.form.get("preco_promo", "")
    data_fim      = request.form.get("data_fim", "").strip() or None
    so_assinantes = request.form.get("so_assinantes") == "1"

    if not cnpj:
        flash("Selecione a loja.", "error")
        return redirect(url_for("admin_promocoes"))

    try:
        preco_promo = float(preco_promo)
        if preco_promo <= 0:
            raise ValueError
    except (ValueError, TypeError):
        flash("Preço promocional inválido.", "error")
        return redirect(url_for("admin_promocoes"))

    if not ean:
        flash("EAN obrigatório.", "error")
        return redirect(url_for("admin_promocoes"))

    conn = db()
    cur  = conn.cursor()
    cur.execute("UPDATE ecommerce_promocoes SET ativo=FALSE WHERE cnpjloja=%s AND ean=%s", (cnpj, ean))
    cur.execute("""
        INSERT INTO ecommerce_promocoes (cnpjloja, ean, nome, preco_promo, data_fim, so_assinantes)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (cnpj, ean, nome or None, preco_promo, data_fim or None, so_assinantes))
    conn.commit()
    cur.close()
    flash("Promoção criada com sucesso!", "success")
    return redirect(url_for("admin_promocoes"))


@app.post("/painel/admin/promocoes/criar-lote")
@admin_required
def admin_promocoes_criar_lote():
    _ensure_promo_schema()
    data = request.get_json(silent=True) or {}
    cnpj = (data.get("cnpjloja") or request.form.get("cnpjloja") or "").strip()
    if not cnpj:
        return jsonify({"ok": False, "msg": "Selecione a loja."}), 400
    texto = data.get("linhas") or request.form.get("linhas") or ""
    data_fim = (data.get("data_fim") or request.form.get("data_fim") or "").strip() or None
    so_assinantes = bool(data.get("so_assinantes")) or request.form.get("so_assinantes") == "1"

    parsed = []
    for line in texto.splitlines():
        line = line.strip()
        if not line:
            continue
        if ";" in line:
            parts = [p.strip() for p in line.split(";", 2)]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t", 2)]
        else:
            m = re.match(r"^(\d{5,})\s+([0-9]+(?:[,.][0-9]+)?)(?:\s+(.+))?$", line)
            parts = [m.group(1), m.group(2), (m.group(3) or "").strip()] if m else [line]
        if len(parts) < 2:
            continue
        ean = re.sub(r"\D", "", parts[0])
        preco_raw = parts[1].replace(",", ".")
        nome = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        parsed.append((ean, preco_raw, nome))

    dedup = {}
    erros = []
    for ean, preco_raw, nome in parsed:
        try:
            preco = float(preco_raw)
            if not ean or preco <= 0:
                raise ValueError
            dedup[ean] = (ean, preco, nome)
        except Exception:
            erros.append(ean or preco_raw)
    if not dedup:
        return jsonify({"ok": False, "msg": "Nenhuma linha valida. Use EAN;preco ou EAN;preco;nome."}), 400
    if len(dedup) > 1000:
        return jsonify({"ok": False, "msg": "Envie no maximo 1000 promocoes por lote."}), 400

    eans = list(dedup.keys())
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE ecommerce_promocoes SET ativo=FALSE WHERE cnpjloja=%s AND ean = ANY(%s)", (cnpj, eans))
    rows = [(cnpj, ean, nome, preco, data_fim, so_assinantes) for ean, preco, nome in dedup.values()]
    execute_values(
        cur,
        """
        INSERT INTO ecommerce_promocoes (cnpjloja, ean, nome, preco_promo, data_fim, so_assinantes)
        VALUES %s
        """,
        rows,
    )
    conn.commit()
    cur.close()
    _batch_cache_clear()
    return jsonify({"ok": True, "criadas": len(rows), "erros": erros[:50]})


@app.post("/painel/admin/promocoes/<int:promo_id>/toggle")
@admin_required
def admin_promocoes_toggle(promo_id):
    _ensure_promo_schema()
    conn = db()
    cur  = conn.cursor()
    cur.execute("UPDATE ecommerce_promocoes SET ativo = NOT ativo WHERE id=%s", (promo_id,))
    conn.commit()
    cur.close()
    return redirect(url_for("admin_promocoes"))


@app.post("/painel/admin/promocoes/<int:promo_id>/delete")
@admin_required
def admin_promocoes_delete(promo_id):
    _ensure_promo_schema()
    conn = db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM ecommerce_promocoes WHERE id=%s", (promo_id,))
    conn.commit()
    cur.close()
    flash("Promoção removida.", "success")
    return redirect(url_for("admin_promocoes"))


@app.get("/api/promocao")
def api_promocao():
    """Retorna promoção vigente para um EAN+CNPJ. Respeita flag so_assinantes."""
    _ensure_promo_schema()
    ean  = request.args.get("ean", "").strip()
    cnpj = request.args.get("cnpj", "").strip()
    if not ean or not cnpj:
        return jsonify({"promo": None})

    conn = db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT id, preco_promo, data_fim, so_assinantes, nome
        FROM ecommerce_promocoes
        WHERE regexp_replace(COALESCE(cnpjloja,''), '\\D', '', 'g') = regexp_replace(%s, '\\D', '', 'g')
          AND LTRIM(COALESCE(ean, ''), '0') = LTRIM(%s, '0')
          AND ativo = TRUE
          AND (data_fim IS NULL OR data_fim > NOW())
        ORDER BY criado_em DESC
        LIMIT 1
    """, (cnpj, ean))
    row = cur.fetchone()
    cur.close()

    if not row:
        return jsonify({"promo": None})

    consumidor_id = session.get("consumidor_id")
    if row["so_assinantes"]:
        if not consumidor_id:
            return jsonify({"promo": None, "assinantes_only": True})
        cur2 = db().cursor()
        cur2.execute(
            """SELECT 1 FROM ecommerce_assinantes
               WHERE consumidor_id=%s
                 AND regexp_replace(COALESCE(cnpjloja,''), '\\D', '', 'g') = regexp_replace(%s, '\\D', '', 'g')
                 AND status='ativo' AND pagamento_status='aprovado'
                 AND (data_fim IS NULL OR data_fim > NOW())""",
            (consumidor_id, cnpj),
        )
        is_sub = bool(cur2.fetchone())
        cur2.close()
        if not is_sub:
            return jsonify({"promo": None, "assinantes_only": True})

    return jsonify({
        "promo": {
            "preco_promo":    float(row["preco_promo"]),
            "data_fim":       row["data_fim"].isoformat() if row["data_fim"] else None,
            "nome":           row["nome"] or "Promoção",
            "so_assinantes":  row["so_assinantes"],
        }
    })


# ─── ASSINATURAS ──────────────────────────────────────────────────────────────

@app.get("/painel/admin/assinaturas")
@admin_required
def admin_assinaturas():
    _ensure_assinatura_schema()
    conn = db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT u.cnpjloja, u.razao,
               p.id AS plano_id, p.nome, p.descricao, p.preco_mensal, p.beneficios, p.ativo,
               COALESCE(p.frete_gratis_primeira_entrega, FALSE) AS frete_gratis_primeira_entrega,
               (SELECT COUNT(*) FROM ecommerce_assinantes a
                 WHERE a.cnpjloja = u.cnpjloja AND a.status='ativo' AND a.pagamento_status='aprovado'
                   AND (a.data_fim IS NULL OR a.data_fim > NOW())) AS total_assinantes
        FROM users u
        LEFT JOIN ecommerce_planos_assinatura p ON p.cnpjloja = u.cnpjloja
        WHERE u.is_admin = FALSE
        ORDER BY u.razao
    """)
    lojas = cur.fetchall()
    cur.close()
    return render_template("admin_assinaturas.html", lojas=lojas)


@app.post("/painel/admin/assinaturas/salvar")
@admin_required
def admin_assinaturas_salvar():
    _ensure_assinatura_schema()
    cnpj = (request.form.get("cnpjloja") or "").strip()
    if not cnpj:
        flash("Loja inválida.", "error")
        return redirect(url_for("admin_assinaturas"))
    nome         = request.form.get("nome", "Clube Fidelidade").strip()
    descricao    = request.form.get("descricao", "").strip()
    preco_mensal = request.form.get("preco_mensal", "")
    beneficios   = request.form.get("beneficios", "").strip()
    ativo        = request.form.get("ativo") == "1"
    frete_gratis_primeira_entrega = request.form.get("frete_gratis_primeira_entrega") == "1"

    try:
        preco_mensal = float(preco_mensal)
        if preco_mensal < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash("Preço mensal inválido.", "error")
        return redirect(url_for("admin_assinaturas"))

    conn = db()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO ecommerce_planos_assinatura (cnpjloja, nome, descricao, preco_mensal, beneficios, ativo, frete_gratis_primeira_entrega)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (cnpjloja) DO UPDATE SET
            nome         = EXCLUDED.nome,
            descricao    = EXCLUDED.descricao,
            preco_mensal = EXCLUDED.preco_mensal,
            beneficios   = EXCLUDED.beneficios,
            ativo        = EXCLUDED.ativo,
            frete_gratis_primeira_entrega = EXCLUDED.frete_gratis_primeira_entrega
    """, (cnpj, nome, descricao or None, preco_mensal, beneficios or None, ativo, frete_gratis_primeira_entrega))
    conn.commit()
    cur.close()
    flash("Plano de assinatura salvo com sucesso!", "success")
    return redirect(url_for("admin_assinaturas"))


@app.post("/painel/admin/assinaturas/aplicar-todas")
@admin_required
def admin_assinaturas_aplicar_todas():
    """Cria/atualiza o mesmo plano de assinatura (nome, preco, beneficios) em
    todas as lojas de uma vez — cada loja continua com sua propria linha em
    ecommerce_planos_assinatura (assinante ainda assina uma loja especifica),
    só o CONTEUDO do plano fica identico em todas."""
    _ensure_assinatura_schema()
    nome         = request.form.get("nome", "Clube Fidelidade").strip()
    descricao    = request.form.get("descricao", "").strip()
    preco_mensal = request.form.get("preco_mensal", "")
    beneficios   = request.form.get("beneficios", "").strip()
    ativo        = request.form.get("ativo") == "1"
    frete_gratis_primeira_entrega = request.form.get("frete_gratis_primeira_entrega") == "1"

    try:
        preco_mensal = float(preco_mensal)
        if preco_mensal < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash("Preço mensal inválido.", "error")
        return redirect(url_for("admin_assinaturas"))

    conn = db()
    cur  = conn.cursor()
    cur.execute("SELECT cnpjloja FROM users WHERE is_admin = FALSE")
    cnpjs = [r["cnpjloja"] for r in cur.fetchall()]
    for cnpj in cnpjs:
        cur.execute("""
            INSERT INTO ecommerce_planos_assinatura (cnpjloja, nome, descricao, preco_mensal, beneficios, ativo, frete_gratis_primeira_entrega)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (cnpjloja) DO UPDATE SET
                nome         = EXCLUDED.nome,
                descricao    = EXCLUDED.descricao,
                preco_mensal = EXCLUDED.preco_mensal,
                beneficios   = EXCLUDED.beneficios,
                ativo        = EXCLUDED.ativo,
                frete_gratis_primeira_entrega = EXCLUDED.frete_gratis_primeira_entrega
        """, (cnpj, nome, descricao or None, preco_mensal, beneficios or None, ativo, frete_gratis_primeira_entrega))
    conn.commit()
    cur.close()
    flash(f"Plano aplicado em {len(cnpjs)} loja(s) com sucesso!", "success")
    return redirect(url_for("admin_assinaturas"))


# ─── FINANCEIRO (REPASSES A LOJAS) ───────────────────────────────────────────
# Beneficios que o admin banca (frete gratis da 1a entrega do assinante,
# cupons administrados pelo admin) reduzem o quanto o consumidor paga, mas a
# loja sempre recebe o valor que ela mesma configurou — a diferenca vira um
# repasse que o admin deve pra loja, registrado aqui pra conferencia/pagamento
# manual (Pix/transferencia), sem depender de split automatico no gateway.

@app.get("/painel/admin/repasses")
@admin_required
def admin_repasses():
    _ensure_repasses_admin_schema()
    status = (request.args.get("status") or "pendente").strip()
    cnpj_filtro = (request.args.get("cnpjloja") or "").strip()
    conn = db()
    cur = conn.cursor()
    where = []
    params: list = []
    if status == "pendente":
        where.append("r.pago = FALSE")
    elif status == "pago":
        where.append("r.pago = TRUE")
    if cnpj_filtro:
        where.append("r.cnpjloja = %s")
        params.append(cnpj_filtro)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    cur.execute(
        f"""
        SELECT r.*, u.razao
        FROM ecommerce_repasses_admin r
        LEFT JOIN users u ON u.cnpjloja = r.cnpjloja
        {where_sql}
        ORDER BY r.criado_em DESC
        LIMIT 500
        """,
        params,
    )
    repasses = cur.fetchall()
    cur.execute("""
        SELECT r.cnpjloja, u.razao, COUNT(*) AS qtd, SUM(r.valor) AS total
        FROM ecommerce_repasses_admin r
        LEFT JOIN users u ON u.cnpjloja = r.cnpjloja
        WHERE r.pago = FALSE
        GROUP BY r.cnpjloja, u.razao
        ORDER BY total DESC
    """)
    pendentes_por_loja = cur.fetchall()
    cur.execute("SELECT COALESCE(SUM(valor),0) AS total FROM ecommerce_repasses_admin WHERE pago=FALSE")
    total_pendente = float(cur.fetchone()["total"] or 0)
    cur.execute("SELECT cnpjloja, razao FROM users WHERE is_admin=FALSE ORDER BY razao")
    lojas = cur.fetchall()
    cur.close()
    return render_template(
        "admin_repasses.html",
        repasses=repasses,
        pendentes_por_loja=pendentes_por_loja,
        total_pendente=total_pendente,
        lojas=lojas,
        status=status,
        cnpj_filtro=cnpj_filtro,
    )


@app.post("/painel/admin/repasses/<int:repasse_id>/marcar-pago")
@admin_required
def admin_repasses_marcar_pago(repasse_id):
    _ensure_repasses_admin_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE ecommerce_repasses_admin SET pago=TRUE, pago_em=NOW() WHERE id=%s AND pago=FALSE",
        (repasse_id,),
    )
    conn.commit()
    cur.close()
    flash("Repasse marcado como pago.", "success")
    return redirect(url_for("admin_repasses"))


@app.post("/painel/admin/repasses/loja/<path:cnpjloja>/marcar-pago")
@admin_required
def admin_repasses_marcar_pago_loja(cnpjloja):
    _ensure_repasses_admin_schema()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE ecommerce_repasses_admin SET pago=TRUE, pago_em=NOW() WHERE cnpjloja=%s AND pago=FALSE",
        (cnpjloja,),
    )
    qtd = cur.rowcount
    conn.commit()
    cur.close()
    flash(f"{qtd} repasse(s) da loja marcados como pago.", "success")
    return redirect(url_for("admin_repasses"))


@app.get("/seja-assinante")
def seja_assinante():
    return render_template("seja_assinante.html")


@app.get("/api/planos-assinatura-proximos")
def api_planos_assinatura_proximos():
    """Lista todas as farmacias com plano de assinatura ativo, ordenadas pela
    distancia do consumidor (mesma logica de /api/vitnatu-produtos)."""
    try:
        lat_usr = float(request.args.get("lat", 0))
        lng_usr = float(request.args.get("lng", 0))
    except (ValueError, TypeError):
        lat_usr, lng_usr = 0.0, 0.0
    sem_loc = (lat_usr == 0.0 and lng_usr == 0.0)

    _ensure_assinatura_schema()
    _ensure_logo_url_column()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.cnpjloja, p.nome AS plano_nome, p.descricao, p.preco_mensal, p.beneficios,
               COALESCE(p.frete_gratis_primeira_entrega, FALSE) AS frete_gratis_primeira_entrega,
               u.razao, u.endereco, u.endereco2, u.uf, g.lat, g.lng, c.logo_url
        FROM ecommerce_planos_assinatura p
        JOIN users u ON u.cnpjloja = p.cnpjloja
        LEFT JOIN ecommerce_lojas_geo g ON g.cnpjloja = p.cnpjloja
        LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = p.cnpjloja
        WHERE p.ativo = TRUE AND u.is_admin = FALSE
          AND COALESCE(c.catalogo_publico, TRUE) = TRUE
    """)
    rows = [dict(r) for r in cur.fetchall()]

    for item in rows:
        item["razao"] = _public_store_name(item)
        lat_l, lng_l = None, None
        if not sem_loc:
            lat_l, lng_l = _geo_override(item.get("endereco"), item.get("endereco2"), item.get("uf"))
            if not lat_l and item.get("lat") is not None and item.get("lng") is not None:
                lat_l, lng_l = float(item["lat"]), float(item["lng"])
        item["distancia_km"] = round(haversine(lat_usr, lng_usr, lat_l, lng_l), 1) if lat_l else None
        item.pop("lat", None); item.pop("lng", None)
        item.pop("endereco2", None)

    if sem_loc:
        rows.sort(key=lambda x: x["razao"] or "")
    else:
        rows.sort(key=lambda x: (x["distancia_km"] is None, x["distancia_km"] if x["distancia_km"] is not None else 0))

    consumidor_id = str(session.get("consumidor_id") or "")
    assinados = set()
    if consumidor_id and rows:
        cnpjs = [r["cnpjloja"] for r in rows]
        placeholders = ",".join(["%s"] * len(cnpjs))
        cur.execute(
            f"""SELECT cnpjloja FROM ecommerce_assinantes
                WHERE consumidor_id=%s AND cnpjloja IN ({placeholders})
                  AND status='ativo' AND pagamento_status='aprovado'
                  AND (data_fim IS NULL OR data_fim > NOW())""",
            [consumidor_id] + cnpjs,
        )
        assinados = {r["cnpjloja"] for r in cur.fetchall()}
    cur.close()

    for item in rows:
        item["ja_assina"] = item["cnpjloja"] in assinados

    return jsonify({"planos": rows})


@app.get("/minhas-assinaturas")
def minhas_assinaturas():
    _ensure_assinatura_schema()
    _ensure_logo_url_column()
    consumidor_id = session.get("consumidor_id")
    if not consumidor_id:
        return redirect(url_for("consumidor_login"))
    conn = db()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE ecommerce_assinantes
           SET status='vencido'
         WHERE consumidor_id=%s
           AND status='ativo'
           AND data_fim IS NOT NULL
           AND data_fim <= NOW()
    """, (str(consumidor_id),))
    conn.commit()
    cur.execute("""
        SELECT a.id, a.status, a.data_inicio, a.data_fim, a.cnpjloja, a.pagamento_status, a.criado_em,
               p.nome AS plano_nome, p.descricao, p.preco_mensal, p.beneficios,
               u.razao, u.endereco, c.logo_url
        FROM ecommerce_assinantes a
        JOIN ecommerce_planos_assinatura p ON p.id = a.plano_id
        JOIN users u ON u.cnpjloja = a.cnpjloja
        LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = a.cnpjloja
        WHERE a.consumidor_id = %s
        ORDER BY a.criado_em DESC
    """, (str(consumidor_id),))
    assinaturas = [dict(a) for a in cur.fetchall()]
    for a in assinaturas:
        a["razao"] = _public_store_name(a)
    ativas = [a for a in assinaturas if a["status"] == "ativo" and a["pagamento_status"] == "aprovado"]
    investindo_mes = sum(float(a.get("preco_mensal") or 0) for a in ativas)

    # Economia estimada: soma real dos beneficios financiados pelo admin (frete
    # gratis da 1a entrega + diferenca de preco so_assinantes) aplicados nos
    # pedidos reais deste consumidor — nao e um numero inventado.
    economia_estimada = 0.0
    try:
        _ensure_repasses_admin_schema()
        cur.execute("""
            SELECT COALESCE(SUM(r.valor), 0) AS economia
            FROM ecommerce_repasses_admin r
            JOIN ecommerce_pedidos p ON p.id = r.pedido_id
            WHERE p.consumidor_id = %s AND r.tipo IN ('promocao', 'frete_primeira_entrega')
        """, (str(consumidor_id),))
        economia_estimada = float((cur.fetchone() or {}).get("economia") or 0)
    except Exception:
        economia_estimada = 0.0
    cur.close()
    consumidor = _consumidor_from_session()
    return render_template(
        "consumidor_assinaturas.html", assinaturas=assinaturas, consumidor=consumidor,
        n_ativas=len(ativas), investindo_mes=investindo_mes, economia_estimada=economia_estimada,
    )


@app.post("/assinar/<cnpjloja>")
def assinar_loja(cnpjloja):
    _ensure_assinatura_schema()
    _ensure_logo_url_column()
    consumidor_id = str(session.get("consumidor_id") or "")
    if not consumidor_id:
        return redirect(url_for("consumidor_login"))

    conn = db()
    cur  = conn.cursor()

    # Busca plano (pagamento de assinatura usa a conta MP central do admin,
    # nao a config da loja — ver _admin_mp_config)
    cur.execute("""
        SELECT p.id AS plano_id, p.nome AS plano_nome, p.preco_mensal, u.razao, c.logo_url
        FROM ecommerce_planos_assinatura p
        JOIN users u ON u.cnpjloja = %s
        LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = u.cnpjloja
        WHERE p.cnpjloja = %s AND p.ativo = TRUE
        LIMIT 1
    """, (cnpjloja, cnpjloja))
    plano = cur.fetchone()
    if not plano:
        flash("Esta farmácia não tem plano de assinatura ativo no momento.", "error")
        cur.close()
        return redirect(request.referrer or url_for("index"))

    preco = float(plano["preco_mensal"] or 0)

    # Cria/atualiza assinatura em status aguardando_pagamento
    cur.execute("""
        INSERT INTO ecommerce_assinantes (consumidor_id, cnpjloja, plano_id, status, pagamento_status)
        VALUES (%s, %s, %s, 'aguardando_pagamento', 'pendente')
        ON CONFLICT (consumidor_id, cnpjloja)
        DO UPDATE SET status='aguardando_pagamento', pagamento_status='pendente',
                  plano_id=EXCLUDED.plano_id, mp_payment_id=NULL, mp_preference_id=NULL,
                  mp_init_point=NULL, mp_preapproval_id=NULL, mp_preapproval_init_point=NULL,
                  assinatura_recorrente=FALSE
        RETURNING id
    """, (consumidor_id, cnpjloja, plano["plano_id"]))
    assinatura_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()

    # Plano gratuito: ativa direto
    if preco <= 0:
        conn2 = db(); cur2 = conn2.cursor()
        _ativar_assinatura_row(cur2, assinatura_id, recorrente=False)
        conn2.commit(); cur2.close()
        flash("Assinatura ativada! Agora você tem acesso a benefícios exclusivos.", "success")
        return redirect(url_for("minhas_assinaturas"))

    # Toda assinatura paga usa a conta Mercado Pago central do admin
    admin_cfg = _admin_mp_config()
    mp_token = admin_cfg.get("mp_access_token") or ""
    if mp_token:
        cliente = _consumidor_from_session() or {}
        item = [{"nome": f"Assinatura {plano['plano_nome']} — {plano['razao']}", "qty": 1, "preco": preco}]
        ext_ref = f"assinatura:{assinatura_id}"
        recorrencia = _criar_assinatura_recorrente_mp(mp_token, assinatura_id, plano, cliente)
        assinatura_recorrente_link = recorrencia.get("init_point") if not recorrencia.get("_erro") else None
        if assinatura_recorrente_link:
            connr = db(); curr = connr.cursor()
            curr.execute(
                "UPDATE ecommerce_assinantes SET mp_preapproval_id=%s, mp_preapproval_init_point=%s WHERE id=%s",
                (str(recorrencia.get("id") or ""), assinatura_recorrente_link, assinatura_id),
            )
            connr.commit(); curr.close()

        cartao_link = None

        # Pix via MP (preferência para Pix)
        try:
            payload = {
                "transaction_amount": round(preco, 2),
                "description": f"Assinatura {plano['plano_nome']} — {plano['razao']}",
                "payment_method_id": "pix",
                "external_reference": ext_ref,
                "payer": _payer_payload(cliente),
            }
            notif = _mp_notification_url()
            if notif:
                payload["notification_url"] = notif
            data = _mp_request(mp_token, "/v1/payments", payload, idempotency_key=f"assin-{assinatura_id}-pix")
            tx = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
            qr  = tx.get("qr_code")
            qr64 = tx.get("qr_code_base64")
            mp_pid = str(data.get("id") or "")
            if mp_pid and qr:
                conn3 = db(); cur3 = conn3.cursor()
                cur3.execute(
                    "UPDATE ecommerce_assinantes SET mp_payment_id=%s WHERE id=%s",
                    (mp_pid, assinatura_id),
                )
                conn3.commit(); cur3.close()
                session["assinatura_pix"] = {
                    "assinatura_id": assinatura_id, "cnpjloja": cnpjloja,
                    "qr_code": qr, "qr_code_base64": qr64,
                    "mp_payment_id": mp_pid,
                    "mp_public_key": admin_cfg.get("mp_public_key") or "",
                    "mp_preapproval_id": str(recorrencia.get("id") or "") if assinatura_recorrente_link else "",
                    "assinatura_recorrente_link": assinatura_recorrente_link,
                    "cartao_link": None,
                    "plano_nome": plano["plano_nome"], "razao": plano["razao"], "logo_url": plano.get("logo_url"),
                    "preco": preco,
                }
        except Exception:
            pass

        # Link para cartão avulso (um mês)
        try:
            pref_payload = {
                "items": [{"title": f"Assinatura {plano['plano_nome']}", "quantity": 1,
                           "unit_price": preco, "currency_id": "BRL"}],
                "external_reference": ext_ref,
                "payer": _payer_payload(cliente),
            }
            base_url = _public_base_url()
            if base_url:
                pref_payload["back_urls"] = {
                    "success": f"{base_url}/minhas-assinaturas",
                    "failure": f"{base_url}/assinatura/pagamento/{cnpjloja}",
                    "pending": f"{base_url}/assinatura/pagamento/{cnpjloja}",
                }
                pref_payload["auto_return"] = "approved"
            notif = _mp_notification_url()
            if notif:
                pref_payload["notification_url"] = notif
            pref = _mp_request(mp_token, "/checkout/preferences", pref_payload,
                               idempotency_key=f"assin-{assinatura_id}-pref")
            cartao_link = pref.get("init_point") or pref.get("sandbox_init_point")
            if cartao_link:
                conn4 = db(); cur4 = conn4.cursor()
                cur4.execute(
                    "UPDATE ecommerce_assinantes SET mp_preference_id=%s, mp_init_point=%s WHERE id=%s",
                    (str(pref.get("id") or ""), cartao_link, assinatura_id),
                )
                conn4.commit(); cur4.close()
        except Exception:
            pass

        info = session.get("assinatura_pix") or {
            "assinatura_id": assinatura_id, "cnpjloja": cnpjloja,
            "qr_code": None, "qr_code_base64": None, "mp_payment_id": None,
            "plano_nome": plano["plano_nome"], "razao": plano["razao"], "logo_url": plano.get("logo_url"),
            "preco": preco,
        }
        info.update({
            "mp_preapproval_id": str(recorrencia.get("id") or "") if assinatura_recorrente_link else "",
            "mp_public_key": admin_cfg.get("mp_public_key") or "",
            "assinatura_recorrente_link": assinatura_recorrente_link,
            "cartao_link": cartao_link,
        })
        session["assinatura_pix"] = info
        return redirect(url_for("assinatura_pagamento", cnpjloja=cnpjloja))

    # Sem token MP do admin configurado: assinatura paga fica indisponivel
    # (nao ha mais fallback de Pix manual/gateway alternativo por loja)
    flash("Pagamento de assinatura temporariamente indisponível. Tente novamente mais tarde.", "error")
    return redirect(request.referrer or url_for("index"))


@app.get("/assinatura/pagamento/<cnpjloja>")
def assinatura_pagamento(cnpjloja):
    consumidor_id = str(session.get("consumidor_id") or "")
    if not consumidor_id:
        return redirect(url_for("consumidor_login"))

    info = session.get("assinatura_pix") or {}
    if not info or info.get("cnpjloja") != cnpjloja:
        # Reconstrói info do banco (caso sessão tenha expirado)
        _ensure_assinatura_schema()
        _ensure_cartoes_schema()
        _ensure_logo_url_column()
        conn = db(); cur = conn.cursor()
        cur.execute("""
            SELECT a.id, a.mp_payment_id, a.mp_preapproval_id, a.mp_preapproval_init_point,
                   a.mp_init_point, a.status, a.pagamento_status, a.data_fim,
                   p.nome AS plano_nome, p.preco_mensal,
                   u.razao, u.endereco, c.logo_url
            FROM ecommerce_assinantes a
            JOIN ecommerce_planos_assinatura p ON p.id = a.plano_id
            JOIN users u ON u.cnpjloja = a.cnpjloja
            LEFT JOIN ecommerce_config_loja c ON c.cnpjloja = a.cnpjloja
            WHERE a.consumidor_id = %s AND a.cnpjloja = %s
            LIMIT 1
        """, (consumidor_id, cnpjloja))
        row = cur.fetchone()
        if not row or _assinatura_vigente_row(row):
            cur.close()
            return redirect(url_for("minhas_assinaturas"))
        qr_code = None
        qr_code_base64 = None
        mp_pid = row.get("mp_payment_id")
        mp_customer_id = None
        cur.execute(
            "SELECT mp_customer_id FROM ecommerce_mp_clientes WHERE consumidor_id=%s AND cnpjloja=%s LIMIT 1",
            (consumidor_id, cnpjloja)
        )
        mp_row = cur.fetchone()
        if mp_row:
            mp_customer_id = mp_row["mp_customer_id"]
        admin_cfg = _admin_mp_config()
        # Tenta re-buscar QR code do MP se o pagamento ainda existir
        if mp_pid and admin_cfg.get("mp_access_token"):
            try:
                pdata = _mp_request(admin_cfg["mp_access_token"], f"/v1/payments/{mp_pid}", method="GET")
                tx = (pdata.get("point_of_interaction") or {}).get("transaction_data") or {}
                qr_code = tx.get("qr_code")
                qr_code_base64 = tx.get("qr_code_base64")
            except Exception:
                pass
        info = {
            "assinatura_id": row["id"],
            "cnpjloja": cnpjloja,
            "qr_code": qr_code,
            "qr_code_base64": qr_code_base64,
            "mp_payment_id": mp_pid,
            "mp_public_key": admin_cfg.get("mp_public_key") or "",
            "mp_customer_id": mp_customer_id or "",
            "mp_preapproval_id": row.get("mp_preapproval_id") or "",
            "assinatura_recorrente_link": row.get("mp_preapproval_init_point") or "",
            "cartao_link": row.get("mp_init_point") or "",
            "plano_nome": row["plano_nome"],
            "razao": _public_store_name(row),
            "logo_url": row.get("logo_url"),
            "preco": float(row["preco_mensal"] or 0),
        }
        cur.close()

    if not info.get("mp_customer_id"):
        try:
            _ensure_cartoes_schema()
            conn_mc = db(); cur_mc = conn_mc.cursor()
            cur_mc.execute(
                "SELECT mp_customer_id FROM ecommerce_mp_clientes WHERE consumidor_id=%s AND cnpjloja=%s LIMIT 1",
                (consumidor_id, cnpjloja),
            )
            mc_row = cur_mc.fetchone()
            cur_mc.close()
            info["mp_customer_id"] = mc_row["mp_customer_id"] if mc_row else ""
        except Exception:
            info["mp_customer_id"] = ""

    consumidor = _consumidor_from_session()
    return render_template("consumidor_assinatura_pagamento.html", info=info, consumidor=consumidor)


@app.post("/assinatura/pagamento/<cnpjloja>/cartao-transparente")
def assinatura_cartao_transparente(cnpjloja):
    _ensure_assinatura_schema()
    _ensure_cartoes_schema()
    consumidor_id = str(session.get("consumidor_id") or "")
    if not consumidor_id:
        return jsonify({"error": "Faça login para assinar."}), 401
    data = request.get_json(force=True) or {}
    card_token = (data.get("token") or "").strip()
    if not card_token:
        return jsonify({"error": "Token do cartão não recebido."}), 400
    payer = data.get("payer") or {}
    identification = payer.get("identification") or {}
    doc_number = _digits(identification.get("number"))
    doc_type = (identification.get("type") or ("CNPJ" if len(doc_number) == 14 else "CPF")).upper()
    if doc_type not in {"CPF", "CNPJ"} or len(doc_number) not in {11, 14}:
        return jsonify({"error": "Informe CPF ou CNPJ valido do pagador."}), 400
    admin_token = _admin_mp_config().get("mp_access_token") or ""
    if not admin_token:
        return jsonify({"error": "Pagamento de assinatura indisponível no momento."}), 400
    conn = db(); cur = conn.cursor()
    cur.execute(
        """
        SELECT a.id, a.status, p.nome AS plano_nome, p.preco_mensal, u.razao
        FROM ecommerce_assinantes a
        JOIN ecommerce_planos_assinatura p ON p.id = a.plano_id
        JOIN users u ON u.cnpjloja = a.cnpjloja
        WHERE a.consumidor_id=%s AND a.cnpjloja=%s
        LIMIT 1
        """,
        (consumidor_id, cnpjloja),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        return jsonify({"error": "Assinatura não encontrada."}), 404
    cliente = _consumidor_from_session() or {}
    plano = {
        "plano_nome": row["plano_nome"],
        "preco_mensal": float(row["preco_mensal"] or 0),
        "razao": row["razao"],
    }
    _obter_ou_criar_mp_customer(admin_token, consumidor_id, cnpjloja, cliente)
    pre = _criar_assinatura_recorrente_cartao_mp(admin_token, row["id"], plano, cliente, card_token)
    erro = pre.get("_erro") if isinstance(pre, dict) else None
    if erro:
        cur.close()
        return jsonify({"error": erro}), 400
    pre_id = str(pre.get("id") or "")
    cur.execute(
        "UPDATE ecommerce_assinantes SET mp_preapproval_id=%s, mp_preapproval_init_point=NULL WHERE id=%s",
        (pre_id, row["id"]),
    )
    status = (pre.get("status") or "").lower()
    if status in {"authorized", "active"}:
        _ativar_assinatura_row(cur, row["id"], recorrente=True)
    conn.commit()
    cur.close()
    return jsonify({
        "status": "ativo" if status in {"authorized", "active"} else status or "pending",
        "preapproval_id": pre_id,
    })


@app.post("/assinatura/pagamento/<cnpjloja>/asaas-cartao")
def assinatura_asaas_cartao(cnpjloja):
    # Assinatura agora só é cobrada via Mercado Pago (conta central do admin) —
    # Asaas por loja não é mais uma opção de pagamento de assinatura.
    return jsonify({"error": "Pagamento de assinatura via Asaas não está mais disponível. Use Mercado Pago."}), 400


@app.get("/assinatura/pagamento/<cnpjloja>/status")
def assinatura_pagamento_status(cnpjloja):
    """Polling do status do pagamento de assinatura."""
    _ensure_assinatura_schema()
    consumidor_id = str(session.get("consumidor_id") or "")
    if not consumidor_id:
        return jsonify({"status": "nao_logado"})
    conn = db(); cur = conn.cursor()
    cur.execute(
        "SELECT id, status, pagamento_status, mp_payment_id, mp_preapproval_id, data_fim FROM ecommerce_assinantes WHERE consumidor_id=%s AND cnpjloja=%s LIMIT 1",
        (consumidor_id, cnpjloja),
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return jsonify({"status": "nao_encontrado"})

    # Se já ativo, retorna
    if _assinatura_vigente_row(row):
        return jsonify({"status": "ativo"})
    if row["status"] == "ativo" and row.get("data_fim") is not None:
        conn_exp = db(); cur_exp = conn_exp.cursor()
        cur_exp.execute("UPDATE ecommerce_assinantes SET status='vencido' WHERE id=%s", (row["id"],))
        conn_exp.commit(); cur_exp.close()
        return jsonify({"status": "vencido", "pagamento": row["pagamento_status"]})

    # Tenta sincronizar com MP se tiver payment_id
    mp_pid = row.get("mp_payment_id")
    preapproval_id = row.get("mp_preapproval_id")
    if preapproval_id:
        status_pre = _sincronizar_assinatura_preapproval(str(preapproval_id))
        if status_pre in {"authorized", "active"}:
            return jsonify({"status": "ativo"})
        if status_pre in {"cancelled", "paused"}:
            return jsonify({"status": "cancelado", "pagamento": row["pagamento_status"]})
    if mp_pid:
        try:
            admin_token = _admin_mp_config().get("mp_access_token") or ""
            if admin_token:
                data = _mp_request(admin_token, f"/v1/payments/{mp_pid}", method="GET")
                mp_status = (data.get("status") or "").lower()
                if mp_status == "approved":
                    conn3 = db(); cur3 = conn3.cursor()
                    cur3.execute(
                        "UPDATE ecommerce_assinantes SET status='ativo', pagamento_status='aprovado', data_inicio=COALESCE(data_inicio, NOW()), data_fim=NOW() + INTERVAL '30 days' WHERE id=%s",
                        (row["id"],),
                    )
                    conn3.commit(); cur3.close()
                    return jsonify({"status": "ativo"})
                if mp_status in ("rejected", "cancelled"):
                    return jsonify({"status": "rejeitado"})
        except Exception:
            pass

    return jsonify({"status": row["status"], "pagamento": row["pagamento_status"]})


@app.post("/cancelar-assinatura/<cnpjloja>")
def cancelar_assinatura(cnpjloja):
    _ensure_assinatura_schema()
    consumidor_id = session.get("consumidor_id")
    if not consumidor_id:
        return redirect(url_for("consumidor_login"))

    conn = db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT mp_preapproval_id FROM ecommerce_assinantes WHERE consumidor_id=%s AND cnpjloja=%s LIMIT 1",
        (consumidor_id, cnpjloja),
    )
    row = cur.fetchone()
    admin_token = _admin_mp_config().get("mp_access_token") or ""
    if row and row.get("mp_preapproval_id") and admin_token:
        try:
            _mp_request(
                admin_token,
                f"/preapproval/{row['mp_preapproval_id']}",
                {"status": "cancelled"},
                method="PUT",
            )
        except Exception:
            pass
    cur.execute(
        "UPDATE ecommerce_assinantes SET status='cancelado', data_fim=NOW() WHERE consumidor_id=%s AND cnpjloja=%s",
        (consumidor_id, cnpjloja),
    )
    conn.commit()
    cur.close()
    flash("Assinatura cancelada com sucesso.", "info")
    return redirect(url_for("minhas_assinaturas"))


@app.get("/api/assinatura-status")
def api_assinatura_status():
    """Retorna se consumidor logado é assinante de uma loja."""
    _ensure_assinatura_schema()
    consumidor_id = session.get("consumidor_id")
    cnpj          = request.args.get("cnpj", "").strip()
    if not consumidor_id or not cnpj:
        return jsonify({"assinante": False})
    conn = db()
    cur  = conn.cursor()
    cur.execute(
        """SELECT 1 FROM ecommerce_assinantes
           WHERE consumidor_id=%s AND cnpjloja=%s
             AND status='ativo' AND pagamento_status='aprovado'
             AND (data_fim IS NULL OR data_fim > NOW())""",
        (consumidor_id, cnpj),
    )
    assinante = bool(cur.fetchone())
    cur.execute(
        "SELECT id, nome, preco_mensal, beneficios, ativo FROM ecommerce_planos_assinatura WHERE cnpjloja=%s LIMIT 1",
        (cnpj,),
    )
    plano = cur.fetchone()
    cur.close()
    return jsonify({
        "assinante": assinante,
        "plano": {
            "nome":        plano["nome"],
            "preco_mensal": float(plano["preco_mensal"]),
            "beneficios":  plano["beneficios"] or "",
            "ativo":       plano["ativo"],
        } if plano else None,
    })


# ── Horário de funcionamento ──────────────────────────────────────────────────

_DIAS_SEMANA = [
    (0, "Domingo"),
    (1, "Segunda-feira"),
    (2, "Terça-feira"),
    (3, "Quarta-feira"),
    (4, "Quinta-feira"),
    (5, "Sexta-feira"),
    (6, "Sábado"),
]

# Dia da semana Brasil: 0=Dom, 1=Seg, ..., 6=Sab
# Python weekday(): 0=Mon...6=Sun → mapeamento: (weekday + 1) % 7
def _dia_semana_br():
    tz_br = timezone(timedelta(hours=-3))
    return (datetime.now(tz_br).weekday() + 1) % 7


def _hora_atual_br():
    tz_br = timezone(timedelta(hours=-3))
    return datetime.now(tz_br).time()


def _data_hoje_br():
    tz_br = timezone(timedelta(hours=-3))
    return datetime.now(tz_br).date()


def _time_from_db(val):
    """psycopg2 pode devolver TIME como timedelta em algumas versões/setups."""
    if val is None:
        return None
    if hasattr(val, "seconds") and not hasattr(val, "hour"):
        return (datetime.min + val).time()
    return val


def _feriados_periodo(cnpjloja, data_inicio, data_fim):
    """Feriados cadastrados pela loja no intervalo [data_inicio, data_fim], por data."""
    conn = db(); cur = conn.cursor()
    cur.execute(
        """SELECT data, descricao, fechado, hora_abertura, hora_fechamento
           FROM ecommerce_config_feriado
           WHERE cnpjloja=%s AND data BETWEEN %s AND %s""",
        (cnpjloja, data_inicio, data_fim),
    )
    rows = {r["data"]: dict(r) for r in cur.fetchall()}
    cur.close()
    return rows


def _proximo_dia_abertura(cnpjloja: str, horarios: list, dia_hoje: int, data_hoje) -> dict | None:
    """Retorna o próximo dia aberto a partir de amanhã (até 14 dias à frente),
    pulando datas marcadas como feriado fechado."""
    feriados = _feriados_periodo(cnpjloja, data_hoje + timedelta(days=1), data_hoje + timedelta(days=14))
    for delta in range(1, 15):
        data = data_hoje + timedelta(days=delta)
        dia = (dia_hoje + delta) % 7
        nome_dia = next((n for d, n in _DIAS_SEMANA if d == dia), str(dia))
        feriado = feriados.get(data)
        if feriado:
            if feriado.get("fechado"):
                continue
            ab = _time_from_db(feriado.get("hora_abertura"))
            if ab:
                return {"dia": dia, "nome_dia": nome_dia, "hora_abertura": str(ab)[:5], "data": data.isoformat()}
            continue
        h = next((h for h in horarios if h["dia_semana"] == dia), None)
        if h and not h.get("fechado") and h.get("hora_abertura"):
            return {"dia": dia, "nome_dia": nome_dia, "hora_abertura": str(_time_from_db(h["hora_abertura"]))[:5], "data": data.isoformat()}
    return None


def _proximo_dia_entrega_disponivel(cnpjloja: str, horarios: list, dia_hoje: int, data_hoje) -> dict | None:
    """Retorna o próximo dia (a partir de amanhã, até 14 dias à frente) em que
    a ENTREGA especificamente estará disponível — pula feriados fechados e
    dias com entrega_habilitada=False, mesmo que a loja abra normalmente
    nesse dia só para retirada."""
    feriados = _feriados_periodo(cnpjloja, data_hoje + timedelta(days=1), data_hoje + timedelta(days=14))
    for delta in range(1, 15):
        data = data_hoje + timedelta(days=delta)
        dia = (dia_hoje + delta) % 7
        nome_dia = next((n for d, n in _DIAS_SEMANA if d == dia), str(dia))
        feriado = feriados.get(data)
        h = next((h for h in horarios if h["dia_semana"] == dia), None)
        if feriado and feriado.get("fechado"):
            continue
        if feriado and not feriado.get("fechado"):
            ab = _time_from_db(feriado.get("hora_abertura")) or (_time_from_db(h["hora_abertura"]) if h else None)
            if ab and (not h or h.get("entrega_habilitada") is not False):
                return {"dia": dia, "nome_dia": nome_dia, "hora_abertura": str(ab)[:5], "data": data.isoformat()}
            continue
        if not h or h.get("fechado") or not h.get("hora_abertura"):
            continue
        entrega_habilitada = True if h.get("entrega_habilitada") is None else bool(h.get("entrega_habilitada"))
        if not entrega_habilitada:
            continue
        e_ab = _time_from_db(h.get("entrega_hora_abertura")) or _time_from_db(h["hora_abertura"])
        return {"dia": dia, "nome_dia": nome_dia, "hora_abertura": str(e_ab)[:5], "data": data.isoformat()}
    return None


def _loja_permite_agendamento_entrega(cnpjloja: str) -> bool:
    """Loja precisa marcar explicitamente que quer oferecer 'agendar entrega
    para outro dia' — por padrão vem desligado."""
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT permite_agendamento_entrega FROM ecommerce_config_loja WHERE cnpjloja=%s", (cnpjloja,))
    row = cur.fetchone()
    cur.close()
    return bool(row and row.get("permite_agendamento_entrega"))


def _entrega_disponivel_na_data(cnpjloja: str, data) -> bool:
    """Verifica se a entrega estaria disponível numa data futura específica —
    usado para revalidar no servidor um pedido de agendamento de entrega
    vindo do carrinho (não confia só no que o cliente mandou)."""
    _ensure_horario_schema()
    conn = db(); cur = conn.cursor()
    cur.execute(
        """SELECT dia_semana, hora_abertura, fechado, entrega_habilitada
           FROM ecommerce_config_horario WHERE cnpjloja=%s""",
        (cnpjloja,),
    )
    horarios = {r["dia_semana"]: r for r in cur.fetchall()}
    cur.close()
    if not horarios:
        return True  # sem configuração de horário = sem restrição
    feriado = _feriados_periodo(cnpjloja, data, data).get(data)
    if feriado:
        return not feriado.get("fechado")
    dia = (data.weekday() + 1) % 7  # mesmo mapeamento de _dia_semana_br
    h = horarios.get(dia)
    if not h or h.get("fechado") or not h.get("hora_abertura"):
        return False
    entrega_habilitada = h.get("entrega_habilitada")
    return True if entrega_habilitada is None else bool(entrega_habilitada)


def _status_horario_entrega(cnpjloja):
    """Status de funcionamento E de entrega da loja agora, considerando
    horário semanal + feriado do dia + horário específico de entrega.
    Usado no endpoint público de status e em toda checagem de elegibilidade
    de entrega (catálogo e checkout) — uma loja aberta não implica entrega
    disponível no mesmo horário."""
    _ensure_horario_schema()
    conn = db(); cur = conn.cursor()
    cur.execute(
        """SELECT dia_semana, hora_abertura, hora_fechamento, fechado,
                  entrega_habilitada, entrega_hora_abertura, entrega_hora_fechamento
           FROM ecommerce_config_horario WHERE cnpjloja=%s ORDER BY dia_semana""",
        (cnpjloja,),
    )
    horarios = [dict(r) for r in cur.fetchall()]
    cur.close()

    if not horarios:
        return {"aberta": True, "configurado": False, "entrega_disponivel_horario": True}

    data_hoje = _data_hoje_br()
    dia_hoje = _dia_semana_br()
    hora_agora = _hora_atual_br()

    h_hoje = next((h for h in horarios if h["dia_semana"] == dia_hoje), None)
    feriado_hoje = _feriados_periodo(cnpjloja, data_hoje, data_hoje).get(data_hoje)

    if feriado_hoje:
        fechado_hoje = bool(feriado_hoje.get("fechado"))
        ab = _time_from_db(feriado_hoje.get("hora_abertura")) or (_time_from_db(h_hoje["hora_abertura"]) if h_hoje else None)
        fech = _time_from_db(feriado_hoje.get("hora_fechamento")) or (_time_from_db(h_hoje["hora_fechamento"]) if h_hoje else None)
    else:
        fechado_hoje = bool(h_hoje.get("fechado")) if h_hoje else True
        ab = _time_from_db(h_hoje.get("hora_abertura")) if h_hoje else None
        fech = _time_from_db(h_hoje.get("hora_fechamento")) if h_hoje else None

    aberta = bool(not fechado_hoje and ab and fech and ab <= hora_agora <= fech)

    entrega_disponivel_horario = False
    if aberta and h_hoje and not feriado_hoje:
        entrega_habilitada_hoje = h_hoje.get("entrega_habilitada")
        entrega_habilitada_hoje = True if entrega_habilitada_hoje is None else bool(entrega_habilitada_hoje)
        if entrega_habilitada_hoje:
            e_ab = _time_from_db(h_hoje.get("entrega_hora_abertura")) or ab
            e_fech = _time_from_db(h_hoje.get("entrega_hora_fechamento")) or fech
            entrega_disponivel_horario = bool(e_ab and e_fech and e_ab <= hora_agora <= e_fech)
    elif aberta and feriado_hoje:
        # Aberta num feriado com horário especial: entrega segue o mesmo horário especial
        entrega_disponivel_horario = True

    nome_dia_hoje = next((n for d, n in _DIAS_SEMANA if d == dia_hoje), "hoje")

    result = {
        "aberta": aberta,
        "configurado": True,
        "entrega_disponivel_horario": entrega_disponivel_horario,
        "hora_abertura": str(ab)[:5] if ab else None,
        "hora_fechamento": str(fech)[:5] if fech else None,
        "feriado_hoje": feriado_hoje.get("descricao") if feriado_hoje else None,
    }
    if not aberta:
        if not fechado_hoje and ab and hora_agora < ab:
            # Loja abre hoje ainda (so nao chegou a hora) - o "proximo" e hoje
            # mesmo, nao o proximo dia da semana que viria só daqui 7 dias.
            proximo = {"dia": dia_hoje, "nome_dia": nome_dia_hoje, "hora_abertura": str(ab)[:5], "data": data_hoje.isoformat(), "hoje": True}
        else:
            proximo = _proximo_dia_abertura(cnpjloja, horarios, dia_hoje, data_hoje)
        result.update({
            "fechado_hoje": True,
            "nome_dia_hoje": nome_dia_hoje,
            "proximo": proximo,
        })
    if not entrega_disponivel_horario and _loja_permite_agendamento_entrega(cnpjloja):
        # Mesma logica do "abre hoje mais tarde": se a entrega de hoje ainda
        # nao comecou (loja/entrega abrem depois), o "proximo dia de entrega"
        # e hoje mesmo, nao o proximo dia da semana daqui a 7 dias.
        proximo_entrega_hoje = None
        if feriado_hoje:
            if not feriado_hoje.get("fechado"):
                fer_ab = _time_from_db(feriado_hoje.get("hora_abertura")) or (_time_from_db(h_hoje["hora_abertura"]) if h_hoje else None)
                entrega_habilitada_fer = (not h_hoje) or (h_hoje.get("entrega_habilitada") is not False)
                if fer_ab and entrega_habilitada_fer and hora_agora < fer_ab:
                    proximo_entrega_hoje = {"dia": dia_hoje, "nome_dia": nome_dia_hoje, "hora_abertura": str(fer_ab)[:5], "data": data_hoje.isoformat(), "hoje": True}
        elif h_hoje and not h_hoje.get("fechado") and h_hoje.get("hora_abertura"):
            entrega_habilitada_hoje2 = True if h_hoje.get("entrega_habilitada") is None else bool(h_hoje.get("entrega_habilitada"))
            if entrega_habilitada_hoje2:
                e_ab_hoje = _time_from_db(h_hoje.get("entrega_hora_abertura")) or ab
                if e_ab_hoje and hora_agora < e_ab_hoje:
                    proximo_entrega_hoje = {"dia": dia_hoje, "nome_dia": nome_dia_hoje, "hora_abertura": str(e_ab_hoje)[:5], "data": data_hoje.isoformat(), "hoje": True}
        result["proximo_dia_entrega"] = proximo_entrega_hoje or _proximo_dia_entrega_disponivel(cnpjloja, horarios, dia_hoje, data_hoje)
    return result


@app.get("/painel/horario")
@painel_required
def painel_horario():
    _ensure_horario_schema()
    _ensure_delivery_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT permite_agendamento_entrega FROM ecommerce_config_loja WHERE cnpjloja=%s", (cnpjloja,))
    _cfg_row = cur.fetchone()
    permite_agendamento_entrega = bool(_cfg_row and _cfg_row.get("permite_agendamento_entrega"))
    cur.execute(
        """SELECT dia_semana, hora_abertura, hora_fechamento, fechado,
                  entrega_habilitada, entrega_hora_abertura, entrega_hora_fechamento
           FROM ecommerce_config_horario WHERE cnpjloja=%s ORDER BY dia_semana""",
        (cnpjloja,),
    )
    rows = {r["dia_semana"]: r for r in cur.fetchall()}
    cur.execute(
        """SELECT id, data, descricao, fechado, hora_abertura, hora_fechamento
           FROM ecommerce_config_feriado WHERE cnpjloja=%s ORDER BY data""",
        (cnpjloja,),
    )
    feriados = [dict(r) for r in cur.fetchall()]
    for fer in feriados:
        fer["hora_abertura_fmt"] = str(fer["hora_abertura"])[:5] if fer.get("hora_abertura") else ""
        fer["hora_fechamento_fmt"] = str(fer["hora_fechamento"])[:5] if fer.get("hora_fechamento") else ""
    cur.close()
    # Garante todos os 7 dias presentes
    horarios = []
    for dia, nome in _DIAS_SEMANA:
        r = rows.get(dia, {})
        horarios.append({
            "dia_semana": dia,
            "nome": nome,
            "hora_abertura":  str(r.get("hora_abertura") or "08:00")[:5],
            "hora_fechamento": str(r.get("hora_fechamento") or "18:00")[:5],
            "fechado": bool(r.get("fechado", False)),
            "entrega_habilitada": True if r.get("entrega_habilitada") is None else bool(r.get("entrega_habilitada")),
            "entrega_hora_abertura": str(r["entrega_hora_abertura"])[:5] if r.get("entrega_hora_abertura") else "",
            "entrega_hora_fechamento": str(r["entrega_hora_fechamento"])[:5] if r.get("entrega_hora_fechamento") else "",
        })
    return render_template("painel_horario.html", horarios=horarios, feriados=feriados,
                           permite_agendamento_entrega=permite_agendamento_entrega)


@app.post("/painel/horario")
@painel_required
def painel_horario_salvar():
    _ensure_horario_schema()
    _ensure_delivery_schema()
    cnpjloja = session.get("cnpjloja")
    f = request.form
    conn = db(); cur = conn.cursor()
    permite_agendamento_entrega = f.get("permite_agendamento_entrega") == "1"
    cur.execute("""
        INSERT INTO ecommerce_config_loja (cnpjloja, permite_agendamento_entrega)
        VALUES (%s, %s)
        ON CONFLICT (cnpjloja) DO UPDATE SET permite_agendamento_entrega = EXCLUDED.permite_agendamento_entrega
    """, (cnpjloja, permite_agendamento_entrega))
    for dia, _ in _DIAS_SEMANA:
        fechado = f.get(f"fechado_{dia}") == "1"
        abertura  = (f.get(f"abertura_{dia}")  or "08:00").strip() or "08:00"
        fechamento = (f.get(f"fechamento_{dia}") or "18:00").strip() or "18:00"
        entrega_habilitada = f.get(f"entrega_habilitada_{dia}", "1") != "0"
        entrega_abertura = (f.get(f"entrega_abertura_{dia}") or "").strip() or None
        entrega_fechamento = (f.get(f"entrega_fechamento_{dia}") or "").strip() or None
        cur.execute("""
            INSERT INTO ecommerce_config_horario
              (cnpjloja, dia_semana, hora_abertura, hora_fechamento, fechado,
               entrega_habilitada, entrega_hora_abertura, entrega_hora_fechamento)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (cnpjloja, dia_semana) DO UPDATE SET
              hora_abertura           = EXCLUDED.hora_abertura,
              hora_fechamento         = EXCLUDED.hora_fechamento,
              fechado                 = EXCLUDED.fechado,
              entrega_habilitada      = EXCLUDED.entrega_habilitada,
              entrega_hora_abertura   = EXCLUDED.entrega_hora_abertura,
              entrega_hora_fechamento = EXCLUDED.entrega_hora_fechamento
        """, (cnpjloja, dia, abertura, fechamento, fechado,
              entrega_habilitada, entrega_abertura, entrega_fechamento))
    conn.commit()
    cur.close()
    flash("Horário de funcionamento salvo com sucesso.", "success")
    return redirect(url_for("painel_horario"))


@app.post("/painel/horario/feriado/novo")
@painel_required
def painel_feriado_novo():
    _ensure_horario_schema()
    cnpjloja = session.get("cnpjloja")
    f = request.form
    data = (f.get("data") or "").strip()
    if not data:
        flash("Informe a data do feriado.", "error")
        return redirect(url_for("painel_horario"))
    descricao = (f.get("descricao") or "").strip() or None
    fechado = f.get("fechado", "1") != "0"
    hora_abertura = None if fechado else ((f.get("hora_abertura") or "").strip() or None)
    hora_fechamento = None if fechado else ((f.get("hora_fechamento") or "").strip() or None)
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO ecommerce_config_feriado (cnpjloja, data, descricao, fechado, hora_abertura, hora_fechamento)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (cnpjloja, data) DO UPDATE SET
              descricao = EXCLUDED.descricao, fechado = EXCLUDED.fechado,
              hora_abertura = EXCLUDED.hora_abertura, hora_fechamento = EXCLUDED.hora_fechamento
        """, (cnpjloja, data, descricao, fechado, hora_abertura, hora_fechamento))
        conn.commit()
        flash("Feriado cadastrado com sucesso.", "success")
    except Exception:
        conn.rollback()
        flash("Data inválida.", "error")
    cur.close()
    return redirect(url_for("painel_horario"))


@app.post("/painel/horario/feriado/<int:feriado_id>/excluir")
@painel_required
def painel_feriado_excluir(feriado_id):
    _ensure_horario_schema()
    cnpjloja = session.get("cnpjloja")
    conn = db(); cur = conn.cursor()
    cur.execute("DELETE FROM ecommerce_config_feriado WHERE id=%s AND cnpjloja=%s", (feriado_id, cnpjloja))
    conn.commit()
    cur.close()
    flash("Feriado removido.", "success")
    return redirect(url_for("painel_horario"))


@app.get("/api/loja/<cnpjloja>/horario-status")
def api_horario_status(cnpjloja):
    """Retorna se a loja está aberta agora, se a entrega está disponível
    neste horário e, se fechada, quando abre."""
    return jsonify(_status_horario_entrega(cnpjloja))


if __name__ == "__main__":
    app.run(debug=True, port=5001)


