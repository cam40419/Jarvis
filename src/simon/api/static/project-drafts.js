'use strict';
(() => {
  const records = new Map();
  const bindings = new Map();
  const node = (tag, text) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    return element;
  };
  function identity(projectId, key) {
    return [
      'simon-project-draft-v1',
      session?.workspace_id,
      session?.actor_id,
      projectId,
      key,
    ].join(':');
  }
  function cache(record) {
    try {
      localStorage.setItem(
        record.id,
        JSON.stringify({
          text: record.text,
          base: record.base,
          version: record.version,
          dirty: record.dirty,
        }),
      );
      record.local = true;
    } catch (_) {
      record.local = false;
    }
  }
  function endpoint(record) {
    return (
      '/v1/projects/' +
      encodeURIComponent(record.projectId) +
      '/workspace/drafts/' +
      encodeURIComponent(record.key)
    );
  }
  function changed(record, value) {
    record.pristineDefault = false;
    record.text = value;
    record.dirty = record.text !== record.base;
    record.error = '';
    cache(record);
    render(record);
    clearTimeout(record.timer);
    if (record.loaded && record.dirty && !record.conflict)
      record.timer = setTimeout(() => save(record), 600);
  }
  function assign(record, text) {
    record.text = text;
    for (const binding of bindings.values())
      if (binding.record === record) {
        binding.field.value = text;
        binding.onChange?.(text);
      }
  }
  function render(record) {
    for (const binding of bindings.values())
      if (binding.record === record) renderBinding(record, binding);
  }
  function renderBinding(record, binding) {
    const target = binding.statusTarget;
    const writable = session?.scopes?.includes('jobs:write');
    let message = record.saving
      ? 'Saving draft…'
      : !record.loaded
        ? 'Loading saved draft…'
        : record.dirty
          ? record.local
            ? 'Draft saved on this device.'
            : 'Draft is only in this open page.'
          : record.text && !record.pristineDefault
            ? 'Draft saved.'
            : '';
    if (record.error) message += ' Could not sync with the server.';
    if (!writable && record.dirty) message += ' Server saving requires project write access.';
    if (record.conflict)
      message =
        'A different draft was saved elsewhere. Your text has been kept; choose which version to use.';
    const fingerprint = JSON.stringify([message, record.conflict, record.saving]);
    if (target.dataset.fingerprint === fingerprint) return;
    target.dataset.fingerprint = fingerprint;
    target.replaceChildren(node('span', message));
    target.classList.toggle('pc-draft-conflict', Boolean(record.conflict));
    function action(label, callback) {
      const control = node('button', label);
      control.type = 'button';
      control.className = 'pc-text-button';
      control.disabled = record.saving;
      control.onclick = callback;
      target.append(control);
    }
    if (record.conflict) {
      const details = node('details');
      details.append(
        node('summary', 'Review saved version'),
        node('pre', record.conflict.text || '(Empty draft)'),
      );
      target.append(details);
      action('Use saved draft', () => {
        const remote = record.conflict;
        record.conflict = null;
        record.version = remote.version;
        record.base = remote.text;
        assign(record, remote.text);
        record.dirty = false;
        record.error = '';
        cache(record);
        render(record);
      });
      if (writable)
        action('Keep my draft', () => {
          record.version = record.conflict.version;
          record.base = record.conflict.text;
          record.conflict = null;
          record.dirty = record.text !== record.base;
          cache(record);
          save(record);
        });
    } else if (record.error && writable) action('Retry draft sync', () => load(record));
  }
  async function load(record) {
    if (record.loading) return;
    record.loading = true;
    try {
      const remote = await api(endpoint(record));
      if (remote.version < record.version) return;
      if (
        record.dirty &&
        remote.text !== record.text &&
        remote.version !== record.version &&
        remote.text !== record.base
      ) {
        record.conflict = remote;
      } else {
        record.version = remote.version;
        record.base = remote.text;
        if (!record.dirty || remote.text === record.text) {
          record.pristineDefault = !remote.text && record.defaultText !== undefined;
          assign(record, record.pristineDefault ? record.defaultText : remote.text);
        }
        record.dirty = !record.pristineDefault && record.text !== record.base;
        record.conflict = null;
      }
      record.loaded = true;
      record.error = '';
      cache(record);
    } catch (error) {
      record.error = error.message;
    } finally {
      record.loading = false;
      render(record);
      if (record.loaded && record.dirty && !record.conflict && !record.error) save(record);
    }
  }
  async function save(record) {
    if (
      !record.loaded ||
      record.saving ||
      record.conflict ||
      !record.dirty ||
      !session?.scopes?.includes('jobs:write')
    )
      return;
    const text = record.text,
      version = record.version;
    record.saving = true;
    render(record);
    let successful = false;
    try {
      const receipt = await api(
        endpoint(record),
        { expected_version: version, text, idempotency_key: crypto.randomUUID() },
        'PUT',
      );
      record.version = receipt.version;
      record.base = receipt.text;
      record.dirty = record.text !== record.base;
      record.error = '';
      successful = true;
    } catch (error) {
      record.error = error.message;
      try {
        const remote = await api(endpoint(record));
        if (remote.text === text) {
          record.version = remote.version;
          record.base = remote.text;
          record.dirty = record.text !== record.base;
          record.error = '';
          successful = true;
        } else if (remote.version !== version) record.conflict = remote;
      } catch (_) {
        /* The local cache retains unsynced edits. */
      }
    } finally {
      record.saving = false;
      cache(record);
      render(record);
      if (successful && record.dirty) save(record);
    }
  }
  function bind(field, { projectId, key = 'composer', statusTarget, onChange, defaultText }) {
    const previous = bindings.get(field);
    if (previous) field.removeEventListener('input', previous.listener);
    const id = identity(projectId, key);
    let record = records.get(id);
    if (!record) {
      let prior = null;
      try {
        prior = JSON.parse(localStorage.getItem(id));
      } catch (_) {
        /* Storage can be unavailable. */
      }
      if (
        !prior ||
        typeof prior.text !== 'string' ||
        prior.text.length > 16000 ||
        !Number.isInteger(prior.version) ||
        typeof prior.base !== 'string'
      )
        prior = null;
      record = {
        id,
        projectId,
        key,
        text: prior?.text ?? defaultText ?? field.value,
        defaultText,
        pristineDefault: defaultText !== undefined && !prior?.dirty,
        base: prior?.base ?? '',
        version: prior?.version ?? 0,
        dirty: prior ? prior.dirty : defaultText === undefined && Boolean(field.value),
        loaded: false,
        local: Boolean(prior),
        conflict: null,
      };
      records.set(id, record);
    }
    if (defaultText !== undefined) {
      record.defaultText = defaultText;
      if (!record.dirty && !record.base) {
        record.text = defaultText;
        record.pristineDefault = true;
      }
    }
    const listener = () => changed(record, field.value);
    const binding = { field, record, statusTarget, onChange, listener };
    bindings.set(field, binding);
    field.addEventListener('input', listener);
    assign(record, record.text);
    render(record);
    load(record);
    return {
      detach() {
        if (bindings.get(field) === binding) {
          field.removeEventListener('input', listener);
          bindings.delete(field);
        }
      },
    };
  }
  function clearAccepted(projectId, key, submitted) {
    const record = records.get(identity(projectId, key));
    if (!record || record.text !== submitted) return;
    assign(record, '');
    changed(record, '');
  }
  window.addEventListener('storage', (event) => {
    for (const record of records.values()) if (event.key === record.id) load(record);
  });
  window.SimonProjectDrafts = { bind, clearAccepted };
})();
