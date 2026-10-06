/* exported showConnectedResults */
'use strict';
let googleConnection;
async function connections() {
  await homeConnection();
  await integrationAccounts();
  await connectionReadiness();
  googleConnection = await api('/v1/connections/google');
  el('web-connection').textContent = assistant.web_search
    ? 'Ready · Public web search is enabled'
    : 'Unavailable · Enable OpenAI and web search on the server';
  const accounts = googleConnection.accounts || [];
  el('google-connection').textContent = accounts.length
    ? accounts.length + ' Google account(s) connected'
    : googleReturn === 'failed'
      ? 'Connection did not finish. Check setup and try again.'
      : 'Not connected';
  el('google-connect').textContent = accounts.length
    ? 'Add another Google account'
    : 'Connect Google';
  el('google-connect').disabled = !googleConnection.configured || !el('google-sharing').checked;
  el('google-disconnect').hidden = true;
  const list = el('google-accounts');
  list.replaceChildren();
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
      row.querySelectorAll('button').forEach((button) => (button.disabled = true));
      try {
        await api('/v1/connections/google/' + path, { account: account.id });
        await connections();
      } catch (error) {
        row.append(make('p', error.message));
        row.querySelectorAll('button').forEach((button) => (button.disabled = false));
      }
    }
    if (!account.is_default) {
      const use = make('button', 'Make default');
      use.onclick = () => change('default');
      row.append(use);
    }
    const reconnect = make('button', 'Reconnect', 'text-button');
    reconnect.disabled = !googleConnection.configured;
    reconnect.onclick = () => beginGoogle(account.id);
    const remove = make('button', 'Disconnect', 'text-button');
    remove.onclick = () => change('disconnect');
    const test = make('button', 'Test');
    test.type = 'button';
    const result = make('p', '', 'muted');
    test.onclick = async () => {
      test.disabled = true;
      result.textContent = 'Testing Google account...';
      try {
        const checked = await api('/v1/connections/google/test', { account: account.id });
        result.textContent = checked.message;
      } catch (error) {
        result.textContent = error.message;
      } finally {
        test.disabled = false;
      }
    };
    row.append(test, reconnect, remove, result);
    if (account.needs_reconnect)
      row.append(make('p', 'Reconnect this account to grant missing Google permissions.', 'muted'));
    list.append(row);
  }
  el('google-setup').textContent = googleConnection.configured
    ? 'Add each account separately. For reconnecting, choose the same Google account. Chat uses the default unless you name another account. Project folders keep their linked account.'
    : 'Set up the Google application below, then connect your Google account. Redirect address: ' +
      googleConnection.redirect_uri;
}

el('connections-open').onclick = () => {
  openPanel('connections-panel');
  connections().catch(report);
};
el('google-sharing').onchange = () => {
  el('google-connect').disabled = !googleConnection?.configured || !el('google-sharing').checked;
};
el('google-connect').onclick = () => beginGoogle('');
async function beginGoogle(account) {
  if (!el('google-sharing').checked) {
    el('google-connection').textContent =
      'Check the Google sharing acknowledgment before connecting.';
    el('google-sharing').focus();
    return;
  }
  el('google-connect').disabled = true;
  try {
    const result = await api('/v1/connections/google/start', {
      shared_chat_acknowledged: true,
      account,
    });
    const url = new URL(result.url);
    if (url.origin !== 'https://accounts.google.com') throw Error('Unexpected connection address.');
    location.assign(url.href);
  } catch (error) {
    el('google-connection').textContent = error.message;
    el('google-connect').disabled = false;
  }
}
el('google-disconnect').onclick = async () => {
  el('google-disconnect').disabled = true;
  try {
    await api('/v1/connections/google/disconnect', {});
    await connections();
  } catch (error) {
    el('google-connection').textContent = error.message;
  } finally {
    el('google-disconnect').disabled = false;
  }
};
function externalLink(label, target) {
  const link = make('a', label);
  try {
    const url = new URL(target);
    if (!['https:', 'http:'].includes(url.protocol)) return make('span', label);
    link.href = url.href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
  } catch {
    return make('span', label);
  }
  return link;
}
function showConnectedResults(answer) {
  const article = [...el('messages').children].find(
    (node) => node.dataset.messageId === answer.output_message_id,
  );
  if (!article) return;
  if (answer.web_sources?.length) {
    const sources = make('details', undefined, 'answer-sources');
    sources.append(make('summary', 'Sources · ' + answer.web_sources.length));
    const list = make('ol');
    for (const source of answer.web_sources) {
      const item = make('li');
      item.append(externalLink(source.title || source.url, source.url));
      list.append(item);
    }
    sources.append(list);
    article.append(sources);
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
    succeeded: command.verified
      ? 'Reported device state matches the request.'
      : 'Command accepted; state not yet verified.',
    failed: 'Could not apply change. ' + (command.error || ''),
    unknown: 'Outcome unknown. Check device status before requesting another command.',
    executing: 'Command started; final result is not available. Check device status.',
  };
  card.append(make('p', labels[command.status], 'action-status'));
  return card;
}
function actionCard(action) {
  const card = make('section', undefined, 'action-card');
  card.dataset.actionId = action.id;
  const home = action.home;
  card.setAttribute(
    'aria-label',
    home
      ? 'Device preview'
      : action.kind === 'email.send'
        ? 'Email preview'
        : action.immediate
          ? 'Calendar result'
          : 'Calendar preview',
  );
  const email = action.email;
  card.append(
    make(
      'h3',
      home
        ? 'Previous device request'
        : email
          ? 'Review email'
          : action.immediate
            ? action.status === 'succeeded'
              ? 'Calendar event created'
              : 'Calendar event status'
            : 'Review calendar event',
    ),
  );
  if (!home)
    card.append(
      make('p', (email ? 'From: ' : 'Calendar account: ') + action.account_email, 'muted'),
    );
  if (home) {
    card.append(
      make('strong', action.device_name),
      make('p', [action.device_room, action.device_provider].filter(Boolean).join(' / ')),
    );
    if (home.on !== null) card.append(make('p', 'Power: ' + (home.on ? 'On' : 'Off')));
    if (home.brightness !== null) card.append(make('p', 'Brightness: ' + home.brightness + '%'));
  } else if (email) {
    card.append(
      make('p', 'To: ' + email.to),
      make('strong', email.subject),
      make('pre', email.body),
    );
  } else {
    const draft = action.calendar;
    const formatDate = (value) =>
      action.immediate
        ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(
            new Date(value),
          )
        : value;
    card.append(
      make('strong', draft.title),
      make('p', 'Starts: ' + formatDate(draft.start)),
      make('p', 'Ends: ' + formatDate(draft.end)),
    );
    if (action.immediate)
      card.append(make('p', Intl.DateTimeFormat().resolvedOptions().timeZone, 'muted'));
    if (draft.location) card.append(make('p', 'Location: ' + draft.location));
    if (draft.description) card.append(make('pre', draft.description));
    card.append(make('p', 'Primary calendar · No invitations', 'muted fine'));
  }
  const expired = new Date(action.expires_at) <= new Date();
  const labels = {
    pending: expired
      ? 'Preview expired. Ask Simon for a new one.'
      : 'Nothing sent or created. Review all details before confirming.',
    executing:
      'Action started. Refresh its status shortly. If it stays here, check Google before requesting another.',
    succeeded: email
      ? 'Email sent. A reservation request still needs a reply from the venue.'
      : 'Event created in Google Calendar.',
    failed:
      'Action failed. ' + (action.error || 'Check the connection before requesting a new preview.'),
    unknown:
      'Outcome unknown. Check Google before requesting another attempt; Simon will not send this again.',
    cancelled: 'Preview cancelled. Nothing was sent or created.',
  };
  if (home)
    Object.assign(labels, {
      pending: 'No device change made. Ask Simon again to execute this request directly.',
      executing:
        'Command started. Refresh status; check the device before requesting another command.',
      succeeded: action.home_verified
        ? 'Reported device state matches the request.'
        : 'Command accepted. The requested device state is not yet verified; refresh device status.',
      unknown: 'Outcome unknown. Check the device before requesting another command.',
      cancelled: 'Preview cancelled. No device change made.',
    });
  card.append(make('p', labels[action.status], 'action-status'));
  if (action.result_url)
    card.append(
      externalLink(email ? 'Open Gmail sent mail' : 'Open calendar event', action.result_url),
    );
  const controls = make('div', undefined, 'action-controls');
  async function decide(path) {
    controls.querySelectorAll('button').forEach((button) => (button.disabled = true));
    try {
      const result = path
        ? await api('/v1/actions/' + action.id + '/' + path, {})
        : await api('/v1/actions/' + action.id);
      card.replaceWith(actionCard(result));
    } catch (error) {
      card.querySelector('.action-status').textContent =
        error.message + ' Refresh status before trying again.';
      controls.querySelectorAll('button').forEach((button) => (button.disabled = false));
    }
  }
  if (!home && !action.immediate && action.status === 'pending' && !expired) {
    const confirm = make(
      'button',
      home ? 'Confirm & apply change' : email ? 'Confirm & send email' : 'Confirm & create event',
      'primary',
    );
    confirm.type = 'button';
    confirm.onclick = () => decide('confirm');
    const cancel = make('button', 'Cancel preview', 'text-button');
    cancel.type = 'button';
    cancel.onclick = () => decide('cancel');
    controls.append(confirm, cancel);
  }
  if (action.status === 'executing' || action.status === 'pending') {
    const refresh = make('button', 'Refresh status', 'text-button');
    refresh.type = 'button';
    refresh.onclick = () => decide('');
    controls.append(refresh);
  }
  card.append(controls);
  return card;
}

async function homeConnection() {
  const target = el('home-connection');
  target.textContent = 'Checking RobbinsHome connection…';
  try {
    const info = await api('/v1/connections/home');
    target.textContent = info.configured
      ? 'RobbinsHome is available as an external tool in chat.'
      : 'RobbinsHome is not connected.';
    if (info.configured && info.url) {
      const link = make('a', 'Open RobbinsHome');
      link.href = info.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      target.append(make('br'), link);
    }
  } catch (error) {
    target.textContent = error.message;
  }
}

let reconnectIntegration = null;
let connectionAccounts = [];
function integrationFields() {
  const provider = el('integration-provider').value;
  for (const kind of ['twilio', 'gateway', 'home', 'google_app', 'email', 'github']) {
    el('integration-' + kind).hidden = provider !== kind;
    el('integration-' + kind).disabled = provider !== kind;
  }
  const storage = ['dropbox', 'box', 'onedrive', 'webdav'].includes(provider);
  el('integration-storage').hidden = !storage;
  el('integration-storage').disabled = !storage;
  for (const kind of ['dropbox', 'box', 'onedrive', 'webdav'])
    el('integration-' + kind + '-fields').hidden = provider !== kind;
  el('integration-storage-write-label').hidden = !['dropbox', 'webdav'].includes(provider);
  const smtp = provider === 'email' && el('integration-email-transport').value === 'smtp';
  el('integration-smtp').hidden = !smtp;
  el('integration-smtp').disabled = !smtp;
  el('integration-credential-label').textContent =
    provider === 'email'
      ? smtp
        ? 'SMTP password'
        : 'Resend API key'
      : provider === 'clickup'
        ? 'Personal API token'
        : provider === 'twilio'
          ? 'Auth token'
          : provider === 'google_app'
            ? 'Client secret'
            : 'API credential';
  el('integration-help').textContent =
    provider === 'openai'
      ? 'Save the site API key here. Changes reach agents, model generation and web search without editing config files.'
      : ['dropbox', 'box', 'onedrive', 'webdav'].includes(provider)
        ? 'Enter an access token from your storage provider and select the folder scope below. Test the connection here before starting work.'
        : provider === 'github'
          ? 'Link your GitHub token and choose permitted repositories here. Reading and creation permissions follow your connection settings.'
          : provider === 'email'
            ? 'Site administrator setup for verification and password-reset email. Choose Resend or your existing SMTP provider.'
            : provider === 'clickup'
              ? 'In ClickUp, open Settings / Apps and copy your personal API token. No List IDs needed.'
              : provider === 'twilio'
                ? 'Use your Twilio account credentials and an originating phone number. Phone messages use the existing review flow.'
                : provider === 'home'
                  ? 'Connect your RobbinsHome server using its API credential.'
                  : provider === 'google_app'
                    ? 'Site administrator setup. Save the OAuth application here, then authorize Google accounts above.'
                    : 'Connect your booking or purchase service. Commitments still use the existing review flow.';
}
el('integration-provider').onchange = integrationFields;
el('integration-email-transport').onchange = integrationFields;
el('integration-cancel').onclick = () => {
  reconnectIntegration = null;
  el('integration-form').reset();
  el('integration-provider').disabled = false;
  el('integration-cancel').hidden = true;
  el('integration-connect').textContent = 'Connect account';
  integrationFields();
};
async function integrationAccounts() {
  const [accounts, setup] = await Promise.all([
    api('/v1/connections/integrations'),
    api('/v1/connections/integrations/setup'),
  ]);
  connectionAccounts = accounts;
  el('integration-email-option').hidden = !setup.can_configure_email;
  el('integration-email-option').disabled = !setup.can_configure_email;
  el('integration-openai-option').hidden = !setup.can_configure_google_app;
  el('integration-openai-option').disabled = !setup.can_configure_google_app;
  el('integration-google-option').hidden = !setup.can_configure_google_app;
  el('integration-google-option').disabled = !setup.can_configure_google_app;
  el('integration-google-redirect').textContent = setup.google_redirect_uri;
  const list = el('integration-accounts');
  list.replaceChildren();
  for (const account of accounts) {
    const row = make('section', undefined, 'memory-card');
    row.append(make('strong', account.name));
    if (account.provider === 'clickup')
      row.append(
        make('p', 'All accessible Workspaces and Lists. New Lists are discovered automatically.'),
      );
    const reconnect = make('button', 'Reconnect');
    reconnect.type = 'button';
    reconnect.onclick = () => {
      reconnectIntegration = account;
      el('integration-provider').value = account.provider;
      el('integration-provider').disabled = true;
      el('integration-name').value = account.name;
      el('integration-credential').value = '';
      el('integration-sid').value = account.settings.account_sid || '';
      el('integration-phone').value = account.settings.from_number || '';
      el('integration-endpoint').value = account.settings.endpoint || '';
      el('integration-home-endpoint').value = account.settings.endpoint || '';
      el('integration-google-client').value = account.settings.client_id || '';
      el('integration-github-repositories').value = (account.settings.repositories || []).join(
        '\n',
      );
      el('integration-github-write').checked = account.settings.write_enabled === true;
      el('integration-webdav-endpoint').value = account.settings.endpoint || '';
      el('integration-webdav-user').value = account.settings.username || '';
      el('integration-root-path').value = account.settings.root_path || '';
      el('integration-root-folder').value = account.settings.root_folder_id || '';
      el('integration-drive').value = account.settings.drive_id || '';
      el('integration-root-item').value = account.settings.root_item_id || '';
      el('integration-download-hosts').value = (account.settings.download_hosts || []).join('\n');
      el('integration-storage-write').checked = account.settings.write_enabled === true;
      el('integration-email-transport').value = account.settings.transport || 'resend';
      el('integration-email-from').value = account.settings.from_email || '';
      el('integration-smtp-host').value = account.settings.smtp_host || '';
      el('integration-smtp-port').value = String(account.settings.smtp_port || 587);
      el('integration-smtp-user').value = account.settings.smtp_username || '';
      el('integration-recipients').value = (account.settings.call_recipients || []).join('\n');
      el('integration-merchants').value = Object.entries(account.settings.merchant_names || {})
        .map(([id, name]) => id + ' = ' + name)
        .join('\n');
      el('integration-cancel').hidden = false;
      el('integration-connect').textContent = 'Save reconnect';
      integrationFields();
      el('integration-credential').focus();
    };
    const remove = make('button', 'Disconnect', 'text-button');
    remove.type = 'button';
    remove.onclick = async () => {
      remove.disabled = true;
      try {
        await api(
          '/v1/connections/integrations/' + encodeURIComponent(account.id),
          undefined,
          'DELETE',
        );
        if (reconnectIntegration?.id === account.id) el('integration-cancel').click();
        await connections();
        window.dispatchEvent(new Event('simon-connections-change'));
      } catch (error) {
        el('integration-status').textContent = error.message;
        remove.disabled = false;
      }
    };
    const test = make('button', 'Test');
    test.type = 'button';
    const result = make(
      'p',
      account.settings.connection_test
        ? account.settings.connection_test.message +
            ' - ' +
            new Date(account.settings.connection_test.checked_at).toLocaleString()
        : 'Not tested yet',
      'muted',
    );
    test.onclick = async () => {
      test.disabled = true;
      result.textContent = 'Testing connection...';
      try {
        const checked = await api(
          '/v1/connections/integrations/' + encodeURIComponent(account.id) + '/test',
          {},
        );
        result.textContent = checked.message;
        await connections();
        window.dispatchEvent(new Event('simon-connections-change'));
      } catch (error) {
        result.textContent = error.message;
      } finally {
        test.disabled = false;
      }
    };
    row.append(test, reconnect, remove, result);
    list.append(row);
  }
}
el('integration-form').onsubmit = async (event) => {
  event.preventDefault();
  const provider = el('integration-provider').value;
  const body = {
    name: el('integration-name').value.trim(),
    credential: el('integration-credential').value,
  };
  if (['dropbox', 'box', 'onedrive', 'webdav'].includes(provider)) {
    if (provider === 'webdav') {
      body.endpoint = el('integration-webdav-endpoint').value.trim();
      body.username = el('integration-webdav-user').value.trim();
    }
    body.root_path = el('integration-root-path').value.trim() || null;
    body.root_folder_id = el('integration-root-folder').value.trim() || null;
    body.drive_id = el('integration-drive').value.trim() || null;
    body.root_item_id = el('integration-root-item').value.trim() || null;
    body.download_hosts = el('integration-download-hosts')
      .value.split(/\r?\n/)
      .map((v) => v.trim())
      .filter(Boolean);
    body.storage_write_enabled = el('integration-storage-write').checked;
  } else if (provider === 'github') {
    body.repositories = el('integration-github-repositories')
      .value.split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);
    body.github_write_enabled = el('integration-github-write').checked;
  } else if (provider === 'email') {
    body.email_transport = el('integration-email-transport').value;
    body.from_email = el('integration-email-from').value.trim();
    if (body.email_transport === 'smtp') {
      body.smtp_host = el('integration-smtp-host').value.trim();
      body.smtp_port = Number(el('integration-smtp-port').value);
      body.smtp_username = el('integration-smtp-user').value.trim();
    }
  } else if (provider === 'twilio') {
    body.account_sid = el('integration-sid').value.trim();
    body.from_number = el('integration-phone').value.trim();
    body.call_recipients = el('integration-recipients')
      .value.split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);
  } else if (provider === 'home') {
    body.endpoint = el('integration-home-endpoint').value.trim();
  } else if (provider === 'google_app') {
    body.client_id = el('integration-google-client').value.trim();
  } else if (provider === 'gateway') {
    body.endpoint = el('integration-endpoint').value.trim();
    body.merchant_names = {};
    for (const line of el('integration-merchants')
      .value.split(/\r?\n/)
      .filter((value) => value.trim())) {
      const split = line.indexOf('=');
      if (split < 1 || !line.slice(split + 1).trim()) {
        el('integration-status').textContent = 'Enter each merchant as identifier = display name.';
        return;
      }
      body.merchant_names[line.slice(0, split).trim()] = line.slice(split + 1).trim();
    }
  }
  el('integration-connect').disabled = true;
  el('integration-status').textContent = 'Connecting...';
  try {
    const suffix = reconnectIntegration ? '/' + encodeURIComponent(reconnectIntegration.id) : '';
    await api('/v1/connections/integrations/' + provider + suffix, body);
    el('integration-cancel').click();
    await connections();
    el('integration-status').textContent = 'Connected. Your account is ready to use.';
    window.dispatchEvent(new Event('simon-connections-change'));
  } catch (error) {
    el('integration-status').textContent = error.message;
  } finally {
    el('integration-credential').value = '';
    el('integration-connect').disabled = false;
  }
};

let readinessCatalog = null;
async function connectionReadiness() {
  readinessCatalog = await api('/v1/agent-platform/catalog');
  renderConnectionReadiness();
}
function renderConnectionReadiness() {
  const target = el('connections-catalog');
  target.replaceChildren();
  const query = el('connections-search').value.toLowerCase();
  const groups = new Map();
  for (const option of el('integration-provider').options) {
    if (!query || option.textContent.toLowerCase().includes(query)) groups.set(option.value, []);
  }
  for (const tool of readinessCatalog?.tool_statuses || []) {
    if (
      query &&
      !(tool.id + ' ' + tool.description + ' ' + tool.transport).toLowerCase().includes(query)
    )
      continue;
    if (!groups.has(tool.transport)) groups.set(tool.transport, []);
    groups.get(tool.transport).push(tool);
  }
  for (const [provider, tools] of [...groups].sort((a, b) => a[0].localeCompare(b[0]))) {
    const card = make('details');
    const ready = tools.filter((tool) => tool.state === 'configured').length;
    const account = connectionAccounts.find((item) => item.provider === provider);
    const label =
      [...el('integration-provider').options]
        .find((option) => option.value === provider)
        ?.textContent.trim() || provider.replaceAll('_', ' ');
    card.append(
      make(
        'summary',
        label +
          ' - ' +
          (tools.length
            ? ready + '/' + tools.length + ' tools ready'
            : account
              ? 'Connected'
              : 'Needs setup'),
      ),
    );
    if (account?.settings.connection_test)
      card.append(make('p', account.settings.connection_test.message));
    if (!tools.length && !account)
      card.append(make('p', 'Connect this service below before requesting work that needs it.'));
    if (provider === 'google_app' || provider === 'email' || provider === 'openai')
      card.append(make('p', 'Shared application settings are managed by the site administrator.'));
    const selectable = [...el('integration-provider').options].some(
      (option) => option.value === provider && !option.disabled,
    );
    if (selectable) {
      const configure = make('button', 'Configure ' + provider);
      configure.type = 'button';
      configure.onclick = () => {
        el('integration-cancel').click();
        el('integration-provider').value = provider;
        integrationFields();
        el('integration-form').scrollIntoView({ block: 'start' });
        el('integration-credential').focus();
      };
      card.append(configure);
    }
    for (const tool of tools) {
      const name =
        readinessCatalog.individual_skills?.find((skill) => skill.tool_ids?.includes(tool.id))
          ?.name || tool.id;
      card.append(
        make('strong', name),
        make(
          'p',
          tool.state === 'configured'
            ? 'Ready'
            : (tool.blocked_reasons || []).join(' ') || tool.state,
        ),
      );
    }
    target.append(card);
  }
  for (const model of readinessCatalog?.models || []) {
    const card = make('details');
    card.append(
      make('summary', 'Model: ' + model.model),
      make('p', model.state === 'configured' ? 'Ready' : model.blocked_reasons.join(' ')),
    );
    target.append(card);
  }
  for (const environment of readinessCatalog?.environments || []) {
    const card = make('details');
    card.append(
      make('summary', 'Runtime: ' + environment.id),
      make(
        'p',
        environment.enabled ? 'Enabled - ' + environment.capabilities.join(', ') : 'Disabled',
      ),
    );
    const test = make('button', 'Test runtime');
    test.type = 'button';
    const result = make('p', '');
    test.onclick = async () => {
      test.disabled = true;
      result.textContent = 'Checking runtime...';
      try {
        const checked = await api(
          '/v1/agent-platform/environments/' + encodeURIComponent(environment.id) + '/test',
          {},
        );
        result.textContent = checked.message;
      } catch (error) {
        result.textContent = error.message;
      } finally {
        test.disabled = false;
      }
    };
    card.append(test, result);
    target.append(card);
  }
}
el('connections-refresh').onclick = () => connectionReadiness().catch(report);
el('connections-search').oninput = renderConnectionReadiness;
window.addEventListener('simon-connections-change', async () => {
  try {
    await connectionReadiness();
    window.dispatchEvent(
      new CustomEvent('simon-agent-library-updated', { detail: { catalog: readinessCatalog } }),
    );
  } catch (error) {
    report(error);
  }
});
