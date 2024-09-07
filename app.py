from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory

app = Flask(__name__)
app.secret_key = 'secret_key'

# Rota principal de medicamentos
@app.route('/medicamentos')
def index():
    medicamentos = [
        {'nome': 'Medicamento A', 'preco': '49.99', 'imagem': '/static/img/medicamentoA.png'},
        {'nome': 'Medicamento B', 'preco': '29.99', 'imagem': '/static/img/medicamentoB.png'}
    ]
    return render_template('index.html', medicamentos=medicamentos)

# Rota para a página do carrinho
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

# Rota para editar item no carrinho
@app.route('/edit_item/<item_nome>', methods=['POST'])
def edit_item(item_nome):
    nova_quantidade = int(request.form.get('quantidade'))
    carrinho = session.get('carrinho', [])
    
    # Atualiza a quantidade do item no carrinho
    for item in carrinho:
        if item['nome'] == item_nome:
            item['quantidade'] = nova_quantidade
            preco_float = float(item['preco'].replace('R$', '').replace(',', '.').strip())
            item['total'] = f'R$ {preco_float * nova_quantidade:.2f}'

    session['carrinho'] = carrinho
    return redirect(url_for('cart'))

# Rota para excluir item do carrinho
@app.route('/delete_item/<item_nome>', methods=['POST'])
def delete_item(item_nome):
    carrinho = session.get('carrinho', [])
    
    # Remove o item do carrinho
    carrinho = [item for item in carrinho if item['nome'] != item_nome]
    
    session['carrinho'] = carrinho
    return redirect(url_for('cart'))

# Rota para download de arquivos de vídeo
@app.route('/videos/<path:filename>')
def download_file(filename):
    return send_from_directory('static/videos', filename)

# Rota para a página de início
@app.route('/')
def inicio():
    return render_template('inicio.html')

# Função para calcular o valor total do carrinho
def calcular_total_carrinho(carrinho):
    total = sum(float(item['total'].replace('R$', '').replace(',', '.')) for item in carrinho)
    return f'R$ {total:.2f}'

if __name__ == '__main__':
    app.run(debug=True)
