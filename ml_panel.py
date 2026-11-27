"""Seller panel presentation based on current Mercado Envios state."""
def label_state(shipping):
    status = shipping.get("status")
    substatus = shipping.get("substatus")
    ready = bool(shipping.get("id") and status == "ready_to_ship"
                 and substatus in {"ready_to_print", "printed"}
                 and shipping.get("logistic_type") != "fulfillment")
    message = "Etiqueta liberada para baixar" if ready else "Aguardando liberação da etiqueta pelo Mercado Livre"
    if not ready and substatus == "invoice_pending":
        message = "Aguardando nota fiscal e liberação pelo Mercado Livre"
    elif not ready and status in {"shipped", "delivered", "cancelled"}:
        message = "Envio encerrado ou em transporte; etiqueta indisponível"
    elif not ready and substatus == "buffered" and shipping.get("label_release"):
        from datetime import datetime
        try:
            date = datetime.fromisoformat(shipping["label_release"].replace("Z", "+00:00"))
            from zoneinfo import ZoneInfo
            date = date.astimezone(ZoneInfo("America/Sao_Paulo"))
            message = "Liberação prevista pelo ML: " + date.strftime("%d/%m/%Y às %H:%M")
        except (ValueError, TypeError):
            pass
    return {"label_ready": ready, "label_message": message}
