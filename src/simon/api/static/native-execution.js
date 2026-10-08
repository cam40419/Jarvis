'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = () => window.SimonNativeProjects;
  const dialog = $('ne-dialog');
  const forms = ['policy', 'workflow', 'start', 'schedule', 'runner', 'signal'];
  const policyFields = {
    active: 'max_active_runs',
    queued: 'max_queued_runs',
    steps: 'max_steps',
    attempts: 'max_attempts',
    depth: 'max_depth',
    children: 'max_children',
    calls: 'max_model_calls',
    lease: 'lease_seconds',
    timeout: 'run_timeout_seconds',
  };
  let generation = 0,
    viewGeneration = 0,
    refreshGeneration = 0,
    runGeneration = 0,
    projectId = null,
    workspaceId = null,
    snapshot = null,
    editing = null,
    latest = null,
    pending = null,
    runDetail = null,
    busy = false,
    uncertain = false,
    dirty = false,
    timer = null,
    runs = [],
    schedules = [],
    events = [],
    moreRuns = false,
    moreSchedules = false,
    moreEvents = false;
  const active = (ticket) =>
    ticket === generation &&
    dialog.open &&
    bridge().getProject()?.id === projectId &&
    bridge().getWorkspaceId() === workspaceId;
  const path = (suffix = '') => '/v2/projects/' + projectId + '/execution' + suffix;
  const node = (tag, text, className) => {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    if (className) result.className = className;
    return result;
  };
  const action = (text, callback, { manage = false, disabled = false } = {}) => {
    const item = node('button', text, 'np-button np-quiet');
    item.type = 'button';
    item.dataset.manage = String(manage);
    item.dataset.unavailable = String(disabled);
    item.onclick = callback;
    return item;
  };
  const feedback = (message = '', tone = '') => {
    $('ne-feedback').textContent = message;
    $('ne-feedback').dataset.tone = tone;
  };
  const date = (value) =>
    value
      ? new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
      : 'None';
  const money = (value) =>
    new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: 6,
    }).format((value || 0) / 1000000);
  const localTime = (value) => {
    if (!value) return '';
    const instant = new Date(value);
    return new Date(instant.getTime() - instant.getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 16);
  };
  const utcTime = (value) => {
    if (!value) return null;
    const instant = new Date(value);
    if (!Number.isFinite(instant.getTime())) throw Error('Enter a valid date and time.');
    return instant.toISOString();
  };
  const taskName = (id) =>
    snapshot?.candidates.find((item) => item.task_id === id)?.title || 'Task ' + id;
  const chosenTask = () => snapshot?.candidates.find((item) => item.task_id === $('ne-task').value);
  const clearSecret = () => {
    $('ne-secret-value').value = '';
    $('ne-secret-feedback').textContent = '';
    $('ne-secret').hidden = true;
  };

  function lock() {
    const managed = Boolean(snapshot?.can_manage);
    for (const control of dialog.querySelectorAll('input, textarea, select, button'))
      control.disabled = busy || uncertain;
    $('ne-refresh').disabled = busy || dirty;
    $('ne-check').disabled = busy || !uncertain;
    $('ne-retry').disabled = busy || !uncertain;
    $('ne-close').disabled = busy || uncertain;
    $('ne-uncertain').hidden = !uncertain;
    for (const control of dialog.querySelectorAll('[data-manage="true"]'))
      control.disabled = busy || uncertain || !managed || control.dataset.unavailable === 'true';
    for (const id of ['ne-policy-edit', 'ne-runner-new', 'ne-workflow', 'ne-schedule-new'])
      $(id).disabled = busy || uncertain || !managed;
    const candidate = chosenTask();
    $('ne-start').disabled = busy || uncertain || !managed || !candidate?.eligible;
    $('ne-workflow').disabled ||= !candidate;
    $('ne-schedule-new').disabled ||= !candidate;
    $('ne-task').disabled ||= !snapshot?.candidates.length;
    $('ne-save').disabled = busy || uncertain || !managed || Boolean(latest);
    $('ne-run-cancel').hidden = !runDetail?.can_cancel;
    $('ne-run-retry').hidden = !runDetail?.can_retry;
    $('ne-run-answer').hidden = !runDetail?.can_signal;
    $('ne-run-answer').textContent = runDetail?.waits.some(
      (wait) => wait.status === 'pending' && wait.kind === 'human',
    )
      ? 'Answer question'
      : 'Provide input';
    $('ne-run-cancel').disabled ||= !runDetail?.can_cancel;
    $('ne-run-retry').disabled ||= !runDetail?.can_retry;
    $('ne-run-answer').disabled ||= !runDetail?.can_signal;
    $('ne-runs-more').hidden = !moreRuns;
    $('ne-schedules-more').hidden = !moreSchedules;
    $('ne-events-more').hidden = !moreEvents;
    $('ne-rebase').disabled = busy || !latest || !managed;
    for (const form of forms) {
      $('ne-' + form + '-form').setAttribute('aria-busy', String(busy));
      for (const control of $('ne-' + form + '-form').querySelectorAll('input, textarea, select'))
        control.disabled ||= !managed;
    }
    if (editing?.kind === 'start' && editing.task.agent_id) $('ne-agent').disabled = true;
    dialog.setAttribute('aria-busy', String(busy));
  }

  function overview({ preserveSecret = false } = {}) {
    viewGeneration++;
    editing = latest = runDetail = null;
    dirty = false;
    events = [];
    if (!preserveSecret) clearSecret();
    $('ne-overview').hidden = false;
    $('ne-editor').hidden = true;
    $('ne-run-detail').hidden = true;
    $('ne-back').hidden = true;
    $('ne-editor-cancel').hidden = true;
    $('ne-save').hidden = true;
    $('ne-close').hidden = false;
    $('ne-conflict').hidden = true;
    lock();
  }

  function editor(kind, record, task = null) {
    if (busy || uncertain || !snapshot?.can_manage) return false;
    clearTimeout(timer);
    viewGeneration++;
    clearSecret();
    editing = { kind, record, task };
    latest = null;
    dirty = false;
    $('ne-overview').hidden = true;
    $('ne-editor').hidden = false;
    $('ne-run-detail').hidden = true;
    $('ne-back').hidden = true;
    $('ne-editor-cancel').hidden = false;
    $('ne-save').hidden = false;
    $('ne-close').hidden = true;
    $('ne-conflict').hidden = true;
    for (const name of forms) $('ne-' + name + '-form').hidden = name !== kind;
    $('ne-save').setAttribute('form', 'ne-' + kind + '-form');
    $('ne-save').textContent =
      kind === 'start'
        ? 'Queue task'
        : kind === 'runner'
          ? 'Enroll runner'
          : kind === 'signal'
            ? 'Send answer'
            : 'Save settings';
    feedback();
    lock();
    return true;
  }

  function taskSummary() {
    const task = chosenTask();
    $('ne-task-reason').textContent =
      (!snapshot?.can_manage ? 'Project owners control execution. ' : '') +
      (task?.reason ||
        (task
          ? 'Ready to queue under the current project limits.'
          : 'Create a task on the board to begin.'));
    lock();
  }

  function renderRuns() {
    const cards = runs.map((run) => {
      const card = node('li', undefined, 'ni-card');
      card.dataset.runId = run.id;
      card.append(
        node('h4', run.task_title || taskName(run.task_id)),
        node(
          'p',
          `${run.status} · Attempt ${run.attempt} · ${date(run.created_at)}`,
          'ni-run-meta',
        ),
        action('View run', () => openRun(run.id)),
      );
      if (run.error_code)
        card.append(node('p', run.error_code.replaceAll('_', ' '), 'np-field-help'));
      return card;
    });
    $('ne-runs').replaceChildren(
      ...(cards.length ? cards : [node('li', 'No runs yet.', 'np-empty')]),
    );
  }

  function renderSchedules() {
    const cards = schedules.map((schedule) => {
      const card = node('li', undefined, 'ni-card');
      card.dataset.scheduleId = schedule.id;
      card.append(
        node('h4', taskName(schedule.task_id)),
        node(
          'p',
          `${schedule.enabled ? 'Enabled' : 'Disabled'} · Next: ${date(schedule.next_run_at)} · ${schedule.timezone}`,
          'ni-run-meta',
        ),
        node(
          'p',
          `${schedule.occurrence_count} of ${schedule.max_occurrences} occurrences · ${schedule.interval_seconds ? 'Every ' + schedule.interval_seconds + ' seconds' : 'One time'}`,
        ),
      );
      if (snapshot.can_manage)
        card.append(action('Edit schedule', () => editSchedule(schedule), { manage: true }));
      return card;
    });
    $('ne-schedules').replaceChildren(
      ...(cards.length ? cards : [node('li', 'No schedules yet.', 'np-empty')]),
    );
  }

  function renderRunners() {
    const cards = snapshot.runners.map((runner) => {
      const card = node('li', undefined, 'ni-card');
      card.dataset.runnerId = runner.id;
      card.append(
        node('h4', runner.name),
        node('p', `${runner.status} · Expires ${date(runner.expires_at)}`, 'ni-run-meta'),
        node(
          'p',
          `Up to ${runner.max_concurrent_runs} concurrent runs · Last seen ${date(runner.last_seen_at)}`,
        ),
      );
      if (runner.status === 'active' && snapshot.can_manage)
        card.append(
          action(
            'Revoke runner',
            () =>
              command('revoke', '/runners/' + runner.id + '/revoke', {
                expected_version: runner.version,
              }),
            { manage: true },
          ),
        );
      return card;
    });
    $('ne-runners').replaceChildren(
      ...(cards.length
        ? cards
        : [
            node(
              'li',
              'No runners enrolled. Queued tasks wait for a connected runner.',
              'np-empty',
            ),
          ]),
    );
  }

  function renderOverview() {
    const policy = snapshot.policy;
    $('ne-policy-summary').textContent =
      (snapshot.can_manage ? '' : 'Read only. Project owners manage execution.\n') +
      `${policy.enabled ? 'Execution enabled' : 'Execution paused'} · ${policy.auto_start ? 'Automatic queuing enabled' : 'Tasks are queued manually'}\n` +
      `Up to ${policy.max_active_runs} active runs, ${policy.max_steps} steps per run, and ${policy.max_model_calls} model calls per root run. Shared cost ceiling ${money(policy.max_cost_microusd)}.`;
    const selected = $('ne-task').value;
    $('ne-task').replaceChildren(
      ...snapshot.candidates.map((item) => new Option(item.title, item.task_id)),
    );
    if (snapshot.candidates.some((item) => item.task_id === selected))
      $('ne-task').value = selected;
    renderRuns();
    renderSchedules();
    renderRunners();
    taskSummary();
  }

  function renderEvents() {
    const cards = events.map((event) => {
      const card = node('li', undefined, 'ni-card');
      card.append(
        node('p', `${event.kind.replaceAll('_', ' ')} · ${date(event.created_at)}`, 'ni-run-meta'),
      );
      if (Object.keys(event.details || {}).length) {
        const detail = node('details');
        detail.append(
          node('summary', 'Details'),
          node('pre', JSON.stringify(event.details, null, 2), 'ne-output'),
        );
        card.append(detail);
      }
      return card;
    });
    $('ne-events').replaceChildren(
      ...(cards.length ? cards : [node('li', 'No activity recorded yet.', 'np-empty')]),
    );
  }

  function renderRun() {
    const { run, totals, waits, steps, children } = runDetail;
    $('ne-run-heading').textContent = run.task_title || taskName(run.task_id);
    $('ne-run-provenance').textContent =
      `Agent: ${run.agent_name || run.agent_id}\n` +
      `Model: ${runDetail.model_label || run.model_label || run.model_id}\n` +
      `Task version ${run.task_version} · Role version ${run.agent_version} · Workflow version ${run.workflow_version}\n` +
      `Run ${run.id}`;
    $('ne-run-summary').textContent =
      `${run.status === 'completed' ? 'Candidate ready' : run.status} · Attempt ${run.attempt} · Step ${run.step_number}\n` +
      `Charged ${money(totals.charged_microusd)} · Held ${money(totals.held_microusd)} · ${totals.model_calls} model calls\n` +
      `Created ${date(run.created_at)} · Deadline ${date(run.deadline_at)}` +
      (run.next_wake_at ? '\nNext check ' + date(run.next_wake_at) : '') +
      (run.error_code ? '\n' + run.error_code.replaceAll('_', ' ') : '');
    $('ne-result-section').hidden = !run.result_text;
    $('ne-result').textContent = run.result_text || '';
    $('ne-run-waits').replaceChildren(
      ...waits.map((wait) => {
        const card = node('section', undefined, 'ni-card ni-section');
        card.append(
          node(
            'h4',
            `${wait.kind === 'human' ? 'Question for you' : wait.kind === 'children' ? 'Waiting for delegated work' : 'Waiting until scheduled time'} · ${wait.status}`,
          ),
        );
        if (wait.question) card.append(node('p', wait.question, 'ni-objective'));
        if (wait.response) card.append(node('p', wait.response, 'ni-objective'));
        card.append(node('p', 'Deadline ' + date(wait.deadline_at), 'np-field-help'));
        return card;
      }),
    );
    $('ne-checkpoints').replaceChildren(
      ...(steps.length
        ? steps.map((step) => {
            const card = node('li', undefined, 'ni-card');
            card.append(node('h4', `Step ${step.sequence} · ${step.kind} · ${step.status}`));
            if (step.error_code) card.append(node('p', step.error_code.replaceAll('_', ' ')));
            if (step.result?.summary) card.append(node('p', step.result.summary));
            card.append(
              node(
                'p',
                `Started ${date(step.created_at)}${step.finished_at ? ' · Finished ' + date(step.finished_at) : ''}`,
                'ni-run-meta',
              ),
            );
            return card;
          })
        : [node('li', 'No completed steps yet.', 'np-empty')]),
    );
    $('ne-children-list').replaceChildren(
      ...(children.length
        ? children.map((child) => {
            const card = node('li', undefined, 'ni-card');
            card.append(
              node('p', `${child.task_title || taskName(child.task_id)} · ${child.status}`),
              action('View delegated run', () => openRun(child.id)),
            );
            return card;
          })
        : [node('li', 'No delegated work.', 'np-empty')]),
    );
    renderEvents();
    lock();
  }

  function failure(error) {
    if ([401, 403, 404].includes(error.status)) {
      reset();
      return;
    }
    feedback(error.message, 'error');
  }

  async function refresh() {
    const ticket = generation;
    const refreshTicket = ++refreshGeneration;
    const data = await bridge().request(path());
    if (!active(ticket) || refreshTicket !== refreshGeneration) return false;
    snapshot = data;
    if (!snapshot.can_manage) {
      clearSecret();
      if (editing) overview();
    }
    runs = data.runs.items;
    schedules = data.schedules.items;
    moreRuns = data.runs.has_more;
    moreSchedules = data.schedules.has_more;
    renderOverview();
    if (runDetail && !editing) await loadRun(runDetail.run.id);
    return true;
  }

  async function loadRun(id) {
    const ticket = generation;
    const viewTicket = viewGeneration;
    const runTicket = ++runGeneration;
    const data = await bridge().request(path('/runs/' + id));
    if (!active(ticket) || viewTicket !== viewGeneration || runTicket !== runGeneration || editing)
      return false;
    if (runDetail?.run.id !== id) events = [];
    const known = new Set(events.map((event) => event.id));
    for (const event of data.events) if (!known.has(event.id)) events.push(event);
    events.sort((a, b) => a.sequence - b.sequence);
    moreEvents = data.events_has_more ?? data.events.length >= 50;
    runDetail = data;
    renderRun();
    return true;
  }

  async function openRun(id) {
    if (busy || uncertain || editing) return;
    const ticket = generation;
    viewGeneration++;
    clearSecret();
    busy = true;
    lock();
    try {
      if (!(await loadRun(id))) return;
      $('ne-overview').hidden = true;
      $('ne-editor').hidden = true;
      $('ne-run-detail').hidden = false;
      $('ne-back').hidden = false;
      $('ne-save').hidden = true;
      $('ne-editor-cancel').hidden = true;
      $('ne-close').hidden = false;
      feedback();
      $('ne-run-heading').focus();
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
        schedulePoll();
      }
    }
  }

  function schedulePoll() {
    clearTimeout(timer);
    if (!dialog.open || editing || uncertain || document.hidden) return;
    const continuing = runDetail
      ? ['queued', 'running', 'waiting'].includes(runDetail.run.status)
      : runs.some((run) => ['queued', 'running', 'waiting'].includes(run.status));
    if (!continuing) return;
    timer = setTimeout(async () => {
      if (busy || editing || uncertain || !dialog.open) return;
      const ticket = generation;
      try {
        if (runDetail) await loadRun(runDetail.run.id);
        else await refresh();
      } catch (error) {
        if (active(ticket)) failure(error);
      } finally {
        if (active(ticket)) schedulePoll();
      }
    }, 4000);
  }

  function editPolicy() {
    if (!editor('policy', snapshot.policy)) return;
    $('ne-editor-heading').textContent = 'Project execution settings';
    $('ne-enabled').checked = snapshot.policy.enabled;
    $('ne-auto').checked = snapshot.policy.auto_start;
    $('ne-cost').value = String(snapshot.policy.max_cost_microusd / 1000000);
    for (const [id, field] of Object.entries(policyFields))
      $('ne-' + id).value = snapshot.policy[field];
    $('ne-enabled').focus();
  }

  async function taskEditor(kind) {
    const task = chosenTask();
    if (busy || uncertain || !task || !snapshot?.can_manage) return;
    const ticket = generation;
    busy = true;
    lock();
    try {
      const data = await bridge().request(path('/tasks/' + task.task_id + '/workflow'));
      if (!active(ticket)) return;
      const currentTask = data.tasks.find((item) => item.id === task.task_id);
      const selected = { ...task, task_version: currentTask?.version || task.task_version };
      busy = false;
      if (!editor(kind, data.config, selected)) return;
      $('ne-editor-heading').textContent =
        (kind === 'start' ? 'Run task: ' : 'Dependencies & timing: ') + task.title;
      if (kind === 'workflow') {
        $('ne-dependencies').replaceChildren(
          ...data.tasks
            .filter((item) => item.id !== task.task_id)
            .map((item) => {
              const label = node('label', undefined, 'nt-checkbox');
              const input = document.createElement('input');
              input.type = 'checkbox';
              input.value = item.id;
              input.checked = data.config.dependency_ids.includes(item.id);
              label.append(input, node('span', item.title + ' · ' + item.status));
              return label;
            }),
        );
        if (!$('ne-dependencies').children.length)
          $('ne-dependencies').append(node('p', 'No other tasks to depend on.', 'np-empty'));
        $('ne-not-before').value = localTime(data.config.not_before);
      } else {
        const agents = data.agents.filter((agent) => agent.status === 'active');
        $('ne-agent').replaceChildren(
          new Option('Choose an agent', ''),
          ...agents.map((agent) => new Option(agent.name, agent.id)),
        );
        $('ne-agent').value = task.agent_id || '';
        $('ne-start-description').textContent =
          `${task.title}\n${task.reason || 'The server checks the current task, dependencies, role, model and limits before every step.'}`;
      }
      lock();
      $('ne-editor-heading').focus();
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  function editSchedule(record = null) {
    const task = record
      ? snapshot.candidates.find((item) => item.task_id === record.task_id) || {
          task_id: record.task_id,
          title: taskName(record.task_id),
        }
      : chosenTask();
    if (!task || !editor('schedule', record, task)) return;
    $('ne-editor-heading').textContent = 'Schedule: ' + task.title;
    $('ne-schedule-enabled-field').hidden = !record;
    $('ne-schedule-enabled').checked = record?.enabled ?? true;
    $('ne-schedule-at').value = localTime(
      record?.next_run_at || new Date(Date.now() + 3600000).toISOString(),
    );
    $('ne-schedule-zone').value =
      record?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    $('ne-schedule-interval').value = record?.interval_seconds || '';
    $('ne-schedule-count').value = record?.max_occurrences || 1;
    scheduleTime();
    $('ne-schedule-at').focus();
  }

  function scheduleTime() {
    try {
      const value = utcTime($('ne-schedule-at').value);
      $('ne-schedule-time-description').textContent = value
        ? 'Saved instant: ' + value
        : 'No future occurrence selected.';
    } catch {
      $('ne-schedule-time-description').textContent = 'Enter a valid date and time.';
    }
    $('ne-schedule-at').required = $('ne-schedule-enabled').checked;
  }

  function editRunner() {
    if (!editor('runner', null)) return;
    $('ne-editor-heading').textContent = 'Enroll an execution runner';
    $('ne-runner-form').reset();
    $('ne-runner-concurrency').value = 1;
    $('ne-runner-ttl').value = 86400;
    $('ne-runner-name').focus();
  }

  function answerQuestion() {
    if (!runDetail?.can_signal) return;
    const record = runDetail;
    if (!editor('signal', record)) return;
    $('ne-editor-heading').textContent = 'Answer the agent';
    $('ne-question').textContent =
      record.waits.find((wait) => wait.status === 'pending' && wait.kind === 'human')?.question ||
      'This run has not asked a question yet. This reply is used only if the current step asks one; it does not change work already sent to the model.';
    $('ne-answer').value = '';
    $('ne-answer').focus();
  }

  function buildOperation() {
    const { kind, record, task } = editing;
    let suffix = '',
      method = 'POST',
      body;
    if (kind === 'policy') {
      const price = $('ne-cost').value;
      if (!/^\d+(?:\.\d{1,6})?$/.test(price))
        throw Error('Enter a USD ceiling with at most six decimal places.');
      body = {
        enabled: $('ne-enabled').checked,
        auto_start: $('ne-auto').checked,
        max_cost_microusd: Math.round(Number(price) * 1000000),
        expected_version: record.version,
      };
      for (const [id, field] of Object.entries(policyFields))
        body[field] = Number($('ne-' + id).value);
      suffix = '/policy';
      method = 'PUT';
    } else if (kind === 'workflow') {
      const dependencyIds = [...$('ne-dependencies').querySelectorAll('input:checked')].map(
        (input) => input.value,
      );
      if (dependencyIds.length > 32) throw Error('Select at most 32 prerequisite tasks.');
      body = {
        dependency_ids: dependencyIds,
        not_before: utcTime($('ne-not-before').value),
        expected_version: record.version,
      };
      suffix = '/tasks/' + task.task_id + '/workflow';
      method = 'PUT';
    } else if (kind === 'start') {
      body = { expected_task_version: task.task_version, agent_id: $('ne-agent').value || null };
      suffix = '/tasks/' + task.task_id + '/runs';
    } else if (kind === 'schedule') {
      body = {
        next_run_at: utcTime($('ne-schedule-at').value),
        interval_seconds: $('ne-schedule-interval').value
          ? Number($('ne-schedule-interval').value)
          : null,
        timezone: $('ne-schedule-zone').value,
        max_occurrences: Number($('ne-schedule-count').value),
      };
      if (record) {
        body.expected_version = record.version;
        body.enabled = $('ne-schedule-enabled').checked;
        suffix = '/schedules/' + record.id;
        method = 'PUT';
      } else {
        body.expected_task_version = task.task_version;
        suffix = '/schedules';
        body.task_id = task.task_id;
      }
    } else if (kind === 'runner') {
      body = {
        name: $('ne-runner-name').value,
        max_concurrent_runs: Number($('ne-runner-concurrency').value),
        ttl_seconds: Number($('ne-runner-ttl').value),
      };
      suffix = '/runners';
    } else {
      body = { correlation_id: record.correlation_id, text: $('ne-answer').value };
      suffix = '/runs/' + record.run.id + '/signals';
    }
    return { kind, suffix, body, method };
  }

  async function showConflict() {
    if (!editing) return;
    const ticket = generation;
    const { kind, task, record } = editing;
    let value;
    if (kind === 'policy') value = (await bridge().request(path())).policy;
    else if (kind === 'workflow')
      value = (await bridge().request(path('/tasks/' + task.task_id + '/workflow'))).config;
    else if (kind === 'schedule' && record) {
      const data = await bridge().request(path('/schedules?offset=0&limit=100'));
      value = data.items.find((item) => item.id === record.id);
    }
    if (!active(ticket) || !value) return;
    latest = value;
    $('ne-latest').textContent = JSON.stringify(value, null, 2);
    $('ne-conflict').hidden = false;
  }

  async function afterSuccess(operation, result, recovered = false) {
    const ticket = generation;
    let runId = null;
    if (['start', 'cancel', 'retry', 'signal'].includes(operation.kind)) runId = result.id;
    overview();
    if (operation.kind === 'runner' && result.token) {
      $('ne-secret-value').value = result.token;
      result.token = null;
      $('ne-secret').hidden = false;
    }
    let message =
      operation.kind === 'start'
        ? 'Task queued. A connected runner can pick it up.'
        : 'Saved. The current state is shown below.';
    if (operation.kind === 'runner' && (recovered || result.replayed))
      message =
        'Runner enrollment was saved, but its one-time credential is no longer available. Revoke that runner and enroll a replacement.';
    feedback(message, operation.kind === 'runner' && recovered ? 'warning' : 'success');
    try {
      await refresh();
      if (!active(ticket)) return;
      if (runId) {
        busy = false;
        await openRun(runId);
        if (active(ticket)) feedback(message, 'success');
      }
    } catch (error) {
      if (active(ticket) && [401, 403, 404].includes(error.status)) failure(error);
      else if (active(ticket))
        feedback(
          message + ' Refresh status to reload the latest state. ' + error.message,
          'warning',
        );
    }
  }

  async function perform() {
    if (busy || !pending) return;
    const ticket = generation,
      operation = pending;
    busy = true;
    lock();
    feedback('Saving…');
    try {
      const result = await bridge().request(operation.url, operation.body, operation.method);
      if (!active(ticket)) return;
      pending = null;
      uncertain = false;
      await afterSuccess(operation, result);
    } catch (error) {
      if (!active(ticket)) return;
      if (!error.status || error.status >= 500) {
        uncertain = true;
        feedback(
          'The response could not be confirmed. Resolve the original request before making another change.',
          'warning',
        );
      } else {
        pending = null;
        uncertain = false;
        if ([401, 403, 404].includes(error.status)) {
          failure(error);
          return;
        }
        feedback(error.message + (editing ? ' Your draft is preserved.' : ''), 'error');
        if (error.status === 409) {
          try {
            await showConflict();
          } catch {
            feedback(error.message + ' Refresh status to compare the latest version.', 'error');
          }
        }
      }
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
        schedulePoll();
      }
    }
  }

  async function command(kind, suffix, body, method = 'POST') {
    if (busy || uncertain || !snapshot?.can_manage) return;
    pending = {
      kind,
      url: path(suffix),
      body: { ...body, idempotency_key: crypto.randomUUID() },
      method,
    };
    await perform();
  }

  async function checkOperation() {
    if (busy || !uncertain || !pending) return;
    const ticket = generation,
      operation = pending;
    busy = true;
    lock();
    try {
      const receipt = await bridge().request(
        path('/operations/' + encodeURIComponent(operation.body.idempotency_key)),
      );
      if (!active(ticket)) return;
      pending = null;
      uncertain = false;
      await afterSuccess(operation, { id: receipt.id, replayed: true }, true);
    } catch (error) {
      if (!active(ticket)) return;
      if (error.status === 404) {
        try {
          const current = await bridge().request(path());
          if (active(ticket) && !current.can_manage) {
            reset();
            return;
          }
          if (active(ticket))
            feedback(
              'No saved receipt was found. Retry the original request; its operation identifier is preserved.',
              'warning',
            );
        } catch (accessError) {
          if (active(ticket)) failure(accessError);
        }
      } else failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
        schedulePoll();
      }
    }
  }

  async function loadMore(kind) {
    if (busy || uncertain) return;
    const ticket = generation;
    const existing = kind === 'runs' ? runs : kind === 'schedules' ? schedules : events;
    const suffix = kind === 'events' ? '/runs/' + runDetail.run.id + '/events' : '/' + kind;
    busy = true;
    lock();
    try {
      const data = await bridge().request(
        path(suffix + '?offset=' + existing.length + '&limit=50'),
      );
      if (!active(ticket)) return;
      const known = new Set(existing.map((item) => item.id));
      if (data.items.some((item) => known.has(item.id)))
        throw Error('The history changed while loading. Refresh status before continuing.');
      existing.push(...data.items);
      if (kind === 'runs') {
        moreRuns = data.has_more;
        renderRuns();
      } else if (kind === 'schedules') {
        moreSchedules = data.has_more;
        renderSchedules();
      } else {
        moreEvents = data.has_more;
        renderEvents();
      }
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  for (const kind of forms) {
    $('ne-' + kind + '-form').addEventListener('input', () => {
      dirty = true;
      lock();
    });
    $('ne-' + kind + '-form').onsubmit = async (event) => {
      event.preventDefault();
      if (busy || uncertain || latest || !editing || !event.currentTarget.reportValidity()) return;
      try {
        const operation = buildOperation();
        await command(operation.kind, operation.suffix, operation.body, operation.method);
      } catch (error) {
        feedback(error.message, 'error');
      }
    };
  }
  $('ne-task').onchange = taskSummary;
  $('ne-policy-edit').onclick = editPolicy;
  $('ne-workflow').onclick = () => taskEditor('workflow');
  $('ne-start').onclick = () => taskEditor('start');
  $('ne-schedule-new').onclick = () => editSchedule();
  $('ne-schedule-at').oninput = scheduleTime;
  $('ne-schedule-enabled').onchange = scheduleTime;
  $('ne-runner-new').onclick = editRunner;
  $('ne-run-answer').onclick = answerQuestion;
  $('ne-run-cancel').onclick = () =>
    command('cancel', '/runs/' + runDetail.run.id + '/cancel', {
      expected_version: runDetail.run.version,
    });
  $('ne-run-retry').onclick = () =>
    command('retry', '/runs/' + runDetail.run.id + '/retry', {
      expected_version: runDetail.run.version,
    });
  $('ne-retry').onclick = perform;
  $('ne-check').onclick = checkOperation;
  $('ne-runs-more').onclick = () => loadMore('runs');
  $('ne-schedules-more').onclick = () => loadMore('schedules');
  $('ne-events-more').onclick = () => loadMore('events');
  $('ne-rebase').onclick = () => {
    if (busy || !latest || !editing) return;
    editing.record = latest;
    latest = null;
    $('ne-conflict').hidden = true;
    feedback('Your draft now uses the latest version. Review it before saving.');
    lock();
  };
  $('ne-editor-cancel').onclick = () => {
    if (!busy && !uncertain) {
      overview();
      feedback();
      schedulePoll();
    }
  };
  $('ne-back').onclick = () => {
    if (!busy && !uncertain) {
      overview();
      feedback();
      schedulePoll();
    }
  };
  $('ne-refresh').onclick = async () => {
    if (busy || dirty) return;
    const ticket = generation;
    busy = true;
    lock();
    try {
      if (await refresh())
        feedback(
          uncertain
            ? 'Status refreshed. Resolve the original request to continue.'
            : 'Status refreshed.',
        );
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
        schedulePoll();
      }
    }
  };
  $('ne-secret-copy').onclick = async () => {
    const ticket = generation;
    try {
      await navigator.clipboard.writeText($('ne-secret-value').value);
      if (active(ticket)) $('ne-secret-feedback').textContent = 'Credential copied.';
    } catch {
      if (active(ticket))
        $('ne-secret-feedback').textContent = 'Select the credential and copy it manually.';
    }
  };

  function reset() {
    generation++;
    viewGeneration++;
    clearTimeout(timer);
    clearSecret();
    projectId = workspaceId = snapshot = editing = latest = pending = runDetail = null;
    busy = uncertain = dirty = false;
    runs = [];
    schedules = [];
    events = [];
    moreRuns = moreSchedules = moreEvents = false;
    for (const kind of forms) $('ne-' + kind + '-form').reset();
    for (const id of [
      'ne-project',
      'ne-editor-heading',
      'ne-schedule-time-description',
      'ne-runs',
      'ne-schedules',
      'ne-runners',
      'ne-task',
      'ne-task-reason',
      'ne-policy-summary',
      'ne-dependencies',
      'ne-agent',
      'ne-start-description',
      'ne-question',
      'ne-run-heading',
      'ne-run-summary',
      'ne-run-provenance',
      'ne-run-waits',
      'ne-result',
      'ne-checkpoints',
      'ne-children-list',
      'ne-events',
      'ne-latest',
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
    $('ne-project').textContent = project.name;
    dialog.showModal();
    overview();
    busy = true;
    lock();
    $('ne-heading').focus();
    feedback('Loading execution…');
    try {
      if (await refresh()) feedback();
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
        schedulePoll();
      }
    }
  }

  function close() {
    if (busy || uncertain) return;
    reset();
    // Task statuses can change while the execution panel is open.
    if (bridge().getProject()) bridge().onTeamChanged();
  }
  $('np-execution').onclick = open;
  $('ne-close').onclick = close;
  $('ne-models').onclick = () => {
    if (!busy && !uncertain) {
      reset();
      window.SimonNativeModels?.open();
    }
  };
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    if (!editing) close();
  });
  window.addEventListener('beforeunload', (event) => {
    if (busy || uncertain || dirty) event.preventDefault();
  });
  window.addEventListener('native-project-loading', (event) => {
    if (projectId && event.detail.id !== projectId) reset();
  });
  window.addEventListener('native-project-unavailable', reset);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearTimeout(timer);
    else schedulePoll();
  });
  window.SimonNativeExecution = { open };
})();
