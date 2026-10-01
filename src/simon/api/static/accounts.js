'use strict';
(() => {
  const panel = $('account-management'),
    list = $('managed-accounts'),
    note = $('accounts-status');
  let pending = null,
    generation = 0;
  const node = (tag, text) => {
    const element = document.createElement(tag);
    element.textContent = text;
    return element;
  };
  function invitation(value) {
    $('invitation-result').hidden = !value.enrollment_token;
    $('invitation-token').value = value.enrollment_token || '';
    $('code-instructions').textContent =
      'Send this code and your Simon login URL to the person you invited. They can set up a passkey or a username and password.';
    note.textContent = value.enrollment_token
      ? 'Invitation created. Share this one-use code privately; it expires in 15 minutes.'
      : 'Account already created. Use Renew invitation if you need a new code.';
  }
  async function refresh() {
    const gen = ++generation;
    panel.hidden = !session?.can_manage_accounts;
    if (panel.hidden) {
      $('invitation-token').value = '';
      list.replaceChildren();
      return;
    }
    const accounts = await api('/v1/accounts');
    if (gen !== generation || !session?.can_manage_accounts) return;
    list.replaceChildren();
    for (const account of accounts) {
      const card = node('article', '');
      card.className = 'managed-account';
      card.append(
        node('strong', account.display_name),
        node('p', `${account.status.replaceAll('_', ' ')} · Private workspace`),
      );
      function button(label, callback) {
        const b = node('button', label);
        b.type = 'button';
        b.onclick = async () => {
          b.disabled = true;
          try {
            await callback();
            await refresh();
          } catch (error) {
            note.textContent = error.message;
            b.disabled = false;
          }
        };
        card.append(b);
      }
      if (['invited', 'invitation_expired'].includes(account.status))
        button('Renew invitation', async () =>
          invitation(await api('/v1/accounts/' + account.actor_id + '/invitation', {})),
        );
      if (passwordEnabled && account.status === 'active' && account.has_password)
        button('Issue password recovery code', async () => {
          const value = await api('/v1/accounts/' + account.actor_id + '/password-recovery', {});
          $('invitation-result').hidden = false;
          $('invitation-token').value = value.enrollment_token;
          $('code-instructions').textContent =
            'Share this code privately. The account holder should open Forgot your password? and enter this code, their existing username, and a new password.';
          note.textContent = 'Recovery code issued. It expires in 15 minutes and can be used once.';
        });
      button(account.disabled ? 'Enable account' : 'Disable account', async () => {
        await api('/v1/accounts/' + account.actor_id + '/access', {
          enabled: account.disabled,
          expected_version: account.version,
        });
        $('invitation-token').value = '';
        $('invitation-result').hidden = true;
        note.textContent = account.disabled
          ? 'Account enabled. Sign in again, or renew its invitation.'
          : 'Account disabled and sessions revoked.';
      });
      list.append(card);
    }
    if (!accounts.length) list.append(node('p', 'No invited accounts yet.'));
  }
  $('invite-form').onsubmit = async (event) => {
    event.preventDefault();
    const name = $('invite-name').value.trim();
    if (!name || !session?.can_manage_accounts) return;
    if (pending?.display_name !== name)
      pending = { display_name: name, idempotency_key: crypto.randomUUID() };
    $('invite-submit').disabled = true;
    try {
      invitation(await api('/v1/accounts/invite', pending));
      pending = null;
      $('invite-name').value = '';
      await refresh();
    } catch (error) {
      note.textContent = error.message;
    } finally {
      $('invite-submit').disabled = false;
    }
  };
  $('invitation-copy').onclick = async () => {
    try {
      await navigator.clipboard.writeText($('invitation-token').value);
      note.textContent = 'Invitation code copied.';
    } catch {
      $('invitation-token').focus();
      $('invitation-token').select();
      note.textContent = 'Select and copy the invitation code.';
    }
  };
  window.addEventListener('simon-session-change', () =>
    refresh().catch((error) => {
      note.textContent = error.message;
    }),
  );
  if (session)
    refresh().catch((error) => {
      note.textContent = error.message;
    });
})();
