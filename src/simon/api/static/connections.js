'use strict';
let googleConnection;
async function connections() {
  await homeConnection();
  googleConnection = await api('/v1/connections/google');
  el('web-connection').textContent = assistant.web_search ? 'Ready · Public web search is enabled' : 'Unavailable · Enable OpenAI and web search on the server';
  const accounts = googleConnection.accounts || [];
  el('google-connection').textContent = accounts.length ? accounts.length + ' Google account(s) connected' :
    (googleReturn === 'failed' ? 'Connection did not finish. Check setup and try again.' : 'Not connected');
  el('google-connect').textContent = accounts.length ? 'Add another Google account' : 'Connect Google';
  el('google-connect').disabled = !googleConnection.configured || !el('google-sharing').checked;
  el('google-disconnect').hidden = true;
  const list = el('google-accounts'); list.replaceChildren();
  for (const account of accounts) {
    const row = make('section', undefined, 'memory-card');
    row.append(make('strong', account.email + (account.is_default ? ' (default)' : '')));
    const permissions = [];
    if (account.calendar) permissions.push('Calendar');
    if (account.gmail_read) permissions.push('Read Gmail');
    if (account.email_send) permissions.push('Send email');
    if (account.drive_write) permissions.push('Read and edit Drive');
    else if (account.drive_read) permissions.push('Read Drive');
    row.append(make('p', permissions.join(' / ')));
    async function change(path) {
      row.querySelectorAll('button').forEach(button => button.disabled = true);
      try { await api('/v1/connections/google/' + path, {account: account.id}); await connections(); }
      catch (error) { row.append(make('p', error.message)); row.querySelectorAll('button').forEach(button => button.disabled = false); }
    }
    if (!account.is_default) {
      const use = make('button', 'Make default'); use.onclick = () => change('default'); row.append(use);
    }
    const reconnect = make('button', 'Reconnect', 'text-button');
    reconnect.disabled = !googleConnection.configured;
    reconnect.onclick = () => beginGoogle(account.id);
    const remove = make('button', 'Disconnect', 'text-button'); remove.onclick = () => change('disconnect');
    row.append(reconnect, remove);
    if (account.needs_reconnect) row.append(make('p', 'Reconnect this account to grant missing Google permissions.', 'muted'));
    list.append(row);
  }
  el('google-setup').textContent = googleConnection.configured ?
    'Add each account separately. For reconnecting, choose the same Google account. Chat uses the default unless you name another account. Project folders keep their linked account.' :
    'Server setup needed: follow docs/runbooks/google.md, then restart Simon. OAuth redirect: ' + googleConnection.redirect_uri;
}

el('connections-open').onclick = () => { openPanel('connections-panel'); connections().catch(report); };
el('google-sharing').onchange = () => { el('google-connect').disabled = !googleConnection?.configured || !el('google-sharing').checked; };
el('google-connect').onclick = () => beginGoogle('');
async function beginGoogle(account) {
  if (!el('google-sharing').checked) { el('google-connection').textContent = 'Check the Google sharing acknowledgment before connecting.'; el('google-sharing').focus(); return; }
  el('google-connect').disabled = true;
  try {
    const result = await api('/v1/connections/google/start', {shared_chat_acknowledged: true, account});
    const url = new URL(result.url);
    if (url.origin !== 'https://accounts.google.com') throw Error('Unexpected connection address.');
    location.assign(url.href);
  } catch (error) { el('google-connection').textContent = error.message; el('google-connect').disabled = false; }
}
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
  card.setAttribute('aria-label', home ? 'Device preview' : action.kind === 'email.send' ? 'Email preview' : action.immediate ? 'Calendar result' : 'Calendar preview');
  const email = action.email;
  card.append(make('h3', home ? 'Previous device request' : email ? 'Review email' : action.immediate ?
    (action.status === 'succeeded' ? 'Calendar event created' : 'Calendar event status') : 'Review calendar event'));
  if (!home) card.append(make('p', (email ? 'From: ' : 'Calendar account: ') + action.account_email, 'muted'));
  if (home) {
    card.append(make('strong', action.device_name), make('p', [action.device_room, action.device_provider].filter(Boolean).join(' / ')));
    if (home.on !== null) card.append(make('p', 'Power: ' + (home.on ? 'On' : 'Off')));
    if (home.brightness !== null) card.append(make('p', 'Brightness: ' + home.brightness + '%'));
  } else if (email) {
    card.append(make('p', 'To: ' + email.to), make('strong', email.subject), make('pre', email.body));
  } else {
    const draft = action.calendar;
    const formatDate = value => action.immediate ? new Intl.DateTimeFormat(undefined,
      {dateStyle: 'medium', timeStyle: 'short'}).format(new Date(value)) : value;
    card.append(make('strong', draft.title), make('p', 'Starts: ' + formatDate(draft.start)), make('p', 'Ends: ' + formatDate(draft.end)));
    if (action.immediate) card.append(make('p', Intl.DateTimeFormat().resolvedOptions().timeZone, 'muted'));
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
  if (!home && !action.immediate && action.status === 'pending' && !expired) {
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

async function homeConnection() {
  const target = el('home-connection');
  target.textContent = 'Checking RobbinsHome connection…';
  try {
    const info = await api('/v1/connections/home');
    target.textContent = info.configured ? 'RobbinsHome is available as an external tool in chat.' : 'RobbinsHome is not connected.';
    if (info.configured && info.url) {
      const link = make('a', 'Open RobbinsHome'); link.href = info.url;
      link.target = '_blank'; link.rel = 'noopener noreferrer'; target.append(make('br'), link);
    }
  } catch (error) { target.textContent = error.message; }
}
