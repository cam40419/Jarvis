'use strict';
let session = null;
let passwordEnabled = false;
const $ = (id) => document.getElementById(id);
function status(message, error = false) {
  $('status').textContent = message;
  $('status').className = error ? 'error' : '';
}
async function api(path, body) {
  const headers = { 'Content-Type': 'application/json' };
  if (session) headers['X-CSRF-Token'] = session.csrf_token;
  const response = await fetch(appPath(path), {
    method: body === undefined ? 'GET' : 'POST',
    headers,
    credentials: 'same-origin',
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (response.status === 204) return null;
  const value = await response.json();
  if (!response.ok) throw new Error(value.error?.message || 'Request failed. Please try again.');
  return value;
}
function decode(value) {
  return Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0));
}
function encode(buffer) {
  return btoa(String.fromCharCode(...new Uint8Array(buffer)))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=/g, '');
}
function credentialJSON(credential) {
  const response = { clientDataJSON: encode(credential.response.clientDataJSON) };
  for (const field of ['attestationObject', 'authenticatorData', 'signature', 'userHandle']) {
    if (credential.response[field]) response[field] = encode(credential.response[field]);
  }
  if (credential.response.getTransports) response.transports = credential.response.getTransports();
  return {
    id: credential.id,
    rawId: encode(credential.rawId),
    type: credential.type,
    response,
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}
function showSession(value) {
  session = value;
  window.dispatchEvent(new Event('simon-session-change'));
  $('signed-in').hidden = !value;
  $('signed-out').hidden = !!value;
  if (!value) return;
  const member = value.memberships.find((m) => m.household_id === value.household_id);
  $('welcome').textContent = `Hello, ${member.display_name}.`;
  const methods = {
    passkey: 'a passkey',
    password: 'a password',
    development: 'development access',
  };
  $('account').textContent = `Signed in with ${methods[value.method] || value.method}.`;
  $('permissions').textContent = `Permissions: ${value.scopes.join(', ')}`;
  $('households').replaceChildren(
    ...value.memberships.map((m) => {
      const option = document.createElement('option');
      option.value = m.household_id;
      option.textContent = m.household_name;
      option.selected = m.household_id === value.household_id;
      return option;
    }),
  );
}
async function loadPasswordStatus() {
  if (!passwordEnabled) return;
  const result = await api('/auth/password');
  $('set-username').value = result.username || '';
}
async function action(callback) {
  document.querySelectorAll('button').forEach((button) => (button.disabled = true));
  status('');
  try {
    await callback();
  } catch (error) {
    status(
      error.name === 'NotAllowedError'
        ? 'Passkey request cancelled or unavailable. Try again.'
        : error.message,
      true,
    );
  } finally {
    document.querySelectorAll('button').forEach((button) => (button.disabled = false));
  }
}
async function passkey(register) {
  if (!window.PublicKeyCredential || !navigator.credentials)
    throw new Error('Passkeys require a supported browser and a secure connection.');
  const base = `/auth/passkeys/${register ? 'register' : 'login'}`;
  const flow = await api(`${base}/options`, register ? { token: $('enrollment').value } : {});
  const options = flow.options;
  options.challenge = decode(options.challenge);
  if (register) options.user.id = decode(options.user.id);
  for (const field of ['allowCredentials', 'excludeCredentials']) {
    if (options[field])
      options[field] = options[field].map((item) => ({ ...item, id: decode(item.id) }));
  }
  const credential = register
    ? await navigator.credentials.create({ publicKey: options })
    : await navigator.credentials.get({ publicKey: options });
  if (!credential) throw new Error('No passkey was returned. Please try again.');
  showSession(
    await api(`${base}/verify`, {
      ceremony_id: flow.ceremony_id,
      credential: credentialJSON(credential),
    }),
  );
  await loadPasswordStatus();
  $('enrollment').value = '';
  status(register ? "Passkey created. You're signed in." : "You're signed in.");
}
$('sign-in').onclick = () => action(() => passkey(false));
$('password-login-form').onsubmit = (event) => {
  event.preventDefault();
  action(async () => {
    showSession(
      await api('/auth/password/login', {
        username: $('login-username').value,
        password: $('login-password').value,
      }),
    );
    $('login-password').value = '';
    await loadPasswordStatus();
    status("You're signed in.");
  });
};
$('password-set-form').onsubmit = (event) => {
  event.preventDefault();
  action(async () => {
    const result = await api('/auth/password', {
      username: $('set-username').value,
      password: $('set-password').value,
      current_password: $('current-password').value || null,
    });
    $('set-username').value = result.username;
    $('set-password').value = '';
    $('current-password').value = '';
    status('Username and password saved.');
  });
};
$('password-reset-form').onsubmit = (event) => {
  event.preventDefault();
  action(async () => {
    showSession(
      await api('/auth/password/reset', {
        token: $('reset-token').value,
        username: $('reset-username').value,
        password: $('reset-password').value,
      }),
    );
    $('reset-token').value = '';
    $('reset-password').value = '';
    await loadPasswordStatus();
    status("Password reset. You're signed in.");
  });
};
$('enroll-form').onsubmit = (event) => {
  event.preventDefault();
  action(() => passkey(true));
};
$('password-register-form').onsubmit = (event) => {
  event.preventDefault();
  action(async () => {
    showSession(
      await api('/auth/password/register', {
        token: $('register-token').value,
        username: $('register-username').value,
        password: $('register-password').value,
      }),
    );
    $('register-token').value = '';
    $('register-password').value = '';
    await loadPasswordStatus();
    status("Account created. You're signed in.");
  });
};
$('dev-form').onsubmit = (event) => {
  event.preventDefault();
  action(async () => {
    showSession(await api('/auth/dev-login', { token: $('dev-token').value }));
    await loadPasswordStatus();
    $('dev-token').value = '';
    status('Development session started.');
  });
};
$('logout').onclick = () =>
  action(async () => {
    await api('/auth/logout', {});
    showSession(null);
    status('Signed out.');
  });
$('households').onchange = () =>
  action(async () => {
    try {
      showSession(await api('/auth/household', { household_id: $('households').value }));
    } catch (error) {
      showSession(session);
      throw error;
    }
  });
$('echo').onclick = () =>
  action(async () => {
    const result = await api('/v1/capabilities/invoke', {
      capability: 'system.echo',
      arguments: { message: 'Simon is connected.' },
      idempotency_key: crypto.randomUUID(),
    });
    status(result.output.message);
  });
(async () => {
  try {
    const config = await api('/auth/config');
    passwordEnabled = !!config.password_enabled;
    $('dev-panel').hidden = !config.dev_login_enabled;
    $('password-login-form').hidden = !config.password_enabled;
    $('password-register-panel').hidden = !config.password_enabled;
    $('password-reset-panel').hidden = !config.password_enabled;
    $('password-set-panel').hidden = !config.password_enabled;
    $('password-separator').hidden = !config.password_enabled;
    if (!config.password_enabled)
      $('login-lede').textContent = 'Use your passkey to open your workspace.';
  } catch (error) {
    status(error.message, true);
  }
  try {
    showSession(await api('/auth/session'));
    await loadPasswordStatus();
  } catch {
    showSession(null);
  }
})();
