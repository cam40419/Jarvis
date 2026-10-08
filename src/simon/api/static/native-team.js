'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = () => window.SimonNativeProjects;
  const dialog = $('nt-dialog');
  let projectId, snapshot, editing, pending, latest;
  let busy = false,
    uncertain = false,
    ticket = 0;
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const feedback = (message = '', tone = '') => {
    $('nt-feedback').textContent = message;
    $('nt-feedback').dataset.tone = tone;
  };
  const endpoint = (suffix = '') => '/v2/projects/' + projectId + suffix;
  const request = (url, body, method) => bridge().request(url, body, method);
  const currentForm = () => $(editing?.kind === 'policy' ? 'nt-policy-form' : 'nt-agent-form');

  async function loadTeam(id = projectId) {
    let result,
      offset = 0;
    const seen = new Set();
    while (offset <= 1000000) {
      const page = await request('/v2/projects/' + id + '/team?limit=100&offset=' + offset);
      if (!result) result = { ...page, agents: [] };
      if (
        page.policy.version !== result.policy.version ||
        page.can_manage !== result.can_manage ||
        page.can_set_policy !== result.can_set_policy ||
        page.can_manage_credentials !== result.can_manage_credentials ||
        page.can_issue_credentials !== result.can_issue_credentials
      )
        throw Error('The team changed while loading. Reopen Agents to get the latest version.');
      for (const agent of page.agents) {
        if (seen.has(agent.id))
          throw Error('The team changed while loading. Reopen Agents to get the latest version.');
        seen.add(agent.id);
        result.agents.push(agent);
      }
      if (page.agents_next_offset == null) return result;
      if (page.agents_next_offset <= offset)
        throw Error('The team could not be loaded. Please try again.');
      offset = page.agents_next_offset;
    }
    throw Error('This team is too large to load. Please narrow its scope.');
  }

  function lock(value) {
    busy = value;
    dialog.setAttribute('aria-busy', String(value));
    for (const fieldset of dialog.querySelectorAll('fieldset'))
      fieldset.disabled = value || uncertain;
    for (const button of dialog.querySelectorAll('button')) button.disabled = value || uncertain;
    $('nt-role-key').disabled = Boolean(editing?.record) || value || uncertain;
    $('nt-save').disabled = value || Boolean(latest);
    $('nt-save').textContent = uncertain
      ? 'Retry save'
      : editing?.kind === 'policy'
        ? 'Save limits'
        : 'Save role';
    $('nt-new-agent').disabled =
      value ||
      uncertain ||
      !snapshot?.can_manage ||
      snapshot.agents.filter((agent) => agent.status === 'active').length >=
        snapshot.policy.max_active_agents;
  }

  function overview(message = '') {
    editing = null;
    latest = null;
    $('nt-overview').hidden = false;
    $('nt-agent-form').hidden = true;
    $('nt-policy-form').hidden = true;
    $('nt-agent-form').dataset.editing = 'false';
    $('nt-policy-form').dataset.editing = 'false';
    $('nt-save').hidden = true;
    $('nt-cancel').hidden = true;
    $('nt-close').hidden = false;
    $('nt-conflict').hidden = true;
    $('nt-new-agent').hidden = !snapshot.can_manage;
    $('nt-policy-open').hidden = !snapshot.can_set_policy;
    const active = snapshot.agents.filter((agent) => agent.status === 'active').length;
    $('nt-summary').textContent =
      `${active} of ${snapshot.policy.max_active_agents} active roles. Agent team management ${snapshot.policy.agents_can_manage_team ? 'allowed' : 'disabled'}.`;
    $('nt-agents').replaceChildren(
      ...snapshot.agents.map((agent) => {
        const row = node('li', undefined, 'nt-agent');
        row.dataset.agentId = agent.id;
        const header = node('div', undefined, 'nt-agent-header');
        header.append(
          node('h3', agent.name),
          node('span', agent.status[0].toUpperCase() + agent.status.slice(1), 'np-badge'),
        );
        if (snapshot.can_manage) {
          const edit = node('button', 'Edit role', 'np-button np-quiet');
          edit.type = 'button';
          edit.setAttribute('aria-label', 'Edit ' + agent.name);
          edit.onclick = () => editRole(agent);
          header.append(edit);
        }
        if (snapshot.can_manage_credentials) {
          const credentials = node('button', 'Worker access', 'np-button np-quiet');
          credentials.type = 'button';
          credentials.setAttribute('aria-label', 'Worker access for ' + agent.name);
          credentials.onclick = () => openCredentials(agent);
          header.append(credentials);
        }
        const details = node('dl');
        for (const [label, value] of [
          ['Responsibilities', agent.instructions],
          ['Success criteria', agent.success_criteria],
        ])
          details.append(node('dt', label), node('dd', value));
        const rationale = node('details');
        rationale.append(node('summary', 'Role purpose and permissions'));
        const role = node('dl');
        for (const [label, value] of [
          ['Why this role is needed', agent.rationale],
          ['Role identifier', agent.role_key],
          [
            'Team management',
            agent.can_manage_team
              ? 'Permitted when enabled for this project and worker credential.'
              : 'Not permitted for this role.',
          ],
        ])
          role.append(node('dt', label), node('dd', value));
        rationale.append(role);
        row.append(header, details, rationale);
        return row;
      }),
    );
    if (!snapshot.agents.length)
      $('nt-agents').append(
        node(
          'li',
          'No agent roles yet. Define a role around a clear responsibility and a reviewable result.',
          'np-empty',
        ),
      );
    feedback(message, message ? 'success' : '');
    lock(false);
  }

  function showEditor(kind, record) {
    if (busy || uncertain) return;
    editing = { kind, record };
    latest = null;
    pending = null;
    $('nt-overview').hidden = true;
    $('nt-agent-form').hidden = kind !== 'agent';
    $('nt-policy-form').hidden = kind !== 'policy';
    $('nt-close').hidden = true;
    $('nt-save').hidden = false;
    $('nt-cancel').hidden = false;
    $('nt-conflict').hidden = true;
    $('nt-save').setAttribute('form', currentForm().id);
    currentForm().dataset.editing = 'false';
    feedback();
    lock(false);
  }

  function editRole(agent = null) {
    if (!snapshot.can_manage) return;
    $('nt-agent-form').reset();
    showEditor('agent', agent);
    $('nt-editor-heading').textContent = agent ? 'Edit agent role' : 'Add agent role';
    $('nt-name').value = agent?.name || '';
    $('nt-role-key').value = agent?.role_key || '';
    $('nt-instructions').value = agent?.instructions || '';
    $('nt-success').value = agent?.success_criteria || '';
    $('nt-rationale').value = agent?.rationale || '';
    $('nt-can-manage').checked = agent?.can_manage_team || false;
    $('nt-state').value = agent?.status || 'active';
    $('nt-state-field').hidden = !agent;
    $('nt-name').focus();
  }

  $('nt-new-agent').onclick = () => editRole();
  $('nt-policy-open').onclick = () => {
    if (!snapshot.can_set_policy) return;
    showEditor('policy', snapshot.policy);
    $('nt-max-agents').value = snapshot.policy.max_active_agents;
    $('nt-agents-manage').checked = snapshot.policy.agents_can_manage_team;
    $('nt-max-agents').focus();
  };
  for (const form of dialog.querySelectorAll('form'))
    form.addEventListener('input', () => {
      form.dataset.editing = 'true';
    });

  function close() {
    if (busy || uncertain) return;
    pending = null;
    ticket++;
    dialog.close();
  }
  $('nt-close').onclick = close;
  $('nt-cancel').onclick = () => {
    if (busy || uncertain) return;
    pending = null;
    overview();
    $('nt-heading').focus();
  };
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    close();
  });
  window.addEventListener('beforeunload', (event) => {
    if (busy || uncertain) event.preventDefault();
  });

  $('np-agents').onclick = async () => {
    const project = bridge().getProject();
    if (!project) return;
    const attempt = ++ticket;
    $('np-agents').disabled = true;
    try {
      const found = await loadTeam(project.id);
      if (attempt !== ticket || bridge().getProject() !== project) return;
      projectId = project.id;
      snapshot = found;
      uncertain = false;
      pending = null;
      overview();
      dialog.showModal();
      $('nt-heading').focus();
    } catch (error) {
      if (attempt === ticket && bridge().getProject() === project) {
        $('np-status').textContent = error.message;
        $('np-status').dataset.tone = 'error';
      }
    } finally {
      if (attempt === ticket) $('np-agents').disabled = false;
    }
  };

  function payload() {
    if (editing.kind === 'policy')
      return {
        max_active_agents: Number($('nt-max-agents').value),
        agents_can_manage_team: $('nt-agents-manage').checked,
        expected_version: editing.record.version,
      };
    return {
      name: $('nt-name').value.trim(),
      instructions: $('nt-instructions').value.trim(),
      success_criteria: $('nt-success').value.trim(),
      rationale: $('nt-rationale').value.trim(),
      can_manage_team: $('nt-can-manage').checked,
      ...(editing.record
        ? { status: $('nt-state').value, expected_version: editing.record.version }
        : { role_key: $('nt-role-key').value.trim() }),
    };
  }

  async function findConflict() {
    const found = await loadTeam();
    snapshot = found;
    const saved =
      editing.kind === 'policy'
        ? found.policy
        : found.agents.find((agent) => agent.id === editing.record?.id);
    if (!saved || saved.version === editing.record?.version) return;
    latest = saved;
    $('nt-latest').textContent =
      editing.kind === 'policy'
        ? `Maximum active agents: ${saved.max_active_agents}\nAgent team management: ${saved.agents_can_manage_team ? 'Allowed' : 'Disabled'}\nVersion ${saved.version}`
        : `${saved.name}\nResponsibilities: ${saved.instructions}\nSuccess criteria: ${saved.success_criteria}\nPurpose: ${saved.rationale}\nStatus: ${saved.status}\nTeam management: ${saved.can_manage_team ? 'Allowed' : 'Disabled'}\nVersion ${saved.version}`;
    $('nt-conflict').hidden = false;
  }
  $('nt-rebase').onclick = () => {
    if (busy || uncertain || !latest) return;
    editing.record = latest;
    latest = null;
    $('nt-conflict').hidden = true;
    feedback(
      'Your draft is preserved. Review it against the latest saved version, then save to apply it.',
      'warning',
    );
    lock(false);
  };

  async function save(event) {
    event.preventDefault();
    if (!editing || busy || latest) return;
    if (!pending)
      pending = {
        url: endpoint(
          editing.kind === 'policy'
            ? '/team/policy'
            : '/agents' + (editing.record ? '/' + editing.record.id : ''),
        ),
        method: editing.kind === 'policy' || editing.record ? 'PUT' : 'POST',
        body: { ...payload(), idempotency_key: crypto.randomUUID() },
      };
    lock(true);
    feedback('Saving…');
    let saved = false;
    try {
      await request(pending.url, pending.body, pending.method);
      pending = null;
      uncertain = false;
      saved = true;
      currentForm().dataset.editing = 'false';
      snapshot = await loadTeam();
      await bridge().onTeamChanged();
      overview('Saved. The project agent team is up to date.');
      $('nt-heading').focus();
    } catch (error) {
      if (saved) {
        // A successful write must never be retried as a new create after refresh fails.
        editing = null;
        $('nt-agent-form').hidden = true;
        $('nt-policy-form').hidden = true;
        $('nt-save').hidden = true;
        $('nt-cancel').hidden = true;
        $('nt-close').hidden = false;
        $('nt-conflict').hidden = true;
        feedback(
          'Saved, but the updated team could not be loaded. Close Agents and refresh the project. ' +
            error.message,
          'warning',
        );
      } else if (!error.status || error.status >= 500) {
        uncertain = true;
        feedback(
          'The save could not be confirmed. Retry this same save before editing or closing; the saved key prevents duplicates.',
          'error',
        );
      } else {
        pending = null;
        uncertain = false;
        feedback(error.message, 'error');
        if (error.status === 409 && editing.record) {
          try {
            await findConflict();
          } catch (refreshError) {
            feedback(refreshError.message + ' Your draft is preserved.', 'error');
          }
        }
      }
    } finally {
      lock(false);
    }
  }
  $('nt-agent-form').onsubmit = save;
  $('nt-policy-form').onsubmit = save;

  const credentialsDialog = $('nt-credentials-dialog');
  let workerAgent, workerPending;
  let workerBusy = false,
    workerUncertain = false;
  const workerFeedback = (message = '', tone = '') => {
    $('nt-credentials-feedback').textContent = message;
    $('nt-credentials-feedback').dataset.tone = tone;
  };
  const credentialPath = (suffix = '') =>
    endpoint('/agents/' + workerAgent.id + '/credentials' + suffix);
  function clearSecret() {
    $('nt-secret-value').value = '';
    $('nt-credential-secret').hidden = true;
    $('nt-secret-copy').textContent = 'Copy token';
  }
  function workerLock(value) {
    workerBusy = value;
    credentialsDialog.setAttribute('aria-busy', String(value));
    $('nt-credentials-form').querySelector('fieldset').disabled =
      value ||
      workerUncertain ||
      !snapshot.can_issue_credentials ||
      workerAgent?.status !== 'active';
    for (const control of credentialsDialog.querySelectorAll('button'))
      control.disabled = value || workerUncertain;
    $('nt-credential-team').disabled =
      !workerAgent?.can_manage_team || !snapshot.policy.agents_can_manage_team;
    $('nt-credential-issue').disabled =
      value ||
      workerUncertain ||
      !snapshot.can_issue_credentials ||
      workerAgent?.status !== 'active';
    $('nt-credential-retry').hidden = !workerUncertain;
    $('nt-credential-retry').disabled = value;
  }
  async function renderCredentials() {
    const credentials = await request(credentialPath());
    $('nt-credentials-list').replaceChildren(
      ...credentials.map((credential) => {
        const row = node('li', undefined, 'np-member');
        row.dataset.credentialId = credential.id;
        const info = node('div', undefined, 'np-member-info');
        info.append(
          node(
            'strong',
            credential.valid
              ? 'Active credential'
              : credential.revoked_at
                ? 'Revoked credential'
                : 'Expired or invalidated credential',
          ),
          node(
            'p',
            'Expires ' +
              new Date(credential.expires_at).toLocaleString() +
              ' · ' +
              credential.scopes.join(', '),
            'np-muted',
          ),
        );
        row.append(info);
        if (!credential.revoked_at) {
          const revoke = node('button', 'Revoke', 'np-button np-danger');
          revoke.type = 'button';
          revoke.onclick = () => {
            if (workerBusy || workerUncertain) return;
            workerPending = {
              operation: 'revoke',
              url: credentialPath('/' + credential.id + '/revoke'),
              body: { expected_version: workerAgent.version, idempotency_key: crypto.randomUUID() },
            };
            performWorkerOperation();
          };
          row.append(revoke);
        }
        return row;
      }),
    );
    if (!credentials.length)
      $('nt-credentials-list').append(
        node('li', 'No worker credentials have been issued.', 'np-empty'),
      );
  }
  async function openCredentials(agent) {
    if (busy || uncertain || !snapshot.can_manage_credentials) return;
    lock(true);
    try {
      const fresh = await loadTeam();
      const current = fresh.agents.find((candidate) => candidate.id === agent.id);
      if (!current || !fresh.can_manage_credentials)
        throw Error('Worker access is no longer available for this role.');
      snapshot = fresh;
      workerAgent = current;
      workerPending = null;
      workerUncertain = false;
      clearSecret();
      $('nt-credentials-form').reset();
      $('nt-credentials-form').hidden = !fresh.can_issue_credentials;
      $('nt-credentials-role').textContent = current.name;
      workerFeedback();
      await renderCredentials();
      workerLock(false);
      credentialsDialog.showModal();
      $('nt-credentials-heading').focus();
    } catch (error) {
      feedback(error.message, 'error');
    } finally {
      lock(false);
    }
  }
  async function performWorkerOperation() {
    if (workerBusy || !workerPending) return;
    workerLock(true);
    clearSecret();
    const operation = workerPending.operation;
    let saved = false;
    try {
      const result = await request(workerPending.url, workerPending.body, 'POST');
      saved = true;
      workerUncertain = false;
      workerPending = null;
      if (operation === 'issue') {
        if (result.token) {
          $('nt-secret-value').value = result.token;
          $('nt-credential-secret').hidden = false;
          workerFeedback(
            'Credential issued. Copy the token before closing this dialog.',
            'success',
          );
        } else {
          workerFeedback(
            'The credential was issued, but its token cannot be shown again after a retry. Revoke it and issue a replacement.',
            'warning',
          );
        }
      } else workerFeedback('Credential revoked.', 'success');
      await renderCredentials();
    } catch (error) {
      if (saved)
        workerFeedback(
          'The operation succeeded, but the list could not be refreshed. Reopen Worker access to refresh. ' +
            error.message,
          'warning',
        );
      else if (!error.status || error.status >= 500) {
        workerUncertain = true;
        workerFeedback(
          'The operation could not be confirmed. Retry it before changing or closing this dialog.',
          'error',
        );
      } else {
        workerPending = null;
        workerUncertain = false;
        workerFeedback(
          error.message + ' Reopen Worker access to refresh permissions and the role version.',
          'error',
        );
      }
    } finally {
      workerLock(false);
    }
  }
  $('nt-credentials-form').onsubmit = (event) => {
    event.preventDefault();
    if (
      workerBusy ||
      workerUncertain ||
      !snapshot.can_issue_credentials ||
      workerAgent.status !== 'active'
    )
      return;
    const scopes = ['board:read'];
    if ($('nt-credential-write').checked) scopes.push('board:write');
    if ($('nt-credential-team').checked && !$('nt-credential-team').disabled)
      scopes.push('team:manage');
    workerPending = {
      operation: 'issue',
      url: credentialPath(),
      body: {
        scopes,
        ttl_seconds: Number($('nt-credential-minutes').value) * 60,
        expected_version: workerAgent.version,
        idempotency_key: crypto.randomUUID(),
      },
    };
    performWorkerOperation();
  };
  $('nt-credential-retry').onclick = performWorkerOperation;
  $('nt-secret-copy').onclick = async () => {
    try {
      await navigator.clipboard.writeText($('nt-secret-value').value);
      $('nt-secret-copy').textContent = 'Copied';
    } catch {
      $('nt-secret-copy').textContent = 'Select and copy the token';
      $('nt-secret-value').focus();
      $('nt-secret-value').select();
    }
  };
  function closeCredentials() {
    if (workerBusy || workerUncertain) return;
    clearSecret();
    workerPending = null;
    credentialsDialog.close();
    overview();
    $('nt-heading').focus();
  }
  $('nt-credentials-close').onclick = closeCredentials;
  credentialsDialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    closeCredentials();
  });
  window.addEventListener('beforeunload', (event) => {
    if (workerBusy || workerUncertain) event.preventDefault();
  });
})();
