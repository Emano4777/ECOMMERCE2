from flask import Flask, render_template

app = Flask(__name__)

# Tela principal
@app.route('/')
def index():
    medicamentos = [
        {'nome': 'Medicamento A', 'preco': 'R$ 49,99', 'imagem': '/static/img/medicamentoA.png'},
        {'nome': 'Medicamento B', 'preco': 'R$ 29,99', 'imagem': '/static/img/medicamentoB.png'}
    ]
    return render_template('index.html', medicamentos=medicamentos)

if __name__ == '__main__':
    app.run(debug=True)
