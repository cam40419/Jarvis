'use strict';
(() => {
  const panel = el('home-panel'), grid = el('home-grid'), notice = el('home-status');
  let devices = [], generation = 0, pollSeconds = 60;
  const path = device => '/v1/home/devices/' + encodeURIComponent(device.id);
  const uncertain = new Set();
  function button(text, action) { const b = make('button', text, 'text-button'); b.type = 'button'; b.onclick = action; return b; }
  function chart(samples, start, end, gap) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 600 150'); svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Power usage in watts over time. Missing readings appear as gaps.');
    const maximum = Math.max(1, ...samples.map(s => s.state === 'ok' ? s.reading?.watts || 0 : 0));
    let points = [], previous = null;
    const draw = () => {
      if (!points.length) return;
      const line = document.createElementNS(svg.namespaceURI, 'polyline');
      line.setAttribute('points', points.join(' ')); line.setAttribute('fill', 'none');
      line.setAttribute('stroke', 'currentColor'); line.setAttribute('stroke-width', '2'); svg.append(line);
      for (const point of points) { const dot = document.createElementNS(svg.namespaceURI, 'circle'); const [x,y] = point.split(','); dot.setAttribute('cx', x); dot.setAttribute('cy', y); dot.setAttribute('r', '2'); dot.setAttribute('fill', 'currentColor'); svg.append(dot); }
      points = [];
    };
    for (const sample of samples) {
      const time = Date.parse(sample.captured_at), watts = sample.reading?.watts;
      if (sample.state !== 'ok' || !Number.isFinite(watts)) { draw(); previous = null; continue; }
      if (previous !== null && time - previous > gap * 1000) draw();
      points.push(`${10 + 580 * (time - start) / (end - start)},${140 - 125 * watts / maximum}`);
      previous = time;
    }
    draw(); return {svg, maximum};
  }
  async function history(device, hours, destination) {
    const key = crypto.randomUUID(); destination.dataset.request = key;
    destination.textContent = 'Loading power history…';
    try {
      const end = Date.now(), start = end - hours * 3600000;
      const data = await api(path(device) + '/power?start=' + encodeURIComponent(new Date(start).toISOString()) + '&end=' + encodeURIComponent(new Date(end).toISOString()));
      if (destination.dataset.request !== key) return;
      const drawing = chart(data.samples, start, end, pollSeconds * 2.5);
      const s = data.summary;
      const energy = s.observed_energy_kwh == null ? 'Not enough readings yet' : `${s.observed_energy_kwh.toFixed(3)} kWh observed`;
      destination.replaceChildren(make('strong', energy), drawing.svg,
        make('p', `0–${drawing.maximum.toFixed(1)} W · ${new Date(start).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})} to ${new Date(end).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`, 'muted fine'),
        make('p', `${Math.round(s.covered_seconds / s.requested_seconds * 100)}% of this window covered. ${s.gaps} gaps; ${s.counter_resets} meter resets.`, 'muted fine'));
      destination.append(make('p', 'Observed consumption only. Missing readings are not counted as zero.', 'muted fine'));
    } catch (error) { if (destination.dataset.request === key) destination.textContent = error.message; }
  }
  function card(device, power, gen) {
    const row = make('article', undefined, 'home-card'); row.dataset.deviceId = device.id;
    row.append(make('p', `${device.provider.toUpperCase()} · ${device.load_type.replaceAll('_', ' ')}`, 'eyebrow'), make('h3', device.name));
    const status = make('p', device.present ? 'Checking status…' : 'Unavailable', 'muted');
    const actions = make('div', undefined, 'home-actions'); let state = null, working = false;
    const on = button('Turn on', () => control({on: true})), off = button('Turn off', () => control({on: false}));
    const check = button('Check status', read);
    const brightness = make('input'); brightness.type = 'range'; brightness.min = 1; brightness.max = 100; brightness.value = 50;
    const color = make('input'); color.type = 'color'; color.value = '#ffffff';
    const brightnessLabel = make('label', 'Brightness'); brightnessLabel.append(brightness);
    const colorLabel = make('label', 'Color'); colorLabel.append(color);
    const apply = button('Apply light settings', () => control({
      ...(state?.capabilities.includes('brightness') ? {brightness: Number(brightness.value)} : {}),
      ...(state?.capabilities.includes('color') ? {color: color.value} : {}),
    }));
    const lighting = make('div', undefined, 'home-lighting'); lighting.append(brightnessLabel, colorLabel, apply); lighting.hidden = true;
    function controls() {
      on.disabled = off.disabled = working || !device.control_enabled || uncertain.has(device.id) || !state?.online;
      apply.disabled = on.disabled; check.disabled = working;
      brightness.disabled = color.disabled = working;
    }
    function show(value) {
      state = value;
      status.textContent = value.online ? `${value.on == null ? 'Available' : value.on ? 'On' : 'Off'}${value.watts == null ? '' : ' · ' + value.watts.toFixed(1) + ' W'}` : 'Offline';
      brightnessLabel.hidden = !value.capabilities.includes('brightness'); colorLabel.hidden = !value.capabilities.includes('color');
      lighting.hidden = device.provider === 'shelly' || (brightnessLabel.hidden && colorLabel.hidden);
      if (value.brightness != null) brightness.value = value.brightness;
      if (value.color) color.value = value.color;
    }
    async function read() {
      if (working) return;
      working = true; controls();
      try { const value = await api(path(device) + '/status'); if (gen !== generation) return; show(value); if (value.online && value.on != null) uncertain.delete(device.id); }
      catch (error) { state = null; status.textContent = error.message; }
      finally { working = false; controls(); }
    }
    async function control(change) {
      if (working || on.disabled) return;
      working = true; controls();
      try {
        const result = await api(path(device) + '/control', {...change, idempotency_key: crypto.randomUUID()});
        if (result.observed) show(result.observed);
        if (result.status === 'unknown' || (result.status === 'succeeded' && !result.verified)) uncertain.add(device.id);
        status.textContent = result.verified ? 'Updated and verified.' : result.error || 'Command sent; check status before another change.';
      } catch (error) { uncertain.add(device.id); status.textContent = error.message + ' Check status before retrying.'; }
      finally { working = false; controls(); }
    }
    actions.append(on, off, check); row.append(status, actions, lighting);
    if (device.setup_available) row.append(button('Name & configure outlet', () => {
      panel.close(); el('connections-open').click();
    }));
    if (device.provider === 'shelly') {
      row.append(make('p', (!power || power.stale) ? 'Power reading unavailable or stale' : `${power.latest.reading.watts?.toFixed(1) ?? '—'} W last sampled`, 'muted fine'));
      const details = make('details'), summary = make('summary', 'Power consumption');
      const range = make('select'); range.setAttribute('aria-label', 'Power history period');
      for (const hours of [1, 6, 24]) { const option = make('option', `Last ${hours} hour${hours > 1 ? 's' : ''}`); option.value = hours; range.append(option); }
      range.value = 24; const plot = make('div', undefined, 'home-power-chart');
      range.onchange = () => history(device, Number(range.value), plot);
      details.ontoggle = () => { if (details.open) history(device, Number(range.value), plot); };
      details.append(summary, range, plot); row.append(details);
    }
    controls(); return {row, read};
  }
  async function refresh() {
    const gen = ++generation; notice.textContent = 'Loading your home…';
    el('home-refresh').disabled = true;
    try {
      const [inventory, power] = await Promise.all([api('/v1/home/devices'), api('/v1/home/power')]);
      if (gen !== generation) return;
      devices = inventory; pollSeconds = power.poll_seconds; grid.replaceChildren();
      const reads = [];
      for (const room of [...new Set(devices.map(d => d.room || 'Unassigned'))].sort()) {
        const section = make('section', undefined, 'home-room'); section.append(make('h3', room));
        const cards = make('div', undefined, 'home-cards'); section.append(cards); grid.append(section);
        for (const device of devices.filter(d => (d.room || 'Unassigned') === room)) {
          const item = card(device, power.devices.find(p => p.device_id === device.id), gen); cards.append(item.row); reads.push(item.read);
        }
      }
      notice.textContent = devices.length ? `${devices.length} devices · ${power.monitoring_enabled ? 'Power monitoring enabled' : 'Power monitoring paused'}` : 'No devices yet. Add your provider credentials, then refresh discovery in Connections.';
      // Avoid a burst of simultaneous LAN/cloud requests.
      for (let i = 0; i < reads.length && gen === generation && panel.open; i += 4) await Promise.all(reads.slice(i, i + 4).map(read => read()));
    } catch (error) { notice.textContent = error.message; }
    finally { if (gen === generation) el('home-refresh').disabled = false; }
  }
  el('home-open').onclick = () => { if (ready) { panel.showModal(); refresh(); } };
  el('home-refresh').onclick = refresh;
  el('home-close').onclick = () => panel.close();
})();
