'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = () => window.SimonNativeProjects;
  const dialog = $('ns-dialog');
  let generation = 0,
    projectId = null,
    workspaceId = null,
    snapshot = null,
    editing = false,
    expectedVersion = null,
    latest = null,
    pending = null,
    busy = false,
    uncertain = false,
    receiptAbsent = false,
    dirty = false;
  const path = (suffix = '') => '/v2/projects/' + projectId + '/storage' + suffix;
  const active = (ticket) =>
    ticket === generation &&
    dialog.open &&
    bridge().getProject()?.id === projectId &&
    bridge().getWorkspaceId() === workspaceId;
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const feedback = (message = '', tone = '') => {
    $('ns-feedback').textContent = message;
    $('ns-feedback').dataset.tone = tone;
  };
  const date = (value) => (value ? new Date(value).toLocaleString() : 'Installation default');
  const bytes = (value) => {
    let size = value,
      unit = 'bytes';
    for (const label of ['KiB', 'MiB', 'GiB', 'TiB']) {
      if (size < 1024) break;
      size /= 1024;
      unit = label;
    }
    return (
      value.toLocaleString() +
      ' bytes' +
      (unit === 'bytes'
        ? ''
        : ' (' + size.toLocaleString(undefined, { maximumFractionDigits: 2 }) + ' ' + unit + ')')
    );
  };
  const localDate = (value) => {
    if (!value) return '';
    const parsed = new Date(value);
    const part = (number) => String(number).padStart(2, '0');
    return (
      parsed.getFullYear() +
      '-' +
      part(parsed.getMonth() + 1) +
      '-' +
      part(parsed.getDate()) +
      'T' +
      part(parsed.getHours()) +
      ':' +
      part(parsed.getMinutes())
    );
  };
  const summary = (entitlement) =>
    'Version ' +
    entitlement.version +
    ' · Plan ' +
    entitlement.plan_key +
    ' · Base allowance: ' +
    bytes(entitlement.limit_bytes) +
    (entitlement.override_limit_bytes === null
      ? ' · No temporary override.'
      : ' · Temporary override: ' +
        bytes(entitlement.override_limit_bytes) +
        ' · Expires ' +
        date(entitlement.override_expires_at));

  function lock() {
    for (const control of dialog.querySelectorAll('input, textarea, button'))
      control.disabled = busy || uncertain;
    for (const control of $('ns-form').querySelectorAll('input, textarea'))
      control.disabled ||= !snapshot?.can_manage;
    $('ns-edit').hidden = !snapshot?.can_manage || editing;
    $('ns-edit').disabled ||= !snapshot?.can_manage;
    $('ns-save').disabled ||= !snapshot?.can_manage || Boolean(latest);
    $('ns-rebase').disabled ||= !snapshot?.can_manage || !latest || latest.version === null;
    $('ns-refresh').disabled = busy || uncertain || editing;
    $('ns-check').disabled = busy || !uncertain;
    $('ns-retry').disabled = busy || !uncertain || !receiptAbsent;
    $('ns-retry').hidden = !receiptAbsent;
    $('ns-uncertain').hidden = !uncertain;
    $('ns-conflict').hidden = !latest;
    $('ns-close').disabled = busy || uncertain;
    $('ns-form').setAttribute('aria-busy', String(busy));
    dialog.setAttribute('aria-busy', String(busy));
  }

  function metric(key, label) {
    const card = node('div', undefined, 'ni-card');
    card.dataset.storageMetric = key;
    card.dataset.bytes = snapshot[key];
    card.append(node('h4', label), node('p', bytes(snapshot[key]), 'np-field-help'));
    return card;
  }

  function render() {
    $('ns-access').textContent = snapshot.can_manage
      ? 'You are authorized as the configured platform administrator to change this workspace allowance.'
      : 'You can view storage. Only the configured platform administrator with a current owner membership can change workspace limits.';
    $('ns-plan').textContent = summary(snapshot.entitlement);
    $('ns-totals').replaceChildren(
      ...[
        ['effective_limit_bytes', 'Effective allowance'],
        ['used_bytes', 'Total usage, including reservations'],
        ['retained_bytes', 'Retained content'],
        ['reserved_bytes', 'Pending upload reservations'],
        ['available_bytes', 'Available for new content'],
        ['overage_bytes', 'Usage above the allowance'],
      ].map(([key, label]) => metric(key, label)),
    );
    $('ns-project-totals').replaceChildren(
      metric('project_used_bytes', "This project's total usage"),
      metric('project_reserved_bytes', "This project's reservations"),
    );
    $('ns-overage').hidden = !snapshot.overage_bytes;
    $('ns-overage').textContent = snapshot.overage_bytes
      ? 'Usage exceeds the effective allowance by ' +
        bytes(snapshot.overage_bytes) +
        '. Retained content remains available; new storage admission is blocked until capacity is available.'
      : '';
    $('ns-reservations').textContent =
      snapshot.reservation_count +
      ' pending upload reservation' +
      (snapshot.reservation_count === 1 ? '' : 's') +
      ' holding ' +
      bytes(snapshot.reserved_bytes) +
      '.';
    $('ns-accounting').textContent = snapshot.accounting;
    $('ns-history').replaceChildren(
      ...(snapshot.history.length
        ? snapshot.history.map((record) => {
            const card = node('li', undefined, 'ni-card');
            card.dataset.entitlementVersion = record.version;
            card.append(
              node('h4', 'Version ' + record.version),
              node('p', summary(record), 'ni-objective'),
              node('p', record.reason, 'ni-objective'),
              node(
                'p',
                'Issued ' + date(record.updated_at) + ' · Administrator ' + record.issued_by,
                'np-field-help',
              ),
            );
            return card;
          })
        : [
            node(
              'li',
              snapshot.entitlement.version === 0
                ? 'No entitlement changes have been recorded.'
                : 'Entitlement history is restricted to platform administrators and workspace owners.',
              'np-empty',
            ),
          ]),
    );
    $('ns-history-note').textContent = snapshot.history_truncated
      ? 'Showing the latest 50 entitlement changes.'
      : '';
    lock();
  }

  function overview() {
    editing = false;
    latest = null;
    expectedVersion = null;
    dirty = false;
    $('ns-form').reset();
    $('ns-form').hidden = true;
    $('ns-save').hidden = $('ns-cancel').hidden = true;
    $('ns-version').textContent = $('ns-latest').textContent = '';
    lock();
  }

  async function refresh() {
    const ticket = generation;
    const result = await bridge().request(path());
    if (!active(ticket)) return false;
    if (editing && !result.can_manage) {
      reset();
      return false;
    }
    snapshot = result;
    render();
    return true;
  }

  function edit() {
    if (busy || uncertain || !snapshot?.can_manage) return;
    editing = true;
    latest = null;
    expectedVersion = snapshot.entitlement.version;
    const saved = snapshot.entitlement;
    $('ns-plan-key').value = saved.plan_key;
    $('ns-limit').value = saved.limit_bytes;
    $('ns-override').value = saved.override_limit_bytes ?? '';
    $('ns-expiry').value = localDate(saved.override_expires_at);
    $('ns-reason').value = '';
    $('ns-version').textContent =
      'Proposing a change against entitlement version ' + expectedVersion + '.';
    $('ns-form').hidden = false;
    $('ns-save').hidden = $('ns-cancel').hidden = false;
    feedback();
    lock();
    $('ns-editor-heading').focus();
  }

  function failure(error) {
    if ([401, 403, 404].includes(error.status)) {
      reset();
      $('np-status').textContent = error.message;
      $('np-status').dataset.tone = 'error';
    } else feedback(error.message, 'error');
  }

  async function perform() {
    if (busy || !pending) return;
    const ticket = generation,
      operation = pending;
    busy = true;
    lock();
    feedback('Saving the workspace allowance…');
    try {
      const result = await bridge().request(operation.url, operation.body, 'POST');
      if (!active(ticket)) return;
      pending = null;
      uncertain = receiptAbsent = false;
      snapshot = result;
      overview();
      render();
      feedback(
        'Entitlement version ' +
          result.applied_entitlement.version +
          ' is recorded. Current usage and limits are shown.',
        'success',
      );
    } catch (error) {
      if (!active(ticket)) return;
      if (!error.status || error.status >= 500) {
        uncertain = true;
        receiptAbsent = false;
        feedback(
          'The response could not be confirmed. Resolve this change before making another.',
          'warning',
        );
      } else {
        pending = null;
        uncertain = receiptAbsent = false;
        if (error.status === 409) {
          try {
            if (await refresh()) {
              latest = snapshot.entitlement;
              $('ns-latest').textContent = summary(latest);
              feedback(error.message + ' Your draft values and reason are preserved.', 'warning');
            }
          } catch (lookupError) {
            latest = { version: null };
            $('ns-latest').textContent =
              'Current entitlement could not be loaded. Cancel this edit and refresh before trying again.';
            failure(lookupError);
          }
        } else failure(error);
      }
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  async function checkOperation() {
    if (busy || !pending || !uncertain) return;
    const ticket = generation;
    busy = true;
    lock();
    try {
      const result = await bridge().request(
        path('/operations/' + encodeURIComponent(pending.body.idempotency_key)),
      );
      if (!active(ticket)) return;
      pending = null;
      uncertain = receiptAbsent = false;
      snapshot = result;
      overview();
      render();
      feedback(
        'Entitlement version ' +
          result.applied_entitlement.version +
          ' is recorded. Current usage and limits are shown.',
        'success',
      );
    } catch (error) {
      if (!active(ticket)) return;
      if (error.status === 404) {
        try {
          if (await refresh()) {
            if (!snapshot.can_manage) {
              reset();
              return;
            }
            receiptAbsent = true;
            feedback(
              'No saved change was found. Retry the same request with its original version and identifier.',
              'warning',
            );
          }
        } catch (accessError) {
          if (active(ticket)) failure(accessError);
        }
      } else failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  function reset() {
    generation++;
    projectId = workspaceId = snapshot = expectedVersion = latest = pending = null;
    busy = uncertain = receiptAbsent = dirty = editing = false;
    $('ns-form').reset();
    for (const id of [
      'ns-project',
      'ns-access',
      'ns-plan',
      'ns-totals',
      'ns-overage',
      'ns-reservations',
      'ns-accounting',
      'ns-project-totals',
      'ns-history',
      'ns-history-note',
      'ns-version',
      'ns-latest',
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
    $('ns-project').textContent = project.name;
    dialog.showModal();
    overview();
    busy = true;
    lock();
    $('ns-heading').focus();
    feedback('Loading storage usage…');
    try {
      if (await refresh()) feedback();
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  $('ns-form').addEventListener('input', () => {
    dirty = true;
  });
  $('ns-form').onsubmit = async (event) => {
    event.preventDefault();
    if (
      busy ||
      uncertain ||
      latest ||
      !editing ||
      !snapshot?.can_manage ||
      !event.currentTarget.reportValidity()
    )
      return;
    const override = $('ns-override').value,
      expiry = $('ns-expiry').value;
    if (Boolean(override) !== Boolean(expiry)) {
      feedback('Set both override fields, or leave both empty.', 'error');
      return;
    }
    pending = {
      url: path('/entitlement'),
      body: {
        idempotency_key: crypto.randomUUID(),
        expected_version: expectedVersion,
        plan_key: $('ns-plan-key').value,
        limit_bytes: Number($('ns-limit').value),
        override_limit_bytes: override ? Number(override) : null,
        override_expires_at: expiry ? new Date(expiry).toISOString() : null,
        reason: $('ns-reason').value,
      },
    };
    await perform();
  };
  $('np-storage').onclick = open;
  $('ns-edit').onclick = edit;
  $('ns-check').onclick = checkOperation;
  $('ns-retry').onclick = perform;
  $('ns-rebase').onclick = () => {
    if (busy || uncertain || !latest || latest.version === null) return;
    expectedVersion = latest.version;
    latest = null;
    $('ns-version').textContent =
      'Proposing a change against entitlement version ' + expectedVersion + '.';
    feedback(
      'Your draft is now based on the current entitlement. Review the proposed values before saving.',
    );
    lock();
  };
  $('ns-cancel').onclick = () => {
    if (!busy && !uncertain) {
      overview();
      feedback();
    }
  };
  $('ns-close').onclick = () => {
    if (!busy && !uncertain) reset();
  };
  $('ns-refresh').onclick = async () => {
    if (busy || uncertain || editing) return;
    const ticket = generation;
    busy = true;
    lock();
    try {
      if (await refresh()) feedback('Current storage usage loaded.');
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  };
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    if (!editing && !busy && !uncertain) reset();
  });
  window.addEventListener('beforeunload', (event) => {
    if (busy || uncertain || dirty) event.preventDefault();
  });
  window.addEventListener('native-project-loading', (event) => {
    if (projectId && event.detail.id !== projectId) reset();
  });
  window.addEventListener('native-project-unavailable', reset);
  window.SimonNativeStorage = { open };
})();
