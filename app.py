from flask import Flask, render_template,send_from_directory

app = Flask(__name__)

# Tela principal
@app.route('/medicamentos')
def index():
    medicamentos = [
        {'nome': 'Medicamento A', 'preco': 'R$ 49,99', 'imagem': '/static/img/medicamentoA.png'},
        {'nome': 'Medicamento B', 'preco': 'R$ 29,99', 'imagem': '/static/img/medicamentoB.png'}
    ]
    return render_template('index.html', medicamentos=medicamentos)

@app.route('/videos/<path:filename>')
def download_file(filename):
    return send_from_directory('static/videos', filename)

# Rota para a página de início
@app.route('/')
def inicio():
    return render_template('inicio.html')


if __name__ == '__main__':
    app.run(debug=True)
