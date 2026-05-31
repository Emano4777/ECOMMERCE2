"""
Upload imagens Vitnatu do Drive local para Cloudinary e insere EANs no banco.
"""
import os
import cloudinary
import cloudinary.uploader
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

cloudinary.config(
    cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
    api_key=os.environ["CLOUDINARY_API_KEY"],
    api_secret=os.environ["CLOUDINARY_API_SECRET"],
)

DB_URL = os.environ["DATABASE_URL"]

# Pasta local com as imagens extraídas
PASTA = r"C:\Users\emano\Downloads\PRODUTOS_VITNATU\PRODUTOS - VITNATU"

# Mapeamento: nome_do_arquivo (sem extensão, case-insensitive) → lista de EANs
MAPA = {
    "calm":                ["7898638348204"],
    "silimarina":          ["7898618986136", "7898722820616"],
    "multi az":            ["7898638341380"],
    "mulher novo":         ["7898638341397"],   # preferir novo se existir
    "mulher":              ["7898638341397"],
    "senior new":          ["7898638341403"],    # preferir new se existir
    "senior":              ["7898638341403"],
    "ginkgo biloba":       ["7898722820647", "7898722820630", "7896545687119"],
    "hialuronico":         ["7898722820555"],
    "amora miura":         ["7898722820562"],
    "carvão vegetal":      ["7898722821637"],
    "hepafort":            ["7898656167887"],
    "immune":              ["7898638349409"],
    "ora pro nobis":       ["7898722821644"],
    "hair":                ["7898716453103"],
    "dimalato":            ["7898638344003"],    # Magnesio Dimalato
    "cinco magnesios":     ["7898722821620"],
    "metil cobalamina":    ["7898920633681"],
    "omega pack":          ["40141777959"],
}

def upload_e_insere():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # EANs já com imagem — não sobrescrever
    cur.execute("SELECT ean FROM vitnatu_imagens WHERE imagem_url IS NOT NULL")
    ja_tem = {r["ean"] for r in cur.fetchall()}

    # Coletar arquivos disponíveis
    arquivos = {}
    for fname in os.listdir(PASTA):
        nome_sem_ext = os.path.splitext(fname)[0].strip().lower()
        arquivos[nome_sem_ext] = os.path.join(PASTA, fname)

    inseridos = []

    for chave, eans in MAPA.items():
        # Encontrar arquivo correspondente (match exato por chave)
        caminho = arquivos.get(chave)
        if not caminho:
            print(f"[SKIP] Arquivo não encontrado para '{chave}'")
            continue

        eans_pendentes = [e for e in eans if e not in ja_tem]
        if not eans_pendentes:
            print(f"[SKIP] Todos EANs de '{chave}' já têm imagem")
            continue

        print(f"[UPLOAD] {os.path.basename(caminho)} - EANs: {eans_pendentes}")
        try:
            result = cloudinary.uploader.upload(
                caminho,
                folder="vitnatu",
                use_filename=True,
                unique_filename=True,
                overwrite=False,
            )
            url = result["secure_url"]
            print(f"         URL: {url}")
            for ean in eans_pendentes:
                inseridos.append((ean, url))
                ja_tem.add(ean)
        except Exception as e:
            print(f"[ERRO] Upload de '{chave}': {e}")

    if inseridos:
        cur.executemany(
            "INSERT INTO vitnatu_imagens (ean, imagem_url) VALUES (%s, %s) ON CONFLICT (ean) DO NOTHING",
            inseridos,
        )
        conn.commit()
        print(f"\n✓ {len(inseridos)} registros inseridos no banco.")
    else:
        print("\nNenhum novo registro para inserir.")

    cur.close()
    conn.close()

if __name__ == "__main__":
    upload_e_insere()
