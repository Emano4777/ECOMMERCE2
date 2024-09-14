import os
import requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory
from efipay import EfiPay  
import base64
import psycopg2

app = Flask(__name__)
app.secret_key = 'secret_key'


def get_db_connection():
    conn = psycopg2.connect(
        host="406279.hstgr.cloud",
        database="postgres",
        user="postgres",
        password="Poupaqui123"
    )
    return conn

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


# Função global para buscar medicamentos
def buscar_medicamentos(termo_pesquisa):
    conn = get_db_connection()  # Conexão com o banco de dados
    cursor = conn.cursor()

    # Pesquisa similar utilizando ILIKE para busca de termos semelhantes (case insensitive)
    termo = f"%{termo_pesquisa}%"
    query = """
        SELECT nome, link_imagem, preco
        FROM sugestao
        WHERE nome ILIKE %s
    """
    cursor.execute(query, (termo,))
    medicamentos = cursor.fetchall()

    # Formatar o resultado em uma lista de dicionários
    resultado = [
        {'nome': row[0], 'link_imagem': row[1], 'preco': row[2]} for row in medicamentos
    ]

    cursor.close()
    conn.close()
    
    return resultado

@app.route('/medicamento/<nome>')
def ver_medicamento(nome):
    conn = get_db_connection()
    cursor = conn.cursor()

    # Consultar o medicamento específico pelo nome
    query = """
    SELECT nome, link_imagem, preco, marca, descricao, presmedica
    FROM sugestao
    WHERE nome = %s
    """
    cursor.execute(query, (nome,))
    medicamento = cursor.fetchone()

    cursor.close()
    conn.close()

    if medicamento:
        medicamento_info = {
            'nome': medicamento[0],
            'link_imagem': medicamento[1],
            'preco': medicamento[2],
            'marca': medicamento[3],
            'descricao': medicamento[4],
            'presmedica': medicamento[5]
        }
        return render_template('medicamento.html', medicamento=medicamento_info)
    else:
        return render_template('404.html', mensagem="Medicamento não encontrado.")

def buscar_medicamentos_pagina(termo_pesquisa, medicamentos_pagina):
    # Filtra os medicamentos que estão na página atual, utilizando o termo de pesquisa com ILIKE
    termo = termo_pesquisa.lower()

    medicamentos_filtrados = [
        medicamento for medicamento in medicamentos_pagina
        if termo in medicamento['nome'].lower()
    ]

    return medicamentos_filtrados


@app.route('/filtrar_medicamentos_index', methods=['POST'])
def filtrar_medicamentos_index():
    termo_pesquisa = request.form.get('termo_pesquisa')

    conn = get_db_connection()  # Conexão com o banco de dados
    cursor = conn.cursor()
    query = """
        SELECT nome, link_imagem, preco 
        FROM sugestao 
        WHERE nome ILIKE %s
    """
    cursor.execute(query, (f"%{termo_pesquisa}%",))  # Busca os medicamentos que contenham o termo
    medicamentos = cursor.fetchall()

    cursor.close()
    conn.close()

    # Formatação do resultado em JSON
    medicamentos_formatados = [
        {'nome': row[0], 'link_imagem': row[1], 'preco': row[2]} for row in medicamentos
    ]

    return jsonify({'medicamentos': medicamentos_formatados})



# Rota que retorna os medicamentos filtrados
@app.route('/buscar_medicamentos', methods=['POST'])
def buscar_medicamentos_route():
    termo_pesquisa = request.form.get('termo_pesquisa')
    medicamentos = buscar_medicamentos(termo_pesquisa)
    return jsonify({'medicamentos': medicamentos})

@app.route('/pagar_cartao', methods=['POST'])
def pagar_cartao():
    try:
        # Recebe os dados enviados no corpo da requisição
        data = request.get_json()
        print(f"Dados recebidos do frontend: {data}")

        # Coleta os dados do frontend
        endereco = data['endereco']
        parcelas = int(data['parcelas'])
        valor_total = float(data['total_carrinho'].replace('R$', '').replace(',', '.').strip())
        
        # Aplica juros se o parcelamento for maior que 2x
        juros = 0
        if parcelas > 2:
            juros = 0.02 + (parcelas - 3) * 0.01  # Juros começa em 2% e aumenta 1% por parcela acima de 3x

        valor_com_juros = valor_total * (1 + juros)  # Aplica o juros ao valor total

        # Log para verificação
        print(f"Parcelas: {parcelas}, Juros: {juros * 100}%, Valor com juros: R$ {valor_com_juros:.2f}, Valor total sem juros: R$ {valor_total:.2f}")

        # Token de pagamento gerado no front-end
        payment_token = data['payment_token']
        print(f"Token de pagamento recebido: {payment_token}")

        # Dados do cliente
        customer_data = {
    "name": data.get("nome_completo", ""),  # Usa .get() para evitar KeyError
    "cpf": data.get("cpf_comprador", ""),   # Usa .get() aqui também
    "birth": data.get("birth", ""),
    "phone_number": data.get("phone_number", "11999999999"),  # Certificando que um valor padrão seja atribuído
    "email": data.get("email", "email@exemplo.com"),  # Utiliza .get() para evitar erros se o email estiver ausente
    "address": {
        "street": endereco['rua'],
        "number": endereco['numero'],
        "neighborhood": endereco.get('bairro', 'Centro'),
        "zipcode": endereco['cep'],
        "city": endereco['cidade'],
        "state": endereco['estado'].upper()  # Verifique se está no formato correto
    }
}

        print(f"Dados do cliente: {customer_data}")

        # Chama a função para criar a cobrança no cartão de crédito
        print("Iniciando criação da cobrança...")
        resultado_cobranca = criar_cobranca_cartao(valor_com_juros, payment_token, customer_data, parcelas)
        print(f"Resultado da criação da cobrança: {resultado_cobranca}")

        # Verifica se a cobrança foi criada com sucesso
        if resultado_cobranca.get('code') == 200:
            charge_id = resultado_cobranca['data']['charge_id']
            print(f"Cobrança criada com sucesso: {charge_id}")
            
            # Chama a função para processar o pagamento após a criação da cobrança
            print("Iniciando o pagamento com cartão de crédito...")
            resultado_pagamento = pagar_cartao_credito(charge_id, payment_token, customer_data, parcelas)
            print(f"Resultado do pagamento: {resultado_pagamento}")
            
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


def criar_cobranca_cartao(valor_total, customer_data, payment_token, parcelas):
    """Cria a cobrança no sistema Efipay."""
    try:
        print("Iniciando o processo de obtenção do token...")
        token = obter_token()  # Obtém o access_token
        print(f"Token obtido: {token}")
        
        url = charge_url
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        # Calcula o valor total em centavos
        valor_total_centavos = int(valor_total * 100)

        # Corpo da requisição para criação da cobrança
        body = {
            "items": [
                {
                    "name": "Compra de Medicamentos",
                    "amount": 1,
                    "value": valor_total_centavos  # Envia o valor em centavos
                }
            ],
            "metadata": {
                "custom_id": "ID-1234567890",
                "notification_url": "https://meusite.com.br/notificacoes"
            }
        }

        # Log dos dados da requisição
        print(f"Enviando requisição para criação de cobrança com os seguintes dados: {body}")
        
        response = requests.post(url, headers=headers, json=body)
        
        # Log da resposta
        print(f"Response status code: {response.status_code}")
        print(f"Response body: {response.text}")
        
        # Verifica se a resposta foi bem sucedida
        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"Erro ao criar cobrança: {response.text}")
    
    except Exception as e:
        print(f"Erro durante a criação da cobrança: {str(e)}")
        raise



def pagar_cartao_credito(charge_id, payment_token, customer_data, parcelas):
    url = f"https://cobrancas-h.api.efipay.com.br/v1/charge/{charge_id}/pay"
    
    headers = {
        "Authorization": f"Bearer {obter_token()}",
        "Content-Type": "application/json"
    }
    
    body = {
        "payment": {
            "credit_card": {
                "payment_token": payment_token,
                "billing_address": customer_data["address"],
                "installments": int(parcelas),
                "customer": {
                    "name": customer_data["name"],
                    "cpf": customer_data["cpf"],
                    "birth": customer_data["birth"],
                    "phone_number": customer_data.get("phone_number", ""),
                    "email": customer_data.get("email", "")
                }
            }
        }
    }

    # Logs adicionais antes da requisição
    print(f"Iniciando o pagamento para charge_id: {charge_id} com token: {payment_token}")
    print(f"Enviando dados para o pagamento com cartão: {body}")    
    response = requests.post(url, headers=headers, json=body)
    print(f"Response status code (pagamento): {response.status_code}")
    print(f"Response body (pagamento): {response.text}")

    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"Erro ao processar pagamento: {response.text}")

@app.route('/medicamentos')
def index():
    conn = get_db_connection()  # Obtenha a conexão com o banco de dados
    cursor = conn.cursor()  # Crie um cursor a partir da conexão
    cursor.execute("SELECT nome, link_imagem, preco FROM sugestao WHERE link_imagem IS NOT NULL")
    medicamentos = cursor.fetchall()
     
    # Transforme o resultado da consulta em uma lista de dicionários
    medicamentos = [
        {'nome': row[0], 'link_imagem': row[1], 'preco': row[2]} for row in medicamentos
    ]

    cursor.close()  # Feche o cursor
    conn.close()  # Feche a conexão

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
    
@app.route('/log-click', methods=['POST'])
def log_click():
    data = request.json
    message = data.get('message', 'Nenhuma mensagem enviada')
    
    # Registrar a mensagem no terminal
    print(f"Log de clique recebido: {message}")
    
    return jsonify({"message": "Log recebido com sucesso!"}), 200

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
