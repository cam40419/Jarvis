'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const dialog = $('nm-dialog');
  const bridge = () => window.SimonNativeProjects;
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const button = (text, callback) => {
    const item = node('button', text, 'np-button np-quiet');
    item.type = 'button';
    item.onclick = callback;
    return item;
  };
  const money = (value) => '$' + (Number(value || 0) / 1000000).toFixed(6).replace(/0{1,4}$/, '');
  const date = (value) => new Date(value).toLocaleString();
  const feedback = (text = '', tone = '') => {
    $('nm-feedback').textContent = text;
    $('nm-feedback').dataset.tone = tone;
  };
  let projectId, workspaceId, snapshot, editing, latest, pending;
  let generation = 0,
    busy = false,
    uncertain = false,
    receiptAbsent = false,
    dirty = false;
  let usage = [],
    hasMore = false;
  const path = (suffix = '') => '/v2/projects/' + projectId + '/models' + suffix;
  const active = (ticket) =>
    ticket === generation &&
    dialog.open &&
    bridge().getProject()?.id === projectId &&
    bridge().getWorkspaceId() === workspaceId;
  const forms = ['connection', 'policy', 'probe', 'reconcile'];
  const currentForm = () => $('nm-' + editing?.form + '-form');
  const modelName = (id) =>
    id
      ? snapshot.models.find((model) => model.id === id)?.label || 'Unavailable model'
      : 'Automatic qualified selection';
  const rates = (model) =>
    `Input $${model.input_cost_per_million_usd ?? 'unknown'} / output $${model.output_cost_per_million_usd ?? 'unknown'} per million tokens`;

  function lock() {
    dialog.setAttribute('aria-busy', String(busy));
    for (const control of dialog.querySelectorAll('button')) control.disabled = busy || uncertain;
    for (const fieldset of dialog.querySelectorAll('fieldset'))
      fieldset.disabled = busy || uncertain;
    $('nm-check').disabled = busy;
    $('nm-retry').disabled = busy;
    $('nm-retry-key').disabled = busy;
    $('nm-uncertain').hidden = !uncertain;
    $('nm-check').hidden = !pending?.receipt;
    $('nm-secret-retry').hidden = !uncertain || !pending?.secret || !receiptAbsent;
    $('nm-retry').hidden = !uncertain || (pending?.receipt && !receiptAbsent);
    $('nm-uncertain-text').textContent = pending?.receipt
      ? receiptAbsent
        ? 'No saved receipt was found. Re-enter the same key if requested and retry the original request. Its operation identifier is preserved to prevent a duplicate.'
        : 'The response was lost. The submitted key has been cleared. Check the saved result before making another change.'
      : 'The response was lost. Retry the original request before editing; its operation identifier is preserved.';
    $('nm-save').disabled = busy || uncertain || Boolean(latest);
    $('nm-add').disabled = busy || uncertain || !snapshot?.can_manage || !snapshot.templates.length;
    $('nm-template').disabled = busy || uncertain || Boolean(editing?.record);
    $('nm-project-policy').hidden = !snapshot?.can_manage;
    $('nm-workspace-policy').hidden = !snapshot?.can_manage_workspace;
    $('nm-add').hidden = !snapshot?.can_manage;
  }

  function clearSecrets() {
    $('nm-key').value = '';
    $('nm-retry-key').value = '';
  }

  function overview() {
    editing = null;
    latest = null;
    dirty = false;
    clearSecrets();
    for (const name of forms) {
      $('nm-' + name + '-form').hidden = true;
      $('nm-' + name + '-form').dataset.editing = 'false';
    }
    $('nm-overview').hidden = false;
    $('nm-save').hidden = true;
    $('nm-cancel').hidden = true;
    $('nm-close').hidden = false;
    $('nm-conflict').hidden = true;
    lock();
  }

  function showEditor(form, record = null, scope = null) {
    if (busy || uncertain) return false;
    clearSecrets();
    editing = { form, record, scope };
    latest = null;
    dirty = false;
    pending = null;
    for (const name of forms) {
      $('nm-' + name + '-form').hidden = name !== form;
      $('nm-' + name + '-form').dataset.editing = 'false';
    }
    $('nm-overview').hidden = true;
    $('nm-save').hidden = false;
    $('nm-save').setAttribute('form', 'nm-' + form + '-form');
    $('nm-save').textContent =
      form === 'probe'
        ? 'Run qualification'
        : form === 'reconcile'
          ? 'Record verified charge'
          : 'Save settings';
    $('nm-cancel').hidden = false;
    $('nm-close').hidden = true;
    $('nm-conflict').hidden = true;
    feedback();
    lock();
    return true;
  }

  function templateDescription() {
    const template = snapshot.templates.find((item) => item.id === $('nm-template').value);
    $('nm-template-description').textContent = template
      ? `${template.local ? 'Local' : 'Hosted'} · ${template.provider} · ${template.model}. ${rates(template)}. ${template.data_policy || ''} ${template.license_note || ''}`
      : 'This approved template is no longer available. Disable this model or choose another model.';
    $('nm-key-field').hidden = !template?.credential_required;
    $('nm-key').required = Boolean(
      template?.credential_required && !editing?.record?.has_credential,
    );
  }

  function editConnection(record = null) {
    if (!snapshot.can_manage || !showEditor('connection', record)) return;
    $('nm-connection-heading').textContent = record ? 'Edit ' + record.label : 'Add project model';
    $('nm-template').replaceChildren(
      ...snapshot.templates.map(
        (template) => new Option(template.name + ' · ' + template.model, template.id),
      ),
    );
    if (record && !snapshot.templates.some((template) => template.id === record.template_id))
      $('nm-template').append(
        new Option(record.template_id + ' (unavailable)', record.template_id),
      );
    if (record) $('nm-template').value = record.template_id;
    $('nm-label').value = record?.label || '';
    $('nm-enabled').checked = record?.enabled ?? true;
    $('nm-enabled-field').hidden = !record;
    templateDescription();
    $('nm-label').focus();
  }

  function editPolicy(scope) {
    if (!(scope === 'workspace' ? snapshot.can_manage_workspace : snapshot.can_manage)) return;
    const policy = snapshot[scope + '_policy'];
    if (!showEditor('policy', policy, scope)) return;
    $('nm-policy-heading').textContent =
      scope === 'workspace' ? 'Workspace limits' : 'Project settings';
    $('nm-project-routing').hidden = scope === 'workspace';
    for (const [id, field] of [
      ['lifetime', 'lifetime'],
      ['daily', 'daily'],
      ['monthly', 'monthly'],
      ['operation', 'per_operation'],
    ])
      $('nm-' + id).value = String(policy[field + '_limit_microusd'] / 1000000);
    $('nm-concurrent').value = policy.max_concurrent_calls;
    $('nm-paused').checked = policy.paused;
    $('nm-paid').checked = policy.allow_paid;
    $('nm-cloud').checked = policy.allow_cloud;
    for (const [id, field] of [
      ['planning', 'planning_model_id'],
      ['review', 'review_model_id'],
    ]) {
      $('nm-' + id).replaceChildren(
        new Option('Automatic qualified selection', ''),
        ...snapshot.models.map(
          (model) => new Option(model.label + (model.ready ? '' : ' (not ready)'), model.id),
        ),
      );
      if (policy[field] && !snapshot.models.some((model) => model.id === policy[field]))
        $('nm-' + id).append(new Option('Unavailable model', policy[field]));
      $('nm-' + id).value = policy[field] || '';
    }
  }

  function probe(model) {
    if (!snapshot.can_manage || !showEditor('probe', model)) return;
    $('nm-probe-description').textContent =
      `${model.label} · ${model.model}. ${rates(model)}. ${model.qualification_reservation_microusd == null ? 'The service calculates the reservation before dispatch.' : 'Maximum reservation ' + money(model.qualification_reservation_microusd) + '.'} Both project and workspace limits apply.`;
    $('nm-probe-consent').checked = false;
  }

  function reconcile(item) {
    if (!snapshot.can_reconcile || !showEditor('reconcile', item)) return;
    $('nm-reconcile-description').textContent =
      `${item.phase} · ${item.model} · ${date(item.started_at)}. Held ${money(item.held_microusd)}; recorded charge ${money(item.charged_microusd)}. Call ${item.id}.`;
    $('nm-charge').value = '';
    $('nm-reason').value = '';
    $('nm-evidence').value = '';
  }

  function totals(scope) {
    const policy = snapshot[scope + '_policy'],
      total = snapshot[scope + '_totals'];
    const card = node('section', undefined, 'ni-card');
    card.append(node('h4', scope === 'workspace' ? 'Workspace' : 'Project'));
    if (!total) {
      card.append(node('p', 'Workspace usage is visible to project and workspace owners.'));
      return card;
    }
    card.append(
      node(
        'p',
        `${policy.paused ? 'Paused · ' : ''}Spent ${money(total.charged_lifetime)} · Reserved ${money(total.held_microusd)} · ${total.active_calls}/${policy.max_concurrent_calls} occupied call slots`,
      ),
    );
    const details = node('dl');
    for (const [label, charged, ceiling] of [
      ['Lifetime', total.charged_lifetime, policy.lifetime_limit_microusd],
      ['Today (UTC)', total.charged_day, policy.daily_limit_microusd],
      ['This month (UTC)', total.charged_month, policy.monthly_limit_microusd],
    ])
      details.append(
        node('dt', label),
        node(
          'dd',
          `${money(charged)} / ${money(ceiling)} · Available ${money(Math.max(0, ceiling - charged - total.held_microusd))}`,
        ),
      );
    details.append(
      node('dt', 'Per operation'),
      node('dd', money(policy.per_operation_limit_microusd)),
    );
    card.append(details);
    return card;
  }

  function renderUsage() {
    $('nm-usage').replaceChildren(
      ...usage.map((item) => {
        const row = node('li', undefined, 'ni-card');
        row.dataset.usageId = item.id;
        row.append(
          node('h4', `${item.phase.replaceAll('_', ' ')} · ${item.status}`),
          node('p', `${item.model || 'Historical intake'} · ${date(item.started_at)}`),
          node(
            'p',
            `Charged ${money(item.charged_microusd)} · Reserved ${money(item.held_microusd)}${item.input_tokens == null ? '' : ' · Input tokens ' + item.input_tokens}${item.output_tokens == null ? '' : ' · Output tokens ' + item.output_tokens}`,
          ),
        );
        if (item.error_code)
          row.append(node('p', item.error_code.replaceAll('_', ' '), 'np-muted'));
        if (item.status === 'unknown') {
          row.append(
            node(
              'p',
              'A charge may have occurred. The reservation remains held until provider evidence is recorded.',
            ),
          );
          if (snapshot.can_reconcile && new Date(item.deadline_at).getTime() <= Date.now())
            row.append(button('Reconcile charge', () => reconcile(item)));
          else
            row.append(
              node(
                'p',
                snapshot.can_reconcile
                  ? 'Reconciliation becomes available after the call deadline. Refresh then.'
                  : 'A workspace owner can reconcile this call after its deadline.',
                'np-field-help',
              ),
            );
        }
        if (item.reconciliation_reason)
          row.append(node('p', 'Reconciliation: ' + item.reconciliation_reason));
        if (item.reconciliation_evidence)
          row.append(node('p', 'Evidence: ' + item.reconciliation_evidence));
        return row;
      }),
    );
    if (!usage.length)
      $('nm-usage').append(
        node('li', 'No model usage has been recorded for this project.', 'np-empty'),
      );
    $('nm-more').hidden = !hasMore;
  }

  function render() {
    $('nm-access').textContent = snapshot.can_manage
      ? 'Project owner · Manage this project’s models, routes and spending limits.'
      : 'Read only · A project owner can manage models and resource settings.';
    $('nm-models').replaceChildren(
      ...snapshot.models.map((model) => {
        const row = node('li', undefined, 'ni-card');
        row.dataset.modelId = model.id;
        row.append(
          node('h4', model.label),
          node(
            'p',
            `${model.ready ? 'Ready' : model.enabled ? 'Not ready' : 'Disabled'} · ${model.local ? 'Local' : 'Hosted'} · ${model.provider} · ${model.model}`,
          ),
          node('p', rates(model), 'np-field-help'),
        );
        row.append(
          node(
            'p',
            model.reason ||
              (model.ready
                ? 'Qualified for basic text and JSON.'
                : 'Qualification is required before routing calls.'),
          ),
        );
        row.append(
          node(
            'p',
            `${model.has_credential ? 'Project key stored · revision ' + model.credential_revision : 'No project key stored'}${model.qualified_at ? ' · Last qualification ' + date(model.qualified_at) : ''}`,
            'np-muted',
          ),
        );
        if (model.qualification_error)
          row.append(
            node(
              'p',
              'Qualification: ' + model.qualification_error.replaceAll('_', ' '),
              'np-field-help',
            ),
          );
        if (snapshot.can_manage) {
          const actions = node('div', undefined, 'np-actions');
          actions.append(button('Edit model', () => editConnection(model)));
          if (model.enabled) actions.append(button('Qualify model', () => probe(model)));
          row.append(actions);
        }
        return row;
      }),
    );
    if (!snapshot.models.length)
      $('nm-models').append(
        node(
          'li',
          snapshot.templates.length
            ? 'No project models yet. Add an approved model, then qualify it before planning.'
            : 'No approved models are configured. An administrator must add provider or local endpoint templates.',
          'np-empty',
        ),
      );
    $('nm-routes').textContent =
      `Planning: ${modelName(snapshot.project_policy.planning_model_id)}. Review: ${modelName(snapshot.project_policy.review_model_id)}. Hosted processing ${snapshot.project_policy.allow_cloud ? 'allowed' : 'disabled'}; paid models ${snapshot.project_policy.allow_paid ? 'allowed' : 'disabled'}.`;
    $('nm-totals').replaceChildren(totals('project'), totals('workspace'));
    renderUsage();
    lock();
  }

  async function refresh() {
    const ticket = generation;
    try {
      const [found, records] = await Promise.all([
        bridge().request(path()),
        bridge().request(path('/usage?offset=0&limit=50')),
      ]);
      if (!active(ticket)) return false;
      snapshot = found;
      usage = records.items;
      hasMore = records.has_more;
      if (
        editing &&
        !(editing.scope === 'workspace'
          ? found.can_manage_workspace
          : editing.form === 'reconcile'
            ? found.can_reconcile
            : found.can_manage)
      ) {
        pending = null;
        uncertain = false;
        overview();
        feedback('Your access changed. The current settings are read only.', 'warning');
      }
      render();
      if (found.configuration_error) feedback(found.configuration_error, 'warning');
      return true;
    } catch (error) {
      if (active(ticket) && [401, 403, 404].includes(error.status)) {
        reset();
        $('np-status').textContent = error.message;
        $('np-status').dataset.tone = 'error';
      }
      throw error;
    }
  }

  function amount(id, max = 1000000000000) {
    const raw = $(id).value;
    if (!/^\d+(?:\.\d{1,6})?$/.test(raw))
      throw Error('Enter a nonnegative USD amount with at most six decimal places.');
    const [whole, fraction = ''] = raw.split('.');
    const value = Number(whole) * 1000000 + Number(fraction.padEnd(6, '0'));
    if (!Number.isSafeInteger(value) || value > max)
      throw Error('The amount is outside the supported limit.');
    return value;
  }

  function operation() {
    const record = editing.record;
    const version = record?.version;
    const command = { idempotency_key: crypto.randomUUID() };
    if (editing.form === 'connection')
      return {
        url: path('/connections' + (record ? '/' + record.id : '')),
        method: record ? 'PUT' : 'POST',
        receipt: true,
        secret: Boolean($('nm-key').value),
        body: {
          ...command,
          label: $('nm-label').value.trim(),
          ...(record
            ? { expected_version: version, enabled: $('nm-enabled').checked }
            : { template_id: $('nm-template').value }),
        },
      };
    if (editing.form === 'policy')
      return {
        url: path(editing.scope === 'workspace' ? '/workspace-policy' : '/policy'),
        method: 'PUT',
        body: {
          ...command,
          expected_version: version,
          lifetime_limit_microusd: amount('nm-lifetime'),
          daily_limit_microusd: amount('nm-daily'),
          monthly_limit_microusd: amount('nm-monthly'),
          per_operation_limit_microusd: amount('nm-operation'),
          max_concurrent_calls: Number($('nm-concurrent').value),
          paused: $('nm-paused').checked,
          ...(editing.scope === 'workspace'
            ? {}
            : {
                allow_paid: $('nm-paid').checked,
                allow_cloud: $('nm-cloud').checked,
                planning_model_id: $('nm-planning').value || null,
                review_model_id: $('nm-review').value || null,
              }),
        },
      };
    if (editing.form === 'probe')
      return {
        url: path('/connections/' + record.id + '/probe'),
        method: 'POST',
        body: { ...command, expected_version: version },
      };
    return {
      url: path('/usage/' + record.id + '/reconcile'),
      method: 'POST',
      body: {
        ...command,
        expected_version: version,
        charged_microusd: amount('nm-charge', Number.MAX_SAFE_INTEGER),
        reason: $('nm-reason').value.trim(),
        evidence: $('nm-evidence').value.trim(),
      },
    };
  }

  async function conflict() {
    if (!editing || !(await refresh())) return;
    latest =
      editing.form === 'policy'
        ? snapshot[editing.scope + '_policy']
        : editing.form === 'reconcile'
          ? usage.find((item) => item.id === editing.record.id)
          : snapshot.models.find((model) => model.id === editing.record?.id);
    if (!latest || latest.version === editing.record?.version) {
      latest = null;
      return;
    }
    $('nm-latest').textContent = JSON.stringify(latest, null, 2);
    $('nm-conflict').hidden = false;
    lock();
  }

  async function saved(message) {
    pending = null;
    uncertain = false;
    receiptAbsent = false;
    clearSecrets();
    overview();
    await refresh();
    feedback(message, 'success');
    window.dispatchEvent(new CustomEvent('native-models-changed', { detail: { projectId } }));
  }

  async function perform(secret = '') {
    if (busy || !pending) return;
    const ticket = generation,
      command = pending;
    busy = true;
    clearSecrets();
    lock();
    feedback(
      editing?.form === 'probe'
        ? 'Checking the model with a bounded synthetic request…'
        : 'Saving…',
    );
    let accepted = false;
    // The persistent pending command contains no credential. Clear the transient body
    // as soon as fetch serializes it, including on errors and uncertain responses.
    const body = { ...command.body, ...(command.secret ? { credential: secret } : {}) };
    secret = '';
    try {
      const request = bridge().request(command.url, body, command.method);
      delete body.credential;
      const result = await request;
      if (!active(ticket)) return;
      accepted = true;
      await saved(
        editing?.form === 'probe'
          ? result.ready
            ? 'Model qualification passed.'
            : 'Qualification finished. Review the model status and usage below.'
          : 'Settings saved.',
      );
    } catch (error) {
      if (!active(ticket)) return;
      if (accepted)
        feedback(
          'Saved, but the current settings could not be refreshed. Refresh before continuing.',
          'warning',
        );
      else if (!error.status || error.status >= 500) {
        uncertain = true;
        feedback(
          'The response could not be confirmed. Resolve the original request before making another change.',
          'warning',
        );
      } else {
        if (error.status === 409 && command.receipt) {
          try {
            await bridge().request(
              path('/operations/' + encodeURIComponent(command.body.idempotency_key)),
            );
            if (active(ticket)) {
              accepted = true;
              await saved(
                'The original model settings were already saved. Their result was recovered.',
              );
            }
            return;
          } catch (lookupError) {
            if (!active(ticket)) return;
            if (!lookupError.status || lookupError.status >= 500) {
              uncertain = true;
              feedback(
                'The original result could not be checked. Check its saved result before continuing.',
                'warning',
              );
              return;
            }
          }
        }
        pending = null;
        uncertain = false;
        feedback(
          error.message + (command.secret ? ' Re-enter the key if you retry.' : ''),
          'error',
        );
        if ([401, 403, 404].includes(error.status)) {
          try {
            await refresh();
          } catch {
            /* refresh clears inaccessible state */
          }
        } else if (error.status === 409) {
          try {
            await conflict();
          } catch {
            feedback(error.message + ' Refresh to compare saved settings.', 'error');
          }
        }
      }
    } finally {
      delete body.credential;
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  async function checkReceipt() {
    if (busy || !pending?.receipt) return;
    const ticket = generation;
    busy = true;
    lock();
    try {
      await bridge().request(
        path('/operations/' + encodeURIComponent(pending.body.idempotency_key)),
      );
      if (active(ticket)) await saved('Saved model settings recovered. No key was sent again.');
    } catch (error) {
      if (!active(ticket)) return;
      if (error.status === 404) {
        // Confirm project access before interpreting 404 as an absent receipt.
        try {
          if (await refresh()) {
            receiptAbsent = true;
            feedback('No saved receipt was found for this request.', 'warning');
          }
        } catch {
          /* refresh clears inaccessible state */
        }
      } else if ([401, 403].includes(error.status)) {
        try {
          await refresh();
        } catch {
          /* refresh clears inaccessible state */
        }
      } else feedback('The saved result could not be checked. Try again.', 'warning');
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (busy || uncertain || latest || !editing || !currentForm().reportValidity()) return;
    try {
      pending = operation();
      await perform($('nm-key').value);
    } catch (error) {
      clearSecrets();
      feedback(error.message, 'error');
    }
  }
  for (const name of forms) {
    $('nm-' + name + '-form').onsubmit = submit;
    $('nm-' + name + '-form').addEventListener('input', () => {
      dirty = true;
      currentForm().dataset.editing = 'true';
    });
  }
  $('nm-template').onchange = templateDescription;
  $('nm-add').onclick = () => editConnection();
  $('nm-project-policy').onclick = () => editPolicy('project');
  $('nm-workspace-policy').onclick = () => editPolicy('workspace');
  $('nm-cancel').onclick = () => {
    if (!busy && !uncertain) {
      overview();
      feedback();
    }
  };
  $('nm-rebase').onclick = () => {
    if (busy || uncertain || !latest) return;
    editing.record = latest;
    latest = null;
    $('nm-conflict').hidden = true;
    feedback(
      'Your draft is preserved against the latest version. Review it before saving.',
      'warning',
    );
    lock();
  };
  $('nm-check').onclick = checkReceipt;
  $('nm-retry').onclick = () => {
    if (pending?.secret && !$('nm-retry-key').value) {
      feedback('Re-enter the same API key to retry this request.', 'error');
      return;
    }
    perform($('nm-retry-key').value);
  };
  $('nm-refresh').onclick = async () => {
    if (busy || uncertain) return;
    busy = true;
    lock();
    try {
      if (await refresh()) feedback('Current model settings and usage loaded.', 'success');
    } catch (error) {
      if (dialog.open) feedback(error.message, 'error');
    } finally {
      busy = false;
      if (dialog.open) lock();
    }
  };
  $('nm-more').onclick = async () => {
    if (busy || uncertain) return;
    const ticket = generation;
    busy = true;
    lock();
    try {
      const found = await bridge().request(path('/usage?offset=' + usage.length + '&limit=50'));
      if (!active(ticket)) return;
      const seen = new Set(usage.map((item) => item.id));
      usage.push(...found.items.filter((item) => !seen.has(item.id)));
      hasMore = found.has_more;
      renderUsage();
    } catch (error) {
      if (active(ticket)) feedback(error.message, 'error');
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  };

  function reset() {
    generation++;
    clearSecrets();
    projectId = workspaceId = snapshot = editing = latest = pending = null;
    busy = uncertain = receiptAbsent = dirty = false;
    usage = [];
    hasMore = false;
    for (const name of forms) $('nm-' + name + '-form').reset();
    for (const id of [
      'nm-models',
      'nm-usage',
      'nm-totals',
      'nm-latest',
      'nm-template-description',
      'nm-probe-description',
      'nm-reconcile-description',
    ])
      $(id).replaceChildren();
    feedback();
    if (dialog.open) dialog.close();
  }

  async function open() {
    const project = bridge().getProject();
    if (!project || dialog.open) return;
    reset();
    projectId = project.id;
    workspaceId = bridge().getWorkspaceId();
    const ticket = ++generation;
    $('nm-project').textContent = project.name;
    dialog.showModal();
    overview();
    busy = true;
    lock();
    $('nm-heading').focus();
    feedback('Loading model settings…');
    try {
      if (await refresh()) feedback(snapshot.configuration_error || '');
    } catch (error) {
      if (active(ticket)) feedback(error.message, 'error');
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }
  function close() {
    if (busy || uncertain) return;
    reset();
  }
  $('np-models').onclick = open;
  $('nm-close').onclick = close;
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    close();
  });
  window.addEventListener('beforeunload', (event) => {
    if (busy || uncertain || dirty) event.preventDefault();
  });
  window.addEventListener('native-project-loading', (event) => {
    if (projectId && event.detail.id !== projectId) reset();
  });
  window.addEventListener('native-project-unavailable', reset);
  window.SimonNativeModels = { open };
})();
