/* Independent of regional stock, cart and location permissions. */
(() => {
  const section = document.getElementById('ml-national');
  if (!section) return;
  const row = document.getElementById('ml-national-products');
  const status = document.getElementById('ml-national-status');
  const home = section.dataset.mode === 'home';
  const params = new URLSearchParams(location.search);
  const all = params.get('nacionais') === '1';
  const search = document.getElementById('search-inp');
  const esc = value => String(value || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const brl = value => Number(value).toLocaleString('pt-BR', {style:'currency',currency:'BRL'});
  let generation = 0;
  let timer;
  function place(integrated) {
    const anchor = document.getElementById(integrated ? 'ml-national-secondary' : 'ml-national-priority');
    if (anchor) anchor.after(section);
  }
  // Keep local products first while coverage is being resolved. Without a saved
  // location, national products can already be shown in the priority position.
  place(!!(Number(localStorage.getItem('pq_lat')) || Number(localStorage.getItem('pq_lng'))));
  document.addEventListener('pq:regional-coverage', event => {
    const detail = event.detail || {};
    place(detail.pending || detail.integrated);
  });
  function card(p) {
    const badge = '<span class="ml-national-badge"><i class="fa fa-globe" aria-hidden="true"></i>Envio nacional</span>';
    const button = '<span class="ml-national-link">Ver no Mercado Livre <i class="fa fa-arrow-up-right-from-square" aria-hidden="true"></i></span>';
    const image = `<img src="${esc(p.imagem)}" alt="${esc(p.nome)}" loading="lazy" ${home ? 'class="home-prod-img"' : ''}>`;
    const content = home
      ? `${badge}${image}<div class="home-prod-name">${esc(p.nome)}</div><div class="home-prod-meta">${esc(p.marca)} · Mercado Livre</div><div class="home-prod-price">${brl(p.preco)}</div>${button}`
      : `<div class="prod-img">${image}</div><div class="prod-body">${badge}<div class="prod-nome">${esc(p.nome)}</div><div class="prod-lab">${esc(p.marca)}</div><div class="prod-preco">${brl(p.preco)}</div>${button}</div>`;
    return `<a class="${home ? 'home-prod' : 'prod-card'}" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer" aria-label="${esc(p.nome)} — ver no Mercado Livre, abre em nova aba">${content}</a>`;
  }
  async function load(query) {
    const seq = ++generation;
    if (!home && !all && !query.trim()) { section.hidden = true; return; }
    try {
      const response = await fetch('/api/produtos-nacionais?' + new URLSearchParams({q:query}));
      if (!response.ok) throw new Error('catalog');
      const data = await response.json();
      if (seq !== generation) return;
      row.innerHTML = (data.produtos || []).map(card).join('');
      section.hidden = !data.produtos?.length && !all;
      status.hidden = !!data.produtos?.length;
      status.textContent = 'Nenhum produto nacional disponível para esta busca no momento.';
      if (home && typeof window._homeUpdateCarouselNav === 'function') window._homeUpdateCarouselNav('ml-national-products');
    } catch (_) {
      if (seq !== generation) return;
      row.replaceChildren();
      section.hidden = !all;
      status.hidden = false;
      status.textContent = 'Não foi possível consultar os produtos nacionais agora. Tente novamente em instantes.';
    }
  }
  document.addEventListener('pq:catalog-query', event => {
    ++generation;
    clearTimeout(timer);
    timer = setTimeout(() => load(String(event.detail || '')), 350);
  });
  load(home ? '' : (search?.value || params.get('q') || params.get('busca') || params.get('search') || ''));
})();
