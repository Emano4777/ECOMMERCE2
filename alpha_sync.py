import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor, execute_values


def _digits(value):
    return re.sub(r"\D+", "", str(value or ""))


def _local_dsn():
    return os.getenv("DATABASE_URL") or os.getenv("DDATABASE_URL")


def _alpha_dsn():
    dsn = os.getenv("ALPHA_DATABASE_URL") or os.getenv("A7_DATABASE_URL")
    if dsn:
        return dsn
    host = os.getenv("ALPHA_DB_HOST")
    user = os.getenv("ALPHA_DB_USER")
    password = os.getenv("ALPHA_DB_PASSWORD")
    dbname = os.getenv("ALPHA_DB_NAME", "postgres")
    port = os.getenv("ALPHA_DB_PORT", "6543")
    sslmode = os.getenv("ALPHA_DB_SSLMODE", "require")
    if not host or not user or not password:
        return ""
    return (
        f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
        f"?sslmode={sslmode}"
    )


def alpha_enabled():
    return bool(_alpha_dsn())


def _alpha_schema():
    return os.getenv("ALPHA_DB_SCHEMA", "alpha7").strip() or "alpha7"


def _alpha_store_cnpj():
    return _digits(os.getenv("ALPHA_DEFAULT_CNPJLOJA") or os.getenv("ALPHA_CNPJLOJA"))


def _active_path():
    return (os.getenv("ALPHA_ACTIVE_CLASSIFICATION") or "LICENCE FARMA > ATIVO").upper()


@contextmanager
def _connect(dsn, timeout_ms=20000):
    conn = psycopg2.connect(
        dsn,
        cursor_factory=RealDictCursor,
        connect_timeout=10,
        options=f"-c statement_timeout={int(timeout_ms)}",
    )
    try:
        yield conn
    finally:
        conn.close()


def _local_connect(timeout_ms=20000):
    dsn = _local_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL nao configurada")
    return _connect(dsn, timeout_ms=timeout_ms)


def _alpha_connect(timeout_ms=20000):
    dsn = _alpha_dsn()
    if not dsn:
        raise RuntimeError("ALPHA_DATABASE_URL/ALPHA_DB_* nao configurado")
    return _connect(dsn, timeout_ms=timeout_ms)


def ensure_local_schema(conn=None):
    own = conn is None
    if own:
        ctx = _local_connect()
        conn = ctx.__enter__()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ecommerce_alpha_produtos (
                cnpjloja TEXT NOT NULL,
                alpha_o_id BIGINT NOT NULL,
                ean TEXT,
                nome TEXT,
                preco_venda NUMERIC(15,4),
                preco_promocional NUMERIC(15,4),
                promo_inicio TIMESTAMPTZ,
                promo_fim TIMESTAMPTZ,
                preco_atual NUMERIC(15,4),
                estoque NUMERIC(15,4),
                fabricante TEXT,
                principio_ativo TEXT,
                classificacao TEXT,
                inativo BOOLEAN DEFAULT FALSE,
                medicamento_sngpc BOOLEAN DEFAULT FALSE,
                altura NUMERIC(15,4),
                largura NUMERIC(15,4),
                comprimento NUMERIC(15,4),
                peso NUMERIC(15,4),
                imagem_url TEXT,
                alpha_integracao_concluida BOOLEAN DEFAULT FALSE,
                alpha_updated_at TIMESTAMPTZ,
                synced_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (cnpjloja, alpha_o_id)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_alpha_produtos_cnpj_ean ON ecommerce_alpha_produtos(cnpjloja, ean)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_alpha_produtos_cnpj_ativo ON ecommerce_alpha_produtos(cnpjloja, inativo, estoque)")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS alpha_status TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS alpha_enviado_em TIMESTAMPTZ")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS alpha_erro TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS alpha_orcamento_id BIGINT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS alpha_orcamento_codigo TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS alpha_nf_chave TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS alpha_nf_numero TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS alpha_entrega_status TEXT")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS alpha_status_atualizado_em TIMESTAMPTZ")
        cur.execute("ALTER TABLE ecommerce_pedidos ADD COLUMN IF NOT EXISTS cliente_documento TEXT")
        conn.commit()
        cur.close()
    finally:
        if own:
            ctx.__exit__(None, None, None)


def _row_price(row):
    now = datetime.now(timezone.utc)
    promo = row.get("o_precopromocional")
    start = row.get("o_inicioprecopromocional")
    end = row.get("o_fimprecopromocional")
    if promo is not None and float(promo or 0) > 0:
        if (start is None or start <= now) and (end is None or end >= now):
            return promo
    return row.get("o_precovenda")


def _image_map(cur, cnpjloja, eans):
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
            UNION ALL
            SELECT LTRIM(COALESCE(ean,''), '0') AS ean_key, imagem_cosmos AS imagem_url, atualizado_em AS updated_at
            FROM produto_canon
            WHERE LTRIM(COALESCE(ean,''), '0') = ANY(%s)
              AND imagem_cosmos IS NOT NULL AND TRIM(imagem_cosmos) <> ''
              AND fonte NOT IN ('cosmos_miss', 'ia_miss', 'placeholder_broken')
            UNION ALL
            SELECT LTRIM(COALESCE(m.barra_norm, m.barra,''), '0') AS ean_key,
                   COALESCE(mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) AS imagem_url,
                   NOW() AS updated_at
            FROM medicamentos m
            LEFT JOIN medicamentos_imagens mi ON mi.medicamento_id = m.id
            WHERE LTRIM(COALESCE(m.barra_norm, m.barra,''), '0') = ANY(%s)
              AND COALESCE(mi.cloudinary_url, NULLIF(TRIM(m.imagem), '')) IS NOT NULL
        ) imgs
        WHERE ean_key <> ''
        ORDER BY ean_key, updated_at DESC NULLS LAST
        """,
        (cnpjloja, clean, clean, clean),
    )
    return {r["ean_key"]: r["imagem_url"] for r in cur.fetchall()}


def sync_products(limit=5000, mark_processed=True):
    if not alpha_enabled():
        return {"ok": False, "erro": "alpha_nao_configurado", "total": 0}
    cnpjloja = _alpha_store_cnpj()
    if not cnpjloja:
        return {"ok": False, "erro": "ALPHA_DEFAULT_CNPJLOJA nao configurado", "total": 0}

    ensure_local_schema()
    schema = _alpha_schema()
    active_path = _active_path()

    with _alpha_connect(timeout_ms=55000) as aconn:
        acur = aconn.cursor()
        query = sql.SQL(
            """
            SELECT *
              FROM {}.out_embalagem
             WHERE COALESCE(io_integracaoconcluida, false) = false
                OR COALESCE(o_caminhoclassificacao, '') ILIKE %s
                OR COALESCE(o_estoque, 0) > 0
             ORDER BY o_id
             LIMIT %s
            """
        ).format(sql.Identifier(schema))
        acur.execute(query, (f"%{active_path}%", int(limit)))
        rows = [dict(r) for r in acur.fetchall()]
        acur.close()

    if not rows:
        return {"ok": True, "total": 0, "ativos": 0}

    eans = [r.get("o_codigobarras") for r in rows]
    with _local_connect(timeout_ms=55000) as lconn:
        ensure_local_schema(lconn)
        lcur = lconn.cursor()
        images = _image_map(lcur, cnpjloja, eans)
        values = []
        active_ids = []
        for r in rows:
            ean = _digits(r.get("o_codigobarras"))
            path = (r.get("o_caminhoclassificacao") or "").upper()
            estoque = float(r.get("o_estoque") or 0)
            is_active = bool(ean) and not bool(r.get("o_inativa")) and (
                active_path in path or estoque > 0
            )
            if is_active:
                active_ids.append(r.get("o_id"))
            values.append(
                (
                    cnpjloja,
                    r.get("o_id"),
                    ean or None,
                    r.get("o_descricao"),
                    r.get("o_precovenda"),
                    r.get("o_precopromocional"),
                    r.get("o_inicioprecopromocional"),
                    r.get("o_fimprecopromocional"),
                    _row_price(r),
                    r.get("o_estoque"),
                    r.get("o_nomefabricante"),
                    r.get("o_nomeprincipioativo"),
                    r.get("o_caminhoclassificacao"),
                    not is_active,
                    bool(r.get("o_medicamentosujeitosngpc")),
                    r.get("o_altura"),
                    r.get("o_largura"),
                    r.get("o_comprimento"),
                    r.get("o_peso"),
                    images.get(ean.lstrip("0")) if ean else None,
                    bool(r.get("io_integracaoconcluida")),
                )
            )
        execute_values(
            lcur,
            """
            INSERT INTO ecommerce_alpha_produtos (
                cnpjloja, alpha_o_id, ean, nome, preco_venda, preco_promocional,
                promo_inicio, promo_fim, preco_atual, estoque, fabricante,
                principio_ativo, classificacao, inativo, medicamento_sngpc,
                altura, largura, comprimento, peso, imagem_url,
                alpha_integracao_concluida
            ) VALUES %s
            ON CONFLICT (cnpjloja, alpha_o_id) DO UPDATE SET
                ean=EXCLUDED.ean,
                nome=EXCLUDED.nome,
                preco_venda=EXCLUDED.preco_venda,
                preco_promocional=EXCLUDED.preco_promocional,
                promo_inicio=EXCLUDED.promo_inicio,
                promo_fim=EXCLUDED.promo_fim,
                preco_atual=EXCLUDED.preco_atual,
                estoque=EXCLUDED.estoque,
                fabricante=EXCLUDED.fabricante,
                principio_ativo=EXCLUDED.principio_ativo,
                classificacao=EXCLUDED.classificacao,
                inativo=EXCLUDED.inativo,
                medicamento_sngpc=EXCLUDED.medicamento_sngpc,
                altura=EXCLUDED.altura,
                largura=EXCLUDED.largura,
                comprimento=EXCLUDED.comprimento,
                peso=EXCLUDED.peso,
                imagem_url=COALESCE(EXCLUDED.imagem_url, ecommerce_alpha_produtos.imagem_url),
                alpha_integracao_concluida=EXCLUDED.alpha_integracao_concluida,
                synced_at=NOW()
            """,
            values,
        )
        lconn.commit()
        lcur.close()

    if mark_processed:
        ids = [r.get("o_id") for r in rows if r.get("o_id") is not None]
        if ids:
            with _alpha_connect(timeout_ms=30000) as aconn:
                acur = aconn.cursor()
                query = sql.SQL(
                    "UPDATE {}.out_embalagem SET io_integracaoconcluida = true WHERE o_id = ANY(%s)"
                ).format(sql.Identifier(schema))
                acur.execute(query, (ids,))
                aconn.commit()
                acur.close()

    return {"ok": True, "total": len(rows), "ativos": len(active_ids)}


def _split_address(raw):
    raw = (raw or "").strip()
    cep = _digits(raw)[-8:] if len(_digits(raw)) >= 8 else ""
    uf = ""
    m_uf = re.search(r"\b([A-Z]{2})\b(?:,|\s+\d{5}|\s*$)", raw)
    if m_uf:
        uf = m_uf.group(1)
    main = re.sub(r",?\s*\d{5}-?\d{3}.*$", "", raw).strip(" ,-")
    parts = [p.strip() for p in re.split(r"\s+-\s+", main) if p.strip()]
    rua_num = parts[0] if parts else main
    bairro = parts[1] if len(parts) > 1 else ""
    cidade = parts[2] if len(parts) > 2 else ""
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
        "numero": numero[:6] or "SN",
        "complemento": complemento[:30],
        "bairro": bairro[:70],
        "cidade": cidade[:70],
        "uf": uf[:2],
        "cep": cep[:10],
    }


def _pedido_payload(local_cur, pedido_id):
    local_cur.execute(
        """
        SELECT p.*, COALESCE(p.cliente_documento, c.documento) AS consumidor_documento
        FROM ecommerce_pedidos p
        LEFT JOIN ecommerce_consumidores c ON c.id = p.consumidor_id
        WHERE p.id=%s
        LIMIT 1
        """,
        (pedido_id,),
    )
    pedido = local_cur.fetchone()
    if not pedido:
        raise RuntimeError("Pedido nao encontrado")
    local_cur.execute(
        """
        SELECT i.*, ap.alpha_o_id
        FROM ecommerce_pedido_itens i
        LEFT JOIN ecommerce_alpha_produtos ap
          ON ap.cnpjloja = %s
         AND LTRIM(COALESCE(ap.ean,''), '0') = LTRIM(COALESCE(i.ean,''), '0')
        WHERE i.pedido_id=%s
        ORDER BY i.id
        """,
        (pedido["cnpjloja"], pedido_id),
    )
    itens = local_cur.fetchall()
    if not itens:
        raise RuntimeError("Pedido sem itens")
    missing = [i.get("ean") for i in itens if not i.get("alpha_o_id")]
    if missing:
        raise RuntimeError("Itens sem alpha_o_id: " + ", ".join(str(x) for x in missing[:10]))
    doc = _digits(pedido.get("consumidor_documento"))
    if len(doc) not in {11, 14}:
        raise RuntimeError("Cliente sem CPF/CNPJ valido")
    return pedido, itens, doc


def export_paid_order(pedido_id):
    if not alpha_enabled():
        return {"ok": False, "erro": "alpha_nao_configurado"}
    ensure_local_schema()
    schema = _alpha_schema()
    with _local_connect(timeout_ms=30000) as lconn:
        lcur = lconn.cursor()
        pedido, itens, doc = _pedido_payload(lcur, str(pedido_id))
        if pedido.get("status") != "pago":
            return {"ok": False, "erro": "pedido_nao_pago"}
        if pedido.get("alpha_enviado_em") and pedido.get("alpha_status") in {"enviado", "integrado"}:
            return {"ok": True, "ja_enviado": True}

        addr = _split_address(pedido.get("endereco_entrega"))
        retirada = (pedido.get("tipo_entrega") or "retirada") != "entrega"
        ext_id = str(pedido["id"])
        pessoa_id = f"cli-{doc}"
        now = datetime.now(timezone.utc)
        pessoa_values = {
            "i_codigointegracao": pessoa_id,
            "io_statusintegracao": "N",
            "i_tipo": "J" if len(doc) == 14 else "F",
            "i_nome": (pedido.get("cliente_nome") or "Cliente Poupaqui")[:70],
            "i_email": (pedido.get("cliente_email") or "")[:50],
            "i_cpf": doc if len(doc) == 11 else None,
            "i_cnpj": doc if len(doc) == 14 else None,
            "i_razaosocial": (pedido.get("cliente_nome") or "")[:70] if len(doc) == 14 else None,
            "i_dddcelular": _digits(pedido.get("cliente_telefone"))[:2] or None,
            "i_celular": _digits(pedido.get("cliente_telefone"))[2:13] or None,
            "i_logradouro": addr["logradouro"],
            "i_numero": addr["numero"],
            "i_complemento": addr["complemento"],
            "i_bairro": addr["bairro"],
            "i_cidade": addr["cidade"],
            "i_estado": addr["uf"],
            "i_cep": addr["cep"],
        }
        pedido_values = {
            "i_codigointegracao": ext_id,
            "i_codigopessoaintegracao": pessoa_id,
            "io_statusintegracao": "N",
            "i_logradouroentrega": None if retirada else addr["logradouro"],
            "i_bairroentrega": None if retirada else addr["bairro"],
            "i_cepentrega": None if retirada else addr["cep"],
            "i_cidadeentrega": None if retirada else addr["cidade"],
            "i_estadoentrega": None if retirada else addr["uf"],
            "i_numeroentrega": None if retirada else addr["numero"],
            "i_complementoentrega": None if retirada else addr["complemento"],
            "i_observacaoentrega": (pedido.get("previsao_entrega") or "")[:255] or None,
            "i_taxaentrega": pedido.get("frete_valor") or 0,
            "i_statuspedido": "B",
            "i_valortotal": pedido.get("total") or 0,
            "i_dataemissaopedido": pedido.get("criado_em") or now,
            "i_formapagamento": (pedido.get("forma_pagamento") or "")[:50],
            "i_tipoentrega": "A" if not retirada else None,
            "i_retiradaloja": retirada,
            "i_pagamentoadiantadoentrega": True,
            "i_vendafinalizadaexternamente": (pedido.get("origem") or "") == "mercado_livre",
            "i_trocoentrega": None,
            "i_informacoescomplementaresdocumentofiscal": (
                f"Pedido Poupaqui {ext_id}. Origem: {pedido.get('origem') or 'ecommerce'}."
            ),
        }

    with _alpha_connect(timeout_ms=30000) as aconn:
        acur = aconn.cursor()
        acur.execute(
            sql.SQL("SELECT 1 FROM {}.in_pedido WHERE i_codigointegracao=%s LIMIT 1").format(sql.Identifier(schema)),
            (ext_id,),
        )
        if acur.fetchone():
            with _local_connect(timeout_ms=10000) as lconn:
                lcur = lconn.cursor()
                lcur.execute(
                    "UPDATE ecommerce_pedidos SET alpha_status='enviado', alpha_enviado_em=COALESCE(alpha_enviado_em, NOW()), alpha_erro=NULL WHERE id=%s",
                    (ext_id,),
                )
                lconn.commit()
            return {"ok": True, "ja_existe_alpha": True}

        acur.execute(
            sql.SQL("SELECT 1 FROM {}.in_pessoa WHERE i_codigointegracao=%s LIMIT 1").format(sql.Identifier(schema)),
            (pessoa_id,),
        )
        if not acur.fetchone():
            _insert_dict(acur, schema, "in_pessoa", pessoa_values)
        _insert_dict(acur, schema, "in_pedido", pedido_values)
        for seq, item in enumerate(itens, start=1):
            qty = float(item.get("qty") or 1)
            bruto = float(item.get("preco_unitario") or 0)
            total = round(bruto * qty, 4)
            _insert_dict(
                acur,
                schema,
                "in_itempedido",
                {
                    "i_codigointegracao": f"{ext_id}-{seq}",
                    "i_codigopedidointegracao": ext_id,
                    "i_idoutembalagem": item.get("alpha_o_id"),
                    "i_valorunitariobruto": bruto,
                    "i_valorunitarioliquido": bruto,
                    "i_quantidade": qty,
                    "i_valortotal": total,
                },
            )
        aconn.commit()

    with _local_connect(timeout_ms=10000) as lconn:
        lcur = lconn.cursor()
        lcur.execute(
            """
            UPDATE ecommerce_pedidos
               SET alpha_status='enviado',
                   alpha_enviado_em=NOW(),
                   alpha_erro=NULL
             WHERE id=%s
            """,
            (ext_id,),
        )
        lconn.commit()
    return {"ok": True}


def _insert_dict(cur, schema, table, values):
    clean = {k: v for k, v in values.items() if v is not None}
    cols = list(clean.keys())
    query = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
        sql.Identifier(schema),
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        sql.SQL(", ").join(sql.Placeholder() for _ in cols),
    )
    cur.execute(query, [clean[c] for c in cols])


def sync_order_statuses(limit=500):
    if not alpha_enabled():
        return {"ok": False, "erro": "alpha_nao_configurado"}
    schema = _alpha_schema()
    ensure_local_schema()
    with _alpha_connect(timeout_ms=30000) as aconn:
        acur = aconn.cursor()
        acur.execute(
            sql.SQL(
                """
                SELECT i_codigointegracao, io_statusintegracao, o_descricaoerrointegracao,
                       o_idorcamento, o_codigoorcamento
                  FROM {}.in_pedido
                 WHERE i_codigointegracao IS NOT NULL
                   AND io_statusintegracao IN ('S','E')
                 ORDER BY i_codigointegracao DESC
                 LIMIT %s
                """
            ).format(sql.Identifier(schema)),
            (int(limit),),
        )
        pedidos = acur.fetchall()
        acur.execute(
            sql.SQL(
                """
                SELECT o_id, o_codigopedidointegracao, o_numero, o_chaveacesso, io_integracaoconcluida
                  FROM {}.out_documentofiscalpedido
                 WHERE COALESCE(io_integracaoconcluida, false) = false
                 LIMIT %s
                """
            ).format(sql.Identifier(schema)),
            (int(limit),),
        )
        nfes = acur.fetchall()
        acur.execute(
            sql.SQL(
                """
                SELECT o_id, o_codigopedidointegracao, o_status, io_integracaoconcluida
                  FROM {}.out_entregaremessapedido
                 WHERE COALESCE(io_integracaoconcluida, false) = false
                 LIMIT %s
                """
            ).format(sql.Identifier(schema)),
            (int(limit),),
        )
        remessas = acur.fetchall()

    with _local_connect(timeout_ms=30000) as lconn:
        lcur = lconn.cursor()
        for p in pedidos:
            status = "integrado" if p.get("io_statusintegracao") == "S" else "erro"
            lcur.execute(
                """
                UPDATE ecommerce_pedidos
                   SET alpha_status=%s,
                       alpha_erro=%s,
                       alpha_orcamento_id=%s,
                       alpha_orcamento_codigo=%s,
                       alpha_status_atualizado_em=NOW()
                 WHERE id=%s
                """,
                (
                    status,
                    p.get("o_descricaoerrointegracao"),
                    p.get("o_idorcamento"),
                    p.get("o_codigoorcamento"),
                    p.get("i_codigointegracao"),
                ),
            )
        for n in nfes:
            lcur.execute(
                """
                UPDATE ecommerce_pedidos
                   SET alpha_nf_numero=%s,
                       alpha_nf_chave=%s,
                       alpha_status_atualizado_em=NOW()
                 WHERE id=%s
                """,
                (str(n.get("o_numero") or ""), n.get("o_chaveacesso"), n.get("o_codigopedidointegracao")),
            )
        for r in remessas:
            lcur.execute(
                """
                UPDATE ecommerce_pedidos
                   SET alpha_entrega_status=%s,
                       alpha_status_atualizado_em=NOW()
                 WHERE id=%s
                """,
                (r.get("o_status"), r.get("o_codigopedidointegracao")),
            )
        lconn.commit()

    with _alpha_connect(timeout_ms=30000) as aconn:
        acur = aconn.cursor()
        nfe_ids = [n.get("o_id") for n in nfes if n.get("o_id") is not None]
        rem_ids = [r.get("o_id") for r in remessas if r.get("o_id") is not None]
        if nfe_ids:
            acur.execute(
                sql.SQL("UPDATE {}.out_documentofiscalpedido SET io_integracaoconcluida=true WHERE o_id=ANY(%s)").format(sql.Identifier(schema)),
                (nfe_ids,),
            )
        if rem_ids:
            acur.execute(
                sql.SQL("UPDATE {}.out_entregaremessapedido SET io_integracaoconcluida=true WHERE o_id=ANY(%s)").format(sql.Identifier(schema)),
                (rem_ids,),
            )
        aconn.commit()

    return {"ok": True, "pedidos": len(pedidos), "nfes": len(nfes), "remessas": len(remessas)}


def sync_all():
    products = sync_products()
    statuses = sync_order_statuses()
    return {"ok": bool(products.get("ok") and statuses.get("ok")), "products": products, "statuses": statuses}
