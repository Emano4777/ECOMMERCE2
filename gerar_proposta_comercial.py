"""
Gerador da Proposta Comercial Poupaqui Ecommerce - v2
Melhorias: layout corrigido, graficos, TOC clicavel, secao de screenshots
"""
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether, Image
)
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Group
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics import renderPDF
from reportlab.pdfgen import canvas
from reportlab.platypus.flowables import Flowable

W, H = A4

# ── PALETA ──────────────────────────────────────────────────────────────────
RED    = colors.HexColor("#c8102e")
YELLOW = colors.HexColor("#f5c842")
DARK   = colors.HexColor("#1a1a2e")
GRAY   = colors.HexColor("#4a4a4a")
LGRAY  = colors.HexColor("#f5f5f5")
MGRAY  = colors.HexColor("#999999")
WHITE  = colors.white
BORDG  = colors.HexColor("#e0e0e0")
GREEN  = colors.HexColor("#2e7d32")
DKGRAY = colors.HexColor("#2a2a2a")
AMBER  = colors.HexColor("#ff8f00")

SCREENSHOTS = r"c:\Users\emano\dns-ecommerce\screenshots"
SS_MAP = {
    "home":          "01_home.png",
    "catalogo":      "02_catalogo.png",
    "marca_propria": "03_marca_propria.png",
    "lojas":         "04_lojas.png",
    "carrinho":      "05_carrinho.png",
    "produto":       "06_produto.png",
    "painel":        "07_painel_login.png",
}


def ss(key):
    """Retorna caminho do screenshot ou None."""
    p = os.path.join(SCREENSHOTS, SS_MAP.get(key, ""))
    return p if os.path.exists(p) else None


# ── BOOKMARK FLOWABLE ────────────────────────────────────────────────────────
class NamedAnchor(Flowable):
    """Cria destino de link PDF + entrada no outline (sidebar do leitor)."""
    def __init__(self, name, outline_title=None, level=0):
        self.name = name
        self.outline_title = outline_title
        self.level = level
        self.width = 0
        self.height = 0

    def draw(self):
        self.canv.bookmarkPage(self.name)
        if self.outline_title:
            self.canv.addOutlineEntry(self.outline_title, self.name, level=self.level, closed=False)


# ── CANVAS COM RODAPÉ ────────────────────────────────────────────────────────
class BrandedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        n = len(self._saved)
        for st in self._saved:
            self.__dict__.update(st)
            pg = self._pageNumber
            self.saveState()
            self.setFillColor(RED)
            self.rect(0, 0, W, 1.0 * cm, fill=1, stroke=0)
            self.setFillColor(WHITE)
            self.setFont("Helvetica", 7.5)
            self.drawString(1.5 * cm, 0.33 * cm, "Poupaqui Ecommerce — Proposta Comercial Confidencial")
            self.drawRightString(W - 1.5 * cm, 0.33 * cm, f"Página {pg} de {n}")
            self.restoreState()
            super().showPage()
        super().save()


# ── ESTILOS ──────────────────────────────────────────────────────────────────
def make_styles():
    def s(name, **kw):
        return ParagraphStyle(name, **kw)
    return {
        "cover_title": s("ct", fontName="Helvetica-Bold", fontSize=36, leading=42,
                         textColor=WHITE, alignment=TA_CENTER),
        "cover_sub":   s("cs", fontName="Helvetica", fontSize=15, leading=22,
                         textColor=YELLOW, alignment=TA_CENTER),
        "cover_meta":  s("cm", fontName="Helvetica", fontSize=10, leading=14,
                         textColor=WHITE, alignment=TA_CENTER),
        "section":     s("sec", fontName="Helvetica-Bold", fontSize=16, leading=20,
                         textColor=RED, spaceBefore=14, spaceAfter=6),
        "sub":         s("sub", fontName="Helvetica-Bold", fontSize=12, leading=16,
                         textColor=DARK, spaceBefore=10, spaceAfter=4),
        "body":        s("body", fontName="Helvetica", fontSize=10, leading=15,
                         textColor=GRAY, alignment=TA_JUSTIFY, spaceAfter=4),
        "bullet":      s("blt", fontName="Helvetica", fontSize=10, leading=15,
                         textColor=GRAY, leftIndent=14, firstLineIndent=-10, spaceAfter=2),
        "caption":     s("cap", fontName="Helvetica-Oblique", fontSize=8, leading=11,
                         textColor=MGRAY, alignment=TA_CENTER, spaceAfter=6),
        "toc_entry":   s("te", fontName="Helvetica", fontSize=11, leading=17,
                         textColor=DARK),
        "toc_num":     s("tn", fontName="Helvetica-Bold", fontSize=11, leading=17,
                         textColor=RED),
        "th":          s("th", fontName="Helvetica-Bold", fontSize=9.5, leading=13,
                         textColor=WHITE, alignment=TA_CENTER),
        "tc":          s("tc", fontName="Helvetica", fontSize=9.5, leading=13,
                         textColor=GRAY, alignment=TA_LEFT),
        "tn":          s("tnc", fontName="Helvetica-Bold", fontSize=9.5, leading=13,
                         textColor=DARK, alignment=TA_CENTER),
        "highlight":   s("hl", fontName="Helvetica-Bold", fontSize=11, leading=15,
                         textColor=DARK, backColor=YELLOW, borderPadding=(4, 6, 4, 6),
                         alignment=TA_CENTER),
        "foot":        s("fn", fontName="Helvetica-Oblique", fontSize=8, leading=11,
                         textColor=MGRAY, alignment=TA_CENTER),
        "feat_title":  s("ft", fontName="Helvetica-Bold", fontSize=10.5, leading=14,
                         textColor=DARK, spaceAfter=2),
        "feat_body":   s("fb", fontName="Helvetica", fontSize=9.5, leading=14,
                         textColor=GRAY, spaceAfter=8),
        "feat_module": s("fm", fontName="Helvetica-Bold", fontSize=9.5, leading=13,
                         textColor=DARK),
        "sc_title":    s("sct", fontName="Helvetica-Bold", fontSize=10, leading=14,
                         textColor=WHITE, alignment=TA_CENTER),
        "sc_body":     s("scb", fontName="Helvetica", fontSize=8.5, leading=12,
                         textColor=MGRAY, alignment=TA_CENTER),
    }


# ── HELPER: TABELA SIMPLES ───────────────────────────────────────────────────
def simple_table(data, col_widths, ST,
                 hdr_bg=RED, row_bgs=None, grid=True, top_pad=7, bot_pad=7):
    t = Table(data, colWidths=col_widths)
    style = [
        ("BACKGROUND",    (0, 0), (-1, 0), hdr_bg),
        ("TOPPADDING",    (0, 0), (-1, -1), top_pad),
        ("BOTTOMPADDING", (0, 0), (-1, -1), bot_pad),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]
    if grid:
        style.append(("GRID", (0, 0), (-1, -1), 0.5, BORDG))
    if row_bgs:
        style.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), row_bgs))
    t.setStyle(TableStyle(style))
    return t


# ── HELPER: SCREENSHOT BOX ────────────────────────────────────────────────────
def screenshot_block(key, title, caption, max_w, max_h, ST):
    """Retorna Image se arquivo existir, senão cria placeholder box."""
    path = ss(key)
    if path:
        img = Image(path, width=max_w, height=max_h, kind='proportional')
        return [
            img,
            Paragraph(caption, ST["caption"]),
        ]
    # placeholder
    d = Drawing(max_w, max_h)
    d.add(Rect(0, 0, max_w, max_h, fillColor=colors.HexColor("#f0f0f0"),
               strokeColor=colors.HexColor("#cccccc"), strokeWidth=1))
    d.add(Rect(0, max_h - 22, max_w, 22, fillColor=RED, strokeColor=None))
    d.add(String(max_w / 2, max_h - 14, title, fontName="Helvetica-Bold",
                 fontSize=8, fillColor=WHITE, textAnchor="middle"))
    d.add(String(max_w / 2, max_h / 2 + 8, "[ Screenshot ]",
                 fontName="Helvetica-Bold", fontSize=11,
                 fillColor=colors.HexColor("#bbbbbb"), textAnchor="middle"))
    d.add(String(max_w / 2, max_h / 2 - 10, caption,
                 fontName="Helvetica", fontSize=8,
                 fillColor=MGRAY, textAnchor="middle"))
    return [d]


# ── HELPER: BAR CHART ────────────────────────────────────────────────────────
def revenue_bar_chart(width=14 * cm, height=6.5 * cm):
    d = Drawing(width, height)
    bc = VerticalBarChart()
    bc.x = 50
    bc.y = 30
    bc.height = height - 50
    bc.width = width - 70
    bc.data = [[4000, 12000, 18400]]
    bc.categoryAxis.categoryNames = ['Conservador\n(5%)', 'Moderado\n(15%)', 'Otimista\n(23%)']
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = 20000
    bc.valueAxis.valueStep = 4000
    bc.bars[0].fillColor = RED
    bc.bars[0].strokeColor = None
    bc.categoryAxis.labels.fontName = "Helvetica"
    bc.categoryAxis.labels.fontSize = 8
    bc.categoryAxis.labels.fillColor = GRAY
    bc.valueAxis.labels.fontName = "Helvetica"
    bc.valueAxis.labels.fontSize = 8
    bc.valueAxis.labels.fillColor = GRAY
    bc.groupSpacing = 14
    d.add(bc)
    # labels on bars
    vals = [4000, 12000, 18400]
    labels = ["R$ 4.000", "R$ 12.000", "R$ 18.400"]
    bar_w = bc.width / 3
    for i, (v, lbl) in enumerate(zip(vals, labels)):
        bar_h = (v / 20000) * (height - 50)
        x = bc.x + i * bar_w + bar_w / 2
        y = bc.y + bar_h + 4
        d.add(String(x, y, lbl, fontName="Helvetica-Bold", fontSize=8,
                     fillColor=DARK, textAnchor="middle"))
    return d


def rede_bar_chart(width=14 * cm, height=6.5 * cm):
    d = Drawing(width, height)
    bc = VerticalBarChart()
    bc.x = 70
    bc.y = 30
    bc.height = height - 50
    bc.width = width - 90
    bc.data = [[9600, 24000, 48000, 96000]]
    bc.categoryAxis.categoryNames = ['10 lojas', '25 lojas', '50 lojas', '100 lojas']
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = 100000
    bc.valueAxis.valueStep = 20000
    bc.bars[0].fillColor = DARK
    bc.bars[0].strokeColor = None
    bc.categoryAxis.labels.fontName = "Helvetica"
    bc.categoryAxis.labels.fontSize = 8
    bc.categoryAxis.labels.fillColor = GRAY
    bc.valueAxis.labels.fontName = "Helvetica"
    bc.valueAxis.labels.fontSize = 8
    bc.valueAxis.labels.fillColor = GRAY
    bc.groupSpacing = 16
    d.add(bc)
    vals = [9600, 24000, 48000, 96000]
    labels = ["R$9.600", "R$24.000", "R$48.000", "R$96.000"]
    bar_w = bc.width / 4
    for i, (v, lbl) in enumerate(zip(vals, labels)):
        bh = (v / 100000) * (height - 50)
        x = bc.x + i * bar_w + bar_w / 2
        y = bc.y + bh + 4
        d.add(String(x, y, lbl, fontName="Helvetica-Bold", fontSize=8,
                     fillColor=DARK, textAnchor="middle"))
    return d


# ════════════════════════════════════════════════════════════════════════════
# BUILD
# ════════════════════════════════════════════════════════════════════════════
def build_pdf(filename):
    doc = SimpleDocTemplate(
        filename, pagesize=A4,
        rightMargin=1.8 * cm, leftMargin=1.8 * cm,
        topMargin=2 * cm, bottomMargin=1.8 * cm,
    )
    ST = make_styles()
    CW = W - 3.6 * cm   # usable content width
    story = []

    # ── CAPA ─────────────────────────────────────────────────────────────────
    bg = Drawing(CW, 11 * cm)
    bg.add(Rect(0, 0, CW, 11 * cm, fillColor=RED, strokeColor=None))
    bg.add(Rect(0, 0, CW, 0.55 * cm, fillColor=YELLOW, strokeColor=None))
    bg.add(Rect(0, 10.5 * cm, CW, 0.5 * cm, fillColor=colors.HexColor("#a0001e"), strokeColor=None))
    story.append(bg)
    story.append(Spacer(1, -10.4 * cm))
    story.append(Spacer(1, 1.6 * cm))
    story.append(Paragraph("Poupaqui Ecommerce", ST["cover_title"]))
    story.append(Spacer(1, 0.35 * cm))
    story.append(Paragraph("Plataforma Digital para a Rede de Farmácias Poupaqui", ST["cover_sub"]))
    story.append(Spacer(1, 0.55 * cm))
    story.append(Paragraph("Proposta Comercial e Apresentação de Funcionalidades", ST["cover_meta"]))
    story.append(Spacer(1, 0.25 * cm))
    story.append(Paragraph("Junho de 2026  ·  Confidencial", ST["cover_meta"]))
    story.append(Spacer(1, 5.8 * cm))

    story.append(HRFlowable(width="100%", thickness=2, color=RED, spaceAfter=16))

    # ── SUMÁRIO ───────────────────────────────────────────────────────────────
    story.append(NamedAnchor("toc", "Sumário", level=0))
    story.append(Paragraph("SUMÁRIO", ST["section"]))

    SECTIONS = [
        ("s1", "1.", "O Mercado — Por que o Ecommerce é Urgente"),
        ("s2", "2.", "Dados e Pesquisas do Setor Farmacêutico Digital"),
        ("s3", "3.", "A Plataforma Poupaqui Ecommerce"),
        ("s4", "4.", "Como Funciona na Prática"),
        ("s5", "5.", "Funcionalidades — Painel da Loja"),
        ("s6", "6.", "Funcionalidades — Experiência do Consumidor"),
        ("s7", "7.", "Integrações e Infraestrutura"),
        ("s8", "8.", "Proposta Comercial e Planos"),
        ("s9", "9.", "Projeções de Retorno (ROI)"),
        ("s10","10.", "Próximos Passos"),
    ]
    toc_rows = []
    for anchor, num, title in SECTIONS:
        toc_rows.append([
            Paragraph(f'<link href="#{anchor}" color="#c8102e"><b>{num}</b></link>', ST["toc_num"]),
            Paragraph(f'<link href="#{anchor}" color="#333333">{title}</link>', ST["toc_entry"]),
        ])
    toc_t = Table(toc_rows, colWidths=[1.0 * cm, None])
    toc_t.setStyle(TableStyle([
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW",     (0, 0), (-1, -1), 0.3, BORDG),
    ]))
    story.append(toc_t)
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════
    # 1. O MERCADO
    # ════════════════════════════════════════════════════════════════════════
    story.append(NamedAnchor("s1", "1. O Mercado", level=0))
    story.append(Paragraph("1. O Mercado — Por que o Ecommerce é Urgente", ST["section"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=YELLOW, spaceAfter=10))

    story.append(Paragraph(
        "O varejo farmacêutico brasileiro está em transformação digital acelerada. "
        "Consumidores já esperam pesquisar, comparar e comprar medicamentos online — "
        "da mesma forma que pedem comida pelo iFood. Farmácias que não estão no digital "
        "perdem visibilidade, clientes e faturamento para redes nacionais e marketplaces "
        "que investem pesado em tecnologia.", ST["body"]))

    story.append(Spacer(1, 0.4 * cm))

    kw = CW / 4
    kpi = [
        [Paragraph("<b>R$ 34,8 bi</b>", ParagraphStyle("k", fontName="Helvetica-Bold", fontSize=20, textColor=RED, alignment=TA_CENTER, leading=24)),
         Paragraph("<b>63%</b>", ParagraphStyle("k2", fontName="Helvetica-Bold", fontSize=20, textColor=RED, alignment=TA_CENTER, leading=24)),
         Paragraph("<b>R$ 8,2 bi</b>", ParagraphStyle("k3", fontName="Helvetica-Bold", fontSize=20, textColor=RED, alignment=TA_CENTER, leading=24)),
         Paragraph("<b>47%</b>", ParagraphStyle("k4", fontName="Helvetica-Bold", fontSize=20, textColor=RED, alignment=TA_CENTER, leading=24))],
        [Paragraph("Faturamento ecommerce\nfarmacêutico Brasil 2024\n<i>(IQVIA/ABComm)</i>",
                   ParagraphStyle("kl", fontName="Helvetica", fontSize=8, textColor=GRAY, alignment=TA_CENTER, leading=12)),
         Paragraph("Das farmácias brasileiras\nainda sem canal digital\n<i>(Sebrae 2024)</i>",
                   ParagraphStyle("kl2", fontName="Helvetica", fontSize=8, textColor=GRAY, alignment=TA_CENTER, leading=12)),
         Paragraph("Crescimento previsto do\ncanal digital até 2027\n<i>(IDC Brasil)</i>",
                   ParagraphStyle("kl3", fontName="Helvetica", fontSize=8, textColor=GRAY, alignment=TA_CENTER, leading=12)),
         Paragraph("Consulta online antes\nde comprar na farmácia\n<i>(Nielsen 2024)</i>",
                   ParagraphStyle("kl4", fontName="Helvetica", fontSize=8, textColor=GRAY, alignment=TA_CENTER, leading=12))],
    ]
    kt = Table(kpi, colWidths=[kw, kw, kw, kw])
    kt.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (0, -1), colors.HexColor("#fff5f7")),
        ("BACKGROUND",    (1, 0), (1, -1), LGRAY),
        ("BACKGROUND",    (2, 0), (2, -1), colors.HexColor("#fff5f7")),
        ("BACKGROUND",    (3, 0), (3, -1), LGRAY),
        ("BOX",           (0, 0), (0, -1), 1, BORDG),
        ("BOX",           (1, 0), (1, -1), 1, BORDG),
        ("BOX",           (2, 0), (2, -1), 1, BORDG),
        ("BOX",           (3, 0), (3, -1), 1, BORDG),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(kt)
    story.append(Paragraph("Fontes: IQVIA, ABComm, Sebrae, IDC Brasil, Nielsen — 2024/2025", ST["caption"]))

    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph("O que os consumidores buscam hoje:", ST["sub"]))
    for b in [
        "Ver se o produto está disponível na farmácia mais próxima <b>antes de sair de casa</b>",
        "Buscar medicamentos por sintoma, nome ou princípio ativo a qualquer hora",
        "Comparar preços entre lojas da mesma rede",
        "Fazer pedido online com <b>retirada na loja</b> (click & collect) ou entrega",
        "Receber notificações de promoções e ofertas personalizadas",
        "Usar cupons de desconto e programas de fidelidade digitais",
    ]:
        story.append(Paragraph(f"• {b}", ST["bullet"]))
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════
    # 2. DADOS E PESQUISAS
    # ════════════════════════════════════════════════════════════════════════
    story.append(NamedAnchor("s2", "2. Dados do Setor", level=0))
    story.append(Paragraph("2. Dados e Pesquisas do Setor Farmacêutico Digital", ST["section"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=YELLOW, spaceAfter=10))

    pesq = [
        [Paragraph("<b>Fonte</b>", ST["th"]),
         Paragraph("<b>Dado</b>", ST["th"]),
         Paragraph("<b>O que significa para a loja</b>", ST["th"])],
        [Paragraph("Euromonitor 2024", ST["tc"]),
         Paragraph("Farmácias com canal digital faturam <b>23% a mais</b>", ST["tc"]),
         Paragraph("R$23 extras a cada R$100 do físico", ST["tn"])],
        [Paragraph("ABComm / NielsenIQ 2024", ST["tc"]),
         Paragraph("<b>71%</b> dos brasileiros pesquisam online antes de comprar", ST["tc"]),
         Paragraph("7 em cada 10 buscas não chegam a loja sem canal digital", ST["tn"])],
        [Paragraph("Google Think Retail 2023", ST["tc"]),
         Paragraph("Farmácias online geram <b>3,2x mais visitas</b> à loja física", ST["tc"]),
         Paragraph("SEO do ecommerce traz tráfego gratuito para o balcão", ST["tn"])],
        [Paragraph("Sebrae 2024", ST["tc"]),
         Paragraph("Ticket médio online é <b>R$ 87</b> vs R$ 52 no balcão (<b>+67%</b>)", ST["tc"]),
         Paragraph("Cada pedido online vale quase o dobro do balcão", ST["tn"])],
        [Paragraph("Mobile Time / Opinion Box 2024", ST["tc"]),
         Paragraph("<b>68%</b> preferem retirar na loja ao ver estoque online", ST["tc"]),
         Paragraph("Click &amp; collect: venda certa sem custo de entrega", ST["tn"])],
        [Paragraph("Deloitte Digital Health 2025", ST["tc"]),
         Paragraph("Farmácias com ecommerce retêm <b>40% mais</b> clientes/ano", ST["tc"]),
         Paragraph("Fidelização digital reduz perda de clientes para concorrência", ST["tn"])],
    ]
    cw2 = [CW * 0.22, CW * 0.44, CW * 0.34]
    pt = Table(pesq, colWidths=cw2, repeatRows=1)
    pt.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), RED),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, LGRAY]),
        ("GRID",          (0, 0), (-1, -1), 0.5, BORDG),
        ("TOPPADDING",    (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING",   (0, 0), (-1, -1), 7),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 7),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(pt)
    story.append(Paragraph("Dados compilados de Euromonitor, ABComm, NielsenIQ, Google, Sebrae e Deloitte (2023–2025).", ST["caption"]))

    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph("Simulação de receita digital adicional — farmácia com R$ 80.000/mês no físico", ST["sub"]))
    story.append(Paragraph(
        "O gráfico abaixo mostra o faturamento digital estimado em 3 cenários, "
        "aplicando os percentuais de crescimento digital reportados pelas pesquisas acima.", ST["body"]))
    story.append(Spacer(1, 0.2 * cm))
    story.append(revenue_bar_chart(CW, 7 * cm))
    story.append(Paragraph("Receita digital mensal estimada por cenário (base: R$ 80.000/mês no físico)", ST["caption"]))

    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════
    # 3. A PLATAFORMA
    # ════════════════════════════════════════════════════════════════════════
    story.append(NamedAnchor("s3", "3. A Plataforma", level=0))
    story.append(Paragraph("3. A Plataforma Poupaqui Ecommerce", ST["section"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=YELLOW, spaceAfter=10))

    story.append(Paragraph(
        "O Poupaqui Ecommerce é uma plataforma digital <b>exclusiva para a rede Poupaqui</b>, "
        "desenvolvida sob medida para integrar o estoque real de cada loja com um catálogo "
        "online acessível a qualquer consumidor. O modelo é inspirado no iFood: o cliente vê "
        "os produtos disponíveis nas farmácias mais próximas e compra com entrega ou retirada.", ST["body"]))

    story.append(Spacer(1, 0.35 * cm))

    pw = CW / 3
    pilares = [
        [Paragraph("Para a Rede", ParagraphStyle("ph", fontName="Helvetica-Bold", fontSize=11, textColor=WHITE, alignment=TA_CENTER)),
         Paragraph("Para as Lojas", ParagraphStyle("ph2", fontName="Helvetica-Bold", fontSize=11, textColor=WHITE, alignment=TA_CENTER)),
         Paragraph("Para o Consumidor", ParagraphStyle("ph3", fontName="Helvetica-Bold", fontSize=11, textColor=WHITE, alignment=TA_CENTER))],
        [Paragraph("Vitrine digital única que fortalece a marca Poupaqui como rede moderna e conectada.",
                   ParagraphStyle("pb", fontName="Helvetica", fontSize=9, textColor=GRAY, alignment=TA_CENTER, leading=13)),
         Paragraph("Cada loja tem ecommerce próprio, painel de gestão e controle total dos seus produtos.",
                   ParagraphStyle("pb2", fontName="Helvetica", fontSize=9, textColor=GRAY, alignment=TA_CENTER, leading=13)),
         Paragraph("Encontra a farmácia mais próxima, vê o estoque em tempo real e compra sem sair de casa.",
                   ParagraphStyle("pb3", fontName="Helvetica", fontSize=9, textColor=GRAY, alignment=TA_CENTER, leading=13))],
    ]
    pt2 = Table(pilares, colWidths=[pw, pw, pw])
    pt2.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (0, 0), RED),
        ("BACKGROUND",    (1, 0), (1, 0), DARK),
        ("BACKGROUND",    (2, 0), (2, 0), RED),
        ("BACKGROUND",    (0, 1), (0, 1), colors.HexColor("#fff5f7")),
        ("BACKGROUND",    (1, 1), (1, 1), LGRAY),
        ("BACKGROUND",    (2, 1), (2, 1), colors.HexColor("#fff5f7")),
        ("BOX",           (0, 0), (0, -1), 1.5, RED),
        ("BOX",           (1, 0), (1, -1), 1, BORDG),
        ("BOX",           (2, 0), (2, -1), 1.5, RED),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
    ]))
    story.append(pt2)

    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph("Como funciona a integração de estoque", ST["sub"]))
    story.append(Paragraph(
        "A plataforma se conecta automaticamente aos sistemas já usados pelas lojas — "
        "<b>Alpha</b> e <b>Automatiza</b> — além do CD DNS/Vitnatu. "
        "Nenhum cadastro manual: o estoque do sistema da loja vira o catálogo online automaticamente.", ST["body"]))

    # fluxo
    fw = [CW * v for v in [0.23, 0.07, 0.23, 0.07, 0.23, 0.07, 0.10]]
    fl = Table([[
        Paragraph("Sistema\nda Loja\n(Alpha / Automatiza)", ParagraphStyle("fl1", fontName="Helvetica-Bold", fontSize=8.5, textColor=WHITE, alignment=TA_CENTER, leading=12)),
        Paragraph("→", ParagraphStyle("arr", fontName="Helvetica-Bold", fontSize=18, textColor=RED, alignment=TA_CENTER)),
        Paragraph("Sincronização\nAutomática\n(tempo real)", ParagraphStyle("fl2", fontName="Helvetica-Bold", fontSize=8.5, textColor=WHITE, alignment=TA_CENTER, leading=12)),
        Paragraph("→", ParagraphStyle("arr2", fontName="Helvetica-Bold", fontSize=18, textColor=RED, alignment=TA_CENTER)),
        Paragraph("Catálogo Online\nPoupaqui\n(ecommerce)", ParagraphStyle("fl3", fontName="Helvetica-Bold", fontSize=8.5, textColor=WHITE, alignment=TA_CENTER, leading=12)),
        Paragraph("→", ParagraphStyle("arr3", fontName="Helvetica-Bold", fontSize=18, textColor=RED, alignment=TA_CENTER)),
        Paragraph("Cliente\nFinal", ParagraphStyle("fl4", fontName="Helvetica-Bold", fontSize=8.5, textColor=DARK, alignment=TA_CENTER, leading=12)),
    ]], colWidths=fw)
    fl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (0, 0), RED),
        ("BACKGROUND",    (2, 0), (2, 0), DARK),
        ("BACKGROUND",    (4, 0), (4, 0), RED),
        ("BACKGROUND",    (6, 0), (6, 0), YELLOW),
        ("BACKGROUND",    (1, 0), (1, 0), WHITE),
        ("BACKGROUND",    (3, 0), (3, 0), WHITE),
        ("BACKGROUND",    (5, 0), (5, 0), WHITE),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
    ]))
    story.append(fl)
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════
    # 4. COMO FUNCIONA NA PRÁTICA — SCREENSHOTS
    # ════════════════════════════════════════════════════════════════════════
    story.append(NamedAnchor("s4", "4. Como Funciona na Prática", level=0))
    story.append(Paragraph("4. Como Funciona na Prática", ST["section"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=YELLOW, spaceAfter=10))
    story.append(Paragraph(
        "A seguir, a experiência real da plataforma — do acesso inicial até o painel de gestão da loja.", ST["body"]))

    story.append(Spacer(1, 0.3 * cm))

    def ss_row(key_a, cap_a, key_b, cap_b, h=7.0 * cm):
        sw = (CW - 0.4 * cm) / 2
        imgs_a = screenshot_block(key_a, "", cap_a, sw, h, ST)
        imgs_b = screenshot_block(key_b, "", cap_b, sw, h, ST)
        # imagem
        r = Table([[imgs_a[0], imgs_b[0]]], colWidths=[sw, sw])
        r.setStyle(TableStyle([
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 2),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("BOX",           (0, 0), (0, 0), 0.5, BORDG),
            ("BOX",           (1, 0), (1, 0), 0.5, BORDG),
        ]))
        # legenda
        c = Table([[Paragraph(cap_a, ST["caption"]), Paragraph(cap_b, ST["caption"])]],
                  colWidths=[sw, sw])
        c.setStyle(TableStyle([
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        return r, c

    r1, c1 = ss_row(
        "home",    "Home: localização automática, categorias e melhores produtos próximos",
        "catalogo","Catálogo: produtos das farmácias mais próximas com preço e distância",
        h=7.0 * cm,
    )
    story.append(r1); story.append(c1)

    r2, c2 = ss_row(
        "marca_propria", "Marca Própria Vitnatu: vitrine exclusiva com disponibilidade por farmácia próxima",
        "carrinho",      "Carrinho e checkout: PIX/cartão, cupom de desconto, retirada na loja ou entrega",
        h=7.0 * cm,
    )
    story.append(r2); story.append(c2)

    r3, c3 = ss_row(
        "lojas",  "Vitrine de lojas: mapa e lista de todas as farmácias da rede Poupaqui",
        "painel", "Painel da loja: login seguro e acesso à gestão completa do ecommerce",
        h=6.5 * cm,
    )
    story.append(r3); story.append(c3)

    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════
    # 5. FUNCIONALIDADES — PAINEL DA LOJA
    # ════════════════════════════════════════════════════════════════════════
    story.append(NamedAnchor("s5", "5. Funcionalidades — Painel da Loja", level=0))
    story.append(Paragraph("5. Funcionalidades — Painel da Loja", ST["section"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=YELLOW, spaceAfter=10))
    story.append(Paragraph(
        "Painel completo de gestão do ecommerce, acessível pelo navegador sem instalar nada. "
        "A loja controla tudo: preços, pedidos, promoções, clientes e relatórios.", ST["body"]))

    story.append(Spacer(1, 0.3 * cm))

    FEAT_LOJA = [
        ("Precificador Inteligente",
         "A loja define preços independentes por produto. Suporta ajuste percentual em lote, "
         "preço individual e importação via planilha Excel."),
        ("Gestão de Pedidos",
         "Visualiza todos os pedidos, confirma pagamento, registra envio e confirma "
         "entrega ou retirada. Histórico completo com status em tempo real. Integra pedidos do Mercado Livre no mesmo painel."),
        ("Cupons de Desconto",
         "Cria cupons com valor fixo ou percentual, validade, limite de uso e restrição "
         "por produto/categoria. Envio para clientes específicos ou uso público."),
        ("Promoções Automáticas",
         "Desconto automático por produto, categoria ou valor mínimo de compra — "
         "sem necessidade de cupom, aplicado direto no checkout."),
        ("Banners Personalizados",
         "Upload de banners promocionais próprios que aparecem no topo do catálogo da loja."),
        ("Catálogo Configurável",
         "Escolhe quais produtos aparecem, define estoque mínimo para publicação e "
         "filtra por categoria. Produtos sem imagem são ocultados automaticamente."),
        ("Horários de Funcionamento",
         "Configura horários por dia da semana. O sistema avisa o consumidor quando a "
         "loja está fechada e bloqueia novos pedidos fora do horário."),
        ("Notificações para Clientes",
         "Envia notificações personalizadas para todos os consumidores que já "
         "compraram na loja — promoções, novidades ou comunicados."),
        ("SAC / Reclamações",
         "Sistema de atendimento integrado: consumidor abre reclamação pelo app, "
         "loja responde pelo painel. Histórico e status de resolução."),
        ("Relatórios e Desempenho",
         "Dashboard com faturamento, ticket médio, produtos mais vendidos, clientes "
         "novos vs. recorrentes e evolução por período."),
        ("Assinaturas Recorrentes",
         "Consumidor assina produtos de uso contínuo (vitaminas, anticoncepcionais etc.) "
         "com entrega automática mensal — receita recorrente garantida."),
        ("Configurações de Pagamento",
         "Ativa/desativa Pix, cartão de crédito (Mercado Pago ou Asaas) e pagamento "
         "na retirada. Configuração individual por loja."),
        ("Integração Mercado Livre",
         "Vincula conta ML e gerencia pedidos do Marketplace dentro do mesmo painel — "
         "sem abrir outro sistema."),
        ("Imagem Customizada",
         "A loja substitui a foto padrão de qualquer produto por imagem própria, "
         "ideal para produtos de marca própria ou itens sem foto no catálogo."),
    ]

    feat_data = [[Paragraph("<b>Módulo</b>", ST["th"]),
                  Paragraph("<b>O que faz</b>", ST["th"])]]
    for title, desc in FEAT_LOJA:
        feat_data.append([
            Paragraph(title, ST["feat_module"]),
            Paragraph(desc, ST["tc"]),
        ])

    fcw = [CW * 0.28, CW * 0.72]
    ft = Table(feat_data, colWidths=fcw, repeatRows=1)
    ft.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), RED),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, LGRAY]),
        ("GRID",          (0, 0), (-1, -1), 0.4, BORDG),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 7),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 7),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(ft)
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════
    # 6. FUNCIONALIDADES — CONSUMIDOR  (layout corrigido: lista 2 colunas)
    # ════════════════════════════════════════════════════════════════════════
    story.append(NamedAnchor("s6", "6. Funcionalidades — Consumidor", level=0))
    story.append(Paragraph("6. Funcionalidades — Experiência do Consumidor", ST["section"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=YELLOW, spaceAfter=10))
    story.append(Paragraph(
        "Experiência de compra moderna e intuitiva — tudo pelo navegador, sem baixar app.", ST["body"]))

    story.append(Spacer(1, 0.3 * cm))

    FEAT_CONS = [
        ("Busca Inteligente",
         "Busca por nome, princípio ativo, sintoma ou laboratório. Filtros por categoria, "
         "tarja, preço e distância. Resultados priorizados pelas farmácias mais próximas."),
        ("Localização Automática",
         "Detecta a geolocalização e mostra as farmácias mais próximas com o produto "
         "desejado em estoque — modelo iFood aplicado ao setor farmacêutico."),
        ("Catálogo com Fotos e Bula",
         "Catálogo com imagens oficiais, tarja da ANVISA, laboratório, descrição e "
         "acesso à bula para qualquer medicamento registrado."),
        ("Carrinho Multi-Farmácia",
         "Adiciona produtos de farmácias diferentes no mesmo carrinho, aplica cupom "
         "de desconto, escolhe retirada ou entrega e finaliza o pagamento."),
        ("PIX e Cartão de Crédito",
         "Pagamento instantâneo via PIX com QR Code ou cartão de crédito em até 12x, "
         "com opção de salvar o cartão para futuras compras."),
        ("Rastreio de Pedidos em Tempo Real",
         "Acompanha o status do pedido: aguardando pagamento, em preparo, saiu para "
         "entrega, pronto para retirada e entregue."),
        ("Receita Médica Digital",
         "Upload da receita médica PDF diretamente no pedido para medicamentos "
         "controlados, avaliada pelo farmacêutico da loja."),
        ("Lista de Favoritos com Alerta de Preço",
         "Salva produtos favoritos e recebe notificação automática quando o preço "
         "cai ou o produto fica disponível em uma farmácia próxima."),
        ("Assinaturas Mensais",
         "Assina produtos de uso contínuo com entrega automática mensal — nunca "
         "fica sem o medicamento do mês. Cancela a qualquer momento."),
        ("Notificações",
         "Notificações de status de pedido, promoções da loja e comunicados — "
         "direto no navegador, sem precisar de app instalado."),
        ("SAC Digital",
         "Abre reclamação, acompanha atendimento e confirma resolução sem ligar "
         "para a loja. Histórico completo de mensagens trocadas."),
        ("Histórico e Perfil",
         "Acessa histórico de compras, repete pedidos anteriores, gerencia "
         "endereços salvos, cartões e cupons disponíveis."),
    ]

    # Layout 2 colunas de cards
    half = len(FEAT_CONS) // 2 + len(FEAT_CONS) % 2
    cw_half = (CW - 0.4 * cm) / 2

    for i in range(half):
        left = FEAT_CONS[i]
        right = FEAT_CONS[i + half] if (i + half) < len(FEAT_CONS) else None

        def card_cell(feat):
            if feat is None:
                return Paragraph("", ST["body"])
            title, desc = feat
            return [
                Paragraph(f"<b>{title}</b>", ST["feat_title"]),
                Paragraph(desc, ST["feat_body"]),
            ]

        left_cell = card_cell(left)
        right_cell = card_cell(right)

        row = Table([[left_cell, right_cell]], colWidths=[cw_half, cw_half])
        row.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (0, 0), colors.HexColor("#fff5f7")),
            ("BACKGROUND",    (1, 0), (1, 0), LGRAY if right else WHITE),
            ("BOX",           (0, 0), (0, 0), 0.5, BORDG),
            ("BOX",           (1, 0), (1, 0), 0.5, BORDG),
            ("TOPPADDING",    (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(row)
        story.append(Spacer(1, 0.15 * cm))

    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════
    # 7. INTEGRAÇÕES
    # ════════════════════════════════════════════════════════════════════════
    story.append(NamedAnchor("s7", "7. Integrações e Infraestrutura", level=0))
    story.append(Paragraph("7. Integrações e Infraestrutura", ST["section"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=YELLOW, spaceAfter=10))
    story.append(Paragraph(
        "Construída com tecnologias de ponta, prontas para suportar centenas de lojas "
        "e milhares de pedidos simultâneos — sem nenhuma infraestrutura da loja.", ST["body"]))

    story.append(Spacer(1, 0.3 * cm))

    INTEG = [
        ("Mercado Pago", "Pagamentos Pix e cartão de crédito", "Recebe online sem maquininha adicional"),
        ("Asaas", "Cartão de crédito e cobranças recorrentes (assinaturas)", "Assinaturas debitadas automaticamente"),
        ("ANVISA / CMED", "Base oficial de medicamentos e preços máximos", "Tarja, bula e laboratório automáticos"),
        ("Cloudinary", "CDN de imagens em alta velocidade", "Fotos carregam rápido em qualquer dispositivo"),
        ("Mercado Livre", "Integração com marketplace", "Pedidos ML gerenciados no mesmo painel"),
        ("OpenStreetMap", "Geocodificação e cálculo de distância", "Consumidor vê farmácias mais próximas — modelo iFood"),
        ("Resend", "E-mail transacional", "Confirmações, pedidos e senhas no domínio Poupaqui"),
        ("Alpha / Automatiza", "Sistemas de gestão das lojas", "Estoque real sincronizado automaticamente"),
        ("Vercel", "Hospedagem cloud com CDN global", "Site sempre no ar, deploy automático, zero servidor"),
        ("Supabase / PostgreSQL", "Banco de dados gerenciado com backup automático", "Dados seguros, escaláveis, acessíveis de qualquer lugar"),
    ]

    integ_data = [[Paragraph("<b>Integração</b>", ST["th"]),
                   Paragraph("<b>Para que serve</b>", ST["th"]),
                   Paragraph("<b>Benefício para a loja</b>", ST["th"])]]
    for title, desc, benefit in INTEG:
        integ_data.append([
            Paragraph(f"<b>{title}</b>", ST["feat_module"]),
            Paragraph(desc, ST["tc"]),
            Paragraph(benefit, ST["tc"]),
        ])

    icw = [CW * 0.22, CW * 0.42, CW * 0.36]
    it = Table(integ_data, colWidths=icw, repeatRows=1)
    it.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), RED),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, LGRAY]),
        ("GRID",          (0, 0), (-1, -1), 0.4, BORDG),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 7),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 7),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(it)
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════
    # 8. PROPOSTA COMERCIAL
    # ════════════════════════════════════════════════════════════════════════
    story.append(NamedAnchor("s8", "8. Proposta Comercial", level=0))
    story.append(Paragraph("8. Proposta Comercial e Planos", ST["section"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=YELLOW, spaceAfter=10))

    story.append(Paragraph(
        "Proposta desenhada para ser justa: <b>quem fatura mais, paga mais; "
        "quem ainda não decolou, não paga nada</b>. Nenhuma barreira de entrada.", ST["body"]))

    story.append(Spacer(1, 0.5 * cm))

    pw_card = (CW - 0.6 * cm) / 2

    def plan_card(header_color, title, price, price_sub, bullets, cta):
        rows = [
            [Paragraph(f"<b>{title}</b>",
                       ParagraphStyle("ph", fontName="Helvetica-Bold", fontSize=13,
                                      textColor=WHITE, alignment=TA_CENTER))],
            [Paragraph(price, ParagraphStyle("pp", fontName="Helvetica-Bold", fontSize=24,
                                              textColor=header_color, alignment=TA_CENTER, leading=28))],
            [Paragraph(price_sub, ParagraphStyle("ps", fontName="Helvetica", fontSize=9,
                                                  textColor=GRAY, alignment=TA_CENTER, leading=13))],
            [[Paragraph(f"• {b}", ParagraphStyle("pb2", fontName="Helvetica", fontSize=9.5,
                                                   textColor=GRAY, leftIndent=10, firstLineIndent=-10,
                                                   leading=14, spaceAfter=3)) for b in bullets]],
            [Paragraph(cta, ParagraphStyle("cta", fontName="Helvetica-Bold", fontSize=10,
                                            textColor=DARK, backColor=YELLOW,
                                            borderPadding=(5, 8, 5, 8), alignment=TA_CENTER))],
        ]
        t = Table(rows, colWidths=[pw_card])
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (0, 0), header_color),
            ("BACKGROUND",    (0, 1), (0, 1), colors.HexColor("#fff5f7") if header_color == RED else colors.HexColor("#f0f4ff")),
            ("BACKGROUND",    (0, 2), (0, 2), WHITE),
            ("BACKGROUND",    (0, 3), (0, 3), WHITE),
            ("BACKGROUND",    (0, 4), (0, 4), colors.HexColor("#fffde7")),
            ("BOX",           (0, 0), (0, -1), 2, header_color),
            ("TOPPADDING",    (0, 0), (-1, -1), 12),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ("LEFTPADDING",   (0, 3), (0, 3), 16),
            ("LEFTPADDING",   (0, 0), (0, 2), 10),
            ("LEFTPADDING",   (0, 4), (0, 4), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ]))
        return t

    card1 = plan_card(
        RED, "PLANO PERFORMANCE",
        "8%", "sobre o faturamento digital mensal",
        [
            "Zero cobrança até R$ 500/mês de faturamento digital",
            "Acima de R$ 500: 8% sobre o total faturado",
            "Paga somente quando vende — sem risco",
            "Sem mensalidade fixa",
            "Ideal para lojas em fase de crescimento",
        ],
        "Recomendado para começar",
    )
    card2 = plan_card(
        DARK, "PLANO FIXO",
        "R$ 250", "por mês, por loja",
        [
            "Mensalidade fixa independente do volume",
            "Previsibilidade total de custos mensais",
            "Sem percentual sobre as vendas",
            "Valor e condições negociáveis",
            "Ideal para lojas com alto volume digital",
        ],
        "Negociável — consulte condições",
    )

    cards_table = Table([[card1, card2]], colWidths=[pw_card, pw_card])
    cards_table.setStyle(TableStyle([
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",   (1, 0), (1, 0), 0.6 * cm),
    ]))
    story.append(cards_table)

    story.append(Spacer(1, 0.6 * cm))
    story.append(Paragraph("O que está incluído em ambos os planos:", ST["sub"]))

    incluso = [
        "Plataforma completa com <b>todas</b> as funcionalidades sem custo adicional",
        "Integração com estoque Alpha e Automatiza — sem configuração extra da loja",
        "Catálogo de imagens com base oficial ANVISA/CMED",
        "Onboarding guiado: loja vendendo em <b>menos de 24 horas</b>",
        "Atualizações e novas funcionalidades incluídas automaticamente",
        "Hospedagem, banco de dados e CDN incluídos — zero infraestrutura da loja",
        "Suporte técnico para configuração inicial",
    ]
    for item in incluso:
        story.append(Paragraph(f"✓  {item}", ST["bullet"]))

    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════
    # 9. ROI
    # ════════════════════════════════════════════════════════════════════════
    story.append(NamedAnchor("s9", "9. Projeções de Retorno (ROI)", level=0))
    story.append(Paragraph("9. Projeções de Retorno (ROI)", ST["section"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=YELLOW, spaceAfter=10))
    story.append(Paragraph(
        "Simulações baseadas nos dados da seção 2, considerando farmácia com "
        "R$ 80.000/mês de faturamento físico — Plano Performance (8%).", ST["body"]))

    story.append(Spacer(1, 0.4 * cm))

    roi_data = [
        [Paragraph("<b>Cenário</b>", ST["th"]),
         Paragraph("<b>Fat. Digital/mês</b>", ST["th"]),
         Paragraph("<b>Taxa (8%)</b>", ST["th"]),
         Paragraph("<b>Receita líquida loja*</b>", ST["th"]),
         Paragraph("<b>ROI sobre a taxa</b>", ST["th"])],
        [Paragraph("Conservador (5%)", ST["tc"]),
         Paragraph("R$ 4.000", ST["tn"]),
         Paragraph("R$ 320", ST["tn"]),
         Paragraph("R$ 3.680", ST["tn"]),
         Paragraph("11,5x", ST["tn"])],
        [Paragraph("Moderado (15%)", ST["tc"]),
         Paragraph("R$ 12.000", ST["tn"]),
         Paragraph("R$ 960", ST["tn"]),
         Paragraph("R$ 11.040", ST["tn"]),
         Paragraph("11,5x", ST["tn"])],
        [Paragraph("Otimista (23%)", ST["tc"]),
         Paragraph("R$ 18.400", ST["tn"]),
         Paragraph("R$ 1.472", ST["tn"]),
         Paragraph("R$ 16.928", ST["tn"]),
         Paragraph("11,5x", ST["tn"])],
    ]

    rcw = [CW * v for v in [0.24, 0.19, 0.15, 0.24, 0.18]]
    rt = Table(roi_data, colWidths=rcw)
    rt.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), RED),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, LGRAY, colors.HexColor("#e8f5e9")]),
        ("GRID",          (0, 0), (-1, -1), 0.5, BORDG),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 7),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME",      (0, 3), (-1, 3), "Helvetica-Bold"),
        ("TEXTCOLOR",     (0, 3), (-1, 3), GREEN),
    ]))
    story.append(rt)
    story.append(Paragraph(
        "* Receita líquida estimada descontando apenas a taxa da plataforma. "
        "Não inclui custos de entrega, embalagem nem taxas de gateway de pagamento.", ST["caption"]))

    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph("Receita mensal da plataforma conforme número de lojas ativas (cenário moderado)", ST["sub"]))

    story.append(rede_bar_chart(CW, 6.5 * cm))
    story.append(Paragraph(
        "Receita mensal da plataforma (8% sobre R$12.000/loja). "
        "Cenário moderado — crescimento real pode ser superior.", ST["caption"]))

    story.append(Spacer(1, 0.4 * cm))

    rede_data = [
        [Paragraph("<b>Lojas Ativas</b>", ST["th"]),
         Paragraph("<b>Fat. Digital Total</b>", ST["th"]),
         Paragraph("<b>Receita Plataforma/mês</b>", ST["th"]),
         Paragraph("<b>Receita Anual</b>", ST["th"])],
        [Paragraph("10 lojas", ST["tc"]), Paragraph("R$ 120.000", ST["tn"]),
         Paragraph("R$ 9.600", ST["tn"]),   Paragraph("R$ 115.200", ST["tn"])],
        [Paragraph("25 lojas", ST["tc"]), Paragraph("R$ 300.000", ST["tn"]),
         Paragraph("R$ 24.000", ST["tn"]),  Paragraph("R$ 288.000", ST["tn"])],
        [Paragraph("50 lojas", ST["tc"]), Paragraph("R$ 600.000", ST["tn"]),
         Paragraph("R$ 48.000", ST["tn"]),  Paragraph("R$ 576.000", ST["tn"])],
        [Paragraph("100 lojas", ST["tc"]), Paragraph("R$ 1.200.000", ST["tn"]),
         Paragraph("R$ 96.000", ST["tn"]),
         Paragraph("<b>R$ 1.152.000</b>",
                   ParagraphStyle("tn_g", fontName="Helvetica-Bold", fontSize=9.5,
                                   textColor=GREEN, alignment=TA_CENTER, leading=13))],
    ]

    rncw = [CW * v for v in [0.20, 0.27, 0.27, 0.26]]
    rnt = Table(rede_data, colWidths=rncw)
    rnt.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), DARK),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, LGRAY, WHITE, LGRAY]),
        ("GRID",          (0, 0), (-1, -1), 0.5, BORDG),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 7),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME",      (0, 4), (-1, 4), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 4), (-1, 4), 10.5),
    ]))
    story.append(rnt)

    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════
    # 10. PRÓXIMOS PASSOS
    # ════════════════════════════════════════════════════════════════════════
    story.append(NamedAnchor("s10", "10. Próximos Passos", level=0))
    story.append(Paragraph("10. Próximos Passos", ST["section"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=YELLOW, spaceAfter=10))
    story.append(Paragraph(
        "A plataforma já está desenvolvida e operacional. Onboarding de novas lojas "
        "em menos de 24 horas, sem desenvolvimento adicional.", ST["body"]))

    story.append(Spacer(1, 0.3 * cm))

    STEPS = [
        ("01", "Alinhamento Contratual",
         "Definição do modelo de cobrança e termos de uso entre a licenciadora e o operador da plataforma."),
        ("02", "Mapeamento das Lojas",
         "Levantamento das lojas que aderirão na primeira fase — priorizando as com maior faturamento."),
        ("03", "Onboarding das Lojas",
         "Cadastro da loja, geocodificação, configuração de pagamentos e publicação do catálogo. "
         "Processo completo em menos de 24h por loja."),
        ("04", "Treinamento do Gestor",
         "Sessão de 1h de treinamento remoto para o responsável operar o painel com total autonomia."),
        ("05", "Go-live e Divulgação",
         "Publicação das lojas na plataforma e suporte à divulgação do canal digital "
         "(QR code, WhatsApp, redes sociais)."),
        ("06", "Acompanhamento e Relatórios",
         "Dashboard mensal com métricas de cada loja: pedidos, faturamento, ticket médio e crescimento."),
    ]

    for num, title, desc in STEPS:
        step_row = Table([[
            Paragraph(f"<b>{num}</b>",
                      ParagraphStyle("sn", fontName="Helvetica-Bold", fontSize=16,
                                     textColor=WHITE, alignment=TA_CENTER)),
            [Paragraph(f"<b>{title}</b>",
                        ParagraphStyle("st", fontName="Helvetica-Bold", fontSize=11,
                                       textColor=DARK, spaceAfter=3)),
             Paragraph(desc, ParagraphStyle("sd", fontName="Helvetica", fontSize=9.5,
                                             textColor=GRAY, leading=14))],
        ]], colWidths=[1.2 * cm, None])
        step_row.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (0, 0), RED),
            ("BACKGROUND",    (1, 0), (1, 0), WHITE),
            ("BOX",           (0, 0), (-1, -1), 0.8, BORDG),
            ("TOPPADDING",    (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ("LEFTPADDING",   (0, 0), (0, 0), 4),
            ("LEFTPADDING",   (1, 0), (1, 0), 12),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("ALIGN",         (0, 0), (0, 0), "CENTER"),
        ]))
        story.append(KeepTogether([step_row, Spacer(1, 0.18 * cm)]))

    story.append(Spacer(1, 0.5 * cm))

    # CTA FINAL
    cta_bg = Drawing(CW, 3.8 * cm)
    cta_bg.add(Rect(0, 0, CW, 3.8 * cm, fillColor=RED, strokeColor=None))
    cta_bg.add(Rect(0, 0, CW, 0.4 * cm, fillColor=YELLOW, strokeColor=None))
    cta_bg.add(Rect(0, 3.4 * cm, CW, 0.4 * cm, fillColor=colors.HexColor("#a0001e"), strokeColor=None))
    story.append(cta_bg)
    story.append(Spacer(1, -3.3 * cm))
    story.append(Paragraph(
        "Pronto para levar as lojas Poupaqui para o digital?",
        ParagraphStyle("cta1", fontName="Helvetica-Bold", fontSize=16, textColor=WHITE, alignment=TA_CENTER, leading=20)))
    story.append(Spacer(1, 0.25 * cm))
    story.append(Paragraph(
        "Entre em contato para uma demonstração ao vivo da plataforma.",
        ParagraphStyle("cta2", fontName="Helvetica", fontSize=11, textColor=YELLOW, alignment=TA_CENTER)))
    story.append(Spacer(1, 0.25 * cm))
    story.append(Paragraph(
        "emano4775@gmail.com",
        ParagraphStyle("cta3", fontName="Helvetica-Bold", fontSize=13, textColor=WHITE, alignment=TA_CENTER)))
    story.append(Spacer(1, 1.5 * cm))
    story.append(Paragraph(
        "Este documento é confidencial e destinado exclusivamente aos representantes da rede Poupaqui.",
        ST["foot"]))

    # ── BUILD ────────────────────────────────────────────────────────────────
    class _Canvas(BrandedCanvas):
        def __init__(self, filename, **kwargs):
            super().__init__(filename, **kwargs)

    doc.build(story, canvasmaker=_Canvas)
    print(f"PDF gerado: {filename}")


if __name__ == "__main__":
    out = r"c:\Users\emano\dns-ecommerce\Poupaqui_Ecommerce_Proposta_Comercial.pdf"
    build_pdf(out)
