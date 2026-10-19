"""Projection of customer contacts for store-facing pages and emails."""
from collections.abc import Mapping

_PHONE_FIELDS = frozenset({
    "telefone", "whatsapp", "cliente_telefone", "consumidor_telefone",
    "cliente_whatsapp", "consumidor_whatsapp", "celular", "phone",
    "cliente_key",  # report fallback identity can contain the phone
})

def filter_customer_contacts(value, allowed):
    if allowed:
        return value
    if isinstance(value, Mapping):
        return {key: ("" if key.lower() in _PHONE_FIELDS else filter_customer_contacts(item, False))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [filter_customer_contacts(item, False) for item in value]
    return value
