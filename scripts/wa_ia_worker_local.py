"""Worker local do atendimento WhatsApp IA com Ollama (roda no PC, nao no servidor).

Le a fila no Postgres (DATABASE_URL do .env), classifica com o Ollama em
127.0.0.1 e, se o modo permitir, responde pela WA Sender (WASENDER_API_KEY).
So processa quando o motor no banco e 'local' -- com motor 'servidor' o cron
do HostGator e quem atende, nunca os dois (uma unica via de envio).

Uso (na raiz do repo):
  python scripts/wa_ia_worker_local.py --check            # Ollama/modelo/RAM, nada no banco
  python scripts/wa_ia_worker_local.py --status           # modo, motor, fila, pendencias
  python scripts/wa_ia_worker_local.py --pendentes        # escalacoes HUMAN abertas
  python scripts/wa_ia_worker_local.py --resolver 5517... # fecha escalacoes de um contato
  python scripts/wa_ia_worker_local.py --humanos          # contatos com atendimento humano ativo
  python scripts/wa_ia_worker_local.py --assumir 5517...  # operador assume (IA quieta ate --devolver)
  python scripts/wa_ia_worker_local.py --assumir 5517... --minutos 60   # assume por 60 min
  python scripts/wa_ia_worker_local.py --devolver 5517... # IA volta, so para mensagens novas
  python scripts/wa_ia_worker_local.py --motor local      # assume a fila (servidor para)
  python scripts/wa_ia_worker_local.py --modo shadow      # off|shadow|test|live (live exige --confirmo-live
                                                          # e POUPAQUI_WA_IA_LIVE_AUTORIZADO=1)
  python scripts/wa_ia_worker_local.py --modo off         # DESLIGA na hora (kill-switch)
  python scripts/wa_ia_worker_local.py --once             # uma passada
  python scripts/wa_ia_worker_local.py --loop 30          # passada a cada 30s (tarefa agendada)
  python scripts/wa_ia_worker_local.py --once --assistente  # dados reais + conversa geral via Ollama

Se o Ollama estiver fora do ar a passada e pulada: as mensagens ficam na fila
(expiram em POUPAQUI_WA_IA_MAX_IDADE_MIN) em vez de virar HUMAN em massa.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import poupaqui_wa_atendimento as wa  # noqa: E402
from poupaqui_ai_policy import (  # noqa: E402
    DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL, is_loopback_url, ollama_classifier,
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
LOCK_PATH = os.path.join(ROOT, ".wa_ia_worker_local.lock")
PILOT_PHONE = "5517991436759"


def live_autorizado():
    """Atendimento para TODOS os numeros (menos grupos/canais, que o webhook ja descarta) so com
    POUPAQUI_WA_IA_LIVE_AUTORIZADO=1 na config do servidor E modo live no banco (--modo live --confirmo-live).
    Sem a flag o worker continua preso ao numero de teste, mesmo com o banco em live."""
    return os.getenv("POUPAQUI_WA_IA_LIVE_AUTORIZADO") == "1"


def load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass


def connect():
    import psycopg2
    dsn = os.getenv("DATABASE_URL") or os.getenv("DDATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL nao configurada no .env")
    return psycopg2.connect(dsn, connect_timeout=10,
                            options="-c statement_timeout=30000 -c idle_in_transaction_session_timeout=20000")


def ollama_url():
    return (os.getenv("OLLAMA_URL") or DEFAULT_OLLAMA_URL).rstrip("/")


def ollama_model():
    return os.getenv("POUPAQUI_OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL


def _get_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ollama_health():
    """(ok, detalhe). ok so se o servidor responde em loopback e tem o modelo."""
    url, model = ollama_url(), ollama_model()
    if not is_loopback_url(url):
        return False, {"erro": "OLLAMA_URL precisa ser 127.0.0.1/localhost"}
    try:
        ver = _get_json(f"{url}/api/version").get("version")
        tags = [m.get("name") for m in _get_json(f"{url}/api/tags").get("models", [])]
        loaded = [{"modelo": m.get("name"), "ram_mb": round((m.get("size") or 0) / 2**20),
                   "vram_mb": round((m.get("size_vram") or 0) / 2**20)}
                  for m in _get_json(f"{url}/api/ps").get("models", [])]
    except Exception as exc:
        return False, {"erro": f"ollama indisponivel: {type(exc).__name__}"}
    has = any(t == model or t == f"{model}:latest" for t in tags)
    info = {"versao": ver, "modelo": model, "modelo_instalado": has, "modelos": tags, "carregados": loaded}
    if not has:
        return False, info
    # modelo so-raciocinio (ex.: qwen3:4b = Qwen3-4B-Thinking-2507) nao desliga
    # o <think>: recusa antes de gastar CPU; a fila espera em vez de virar HUMAN
    try:
        req = urllib.request.Request(f"{url}/api/show", data=json.dumps({"model": model}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            finetune = str(json.loads(r.read().decode("utf-8")).get("model_info", {})
                           .get("general.finetune") or "")
    except Exception as exc:
        return False, {**info, "erro": f"ollama /api/show falhou: {type(exc).__name__}"}
    info["finetune"] = finetune
    if "think" in finetune.lower():
        return False, {**info, "erro": "modelo de raciocinio obrigatorio; use um modelo instruct"}
    return True, info


def make_sender():
    """WA Sender direto; piloto local limitado ao unico contato autorizado."""
    api_key = (os.getenv("WASENDER_API_KEY") or "").strip()

    def send(numero, texto, _tentativa=0):
        if not api_key:
            raise RuntimeError("WASENDER_API_KEY ausente")
        phone = wa.normalize_phone(numero)
        if not phone:
            raise RuntimeError("numero invalido")
        if not live_autorizado() and (phone != PILOT_PHONE or phone not in wa.test_phones()):
            raise RuntimeError("destino fora da allowlist de teste")
        req = urllib.request.Request(
            "https://wasenderapi.com/api/send-message",
            data=json.dumps({"to": "+" + phone, "text": texto}).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "User-Agent": UA, "Accept": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                try:
                    return json.loads(r.read().decode("utf-8"))
                except ValueError:
                    return {"success": True}
        except urllib.error.HTTPError as e:
            body = e.read(300).decode(errors="replace")
            if e.code == 429 and _tentativa < 2:  # Account Protection: 1 msg/5s
                try:
                    wait = float(json.loads(body).get("retry_after") or 5) + 0.5
                except Exception:
                    wait = 5.5
                time.sleep(min(wait, 20))
                return send(numero, texto, _tentativa + 1)
            raise RuntimeError(f"WA Sender HTTP {e.code}")
    return send


def single_instance():
    """Trava de arquivo: uma unica instancia do worker nesta maquina (Windows ou Linux/servidor)."""
    fh = open(LOCK_PATH, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def run_once(stats=None, assistente=False):
    if not live_autorizado():
        # piloto: teto e destino restritos mesmo com env herdado live
        os.environ["POUPAQUI_WA_IA_MAX_MODE"] = wa.effective_mode(wa.env_mode_ceiling(), "test")
        os.environ["POUPAQUI_WA_IA_TEST_PHONES"] = PILOT_PHONE
    if assistente:
        # Dados reais e suporte deterministico; conversa geral via Ollama local.
        # o alerta sai antes e a resposta so diz "ja passei" se ele foi entregue
        import poupaqui_ai_assistente as A
        conn = connect()
        try:
            A.liberar_whisper_ocioso()   # memoria do servidor: solta o Whisper parado ha 10 min
            # print/foto/audio: le com o modelo multimodal do Ollama local; desligado = "nao abro midia"
            leitor = None
            if os.getenv("POUPAQUI_WA_IA_LER_IMAGENS") == "1":
                repo = A.RepoLeitura(conn)
                leitor = A.leitor_midia_ollama(timeout=75, vocabulario=lambda: [
                    f["nome"] for f in repo.fornecedores_entrega()])
            return wa.run_worker(conn, classifier=None, sender=make_sender(), engine="local",
                                 model_label="assistente", assistente=A.assistente_worker(llm_geral=A.ollama_geral(timeout=60)),
                                 max_contacts=wa._cfg_int("POUPAQUI_WA_IA_MAX_CONTATOS_RUN", 4), leitor_midia=leitor)
        finally:
            conn.close()
    ok, info = ollama_health()
    if not ok:
        return {"ignorado": "ollama/modelo indisponivel", "ollama": info}
    conn = connect()
    try:
        lat = [] if stats is None else stats
        res = wa.run_worker(conn, classifier=ollama_classifier(stats=lat), sender=make_sender(),
                            engine="local", model_label=f"ollama:{ollama_model()}",
                            max_contacts=wa._cfg_int("POUPAQUI_WA_IA_MAX_CONTATOS_RUN", 4))
        if lat:
            res["latencia_ia_s"] = lat
        return res
    finally:
        conn.close()


def _mask(phone):
    return f"...{phone[-4:]}" if phone else ""


def main(argv=None):
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--pendentes", action="store_true")
    g.add_argument("--resolver", metavar="TELEFONE")
    g.add_argument("--humanos", action="store_true")
    g.add_argument("--assumir", metavar="TELEFONE")
    g.add_argument("--devolver", metavar="TELEFONE")
    ap.add_argument("--minutos", type=int, help="prazo do --assumir (padrao: ate --devolver)")
    g.add_argument("--motor", choices=wa.ENGINES)
    g.add_argument("--modo", choices=wa.MODES)
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", type=int, metavar="SEG")
    ap.add_argument("--confirmo-live", action="store_true", help="necessario para --modo live")
    ap.add_argument("--completo", action="store_true", help="mostra telefones inteiros em --pendentes")
    ap.add_argument("--assistente", action="store_true",
                    help="com --once/--loop: usa o assistente com dados reais em vez do classificador Ollama")
    a = ap.parse_args(argv)
    if a.modo == "live" and not live_autorizado():
        raise SystemExit("live exige POUPAQUI_WA_IA_LIVE_AUTORIZADO=1 na config do worker")

    def out(obj):
        print(json.dumps(obj, ensure_ascii=False, default=str), flush=True)

    if a.check:
        ok, info = ollama_health()
        out({"ok": ok, **info})
        return 0 if ok else 1
    if a.loop:
        lock = single_instance()
        if not lock:
            out({"ignorado": "outra instancia do worker local ja esta rodando"})
            return 0
        interval = max(5, a.loop)  # checagem vazia custa ~1 s de banco
        # systemctl stop/restart manda SIGTERM: vira SystemExit para os finally rodarem (o run_worker
        # libera o lease) e as mensagens em maos voltam para a fila na hora, nao em 10 min
        import signal
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            while True:
                try:
                    out({"em": time.strftime("%Y-%m-%d %H:%M:%S"), **run_once(assistente=a.assistente)})
                except Exception as exc:  # banco/rede caiu: tenta na proxima volta
                    out({"erro": f"{type(exc).__name__}: {str(exc)[:200]}"})
                time.sleep(interval)
        except (SystemExit, KeyboardInterrupt):
            try:
                conn = connect()
                try:
                    out({"encerrando": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "devolvidas_para_fila": wa.devolver_em_curso(conn)})
                finally:
                    conn.close()
            except Exception as exc:
                out({"erro_ao_encerrar": type(exc).__name__})
            raise
    if a.once:
        out(run_once(assistente=a.assistente))
        return 0

    conn = connect()
    try:
        if a.motor:
            wa.set_db_engine(conn, a.motor)
        elif a.modo:
            if a.modo == "live" and not a.confirmo_live:
                raise SystemExit("modo live afeta todos os clientes reais: use --confirmo-live")
            wa.set_db_mode(conn, a.modo)
        elif a.resolver:
            out({"resolvidas": wa.resolve_human(conn, a.resolver)})
            return 0
        elif a.assumir:
            phone = wa.assumir_atendimento(conn, a.assumir, a.minutos)
            out({"assumido": _mask(phone), "ate": "devolver" if not a.minutos else f"{a.minutos} min"})
            return 0
        elif a.devolver:
            out({"devolvido": wa.devolver_atendimento(conn, a.devolver)})
            return 0
        elif a.humanos:
            rows = wa.atendimentos_humanos(conn)
            for r in rows:
                if not a.completo:
                    r["telefone"] = _mask(r["telefone"])
                out(r)
            out({"total": len(rows)})
            return 0
        elif a.pendentes:
            rows = wa.pending_human(conn)
            for r in rows:
                if not a.completo:
                    r["telefone"] = _mask(r["telefone"])
                out(r)
            out({"total": len(rows)})
            return 0
        out(wa.status_summary(conn))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
