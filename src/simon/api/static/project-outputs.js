'use strict';
(() => {
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const canWrite = () => typeof session !== 'undefined' && session?.scopes?.includes('jobs:write');
  const date = (value) =>
    value
      ? new Date(value).toLocaleString([], {
          month: 'short',
          day: 'numeric',
          hour: 'numeric',
          minute: '2-digit',
        })
      : '';
  const size = (bytes) =>
    bytes < 1024
      ? bytes + ' B'
      : bytes < 1048576
        ? Math.ceil(bytes / 1024) + ' KB'
        : (bytes / 1048576).toFixed(1) + ' MB';
  let projectId = null,
    epoch = 0,
    page = null,
    loading = false,
    error = '',
    request = 0;
  const keys = new Map(),
    pending = new Map(),
    saved = new Map();
  const panel = node('section', undefined, 'pc-file-section po-panel');
  panel.id = 'project-outputs';
  panel.innerHTML =
    '<div class="pc-section-heading"><div><h3>Generated outputs</h3><p>Files from every project run. Save a local project copy to use in later work.</p></div><button id="po-refresh" type="button" class="pc-button">Refresh outputs</button></div><p id="po-status" role="status" aria-live="polite"></p><div id="po-list"></div><button id="po-more" type="button" class="pc-button" hidden>Load earlier outputs</button>';
  const local = (id) => panel.querySelector('#' + id);
  const outputKey = (id, runId, artifactId) => id + ':' + runId + ':' + artifactId;
  function select(id) {
    if (id === projectId) return;
    projectId = id;
    ++epoch;
    ++request;
    page = null;
    error = '';
    loading = false;
    render();
  }
  function action(label, callback, className = '') {
    const item = node('button', label, 'pc-button ' + className);
    item.type = 'button';
    item.onclick = async () => {
      item.disabled = true;
      try {
        await callback();
      } catch (failure) {
        error = failure.message;
        render();
      } finally {
        if (item.isConnected && item.dataset.saved !== 'true') item.disabled = false;
      }
    };
    return item;
  }
  function download(label, url, name) {
    const link = node('a', label, 'pc-file-link');
    try {
      const value = new URL(url, location.origin);
      if (
        value.origin !== location.origin ||
        !(value.pathname.startsWith('/v1/') || value.pathname.startsWith(appPath('/v1/')))
      )
        return node('span', 'Download unavailable');
      link.href = value.pathname.startsWith('/v1/')
        ? appPath(value.pathname + value.search)
        : value.pathname + value.search;
    } catch (_) {
      return node('span', 'Download unavailable');
    }
    link.download = name;
    return link;
  }
  async function load(cursor = null) {
    if (!projectId) return;
    const current = epoch,
      token = ++request,
      previous = page;
    const params = new URLSearchParams({ limit: '20' });
    if (cursor) params.set('cursor', cursor);
    loading = true;
    error = '';
    render();
    try {
      const result = await api(
        '/v1/projects/' + encodeURIComponent(projectId) + '/outputs?' + params,
      );
      if (current !== epoch || token !== request) return;
      page = cursor ? { ...result, items: [...(previous?.items || []), ...result.items] } : result;
      for (const item of result.items)
        if (item.project_copy) saved.set(outputKey(projectId, item.run_id, item.id), item);
    } catch (failure) {
      if (current === epoch && token === request) error = failure.message;
    } finally {
      if (current === epoch && token === request) {
        loading = false;
        render();
      }
    }
  }
  function render() {
    local('po-status').textContent =
      error ||
      (loading
        ? 'Loading saved outputs…'
        : page?.promotion_blocked_reason ||
          (!canWrite()
            ? 'You can download outputs. Saving a project copy requires project write access.'
            : ''));
    local('po-status').classList.toggle('error', Boolean(error));
    local('po-more').hidden = !page?.next_cursor;
    local('po-more').disabled = loading;
    local('po-refresh').disabled = loading;
    const target = local('po-list');
    const fingerprint = JSON.stringify([projectId, page, canWrite()]);
    if (target.dataset.fingerprint === fingerprint) return;
    target.dataset.fingerprint = fingerprint;
    target.replaceChildren();
    for (const item of page?.items || []) {
      const row = node('article', undefined, 'po-output');
      row.dataset.output = item.id;
      const top = node('div', undefined, 'pc-resource-row');
      const copy = node('div');
      copy.append(
        node('strong', item.name),
        node(
          'small',
          [
            item.task_id?.replaceAll('_', ' ').replaceAll('-', ' '),
            date(item.created_at),
            size(item.size),
          ]
            .filter(Boolean)
            .join(' · '),
        ),
      );
      const controls = node('div', undefined, 'pc-resource-actions');
      controls.append(download('Download original', item.download_url, item.name));
      if (!item.project_copy && canWrite() && page.can_promote !== false)
        controls.append(saveButton(projectId, item.run_id, item));
      top.append(copy, controls);
      row.append(top);
      if (item.project_copy) {
        const savedCopy = node('div', undefined, 'po-saved-copy');
        savedCopy.append(
          node('strong', 'Saved project copy'),
          node('span', item.project_copy.path),
          node(
            'small',
            'Saved ' +
              date(item.project_copy.saved_at) +
              '. Later edits are managed in local files.',
          ),
        );
        const actions = node('div', undefined, 'pc-resource-actions');
        actions.append(
          download('Download project copy', item.project_copy.download_url, item.name),
        );
        if (canWrite())
          actions.append(
            action(
              'Use in next task',
              () => window.SimonProjectCommand.reuseFile(projectId, item.project_copy.path),
              'pc-text-button',
            ),
          );
        actions.append(
          action(
            'Open local folder',
            () =>
              window.SimonLocalFiles.open(
                item.project_copy.root,
                item.project_copy.path.includes('/')
                  ? item.project_copy.path.slice(0, item.project_copy.path.lastIndexOf('/'))
                  : '',
              ),
            'pc-text-button',
          ),
        );
        savedCopy.append(actions);
        row.append(savedCopy);
      }
      target.append(row);
    }
    if (page && !page.items.length)
      target.append(
        node(
          'p',
          page.next_cursor
            ? 'No accessible outputs on this page. Load earlier outputs to continue.'
            : 'No generated files yet. Completed task outputs will appear here.',
          'pc-section-empty',
        ),
      );
  }
  function saveButton(id, runId, artifact) {
    const key = outputKey(id, runId, artifact.id),
      wrap = node('span', undefined, 'po-save-action');
    wrap.dataset.outputKey = key;
    const feedback = node('span', undefined, 'po-action-status');
    feedback.setAttribute('role', 'status');
    const button = action(
      saved.has(key) ? 'Saved to project' : pending.has(key) ? 'Saving…' : 'Save to project',
      async () => {
        feedback.textContent = '';
        button.textContent = 'Saving…';
        try {
          if (!keys.has(key)) keys.set(key, crypto.randomUUID());
          let operation = pending.get(key);
          if (!operation) {
            operation = api(
              '/v1/projects/' +
                encodeURIComponent(id) +
                '/outputs/' +
                encodeURIComponent(runId) +
                '/' +
                encodeURIComponent(artifact.id) +
                '/promote',
              { idempotency_key: keys.get(key) },
            );
            pending.set(key, operation);
          }
          const item = await operation;
          saved.set(key, item);
          button.textContent = 'Saved to project';
          button.dataset.saved = 'true';
          if (id === projectId && page) {
            page = {
              ...page,
              items: page.items.map((previous) =>
                previous.id === item.id && previous.run_id === item.run_id ? item : previous,
              ),
            };
            render();
          }
          window.dispatchEvent(
            new CustomEvent('simon-project-output-saved', {
              detail: { projectId: id, output: item },
            }),
          );
        } catch (failure) {
          button.textContent = 'Save to project';
          feedback.textContent = /destination already exists|file changed/i.test(failure.message)
            ? 'A different file already exists here. Existing files are kept. Open local files to review it before saving again.'
            : failure.message + ' Existing files are kept. Check local files before trying again.';
        } finally {
          pending.delete(key);
          for (const element of document.querySelectorAll('.po-save-action'))
            if (element.dataset.outputKey === key) {
              const control = element.querySelector('button');
              control.disabled = saved.has(key);
              control.dataset.saved = String(saved.has(key));
              control.textContent = saved.has(key) ? 'Saved to project' : 'Save to project';
            }
        }
      },
      'pc-text-button',
    );
    button.setAttribute('aria-label', 'Save to project: ' + artifact.name);
    button.disabled = saved.has(key) || pending.has(key) || !canWrite();
    button.dataset.saved = String(saved.has(key));
    wrap.append(button, feedback);
    return wrap;
  }
  function mount(target, id) {
    select(id);
    if (panel.parentElement !== target) target.replaceChildren(panel);
    if (!page && !loading) load();
  }
  local('po-refresh').onclick = () => load();
  local('po-more').onclick = () => load(page?.next_cursor);
  window.addEventListener('simon-project-changing', (event) => {
    projectId = null;
    select(event.detail.projectId);
  });
  window.SimonProjectOutputs = {
    mount,
    saveButton,
    refresh: () => (loading ? Promise.resolve() : load()),
  };
})();
