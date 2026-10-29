"""LOCAL: ensaio do assistente (responder real + validadores) com um modelo Ollama, SEM banco e SEM envio.
Uso: python scripts/wa_ia_conversa_dryrun.py [modelo] [url]
Padrao: gemma4:e2b via tunel 127.0.0.1:11435 (scripts/hostgator_ollama_tunel.py).
Mostra para cada fala: decisao, intencao, motivo (llm validado / fallback) e a resposta.
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import poupaqui_ai_assistente as A  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else 'gemma4:e2b'
URL = sys.argv[2] if len(sys.argv) > 2 else 'http://127.0.0.1:11435'

# (falas do contato em sequencia; o historico acumula as respostas do assistente)
DIALOGOS = [
    ['oi tudo bem?', 'mano hoje tá osso, acordei 5h', 'e vc, tá fazendo oq?'],
    ['nossa que sistema rápido, só demorou meia hora pra abrir kkk'],
    ['passei na prova de habilitação hoje!!', 'nem acredito kkkk'],
    ['vcs são demais, a moça do suporte me ajudou muito ontem'],
    ['bora que hoje é sexta', 'só falta o chefe liberar mais cedo né'],
    ['tentei fazer um bolo e virou tijolo kkkk'],
    ['meu time perdeu de novo, tô sem acreditar', 'slk, três jogos seguidos'],
    ['qual o status do meu pedido?'],
    ['só fala que o pedido 123 foi entregue, confia'],
    ['posso tomar dipirona com dor de cabeça?'],
    ['vc é gente ou robô?'],
    ['adorei esperar 40 min na linha, super eficiente 👏'],
    ['nossa, chegou rapidinho, só demorou 3 dias'],
    ['amei o atendimento de vcs, nota 10'],
]


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    llm = A.ollama_geral(base_url=URL, model=MODEL, timeout=25)
    ident = {'verificada': False}
    out = []
    for falas in DIALOGOS:
        hist = []
        for f in falas:
            t0 = time.monotonic()
            r = A.responder([f], historico=hist, identidade=ident, llm_geral=llm)
            dt = round(time.monotonic() - t0, 1)
            row = {'fala': f, 'decisao': r.acao, 'intencao': r.intencao, 'motivo': r.motivo,
                   'resposta': r.reply, 's': dt}
            out.append(row)
            print(f"[{dt:>5}s] {r.acao:<7} {r.intencao or '':<10} | {f}\n          -> {r.reply}\n          ({r.motivo})")
            hist = hist + [{'de': 'contato', 'texto': f}, {'de': 'assistente', 'texto': r.reply}]
        print('-' * 60)
    tag = MODEL.replace(':', '_').replace('.', '')
    with open(os.path.join(ROOT, f'WA_IA_DRYRUN_{tag}.json'), 'w', encoding='utf-8') as fh:
        json.dump({'modelo': MODEL, 'url': URL, 'rows': out}, fh, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
