'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const node = (tag, text, className) => {
    const value = document.createElement(tag);
    if (text !== undefined) value.textContent = text;
    if (className) value.className = className;
    return value;
  };
  const endStates = new Set(['succeeded', 'failed', 'cancelled', 'expired', 'needs_human']);
  const labels = {planned: 'Ready to start', blocked: 'Needs setup', queued: 'Queued', running: 'Running', succeeded: 'Complete', failed: 'Failed', cancelled: 'Cancelled', needs_human: 'Needs attention', unknown: 'Needs attention', waiting: 'Waiting'};
  const badge = status => node('span', labels[status] || status, 'agent-badge agent-' + status);
  const short = value => value?.slice(0, 8) || '';
  const money = value => value == null ? 'Not reported' : '$' + Number(value).toFixed(4);
  const size = value => value < 1024 ? value + ' B' : value < 1048576 ? (value / 1024).toFixed(1) + ' KB' : (value / 1048576).toFixed(1) + ' MB';
  const apiRoot = '/v1/agent-platform';
  let catalog = null, plans = [], runs = [], activeTab = 'runs', selected = null;
  let timer = null, refreshing = false, saving = false, taskSequence = 0, planSubmission = null;
  let revision = 0;
  const toolView = {query: '', state: 'all', page: 1};
  const runKeys = new Map();
  const dashboard = node('section', undefined, 'agent-work');
  dashboard.id = 'agent-work'; dashboard.setAttribute('aria-labelledby', 'agent-heading');
  dashboard.innerHTML = `
    <div class="agent-heading"><div><h2 id="agent-heading">Agents &amp; work</h2><p class="muted">Manage reusable agents, review plans, and follow their results.</p></div><button id="agent-create" class="primary" type="button" disabled>New agent plan</button></div>
    <div class="agent-metrics" aria-label="Workspace summary">
      <div><span>In progress</span><strong id="agent-active-count">—</strong><small>Queued and running</small></div>
      <div><span>Completed</span><strong id="agent-done-count">—</strong><small>Saved runs</small></div>
      <div><span>Agent teams</span><strong id="agent-team-count">—</strong><small>Configured for this workspace</small></div>
      <div><span>Execution</span><strong id="agent-execution">Loading</strong><small id="agent-execution-note">Checking server configuration</small></div>
    </div>
    <div id="agent-notice" class="agent-notice" hidden></div>
    <div class="agent-toolbar"><div class="agent-tabs" role="tablist" aria-label="Agent workspace"><button id="agent-tab-runs" type="button" role="tab" aria-selected="true" aria-controls="agent-browser" data-agent-tab="runs">Runs <span id="agent-runs-count">0</span></button><button id="agent-tab-plans" type="button" role="tab" aria-selected="false" tabindex="-1" aria-controls="agent-browser" data-agent-tab="plans">Plans <span id="agent-plans-count">0</span></button><button id="agent-tab-tools" type="button" role="tab" aria-selected="false" tabindex="-1" aria-controls="agent-browser" data-agent-tab="tools">Tools &amp; setup</button></div><button id="agent-refresh" type="button" class="agent-subtle">Refresh</button></div>
    <p id="agent-status" role="status" aria-live="polite"></p>
    <div id="agent-browser" role="tabpanel" aria-labelledby="agent-tab-runs"><div class="agent-split"><nav id="agent-list" aria-label="Agent runs"></nav><section id="agent-detail" aria-label="Selected work"></section></div></div>`;
  $('work-status').after(dashboard);
  const agentsTab = node('button', 'Agents'); agentsTab.id = 'agent-tab-agents'; agentsTab.type = 'button'; agentsTab.dataset.agentTab = 'agents'; agentsTab.setAttribute('role', 'tab'); agentsTab.setAttribute('aria-selected', 'false'); agentsTab.setAttribute('aria-controls', 'agent-browser'); agentsTab.tabIndex = -1; dashboard.querySelector('.agent-tabs').prepend(agentsTab);
  const newAgent = node('button', 'New agent', 'agent-subtle'); newAgent.id = 'agent-new-profile'; newAgent.type = 'button'; newAgent.onclick = () => window.SimonAgentLibrary?.create();
  const headingActions = node('div', undefined, 'agent-heading-actions'); headingActions.append(newAgent, $('agent-create')); dashboard.querySelector('.agent-heading').append(headingActions);
  const dialog = node('dialog', undefined, 'agent-dialog');
  dialog.id = 'agent-plan-dialog'; dialog.setAttribute('aria-labelledby', 'agent-plan-title');
  dialog.innerHTML = `
    <div class="agent-dialog-heading"><div><p class="eyebrow">START WITH A PLAN</p><h2 id="agent-plan-title">What should your team do?</h2></div><button id="agent-plan-close" type="button" class="icon-button" aria-label="Close plan builder">&#215;</button></div>
    <p class="muted">Define the work and review the assigned models and tools before starting a run.</p>
    <form id="agent-plan-form"><fieldset id="agent-plan-fields"><div class="agent-form-grid"><div><label for="agent-team">Team</label><select id="agent-team" required></select></div><div><label for="agent-context">Work context</label><select id="agent-context"><option value="">Personal workspace</option></select></div></div><p id="agent-team-description" class="muted"></p><div id="agent-task-editor"></div><button id="agent-add-task" type="button" class="agent-add">+ Add another task</button><details class="agent-advanced"><summary>Execution preferences</summary><div class="agent-form-grid"><label for="agent-parallel">Maximum parallel tasks<input id="agent-parallel" type="number" min="1" max="128" value="1" required></label><div><label for="agent-privacy">Model location</label><select id="agent-privacy"><option value="">Use each agent's policy</option><option value="local_only">Local models only</option></select></div></div></details></fieldset><div class="agent-form-footer"><p id="agent-plan-status" role="status" aria-live="polite">Creating a plan does not run its tasks.</p><button id="agent-plan-save" class="primary" type="submit">Review plan</button></div></form>`;
  document.body.append(dialog);

  function status(message, error = false) {
    $('agent-status').textContent = message;
    $('agent-status').classList.toggle('error', error);
  }
  function action(label, callback, className = '') {
    const button = node('button', label, className); button.type = 'button';
    button.onclick = async () => {
      button.disabled = true;
      try { await callback(); } catch (error) { status(error.message, true); }
      finally { if (button.isConnected) button.disabled = false; }
    };
    return button;
  }
  function teamName(id) { return catalog?.teams.find(item => item.id === id)?.name || id || 'Agent team'; }
  function agentName(id) { return catalog?.agents.find(item => item.id === id)?.name || id; }
  function planFor(run) { return plans.find(item => item.id === run.plan_id); }
  function titleFor(plan) { return plan?.tasks[0]?.objective || 'Agent work'; }
  function empty(title, description, cta) {
    const box = node('div', undefined, 'agent-empty');
    box.append(node('span', '↗', 'agent-empty-mark'), node('h3', title), node('p', description));
    if (cta) box.append(cta);
    return box;
  }
  function visible() { return !$('work-view').hidden && !dashboard.hidden && !document.hidden; }
  function poll() {
    clearTimeout(timer);
    if (visible()) timer = setTimeout(() => refresh(true), runs.some(run => !endStates.has(run.status)) ? 4000 : 30000);
  }
  async function refresh(quiet = false) {
    if (refreshing || !ready || !visible()) return;
    refreshing = true;
    $('agent-refresh').disabled = true;
    if (!quiet) status('Loading your workspace…');
    try {
      const requestRevision = revision;
      const results = await Promise.all([api(apiRoot + '/catalog'), api(apiRoot + '/plans'), api(apiRoot + '/runs')]);
      if (requestRevision !== revision) return;
      [catalog, plans, runs] = results;
      $('agent-create').disabled = !catalog.configured;
      $('agent-active-count').textContent = runs.filter(run => !endStates.has(run.status)).length;
      $('agent-done-count').textContent = runs.filter(run => run.status === 'succeeded').length;
      $('agent-team-count').textContent = catalog.teams.length;
      $('agent-execution').textContent = catalog.execution_enabled ? 'Enabled' : 'Planning only';
      $('agent-execution-note').textContent = catalog.execution_enabled ? 'Runs require an active dispatcher' : 'Execution is disabled on this server';
      $('agent-runs-count').textContent = runs.length; $('agent-plans-count').textContent = plans.length;
      const notice = $('agent-notice'); notice.hidden = catalog.configured && catalog.execution_enabled;
      notice.replaceChildren(node('strong', catalog.configured ? 'Planning is available. Execution is off.' : 'Set up your first agent team.'));
      notice.append(node('p', catalog.configured ? 'You can save and review plans now. Enable execution and run the dispatcher on your server when you are ready.' : 'Add an agent manifest with teams, profiles, and model endpoints on your server. Existing chat, projects, and local files remain available below.'));
      notice.append(action('View setup', () => setTab('tools'), 'agent-subtle'));
      // Preserve inputs and focus while a person is reviewing a start request or output.
      const previous = $('agent-detail')?.dataset.fingerprint;
      render(previous);
      if (!quiet) status('Updated ' + new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) + '.');
    } catch (error) {
      status(error.message + ' Use Refresh to try again.', true);
      if (!catalog) $('agent-detail').replaceChildren(empty('Workspace unavailable', 'Check your server connection and refresh to load your saved work.'));
    } finally { refreshing = false; $('agent-refresh').disabled = false; poll(); }
  }
  function setTab(name) {
    activeTab = name; selected = null;
    document.querySelectorAll('[data-agent-tab]').forEach(button => {
      const current = button.dataset.agentTab === name;
      button.setAttribute('aria-selected', String(current)); button.tabIndex = current ? 0 : -1;
    });
    $('agent-browser').setAttribute('aria-labelledby', 'agent-tab-' + name);
    render();
  }
  function render(previous) {
    if (!catalog) return;
    newAgent.hidden = activeTab === 'agents';
    if (activeTab === 'agents') { window.SimonAgentLibrary?.mount($('agent-browser'), catalog); return; }
    if (activeTab === 'tools') { renderTools(); return; }
    if (!$('agent-list')) $('agent-browser').innerHTML = '<div class="agent-split"><nav id="agent-list" aria-label="Agent work"></nav><section id="agent-detail" aria-label="Selected work"></section></div>';
    const items = activeTab === 'runs' ? runs : plans;
    if (!items.some(item => item.id === selected)) selected = items[0]?.id || null;
    const list = $('agent-list'); list.setAttribute('aria-label', activeTab === 'runs' ? 'Agent runs' : 'Saved agent plans'); list.replaceChildren();
    for (const item of items) {
      const plan = activeTab === 'runs' ? planFor(item) : item;
      const button = action('', () => { selected = item.id; render(); });
      button.className = 'agent-list-item'; button.setAttribute('aria-current', item.id === selected ? 'true' : 'false');
      const head = node('div', undefined, 'agent-row-heading'); head.append(node('small', teamName(plan?.team_id)), badge(item.status || item.state));
      button.append(head, node('strong', titleFor(plan)), node('small', `${item.tasks.length} task${item.tasks.length === 1 ? '' : 's'} · ${short(item.id)}`));
      list.append(button);
    }
    if (!items.length) {
      list.append(node('p', activeTab === 'runs' ? 'Your runs will appear here.' : 'Your saved plans will appear here.', 'muted'));
      $('agent-detail').replaceChildren(empty(activeTab === 'runs' ? 'Good work starts with a clear plan.' : 'Build a plan for your next project.', 'Choose a team, define a task, and review its model and tools. Results stay available when you return.', catalog.configured ? action('Create a plan', openBuilder, 'primary') : action('View setup', () => setTab('tools'), 'agent-subtle')));
      return;
    }
    const item = items.find(item => item.id === selected);
    const fingerprint = JSON.stringify([activeTab, item, catalog.execution_enabled]);
    if (previous === fingerprint) return;
    $('agent-detail').dataset.fingerprint = fingerprint;
    if (activeTab === 'runs') renderRun(item); else renderPlan(item);
  }
  function metadata(parent, values) {
    const dl = node('dl', undefined, 'agent-metadata');
    for (const [label, value] of values) { const row = node('div'); row.append(node('dt', label), node('dd', value)); dl.append(row); }
    parent.append(dl);
  }
  function detailHeading(parent, eyebrow, title, state) {
    const head = node('div', undefined, 'agent-detail-heading');
    head.append(node('p', eyebrow, 'eyebrow'), badge(state), node('h3', title)); parent.append(head);
  }
  function renderPlan(plan) {
    const target = $('agent-detail'); target.replaceChildren();
    detailHeading(target, 'PLAN / ' + short(plan.id), teamName(plan.team_id), plan.state);
    const estimates = plan.tasks.map(task => task.model?.estimated_cost_usd);
    const estimatedCost = estimates.every(value => value != null) ? money(estimates.reduce((sum, value) => sum + value, 0)) : 'Estimate unavailable';
    metadata(target, [['Tasks', String(plan.tasks.length)], ['Parallel limit', String(plan.max_parallel)], ['Initial model estimate', estimatedCost]]);
    if (plan.waves.length) {
      const stages = node('div', undefined, 'agent-stages'); stages.setAttribute('aria-label', 'Execution stages');
      plan.waves.forEach((wave, index) => { const stage = node('div'); stage.append(node('small', 'Stage ' + (index + 1)), node('span', wave.join(' + '))); stages.append(stage); }); target.append(stages);
    }
    for (const task of plan.tasks) {
      const card = node('article', undefined, 'agent-task-card');
      const head = node('div', undefined, 'agent-row-heading'); head.append(node('h4', task.id), node('span', agentName(task.agent_id), 'muted'));
      card.append(head, node('p', task.objective, 'agent-objective'));
      metadata(card, [['Model', task.model ? task.model.model + (task.model.local ? ' · Local' : ' · Cloud') : 'Unassigned'], ['Tools', task.tool_ids.length ? task.tool_ids.join(', ') : 'Text only'], ['Depends on', task.depends_on.join(', ') || 'No dependencies']]);
      if (task.environment) card.append(node('p', 'Environment: ' + task.environment.environment_id, 'muted'));
      for (const reason of task.blocked_reasons) card.append(node('p', reason, 'agent-blocked-reason'));
      target.append(card);
    }
    const footer = node('div', undefined, 'agent-start');
    if (plan.state !== 'planned') {
      footer.append(node('strong', 'Resolve the setup issues above to run this plan.'), node('p', 'After updating server configuration, create a new plan to use those changes.', 'muted'));
    } else if (!catalog.execution_enabled) {
      footer.append(node('strong', 'Plan saved. Execution is disabled.'), node('p', 'Enable agent execution and start the dispatcher on your server to queue this work.', 'muted'));
    } else {
      const form = node('form');
      const budgetLabel = node('label', 'Model budget (USD, optional)');
      const budget = node('input'); budget.type = 'number'; budget.min = '0'; budget.step = '0.01'; budget.placeholder = 'Use server limit'; budget.id = 'agent-run-budget'; budgetLabel.htmlFor = budget.id; budgetLabel.append(budget);
      const note = node('p', 'Starting queues this plan for the dispatcher. Model calls may incur costs. The budget covers model reservations; external tool charges are separate.', 'muted');
      const start = node('button', 'Start run', 'primary'); start.type = 'submit'; start.id = 'agent-start-run';
      form.append(budgetLabel, note, start); footer.append(form);
      form.onsubmit = async event => {
        event.preventDefault(); start.disabled = true;
        try {
          const modelBudget = budget.value === '' ? null : Number(budget.value);
          const requestFingerprint = JSON.stringify([plan.id, modelBudget]);
          if (!runKeys.has(requestFingerprint)) runKeys.set(requestFingerprint, crypto.randomUUID());
          const run = await api(apiRoot + '/plans/' + plan.id + '/runs', {idempotency_key: runKeys.get(requestFingerprint), model_budget_usd: modelBudget});
          revision++;
          runKeys.delete(requestFingerprint); runs = [run, ...runs.filter(item => item.id !== run.id)];
          setTab('runs'); selected = run.id; render(); status('Run queued. You can leave this page and return to its progress.'); poll();
        } catch (error) { status(error.message, true); }
        finally { start.disabled = false; }
      };
    }
    target.append(footer);
    target.append(action('Create another plan', openBuilder, 'agent-subtle'));
  }
  function renderRun(run) {
    const target = $('agent-detail'); target.replaceChildren();
    const plan = planFor(run);
    detailHeading(target, 'RUN / ' + short(run.id), teamName(plan?.team_id), run.status);
    const finished = run.tasks.filter(task => ['succeeded', 'failed', 'cancelled', 'blocked'].includes(task.status)).length;
    const progress = node('progress'); progress.max = run.tasks.length || 1; progress.value = finished; progress.setAttribute('aria-label', 'Tasks finished');
    target.append(progress, node('p', `${finished} of ${run.tasks.length} tasks finished`, 'muted'));
    metadata(target, [['Started', run.started_at ? new Date(run.started_at).toLocaleString() : 'Waiting for dispatcher'], ['Model reservation', money(run.model_reserved_usd)], ['Model budget', run.model_budget_usd == null ? 'Server limit' : money(run.model_budget_usd)]]);
    if (run.status === 'queued') target.append(node('p', 'This run is saved on your server. It will start when the agent dispatcher is available.', 'agent-inline-notice'));
    if (run.cancel_requested && !endStates.has(run.status)) target.append(node('p', 'Cancellation requested. An active tool call may need to finish before the run stops.', 'agent-inline-notice'));
    if (run.status === 'needs_human') target.append(node('p', 'This run needs operator review. Check the dispatcher and its logs before recovering a stopped worker.', 'agent-blocked-reason'));
    if (!endStates.has(run.status) && !run.cancel_requested) target.append(action('Cancel run', async () => {
      const saved = await api(apiRoot + '/runs/' + run.id + '/cancel', {});
      revision++;
      runs = runs.map(item => item.id === saved.id ? saved : item); render(); poll(); status('Cancellation requested.');
    }, 'agent-cancel'));
    for (const task of run.tasks) {
      const card = node('article', undefined, 'agent-task-card');
      const head = node('div', undefined, 'agent-row-heading'); head.append(node('h4', task.id), badge(task.status));
      card.append(head, node('p', agentName(task.agent_id), 'muted'));
      const spec = plan?.tasks.find(item => item.id === task.id);
      if (spec) card.append(node('p', spec.objective, 'agent-objective'));
      card.append(node('small', `${task.steps} steps · ${task.tool_calls} tool calls` + (task.output_tokens != null ? ` · ${task.output_tokens.toLocaleString()} output tokens` : ''), 'muted'));
      if (task.error_code) card.append(node('p', task.error_code.replaceAll('_', ' '), 'agent-blocked-reason'));
      if (task.output) {
        const details = node('details', undefined, 'agent-output'); details.open = run.tasks.length === 1;
        details.append(node('summary', 'View output'));
        const body = node('div', undefined, 'agent-output-body'); body.append(SimonMarkdown.render(task.output)); details.append(body);
        details.append(action('Copy output', async () => { await navigator.clipboard.writeText(task.output); status('Output copied.'); }, 'agent-subtle'));
        card.append(details);
      }
      const artifacts = node('div', undefined, 'agent-artifacts');
      for (const artifact of task.artifacts) {
        const link = node('a', undefined, 'agent-artifact');
        link.href = appPath(`${apiRoot}/runs/${run.id}/artifacts/${artifact.id}`); link.download = artifact.name;
        link.append(node('strong', artifact.name), node('small', size(artifact.size) + ' · Download')); artifacts.append(link);
      }
      if (task.artifacts.length) card.append(artifacts);
      target.append(card);
    }
    if (plan) target.append(action('View saved plan', () => { setTab('plans'); selected = plan.id; render(); }, 'agent-subtle'));
  }

  function renderTools() {
    const target = $('agent-browser');
    const fingerprint = JSON.stringify(catalog);
    if ($('agent-tool-search') && target.dataset.catalogFingerprint === fingerprint) return;
    const focused = ['agent-tool-search', 'agent-tool-state'].includes(document.activeElement?.id) ? document.activeElement : null;
    const focusId = focused?.id, selectionStart = focused?.selectionStart, selectionEnd = focused?.selectionEnd;
    target.dataset.catalogFingerprint = fingerprint;
    target.replaceChildren();
    const heading = node('div', undefined, 'agent-catalog-heading');
    heading.append(node('h3', 'Your tools and execution setup'), node('p', 'Configured tools still depend on account permissions, credentials, and service availability. Each plan checks its selected tools before it can run.', 'muted'));
    const shortcuts = node('div', undefined, 'agent-shortcuts');
    shortcuts.append(action('Browse local files', () => window.SimonLocalFiles.open()), action('Manage connections', () => $('connections-open').click()));
    heading.append(shortcuts); target.append(heading);
    const grid = node('div', undefined, 'agent-catalog-grid');
    function group(title, items, renderItem, emptyText, parent = grid) {
      const section = node('section', undefined, 'agent-catalog-group'); section.append(node('h4', title));
      if (!items.length) section.append(node('p', emptyText, 'muted'));
      items.forEach(item => section.append(renderItem(item))); parent.append(section);
    }
    function entry(title, description, state, stateText) {
      const card = node('article', undefined, 'agent-catalog-item'); const head = node('div', undefined, 'agent-row-heading');
      const tag = badge(state); if (stateText) tag.textContent = stateText;
      head.append(node('strong', title), tag); card.append(head, node('p', description, 'muted')); return card;
    }
    const setupLabels = {configured: 'Configured', disabled: 'Disabled', unconfigured: 'Needs setup', unavailable: 'Unavailable', permission_required: 'Needs permission'};
    const tools = [...(catalog.tool_statuses || catalog.tools)].sort((a, b) => a.id.localeCompare(b.id));
    const toolSection = node('section', undefined, 'agent-tool-catalog');
    toolSection.setAttribute('aria-label', 'Agent tools');
    toolSection.innerHTML = '<div class="agent-tool-filters"><div><label for="agent-tool-search">Find a tool</label><input id="agent-tool-search" type="search" placeholder="Search tools, providers, or capabilities" autocomplete="off"></div><div><label for="agent-tool-state">Setup status</label><select id="agent-tool-state"><option value="all">All statuses</option><option value="configured">Configured</option><option value="needs_setup">Needs setup</option></select></div></div><div class="agent-tool-pagination"><p id="agent-tool-count" class="muted" role="status" aria-live="polite"></p><div><button id="agent-tool-previous" class="agent-subtle" type="button">Previous</button><button id="agent-tool-next" class="agent-subtle" type="button">Next</button></div></div><div id="agent-tool-results" class="agent-tool-grid"></div>';
    target.append(toolSection);
    function toolCard(tool) {
      const state = tool.state || 'configured';
      const card = entry(tool.id, tool.description || (tool.categories || []).join(', '), state, setupLabels[state] || state);
      card.dataset.toolId = tool.id;
      card.append(node('p', `${tool.transport} · ${tool.side_effect ? 'Can make changes' : 'Read access'}`, 'muted'));
      for (const reason of tool.blocked_reasons || []) card.append(node('p', reason, 'agent-blocked-reason'));
      return card;
    }
    function toolResults() {
      const query = toolView.query.trim().toLowerCase();
      const filtered = tools.filter(tool => {
        const state = tool.state || 'configured';
        return (toolView.state === 'all' || (toolView.state === 'configured' ? state === 'configured' : state !== 'configured')) &&
          [tool.id, tool.description, tool.transport, ...(tool.categories || []), ...(tool.capabilities || [])].join(' ').toLowerCase().includes(query);
      });
      const perPage = 12, pages = Math.max(1, Math.ceil(filtered.length / perPage));
      toolView.page = Math.min(toolView.page, pages);
      const offset = (toolView.page - 1) * perPage;
      $('agent-tool-results').replaceChildren(...filtered.slice(offset, offset + perPage).map(toolCard));
      if (!filtered.length) $('agent-tool-results').append(empty(tools.length ? 'No matching tools.' : 'No agent tools configured.', tools.length ? 'Try another search or choose All statuses.' : 'Text-only plans can still run when a model is available.'));
      $('agent-tool-count').textContent = filtered.length ? `${offset + 1}–${Math.min(offset + perPage, filtered.length)} of ${filtered.length} tools · Page ${toolView.page} of ${pages}` : '0 matching tools';
      $('agent-tool-previous').disabled = toolView.page === 1;
      $('agent-tool-next').disabled = toolView.page === pages;
    }
    $('agent-tool-search').value = toolView.query;
    $('agent-tool-state').value = toolView.state;
    $('agent-tool-search').oninput = event => { toolView.query = event.target.value; toolView.page = 1; toolResults(); };
    $('agent-tool-state').onchange = event => { toolView.state = event.target.value; toolView.page = 1; toolResults(); };
    $('agent-tool-previous').onclick = () => { toolView.page--; toolResults(); };
    $('agent-tool-next').onclick = () => { toolView.page++; toolResults(); };
    toolResults();
    const setup = node('details', undefined, 'agent-setup-details');
    setup.append(node('summary', `Models and execution (${catalog.models.length} models, ${catalog.environments.length} environments)`));
    setup.append(node('p', 'Expand to review model availability and the environments used for coding, documents, media, and browser tasks.', 'muted'));
    group('Models', catalog.models, model => {
      const state = model.state || (model.enabled ? 'configured' : 'disabled');
      const card = entry(model.model, `${model.provider} · ${model.local ? 'Local' : 'Cloud'} · ${(model.capabilities || []).join(', ')}`, state, setupLabels[state] || state);
      for (const reason of model.blocked_reasons || []) card.append(node('p', reason, 'agent-blocked-reason'));
      return card;
    }, 'Add a model endpoint to route agent tasks.');
    group('Execution environments', catalog.environments, environment => entry(environment.id, `${environment.kind} · ${environment.os} · ${environment.max_concurrency} concurrent`, environment.enabled ? 'configured' : 'disabled', environment.enabled ? 'Enabled' : 'Disabled'), 'No execution environments configured. Add an environment for coding, Git, media, or desktop tools.');
    setup.append(grid); target.append(setup);
    const profiles = node('details', undefined, 'agent-setup-details');
    profiles.append(node('summary', `Agent profiles (${catalog.agents.length})`));
    group('Profiles available to your teams', catalog.agents, agent => entry(agent.name || agent.id, agent.description || `${agent.tool_ids.length} tools · ${agent.privacy === 'local_only' ? 'Local models only' : 'Cloud models permitted'} · ${agent.max_steps} steps maximum`, 'configured', 'Profile'), 'Add profiles to an agent team to make them available here.', profiles);
    target.append(profiles);
    const templates = node('details', undefined, 'agent-template-list');
    templates.append(node('summary', 'Available tool templates (' + catalog.tool_templates.length + ')'));
    templates.append(node('p', 'Templates describe optional integrations. They must be configured and connected on the server before agents can use them.', 'muted'));
    for (const tool of catalog.tool_templates) templates.append(entry(tool.id, tool.description || (tool.categories || []).join(', '), 'disabled', catalog.tools.some(item => item.id === tool.id) ? 'See configured tools' : 'Template'));
    target.append(templates);
    if (focusId) { $(focusId).focus({preventScroll: true}); if (selectionStart != null) $(focusId).setSelectionRange(selectionStart, selectionEnd); }
  }
  function updateTeam() {
    const team = catalog.teams.find(item => item.id === $('agent-team').value);
    if (!team) return;
    const limit = Math.min(team.max_parallel, catalog.max_parallel);
    $('agent-parallel').max = limit; $('agent-parallel').value = Math.min(Number($('agent-parallel').value) || 1, limit);
    $('agent-team-description').textContent = `${team.agent_ids.length} agent profile${team.agent_ids.length === 1 ? '' : 's'} · Up to ${limit} tasks in parallel`;
    for (const select of document.querySelectorAll('.agent-task-profile')) {
      const previous = select.value; select.replaceChildren(...team.agent_ids.map(id => new Option(agentName(id), id)));
      if (team.agent_ids.includes(previous)) select.value = previous;
      profileDescription(select);
    }
  }
  function profileDescription(select) {
    const profile = catalog.agents.find(item => item.id === select.value);
    const card = select.closest('.agent-edit-task');
    const unavailable = (catalog.tool_statuses || []).filter(tool => profile?.tool_ids.includes(tool.id) && tool.state !== 'configured').length;
    card.querySelector('.agent-profile-description').textContent = profile ? (profile.description || profile.instructions.slice(0, 180)) + (unavailable ? ` Setup needed: ${unavailable} tool${unavailable === 1 ? '' : 's'} unavailable. Review Tools & setup for details.` : '') : '';
  }
  function updateDependencies() {
    const cards = [...document.querySelectorAll('.agent-edit-task')];
    cards.forEach(card => {
      const choices = card.querySelector('.agent-dependencies');
      const checked = new Set([...choices.querySelectorAll('input:checked')].map(input => input.value)); choices.replaceChildren();
      for (const other of cards) {
        if (other === card) continue;
        const label = node('label', undefined, 'agent-check'); const check = node('input'); check.type = 'checkbox'; check.value = other.dataset.taskId; check.checked = checked.has(check.value);
        label.append(check, node('span', other.dataset.taskId)); choices.append(label);
      }
      card.querySelector('.agent-dependency-field').hidden = cards.length < 2;
      card.querySelector('.agent-remove-task').disabled = cards.length === 1;
    });
    $('agent-add-task').disabled = cards.length >= 100;
  }
  function addTask() {
    const id = 'task-' + (++taskSequence);
    const card = node('section', undefined, 'agent-edit-task'); card.dataset.taskId = id;
    card.innerHTML = `<div class="agent-row-heading"><h3>${id}</h3><button type="button" class="agent-remove-task agent-subtle" aria-label="Remove ${id}">Remove</button></div><label for="${id}-profile">Agent profile</label><select id="${id}-profile" class="agent-task-profile" required></select><p class="agent-profile-description muted"></p><label for="${id}-objective">What should this task produce?<textarea id="${id}-objective" class="agent-task-objective" rows="3" maxlength="16000" placeholder="Describe the goal, source files, and what a good result looks like." required></textarea></label><fieldset class="agent-dependency-field"><legend>Wait for these tasks to finish</legend><div class="agent-dependencies"></div></fieldset>`;
    card.querySelector('.agent-remove-task').onclick = () => { card.remove(); updateDependencies(); };
    card.querySelector('select').onchange = event => profileDescription(event.target);
    $('agent-task-editor').append(card); updateTeam(); updateDependencies();
    if (taskSequence > 1) card.querySelector('textarea').focus();
  }
  function openBuilder() {
    if (!catalog?.configured) return;
    if (!$('agent-task-editor').children.length) {
      $('agent-team').replaceChildren(...catalog.teams.map(team => new Option(team.name, team.id)));
      $('agent-context').replaceChildren(new Option('Personal workspace', ''), ...catalog.contexts.map(context => new Option(context.name, context.id)));
      $('agent-plan-status').textContent = 'Creating a plan does not run its tasks.';
      taskSequence = 0; addTask();
    }
    dialog.showModal(); $('agent-task-editor').querySelector('textarea').focus();
  }
  $('agent-plan-form').onsubmit = async event => {
    event.preventDefault(); if (saving) return;
    const tasks = [...document.querySelectorAll('.agent-edit-task')].map(card => ({
      id: card.dataset.taskId, agent_id: card.querySelector('select').value,
      objective: card.querySelector('textarea').value.trim(),
      depends_on: [...card.querySelectorAll('.agent-dependencies input:checked')].map(input => input.value),
      ...($('agent-privacy').value ? {privacy: $('agent-privacy').value} : {}),
    }));
    const pending = new Map(tasks.map(task => [task.id, new Set(task.depends_on)]));
    while (pending.size) {
      const available = [...pending].filter(([, dependencies]) => dependencies.size === 0).map(([id]) => id);
      if (!available.length) { $('agent-plan-status').textContent = 'These dependencies create a loop. Remove one dependency so at least one task can start.'; return; }
      available.forEach(id => pending.delete(id)); pending.forEach(dependencies => available.forEach(id => dependencies.delete(id)));
    }
    if (tasks.some(task => !task.objective)) { $('agent-plan-status').textContent = 'Describe the goal for every task.'; return; }
    const payload = {team_id: $('agent-team').value, context_id: $('agent-context').value || null, tasks, max_parallel: Number($('agent-parallel').value)};
    const fingerprint = JSON.stringify(payload);
    if (planSubmission?.fingerprint !== fingerprint) planSubmission = {fingerprint, key: crypto.randomUUID()};
    saving = true; $('agent-plan-fields').disabled = true; $('agent-plan-save').disabled = true; $('agent-plan-status').textContent = 'Checking your plan…';
    try {
      const plan = await api(apiRoot + '/plans', {...payload, idempotency_key: planSubmission.key});
      revision++;
      plans = [plan, ...plans.filter(item => item.id !== plan.id)]; planSubmission = null;
      $('agent-task-editor').replaceChildren(); dialog.close(); setTab('plans'); selected = plan.id; render();
      $('agent-plans-count').textContent = plans.length;
      status(plan.state === 'planned' ? 'Plan saved. Review the tasks, then start when ready.' : 'Plan saved. Review the setup issues before starting.');
    } catch (error) { $('agent-plan-status').textContent = error.message; }
    finally { saving = false; $('agent-plan-fields').disabled = false; $('agent-plan-save').disabled = false; }
  };
  $('agent-team').onchange = updateTeam;
  $('agent-add-task').onclick = addTask;
  $('agent-create').onclick = openBuilder;
  $('agent-plan-close').onclick = () => dialog.close();
  $('agent-refresh').onclick = () => refresh();
  document.querySelectorAll('[data-agent-tab]').forEach(button => {
    button.onclick = () => setTab(button.dataset.agentTab);
    button.onkeydown = event => {
      const tabs = [...document.querySelectorAll('[data-agent-tab]')]; let index = tabs.indexOf(button);
      if (event.key === 'ArrowRight') index = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') index = (index + tabs.length - 1) % tabs.length;
      else if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = tabs.length - 1;
      else return;
      event.preventDefault(); tabs[index].focus(); tabs[index].click();
    };
  });
  const observer = new MutationObserver(() => { if (visible()) refresh(true); else clearTimeout(timer); });
  observer.observe($('work-view'), {attributes: true, attributeFilter: ['hidden']});
  observer.observe(dashboard, {attributes: true, attributeFilter: ['hidden']});
  document.addEventListener('visibilitychange', () => { if (visible()) refresh(true); else clearTimeout(timer); });
  window.addEventListener('simon-ready', () => refresh(true));
  window.addEventListener('simon-agent-library-updated', event => { catalog = event.detail.catalog; render(); });
  $('work-refresh').addEventListener('click', () => refresh());
})();
