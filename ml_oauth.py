"""Small, secret-free helpers for the Mercado Livre authorization flow."""
import json

DEFAULT_REDIRECT_URI = "https://www.drogariaspoupaqui.com.br/ml/callback"


def redirect_uri(value):
    return (value or "").strip() or DEFAULT_REDIRECT_URI


def token_error(raw_body):
    """Return an allowlisted code/message, never echo provider bodies or grants."""
    try:
        data = json.loads(raw_body)
        code = data.get("error") if isinstance(data, dict) else None
    except (ValueError, TypeError):
        code = None
    messages = {
        "invalid_grant": "A autorização expirou ou foi recusada pelo Mercado Livre. Clique em Conectar conta Mercado Livre para iniciar uma nova autorização.",
        "invalid_operator_user_id": "Entre no Mercado Livre com a conta titular do vendedor, e não com um acesso de colaborador, e tente conectar novamente.",
        "invalid_client": "Não foi possível validar o aplicativo da integração. Entre em contato com o suporte Poupaqui.",
        "invalid_request": "O Mercado Livre recusou os parâmetros da autorização. Inicie uma nova conexão pelo painel; se persistir, contate o suporte Poupaqui.",
        "unauthorized_application": "O aplicativo da integração foi bloqueado pelo Mercado Livre. Entre em contato com o suporte Poupaqui.",
        "unauthorized_client": "O Mercado Livre não autorizou este aplicativo para a conta. Tente conectar novamente com a conta titular do vendedor.",
    }
    if code not in messages:
        code = "unknown"
    return code, messages.get(code, "Não foi possível concluir a autorização no Mercado Livre. Tente conectar novamente pelo painel da loja.")
