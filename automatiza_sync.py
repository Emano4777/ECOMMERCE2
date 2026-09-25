"""Integracao com o Automatiza (ERP do MySQL local da loja de Sao Jose do
Rio Preto) -- diferente do Alpha (Postgres remoto, ja integrado pras outras
lojas), o banco do Automatiza so aceita conexao em localhost:3306, entao
esse script PRECISA rodar na propria maquina da loja (Agendador de Tarefas
do Windows), nunca no servidor/Vercel. Ele fala local com o MySQL e direto
com o Supabase (DATABASE_URL), sem precisar de nenhuma API/servidor novo.

Duas direções:
  sync_products()      -- Automatiza -> Supabase (catalogo/preco/estoque)
  export_paid_order()  -- Supabase -> Automatiza (pedido pago vira pedido lá)
  sync_notas_fiscais() -- Automatiza -> Supabase (cupom fiscal cacheado pro cliente ver)
"""
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
import pymysql
import pymysql.cursors


def _digits(value):
    return re.sub(r"\D+", "", str(value or ""))


def _local_dsn():
    return os.getenv("DATABASE_URL") or os.getenv("DDATABASE_URL")


def automatiza_enabled():
    return bool(os.getenv("AUTOMATIZA_DB_HOST") or os.getenv("AUTOMATIZA_DB_USER"))


def _cnpjloja():
    v = _digits(os.getenv("AUTOMATIZA_CNPJLOJA", "64369730000147"))
    if not v:
        raise RuntimeError("AUTOMATIZA_CNPJLOJA nao configurado")
    return v


@contextmanager
def _local_connect(timeout_ms=30000):
    dsn = _local_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL nao configurada")
    conn = psycopg2.connect(
        dsn, cursor_factory=RealDictCursor, connect_timeout=10,
        options=f"-c statement_timeout={int(timeout_ms)}",
    )
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _automatiza_connect(timeout=20):
    conn = pymysql.connect(
        host=os.getenv("AUTOMATIZA_DB_HOST", "localhost"),
        port=int(os.getenv("AUTOMATIZA_DB_PORT", "3306")),
        # Confirmado em campo: usuario e minusculo ("delivery"), mesmo o
        # nome tendo sido passado com D maiusculo originalmente -- MySQL
        # diferencia maiuscula/minuscula em nome de usuario.
        user=os.getenv("AUTOMATIZA_DB_USER", "delivery"),
        password=os.getenv("AUTOMATIZA_DB_PASSWORD", "Delivery"),
        database=os.getenv("AUTOMATIZA_DB_SCHEMA", "automatiza"),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=timeout,
        charset="utf8mb4",
        autocommit=False,
    )
    try:
        yield conn
    finally:
        conn.close()


def ensure_local_schema():
    """Colunas de controle no pedido + tabela de cache da nota fiscal --
    idempotente. Nao precisa de tabela nova pro catalogo: reaproveita
    ecommerce_alpha_produtos, ja generico o bastante -- chave e
    (cnpjloja, alpha_o_id), e aqui alpha_o_id guarda o codigo_mercadoria
    do Automatiza."""
    with _local_connect(timeout_ms=10000) as conn:
        cur = conn.cursor()
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS automatiza_enviado_em TIMESTAMPTZ")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS automatiza_pedido_codigo TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS automatiza_erro TEXT")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ecommerce_automatiza_notas_fiscais (
                pedido_id UUID PRIMARY KEY,
                cnpjloja TEXT NOT NULL,
                numero_nota TEXT,
                chave_acesso TEXT,
                data_lancamento TIMESTAMPTZ,
                valor_bruto NUMERIC(15,4),
                valor_desconto NUMERIC(15,4),
                valor_liquido NUMERIC(15,4),
                itens JSONB,
                sincronizado_em TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()


# ── Produtos: Automatiza -> Supabase ────────────────────────────────────────

def _preco_calc(row):
    """Regra confirmada com a loja: preco_venda_delivery e o preco pra
    ecommerce (prioridade); quando nao tem, usa preco_venda_loja (que sem
    promocao fica igual a preco_venda). So considera "promocao" de verdade
    se o valor calculado for MENOR que o preco_venda cheio -- evita marcar
    preco_promocional igual ao preco normal (mesmo problema de "desconto
    falso" que ja corrigimos pro Alpha)."""
    base = float(row.get("preco_venda") or 0)
    delivery = row.get("preco_venda_delivery")
    loja = row.get("preco_venda_loja")
    candidato = delivery if delivery is not None else loja
    if candidato is not None:
        candidato = float(candidato)
        if 0 < candidato < base:
            return base, candidato, candidato
    return base, None, base


_IMAGE_PLACEHOLDER_FILTER = "imagem_url NOT ILIKE '%%CAIXA_GEN%%POUPAQUI%%' AND imagem_url NOT ILIKE '%%ChatGPT_Image%%'"


def _image_map(cur, cnpjloja, eans):
    """Cruza EAN com o banco de imagens que ja existe (mesma logica do
    alpha_sync.py) -- e assim que 'confronta com as imagens que temos pelo
    EAN' acontece: o Automatiza manda produto sem imagem, a gente completa
    com o que ja tem de outras lojas/fontes (Cosmos, cadastro manual etc)."""
    clean = sorted({_digits(e).lstrip("0") for e in eans if _digits(e)})
    if not clean:
        return {}
    cur.execute(
        """
        SELECT DISTINCT ON (ean_key)
               ean_key, imagem_url
        FROM (
            SELECT LTRIM(COALESCE(ean,''), '0') AS ean_key, imagem_url, updated_at
            FROM ecommerce_produto_imagens
            WHERE cnpjloja=%s AND LTRIM(COALESCE(ean,''), '0') = ANY(%s)
              AND imagem_url IS NOT NULL AND TRIM(imagem_url) <> ''
              AND {placeholder_filter}
            UNION ALL
            SELECT LTRIM(COALESCE(ean,''), '0') AS ean_key, imagem_cosmos AS imagem_url, atualizado_em AS updated_at
            FROM produto_canon
            WHERE LTRIM(COALESCE(ean,''), '0') = ANY(%s)
              AND imagem_cosmos IS NOT NULL AND TRIM(imagem_cosmos) <> ''
              AND fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
              AND {placeholder_filter_cosmos}
            UNION ALL
            SELECT LTRIM(COALESCE(m.barra_norm, m.barra,''), '0') AS ean_key,
                   COALESCE(mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem_url,
                   TIMESTAMP '1970-01-01' AS updated_at
            FROM medicamentos m
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            WHERE LTRIM(COALESCE(m.barra_norm, m.barra,''), '0') = ANY(%s)
              AND COALESCE(mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) IS NOT NULL
              AND COALESCE(mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) NOT ILIKE '%%CAIXA_GEN%%POUPAQUI%%'
              AND COALESCE(mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) NOT ILIKE '%%ChatGPT_Image%%'
        ) imgs
        WHERE ean_key <> ''
        ORDER BY ean_key, updated_at DESC NULLS LAST
        """.format(
            placeholder_filter=_IMAGE_PLACEHOLDER_FILTER,
            placeholder_filter_cosmos=_IMAGE_PLACEHOLDER_FILTER.replace("imagem_url", "imagem_cosmos"),
        ),
        (cnpjloja, clean, clean, clean),
    )
    return {r["ean_key"]: r["imagem_url"] for r in cur.fetchall()}


def sync_products():
    """Roda a cada 15-30min (Agendador de Tarefas) -- pega a view inteira
    de produtos (preco + estoque juntos, ja que a loja disse que preco so
    muda 1x por dia e estoque em tempo real, mas fazer upsert completo com
    frequencia moderada e mais simples/robusto que manter 2 jobs
    separados, e o volume de 1 loja nao pesa no Supabase)."""
    if not automatiza_enabled():
        return {"ok": False, "erro": "automatiza_nao_configurado"}
    cnpjloja = _cnpjloja()
    ensure_local_schema()

    with _automatiza_connect() as aconn:
        acur = aconn.cursor()
        acur.execute("SELECT * FROM automatiza.view_ecommerce_delivery_produtos")
        rows = acur.fetchall()
        acur.close()

    if not rows:
        return {"ok": True, "total": 0}

    with _local_connect() as lconn:
        lcur = lconn.cursor()
        images = _image_map(lcur, cnpjloja, [r.get("EAN") for r in rows])

        values = []
        for r in rows:
            codigo = r.get("codigo_mercadoria")
            if codigo is None:
                continue
            ean = _digits(r.get("EAN"))
            preco_venda, preco_promocional, preco_atual = _preco_calc(r)
            classificacao = " > ".join(
                p for p in [r.get("grupo_principal"), r.get("subgrupo"), r.get("categoria")]
                if p and str(p).strip().upper() != "NAO INFORMADO"
            ) or None
            situacao = (r.get("situacao") or "").strip().upper()
            values.append((
                cnpjloja,
                int(codigo),
                ean or None,
                r.get("descricao"),
                preco_venda,
                preco_promocional,
                None, None,  # promo_inicio/promo_fim -- Automatiza nao informa janela de data
                preco_atual,
                r.get("estoque"),
                r.get("laboratorio"),
                classificacao,
                situacao != "ATIVO",
                bool(r.get("cotrole_especial")),
                r.get("altura"),
                r.get("largura"),
                r.get("comprimento"),
                r.get("peso"),
                images.get(ean.lstrip("0")) if ean else None,
            ))

        execute_values(
            lcur,
            """
            INSERT INTO ecommerce_alpha_produtos (
                cnpjloja, alpha_o_id, ean, nome, preco_venda, preco_promocional,
                promo_inicio, promo_fim, preco_atual, estoque, fabricante,
                classificacao, inativo, medicamento_sngpc,
                altura, largura, comprimento, peso, imagem_url, primeiro_visto_em
            ) VALUES %s
            ON CONFLICT (cnpjloja, alpha_o_id) DO UPDATE SET
                ean=EXCLUDED.ean,
                nome=EXCLUDED.nome,
                preco_venda=EXCLUDED.preco_venda,
                preco_promocional=EXCLUDED.preco_promocional,
                preco_atual=EXCLUDED.preco_atual,
                estoque=EXCLUDED.estoque,
                fabricante=EXCLUDED.fabricante,
                classificacao=EXCLUDED.classificacao,
                inativo=EXCLUDED.inativo,
                medicamento_sngpc=EXCLUDED.medicamento_sngpc,
                altura=EXCLUDED.altura,
                largura=EXCLUDED.largura,
                comprimento=EXCLUDED.comprimento,
                peso=EXCLUDED.peso,
                imagem_url=COALESCE(EXCLUDED.imagem_url, ecommerce_alpha_produtos.imagem_url),
                synced_at=NOW()
            """,
            values,
            template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
        )
        lconn.commit()
        lcur.close()

    return {"ok": True, "total": len(values)}


# ── Endereco: mesmo parser ja usado no alpha_sync.py (formato do site) ─────

def _extrair_cidade_uf(main):
    tokens = [t.strip() for t in re.split(r"\s*(?:,|/|\s-\s)\s*", main) if t.strip()]
    if tokens and re.fullmatch(r"[A-Za-z]{2}", tokens[-1]):
        uf = tokens[-1].upper()
        cidade = tokens[-2] if len(tokens) > 1 else ""
    elif tokens:
        uf = ""
        cidade = tokens[-1]
    else:
        uf, cidade = "", ""
    return cidade, uf


def _split_address(raw):
    raw = (raw or "").strip()
    cep = _digits(raw)[-8:] if len(_digits(raw)) >= 8 else ""
    main = re.sub(r",?\s*\d{5}-?\d{3}.*$", "", raw).strip(" ,-")
    cidade, uf = _extrair_cidade_uf(main)
    parts = [p.strip() for p in re.split(r"\s+-\s+", main) if p.strip()]
    rua_num = parts[0] if parts else main
    bairro = parts[1] if len(parts) > 1 else ""
    numero = "SN"
    logradouro = rua_num
    m_num = re.match(r"(.+?),\s*([^,\s]+)(?:\s+(.*))?$", rua_num)
    complemento = ""
    if m_num:
        logradouro = m_num.group(1).strip()
        numero = (m_num.group(2) or "SN")[:6]
        complemento = (m_num.group(3) or "")[:30]
    return {
        "logradouro": logradouro[:70],
        "numero": (numero[:6] or "SN"),
        "complemento": complemento[:30],
        "bairro": bairro[:70],
        "cidade": cidade[:70],
        "uf": uf[:2],
        "cep": cep[:10],
    }


# ── Pedido: Supabase -> Automatiza ──────────────────────────────────────────

def _pedido_payload(local_cur, pedido_id, cnpjloja):
    local_cur.execute(
        """
        SELECT p.*, COALESCE(p.cliente_documento, c.documento) AS consumidor_documento
        FROM ecommerce_pedidos p
        LEFT JOIN ecommerce_consumidores c ON c.id = p.consumidor_id
        WHERE p.id=%s AND p.cnpjloja=%s
        LIMIT 1
        """,
        (pedido_id, cnpjloja),
    )
    pedido = local_cur.fetchone()
    if not pedido:
        raise RuntimeError("Pedido nao encontrado (ou nao e dessa loja)")
    local_cur.execute(
        """
        SELECT i.*, ap.alpha_o_id AS codigo_mercadoria
        FROM ecommerce_pedido_itens i
        LEFT JOIN ecommerce_alpha_produtos ap
          ON ap.cnpjloja=%s AND LTRIM(COALESCE(ap.ean,''),'0') = LTRIM(COALESCE(i.ean,''),'0')
        WHERE i.pedido_id=%s
        ORDER BY i.id
        """,
        (cnpjloja, pedido_id),
    )
    itens = local_cur.fetchall()
    if not itens:
        raise RuntimeError("Pedido sem itens")
    faltando = [i.get("ean") for i in itens if not i.get("codigo_mercadoria")]
    if faltando:
        raise RuntimeError(
            "Itens sem codigo_mercadoria (ainda nao sincronizado do Automatiza): "
            + ", ".join(str(x) for x in faltando[:5])
        )
    return pedido, itens


_PAGO_STATUSES = {
    "pago", "pronto_retirada", "em_separacao", "separado",
    "em_transito", "saiu_entrega", "saiu_para_entrega", "entregue", "concluido",
}


def export_paid_order(pedido_id):
    """Chamado pelo webhook/cron do app.py (mesmo padrao do
    _alpha_export_paid_order_safe), UMA vez por pedido pago. Idempotente
    via automatiza_enviado_em -- reenviar o mesmo pedido nao duplica."""
    if not automatiza_enabled():
        return {"ok": False, "erro": "automatiza_nao_configurado"}
    cnpjloja = _cnpjloja()
    ensure_local_schema()

    with _local_connect() as lconn:
        lcur = lconn.cursor()
        pedido, itens = _pedido_payload(lcur, str(pedido_id), cnpjloja)

        if pedido.get("automatiza_enviado_em"):
            return {"ok": True, "ja_enviado": True}

        pago = (
            pedido.get("status") in _PAGO_STATUSES
            or (pedido.get("pagamento_status") or "").lower() == "approved"
        )
        if not pago:
            return {"ok": False, "erro": "pedido_nao_pago"}

        endereco = _split_address(pedido.get("endereco_entrega"))
        retirada = (pedido.get("tipo_entrega") or "retirada") != "entrega"
        total_bruto_itens = sum(float(i["preco_unitario"]) * int(i["qty"]) for i in itens) or 0.0
        desconto_total = float(pedido.get("desconto_cupom") or 0)
        # Retirada nunca cobra frete, mesmo que frete_valor tenha algum
        # resquicio de calculo -- confirmado pela loja.
        frete = 0.0 if retirada else float(pedido.get("frete_valor") or 0)
        codigo_ecommerce = str(pedido["id"])[:8].upper()
        doc = _digits(pedido.get("cliente_documento") or pedido.get("consumidor_documento"))
        # So temos pix e cartao no site -- mapeia pros codigos reais do
        # Automatiza (confirmados: 2=CARTAO, 5=PIX; 1=DINHEIRO so entra
        # como fallback se um dia aparecer outra forma de pagamento).
        forma = (pedido.get("forma_pagamento") or "").lower()
        if forma == "pix":
            cod_condicao_pagamento, desc_pagamento = 5, "PIX"
        elif forma in ("mercadopago", "cartao", "cartao_credito", "cartao_debito"):
            cod_condicao_pagamento, desc_pagamento = 2, "CARTAO"
        else:
            cod_condicao_pagamento, desc_pagamento = 1, "DINHEIRO"

        try:
            with _automatiza_connect() as aconn:
                acur = aconn.cursor()
                acur.execute(
                    """
                    INSERT INTO automatiza.pedido_ecommerce SET
                        filiCodigo = automatiza.obter_filial_logada(),
                        peecParceiro = 'Delivery',
                        peecTransporte_Metodo = %s,
                        peecCanal = 'Delivery',
                        peecData_Cadastro = NOW(),
                        peecData_Atualizacao = NOW(),
                        peecUltimo_Evento = 'Confirmado',
                        peecCodigo_Ecommerce = %s,
                        peecValor_Total = %s,
                        peecValor_Frete = %s,
                        peecValor_Desconto = %s,
                        Codigo_Cliente = 0,
                        peecNome_Cliente = %s,
                        peecTelefone_Cliente = %s,
                        peecEmail_Cliente = %s,
                        peecDocumento_Cliente = %s,
                        peecData_Nascimento_Cliente = %s,
                        peecInscricao_Estadual_Cliente = '',
                        peecData_Entrega = %s,
                        peecCep_Entrega = %s,
                        peecLogradouro_Entrega = %s,
                        peecNumero_Entrega = %s,
                        peecBairro_Entrega = %s,
                        peecCidade_Entrega = %s,
                        peecUF_Entrega = %s,
                        peecComplemento_Entrega = %s,
                        peecReferencia_Entrega = '',
                        peecPagamento_Realizado = 1
                    """,
                    (
                        "Retirada" if retirada else "Delivery",
                        codigo_ecommerce,
                        round(float(pedido["total"]), 2),
                        round(frete, 2),
                        round(desconto_total, 2),
                        (pedido.get("cliente_nome") or "Cliente Poupaqui")[:100],
                        _digits(pedido.get("cliente_telefone")),
                        pedido.get("cliente_email") or "",
                        doc,
                        None,
                        "1900-01-01" if retirada else "1900-01-01",
                        "" if retirada else endereco["cep"],
                        "" if retirada else endereco["logradouro"],
                        "" if retirada else endereco["numero"],
                        "" if retirada else endereco["bairro"],
                        "" if retirada else endereco["cidade"],
                        "" if retirada else endereco["uf"],
                        "" if retirada else endereco["complemento"],
                    ),
                )
                acur.execute("SELECT LAST_INSERT_ID() AS id")
                codigo_pedido = acur.fetchone()["id"]

                for it in itens:
                    qty = int(it["qty"])
                    preco_unit_cheio = float(it["preco_unitario"])
                    total_produto = round(preco_unit_cheio * qty, 2)
                    share = (total_produto / total_bruto_itens) if total_bruto_itens else 0
                    desconto_item = round(desconto_total * share, 2)
                    total_liquido = round(total_produto - desconto_item, 2)
                    acur.execute(
                        """
                        INSERT INTO automatiza.itens_pedido_ecommerce SET
                            Codigo_Pedido_Ecommerce = %s,
                            filiCodigo_Pedido_Ecommerce = automatiza.obter_filial_logada(),
                            itpeDescricao_Mercadoria = %s,
                            Codigo_Mercadoria = %s,
                            itpeQuantidade = %s,
                            itpeValor = %s,
                            itpeTotalDesconto = %s,
                            itpeTotalProduto = %s,
                            itpeTotalLiquido = %s
                        """,
                        (
                            codigo_pedido,
                            (it.get("nome") or "Produto")[:200],
                            int(it["codigo_mercadoria"]),
                            qty,
                            preco_unit_cheio,
                            desconto_item,
                            total_produto,
                            total_liquido,
                        ),
                    )

                acur.execute(
                    """
                    INSERT INTO automatiza.itens_pedido_ecommerce_pagamento SET
                        Codigo_Pedido_Ecommerce = %s,
                        filiCodigo_Pedido_Ecommerce = automatiza.obter_filial_logada(),
                        ipepPagamento_Realizado = 1,
                        ipepCodigo_Condicao_Pagamento = %s,
                        ipepForma_Pagamento = %s,
                        ipepDescricao_Pagamento = %s,
                        ipepValor_Total = %s,
                        ipepQuantidade_Parcelas_Pagamento = 0,
                        ipepNSU_Pagamento = '',
                        ipepBandeira_Pagamento = '',
                        ipepGateway_Pagamento = %s,
                        ipepCodigo_Transacao_Pagamento = %s
                    """,
                    (
                        codigo_pedido,
                        cod_condicao_pagamento,
                        desc_pagamento,
                        desc_pagamento,
                        round(float(pedido["total"]), 2),
                        "MercadoPago",
                        str(pedido.get("mp_payment_id") or ""),
                    ),
                )
                aconn.commit()
                acur.close()
        except Exception as exc:
            lcur.execute(
                "UPDATE ecommerce_pedidos SET automatiza_erro=%s WHERE id=%s",
                (str(exc)[:500], pedido_id),
            )
            lconn.commit()
            lcur.close()
            raise

        lcur.execute(
            "UPDATE ecommerce_pedidos SET automatiza_enviado_em=NOW(), automatiza_pedido_codigo=%s, automatiza_erro=NULL WHERE id=%s",
            (str(codigo_pedido), pedido_id),
        )
        lconn.commit()
        lcur.close()

    return {"ok": True, "codigo_pedido": codigo_pedido}


def export_pending_orders(limit=50):
    """Varre pedidos pagos dessa loja ainda nao enviados -- chamada pelo
    loop principal do agente local a cada execucao."""
    if not automatiza_enabled():
        return {"ok": False, "erro": "automatiza_nao_configurado"}
    cnpjloja = _cnpjloja()
    with _local_connect() as lconn:
        lcur = lconn.cursor()
        lcur.execute(
            """
            SELECT id FROM ecommerce_pedidos
            WHERE cnpjloja=%s AND automatiza_enviado_em IS NULL
              AND (status = ANY(%s) OR pagamento_status = 'approved')
            ORDER BY criado_em
            LIMIT %s
            """,
            (cnpjloja, list(_PAGO_STATUSES), limit),
        )
        ids = [r["id"] for r in lcur.fetchall()]
        lcur.close()
    resultado = {"ok": True, "enviados": 0, "erros": []}
    for pid in ids:
        try:
            r = export_paid_order(str(pid))
            if r.get("ok") and not r.get("ja_enviado"):
                resultado["enviados"] += 1
        except Exception as exc:
            resultado["erros"].append({"pedido_id": str(pid), "erro": str(exc)[:300]})
    return resultado


def sync_notas_fiscais():
    """Depois que um pedido e enviado, o Automatiza leva um tempo pra gerar
    o cupom fiscal -- entao varre pedidos ja enviados que AINDA nao tem
    nota cacheada e tenta de novo a cada execucao, ate achar. Sem isso a
    nota nunca aparece pro cliente no site: o Vercel nao alcanca essa view
    diretamente, so o agente local consegue ler e guardar no Supabase."""
    if not automatiza_enabled():
        return {"ok": False, "erro": "automatiza_nao_configurado"}
    cnpjloja = _cnpjloja()
    ensure_local_schema()

    with _local_connect() as lconn:
        lcur = lconn.cursor()
        lcur.execute(
            """
            SELECT id, automatiza_pedido_codigo FROM ecommerce_pedidos p
            WHERE cnpjloja=%s AND automatiza_pedido_codigo IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM ecommerce_automatiza_notas_fiscais n WHERE n.pedido_id = p.id
              )
            ORDER BY criado_em DESC
            LIMIT 100
            """,
            (cnpjloja,),
        )
        pendentes = lcur.fetchall()
        lcur.close()

    if not pendentes:
        return {"ok": True, "total": 0}

    encontrados = 0
    with _automatiza_connect() as aconn, _local_connect() as lconn:
        acur = aconn.cursor()
        lcur = lconn.cursor()
        for p in pendentes:
            acur.execute(
                "SELECT * FROM automatiza.view_ecommerce_delivery_cupons_fiscais WHERE codigo_pedido_ecommerce=%s",
                (p["automatiza_pedido_codigo"],),
            )
            linhas = acur.fetchall()
            if not linhas:
                continue
            primeira = linhas[0]
            itens = [
                {
                    "codigo_mercadoria": l.get("codigo_mercadoria"),
                    "descricao": l.get("descricao_mercadoria"),
                    "ean": l.get("ean"),
                    "quantidade": float(l.get("quantidade") or 0),
                    "valor_bruto_unitario": float(
                        l.get("valor_bruto_unitario") or l.get("valor_bruto") or 0
                    ),
                    "valor_desconto": float(l.get("valor_desconto") or 0),
                    "valor_liquido": float(l.get("valor_liquido") or 0),
                }
                for l in linhas
            ]
            lcur.execute(
                """
                INSERT INTO ecommerce_automatiza_notas_fiscais
                    (pedido_id, cnpjloja, numero_nota, chave_acesso, data_lancamento,
                     valor_bruto, valor_desconto, valor_liquido, itens)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (pedido_id) DO UPDATE SET
                    numero_nota=EXCLUDED.numero_nota, chave_acesso=EXCLUDED.chave_acesso,
                    data_lancamento=EXCLUDED.data_lancamento, valor_bruto=EXCLUDED.valor_bruto,
                    valor_desconto=EXCLUDED.valor_desconto, valor_liquido=EXCLUDED.valor_liquido,
                    itens=EXCLUDED.itens, sincronizado_em=NOW()
                """,
                (
                    p["id"], cnpjloja,
                    primeira.get("numero_nota"), primeira.get("chave_acesso"),
                    primeira.get("data_lancamento"),
                    sum(float(l.get("valor_bruto") or 0) for l in linhas),
                    sum(float(l.get("valor_desconto") or 0) for l in linhas),
                    sum(float(l.get("valor_liquido") or 0) for l in linhas),
                    json.dumps(itens),
                ),
            )
            encontrados += 1
        lconn.commit()
        acur.close()
        lcur.close()

    return {"ok": True, "total": encontrados}


def sync_all():
    produtos = sync_products()
    pedidos = export_pending_orders()
    notas = sync_notas_fiscais()
    return {"produtos": produtos, "pedidos": pedidos, "notas": notas}


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    print(json.dumps(sync_all(), indent=2, ensure_ascii=False, default=str))
