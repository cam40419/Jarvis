'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  let session, entries = new Map(), meter = null, selectedRoom = 'All spaces', generation = 0, bulkBusy = false;
  let polling = false, lastStatusPoll = 0;
  let printerStatus = null, printerAvailable = false, printerBusy = false;
  const devicePath = device => '/v1/home/devices/' + encodeURIComponent(device.id);
  const roomName = device => device.room || 'Unassigned';
  function notice(message, error = false) {
    $('notice').textContent = message;
    $('notice').classList.toggle('error', error);
  }
  async function api(path, body) {
    const response = await fetch(appPath(path), {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
      headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (response.status === 401) { location.assign(appPath('/login')); throw Error('Sign in to continue.'); }
    const value = await response.json();
    if (!response.ok) throw Error(value.error?.message || 'Request failed. Please try again.');
    return value;
  }
  const formatTime = value => new Date(value).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  function updateClock() {
    const now = new Date();
    $('clock').textContent = now.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
    $('today').textContent = now.toLocaleDateString([], {weekday: 'long', month: 'long', day: 'numeric'});
  }
  function metrics() {
    const list = [...entries.values()];
    const lights = list.filter(item => item.device.load_type === 'lighting');
    const lit = lights.filter(item => item.state?.on === true && item.state?.online !== false).length;
    const attention = list.filter(item => !item.device.present || (item.checked && !item.state) || item.state?.online === false || item.uncertain).length + (printerAvailable && printerStatus?.online === false ? 1 : 0);
    const waiting = list.filter(item => item.device.present && !item.checked).length;
    const power = (meter?.devices || []).filter(item => !item.stale && Number.isFinite(item.latest?.reading?.watts));
    const watts = power.reduce((sum, item) => sum + item.latest.reading.watts, 0);
    $('device-count').textContent = list.length + (printerAvailable ? 1 : 0);
    $('device-detail').textContent = `${list.filter(item => item.device.present).length + (printerAvailable && printerStatus?.online ? 1 : 0)} available`;
    $('lights-count').textContent = lit;
    $('lights-detail').textContent = `${lights.length} lighting device${lights.length === 1 ? '' : 's'}`;
    $('power-total').textContent = power.length ? `${watts.toFixed(1)} W` : '—';
    $('power-detail').textContent = power.length ? `Across ${power.length} live meter${power.length === 1 ? '' : 's'}` : 'No current meter readings';
    $('attention-count').textContent = waiting ? '…' : attention;
    $('attention-detail').textContent = waiting ? `Checking ${waiting} device${waiting === 1 ? '' : 's'}` : attention ? 'Check cards below' : 'Everything looks connected';
    const controllableLights = lights.some(item => item.device.control_enabled && item.state?.online !== false &&
      typeof item.state?.on === 'boolean' && item.state.capabilities.includes('power') && !item.uncertain && !item.busy);
    $('all-lights-on').disabled = $('all-lights-off').disabled = bulkBusy || !session.scopes.includes('home:control') || !controllableLights;
    const total = list.length + (printerAvailable ? 1 : 0);
    const spaces = new Set([...list.map(item => roomName(item.device)), ...(printerAvailable ? ['Office'] : [])]);
    $('home-subtitle').textContent = total ? `${total} devices across ${spaces.size} spaces. Your controls are ready below.` : 'Your connected devices will appear here.';
  }
  function row(label, detail, value) {
    const item = node('div', undefined, 'mini-row'), left = node('span');
    left.append(node('strong', label), node('small', detail)); item.append(left, node('strong', value)); return item;
  }
  function renderMeter() {
    const devices = meter?.devices || [];
    $('power-summary').textContent = meter?.monitoring_enabled ? 'Live readings from your monitored outlets.' : 'Monitoring is paused. Last known readings appear below.';
    $('power-devices').replaceChildren(...(devices.length ? devices.map(item => row(
      item.name, item.stale ? 'Stale or unavailable' : `Updated ${formatTime(item.latest.captured_at)}`,
      item.stale || !Number.isFinite(item.latest?.reading?.watts) ? '—' : `${item.latest.reading.watts.toFixed(1)} W`
    )) : [node('p', 'No power meters connected.') ]));
    metrics();
  }
  function renderActivity(commands) {
    $('recent-commands').replaceChildren(...(commands.length ? commands.slice(0, 6).map(command => {
      const item = node('div', undefined, 'mini-row');
      const changes = [command.change?.on === true ? 'On' : command.change?.on === false ? 'Off' : '',
        command.change?.brightness != null ? `${command.change.brightness}%` : '', command.change?.color || ''].filter(Boolean).join(' · ');
      item.append(node('strong', command.device_name), node('p', `${changes || 'Change'} · ${command.status}${command.verified ? ' (verified)' : ''}`), node('small', formatTime(command.created_at)));
      return item;
    }) : [node('p', 'No recent device commands.') ]));
  }
  function renderProviders(providers) {
    $('provider-list').replaceChildren(...providers.map(provider => {
      const detail = provider.provider === 'shelly' ? `${provider.count} registered${provider.error ? '; ' + provider.error : ''}` :
        provider.error || (provider.configured ? `${provider.count} discovered` : 'Not configured');
      return row(provider.provider.toUpperCase(), detail,
        provider.status === 'ready' ? 'Ready' : provider.status.replaceAll('_', ' '));
    }));
  }
  function updateCard(item) {
    const {device, state, card, powerButton, checkButton, brightness, brightnessWrap, color, colorWrap, swatches, colorSet, stateLine, note} = item;
    const online = !!state && state.online !== false && device.present;
    const knownPower = typeof state?.on === 'boolean';
    const allowed = online && device.control_enabled && session.scopes.includes('home:control') && !item.uncertain && !item.busy;
    const caps = state?.capabilities || [];
    card.classList.toggle('offline', !online);
    card.classList.toggle('uncertain', item.uncertain);
    card.classList.toggle('light-on', device.load_type === 'lighting' && state?.on === true);
    stateLine.textContent = !device.present ? 'Unavailable' : state?.online === false ? 'Offline' : !state ? 'Status unavailable' :
      knownPower ? (state.on ? 'On' : 'Off') + (Number.isFinite(state.watts) ? ` · ${state.watts.toFixed(1)} W` : '') : 'Power state unknown';
    powerButton.textContent = state?.on ? 'Turn off' : 'Turn on';
    powerButton.classList.toggle('on', state?.on === true);
    powerButton.disabled = !allowed || !knownPower || !caps.includes('power');
    checkButton.disabled = item.busy;
    brightnessWrap.hidden = !caps.includes('brightness');
    colorWrap.hidden = !caps.includes('color');
    item.lighting.hidden = brightnessWrap.hidden && colorWrap.hidden;
    brightness.disabled = !allowed || !caps.includes('brightness');
    color.disabled = !allowed || !caps.includes('color');
    swatches.querySelectorAll('button').forEach(button => button.disabled = color.disabled);
    colorSet.disabled = color.disabled;
    if (state?.brightness != null && !item.editingBrightness) {
      brightness.value = Math.round(state.brightness);
      item.brightnessOutput.value = `${brightness.value}%`;
    }
    if (state?.color && /^#[0-9a-f]{6}$/i.test(state.color) && !item.editingColor) color.value = state.color;
    note.textContent = item.message || (item.uncertain ? 'Refresh status before another change.' : !device.control_enabled ? 'Read only until control is enabled in settings.' : state?.note || '');
    metrics();
  }
  async function read(item) {
    if (item.busy) return;
    item.busy = true; item.message = 'Checking device…'; updateCard(item);
    try {
      item.state = await api(devicePath(item.device) + '/status');
      item.uncertain = false; item.message = '';
    } catch (error) { item.state = null; item.message = error.message; }
    finally { item.checked = true; item.busy = false; updateCard(item); }
  }
  async function control(item, change) {
    if (item.busy || item.uncertain || !item.state || item.state.online === false) return false;
    item.busy = true; item.message = 'Applying change…'; updateCard(item);
    try {
      let result = await api(devicePath(item.device) + '/control', {...change, idempotency_key: crypto.randomUUID()});
      if (result.observed) item.state = result.observed;
      if (result.status === 'succeeded' && !result.verified && result.id &&
          ['lifx', 'tuya'].includes(item.device.provider)) {
        item.message = 'Waiting for updated cloud status...'; updateCard(item);
        for (const delay of [750, 1500, 2500, 4000]) {
          await new Promise(resolve => setTimeout(resolve, delay));
          try {
            result = await api('/v1/home/commands/' + encodeURIComponent(result.id) + '/verify', {});
            if (result.observed) item.state = result.observed;
            if (result.verified) break;
          } catch { /* Keep the accepted result and let the next read retry. */ }
        }
      }
      item.uncertain = result.status === 'unknown' || (result.status === 'succeeded' && !result.verified);
      item.message = result.status === 'failed' ? result.error || 'Change failed.' : item.uncertain ? 'State not verified yet. Check status before another change.' : 'Updated and verified.';
      if (result.status === 'succeeded') notice(`${item.device.name}: ${item.message}`);
      else notice(`${item.device.name}: ${item.message}`, true);
      api('/v1/home/commands').then(renderActivity).catch(() => {});
      return result.status === 'succeeded' && result.verified;
    } catch (error) { item.uncertain = true; item.message = `${error.message} Refresh status before retrying.`; notice(`${item.device.name}: ${item.message}`, true); return false; }
    finally { item.busy = false; updateCard(item); }
  }
  function settings(item, card) {
    if (!session.scopes.includes('home:organize')) return;
    const details = node('details', undefined, 'device-settings');
    details.append(node('summary', 'Device settings'));
    const form = node('form');
    const field = (label, input) => { const wrapper = node('label', label); wrapper.append(input); form.append(wrapper); return input; };
    const room = field('Room', node('input')); room.value = item.device.room || ''; room.maxLength = 100;
    const groups = field('Groups (comma separated)', node('input')); groups.value = (item.device.groups || []).join(', ');
    const save = node('button', 'Save room and groups'); save.type = 'submit'; form.append(save);
    form.onsubmit = async event => {
      event.preventDefault(); save.disabled = true;
      try {
        const values = groups.value.split(',').map(value => value.trim()).filter(Boolean);
        await api('/v1/home/organize', {device_ids: [item.device.id], room: room.value.trim(), groups: values});
        notice(`${item.device.name} settings saved.`); await refresh();
      } catch (error) { notice(error.message, true); save.disabled = false; }
    };
    if (item.device.setup_available) {
      const type = node('select');
      for (const [value, label] of [['unclassified', 'Unassigned'], ['lighting', 'Light or lamp'], ['air_purifier', 'Air purifier']]) {
        const option = node('option', label); option.value = value; type.append(option);
      }
      type.value = ['lighting', 'air_purifier'].includes(item.device.load_type) ? item.device.load_type : 'unclassified';
      const outletName = field('Outlet name', node('input')); outletName.value = item.device.name; outletName.maxLength = 100;
      field('What this outlet powers', type);
      const enabled = node('input'); enabled.type = 'checkbox'; enabled.checked = item.device.control_enabled;
      const checkLabel = node('label', undefined, 'check-label'); checkLabel.append(enabled, node('span', 'Allow control')); form.append(checkLabel);
      const outletSave = node('button', 'Save outlet setup'); outletSave.type = 'button'; form.append(outletSave);
      outletSave.onclick = async () => {
        outletSave.disabled = true;
        try {
          await api('/v1/home/outlets/' + encodeURIComponent(item.device.id) + '/setup', {
            name: outletName.value.trim(), room: room.value.trim(), load_type: type.value,
            control_enabled: type.value !== 'unclassified' && enabled.checked,
          });
          notice(`${item.device.name} outlet setup saved.`); await refresh();
        } catch (error) { notice(error.message, true); outletSave.disabled = false; }
      };
    }
    details.append(form); card.append(details);
  }
  function powerHistory(item, card) {
    if (item.device.provider !== 'shelly') return;
    const details = node('details', undefined, 'power-history'); details.append(node('summary', 'Power history'));
    const period = node('select'); period.setAttribute('aria-label', 'Power history period');
    for (const hours of [1, 6, 24]) { const option = node('option', `Last ${hours} hour${hours === 1 ? '' : 's'}`); option.value = hours; period.append(option); }
    period.value = 24;
    const plot = node('div'); details.append(period, plot);
    async function loadHistory() {
      const token = crypto.randomUUID(); plot.dataset.token = token; plot.textContent = 'Loading history…';
      try {
        const end = Date.now(), start = end - Number(period.value) * 3600000;
        const value = await api(devicePath(item.device) + '/power?start=' + encodeURIComponent(new Date(start).toISOString()) + '&end=' + encodeURIComponent(new Date(end).toISOString()));
        if (plot.dataset.token !== token) return;
        const samples = value.samples.filter(sample => sample.state === 'ok' && Number.isFinite(sample.reading?.watts));
        if (!samples.length) { plot.textContent = 'No readings in this period.'; return; }
        const max = Math.max(1, ...samples.map(sample => sample.reading.watts));
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 300 90'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'Power readings in watts');
        const line = document.createElementNS(svg.namespaceURI, 'polyline');
        line.setAttribute('points', samples.map(sample => `${Math.max(0, Math.min(300, 300 * (Date.parse(sample.captured_at) - start) / (end - start)))},${82 - 72 * sample.reading.watts / max}`).join(' '));
        line.setAttribute('fill', 'none'); line.setAttribute('stroke', 'currentColor'); line.setAttribute('stroke-width', '2'); svg.append(line);
        const energy = value.summary.observed_energy_kwh;
        plot.replaceChildren(svg, node('p', `${energy == null ? 'Insufficient readings for energy' : energy.toFixed(3) + ' kWh observed'} · Peak ${max.toFixed(1)} W`));
      } catch (error) { if (plot.dataset.token === token) plot.textContent = error.message; }
    }
    details.ontoggle = () => { if (details.open) loadHistory(); };
    period.onchange = loadHistory; card.append(details);
  }
  function createCard(device) {
    const card = node('article', undefined, 'device-card'); card.dataset.deviceId = device.id;
    const top = node('div', undefined, 'device-top'), icon = node('span', device.load_type === 'lighting' ? '☼' : device.load_type === 'air_purifier' ? '≋' : 'ϟ', 'device-icon');
    top.append(icon, node('span', device.provider, 'provider'));
    const title = node('h4', device.name), meta = node('p', `${roomName(device)} · ${device.load_type.replaceAll('_', ' ')}`, 'device-meta');
    const stateLine = node('div', 'Checking status…', 'device-state'), note = node('p', '', 'device-note');
    const actions = node('div', undefined, 'device-actions'), powerButton = node('button', 'Turn on', 'power-toggle'), checkButton = node('button', 'Refresh', 'status-button');
    powerButton.type = checkButton.type = 'button'; actions.append(powerButton, checkButton);
    const lighting = node('div', undefined, 'lighting-controls'), brightnessWrap = node('div', undefined, 'brightness-row');
    const brightnessLabel = node('label', 'Brightness', 'control-label'), brightnessOutput = node('output', '50%');
    const brightness = node('input'); brightness.type = 'range'; brightness.min = 1; brightness.max = 100; brightness.value = 50;
    brightness.setAttribute('aria-label', `${device.name} brightness`);
    brightnessLabel.append(brightnessOutput); brightnessWrap.append(brightnessLabel, brightness);
    const colorWrap = node('div', undefined, 'color-row'), colorLabel = node('label', 'Color'), colorControls = node('div', undefined, 'color-controls'), swatches = node('div', undefined, 'swatches');
    const color = node('input', undefined, 'color-picker'); color.type = 'color'; color.value = '#ffffff'; color.setAttribute('aria-label', `${device.name} color`);
    const colorSet = node('button', 'Set', 'color-set'); colorSet.type = 'button'; colorSet.setAttribute('aria-label', `Set ${device.name} color`);
    colorControls.append(swatches, color, colorSet); colorWrap.append(colorLabel, colorControls); lighting.append(brightnessWrap, colorWrap);
    card.append(top, title, meta, stateLine, note, actions, lighting);
    const item = {device, card, state: null, checked: false, uncertain: false, busy: false, message: '', powerButton, checkButton, brightness, brightnessOutput, brightnessWrap, color, colorWrap, swatches, colorSet, lighting, stateLine, note};
    powerButton.onclick = () => control(item, {on: !item.state.on});
    checkButton.onclick = () => read(item);
    brightness.oninput = () => { item.editingBrightness = true; brightnessOutput.value = `${brightness.value}%`; };
    brightness.onchange = () => { item.editingBrightness = false; control(item, {brightness: Number(brightness.value)}); };
    color.oninput = () => { item.editingColor = true; };
    colorSet.onclick = async () => {
      item.editingColor = true;
      await control(item, {color: color.value});
      item.editingColor = false;
      updateCard(item);
    };
    for (const [name, shade] of [['Red', '#ff0000'], ['Green', '#00ff00'], ['Blue', '#0000ff']]) {
      const button = node('button', name, 'swatch'); button.type = 'button'; button.style.setProperty('--swatch-color', shade);
      button.setAttribute('aria-label', `Choose pure ${name.toLowerCase()} for ${device.name}`);
      button.onclick = () => { color.value = shade; item.editingColor = true; }; swatches.append(button);
    }
    settings(item, card); powerHistory(item, card); updateCard(item); return item;
  }
  async function bulkLights(items, on, label) {
    if (bulkBusy) return;
    bulkBusy = true; metrics();
    const ready = items.filter(item => item.device.control_enabled && item.state?.online !== false &&
      typeof item.state?.on === 'boolean' && item.state.capabilities.includes('power') && !item.uncertain && !item.busy);
    let updated = 0;
    try {
      for (let i = 0; i < ready.length; i += 4) {
        const results = await Promise.all(ready.slice(i, i + 4).map(item => control(item, {on})));
        updated += results.filter(Boolean).length;
      }
      notice(`${label}: updated and verified ${updated} of ${items.length} lights. Check any device with an unverified result.`, updated !== items.length);
    } finally { bulkBusy = false; metrics(); }
  }
  async function readPrinter() {
    try {
      printerStatus = await api('/v1/printers/a1/status');
      printerAvailable = true;
    } catch (error) {
      if (!printerAvailable) return;
      printerStatus = {online: false, state: 'unknown', message: error.message};
    }
    renderRooms(); metrics();
  }
  async function printerCommand(action) {
    if (printerBusy) return;
    printerBusy = true; renderRooms();
    const label = action === 'home' ? 'Homing' : 'Plate swap';
    notice(`${label} requested. Waiting for the printer…`);
    try {
      const result = await api('/v1/printers/a1/control', {action});
      notice(`${label} ${result.state} by printer. Check physical motion.`);
      await readPrinter();
    } catch (error) { notice(error.message, true); }
    finally { printerBusy = false; renderRooms(); }
  }
  function createPrinterCard() {
    const status = printerStatus || {online: false, state: 'unknown'};
    const card = node('article', undefined, 'device-card printer-device');
    card.dataset.deviceId = 'bambu-a1';
    if (!status.online) card.classList.add('offline');
    const top = node('div', undefined, 'device-top');
    top.append(node('span', '3D', 'device-icon'), node('span', 'Bambu Lab', 'provider'));
    card.append(top, node('h4', 'Bambu Lab A1'), node('p', 'Office · 3D printer', 'device-meta'));
    const state = status.online ? status.state : 'Offline';
    card.append(node('div', state, 'device-state'));
    const homed = status.online ? `Homed axes: ${status.homed_axes || 'none'}` : status.message || 'Printer unavailable';
    const progress = status.job_name ? ` · ${status.job_name}${status.progress_percent == null ? '' : ` · ${status.progress_percent}%`}` : '';
    card.append(node('p', homed + progress, 'device-note'));
    const actions = node('div', undefined, 'device-actions');
    const home = node('button', 'Home X, Y, Z', 'status-button');
    const swap = node('button', 'Swap plate', 'power-toggle');
    home.type = swap.type = 'button';
    const ready = status.online && (['IDLE', 'FINISH'].includes(status.state) || status.stopped_job_ready === true) && !status.alerts && !printerBusy && session.scopes.includes('jobs:write');
    home.disabled = !ready;
    swap.disabled = !ready || status.homed_axes !== 'XYZ';
    home.onclick = () => printerCommand('home');
    swap.onclick = () => printerCommand('swap');
    actions.append(home, swap); card.append(actions);
    const details = node('a', 'Printer details and print queue', 'printer-detail-link');
    details.href = appPath('/automations'); card.append(details);
    return card;
  }
  function renderRooms() {
    const list = [...entries.values()], rooms = [...new Set([...list.map(item => roomName(item.device)), ...(printerAvailable ? ['Office'] : [])])].sort((a, b) => a.localeCompare(b));
    if (selectedRoom !== 'All spaces' && !rooms.includes(selectedRoom)) selectedRoom = 'All spaces';
    $('room-filters').replaceChildren(...['All spaces', ...rooms].map(room => {
      const button = node('button', room); button.type = 'button'; button.setAttribute('aria-pressed', String(room === selectedRoom));
      button.onclick = () => { selectedRoom = room; renderRooms(); }; return button;
    }));
    if (!list.length && !printerAvailable) {
      const empty = node('div', undefined, 'empty-state'); empty.append(node('strong', 'No devices yet'), node('span', 'Add provider credentials or use Find devices to check again.'));
      $('room-sections').replaceChildren(empty); return;
    }
    $('room-sections').replaceChildren(...rooms.filter(room => selectedRoom === 'All spaces' || selectedRoom === room).map(room => {
      const section = node('section', undefined, 'room-block'), heading = node('div', undefined, 'room-heading');
      const items = list.filter(item => roomName(item.device) === room), title = node('h3', room);
      const count = items.length + (room === 'Office' && printerAvailable ? 1 : 0);
      title.append(node('small', `${count} device${count === 1 ? '' : 's'}`)); heading.append(title);
      const lights = items.filter(item => item.device.load_type === 'lighting' && item.device.control_enabled);
      if (lights.length && session.scopes.includes('home:control')) {
        const actions = node('div', undefined, 'room-actions');
        for (const [label, on] of [['All on', true], ['All off', false]]) {
          const button = node('button', label); button.type = 'button';
          button.onclick = async () => {
            button.disabled = true;
            await bulkLights(lights, on, room);
            button.disabled = false;
          }; actions.append(button);
        }
        heading.append(actions);
      }
      const grid = node('div', undefined, 'device-grid'); grid.append(...items.map(item => item.card));
      if (room === 'Office' && printerAvailable) grid.append(createPrinterCard());
      section.append(heading, grid); return section;
    }));
  }
  async function pollStatuses() {
    if (polling || bulkBusy || [...entries.values()].some(item => item.busy)) return;
    polling = true;
    const ticket = generation;
    try {
      const items = [...entries.values()].filter(item => item.device.present);
      for (let i = 0; i < items.length && ticket === generation; i += 4) {
        await Promise.all(items.slice(i, i + 4).map(read));
      }
      if (ticket !== generation) return;
      lastStatusPoll = Date.now();
      $('last-checked').textContent = `Status checked ${formatTime(lastStatusPoll)} · updates every 30 seconds`;
      try { meter = await api('/v1/home/power'); renderMeter(); }
      catch { /* Device status is still current if metering is unavailable. */ }
      await readPrinter();
    } finally { polling = false; }
  }
  async function refresh() {
    if (polling || bulkBusy || [...entries.values()].some(item => item.busy)) { notice('Wait for the current device update to finish.'); return; }
    const ticket = ++generation;
    $('refresh-home').disabled = true; notice('Updating your home…');
    try {
      const result = await Promise.allSettled([api('/v1/home/devices'), api('/v1/home/power'), api('/v1/home/discovery'), api('/v1/home/commands'), api('/v1/printers/a1/status')]);
      if (ticket !== generation) return;
      const [inventory, power, providers, commands, printer] = result;
      if (inventory.status !== 'fulfilled') throw inventory.reason;
      entries = new Map(inventory.value.map(device => { const item = createCard(device); return [device.id, item]; }));
      printerAvailable = printer.status === 'fulfilled';
      printerStatus = printerAvailable ? printer.value : null;
      meter = power.status === 'fulfilled' ? power.value : null;
      renderRooms(); renderMeter();
      if (providers.status === 'fulfilled') renderProviders(providers.value);
      else $('provider-list').textContent = providers.reason.message;
      if (commands.status === 'fulfilled') renderActivity(commands.value);
      else $('recent-commands').textContent = commands.reason.message;
      notice(`Updated ${formatTime(new Date())}.`);
      await pollStatuses();
    } catch (error) { notice(error.message, true); }
    finally { if (ticket === generation) $('refresh-home').disabled = false; }
  }
  $('refresh-home').onclick = refresh;
  $('all-lights-on').onclick = () => bulkLights([...entries.values()].filter(item => item.device.load_type === 'lighting'), true, 'All lights');
  $('all-lights-off').onclick = () => bulkLights([...entries.values()].filter(item => item.device.load_type === 'lighting'), false, 'All lights');
  $('discover-home').onclick = async () => {
    $('discover-home').disabled = true; notice('Looking for devices…');
    try { await api('/v1/home/discovery/refresh', {}); await refresh(); }
    catch (error) { notice(error.message, true); }
    finally { $('discover-home').disabled = false; }
  };
  $('refresh-power').onclick = async () => {
    $('refresh-power').disabled = true; notice('Taking new meter readings…');
    try { meter = await api('/v1/home/power/refresh', {}); renderMeter(); notice('Meter readings updated.'); }
    catch (error) { notice(error.message, true); }
    finally { $('refresh-power').disabled = false; }
  };
  async function init() {
    updateClock(); setInterval(updateClock, 30000);
    try {
      session = await api('/auth/session');
      const member = session.memberships.find(value => value.household_id === session.household_id);
      $('account-name').textContent = member?.display_name || 'Account';
      $('avatar').textContent = (member?.display_name || 'S').split(/\s+/).map(word => word[0]).slice(0, 2).join('').toUpperCase();
      await refresh();
      setInterval(() => { if (!document.hidden && !$('refresh-home').disabled) pollStatuses(); }, 30000);
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden && !$('refresh-home').disabled && Date.now() - lastStatusPoll > 15000) pollStatuses();
      });
    } catch (error) { notice(error.message, true); }
  }
  init();
})();
