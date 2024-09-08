import os
import requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory
from efipay import EfiPay  
import base64


app = Flask(__name__)
app.secret_key = 'secret_key'

# Credenciais fornecidas
# Configuração das credenciais
# Credenciais do cliente
credentials = {
    "client_id": "Client_Id_1750657c283e7e37986ec9cf79b971bc6f7e1b7d",  # Substitua pelo seu Client ID
    "client_secret": "Client_Secret_53e11a50d2a03c03f917e9878c11d31e46bfd8ff"  # Substitua pelo seu Client Secret
}

# Instância do cliente Efipay
efi = EfiPay(credentials)
# Definir o ambiente (True para homologação, False para produção)
sandbox = True  # Alterar para False quando estiver em produção


## Gera o token de autorização base64
# Gera o token de autorização base64
auth = base64.b64encode(f"{credentials['client_id']}:{credentials['client_secret']}".encode()).decode()

# URL de autenticação para o ambiente de desenvolvimento (sandbox)
auth_url = "https://cobrancas-h.api.efipay.com.br/v1/authorize"
charge_url = "https://cobrancas-h.api.efipay.com.br/v1/charge"
  # URL correta para o token

# Payload deve ser uma string JSON
payload = "{\r\n    \"grant_type\": \"client_credentials\"\r\n}"

# Cabeçalhos corretos
headers = {
    'Authorization': f"Basic {auth}",
    'Content-Type': 'application/json'
}

# Faz a requisição POST
response = requests.post(auth_url, headers=headers, data=payload)

# Imprime o resultado para verificar o que está retornando
print("Response:", response.text)

if response.status_code == 200:
    access_token = response.json()["access_token"]
    print("Access Token:", access_token)
else:
    raise Exception(f"Erro ao obter token de acesso: {response.text}")



def obter_token():
    """Obtém o token de acesso OAuth."""
    url = "https://cobrancas-h.api.efipay.com.br/v1/authorize"  # URL correta para o token de acesso
    headers = {
        'Authorization': f"Basic {auth}",
        'Content-Type': 'application/json'  # Certifique-se de usar application/json
    }
    data = {
        "grant_type": "client_credentials"
    }

    response = requests.post(url, headers=headers, data=payload)
    if response.status_code == 200:
        return response.json()["access_token"]
    else:
        print(f"Erro ao obter token de acesso: {response.text}")
        raise Exception(f"Erro ao obter token de acesso: {response.text}")




@app.route('/pagar_cartao', methods=['POST'])
def pagar_cartao():
    try:
        # Recebe os dados enviados no corpo da requisição
        data = request.get_json()

        # Adicionando logs para verificar os dados recebidos
        print(f"Dados recebidos do frontend: {data}")

        # Dados do cliente
        customer_data = {
            "name": data["nome_completo"],
            "cpf": data["cpf_comprador"],
            "birth": data["birth"],
            "phone_number": data["phone_number"],  
            "email": data["email"],  # Inclui o email
            "address": {
                "street": "Rua Exemplo",
                "number": "123",
                "neighborhood": "Centro",
                "zipcode": "12345678",
                "city": "São Paulo",
                "state": "SP"
            }
        }

        # Token de pagamento gerado no front-end
        payment_token = data['payment_token']
        valor_total = data['total_carrinho']

        # Log dos dados recebidos do frontend
        print(f"Token de pagamento recebido: {payment_token}")
        print(f"Valor total do carrinho: {valor_total}")

        # Chama a função para criar a cobrança no cartão de crédito
        print("Iniciando criação da cobrança...")
        resultado_cobranca = criar_cobranca_cartao(valor_total, payment_token, customer_data)

        # Verifica se a cobrança foi criada com sucesso
        if resultado_cobranca.get('code') == 200:
            charge_id = resultado_cobranca['data']['charge_id']
            print(f"Cobrança criada com sucesso: {charge_id}")
            
            # Chama a função para processar o pagamento após a criação da cobrança
            print("Iniciando o pagamento com cartão de crédito...")
            resultado_pagamento = pagar_cartao_credito(charge_id, payment_token, customer_data)
            
            if resultado_pagamento['data']['status'] == 'approved':
             return jsonify({"message": "Pagamento efetuado com sucesso!"})
            else:
             print(f"Erro no pagamento: {resultado_pagamento}")
             return jsonify({"error": "Erro ao processar o pagamento."}), 400
        else:
            print(f"Erro ao criar a cobrança: {resultado_cobranca}")
            return jsonify({"error": "Erro ao criar a cobrança."}), 400

    except Exception as e:
        print(f"Erro no processo de pagamento: {str(e)}")
        return jsonify({"error": str(e)}), 500


def gerar_payment_token(card_data):
    """Gera o token de pagamento usando os dados do cartão."""
    body = {
        "card_number": card_data['number'],
        "cvv": card_data['cvv'],
        "expiration_month": card_data['expiration_month'],
        "expiration_year": card_data['expiration_year'],
        "card_holder_name": card_data['nome_completo']
    }

    response = efi.create_payment_token(body=body)
    
    if response['status'] == 'success':
        return response['data']['payment_token']
    else:
        raise Exception(f"Erro ao gerar token de pagamento: {response['errors']}")


def criar_cobranca_cartao(valor_total, payment_token, customer_data):
    """Cria a cobrança no cartão de crédito com logs adicionais."""
    try:
        # Log antes de obter o token
        print("Iniciando o processo de obtenção do token...")

        token = obter_token()  # Obtém o access_token

        # Log após a obtenção do token
        print(f"Token obtido com sucesso: {token}")

        url = charge_url

        headers = {
            "Authorization": f"Bearer {token}",  # Usa o token de acesso aqui
            "Content-Type": "application/json"
        }

        # Remover "R$" e converter a vírgula em ponto para o valor_total
        valor_total_float = float(valor_total.replace('R$', '').replace(',', '.').strip())
        valor_total_centavos = int(valor_total_float * 100)  # Convertendo para centavos

        # Corpo da requisição de acordo com a documentação
        body = {
            "items": [
                {
                    "name": "Compra de Medicamentos",  # Nome do item
                    "amount": 1,  # Quantidade de itens
                    "value": valor_total_centavos  # Agora em centavos
                }
            ],
            "shippings": [
                {
                    "name": "Entrega Padrão",  # Rótulo do frete (opcional)
                    "value": 1500  # Valor do frete em centavos (opcional)
                }
            ],
            "metadata": {
                "custom_id": "ID-1234567890",  # ID opcional para associar a transação
                "notification_url": "https://meusite.com.br/notificacoes"  # URL de notificação opcional
            }
        }

        # Log antes da requisição
        print(f"Enviando dados do pagamento: {body}")
        print(f"Headers: {headers}")
        print(f"URL: {url}")

        # Faz a requisição POST
        response = requests.post(url, headers=headers, json=body)

        # Log após a resposta
        print(f"Response status code: {response.status_code}")
        print(f"Response body: {response.text}")

        if response.status_code == 200:
            print("Cobrança criada com sucesso, retornando dados...")
            return response.json()
        else:
            raise Exception(f"Erro ao criar cobrança: {response.text}")
    
    except Exception as e:
        # Log de erro detalhado
        print(f"Erro durante a criação da cobrança: {str(e)}")
        raise


def criar_cobranca_cartao(valor_total, payment_token, customer_data):
    """Cria a cobrança no cartão de crédito."""
    try:
        # Log antes de obter o token
        print("Iniciando o processo de obtenção do token...")

        token = obter_token()  # Obtém o access_token

        # Log após a obtenção do token
        print(f"Token obtido com sucesso: {token}")

        url = charge_url

        headers = {
            "Authorization": f"Bearer {token}",  # Usa o token de acesso aqui
            "Content-Type": "application/json"
        }

        # Remover "R$" e converter a vírgula em ponto para o valor_total
        valor_total_float = float(valor_total.replace('R$', '').replace(',', '.').strip())
        valor_total_centavos = int(valor_total_float * 100)  # Convertendo para centavos

        # Corpo da requisição de acordo com a documentação
        body = {
            "items": [
                {
                    "name": "Compra de Medicamentos",  # Nome do item
                    "amount": 1,  # Quantidade de itens
                    "value": valor_total_centavos  # Agora em centavos
                }
            ],
            "shippings": [
                {
                    "name": "Entrega Padrão",  # Rótulo do frete (opcional)
                    "value": 1500  # Valor do frete em centavos (opcional)
                }
            ],
            "metadata": {
                "custom_id": "ID-1234567890",  # ID opcional para associar a transação
                "notification_url": "https://meusite.com.br/notificacoes"  # URL de notificação opcional
            }
        }

        # Log antes da requisição
        print(f"Enviando dados do pagamento: {body}")
        print(f"Headers: {headers}")
        print(f"URL: {url}")

        # Faz a requisição POST
        response = requests.post(url, headers=headers, json=body)

        # Log após a resposta
        print(f"Response status code (criação da cobrança): {response.status_code}")
        print(f"Response body (criação da cobrança): {response.text}")

        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"Erro ao criar cobrança: {response.text}")
    
    except Exception as e:
        # Log de erro detalhado
        print(f"Erro durante a criação da cobrança: {str(e)}")
        raise


def pagar_cartao_credito(charge_id, payment_token, customer_data):
    url = f"https://cobrancas-h.api.efipay.com.br/v1/charge/{charge_id}/pay"
    
    headers = {
        "Authorization": f"Bearer {obter_token()}",
        "Content-Type": "application/json"
    }
    
    # Adicionando o campo 'birth' (data de nascimento) no corpo da requisição
    body = {
        "payment": {
            "credit_card": {
                "payment_token": payment_token,
                "billing_address": customer_data["address"],
                "customer": {
                    "name": customer_data["name"],
                    "cpf": customer_data["cpf"],
                    "birth": customer_data["birth"],  # Adiciona a data de nascimento
                    "phone_number": customer_data.get("phone_number", ""),
                    "email": customer_data.get("email", "")
                }
            }
        }
    }

    # Logs adicionais antes da requisição
    print(f"Iniciando o pagamento para charge_id: {charge_id} com token: {payment_token}")
    print(f"Enviando dados para o pagamento com cartão: {body}")    
    print(f"Headers: {headers}")
    print(f"URL de pagamento: {url}")

    response = requests.post(url, headers=headers, json=body)

    # Logs após a requisição
    print(f"Response status code (pagamento): {response.status_code}")
    print(f"Response body (pagamento): {response.text}")

    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"Erro ao processar pagamento: {response.text}")






@app.route('/medicamentos')
def index():
    medicamentos = [
        {'nome': 'Medicamento A', 'preco': '49.99', 'imagem': '/static/img/medicamentoA.png'},
        {'nome': 'Medicamento B', 'preco': '29.99', 'imagem': '/static/img/medicamentoB.png'}
    ]
    return render_template('index.html', medicamentos=medicamentos)



@app.route('/cart', methods=['GET', 'POST'])
def cart():
    if request.method == 'POST':
        carrinho = []
        nomes = request.form.getlist('nome')
        precos = request.form.getlist('preco')
        quantidades = request.form.getlist('quantidade')

        for nome, preco, quantidade in zip(nomes, precos, quantidades):
            preco_float = float(preco.replace('R$', '').replace(',', '.').strip())
            quantidade_int = int(quantidade)
            total = preco_float * quantidade_int

            carrinho.append({
                'nome': nome,
                'preco': f'R$ {preco_float:.2f}',
                'quantidade': quantidade_int,
                'total': f'R$ {total:.2f}'
            })

        session['carrinho'] = carrinho
        total_carrinho = calcular_total_carrinho(carrinho)
        return render_template('cart.html', carrinho=carrinho, total_carrinho=total_carrinho)
    else:
        carrinho = session.get('carrinho', [])
        total_carrinho = calcular_total_carrinho(carrinho)
        return render_template('cart.html', carrinho=carrinho, total_carrinho=total_carrinho)

@app.route('/edit_item/<item_nome>', methods=['POST'])
def edit_item(item_nome):
    nova_quantidade = int(request.form.get('quantidade'))
    carrinho = session.get('carrinho', [])

    for item in carrinho:
        if item['nome'] == item_nome:
            item['quantidade'] = nova_quantidade
            preco_float = float(item['preco'].replace('R$', '').replace(',', '.').strip())
            item['total'] = f'R$ {preco_float * nova_quantidade:.2f}'

    session['carrinho'] = carrinho
    return redirect(url_for('cart'))

@app.route('/delete_item/<item_nome>', methods=['POST'])
def delete_item(item_nome):
    carrinho = session.get('carrinho', [])
    carrinho = [item for item in carrinho if item['nome'] != item_nome]
    session['carrinho'] = carrinho
    return redirect(url_for('cart'))

@app.route('/videos/<path:filename>')
def download_file(filename):
    return send_from_directory('static/videos', filename)

@app.route('/')
def inicio():
    return render_template('inicio.html')

def calcular_total_carrinho(carrinho):
    total = sum(float(item['total'].replace('R$', '').replace(',', '.')) for item in carrinho)
    return f'R$ {total:.2f}'

if __name__ == '__main__':
    app.run(debug=True)
