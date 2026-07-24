"""
Captura screenshots automáticos do Poupaqui Ecommerce para a proposta comercial.
"""
import os, time
from playwright.sync_api import sync_playwright

BASE = "https://www.drogariaspoupaqui.com.br"
OUT  = r"c:\Users\emano\dns-ecommerce\screenshots"
os.makedirs(OUT, exist_ok=True)

def shot(page, path, full=False):
    page.wait_for_load_state("networkidle")
    time.sleep(1.2)
    page.screenshot(path=path, full_page=full)
    print(f"  OK  {os.path.basename(path)}")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    # ── viewport largo (desktop) ─────────────────────────────────────────
    ctx = browser.new_context(
        viewport={"width": 1400, "height": 820},
        device_scale_factor=1.5,
    )
    page = ctx.new_page()

    # 1. HOME ──────────────────────────────────────────────────────────────
    print("Capturando home...")
    page.goto(BASE, wait_until="networkidle", timeout=20000)
    # fecha popup de localização se aparecer
    try:
        page.locator("text=Usar minha localização").wait_for(timeout=3000)
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    time.sleep(0.8)
    page.evaluate("window.scrollTo(0,0)")
    shot(page, os.path.join(OUT, "01_home.png"))

    # 2. CATÁLOGO ──────────────────────────────────────────────────────────
    print("Capturando catálogo...")
    page.goto(f"{BASE}/catalogo", wait_until="networkidle", timeout=20000)
    shot(page, os.path.join(OUT, "02_catalogo.png"))

    # 3. MARCA PRÓPRIA (Vitnatu) ───────────────────────────────────────────
    print("Capturando marca própria...")
    page.goto(f"{BASE}/vitnatu", wait_until="networkidle", timeout=20000)
    shot(page, os.path.join(OUT, "03_marca_propria.png"))

    # 4. LOJAS ─────────────────────────────────────────────────────────────
    print("Capturando lojas...")
    page.goto(f"{BASE}/lojas", wait_until="networkidle", timeout=20000)
    shot(page, os.path.join(OUT, "04_lojas.png"))

    # 5. CARRINHO (navega para catálogo e adiciona produto) ────────────────
    print("Capturando carrinho...")
    page.goto(f"{BASE}/carrinho", wait_until="networkidle", timeout=20000)
    shot(page, os.path.join(OUT, "05_carrinho.png"))

    # 6. PRODUTO DETALHE ───────────────────────────────────────────────────
    print("Capturando produto detalhe...")
    # pega o catálogo e clica no primeiro produto
    page.goto(f"{BASE}/catalogo", wait_until="networkidle", timeout=20000)
    time.sleep(1)
    try:
        first_card = page.locator("a[href*='/produto/']").first
        href = first_card.get_attribute("href")
        if href:
            page.goto(f"{BASE}{href}", wait_until="networkidle", timeout=15000)
            shot(page, os.path.join(OUT, "06_produto.png"))
        else:
            raise Exception("no href")
    except Exception as e:
        print(f"  ! produto detalhe: {e}")
        page.screenshot(path=os.path.join(OUT, "06_produto.png"))

    ctx.close()

    # ── PAINEL (dark, login necessário) ───────────────────────────────────
    # Usa viewport um pouco menor para caber mais na tela
    ctx2 = browser.new_context(
        viewport={"width": 1400, "height": 820},
        device_scale_factor=1.5,
    )
    page2 = ctx2.new_page()
    print("Capturando painel...")
    page2.goto(f"{BASE}/painel/login", wait_until="networkidle", timeout=20000)
    shot(page2, os.path.join(OUT, "07_painel_login.png"))

    # tenta logar com a loja de teste (ajuste se necessário)
    # Se não tiver credenciais, captura só a tela de login
    ctx2.close()
    browser.close()

print("\nScreenshots salvos em:", OUT)
print("Arquivos:")
for f in sorted(os.listdir(OUT)):
    sz = os.path.getsize(os.path.join(OUT, f))
    print(f"  {f}  ({sz//1024} KB)")
