'use strict';
let googleConnection;
async function connections() {
  await homeDevices();
  googleConnection = await api('/v1/connections/google');
  el('web-connection').textContent = assistant.web_search ? 'Ready · Public web search is enabled' : 'Unavailable · Enable OpenAI and web search on the server';
  el('google-connection').textContent = googleConnection.connected ? 'Connected as ' + googleConnection.email +
    (googleConnection.calendar ? ' · Calendar' : '') + (googleConnection.email_send ? ' · Send email' : '') :
    (googleReturn === 'failed' ? 'Connection did not finish. Check setup and try connecting again.' : 'Not connected');
  el('google-connect').textContent = googleConnection.connected ? 'Reconnect Google' : 'Connect Google';
  el('google-connect').disabled = !googleConnection.configured || !el('google-sharing').checked;
  el('google-disconnect').hidden = !googleConnection.connected;
  el('google-setup').textContent = googleConnection.configured ? 'Google will ask which permissions you want to grant.' :
    'Server setup needed: follow docs/runbooks/google.md, then restart Simon. OAuth redirect: ' + googleConnection.redirect_uri;
}
el('connections-open').onclick = () => { openPanel('connections-panel'); connections().catch(report); };
el('google-sharing').onchange = () => { el('google-connect').disabled = !googleConnection?.configured || !el('google-sharing').checked; };
el('google-connect').onclick = async () => {
  el('google-connect').disabled = true;
  try {
    const result = await api('/v1/connections/google/start', {shared_chat_acknowledged: true});
    const url = new URL(result.url);
    if (url.origin !== 'https://accounts.google.com') throw Error('Unexpected connection address.');
    location.assign(url.href);
  } catch (error) { el('google-connection').textContent = error.message; el('google-connect').disabled = false; }
};
el('google-disconnect').onclick = async () => {
  el('google-disconnect').disabled = true;
  try { await api('/v1/connections/google/disconnect', {}); await connections(); }
  catch (error) { el('google-connection').textContent = error.message; }
  finally { el('google-disconnect').disabled = false; }
};
function externalLink(label, target) {
  const link = make('a', label);
  try {
    const url = new URL(target);
    if (!['https:', 'http:'].includes(url.protocol)) return make('span', label);
    link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
  } catch { return make('span', label); }
  return link;
}
function showConnectedResults(answer) {
  const article = [...el('messages').children].find(node => node.dataset.messageId === answer.output_message_id);
  if (!article) return;
  if (answer.web_sources?.length) {
    const sources = make('details', undefined, 'answer-sources');
    sources.append(make('summary', 'Sources · ' + answer.web_sources.length));
    const list = make('ol');
    for (const source of answer.web_sources) {
      const item = make('li'); item.append(externalLink(source.title || source.url, source.url)); list.append(item);
    }
    sources.append(list); article.append(sources);
  }
  for (const action of answer.actions || []) article.append(actionCard(action));
  for (const command of answer.home_commands || []) article.append(homeCommandCard(command));
}
function homeCommandCard(command) {
  const card = make('section', undefined, 'action-card');
  card.setAttribute('aria-label', 'Device result');
  card.append(make('strong', command.device_name));
  const change = command.change;
  const settings = [];
  if (change.on != null) settings.push(change.on ? 'On' : 'Off');
  if (change.brightness != null) settings.push(change.brightness + '% brightness');
  if (change.color) settings.push('Color: ' + change.color);
  card.append(make('p', settings.join(' / ')));
  const labels = {
    succeeded: command.verified ? 'Reported device state matches the request.' : 'Command accepted; state not yet verified.',
    failed: 'Could not apply change. ' + (command.error || ''),
    unknown: 'Outcome unknown. Check device status before requesting another command.',
    executing: 'Command started; final result is not available. Check device status.'
  };
  card.append(make('p', labels[command.status], 'action-status'));
  return card;
}
function actionCard(action) {
  const card = make('section', undefined, 'action-card'); card.dataset.actionId = action.id;
  const home = action.home;
  card.setAttribute('aria-label', home ? 'Device preview' : action.kind === 'email.send' ? 'Email preview' : 'Calendar preview');
  const email = action.email;
  card.append(make('h3', home ? 'Previous device request' : email ? 'Review email' : 'Review calendar event'));
  if (!home) card.append(make('p', (email ? 'From: ' : 'Calendar account: ') + action.account_email, 'muted'));
  if (home) {
    card.append(make('strong', action.device_name), make('p', [action.device_room, action.device_provider].filter(Boolean).join(' / ')));
    if (home.on !== null) card.append(make('p', 'Power: ' + (home.on ? 'On' : 'Off')));
    if (home.brightness !== null) card.append(make('p', 'Brightness: ' + home.brightness + '%'));
  } else if (email) {
    card.append(make('p', 'To: ' + email.to), make('strong', email.subject), make('pre', email.body));
  } else {
    const draft = action.calendar;
    card.append(make('strong', draft.title), make('p', 'Starts: ' + draft.start), make('p', 'Ends: ' + draft.end));
    if (draft.location) card.append(make('p', 'Location: ' + draft.location));
    if (draft.description) card.append(make('pre', draft.description));
    card.append(make('p', 'Primary calendar · No invitations', 'muted fine'));
  }
  const expired = new Date(action.expires_at) <= new Date();
  const labels = {pending: expired ? 'Preview expired. Ask Simon for a new one.' : 'Nothing sent or created. Review all details before confirming.',
    executing: 'Action started. Refresh its status shortly. If it stays here, check Google before requesting another.',
    succeeded: email ? 'Email sent. A reservation request still needs a reply from the venue.' : 'Event created in Google Calendar.',
    failed: 'Action failed. ' + (action.error || 'Check the connection before requesting a new preview.'),
    unknown: 'Outcome unknown. Check Google before requesting another attempt; Simon will not send this again.',
    cancelled: 'Preview cancelled. Nothing was sent or created.'};
  if (home) Object.assign(labels, {pending: 'No device change made. Ask Simon again to execute this request directly.',
    executing: 'Command started. Refresh status; check the device before requesting another command.',
    succeeded: action.home_verified ? 'Reported device state matches the request.' : 'Command accepted. The requested device state is not yet verified; refresh device status.',
    unknown: 'Outcome unknown. Check the device before requesting another command.',
    cancelled: 'Preview cancelled. No device change made.'});
  card.append(make('p', labels[action.status], 'action-status'));
  if (action.result_url) card.append(externalLink(email ? 'Open Gmail sent mail' : 'Open calendar event', action.result_url));
  const controls = make('div', undefined, 'action-controls');
  async function decide(path) {
    controls.querySelectorAll('button').forEach(button => button.disabled = true);
    try {
      const result = path ? await api('/v1/actions/' + action.id + '/' + path, {}) : await api('/v1/actions/' + action.id);
      card.replaceWith(actionCard(result));
    } catch (error) {
      card.querySelector('.action-status').textContent = error.message + ' Refresh status before trying again.';
      controls.querySelectorAll('button').forEach(button => button.disabled = false);
    }
  }
  if (!home && action.status === 'pending' && !expired) {
    const confirm = make('button', home ? 'Confirm & apply change' : email ? 'Confirm & send email' : 'Confirm & create event', 'primary');
    confirm.type = 'button'; confirm.onclick = () => decide('confirm');
    const cancel = make('button', 'Cancel preview', 'text-button'); cancel.type = 'button'; cancel.onclick = () => decide('cancel');
    controls.append(confirm, cancel);
  }
  if (action.status === 'executing' || action.status === 'pending') {
    const refresh = make('button', 'Refresh status', 'text-button'); refresh.type = 'button'; refresh.onclick = () => decide(''); controls.append(refresh);
  }
  card.append(controls); return card;
}

function outletControls(device, row) {
  const path = '/v1/home/outlets/' + encodeURIComponent(device.id);
  const statusPath = '/v1/home/devices/' + encodeURIComponent(device.id) + '/status';
  const output = make('p', 'Power has not been checked.', 'outlet-status');
  output.setAttribute('role', 'status');
  const result = make('div'); result.setAttribute('aria-live', 'polite');
  const controls = make('div', undefined, 'outlet-power');
  controls.setAttribute('role', 'group'); controls.setAttribute('aria-label', 'Outlet power');
  const on = make('button', 'Turn on'); const off = make('button', 'Turn off');
  const check = make('button', 'Check status', 'text-button');
  for (const button of [on, off, check]) button.type = 'button';
  for (const button of [on, off]) button.setAttribute('aria-pressed', 'false');
  controls.append(on, off);
  const allowed = device.control_enabled && session.scopes.includes('home:control');
  let busy = false, needsCheck = false, formFields;
  function updateControls() {
    on.disabled = off.disabled = busy || needsCheck || !allowed;
    check.disabled = busy;
    if (formFields) formFields.disabled = busy;
    row.setAttribute('aria-busy', String(busy));
  }
  function showState(state) {
    const known = state?.online !== false && typeof state?.on === 'boolean';
    on.setAttribute('aria-pressed', String(known && state.on));
    off.setAttribute('aria-pressed', String(known && !state.on));
    output.textContent = state?.online === false ? 'Offline' : known ? (state.on ? 'On' : 'Off') : 'Power unknown';
    if (state?.watts != null) output.textContent += ' / ' + state.watts + ' W';
    return known;
  }
  async function power(value) {
    if (busy || needsCheck || !allowed) return;
    busy = true; updateControls(); result.replaceChildren();
    output.textContent = value ? 'Turning on...' : 'Turning off...';
    try {
      const command = await api(path + '/power', {on: value, idempotency_key: crypto.randomUUID()});
      const known = showState(command.observed);
      needsCheck = command.status === 'unknown' || command.status === 'executing' || !known;
      result.append(homeCommandCard(command));
    } catch (error) {
      showState(null); needsCheck = true;
      result.textContent = error.message + ' Check status before sending another command.';
    } finally { busy = false; updateControls(); }
  }
  on.onclick = () => power(true); off.onclick = () => power(false);
  check.onclick = async () => {
    busy = true; updateControls(); output.textContent = 'Checking...';
    try { needsCheck = !showState(await api(statusPath)); }
    catch (error) { showState(null); output.textContent = error.message; needsCheck = true; }
    finally { busy = false; updateControls(); }
  };

  if (device.identifier_suffix) row.append(make('p', 'Outlet ID ending ' + device.identifier_suffix, 'muted'));
  if (device.setup_available && session.scopes.includes('identity:manage')) {
    const form = make('form', undefined, 'outlet-setup');
    form.setAttribute('aria-label', 'Outlet settings');
    formFields = make('fieldset');
    formFields.append(make('legend', 'Outlet settings'));
    const nameLabel = make('label', 'Device name');
    const name = make('input'); name.type = 'text'; name.required = true; name.maxLength = 100; name.value = device.name;
    nameLabel.append(name);
    const roomLabel = make('label', 'Outlet room');
    const room = make('input'); room.type = 'text'; room.maxLength = 100; room.value = device.room || ''; room.placeholder = 'e.g. Bedroom';
    roomLabel.append(room);
    const typeLabel = make('label', 'Powers'); const type = make('select');
    type.id = 'outlet-type-' + device.id; typeLabel.htmlFor = type.id;
    for (const [value, label] of [['unclassified', 'Not assigned'], ['lighting', 'Light / lamp'], ['air_purifier', 'Air purifier']]) {
      const option = make('option', label); option.value = value; type.append(option);
    }
    type.value = ['lighting', 'air_purifier'].includes(device.load_type) ? device.load_type : 'unclassified';
    const enabledLabel = make('label', undefined, 'checkbox-label');
    const enabled = make('input'); enabled.type = 'checkbox'; enabled.checked = device.control_enabled;
    enabled.disabled = type.value === 'unclassified';
    enabledLabel.append(enabled, make('span', 'Allow control here and in chat'));
    type.onchange = () => { enabled.disabled = type.value === 'unclassified'; enabled.checked = !enabled.disabled; };
    const hint = make('p', 'Choose what is plugged in. Saving settings does not switch power.', 'muted');
    const save = make('button', 'Save outlet', 'primary'); save.type = 'submit';
    const saved = make('p', '', 'muted'); saved.setAttribute('role', 'status');
    formFields.append(nameLabel, roomLabel, typeLabel, type, enabledLabel, hint, save, saved); form.append(formFields);
    form.onsubmit = async event => {
      event.preventDefault();
      if (busy) return;
      if (!name.value.trim()) { saved.textContent = 'Enter a device name.'; name.focus(); return; }
      busy = true; updateControls(); saved.textContent = 'Saving...';
      try {
        await api(path + '/setup', {name: name.value.trim(), room: room.value.trim(),
          load_type: type.value, control_enabled: type.value !== 'unclassified' && enabled.checked});
        await homeDevices('Outlet saved. You can use its name in chat.');
      } catch (error) { saved.textContent = error.message; }
      finally { busy = false; updateControls(); }
    };
    row.append(form);
  } else if (!device.control_enabled) {
    row.append(make('p', 'An owner must identify the connected load and enable control.', 'muted'));
  }
  if (!device.control_enabled) row.append(make('p', 'Power control is disabled until outlet settings are saved with control enabled.', 'muted'));
  else if (!allowed) row.append(make('p', 'Your account has read-only device access.', 'muted'));
  row.append(output, controls, check, result); updateControls();
}

async function homeDevices(notice = '') {
  const target = el('home-devices');
  if (!session.scopes.includes('home:read')) { target.textContent = 'Home device access is not enabled for this account.'; return; }
  const [devices, providers, commands] = await Promise.all([
    api('/v1/home/devices'), api('/v1/home/discovery'), api('/v1/home/commands')]);
  target.replaceChildren();
  if (notice) { const saved = make('p', notice, 'home-notice'); saved.setAttribute('role', 'status'); target.append(saved); }
  for (const provider of providers) {
    const label = {lifx: 'LIFX', tuya: 'Tuya / Smart Life', shelly: 'Shelly LAN'}[provider.provider] || provider.provider;
    const status = !provider.configured ? provider.provider === 'shelly' ? 'LAN discovery is disabled.' : 'Add account credentials on the server to discover devices.' :
      provider.status === 'ready' ? 'Synced ' + provider.count + ' device(s).' :
      provider.status === 'error' ? provider.error : 'Discovering devices...';
    target.append(make('p', label + ': ' + status, 'muted'));
  }
  const refresh = make('button', 'Refresh devices', 'text-button'); refresh.type = 'button';
  refresh.onclick = async () => {
    refresh.disabled = true; refresh.textContent = 'Discovering...';
    try { await api('/v1/home/discovery/refresh', {}); await homeDevices(); }
    catch (error) { refresh.textContent = error.message; refresh.disabled = false; }
  };
  target.append(refresh);
  if (commands.length) {
    const recent = make('details'); recent.append(make('summary', 'Recent device commands'));
    for (const command of commands.slice(0, 12)) recent.append(homeCommandCard(command));
    target.append(recent);
  }
  if (!devices.length) {
    target.append(make('p', 'Linked LIFX, Tuya, and Shelly devices appear automatically. Refresh devices to discover them.', 'muted'));
    return;
  }
  for (const device of devices) {
    const row = make('div', undefined, 'memory-card');
    row.dataset.deviceId = device.id;
    row.setAttribute('role', 'group'); row.setAttribute('aria-label', device.name);
    row.append(make('strong', device.name), make('p', [device.room || 'No room', device.provider,
      device.present === false ? 'Missing from account' : device.control_enabled ? 'Ready for chat control' : 'Read-only'].filter(Boolean).join(' / ')));
    if (device.groups?.length) row.append(make('p', 'Groups: ' + device.groups.join(', '), 'muted'));
    if (device.provider === 'shelly') outletControls(device, row);
    if (session.scopes.includes('home:organize')) {
      const form = make('form', undefined, 'home-organization');
      const roomLabel = make('label', 'Room');
      const room = make('input'); room.type = 'text'; room.value = device.room || ''; room.maxLength = 100;
      roomLabel.append(room);
      const groupsLabel = make('label', 'Groups (one per line)');
      const groups = make('textarea'); groups.rows = 2; groups.value = (device.groups || []).join('\n');
      groupsLabel.append(groups);
      const save = make('button', 'Save room & groups', 'text-button'); save.type = 'submit';
      const saved = make('p', '', 'muted'); saved.setAttribute('role', 'status');
      form.append(roomLabel, groupsLabel, save, saved);
      form.onsubmit = async event => {
        event.preventDefault(); save.disabled = true;
        try {
          await api('/v1/home/organize', {device_ids: [device.id], room: room.value.trim(),
            groups: groups.value.split('\n').map(group => group.trim()).filter(Boolean)});
          await homeDevices();
        } catch (error) { saved.textContent = error.message; save.disabled = false; }
      };
      const details = make('details'); details.append(make('summary', 'Edit room & groups'), form); row.append(details);
    }
    if (device.provider === 'shelly') { target.append(row); continue; }
    const output = make('p', 'Status has not been checked.', 'muted');
    const button = make('button', 'Check status', 'text-button'); button.type = 'button';
    button.onclick = async () => {
      button.disabled = true; output.textContent = 'Checking...';
      try {
        const state = await api('/v1/home/devices/' + encodeURIComponent(device.id) + '/status');
        const parts = [state.online === false ? 'Offline' : state.on === null ? 'Power unknown' : state.on ? 'On' : 'Off'];
        if (state.brightness !== null) parts.push(Math.round(state.brightness) + '% brightness');
        if (state.color) parts.push('Color: ' + state.color);
        if (state.watts !== null) parts.push(state.watts + ' W');
        if (state.energy_wh !== null) parts.push(state.energy_wh + ' Wh');
        output.textContent = parts.join(' / ') + (state.note ? '. ' + state.note : '');
      } catch (error) { output.textContent = error.message; }
      finally { button.disabled = false; }
    };
    row.append(output, button); target.append(row);
  }
}
