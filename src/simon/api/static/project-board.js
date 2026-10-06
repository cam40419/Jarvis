'use strict';
(() => {
  const slot = document.getElementById('pc-board-slot');
  if (!slot) return;
  const node = (tag, text, className) => {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    if (className) result.className = className;
    return result;
  };
  let projectId = null,
    projectDetail = null,
    state = null,
    loading = false,
    generation = 0;
  let configuredConnections = [],
    operation = null,
    operations = [],
    availabilityReasons = [];
  let bindingSnapshot = null,
    availableBoards = [],
    reviewSession = null,
    reconcileSnapshot = null;
  const root = node('section', undefined, 'pb-panel');
  root.setAttribute('aria-labelledby', 'pb-heading');
  const header = node('div', undefined, 'pb-heading');
  const identity = node('div', undefined, 'pb-identity');
  const mark = node('span', 'CU', 'pb-mark');
  mark.setAttribute('aria-hidden', 'true');
  const copy = node('div');
  copy.append(node('p', 'PRIMARY PROJECT BOARD', 'pb-eyebrow'));
  const title = node('h3', 'Your company project board', 'pb-title');
  title.id = 'pb-heading';
  const subtitle = node('p', '', 'pb-subtitle');
  copy.append(title, subtitle);
  identity.append(mark, copy);
  const badge = node('span', 'Loading', 'pb-badge');
  header.append(identity, badge);
  const toolbar = node('div', undefined, 'pb-toolbar');
  const notice = node('div', undefined, 'pb-notice');
  notice.hidden = true;
  const metadata = node('dl', undefined, 'pb-meta');
  metadata.hidden = true;
  const feedback = node('p', '', 'pb-status');
  feedback.setAttribute('role', 'status');
  feedback.setAttribute('aria-live', 'polite');
  root.append(header, toolbar, notice, metadata, feedback);
  slot.append(root);
  const bindingDialog = node('dialog', undefined, 'pb-dialog');
  bindingDialog.id = 'pb-binding-dialog';
  bindingDialog.setAttribute('aria-labelledby', 'pb-binding-heading');
  bindingDialog.innerHTML = /* HTML */ `<div class="pb-dialog-heading">
      <div>
        <h2 id="pb-binding-heading">Connect the project board</h2>
        <p>
          Choose the ClickUp list your company uses to manage this project. Its tasks remain the
          primary project record.
        </p>
      </div>
      <button type="button" class="pb-button pb-dialog-close" aria-label="Close board connection">
        ×
      </button>
    </div>
    <div id="pb-binding-body"></div>
    <p id="pb-binding-feedback" class="pb-status" role="status" aria-live="polite"></p>`;
  document.body.append(bindingDialog);
  const reviewDialog = node('dialog', undefined, 'pb-dialog');
  reviewDialog.id = 'pb-review-dialog';
  reviewDialog.setAttribute('aria-labelledby', 'pb-review-heading');
  reviewDialog.innerHTML = /* HTML */ `<div class="pb-dialog-heading">
      <div>
        <h2 id="pb-review-heading">Review board changes</h2>
        <p id="pb-review-description"></p>
      </div>
      <button type="button" class="pb-button pb-dialog-close" aria-label="Close board review">
        ×
      </button>
    </div>
    <div id="pb-review-body"></div>
    <p id="pb-review-feedback" class="pb-status" role="status" aria-live="polite"></p>
    <div id="pb-review-footer" class="pb-dialog-footer"></div>`;
  document.body.append(reviewDialog);
  const reconcileDialog = node('dialog', undefined, 'pb-dialog');
  reconcileDialog.id = 'pb-reconcile-dialog';
  reconcileDialog.setAttribute('aria-labelledby', 'pb-reconcile-heading');
  reconcileDialog.innerHTML = /* HTML */ `<div class="pb-dialog-heading">
      <div>
        <h2 id="pb-reconcile-heading">Review an uncertain board update</h2>
        <p id="pb-reconcile-description"></p>
      </div>
      <button
        type="button"
        class="pb-button pb-dialog-close"
        aria-label="Close board outcome review"
      >
        ×
      </button>
    </div>
    <form id="pb-reconcile-form">
      <fieldset id="pb-reconcile-fields">
        <label for="pb-reconcile-resolution">Verified outcome</label
        ><select id="pb-reconcile-resolution">
          <option value="attach">The task exists in ClickUp — link it</option>
          <option value="acknowledge">I checked the outcome and recorded what happened</option>
        </select>
        <div id="pb-remote-id-wrap">
          <label for="pb-remote-id">Existing ClickUp task ID</label
          ><input
            id="pb-remote-id"
            maxlength="100"
            pattern="[A-Za-z0-9_-]+"
            placeholder="The ID in the ClickUp task URL"
          />
          <p class="pb-help">The server checks the task belongs to the connected list.</p>
        </div>
        <label for="pb-reconcile-note">What did you verify in ClickUp?</label
        ><textarea id="pb-reconcile-note" required maxlength="2000" rows="4"></textarea>
      </fieldset>
      <p id="pb-reconcile-feedback" class="pb-status" role="status" aria-live="polite"></p>
      <div class="pb-dialog-footer">
        <p>The saved outcome determines whether this project can continue.</p>
        <button id="pb-reconcile-save" type="submit" class="pb-button primary">
          Record verified outcome
        </button>
      </div>
    </form>`;
  document.body.append(reconcileDialog);
  for (const dialog of [bindingDialog, reviewDialog, reconcileDialog])
    dialog.querySelector('.pb-dialog-close').onclick = () => dialog.close();
  const endpoint = (suffix) =>
    '/v1/projects/' + encodeURIComponent(projectId) + '/board' + (suffix || '');
  const message = (text, error = false) => {
    feedback.textContent = text;
    feedback.classList.toggle('error', error);
  };
  const date = (value) =>
    value
      ? new Date(value).toLocaleString([], {
          month: 'short',
          day: 'numeric',
          hour: 'numeric',
          minute: '2-digit',
        })
      : 'Not yet';
  const errorText = (error) =>
    error instanceof SyntaxError
      ? 'The server response could not be read. Refresh the saved board state before retrying.'
      : error.message;
  function button(label, callback, primary = false) {
    const result = node('button', label, 'pb-button' + (primary ? ' primary' : ''));
    result.type = 'button';
    result.onclick = async () => {
      result.disabled = true;
      try {
        await callback();
      } catch (error) {
        message(errorText(error), true);
      } finally {
        if (result.isConnected && result.dataset.locked !== 'true') result.disabled = false;
      }
    };
    return result;
  }
  function safeBoardUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' &&
        url.hostname === 'app.clickup.com' &&
        !url.username &&
        !url.password
        ? url.href
        : null;
    } catch (_) {
      return null;
    }
  }
  function showNotice(reasons, heading = 'Board setup needs attention') {
    notice.hidden = !reasons.length;
    notice.replaceChildren();
    if (!reasons.length) return;
    notice.append(node('strong', heading));
    const list = node('ul');
    for (const reason of reasons) list.append(node('li', reason));
    notice.append(list);
  }
  function render() {
    document
      .querySelectorAll('#pc-panel-content [data-todo-id]')
      .forEach((card) => decorateTask(card, card.dataset.todoId));
    const fingerprint = JSON.stringify([
      projectId,
      state?.version,
      state?.binding,
      state?.board,
      state?.mappings,
      state?.pending_operation_ids,
      state?.blocked_reasons,
      operations,
      availabilityReasons,
      !state && loading,
    ]);
    if (root.dataset.fingerprint === fingerprint) return;
    root.dataset.fingerprint = fingerprint;
    toolbar.replaceChildren();
    metadata.replaceChildren();
    metadata.hidden = true;
    root.querySelector('.pb-pending')?.remove();
    if (!state) {
      badge.textContent = loading ? 'Loading' : 'Unavailable';
      badge.className = 'pb-badge';
      title.textContent = 'Your company project board';
      subtitle.textContent =
        'Connect a shared ClickUp board to keep project tasks and Simon’s execution work together.';
      if (!loading) toolbar.append(button('Retry board connection', refresh));
      return;
    }
    const binding = state.binding;
    const reasons = [...new Set([...(state.blocked_reasons || []), ...availabilityReasons])];
    showNotice(reasons);
    if (!binding) {
      title.textContent = 'Connect your ClickUp project board';
      subtitle.textContent =
        'Use your shared board as the primary task record. Review which tasks Simon imports or publishes.';
      badge.textContent = 'Not connected';
      badge.className = 'pb-badge';
      toolbar.append(button('Connect board', openBinding, true));
      return;
    }
    title.textContent = state.board?.name || 'ClickUp project board';
    subtitle.textContent =
      'ClickUp is the primary project record. Simon tracks the execution tasks below.';
    badge.textContent = reasons.length ? 'Needs attention' : 'Connected';
    badge.className = 'pb-badge ' + (reasons.length ? 'blocked' : 'ready');
    const url = safeBoardUrl(state.board?.url);
    if (url) {
      const link = node('a', 'Open project board', 'pb-button primary');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      toolbar.append(link);
    }
    const importTasks = button('Preview board tasks', () => openPreview('import'));
    const publishTasks = button('Publish execution tasks', () => openPreview('publish'));
    const sync = button('Sync now', synchronize);
    const held = Boolean(state.pending_operation_ids?.length);
    importTasks.disabled = publishTasks.disabled = held || reasons.length > 0;
    sync.disabled = held;
    toolbar.append(
      importTasks,
      publishTasks,
      sync,
      button('Board settings', openBinding),
      button('Refresh board status', refresh),
    );
    metadata.hidden = false;
    const rows = [
      ['Last sync', date(state.last_sync_at)],
      ['Linked tasks', String(state.mappings?.length || 0)],
      ['Progress comments', binding.sync_progress ? 'Enabled' : 'Off'],
      ['Auto-publish', binding.auto_publish ? 'Enabled' : 'Off'],
      ['Status updates', binding.sync_status ? 'Enabled' : 'Off'],
    ];
    if (state.abandoned_create_todo_ids?.length)
      rows.push(['Creation attempts abandoned', String(state.abandoned_create_todo_ids.length)]);
    for (const [label, value] of rows) {
      const row = node('div');
      row.append(node('dt', label), node('dd', value));
      metadata.append(row);
    }
    root.querySelector('.pb-pending')?.remove();
    if (state.pending_operation_ids?.length) {
      const pending = node('div', undefined, 'pb-notice error pb-pending');
      pending.append(node('strong', 'A board update needs a verified outcome'));
      pending.append(
        node(
          'p',
          'Open ClickUp and check the saved task before continuing. Uncertain writes are held for review.',
        ),
      );
      for (const item of operations.filter((item) =>
        state.pending_operation_ids.includes(item.id),
      )) {
        const task = projectDetail?.state?.todos?.find((todo) => todo.id === item.todo_id);
        const row = node('div', undefined, 'pb-toolbar');
        row.append(
          node('span', (task?.title || 'Project task') + ' · ' + item.kind + ' · ' + item.state),
        );
        if (item.state === 'unknown' || item.state === 'failed')
          row.append(button('Review outcome', () => openReconcile(item)));
        pending.append(row);
      }
      root.append(pending);
    }
  }
  async function refresh() {
    if (!projectId || loading || operation) return;
    const requestedProject = projectId,
      request = ++generation;
    loading = true;
    if (!state) render();
    try {
      const current = await api(endpoint());
      if (request !== generation || requestedProject !== projectId) return;
      state = current.state || current;
      operations = current.operations || [];
      availabilityReasons = current.blocked_reasons || [];
      render();
      message('Board connection status updated.');
    } catch (error) {
      if (request !== generation || requestedProject !== projectId) return;
      root.dataset.fingerprint = '';
      message(errorText(error), true);
      showNotice([
        'The board connection could not be loaded. Your execution tasks remain available.',
      ]);
    } finally {
      if (request === generation) {
        loading = false;
        if (!state) render();
      }
    }
  }
  async function openBinding() {
    // The form is populated only from operator-authorized connections and lists.
    bindingSnapshot = {
      projectId,
      version: state.version,
      binding: state.binding,
      statusDraft: { ...(state.binding?.status_map || {}) },
    };
    bindingDialog.showModal();
    const target = document.getElementById('pb-binding-body');
    target.replaceChildren(node('p', 'Loading available connections…'));
    const status = document.getElementById('pb-binding-feedback');
    status.textContent = '';
    try {
      const result = await api('/v1/project-boards/connections');
      configuredConnections = result.connections || result;
      renderConnections(target);
    } catch (error) {
      target.replaceChildren();
      status.textContent = errorText(error);
      status.classList.add('error');
    }
  }
  function renderConnections(target) {
    target.replaceChildren();
    if (!configuredConnections.length) {
      const empty = node('div', undefined, 'pb-empty');
      empty.append(
        node('h3', 'A ClickUp connection is needed'),
        node(
          'p',
          'Connect your ClickUp account in Connections. Simon discovers the Workspaces and Lists your account can access.',
        ),
      );
      empty.append(
        button(
          'Connect ClickUp account',
          () => {
            bindingDialog.close();
            document.getElementById('connections-open').click();
            document.getElementById('integration-credential').focus();
          },
          true,
        ),
      );
      target.append(empty);
      return;
    }
    target.innerHTML = /* HTML */ `<form id="pb-binding-form">
      <fieldset id="pb-binding-fields">
        <label for="pb-connection">Company connection</label
        ><select id="pb-connection" required></select>
        <p id="pb-connection-reasons" class="pb-help"></p>
        <label for="pb-list">Project board / ClickUp list</label
        ><select id="pb-list" required>
          <option value="">Choose a connection first</option>
        </select>
        <p class="pb-help">Choose from the Lists accessible to your connected account.</p>
        <div class="pb-permissions">
          <label class="pb-check"
            ><input id="pb-sync-progress" type="checkbox" checked /><span
              >Post execution progress and results to ClickUp<small
                >Simon adds comments to linked tasks as work progresses.</small
              ></span
            ></label
          ><label class="pb-check"
            ><input id="pb-auto-publish" type="checkbox" /><span
              >Automatically publish new execution tasks<small
                >Simon may create tasks in this list for future plans.</small
              ></span
            ></label
          ><label class="pb-check"
            ><input id="pb-sync-status" type="checkbox" /><span
              >Update ClickUp statuses from execution progress<small
                >Choose the exact status mapping below. Conflicting edits require review.</small
              ></span
            ></label
          >
        </div>
        <div id="pb-status-mapping" hidden></div>
      </fieldset>
      <div class="pb-dialog-footer">
        <p>
          Saving authorizes only the synchronization options selected here. Imported tasks still
          follow the project's execution policy.
        </p>
        <button id="pb-binding-save" type="submit" class="pb-button primary" disabled>
          Save board connection
        </button>
      </div>
    </form>`;
    const select = document.getElementById('pb-connection');
    select.append(new Option('Choose a company connection', ''));
    for (const connection of configuredConnections)
      select.append(
        new Option(
          connection.name + (connection.available ? '' : ' — setup needed'),
          connection.id,
        ),
      );
    const binding = bindingSnapshot.binding;
    if (binding) {
      select.value = binding.connection_id;
      document.getElementById('pb-sync-progress').checked = binding.sync_progress;
      document.getElementById('pb-auto-publish').checked = binding.auto_publish;
      document.getElementById('pb-sync-status').checked = binding.sync_status;
    }
    select.onchange = () => loadBoards(select.value);
    document.getElementById('pb-list').onchange = renderStatusMapping;
    document.getElementById('pb-sync-status').onchange = renderStatusMapping;
    document.getElementById('pb-binding-form').onsubmit = saveBinding;
    if (select.value) loadBoards(select.value);
  }
  async function loadBoards(connectionId) {
    const select = document.getElementById('pb-list'),
      save = document.getElementById('pb-binding-save');
    const connection = configuredConnections.find((item) => item.id === connectionId);
    const reasons = document.getElementById('pb-connection-reasons');
    availableBoards = [];
    save.disabled = true;
    select.disabled = true;
    select.replaceChildren(new Option('Loading authorized lists…', ''));
    renderStatusMapping();
    reasons.textContent = (connection?.blocked_reasons || []).join(' ');
    if (!connection?.available) {
      select.replaceChildren(
        new Option(connection ? 'Connection setup is required' : 'Choose a company connection', ''),
      );
      return;
    }
    try {
      const result = await api(
        '/v1/project-boards/connections/' + encodeURIComponent(connectionId) + '/boards',
      );
      if (document.getElementById('pb-connection').value !== connectionId || !bindingDialog.open)
        return;
      availableBoards = result.boards || result.items || result;
      if (!availableBoards.length)
        reasons.textContent =
          'ClickUp returned no accessible Lists for this account. Check the token belongs to the ClickUp account that has your boards, or share those Lists with that account.';
      select.replaceChildren(
        new Option(
          availableBoards.length ? 'Choose the project list' : 'No authorized lists are available',
          '',
        ),
      );
      for (const board of availableBoards) {
        const option = new Option(board.name + (board.archived ? ' — archived' : ''), board.id);
        option.disabled = board.archived;
        select.append(option);
      }
      if (bindingSnapshot.binding?.connection_id === connectionId)
        select.value = bindingSnapshot.binding.list_id;
      select.disabled = !availableBoards.length;
      renderStatusMapping();
    } catch (error) {
      select.replaceChildren(new Option('Lists could not be loaded', ''));
      reasons.textContent = errorText(error) + ' Choose the connection again to retry.';
    }
  }
  function renderStatusMapping() {
    const target = document.getElementById('pb-status-mapping');
    if (!target) return;
    const selectedBoard = availableBoards.find(
      (board) => board.id === document.getElementById('pb-list').value,
    );
    const enabled = document.getElementById('pb-sync-status').checked;
    Object.assign(
      bindingSnapshot.statusDraft,
      Object.fromEntries(
        [...target.querySelectorAll('select')].map((field) => [field.dataset.state, field.value]),
      ),
    );
    target.hidden = !enabled;
    target.replaceChildren();
    document.getElementById('pb-binding-save').disabled = !selectedBoard || selectedBoard.archived;
    if (!enabled || !selectedBoard) return;
    target.append(node('h3', 'Execution status → ClickUp status'));
    for (const [key, label] of [
      ['ready', 'Ready to run'],
      ['running', 'In progress'],
      ['done', 'Completed'],
      ['blocked', 'Blocked'],
    ]) {
      const field = node('select');
      field.id = 'pb-map-' + key;
      field.dataset.state = key;
      field.required = true;
      field.append(new Option('Choose a ClickUp status', ''));
      for (const item of selectedBoard.statuses) field.append(new Option(item.status, item.status));
      const selected = bindingSnapshot.statusDraft[key];
      if (selected && selectedBoard.statuses.some((item) => item.status === selected))
        field.value = selected;
      const title = node('label', label);
      title.htmlFor = field.id;
      target.append(title, field);
    }
  }
  async function saveBinding(event) {
    event.preventDefault();
    const fields = document.getElementById('pb-binding-fields'),
      save = document.getElementById('pb-binding-save'),
      status = document.getElementById('pb-binding-feedback');
    save.disabled = true;
    fields.disabled = true;
    status.textContent = 'Saving board connection…';
    status.classList.remove('error');
    try {
      const syncStatus = document.getElementById('pb-sync-status').checked;
      const body = {
        expected_version: bindingSnapshot.version,
        binding: {
          connection_id: document.getElementById('pb-connection').value,
          list_id: document.getElementById('pb-list').value,
          status_map: syncStatus
            ? Object.fromEntries(
                [...document.querySelectorAll('#pb-status-mapping select')].map((field) => [
                  field.dataset.state,
                  field.value,
                ]),
              )
            : {},
          auto_publish: document.getElementById('pb-auto-publish').checked,
          sync_status: syncStatus,
          sync_progress: document.getElementById('pb-sync-progress').checked,
        },
      };
      await api(
        '/v1/projects/' + encodeURIComponent(bindingSnapshot.projectId) + '/board',
        body,
        'PATCH',
      );
      bindingDialog.close();
      await refresh();
      message(
        'Project board connected. Review board tasks or publish selected execution tasks below.',
      );
    } catch (error) {
      status.textContent = errorText(error);
      status.classList.add('error');
    } finally {
      save.disabled = false;
      fields.disabled = false;
    }
  }
  async function synchronize() {
    if (operation) return;
    operation = 'sync';
    const requestProject = projectId,
      path = endpoint('/sync'),
      version = state.version;
    message('Checking linked tasks and the synchronization options you enabled…');
    try {
      await api(path, { expected_version: version });
      message('Synchronization finished. Check task links and any conflicts below.');
    } finally {
      operation = null;
      if (requestProject === projectId) {
        await refresh();
        await window.SimonProjectCommand?.refresh(true);
      }
    }
  }
  async function openPreview(direction) {
    if (!state?.binding || operation) return;
    reviewSession = {
      projectId,
      direction,
      version: state.version,
      key: crypto.randomUUID(),
      items: [],
      selected: new Set(),
      page: 0,
      nextPage: null,
    };
    document.getElementById('pb-review-heading').textContent =
      direction === 'import'
        ? 'Choose board tasks for Simon'
        : 'Publish execution tasks to ClickUp';
    document.getElementById('pb-review-description').textContent =
      direction === 'import'
        ? 'Read the authorized ClickUp list and select up to 25 tasks for this project’s execution backlog. Importing saves the tasks; your project policy controls when work starts.'
        : 'Select up to 25 execution tasks to create in the connected ClickUp list. Existing task links are preserved. Review dependencies before publishing.';
    document.getElementById('pb-review-feedback').textContent = '';
    document.getElementById('pb-review-footer').replaceChildren();
    document
      .getElementById('pb-review-body')
      .replaceChildren(node('p', 'Preparing the task preview…'));
    reviewDialog.showModal();
    if (direction === 'import') await loadPreviewPage(0);
    else {
      const mapped = new Set((state.mappings || []).map((item) => item.todo_id));
      reviewSession.items = (projectDetail?.state?.todos || []).filter(
        (item) => !mapped.has(item.id) && !['cancelled', 'archived'].includes(item.status),
      );
      renderPreview();
    }
  }
  async function loadPreviewPage(page) {
    const current = reviewSession;
    try {
      const result = await api(
        '/v1/projects/' + encodeURIComponent(current.projectId) + '/board/preview?page=' + page,
      );
      if (reviewSession !== current || !reviewDialog.open) return;
      current.items = result.tasks;
      current.page = result.page;
      current.nextPage = result.next_page;
      renderPreview();
    } catch (error) {
      if (reviewSession !== current) return;
      document.getElementById('pb-review-body').replaceChildren(
        node('p', 'The board task preview could not be loaded.'),
        button('Retry preview', () => loadPreviewPage(page)),
      );
      const status = document.getElementById('pb-review-feedback');
      status.textContent = errorText(error);
      status.classList.add('error');
    }
  }
  function renderPreview() {
    const current = reviewSession,
      target = document.getElementById('pb-review-body'),
      footer = document.getElementById('pb-review-footer');
    target.replaceChildren();
    footer.replaceChildren();
    const list = node('div', undefined, 'pb-preview-list');
    const linked = new Set(
      (state.mappings || []).map((item) =>
        current.direction === 'import' ? item.remote_id : item.todo_id,
      ),
    );
    for (const item of current.items) {
      const card = node('article', undefined, 'pb-preview-task');
      const label = node('label', undefined, 'pb-check');
      const check = node('input');
      check.type = 'checkbox';
      check.value = item.id;
      check.checked = current.selected.has(item.id);
      check.disabled = Boolean(
        item.archived || (linked.has(item.id) && current.direction === 'publish'),
      );
      const copy = node('span');
      copy.append(node('strong', item.name || item.title));
      copy.append(
        node(
          'small',
          item.status +
            (linked.has(item.id)
              ? ' · Already linked; import checks for updates'
              : current.direction === 'import'
                ? ' · Import from ClickUp'
                : ' · Create in ClickUp'),
        ),
      );
      label.append(check, copy);
      card.append(label);
      if (current.direction === 'publish' && state.abandoned_create_todo_ids?.includes(item.id))
        card.append(
          node(
            'p',
            'The earlier creation attempt was abandoned after review. Publishing now explicitly authorizes a new task in ClickUp.',
          ),
        );
      const description = item.description || item.objective;
      if (description) {
        const details = node('details');
        details.append(
          node('summary', 'Task details'),
          node('p', description.length > 4000 ? description.slice(0, 4000) + '…' : description),
        );
        card.append(details);
      }
      const url = safeBoardUrl(item.url);
      if (url) {
        const link = node('a', 'Read task in ClickUp', 'pb-button subtle');
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        card.append(link);
      }
      const dependencies = item.dependencies || item.depends_on || [];
      if (dependencies.length)
        card.append(
          node(
            'p',
            dependencies.length +
              ' prerequisite' +
              (dependencies.length === 1 ? '' : 's') +
              '. Include required unlinked tasks in this selection.',
          ),
        );
      check.onchange = () => {
        if (check.checked && current.selected.size >= 25) {
          check.checked = false;
          document.getElementById('pb-review-feedback').textContent =
            'Select at most 25 tasks for one operation.';
          return;
        }
        if (check.checked) current.selected.add(item.id);
        else current.selected.delete(item.id);
        updateSelection();
      };
      list.append(card);
    }
    if (!current.items.length)
      list.append(
        node(
          'div',
          current.direction === 'import'
            ? 'No tasks on this board page.'
            : 'All eligible execution tasks are already linked, or there are no tasks to publish.',
          'pb-empty',
        ),
      );
    target.append(list);
    if (current.direction === 'import') {
      const navigation = node('div', undefined, 'pb-toolbar');
      const previous = button('Previous board page', () => loadPreviewPage(current.page - 1));
      previous.disabled = current.page === 0;
      const next = button('Next board page', () => loadPreviewPage(current.nextPage));
      next.disabled = current.nextPage == null;
      navigation.append(previous, node('span', 'Page ' + (current.page + 1)), next);
      target.append(navigation);
    }
    const summary = node('p');
    summary.id = 'pb-selection-summary';
    const confirm = button(
      current.direction === 'import' ? 'Import selected tasks' : 'Publish selected tasks',
      commitPreview,
      true,
    );
    confirm.id = 'pb-confirm-selection';
    footer.append(summary, confirm);
    updateSelection();
  }
  function updateSelection() {
    const count = reviewSession.selected.size;
    document.getElementById('pb-selection-summary').textContent =
      count +
      ' task' +
      (count === 1 ? '' : 's') +
      ' selected' +
      (reviewSession.direction === 'publish'
        ? ' for creation in ClickUp.'
        : ' for the execution backlog.');
    document.getElementById('pb-confirm-selection').disabled = !count;
  }
  async function commitPreview() {
    const current = reviewSession;
    if (!current.selected.size || operation) return;
    operation = current.direction;
    const status = document.getElementById('pb-review-feedback');
    status.classList.remove('error');
    status.textContent =
      current.direction === 'import' ? 'Importing selected tasks…' : 'Publishing selected tasks…';
    const controls = [...reviewDialog.querySelectorAll('input,button')].map((item) => [
      item,
      item.disabled,
    ]);
    controls.forEach(([item]) => {
      item.disabled = true;
    });
    try {
      const identifiers = [...current.selected],
        content = JSON.stringify(identifiers);
      if (current.content && current.content !== content) current.key = crypto.randomUUID();
      current.content = content;
      const body = {
        expected_version: current.version,
        idempotency_key: current.key,
        [current.direction === 'import' ? 'task_ids' : 'todo_ids']: identifiers,
      };
      await api(
        '/v1/projects/' + encodeURIComponent(current.projectId) + '/board/' + current.direction,
        body,
      );
      reviewDialog.close();
      message(
        current.direction === 'import'
          ? 'Board tasks imported into the execution backlog.'
          : 'Selected tasks published to ClickUp.',
      );
    } catch (error) {
      status.textContent =
        errorText(error) + ' Refresh the board status before retrying an uncertain update.';
      status.classList.add('error');
    } finally {
      operation = null;
      controls.forEach(([item, disabled]) => {
        if (item.isConnected) item.disabled = disabled;
      });
      if (projectId === current.projectId) {
        await refresh();
        await window.SimonProjectCommand?.refresh(true);
      }
      if (reviewDialog.open && state?.pending_operation_ids?.length) {
        const confirm = document.getElementById('pb-confirm-selection');
        confirm.disabled = true;
        confirm.dataset.locked = 'true';
        status.textContent =
          'The board update needs an outcome review. Close this preview and use Review outcome before continuing.';
        status.classList.add('error');
      }
    }
  }
  function openReconcile(item) {
    reconcileSnapshot = { projectId, version: state.version, operation: item };
    const task = projectDetail?.state?.todos?.find((todo) => todo.id === item.todo_id);
    document.getElementById('pb-reconcile-description').textContent =
      (task?.title || 'Project task') +
      ': check the actual ' +
      item.kind +
      ' outcome in ClickUp. Record what you verified before allowing more board writes.';
    const field = document.getElementById('pb-reconcile-resolution');
    field.querySelector('option[value="attach"]').disabled = item.kind !== 'create';
    field.querySelector('option[value="acknowledge"]').textContent =
      item.kind === 'create' && item.state === 'unknown'
        ? 'No task exists — abandon this creation attempt'
        : item.kind === 'create'
          ? 'Acknowledge the rejected creation attempt'
          : 'I checked the outcome and recorded what happened';
    field.value = item.kind === 'create' ? 'attach' : 'acknowledge';
    if (item.kind === 'create' && item.state === 'unknown')
      document
        .getElementById('pb-reconcile-description')
        .append(
          ' Link the existing task if it was created. Abandon only after verifying that no task exists. Abandoned creation attempts are never retried automatically; a later creation requires a new, explicit Publish.',
        );
    reconcileDialog.querySelector('.pb-operation-reference')?.remove();
    if (item.kind === 'create' && item.marker) {
      const reference = node('details', undefined, 'pb-details pb-operation-reference');
      reference.append(
        node('summary', 'Task verification reference'),
        node('p', 'Match this reference in the ClickUp task description before linking it:'),
        node('p', '[Simon reference: ' + item.marker + ']'),
      );
      document.getElementById('pb-reconcile-fields').before(reference);
    }
    document.getElementById('pb-remote-id').value = item.remote_id || '';
    document.getElementById('pb-reconcile-note').value = '';
    document.getElementById('pb-reconcile-feedback').textContent = '';
    updateReconcile();
    reconcileDialog.showModal();
  }
  function updateReconcile() {
    const attach = document.getElementById('pb-reconcile-resolution').value === 'attach';
    document.getElementById('pb-remote-id-wrap').hidden = !attach;
    document.getElementById('pb-remote-id').required = attach;
  }
  document.getElementById('pb-reconcile-resolution').onchange = updateReconcile;
  document.getElementById('pb-reconcile-form').onsubmit = async (event) => {
    event.preventDefault();
    const current = reconcileSnapshot,
      status = document.getElementById('pb-reconcile-feedback');
    const save = document.getElementById('pb-reconcile-save');
    save.disabled = true;
    document.getElementById('pb-reconcile-fields').disabled = true;
    try {
      const body = {
        expected_version: current.version,
        operation_id: current.operation.id,
        resolution: document.getElementById('pb-reconcile-resolution').value,
        note: document.getElementById('pb-reconcile-note').value.trim(),
        remote_id:
          document.getElementById('pb-reconcile-resolution').value === 'attach'
            ? document.getElementById('pb-remote-id').value.trim()
            : null,
      };
      await api('/v1/projects/' + encodeURIComponent(current.projectId) + '/board/reconcile', body);
      reconcileDialog.close();
      await refresh();
      message('The verified board outcome has been saved.');
      title.tabIndex = -1;
      title.focus({ preventScroll: true });
    } catch (error) {
      status.textContent = errorText(error);
      status.classList.add('error');
    } finally {
      save.disabled = false;
      document.getElementById('pb-reconcile-fields').disabled = false;
    }
  };
  function selectProject(event) {
    const current = event.detail;
    if (!current?.projectId) return;
    if (current.projectId !== projectId) {
      projectId = current.projectId;
      state = null;
      ++generation;
      loading = false;
      operation = null;
      operations = [];
      availabilityReasons = [];
      message('');
      showNotice([]);
    }
    projectDetail = current.detail;
    refresh();
  }
  function decorateTask(card, todoId) {
    const mapping = state?.mappings?.find((item) => item.todo_id === todoId),
      url = safeBoardUrl(mapping?.url);
    if (!url || card.querySelector('[data-board-task-link]')) return;
    const edit = card.querySelector('[data-focus-key]');
    if (edit) edit.hidden = true;
    const actions =
      card.querySelector('.pc-task-actions') || node('div', undefined, 'pc-task-actions');
    const link = node('a', 'Edit task in ClickUp', 'pc-button');
    link.dataset.boardTaskLink = 'true';
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    actions.append(link);
    if (!actions.parentElement) card.append(actions);
    card.querySelector('.pc-task-meta')?.append(node('span', 'Managed in ClickUp'));
  }
  window.addEventListener('simon-project-command-update', selectProject);
  window.addEventListener('simon-connections-change', () => {
    if (projectId) refresh();
  });
  const initial = window.SimonProjectCommand?.getSnapshot();
  if (initial) selectProject({ detail: initial });
  window.SimonProjectBoard = { refresh, decorateTask };
})();
