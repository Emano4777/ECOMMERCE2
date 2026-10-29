"""Piloto SHADOW somente-leitura do atendimento WhatsApp IA com Ollama no servidor.

Le as ultimas mensagens recebidas SO dos telefones da allowlist de teste
(POUPAQUI_WA_IA_TEST_PHONES), monta o mesmo payload do worker e mostra o que o
Ollama decidiria. Nao existe caminho de envio aqui: nao importa WA Sender, nao
grava decisao, nao reserva fila e nao muda modo/motor. A sessao do Postgres e
aberta com default_transaction_read_only=on -- qualquer escrita falha no banco.

Uso (no servidor, depois de aprovado pelo usuario):
  python scripts/wa_ia_shadow_servidor.py --check           # Ollama/modelo, nada no banco
  python scripts/wa_ia_shadow_servidor.py --horas 24        # shadow da allowlist
  python scripts/wa_ia_shadow_servidor.py --assistente      # shadow do assistente novo (sem LLM)

No modo --assistente as unicas acoes externas sao SELECTs read-only e a sonda
HTTP GET das 3 paginas fixas (poupaqui_ai_suporte.SITES).
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import poupaqui_wa_atendimento as wa  # noqa: E402
from poupaqui_ai_policy import (  # noqa: E402
    DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL, Decision, classify_with_ollama, is_loopback_url,
    preliminary_gate, validate_answer,
)

READ_ONLY_OPTIONS = ("-c default_transaction_read_only=on -c statement_timeout=30000 "
                     "-c idle_in_transaction_session_timeout=20000")


def connect_read_only():
    import psycopg2
    dsn = os.getenv("DATABASE_URL") or os.getenv("DDATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL nao configurada")
    conn = psycopg2.connect(dsn, connect_timeout=10, options=READ_ONLY_OPTIONS)
    # pooler em modo transacao pode ignorar options de startup: reforca por sessao
    conn.set_session(readonly=True)
    return conn


def last_sequences(conn, phones, hours, per_phone=5):
    """Ultimas mensagens recebidas por telefone autorizado (so SELECT)."""
    out = {}
    with conn.cursor() as cur:
        for phone in sorted(phones):
            cur.execute("""
                SELECT id, tipo, corpo, nome_whatsapp FROM ecommerce_wa_mensagens
                WHERE telefone = %s AND direcao = 'in'
                  AND recebido_em > NOW() - (%s || ' hours')::interval
                ORDER BY recebido_em DESC, id DESC LIMIT %s
            """, (phone, str(hours), per_phone))
            rows = sorted(cur.fetchall())
            if rows:
                out[phone] = rows
    return out


def shadow_contact(conn, phone, rows, classifier):
    ids = [r[0] for r in rows]
    kinds, texts = [r[1] for r in rows], [r[2] for r in rows]
    push_name = next((r[3] for r in reversed(rows) if r[3]), None)
    gate = preliminary_gate(texts, kinds)
    if gate is not None:
        return {"decisao": gate.decision, "origem": "gate", "motivo": gate.reason, "resposta": gate.reply}
    with conn.cursor() as cur:
        cur.execute("""SELECT opt_out, vinculo_manual_tipo, vinculo_manual_ref
                       FROM ecommerce_wa_contatos WHERE telefone = %s""", (phone,))
        contato = cur.fetchone() or (False, None, None)
    if contato[0]:
        return {"decisao": "SILENCE", "origem": "regra", "motivo": "contato com opt-out", "resposta": ""}
    identity = wa.identify_contact(conn, phone, (contato[1], contato[2]))
    facts = wa.verified_context(conn, identity)
    payload = {
        "identidade_verificada": identity if identity["verificada"] else None,
        "identidade_nao_verificada": None if identity["verificada"] else {
            "motivo": identity["metodo"], "nome_exibido_no_whatsapp_nao_confiavel": push_name},
        "dados_verificados": facts,
        "historico_recente": wa._history(conn, phone, ids, wa._cfg_int("POUPAQUI_WA_IA_HISTORICO_MAX", 20)),
        "mensagens_novas": [{"tipo": k, "texto": t} for k, t in zip(kinds, texts)],
    }
    conn.rollback()  # nao segura transacao durante a chamada ao modelo
    t0 = time.monotonic()
    raw = classifier(payload)
    dt = round(time.monotonic() - t0, 2)
    dec = validate_answer(raw, texts, identity["verificada"], facts)
    return {"decisao": dec.decision, "origem": "modelo", "seg": dt, "motivo": dec.reason,
            "resposta": dec.reply, "verificado": identity["verificada"]}


def shadow_assistente(conn, phone, rows, sonda=None, llm=None, repo_factory=None):
    """O que o assistente novo (dados reais + suporte tecnico) responderia.
    Mesma conexao read-only: as consultas do assistente sao SELECT em transacao
    READ ONLY. Nao grava decisao, nao alerta ninguem, nao envia. A resposta e
    mostrada como sairia em shadow (sem alerta -> frase honesta) e tambem como
    sairia se o alerta fosse entregue, para avaliacao."""
    import poupaqui_ai_assistente as A
    import poupaqui_ai_suporte as S
    ids = [r[0] for r in rows]
    kinds, texts = [r[1] for r in rows], [r[2] for r in rows]
    with conn.cursor() as cur:
        cur.execute("""SELECT opt_out, vinculo_manual_tipo, vinculo_manual_ref
                       FROM ecommerce_wa_contatos WHERE telefone = %s""", (phone,))
        contato = cur.fetchone() or (False, None, None)
    if contato[0]:
        return {"decisao": "SILENCE", "origem": "regra", "motivo": "contato com opt-out", "resposta": ""}
    if wa.takeover_ativo(conn, phone):
        return {"decisao": "SILENCE", "origem": "humano", "motivo": wa.HUMANO_ATIVO_REASON, "resposta": ""}
    identity = wa.identify_contact(conn, phone, (contato[1], contato[2]))
    hist = wa._history(conn, phone, ids, wa._cfg_int("POUPAQUI_WA_IA_HISTORICO_MAX", 20))
    conn.rollback()
    t0 = time.monotonic()
    r = A.responder(texts, kinds, identity, hist, (repo_factory or S.RepoSuporte)(conn), None, llm, sonda)
    conn.rollback()
    out = {"decisao": r.acao, "origem": "assistente", "seg": round(time.monotonic() - t0, 2),
           "intencao": r.intencao, "motivo": r.motivo, "escalar": r.escalar, "verificado": identity["verificada"],
           "fontes": r.fontes, "resposta_base": r.reply}
    if r.acao == "ANSWER" and r.escalar:
        out.update({"equipe": r.equipe, "resumo": r.resumo, "alerta": "nao_enviado (shadow)",
                    "resposta": A.completar_resposta(r.reply, "indisponivel", None, r.equipe),
                    "resposta_se_alerta_entregue": A.completar_resposta(r.reply, "enviado", None, r.equipe)})
    else:
        out["resposta"] = r.reply
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--horas", type=int, default=24)
    ap.add_argument("--assistente", action="store_true",
                    help="avalia o assistente novo (dados reais + suporte) em vez do classificador Ollama")
    a = ap.parse_args(argv)
    if a.assistente:
        phones = wa.test_phones()
        conn = connect_read_only()
        try:
            seqs = last_sequences(conn, phones, max(1, min(a.horas, 168)))
            conn.rollback()
            for phone, rows in seqs.items():
                res = shadow_assistente(conn, phone, rows)
                print(json.dumps({"telefone": f"...{phone[-4:]}", "mensagens": len(rows), **res},
                                 ensure_ascii=False, default=str), flush=True)
            print(json.dumps({"contatos": len(seqs), "allowlist": len(phones), "enviado": 0, "gravado": 0,
                              "alertado": 0}), flush=True)
        finally:
            conn.close()
        return 0
    url = (os.getenv("OLLAMA_URL") or DEFAULT_OLLAMA_URL).rstrip("/")
    model = os.getenv("POUPAQUI_OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL
    if not is_loopback_url(url):
        raise SystemExit("OLLAMA_URL precisa ser 127.0.0.1/localhost")

    def out(obj):
        print(json.dumps(obj, ensure_ascii=False, default=str), flush=True)

    if a.check:
        d = classify_with_ollama({"mensagens_novas": [{"tipo": "text", "texto": "teste"}]},
                                 base_url=url, model=model)
        ok = isinstance(d, Decision) and not d.reason.startswith("AI ")
        out({"ok": ok, "modelo": model, "decisao_teste": d.decision, "motivo": d.reason})
        return 0 if ok else 1

    phones = wa.test_phones()
    conn = connect_read_only()
    try:
        seqs = last_sequences(conn, phones, max(1, min(a.horas, 168)))
        conn.rollback()
        for phone, rows in seqs.items():
            res = shadow_contact(conn, phone, rows,
                                 lambda p: classify_with_ollama(p, base_url=url, model=model))
            out({"telefone": f"...{phone[-4:]}", "mensagens": len(rows), **res})
        out({"contatos": len(seqs), "allowlist": len(phones), "enviado": 0, "gravado": 0})
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
