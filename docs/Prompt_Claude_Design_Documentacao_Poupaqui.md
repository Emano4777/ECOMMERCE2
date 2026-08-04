# Prompt para Claude Design

Copie o texto abaixo e anexe também:

- `Documentacao_Completa_Ecommerce_Poupaqui.md`
- a pasta `assets/documentacao` com as imagens;
- o logotipo oficial da Poupaqui;
- `Custos_Ecommerce_Poupaqui.xlsx` (ou a versão CSV, caso a ferramenta prefira texto tabular).

---

Crie uma experiência web interativa, responsiva e premium a partir da documentação anexada do Poupaqui Ecommerce. O objetivo é apresentar o projeto para dois públicos: diretoria da Licence Farma e donos de lojas licenciadas. O material precisa funcionar como apresentação comercial, central de treinamento, calculadora de custos e manual operacional.

Identidade visual:

- Utilize a identidade Poupaqui existente.
- Cor principal: vermelho profundo próximo de `#c8102e`.
- Cor de destaque: amarelo próximo de `#f5c842`.
- Fundo principal claro, com seções escuras pontuais para contraste.
- Tipografia moderna, acolhedora e muito legível.
- Aparência confiável, farmacêutica e tecnológica, sem parecer um template genérico de startup.
- Use o logotipo fornecido; não redesenhe nem deforme a marca.

Estrutura de navegação:

1. Hero com mensagem “Sua loja física também pode vender onde o cliente está: no celular”.
2. Resumo executivo em cards.
3. Seção “Por que participar” com dados de mercado e fontes clicáveis.
4. Demonstração visual da jornada consumidor → plataforma → loja → entrega.
5. Tour do produto com as capturas reais anexadas.
6. Comparador de responsabilidades: Licence Farma, loja, entregador e consumidor.
7. Tutorial interativo da loja em formato stepper/checklist.
8. Tutorial de instalação PWA com abas Android e iPhone, e alternância consumidor/painel.
9. Catálogo das funcionalidades por perfil.
10. Arquitetura técnica em diagrama visual.
11. Calculadora de custos.
12. Calculadora de IA.
13. Checklist sanitário, LGPD e segurança.
14. Onboarding de novo licenciado em cinco fases.
15. Indicadores e plano de 90 dias.
16. CTA final para adesão do licenciado.

Interações obrigatórias:

- Menu lateral ou superior fixo com progresso de leitura.
- Alternância “Visão comercial” e “Visão operacional”.
- Filtro de conteúdo por perfil: Licence Farma, loja, entregador ou consumidor.
- Cards expansíveis para evitar páginas excessivamente longas.
- Tour de imagens com lightbox e legendas.
- Checklists persistidos em `localStorage`, sem necessidade de backend.
- Calculadora mensal de custos com:
  - câmbio editável;
  - planos selecionáveis de Vercel, Supabase, Cloudinary, Resend, WasenderAPI e OCR;
  - campos de Google Maps, domínio, marketing, suporte e logística;
  - custos de Mercado Pago e Mercado Livre como percentuais sobre faturamento;
  - subtotal fixo, variável, total mensal e custo por pedido;
  - indicação visual de quais valores são estimativas e quais foram informados pelo usuário.
- Calculadora de IA com campos de buscas, atendimentos, personalizações, descrições e análises visuais por mês. Use as fórmulas e preços unitários da documentação, permitindo editar tokens e preços dos modelos.
- Gráficos simples para composição de custos e projeção de três cenários: piloto, crescimento e escala.
- Botão para imprimir/exportar uma versão limpa em PDF usando CSS de impressão.
- Botão “Copiar checklist de implantação”.
- Links externos sempre identificados como fonte.

Regras de conteúdo:

- Não invente estatísticas, custos, funcionalidades ou garantias.
- Preserve números, datas, ressalvas e links da documentação.
- Não afirme que o e-commerce supera todo o varejo físico em faturamento.
- Destaque corretamente que o canal digital farmacêutico acompanhado pela Abrafarma alcançou R$ 21,58 bilhões e cresceu 54,82% no período citado.
- Separe claramente preço público, estimativa, custo contratado e custo por uso.
- Mostre aviso de que taxas, câmbio e planos mudam e devem ser validados nas faturas.
- A seção “Diagnóstico interno” deve ficar oculta na visão comercial e acessível apenas por um controle chamado “Modo interno”. Não implemente autenticação falsa; apenas sinalize que o conteúdo é reservado.
- Não exiba credenciais, tokens, dados de clientes ou campos que estejam mascarados nas imagens.
- O checklist regulatório deve informar que não substitui assessoria jurídica, sanitária ou farmacêutica.

Requisitos de acessibilidade e qualidade:

- HTML semântico e navegação completa por teclado.
- Contraste WCAG AA.
- Estados de foco visíveis.
- Textos alternativos nas imagens.
- Layout perfeito em celular, tablet e desktop.
- Respeitar `prefers-reduced-motion`.
- Evitar animações excessivas; usar movimento apenas para orientar.
- Não usar bibliotecas pesadas sem necessidade.
- Não depender de backend para funcionar.

Entrega técnica:

- Gere uma aplicação web completa e executável.
- Prefira React + TypeScript + Tailwind se o ambiente já suportar; caso contrário, entregue HTML, CSS e JavaScript organizados.
- Componentize navegação, cards, calculadoras, tabelas, checklists, fontes e galeria.
- Centralize todos os preços e premissas em um único objeto configurável.
- Inclua dados de exemplo claramente marcados.
- Inclua README com comandos para executar, editar preços e substituir imagens.
- Antes de finalizar, verifique responsividade, cálculos, links, impressão e ausência de conteúdo inventado.

Tom de comunicação:

- Profissional, claro, convincente e humano.
- Falar com o dono da loja em linguagem direta.
- Demonstrar valor sem promessas de faturamento garantido.
- Reforçar que digital e físico trabalham juntos.

Mensagem central:

“A loja já tem confiança, estoque e presença local. O Poupaqui Ecommerce acrescenta alcance digital, conveniência e inteligência para transformar essa estrutura em uma operação omnichannel.”
