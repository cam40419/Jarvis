(() => {
  const base = document.querySelector('meta[name="simon-base"]')?.content || '';
  let session;
  const api = async (path, options = {}) => {
    const headers = {...(options.headers || {})};
    if (options.method && options.method !== 'GET') headers['x-csrf-token'] = session.csrf_token;
    if (options.body && !(options.body instanceof FormData)) headers['content-type'] = 'application/json';
    const response = await fetch(base + path, {...options, headers});
    if (!response.ok) { const value = await response.json().catch(() => ({})); throw new Error(value.error?.message || 'Request failed'); }
    return response.status === 204 ? null : response.json();
  };
  const node = (tag, text, cls) => { const value = document.createElement(tag); if (text != null) value.textContent = text; if (cls) value.className = cls; return value; };
  const notice = (text) => document.getElementById('notice').textContent = text;
  async function save(id, card) {
    try { await api(`/v1/displays/${id}`, {method: 'PATCH', body: JSON.stringify({layout: card.querySelector('.layout').value, slide_seconds: Number(card.querySelector('.speed').value), dim_percent: Number(card.querySelector('.dim').value)})}); notice('Display updated. The Pi will refresh within 15 seconds.'); await load(); }
    catch (error) { notice(error.message); }
  }
  function card(value) {
    const result = node('article', null, 'device');
    const head = node('div', null, 'device-head'); head.append(node('h2', value.name), node('span', value.online ? 'Online' : 'Offline', value.online ? 'online' : 'offline')); result.append(head);
    const controls = node('div', null, 'controls');
    const layout = node('select', null, 'layout'); ['overlay','split','focus'].forEach(name => { const option = node('option', name); option.value = name; option.selected = name === value.configuration.layout; layout.append(option); });
    const speed = node('input', null, 'speed'); speed.type = 'number'; speed.min = 5; speed.max = 3600; speed.value = value.configuration.slide_seconds;
    const dim = node('input', null, 'dim'); dim.type = 'number'; dim.min = 0; dim.max = 80; dim.value = value.configuration.dim_percent;
    for (const [name, input] of [['Layout',layout],['Seconds per image',speed],['Image dimming %',dim]]) { const label = node('label', name); label.append(input); controls.append(label); }
    const saveButton = node('button', 'Save layout'); saveButton.onclick = () => save(value.id, result); controls.append(saveButton); result.append(controls);
    const upload = node('input'); upload.type = 'file'; upload.accept = 'image/jpeg,image/png,image/webp,image/gif'; upload.multiple = true;
    upload.onchange = async () => { for (const file of upload.files) { const data = new FormData(); data.append('image', file); try { await api(`/v1/displays/${value.id}/images`, {method: 'POST', body: data}); } catch (error) { notice(error.message); } } await load(); };
    result.append(upload);
    const images = node('div', null, 'images');
    for (const image of value.images) { const wrap = node('div', null, 'image'), preview = node('img'); preview.src = `${base}/v1/displays/${value.id}/managed-images/${image.id}`; preview.alt = image.filename; const remove = node('button', '×'); remove.title = 'Remove image'; remove.onclick = async () => { await api(`/v1/displays/${value.id}/images/${image.id}`, {method:'DELETE'}); await load(); }; wrap.append(preview, remove); images.append(wrap); }
    result.append(images); return result;
  }
  async function load() { try { const values = await api('/v1/displays'); document.getElementById('list').replaceChildren(...values.map(card)); if (!values.length) notice('No displays yet. Add the Raspberry Pi to begin.'); } catch (error) { notice(error.message); } }
  document.getElementById('add').onclick = () => document.getElementById('create-dialog').showModal();
  document.getElementById('create').onclick = async event => { event.preventDefault(); try { const value = await api('/v1/displays', {method:'POST', body:JSON.stringify({name:document.getElementById('name').value})}); document.getElementById('create-dialog').close(); document.getElementById('provision-url').value = value.provisioning_url; document.getElementById('provision-dialog').showModal(); await load(); } catch (error) { notice(error.message); } };
  document.getElementById('copy').onclick = () => navigator.clipboard.writeText(document.getElementById('provision-url').value);
  document.getElementById('close-provision').onclick = () => document.getElementById('provision-dialog').close();
  (async () => { try { session = await api('/auth/session'); await load(); } catch (error) { notice(error.message); } })();
})();
