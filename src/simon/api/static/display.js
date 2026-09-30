(() => {
  const root = document.getElementById('display');
  const id = location.pathname.split('/').filter(Boolean).at(-1);
  const base = document.querySelector('meta[name="simon-base"]')?.content || '';
  let revision = 0, slideIndex = -1, timer, state;
  const el = (tag, text, cls) => { const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node; };
  const imageUrl = image => `${base}/v1/displays/${id}/images/${image.id}`;
  function slides(images, seconds) {
    const host = document.getElementById('slides');
    const ids = [...host.children].map(node => node.dataset.id).join();
    if (ids === images.map(image => image.id).join() && host.dataset.seconds === String(seconds)) return;
    host.dataset.seconds = String(seconds);
    clearInterval(timer); host.replaceChildren(); slideIndex = -1;
    if (!images.length) host.append(el('div', null, 'empty-art'));
    for (const image of images) { const node = el('div', null, 'slide'); node.dataset.id = image.id; node.style.backgroundImage = `url("${imageUrl(image)}")`; host.append(node); }
    const advance = () => { const nodes = [...host.querySelectorAll('.slide')]; if (!nodes.length) return; slideIndex = (slideIndex + 1) % nodes.length; nodes.forEach((node, i) => node.classList.toggle('active', i === slideIndex)); };
    advance(); if (images.length > 1) timer = setInterval(advance, seconds * 1000);
  }
  function widget(value, data) {
    const card = el('article', null, `widget ${value.position}`);
    if (value.title) card.append(el('h2', value.title));
    if (value.kind === 'clock') { const now = new Date(); card.append(el('p', now.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}), 'big'), el('p', now.toLocaleDateString([], {weekday: 'long', month: 'long', day: 'numeric'}), 'muted')); }
    if (value.kind === 'power') { const devices = data.power?.devices || []; const watts = devices.reduce((sum, item) => sum + (item.latest?.reading?.watts || 0), 0); card.append(el('p', `${watts.toFixed(0)} W`, 'big'), el('p', `${devices.length} monitored device${devices.length === 1 ? '' : 's'}`, 'muted')); }
    if (value.kind === 'running_jobs') { if (!data.jobs.length) card.append(el('p', 'Nothing running', 'muted')); for (const job of data.jobs.slice(0, 4)) card.append(el('p', `${job.title} · ${job.status.replace('_', ' ')}`)); }
    if (value.kind === 'printer') { const printer = data.printer; card.append(el('p', printer?.online ? printer.state : 'Offline', 'big')); if (printer?.job_name) card.append(el('p', `${printer.job_name}${printer.progress_percent == null ? '' : ` · ${printer.progress_percent}%`}`, 'muted')); }
    if (value.kind === 'message') card.append(el('p', value.message, 'big'));
    return card;
  }
  function render(payload) {
    state = payload; const display = payload.display, config = display.configuration;
    root.dataset.layout = config.layout; document.getElementById('shade').style.opacity = String(config.dim_percent / 100);
    slides(display.images, config.slide_seconds);
    document.getElementById('widgets').replaceChildren(...config.widgets.map(value => widget(value, payload.data)));
    revision = display.revision; document.getElementById('status').textContent = `Connected · ${display.name}`;
  }
  async function poll() {
    try { const response = await fetch(`${base}/v1/displays/${id}/state`, {cache: 'no-store'}); if (!response.ok) throw new Error('Display unavailable'); const value = await response.json(); render(value); }
    catch (error) { document.getElementById('status').textContent = `${error.message} · retrying`; }
  }
  poll(); setInterval(poll, 15000); setInterval(() => { if (state) render(state); }, 30000);
})();
