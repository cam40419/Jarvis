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
    '<div class="pc-section-heading"><div><h3>Deliverable files</h3><p>Documents, spreadsheets, and other files created for this project. Conversation answers stay in Overview and Run history.</p></div><button id="po-refresh" type="button" class="pc-button">Refresh outputs</button></div><button id="po-history" type="button" class="pc-button pc-text-button po-history">Read answers in Run history</button><p id="po-status" role="status" aria-live="polite"></p><div id="po-list"></div><button id="po-more" type="button" class="pc-button" hidden>Load earlier outputs</button>';
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
    const params = new URLSearchParams({ limit: '20', kind: 'deliverable' });
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
            ? 'You can download outputs. Creating an editable copy requires project write access.'
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
    const files = (page?.items || []).filter(
      (item) => item.kind !== 'response' || Boolean(item.document_url && item.document_name),
    );
    for (const item of files) {
      const namedReport = Boolean(item.document_url && item.document_name);
      const wordDocument =
        item.media_type ===
          'application/vnd.openxmlformats-officedocument.wordprocessingml.document' ||
        /\.docx$/i.test(item.name);
      const row = node('article', undefined, 'po-output');
      row.dataset.output = item.id;
      const top = node('div', undefined, 'pc-resource-row');
      const copy = node('div');
      copy.append(
        node('strong', namedReport ? item.document_name : item.title || item.name),
        node(
          'small',
          [
            wordDocument || namedReport ? 'Word document' : null,
            !namedReport && item.title && item.title !== item.name ? item.name : null,
            item.task_id?.replaceAll('_', ' ').replaceAll('-', ' '),
            date(item.created_at),
            namedReport && !wordDocument ? null : size(item.size),
          ]
            .filter(Boolean)
            .join(' · '),
        ),
      );
      const outputState = item.status || 'accepted';
      const statusLabel = {
        accepted: 'Saved file',
        draft: 'Draft file - awaiting review',
        partial: 'Partial file - needs attention',
      };
      copy.append(
        node('span', statusLabel[outputState] || 'Saved file', 'po-output-status ' + outputState),
      );
      const controls = node('div', undefined, 'pc-resource-actions');
      if (item.document_url && !wordDocument) {
        const document = download(
          'Download Word document',
          item.document_url,
          item.document_name || item.name.replace(/\.[^.]+$/, '') + '.docx',
        );
        document.classList.add('po-document-download');
        controls.append(document);
      }
      controls.append(
        download(
          wordDocument
            ? 'Download Word document'
            : namedReport
              ? 'Download source'
              : 'Download original',
          item.download_url,
          item.name,
        ),
      );
      if (!item.project_copy && canWrite() && page.can_promote !== false)
        controls.append(saveButton(projectId, item.run_id, item));
      top.append(copy, controls);
      row.append(top);
      if (
        !wordDocument &&
        window.SimonResults &&
        (item.media_type?.startsWith('text/') ||
          [
            'application/json',
            'application/xml',
            'image/png',
            'image/jpeg',
            'image/webp',
            'image/gif',
          ].includes(item.media_type))
      ) {
        const preview = window.SimonResults.artifactCard(item.run_id, item, {
          hideName: true,
          hideDownload: true,
        });
        const control = preview.querySelector('.sr-file-actions button');
        if (control) controls.prepend(control);
        preview.querySelector('.sr-file-row').remove();
        row.append(preview);
      }
      if (item.project_copy) {
        const savedCopy = node('div', undefined, 'po-saved-copy');
        savedCopy.append(
          node('strong', 'Editable local copy'),
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
    if (page && !files.length)
      target.append(
        node(
          'p',
          page.next_cursor
            ? 'No deliverable files on this page. Load earlier outputs to continue.'
            : 'No deliverable files yet. Requested documents and other created files will appear here.',
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
      saved.has(key)
        ? 'Editable copy created'
        : pending.has(key)
          ? 'Saving…'
          : 'Create editable copy',
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
          button.textContent = 'Editable copy created';
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
          button.textContent = 'Create editable copy';
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
              control.textContent = saved.has(key)
                ? 'Editable copy created'
                : 'Create editable copy';
            }
        }
      },
      'pc-text-button',
    );
    button.setAttribute('aria-label', 'Create editable copy: ' + artifact.name);
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
  local('po-history').onclick = () => window.SimonProjectCommand?.navigate('history');
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
