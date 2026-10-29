"""LOCAL: conversas completas pelo responder() do worker, SEM envio e SEM escrita no banco.
(a) identidade real do numero de teste (vinculo lido do banco); (b) loja real com pedidos recentes,
consultas reais em transacao READ ONLY (RepoSuporte). LLM = o configurado no .env (gemma4:e2b via tunel).
Saida: WA_IA_E2E_CONVERSAS.json (CNPJ/uuid mascarados).
Uso: python scripts/wa_ia_e2e_conversas.py
"""
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "scripts")]
import wa_ia_worker_local as W  # noqa: E402
import poupaqui_wa_atendimento as wa  # noqa: E402
import poupaqui_ai_assistente as A  # noqa: E402
import poupaqui_ai_suporte as S  # noqa: E402

CONVERSAS_TESTE = [  # identidade real do 5517991436759
    ("casual_contexto", ["oi, tudo bem?", "mano hoje tá osso, dormi 4h", "e vc, tá fazendo oq?",
                         "kkk sorte sua", "o que eu falei antes?"]),
    ("ironia_giria", ["nossa, que sistema rápido, só demorou meia hora pra abrir kkk", "slk, tô passado",
                      "mas de boa, bora que hoje é sexta"]),
    ("tecnico_misto", ["qual o horário de corte da germed?", "e da medley?", "show, valeu",
                       "como tá meu pedido?"]),
    ("humano_ou_ia", ["vc é gente ou robô?", "sério? parece gente kkk"]),
]
CONVERSAS_LOJA = [  # loja real, somente leitura
    ("pedido_referencia", ["bom dia, como tá meu pedido?", "e o da {forn}?", "e esse pedido, já saiu?",
                           "ainda não chegou, era pra ontem"]),
    ("pedido_e_conversa", ["opa, tudo certo?", "o pedido de hoje foi?", "massa, valeu mano",
                           "tá corrido aqui hoje kkk"]),
]


def mask(s):
    s = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", str(s))
    return re.sub(r"\d{14}", "<cnpj>", s)


def rodar(nome, falas, ident, repo, llm):
    hist, linhas = [], []
    for f in falas:
        t = time.monotonic()
        r = A.responder([f], identidade=ident, historico=hist, repo=repo, llm_geral=llm)
        dt = round(time.monotonic() - t, 1)
        linhas.append({"fala": f, "resposta": mask(r.reply), "acao": r.acao, "intencao": r.intencao,
                       "motivo": mask(r.motivo), "fontes": r.fontes, "escalar": r.escalar, "s": dt})
        print(f"[{dt:5.1f}s] {r.acao:<7} {r.intencao:<10} | {f}\n          -> {mask(r.reply)}")
        hist += [{"de": "contato", "texto": f}] + ([{"de": "assistente", "texto": r.reply}] if r.reply else [])
    print("-" * 70)
    return {"conversa": nome, "linhas": linhas}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    W.load_env()
    conn = W.connect()
    conn.set_session(readonly=True)
    llm = A.ollama_geral(timeout=25)
    repo = S.RepoSuporte(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT vinculo_manual_tipo, vinculo_manual_ref FROM ecommerce_wa_contatos WHERE telefone = %s",
                    (W.PILOT_PHONE,))
        manual = cur.fetchone()
    ident_teste = wa.identify_contact(conn, W.PILOT_PHONE, manual)
    with conn.cursor() as cur:
        cur.execute("""SELECT u.cnpjloja, p.fornecedor FROM pedidos3 p JOIN users u ON u.id = p.user_id
                       WHERE p.created_at > NOW() - INTERVAL '7 days' AND COALESCE(u.cnpjloja, '') <> ''
                       ORDER BY p.created_at DESC LIMIT 1""")
        cnpj, forn = cur.fetchone()
    conn.rollback()
    ident_loja = {"verificada": True, "metodo": "telefone", "tipo": "loja", "ref": cnpj, "nome": "LOJA"}
    out = {"modelo": os.getenv("POUPAQUI_OLLAMA_MODEL"), "identidade_teste": ident_teste.get("tipo"), "conversas": []}
    print(f"== numero de teste (identidade: {ident_teste.get('tipo')})")
    for nome, falas in CONVERSAS_TESTE:
        out["conversas"].append(rodar(nome, falas, ident_teste, repo, llm))
    print("== loja real (somente leitura)")
    for nome, falas in CONVERSAS_LOJA:
        out["conversas"].append(rodar(nome, [f.format(forn=forn.lower()) for f in falas], ident_loja, repo, llm))
    conn.close()
    with open(os.path.join(ROOT, "WA_IA_E2E_CONVERSAS.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
