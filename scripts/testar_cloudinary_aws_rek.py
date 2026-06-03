"""
testar_cloudinary_aws_rek.py — Verifica se o AWS Rekognition AI Tagging está habilitado
no Cloudinary e testa detecção de pessoa em imagens.

Uso:
    py scripts/testar_cloudinary_aws_rek.py
    py scripts/testar_cloudinary_aws_rek.py --url <url_com_pessoa>
    py scripts/testar_cloudinary_aws_rek.py --url <url_com_pessoa> --url-produto <url_produto_limpo>
"""
from __future__ import annotations
import argparse
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_PERSON_LABELS = {
    "person", "human", "people", "man", "woman", "boy", "girl", "adult",
    "hand", "arm", "finger", "body", "face", "head", "shoulder",
}


def _configurar_cloudinary():
    try:
        import cloudinary
        import cloudinary.uploader
    except ImportError:
        print("[ERRO] Módulo cloudinary não instalado. Execute: pip install cloudinary")
        return None, None

    cloud = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
    key   = os.getenv("CLOUDINARY_API_KEY", "").strip()
    sec   = os.getenv("CLOUDINARY_API_SECRET", "").strip()

    if not all([cloud, key, sec]):
        print("[ERRO] Variáveis CLOUDINARY_CLOUD_NAME / CLOUDINARY_API_KEY / CLOUDINARY_API_SECRET não configuradas no .env")
        return None, None

    cloudinary.config(cloud_name=cloud, api_key=key, api_secret=sec)
    print(f"[OK] Cloudinary configurado: cloud={cloud!r}")
    return cloudinary, cloudinary.uploader


def _download(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except Exception as exc:
        print(f"[ERRO] Download falhou: {exc}")
        return None


def _testar_imagem(cloudinary, uploader, url: str, label: str, expect_person: bool):
    print(f"\n--- {label} ---")
    print(f"URL: {url[:80]}")

    img_bytes = _download(url)
    if not img_bytes:
        print("[ERRO] Não foi possível baixar a imagem.")
        return

    print(f"Baixou {len(img_bytes):,} bytes. Enviando para Cloudinary com AWS Rekognition...")

    try:
        result = uploader.upload(
            img_bytes,
            public_id=f"_teste_aws_rek_{label.lower().replace(' ', '_')}",
            folder="catalogo_produtos/_testes",
            resource_type="image",
            overwrite=True,
            unique_filename=False,
            faces=True,                         # detecção built-in de rostos
        )
    except Exception as exc:
        print(f"[ERRO] Upload Cloudinary falhou: {exc}")
        return

    # ── Faces (built-in, sem add-on) ─────────────────────────────────────────
    faces = result.get("faces", [])
    print(f"faces detectados: {len(faces)}")
    if faces:
        print(f"  coordenadas: {faces}")

    # ── Info raw (debug) ─────────────────────────────────────────────────────
    info = result.get("info", {})
    print(f"info keys: {list(info.keys())}")

    # ── AWS Rekognition tags ──────────────────────────────────────────────────
    rek_data = (
        result.get("info", {})
        .get("categorization", {})
        .get("aws_rek_tagging", {})
    )
    rek_status = rek_data.get("status", "não retornado")
    rek_tags   = rek_data.get("data", [])

    print(f"aws_rek_tagging status : {rek_status!r}")

    if not rek_tags:
        print("[AVISO] Nenhuma tag retornada.")
        print("        Ative um add-on de tagging no painel Cloudinary > Add-ons:")
        print("        - Imagga Auto Tagging (imagga_tagging)")
        print("        - AWS Rekognition AI Tagging (aws_rek_tagging)")
        print("        - Google Auto Tagging (google_tagging)")
    else:
        print(f"Tags recebidas ({len(rek_tags)}):")
        for t in sorted(rek_tags, key=lambda x: -x.get("confidence", 0)):
            tag  = t.get("tag", "")
            conf = t.get("confidence", 0)
            flag = " ← PESSOA" if tag.lower() in _PERSON_LABELS else ""
            print(f"  {conf:5.1f}%  {tag}{flag}")

    # ── Avalia resultado ─────────────────────────────────────────────────────
    has_person = (
        bool(faces)
        or any(
            t.get("tag", "").lower() in _PERSON_LABELS and t.get("confidence", 0) >= 70
            for t in rek_tags
        )
    )

    if expect_person:
        verdict = "[PASSOU] pessoa detectada corretamente" if has_person else "[FALHOU] pessoa NÃO detectada"
    else:
        verdict = "[PASSOU] imagem limpa sem pessoa" if not has_person else "[FALHOU] falso positivo (pessoa detectada em imagem limpa)"

    print(f"\nResultado: {verdict}")

    # Limpa a imagem de teste do Cloudinary
    pub_id = result.get("public_id", "")
    if pub_id:
        try:
            cloudinary.uploader.destroy(pub_id)
            print(f"(imagem de teste removida do Cloudinary: {pub_id})")
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Testa AWS Rekognition no Cloudinary")
    parser.add_argument("--url", metavar="URL_PESSOA",
                        help="URL de imagem COM pessoa/mão (espera detecção positiva)")
    parser.add_argument("--url-produto", metavar="URL_PRODUTO",
                        help="URL de imagem LIMPA de produto (espera detecção negativa)")
    args = parser.parse_args()

    cloudinary_mod, uploader = _configurar_cloudinary()
    if cloudinary_mod is None:
        sys.exit(1)

    # Imagem limpa padrão: produto de embalagem fechada (Aspirina do catálogo se disponível)
    url_produto_default = (
        "https://principia.vteximg.com.br/arquivos/ids/168625/AAS_500mg_30cp_Principia.jpg"
    )
    url_pessoa_default = args.url  # requer --url

    if args.url_produto or url_produto_default:
        _testar_imagem(
            cloudinary_mod, uploader,
            args.url_produto or url_produto_default,
            label="produto_limpo",
            expect_person=False,
        )

    if args.url:
        _testar_imagem(
            cloudinary_mod, uploader,
            args.url,
            label="imagem_com_pessoa",
            expect_person=True,
        )
    else:
        print("\n[INFO] Para testar detecção de pessoa, passe --url <url_com_pessoa>")
        print("       Exemplo: py scripts/testar_cloudinary_aws_rek.py --url https://exemplo.com/foto_com_mao.jpg")


if __name__ == "__main__":
    main()
