"""HostGator-only worker: share ERP stock and authorized invoices with ML."""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import requests

STORE = "54185432000143"
API = "https://api.mercadolibre.com"


def is_virtual_bundle(item):
    return bool(item.get("bundle") or "bundle" in item.get("tags", []))


def sync(db, get_token, apply=False):
    if os.getenv("VERCEL"):
        raise RuntimeError("Rotina exclusiva do HostGator")
    from erp_routing import source_for_store
    from automatiza_fiscal import ensure_schema
    conn = db()
    cur = conn.cursor()
    if source_for_store(cur, STORE) != "automatiza":
        return {"ok": False, "erro": "migracao_nao_ativada"}
    ensure_schema(cur)
    conn.commit()
    cur.execute("""SELECT m.ml_item_id,m.ean,ap.estoque,ap.inativo,ap.synced_at,
        b.preco,b.sincronizado_em FROM ml_items m
        JOIN LATERAL (SELECT * FROM ecommerce_alpha_produtos ap WHERE ap.cnpjloja=m.cnpjloja
        AND LTRIM(ap.ean,'0')=LTRIM(m.ean,'0') ORDER BY ap.synced_at DESC,ap.inativo,ap.alpha_o_id LIMIT 1) ap ON TRUE
        LEFT JOIN ecommerce_automatiza_ml_precos b ON b.cnpjloja=m.cnpjloja AND LTRIM(b.ean,'0')=LTRIM(m.ean,'0')
        WHERE m.cnpjloja=%s ORDER BY m.ml_item_id""", (STORE,))
    mappings = cur.fetchall()
    token = get_token(cnpjloja=STORE)
    if not token:
        return {"ok": False,"erro":"sem_token"}
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + token
    cached_items = {}
    known_ids = {row["ml_item_id"] for row in mappings}
    me = session.get(API+"/users/me",timeout=20)
    if me.status_code != 200 or str(me.json().get("id")) != "3501375634":
        raise RuntimeError("Conta ML nao corresponde a loja Sao Pedro")
    for status in ("active", "paused"):
        listing = session.get(API+"/users/3501375634/items/search",params={"status":status,"limit":100},timeout=20)
        if listing.status_code != 200:
            raise RuntimeError("Nao foi possivel enumerar os anuncios da loja")
        if listing.json().get("paging",{}).get("total",0)>100:
            raise RuntimeError("Catalogo maior que limite de sincronizacao; ampliar paginacao")
        for iid in listing.json().get("results",[]):
            if iid in known_ids:
                continue
            item_r = session.get(API+"/items/"+iid,timeout=20)
            if item_r.status_code != 200:
                continue
            item = item_r.json()
            cached_items[iid] = item
            gtin = next((a.get("value_name") for a in item.get("attributes",[]) if a.get("id")=="GTIN"),None)
            if not gtin or is_virtual_bundle(item):
                continue
            cur.execute("""SELECT %s AS ml_item_id,ap.ean,ap.estoque,ap.inativo,ap.synced_at,
                b.preco,b.sincronizado_em FROM ecommerce_alpha_produtos ap
                LEFT JOIN ecommerce_automatiza_ml_precos b ON b.cnpjloja=ap.cnpjloja AND LTRIM(b.ean,'0')=LTRIM(ap.ean,'0')
                WHERE ap.cnpjloja=%s AND LTRIM(ap.ean,'0')=LTRIM(%s,'0')
                ORDER BY ap.synced_at DESC,ap.inativo,ap.alpha_o_id LIMIT 1""",(iid,STORE,gtin))
            match = cur.fetchone()
            if match:
                mappings.append(match)
                known_ids.add(iid)
                if apply:
                    cur.execute("""INSERT INTO ml_items(ml_item_id,ean,titulo,preco,status,updated_at,category_id,cnpjloja)
                        VALUES (%s,%s,%s,%s,%s,NOW(),%s,%s) ON CONFLICT(ml_item_id) DO NOTHING""",
                        (iid,match["ean"],item.get("title"),item.get("price"),item.get("status"),item.get("category_id"),STORE))
    conn.commit()
    result = {"ok": True,"aplicar":apply,"estoques":0,"precos":0,"notas":0,"ignorados":[],"erros":[],"plano":[]}
    updated = set()
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=20)
    for mapping in mappings:
        iid = mapping["ml_item_id"]
        if mapping["synced_at"] < stale_before:
            result["ignorados"].append({"id":iid,"motivo":"estoque_desatualizado"}); continue
        item = cached_items.get(iid)
        if item is None:
            item_r = session.get(API+"/items/"+iid,timeout=20)
            if item_r.status_code != 200:
                result["erros"].append({"id":iid,"http":item_r.status_code}); continue
            item = item_r.json()
        if item.get("status") not in {"active","paused"} or item.get("variations") or is_virtual_bundle(item) or item.get("shipping",{}).get("logistic_type")=="fulfillment":
            result["ignorados"].append({"id":iid,"motivo":"tipo_ou_status_nao_suportado"}); continue
        gtins = [a.get("value_name","").lstrip("0") for a in item.get("attributes",[]) if a.get("id")=="GTIN"]
        if mapping["ean"].lstrip("0") not in gtins:
            result["erros"].append({"id":iid,"erro":"ean_divergente"}); continue
        qty = 0 if mapping["inativo"] else max(0,int(mapping["estoque"] or 0))
        # Reserve paid orders while the pharmacy has not invoiced them yet.
        cur.execute("""SELECT COALESCE(SUM(i.qty),0) AS reserved FROM ecommerce_pedido_itens i
            JOIN ecommerce_pedidos p ON p.id=i.pedido_id
            WHERE p.cnpjloja=%s AND LTRIM(i.ean,'0')=LTRIM(%s,'0')
            AND p.status NOT IN ('cancelado','entregue','concluido','estornado','reembolsado')
            AND (p.pagamento_status='approved' OR p.status='pago')
            AND p.criado_em >= (SELECT alterado_em FROM ecommerce_erp_lojas WHERE cnpjloja=%s)
            AND NOT EXISTS(SELECT 1 FROM ecommerce_automatiza_xml x WHERE x.pedido_id=p.id)""", (STORE,mapping["ean"],STORE))
        qty = max(0,qty-int(cur.fetchone()["reserved"]))
        price = mapping.get("preco") if mapping.get("sincronizado_em") and mapping["sincronizado_em"]>=stale_before else None
        result["plano"].append({"id":iid,"ean":mapping["ean"],"estoque":qty,"preco":float(price) if price else None})
        up = item.get("user_product_id")
        if up and up not in updated:
            stock_r = session.get(API+"/user-products/"+up+"/stock",timeout=20)
            if stock_r.status_code == 200:
                stock = stock_r.json()
                locations = stock.get("locations",[])
                version = stock_r.headers.get("x-version")
                if len(locations)==1 and locations[0].get("type")=="selling_address" and (item.get("site_id")=="MLB" or version):
                    if int(locations[0].get("quantity",0)) != qty and apply:
                        # MLB without multi-origin accepts stock through /items.
                        # selling_address writes are enabled only in other sites.
                        if item.get("site_id")=="MLB":
                            response = session.put(API+"/items/"+iid,json={"available_quantity":qty},timeout=25)
                        else:
                            response = session.put(API+"/user-products/"+up+"/stock/type/selling_address",headers={"x-version":version},json={"quantity":qty},timeout=25)
                        if response.status_code not in {200,204}:
                            result["erros"].append({"id":iid,"operacao":"estoque","http":response.status_code})
                        else:
                            result["estoques"]+=1
                    updated.add(up)
                else:
                    result["ignorados"].append({"id":iid,"motivo":"depositos_multiplos_ou_sem_versao"})
            else:
                result["erros"].append({"id":iid,"operacao":"ler_estoque","http":stock_r.status_code})
        if price and Decimal(str(item.get("price",0))) != price and apply:
            response = session.put(API+"/items/"+iid,json={"price":float(price)},timeout=25)
            if response.status_code not in {200,204}:
                result["erros"].append({"id":iid,"operacao":"preco","http":response.status_code})
            else:
                result["precos"]+=1
                cur.execute("UPDATE ml_items SET preco=%s,updated_at=NOW() WHERE ml_item_id=%s AND cnpjloja=%s", (price,iid,STORE))
                conn.commit()
        elif price:
            cur.execute("UPDATE ml_items SET preco=%s WHERE ml_item_id=%s AND cnpjloja=%s AND preco IS DISTINCT FROM %s", (price,iid,STORE,price))
            conn.commit()
    cur.execute("""SELECT x.*,p.ml_order_id,p.ml_shipping_id,p.status FROM ecommerce_automatiza_xml x
        JOIN ecommerce_pedidos p ON p.id=x.pedido_id
        WHERE x.cnpjloja=%s AND x.modelo='55' AND x.ml_enviado_em IS NULL
        AND p.ml_order_id IS NOT NULL AND p.ml_shipping_id IS NOT NULL
        AND p.status NOT IN ('cancelado','estornado','reembolsado')
        AND (x.ml_tentativa_em IS NULL OR x.ml_tentativa_em<NOW()-INTERVAL '5 minutes') LIMIT 20""",(STORE,))
    for invoice in cur.fetchall():
        shipment = str(invoice["ml_shipping_id"])
        r = session.get(API+"/shipments/"+shipment,headers={"x-format-new":"true"},timeout=20)
        if r.status_code != 200 or r.json().get("status") != "ready_to_ship" or r.json().get("substatus") != "invoice_pending":
            continue
        r = session.get(API+"/shipments/"+shipment+"/invoice_data",params={"siteId":"MLB"},timeout=20)
        if r.status_code==200 and r.json().get("fiscal_key"):
            if r.json()["fiscal_key"] != invoice["chave_acesso"]:
                result["erros"].append({"pedido":str(invoice["pedido_id"]),"erro":"nota_ja_existe_divergente"})
            elif apply:
                cur.execute("UPDATE ecommerce_automatiza_xml SET ml_enviado_em=NOW(),ml_erro=NULL WHERE pedido_id=%s",(invoice["pedido_id"],));conn.commit()
            continue
        if r.status_code not in {200,404}:
            continue
        if apply:
            from automatiza_fiscal import authorized_xml
            authorized_xml(invoice["xml_nfe"], STORE)
            response = session.post(API+"/shipments/"+shipment+"/invoice_data/",params={"siteId":"MLB"},headers={"Content-Type":"application/xml"},data=invoice["xml_nfe"].encode("utf-8"),timeout=30)
            ok = response.status_code in {200,201,202,204}
            cur.execute("""UPDATE ecommerce_automatiza_xml SET ml_tentativa_em=NOW(),ml_erro=%s,
                ml_enviado_em=CASE WHEN %s THEN NOW() ELSE ml_enviado_em END WHERE pedido_id=%s""",
                (None if ok else "HTTP "+str(response.status_code),ok,invoice["pedido_id"]))
            conn.commit()
            result["notas"]+=int(ok)
    cur.close()
    result["ok"] = not result["erros"]
    return result


if __name__ == "__main__":
    import json,sys
    from dotenv import load_dotenv
    load_dotenv()
    import app as web
    with web.app.app_context():
        result=sync(web.db,web._ml_get_token,apply="--apply" in sys.argv)
        if "--summary" in sys.argv and "plano" in result:
            result["anuncios_conferidos"]=len(result.pop("plano"))
        print(json.dumps(result,ensure_ascii=True))
