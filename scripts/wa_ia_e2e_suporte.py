"""LOCAL: duvidas tecnicas dos 3 sistemas pelo responder() real, SEM envio e SEM escrita.
Loja real com vendas recentes (read-only) e a identidade real do numero de teste; inclui textos
como chegam de audio transcrito e de print lido. Uso: python scripts/wa_ia_e2e_suporte.py"""
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

CASOS = [
    ("texto", "o relatório de vendas não atualizou hoje"),
    ("texto", "as vendas de ontem não aparecem no relatorio"),
    ("texto", "o valor do relatório tá diferente do meu caixa"),
    ("texto", "o relatoiro ta fora do ar?"),
    ("texto", "o site da loja caiu? cliente reclamou que não abre"),
    ("texto", "cliente não consegue finalizar a compra no site, trava no pagamento"),
    ("texto", "a dipirona não aparece no ecommerce"),
    ("texto", "como faço pra entrar nos relatórios?"),
    ("texto", "não consigo entrar, diz senha inválida"),
    ("audio", "[Áudio transcrito] Oi, bom dia. O site de vendas da loja tá fora do ar desde cedo, os clientes "
              "tão reclamando que não conseguem comprar nada."),
    ("imagem", wa.texto_da_midia({"resumo": "Tela do painel de relatórios mostrando erro ao carregar o dashboard",
                                  "texto": "Relatórios PoupAqui\nErro ao carregar dashboard\nTente novamente mais tarde"})),
    ("imagem", wa.texto_da_midia({"resumo": "Foto de uma caixa de remédio", "texto": "Dipirona Monoidratada 500mg"},
                                 "esse aqui não aparece no site")),
]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    W.load_env()
    conn = W.connect()
    conn.set_session(readonly=True)
    repo = S.RepoSuporte(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT vinculo_manual_tipo, vinculo_manual_ref FROM ecommerce_wa_contatos WHERE telefone = %s",
                    (W.PILOT_PHONE,))
        ident_teste = wa.identify_contact(conn, W.PILOT_PHONE, cur.fetchone())
        cur.execute("""SELECT u.cnpjloja FROM pedidos3 p JOIN users u ON u.id = p.user_id
                       WHERE p.created_at > NOW() - INTERVAL '3 days' AND COALESCE(u.cnpjloja, '') <> ''
                       ORDER BY p.created_at DESC LIMIT 1""")
        cnpj = cur.fetchone()[0]
    conn.rollback()
    ident_loja = {"verificada": True, "metodo": "telefone", "tipo": "loja", "ref": cnpj, "nome": "LOJA"}
    llm = A.ollama_geral(timeout=25)
    for nome, ident in (("LOJA", ident_loja), ("SEU NUMERO (interno)", ident_teste)):
        print(f"===== {nome}")
        for tipo, txt in CASOS:
            t = time.monotonic()
            r = A.responder([txt], identidade=ident, repo=repo, llm_geral=llm)
            reply = re.sub(r"\d{14}", "<cnpj>", r.reply).replace("\n", " ")
            print(f"[{time.monotonic()-t:4.1f}s] {tipo:<6} {r.intencao:<12} esc={int(r.escalar)} | {txt[:70]}\n"
                  f"       -> {reply[:330]}")
    conn.close()


if __name__ == "__main__":
    main()
