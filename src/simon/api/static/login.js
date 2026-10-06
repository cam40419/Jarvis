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
function showSession(value) {
  session = value;
  window.dispatchEvent(new Event('simon-session-change'));
  $('signed-in').hidden = !value;
  $('signed-out').hidden = !!value;
  if (!value) return;
  const member = value.memberships.find((m) => m.workspace_id === value.workspace_id);
  $('welcome').textContent = `Hello, ${member.display_name}.`;
  const methods = {
    password: 'a password',
    development: 'development access',
  };
  $('account').textContent = `Signed in with ${methods[value.method] || value.method}.`;
  $('permissions').textContent = `Permissions: ${value.scopes.join(', ')}`;
  $('workspaces').replaceChildren(
    ...value.memberships.map((m) => {
      const option = document.createElement('option');
      option.value = m.workspace_id;
      option.textContent = m.workspace_name;
      option.selected = m.workspace_id === value.workspace_id;
      return option;
    }),
  );
}
async function loadPasswordStatus() {
  if (!passwordEnabled) return;
  const result = await api('/auth/password');
  $('set-username').value = result.username || '';
  const email = await api('/auth/email');
  $('recovery-email').value = email.email || '';
  $('recovery-email-status').textContent = email.email
    ? 'Verified recovery email: ' + email.email
    : email.configured
      ? 'Verify your email to enable password recovery.'
      : 'Your administrator needs to set up email delivery before you can verify your address.';
}
async function action(callback) {
  document.querySelectorAll('button').forEach((button) => (button.disabled = true));
  status('');
  try {
    await callback();
  } catch (error) {
    status(error.message, true);
  } finally {
    document.querySelectorAll('button').forEach((button) => (button.disabled = false));
  }
}
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
let emailChallenge = null;
$('forgot-password-form').onsubmit = (event) => {
  event.preventDefault();
  action(async () => {
    const result = await api('/auth/password/forgot', { email: $('forgot-email').value.trim() });
    $('password-reset-form').hidden = false;
    status(result.message);
    $('reset-code').focus();
  });
};
$('password-reset-form').onsubmit = (event) => {
  event.preventDefault();
  action(async () => {
    if ($('reset-password').value !== $('reset-confirm').value)
      throw new Error('The new passwords do not match.');
    await api('/auth/password/reset-email', {
      email: $('forgot-email').value.trim(),
      code: $('reset-code').value.trim(),
      password: $('reset-password').value,
      confirm_password: $('reset-confirm').value,
    });
    $('password-reset-form').reset();
    $('password-reset-form').hidden = true;
    $('password-reset-panel').open = false;
    $('login-password').focus();
    status('Password reset. Sign in with your new password.');
  });
};
$('recovery-email-form').onsubmit = (event) => {
  event.preventDefault();
  action(async () => {
    const result = await api('/auth/email/start', {
      email: $('recovery-email').value.trim(),
      current_password: $('email-current-password').value,
    });
    emailChallenge = result.challenge_id;
    $('email-current-password').value = '';
    $('email-code-form').hidden = false;
    $('email-code').focus();
    status('Verification code sent. Check your email. The code expires in 10 minutes.');
  });
};
$('email-code-form').onsubmit = (event) => {
  event.preventDefault();
  action(async () => {
    await api('/auth/email/verify', { challenge_id: emailChallenge, code: $('email-code').value });
    emailChallenge = null;
    $('email-code-form').reset();
    $('email-code-form').hidden = true;
    await loadPasswordStatus();
    status('Recovery email verified.');
  });
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
$('workspaces').onchange = () =>
  action(async () => {
    try {
      showSession(await api('/auth/workspace', { workspace_id: $('workspaces').value }));
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
