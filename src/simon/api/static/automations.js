'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const make = (tag, content, className) => {
    const node = document.createElement(tag);
    if (content !== undefined) node.textContent = content;
    if (className) node.className = className;
    return node;
  };
  let session, devices = [], flows = [], runs = [], schedules = [], triggers = [], batches = [], swapTrials = [], activeSwapId = '', busy = false, directBusy = false, printerReady = false, printerHomed = false, selectedRun = null, editingFlow = null;
  let printSteps = [];
  let swapPrintBusy = false;
  let activeSwapHash = '', loadedSwapGcode = '';
  const batchRepeatCounts = new Map();
  const note = (message, error = false) => { $('notice').textContent = message; $('notice').classList.toggle('error', error); };
  async function api(path, body, method) {
    const verb = method || (body === undefined ? 'GET' : 'POST');
    const response = await fetch(appPath(path), {
      method: verb, credentials: 'same-origin',
      headers: verb === 'GET' ? {} : {
        ...(body === undefined ? {} : {'Content-Type': 'application/json'}),
        'X-CSRF-Token': session.csrf_token,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (response.status === 401) { location.assign(appPath('/login')); throw Error('Sign in to continue.'); }
    const value = await response.json();
    if (!response.ok) throw Error(value.error?.message || 'Request failed. Please try again.');
    return value;
  }
  const when = value => value ? new Date(value).toLocaleString() : '—';
  const number = (value, suffix = '') => value == null ? '—' : `${value}${suffix}`;
  function printer(value) {
    const online = value.online === true;
    printerReady = online && (['IDLE', 'FINISH'].includes(value.state) || value.stopped_job_ready === true) && !value.alerts;
    printerHomed = value.homed_axes === 'XYZ';
    updateDirectButtons();
    $('swap-test-readiness').textContent = !online ? 'A1 is offline; refresh when it reconnects.'
      : !printerReady ? `A1 reports ${value.state}. Clear its active job or alerts before testing.`
      : !printerHomed ? 'A1 is stopped and unhomed. Use Home X, Y, Z above, then refresh.'
      : 'A1 is ready for a supervised swap-only test.';
    const status = !value.configured ? 'Not configured' : online ? value.state : 'Offline';
    $('printer-metric').textContent = status;
    $('printer-badge').textContent = status;
    $('printer-badge').className = 'badge ' + (online ? 'good' : 'warn');
    const alertCodes = value.hms_codes?.length ? ` (${value.hms_codes.join(', ')})` : '';
    const printerError = value.error_code_hex || value.alerts
      ? ` Printer error ${value.error_code_hex || value.state}${value.alerts ? ` with ${value.alerts} active alert${value.alerts === 1 ? '' : 's'}${alertCodes}` : ''}. Clear it on the printer before starting a batch.`
      : '';
    $('printer-message').textContent = (value.message || (online ? `LAN status received from A1${value.serial_suffix ? ` · …${value.serial_suffix}` : ''}.` : 'Status unavailable.')) + printerError;
    const percent = Number.isFinite(value.progress_percent) ? value.progress_percent : 0;
    $('print-progress').style.width = `${percent}%`;
    document.querySelector('.progress-track').setAttribute('aria-valuenow', String(percent));
    $('print-percent').textContent = online && value.progress_percent != null ? `${percent}%` : '—';
    $('print-job').textContent = value.job_name || (online ? 'No print identified' : 'Printer unavailable');
    $('print-layer').textContent = value.layer == null ? '—' : `${value.layer}${value.total_layers == null ? '' : ` / ${value.total_layers}`}`;
    $('print-time').textContent = number(value.remaining_minutes, ' min estimated');
    $('print-nozzle').textContent = number(value.nozzle_c, ' °C');
    $('print-bed').textContent = number(value.bed_c, ' °C');
    $('printer-freshness').textContent = value.observed_at ? `Status observed ${when(value.observed_at)}. Homed axes: ${value.homed_axes || 'none reported'}. Refreshes every 20 seconds while this page is open.` : 'No current printer report.';

  }
  function button(label, action, className = 'secondary') {
    const node = make('button', label, className);
    node.type = 'button';
    node.onclick = async () => {
      node.disabled = true;
      try { await action(); } catch (error) { note(error.message, true); }
      finally { node.disabled = false; }
    };
    return node;
  }
  function updateDirectButtons() {
    const allowed = session?.scopes.includes('jobs:write') && printerReady && !directBusy;
    $('home-axes').disabled = !allowed;
    $('direct-swap').disabled = !allowed || !printerHomed || !activeSwapId;
    $('direct-swap').textContent = 'Swap plate';
    const pending = batches.some(batch => !['done', 'canceled'].includes(batch.state));
    $('run-swap-print').disabled = !allowed || !printerHomed || swapPrintBusy || pending;
  }
  function option(value, label) { const item = make('option', label); item.value = value; return item; }
  function actionRow(stage, initial = null) {
    const row = make('div', undefined, 'timeline-action');
    row.stepId = initial?.id || `step_${crypto.randomUUID().replaceAll('-', '').slice(0, 12)}`;
    const head = make('div', undefined, 'step-head');
    const title = make('strong', 'Action');
    head.append(title, button('Remove action', () => {
      if (stage.actions.children.length === 1) throw Error('Each timeline step needs at least one action. Remove the whole step instead.');
      row.remove(); renumber();
    }));
    const fields = make('div', undefined, 'step-fields');
    const action = make('select');
    action.append(option('home.set', 'Set a connected device'), option('home.inventory', 'Check home inventory'),
      option('system.echo', 'Record a note'), option('wait', 'Wait for a duration'),
      option('printer.not_printing', 'Wait for the print to stop'));
    const actionLabel = make('label', 'Step type'); actionLabel.append(action); fields.append(actionLabel);
    const delayControls = make('div', undefined, 'duration-controls');
    const delay = make('input'); delay.type = 'number'; delay.min = '0'; delay.max = '604800'; delay.value = '0';
    const delayUnit = make('select'); delayUnit.append(option('1', 'seconds'), option('60', 'minutes'), option('3600', 'hours'));
    delayControls.append(delay, delayUnit);
    const delayLabel = make('label', 'Delay before this step'); delayLabel.append(delayControls); fields.append(delayLabel);
    const details = make('div', undefined, 'step-fields'); details.style.gridColumn = '1 / -1';
    function renderDetails() {
      details.replaceChildren();
      delayLabel.firstChild.textContent = action.value === 'wait' ? 'Wait duration' : 'Delay before this step';
      if (action.value === 'home.set') {
        const target = make('select'); target.className = 'target';
        const enabled = devices.filter(device => device.control_enabled);
        target.append(...(enabled.length ? enabled.map(device => option(device.id, `${device.name} · ${device.room || 'Unassigned'}`)) : [option('', 'No enabled lights or outlets')]));
        const targetLabel = make('label', 'Device'); targetLabel.append(target);
        const power = make('select'); power.className = 'power';
        power.append(option('on', 'Turn on'), option('off', 'Turn off'));
        const powerLabel = make('label', 'Power'); powerLabel.append(power);
        const brightness = make('input'); brightness.className = 'brightness'; brightness.type = 'number'; brightness.min = '1'; brightness.max = '100'; brightness.placeholder = 'Optional';
        const brightnessLabel = make('label', 'Brightness % (optional)'); brightnessLabel.append(brightness);
        const color = make('input'); color.className = 'color'; color.type = 'text'; color.pattern = '#[0-9a-fA-F]{6}'; color.placeholder = '#408ff4 (optional)';
        const colorLabel = make('label', 'Color (optional)'); colorLabel.append(color);
        details.append(targetLabel, powerLabel, brightnessLabel, colorLabel);
      } else if (action.value === 'system.echo') {
        const message = make('input'); message.className = 'message'; message.maxLength = 1000; message.required = true; message.placeholder = 'Note for run history';
        const label = make('label', 'Note'); label.className = 'full'; label.append(message); details.append(label);
      } else if (action.value === 'wait') {
        details.append(make('p', 'Pauses for this duration, then continues to the next step.', 'muted'));
      } else if (action.value === 'printer.not_printing') {
        details.append(make('p', 'Pauses here until the A1 leaves its active printing state, then continues. Use this after a print-start trigger.', 'muted'));
      }
    }
    action.value = initial?.kind === 'wait' ? 'wait' : initial?.kind === 'condition' ? 'printer.not_printing' : initial?.action || 'home.set';
    action.onchange = () => { renderDetails(); refreshTriggerStepTargets(); }; renderDetails(); fields.append(details); row.append(head, fields);
    if (initial) {
      const seconds = initial.delay_seconds || 0;
      const multiplier = seconds && seconds % 3600 === 0 ? 3600 : seconds && seconds % 60 === 0 ? 60 : 1;
      delayUnit.value = String(multiplier); delay.value = String(seconds / multiplier);
      if (action.value === 'home.set') {
        row.querySelector('.target').value = initial.inputs?.device_id || '';
        row.querySelector('.power').value = initial.inputs?.on === false ? 'off' : 'on';
        row.querySelector('.brightness').value = initial.inputs?.brightness ?? '';
        row.querySelector('.color').value = initial.inputs?.color || '';
      } else if (action.value === 'system.echo') row.querySelector('.message').value = initial.inputs?.message || '';
    }
    row.action = action; row.delay = delay; row.delayUnit = delayUnit;
    stage.actions.append(row); renumber();
    return row;
  }
  function stageRow(initialSteps = [null]) {
    const stage = make('section', undefined, 'timeline-stage');
    const header = make('div', undefined, 'timeline-stage-head');
    const heading = make('div');
    const marker = make('span', '', 'timeline-marker'), title = make('strong', 'Timeline step', 'stage-title');
    heading.append(marker, title);
    header.append(heading, button('Remove step', () => { stage.remove(); renumber(); }));
    const actions = make('div', undefined, 'stage-actions'); stage.actions = actions;
    const add = button('+ Add action at this step', () => actionRow(stage)); add.classList.add('add-stage-action');
    stage.append(header, actions, add); $('steps').append(stage);
    for (const initial of initialSteps.length ? initialSteps : [null]) actionRow(stage, initial);
    renumber(); return stage;
  }
  function refreshTriggerStepTargets() {
    const stages = [...$('steps').children];
    for (const row of $('trigger-drafts').children) {
      if (!row.startStep) continue;
      const selected = row.startStep.value;
      row.startStep.replaceChildren(option('', 'Start at timeline step 1 (whole workflow)'), ...stages.map((stage, index) => {
        const actions = [...stage.actions.children];
        const summary = actions.map(action => action.action.options[action.action.selectedIndex]?.text || 'Action').join(' + ');
        return option(actions[0]?.stepId || '', `Start at timeline step ${index + 1}: ${summary}`);
      }));
      row.startStep.value = [...row.startStep.options].some(item => item.value === selected) ? selected : '';
    }
  }
  function renumber() {
    [...$('steps').children].forEach((stage, stageIndex) => {
      stage.querySelector('.timeline-marker').textContent = String(stageIndex + 1);
      stage.querySelector('.stage-title').textContent = `Timeline step ${stageIndex + 1}`;
      [...stage.actions.children].forEach((row, actionIndex) => {
        row.querySelector('.step-head strong').textContent = `Action ${actionIndex + 1}`;
      });
    });
    refreshTriggerStepTargets();
  }
  function specFromForm() {
    const stages = [...$('steps').children];
    if (!stages.length) throw Error('Add at least one timeline step.');
    const actionCount = stages.reduce((total, stage) => total + stage.actions.children.length, 0);
    if (actionCount > 25) throw Error('A workflow can contain at most 25 actions across all timeline steps.');
    const steps = [];
    stages.forEach((stage, stageIndex) => {
      const previous = stageIndex ? [...stages[stageIndex - 1].actions.children].map(row => row.stepId) : [];
      [...stage.actions.children].forEach((row, actionIndex) => {
        const delay_seconds = Number(row.delay.value) * Number(row.delayUnit.value);
        if (!Number.isInteger(delay_seconds) || delay_seconds < 0 || delay_seconds > 604800) throw Error(`Timeline step ${stageIndex + 1}, action ${actionIndex + 1} has an invalid delay.`);
        if (row.action.value === 'wait') { steps.push({id: row.stepId, kind: 'wait', depends_on: previous, delay_seconds}); return; }
        if (row.action.value === 'printer.not_printing') { steps.push({id: row.stepId, kind: 'condition', condition: 'printer.not_printing', depends_on: previous, delay_seconds}); return; }
        const inputs = {};
        if (row.action.value === 'home.set') {
          inputs.device_id = row.querySelector('.target').value;
          if (!inputs.device_id) throw Error('Choose a configured light or outlet.');
          inputs.on = row.querySelector('.power').value === 'on';
          const brightness = row.querySelector('.brightness').value;
          const color = row.querySelector('.color').value.trim();
          if (brightness) inputs.brightness = Number(brightness);
          if (color) inputs.color = color;
        }
        if (row.action.value === 'system.echo') inputs.message = row.querySelector('.message').value.trim();
        steps.push({id: row.stepId, action: row.action.value, inputs, depends_on: previous, delay_seconds});
      });
    });
    return {name: $('flow-name').value.trim(), steps};
  }
  async function start(flow, startAt) {
    const body = {definition_version: flow.version, idempotency_key: crypto.randomUUID()};
    if (startAt) {
      const date = new Date(startAt);
      if (!Number.isFinite(date.getTime()) || date <= new Date()) throw Error('Choose a future start time.');
      body.start_at = date.toISOString();
    }
    const run = await api(`/v1/workflows/${flow.id}/runs`, body);
    note(`${flow.spec.name} ${startAt ? 'scheduled' : 'queued'} for ${when(run.start_at)}.`);
    await refresh();
  }
  async function repeat(flow, frequency, startAt) {
    const date = new Date(startAt);
    if (!Number.isFinite(date.getTime()) || date <= new Date()) throw Error('Choose a future first run time.');
    await api(`/v1/workflows/${flow.id}/schedules`, {
      definition_version: flow.version, frequency, start_at: date.toISOString(),
      time_zone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
      idempotency_key: crypto.randomUUID(),
    });
    note(`${flow.spec.name} will run ${frequency}, starting ${when(date)}.`); await refresh();
  }
  async function controlSchedule(schedule, action) {
    await api(`/v1/workflow-schedules/${schedule.id}/control`, {action, expected_version: schedule.version});
    note(`${schedule.name}: recurring schedule ${action}d.`); await refresh();
  }
  async function removeSchedule(schedule) {
    if (!window.confirm(`Remove the ${schedule.frequency} schedule for “${schedule.name}”?`)) return;
    await api(`/v1/workflow-schedules/${schedule.id}?expected_version=${schedule.version}`, undefined, 'DELETE');
    note(`${schedule.name}: recurring schedule removed.`); await refresh();
  }
  async function controlTrigger(trigger, action) {
    try {
      await api(`/v1/workflow-triggers/${trigger.id}/control`, {action, expected_version: trigger.version});
    } catch (error) {
      if (!error.message.includes('trigger changed')) throw error;
      const current = (await api('/v1/workflow-triggers')).find(item => item.id === trigger.id);
      if (!current) throw error;
      await api(`/v1/workflow-triggers/${trigger.id}/control`, {action, expected_version: current.version});
    }
    note(`${trigger.name}: event trigger ${action}d.`); await refresh();
  }
  async function removeTrigger(trigger) {
    try {
      await api(`/v1/workflow-triggers/${trigger.id}?expected_version=${trigger.version}`, undefined, 'DELETE');
    } catch (error) {
      if (!error.message.includes('trigger changed')) throw error;
      const current = (await api('/v1/workflow-triggers')).find(item => item.id === trigger.id);
      if (!current) throw error;
      await api(`/v1/workflow-triggers/${trigger.id}?expected_version=${current.version}`, undefined, 'DELETE');
    }
    note(`${trigger.name}: trigger removed.`); await refresh();
  }
  function triggerDescription(trigger) {
    if (trigger.kind === 'printer.print_started') return 'A1 starts printing';
    if (trigger.kind === 'printer.print_finished') return 'A1 finishes printing';
    const device = devices.find(item => item.id === trigger.device_id);
    const value = trigger.field === 'on' ? (trigger.value ? 'On' : 'Off')
      : trigger.field === 'online' ? (trigger.value ? 'Online' : 'Offline')
      : String(trigger.value);
    return `${device?.name || trigger.device_id}: ${trigger.field} ${trigger.operator} ${value}`;
  }
  function triggerDraftRow(initial = null) {
    const editor = make('div', undefined, 'launch-draft');
    const head = make('div', undefined, 'step-head');
    head.append(make('strong', 'State trigger'), button('Remove', () => editor.remove()));
    editor.append(head);
    const fields = make('div', undefined, 'step-fields');
    const device = make('select');
    device.append(option('printer.a1', 'Bambu Lab A1'));
    for (const item of devices) device.append(option(item.id, `${item.name} · ${item.room || 'Unassigned'}`));
    const deviceLabel = make('label', 'Device'); deviceLabel.append(device);
    const state = make('select');
    const stateLabel = make('label', 'State begins'); stateLabel.append(state);
    const comparison = make('select');
    const comparisonLabel = make('label', 'Comparison'); comparisonLabel.append(comparison);
    const value = make('input');
    const valueLabel = make('label', 'Value'); valueLabel.append(value);
    const startStep = make('select');
    const startStepLabel = make('label', 'Start workflow at'); startStepLabel.append(startStep);
    const numericStates = [
      ['brightness', 'Brightness'], ['watts', 'Power draw'], ['energy_wh', 'Energy'],
      ['voltage', 'Voltage'], ['current', 'Current'], ['frequency', 'Frequency'],
      ['temperature_c', 'Temperature'], ['uptime_seconds', 'Uptime'],
    ];
    function configureValue() {
      comparison.replaceChildren();
      value.value = '';
      const simple = ['printer.print_started', 'printer.print_finished', 'on.true', 'on.false', 'online.true', 'online.false'].includes(state.value);
      comparisonLabel.hidden = simple;
      valueLabel.hidden = simple;
      if (simple) return;
      if (state.value === 'color') {
        comparison.append(option('equals', 'Becomes'));
        value.type = 'text'; value.pattern = '#[0-9a-fA-F]{6}'; value.value = '#ffffff';
      } else {
        comparison.append(option('above', 'Crosses above'), option('below', 'Crosses below'), option('equals', 'Becomes'));
        value.type = 'number'; value.step = 'any'; value.value = '0'; value.removeAttribute('pattern');
      }
    }
    function configureStates() {
      state.replaceChildren();
      if (device.value === 'printer.a1') {
        state.append(option('printer.print_started', 'Starts printing'), option('printer.print_finished', 'Finishes printing'));
      } else {
        state.append(
          option('on.true', 'Turns on'), option('on.false', 'Turns off'),
          option('online.true', 'Comes online'), option('online.false', 'Goes offline'),
          option('color', 'Color'),
        );
        for (const [field, label] of numericStates) state.append(option(field, label));
      }
      configureValue();
    }
    device.onchange = configureStates;
    state.onchange = configureValue;
    device.value = initial?.kind === 'device.state' ? initial.device_id : 'printer.a1';
    configureStates();
    if (initial) {
      state.value = initial.kind !== 'device.state' ? initial.kind
        : ['on', 'online'].includes(initial.field) ? `${initial.field}.${initial.value}` : initial.field;
      configureValue();
      comparison.value = initial.operator || 'equals';
      if (!['on', 'online'].includes(initial.field) && initial.kind === 'device.state') value.value = String(initial.value);
    }
    fields.append(deviceLabel, stateLabel, comparisonLabel, valueLabel, startStepLabel);
    startStepLabel.className = 'full';
    editor.append(fields);
    $('trigger-drafts').append(editor);
    editor.startStep = startStep;
    refreshTriggerStepTargets();
    startStep.value = initial?.start_step_id || '';
    editor.definition = () => {
      let trigger;
      if (state.value.startsWith('printer.')) trigger = {kind: state.value};
      else if (state.value.includes('.')) {
        const [field, raw] = state.value.split('.');
        trigger = {kind: 'device.state', device_id: device.value, field, operator: 'equals', value: raw === 'true'};
      } else {
        const raw = value.value.trim();
        const parsed = state.value === 'color' ? raw : Number(raw);
        if (typeof parsed === 'number' && !Number.isFinite(parsed)) throw Error('Enter a numeric trigger value.');
        trigger = {kind: 'device.state', device_id: device.value, field: state.value, operator: comparison.value, value: parsed};
      }
      return {trigger, start_step_id: startStep.value || null};
    };
    return editor;
  }
  function scheduleDraftRow(initial = null) {
    const row = make('div', undefined, 'launch-draft');
    const head = make('div', undefined, 'step-head');
    head.append(make('strong', 'Scheduled time'), button('Remove', () => row.remove()));
    const fields = make('div', undefined, 'step-fields');
    const frequency = make('select');
    frequency.append(option('once', 'Run once'), option('daily', 'Every day'), option('weekly', 'Every week'));
    frequency.value = initial?.frequency || 'once';
    const frequencyLabel = make('label', 'Repeats'); frequencyLabel.append(frequency);
    const date = make('input'); date.type = 'datetime-local';
    if (initial?.start_at) {
      const local = new Date(initial.start_at); local.setMinutes(local.getMinutes() - local.getTimezoneOffset());
      date.value = local.toISOString().slice(0, 16);
    }
    const dateLabel = make('label', initial?.frequency && initial.frequency !== 'once' ? 'First run' : 'Run at'); dateLabel.append(date);
    frequency.onchange = () => { dateLabel.firstChild.textContent = frequency.value === 'once' ? 'Run at' : 'First run'; };
    fields.append(frequencyLabel, dateLabel); row.append(head, fields);
    row.definition = () => {
      if (!date.value) throw Error('Choose a date and time for every scheduled launch.');
      const parsed = new Date(date.value);
      if (!Number.isFinite(parsed.getTime()) || parsed <= new Date()) throw Error('Scheduled launch times must be in the future.');
      return {frequency: frequency.value, start_at: parsed.toISOString()};
    };
    $('schedule-drafts').append(row);
    return row;
  }
  function resetBuilder() {
    editingFlow = null;
    $('flow-name').value = '';
    $('steps').replaceChildren(); $('trigger-drafts').replaceChildren(); $('schedule-drafts').replaceChildren();
    stageRow(); triggerDraftRow();
    $('builder-heading').textContent = 'Build a workflow';
    $('save-workflow').textContent = 'Save complete workflow';
    $('cancel-edit').hidden = true;
  }
  function stepsIntoStages(steps) {
    const remaining = [...steps], completed = new Set(), stages = [];
    while (remaining.length) {
      const ready = remaining.filter(step => (step.depends_on || []).every(id => completed.has(id)));
      if (!ready.length) return steps.map(step => [step]);
      stages.push(ready);
      ready.forEach(step => completed.add(step.id));
      for (const step of ready) remaining.splice(remaining.indexOf(step), 1);
    }
    return stages;
  }
  function editWorkflow(flow) {
    editingFlow = flow;
    $('flow-name').value = flow.spec.name;
    $('steps').replaceChildren(); $('trigger-drafts').replaceChildren(); $('schedule-drafts').replaceChildren();
    stepsIntoStages(flow.spec.steps).forEach(stage => stageRow(stage));
    triggers.filter(item => item.definition_id === flow.id).forEach(trigger => triggerDraftRow(trigger));
    schedules.filter(item => item.definition_id === flow.id).forEach(schedule => scheduleDraftRow({
      frequency: schedule.frequency, start_at: schedule.next_run_at || schedule.start_at,
    }));
    $('builder-heading').textContent = `Edit ${flow.spec.name}`;
    $('save-workflow').textContent = 'Save workflow changes';
    $('cancel-edit').hidden = false;
    document.querySelector('.workflow-studio').scrollIntoView({behavior: 'smooth', block: 'start'});
  }
  const actionLabel = step => step.kind === 'wait' ? `Wait${step.delay_seconds ? ` ${Math.round(step.delay_seconds / 60)} min` : ''}` : step.kind === 'condition' ? 'Print stops'
    : step.action === 'home.set' ? 'Set device' : step.action === 'home.inventory' ? 'Check home' : 'Record note';
  function renderFlows() {
    $('flows').replaceChildren(...(flows.length ? flows.map(flow => {
      const card = make('article', undefined, 'automation-card');
      const top = make('div', undefined, 'automation-card-head');
      const flowStages = stepsIntoStages(flow.spec.steps);
      const title = make('div'); title.append(make('h3', flow.spec.name), make('p', `${flowStages.length} timeline step${flowStages.length === 1 ? '' : 's'} · ${flow.spec.steps.length} action${flow.spec.steps.length === 1 ? '' : 's'} · Version ${flow.version}`));
      const actions = make('div', undefined, 'card-actions');
      actions.append(button('Run now', () => start(flow), 'primary'), button('Edit workflow', () => editWorkflow(flow)));
      top.append(title, actions); card.append(top);
      const sequence = make('div', undefined, 'action-sequence');
      flowStages.forEach((stage, index) => {
        const group = make('div', undefined, 'timeline-stage-summary'); group.append(make('b', `Step ${index + 1}`));
        stage.forEach(step => group.append(make('span', `${actionLabel(step)}${step.delay_seconds && step.kind !== 'wait' ? ` (+${step.delay_seconds}s)` : ''}`)));
        sequence.append(group);
      });
      card.append(sequence);
      const launchHeading = make('div', undefined, 'launch-heading'); launchHeading.append(make('strong', 'Launches'), make('small', 'State changes and scheduled times'));
      card.append(launchHeading);
      const launches = make('div', undefined, 'launch-cards');
      const attachedTriggers = triggers.filter(item => item.definition_id === flow.id);
      for (const eventTrigger of attachedTriggers) {
        const row = make('div', undefined, 'launch-card event');
        const targetIndex = flowStages.findIndex(stage => stage.some(step => step.id === eventTrigger.start_step_id));
        const target = eventTrigger.start_step_id
          ? targetIndex >= 0 ? `starts at timeline step ${targetIndex + 1}` : 'target step no longer exists'
          : 'starts whole workflow';
        const copy = make('div'); copy.append(make('span', 'WHEN STATE STARTS', 'launch-kind'), make('strong', triggerDescription(eventTrigger)), make('small', `${eventTrigger.enabled ? 'Armed' : 'Paused'} · ${target}`));
        const controls = make('div', undefined, 'launch-actions');
        controls.append(button(eventTrigger.enabled ? 'Pause' : 'Resume', () => controlTrigger(eventTrigger, eventTrigger.enabled ? 'pause' : 'resume')));
        controls.append(button('Remove', () => removeTrigger(eventTrigger)));
        row.append(copy, controls); launches.append(row);
      }
      const recurring = schedules.filter(item => item.definition_id === flow.id);
      for (const item of recurring) {
        const row = make('div', undefined, 'launch-card timed');
        const copy = make('div'); copy.append(make('span', item.frequency.toUpperCase(), 'launch-kind'), make('strong', item.enabled ? when(item.next_run_at) : 'Paused'), make('small', item.time_zone));
        const controls = make('div', undefined, 'launch-actions');
        controls.append(button(item.enabled ? 'Pause' : 'Resume', () => controlSchedule(item, item.enabled ? 'pause' : 'resume')));
        controls.append(button('Remove', () => removeSchedule(item))); row.append(copy, controls); launches.append(row);
      }
      const oneTime = runs.filter(run => run.definition_id === flow.id && run.status === 'queued' && new Date(run.start_at) > new Date());
      for (const run of oneTime) {
        const row = make('div', undefined, 'launch-card timed');
        const copy = make('div'); copy.append(make('span', 'ONE TIME', 'launch-kind'), make('strong', when(run.start_at)), make('small', 'Queued'));
        row.append(copy, button('Cancel', () => control(run, 'cancel'))); launches.append(row);
      }
      if (!launches.children.length) launches.append(make('p', 'Manual only · use Edit workflow to add triggers or times.', 'empty-launches'));
      card.append(launches);
      const footer = make('div', undefined, 'automation-footer');
      footer.append(button('Delete workflow', async () => {
        if (!window.confirm(`Delete “${flow.spec.name}”? Completed run history will remain.`)) return;
        await api(`/v1/workflows/${flow.id}?expected_version=${flow.version}`, undefined, 'DELETE');
        note(`${flow.spec.name} deleted.`); await refresh();
      })); card.append(footer); return card;
    }) : [make('p', 'No workflows yet. Build one above to get started.', 'muted')]));
  }
  async function control(run, action) {
    await api(`/v1/workflow-runs/${run.id}/control`, {action, expected_version: run.version});
    note(`${run.name}: ${action} requested.`); await refresh();
  }
  function renderRuns() {
    const active = runs.filter(run => ['running', 'waiting', 'needs_attention'].includes(run.status)).length;
    const scheduled = runs.filter(run => run.status === 'queued' && new Date(run.start_at) > new Date()).length;
    $('active-metric').textContent = active; $('scheduled-metric').textContent = scheduled;
    $('runs').replaceChildren(...(runs.length ? runs.map(run => {
      const card = make('article', undefined, 'card');
      card.append(make('h3', run.name), make('p', `${run.status.replaceAll('_', ' ')} · ${run.steps.filter(step => step.status === 'succeeded').length}/${run.steps.length} steps complete`), make('small', `Start: ${when(run.start_at)}`));
      const actions = make('div', undefined, 'card-actions');
      actions.append(button('Details', () => showRun(run)));
      if (!['succeeded', 'failed', 'cancelled', 'expired'].includes(run.status)) {
        if (run.status === 'paused') actions.append(button('Resume', () => control(run, 'resume')));
        else if (run.status !== 'needs_attention') actions.append(button('Pause', () => control(run, 'pause')));
        actions.append(button('Cancel', () => control(run, 'cancel')));
      }
      card.append(actions); return card;
    }) : [make('p', 'No runs yet. Run or schedule a saved workflow.', 'muted')]));
  }
  function batchFileRows() {
    const files = [...$('batch-files').files];
    printSteps = files.map((_, fileIndex) => ({fileIndex, useAms: false, amsSlot: 1, backupAmsSlot: null}));
    $('batch-file-settings').replaceChildren(...files.map(file => make('span', file.name, 'batch-file-chip')));
    $('build-repeats').disabled = files.length !== 1;
    renderPrintSteps();
  }
  function renderPrintSteps() {
    const files = [...$('batch-files').files];
    const sequence = $('batch-sequence');
    sequence.replaceChildren();
    printSteps.forEach((step, index) => {
      const row = make('div', undefined, 'print-step');
      const marker = make('span', String(index * 2 + 1).padStart(2, '0'), 'step-number');
      const body = make('div', undefined, 'print-step-body');
      body.append(make('strong', 'Print'));
      const choice = make('select'); choice.setAttribute('aria-label', `File for print ${index + 1}`);
      files.forEach((file, fileIndex) => choice.append(option(String(fileIndex), file.name)));
      choice.value = String(step.fileIndex);
      choice.onchange = () => { step.fileIndex = Number(choice.value); };
      body.append(choice);
      const amsLabel = make('label', undefined, 'print-ams');
      const ams = make('input'); ams.type = 'checkbox'; ams.checked = step.useAms;
      const slot = make('select'); slot.setAttribute('aria-label', `AMS Lite slot for print ${index + 1}`);
      for (let number = 1; number <= 4; number++) slot.append(option(String(number), `Slot ${number}`));
      slot.value = String(step.amsSlot || 1); slot.disabled = !step.useAms;
      slot.onchange = () => { step.amsSlot = Number(slot.value); };
      const backup = make('select'); backup.setAttribute('aria-label', `Backup AMS Lite slot for print ${index + 1}`);
      backup.append(option('', 'No backup'));
      for (let number = 1; number <= 4; number++) backup.append(option(String(number), `Slot ${number}`));
      backup.value = step.backupAmsSlot == null ? '' : String(step.backupAmsSlot);
      backup.disabled = !step.useAms;
      backup.onchange = () => { step.backupAmsSlot = backup.value ? Number(backup.value) : null; };
      ams.onchange = () => {
        step.useAms = ams.checked; slot.disabled = !ams.checked; backup.disabled = !ams.checked;
        if (!ams.checked) step.backupAmsSlot = null;
      };
      amsLabel.append(ams, make('span', 'Use AMS Lite for this print')); body.append(amsLabel);
      body.append(slot);
      const backupLabel = make('label', undefined, 'print-ams');
      backupLabel.append(make('span', 'Backup roll if the primary runs out')); body.append(backupLabel, backup);
      const actions = make('div', undefined, 'print-step-actions');
      for (const [label, offset] of [['Move up', -1], ['Move down', 1]]) {
        const move = make('button', label, 'secondary'); move.type = 'button';
        move.disabled = index + offset < 0 || index + offset >= printSteps.length;
        move.onclick = () => { [printSteps[index], printSteps[index + offset]] = [printSteps[index + offset], printSteps[index]]; renderPrintSteps(); };
        actions.append(move);
      }
      const duplicate = make('button', 'Repeat', 'secondary'); duplicate.type = 'button';
      duplicate.disabled = printSteps.length >= 8;
      duplicate.onclick = () => { printSteps.splice(index + 1, 0, {...step}); renderPrintSteps(); };
      const remove = make('button', 'Remove', 'secondary'); remove.type = 'button';
      remove.onclick = () => { printSteps.splice(index, 1); renderPrintSteps(); };
      actions.append(duplicate, remove); row.append(marker, body, actions); sequence.append(row);
      if (index < printSteps.length - 1) {
        const swap = make('div', undefined, 'swap-step');
        swap.append(make('span', String(index * 2 + 2).padStart(2, '0'), 'step-number'),
          make('span', 'Swap plate', 'swap-step-name'),
          make('small', 'Swap inside print file · no homing'));
        sequence.append(swap);
      }
    });
    $('batch-step-count').textContent = printSteps.length
      ? `${printSteps.length} print${printSteps.length === 1 ? '' : 's'} · ${Math.max(0, printSteps.length - 1)} swap${printSteps.length === 2 ? '' : 's'}`
      : 'Add a print step to begin.';
    const maxRuns = printSteps.length ? Math.floor(8 / printSteps.length) : 0;
    $('repeat-sequence').disabled = maxRuns < 2;
    $('sequence-repeat').disabled = maxRuns < 2;
    $('sequence-repeat').max = String(Math.max(2, maxRuns));
    if (maxRuns >= 2 && Number($('sequence-repeat').value) > maxRuns) $('sequence-repeat').value = String(maxRuns);
    $('sequence-repeat-help').textContent = maxRuns >= 2
      ? `Repeat this ${printSteps.length}-print sequence up to ${maxRuns} times. Swaps are added between every print.`
      : 'Up to eight prints total. A sequence of four prints or fewer can be repeated.';
  }
  const batchModes = new Map();
  const batchChecks = new Map();
  const startingBatches = new Set();
  let batchRevision = 0, batchRenderSignature = '';
  function renderBatches() {
    // Keep the actual button and its in-flight handler stable during polling.
    if (startingBatches.size) return;
    renderSwapPrints();
    updateDirectButtons();
    const signature = JSON.stringify(batches);
    if (signature === batchRenderSignature) return;
    batchRenderSignature = signature;
    const printBatches = batches.filter(batch => !batch.jobs.some(job => job.kind === 'swap_test'));
    $('batch-list').replaceChildren(...(printBatches.length ? printBatches.map(batch => {
      const card = make('article', undefined, 'card');
      card.id = `batch-${batch.id}`;
      const state = batch.state.replaceAll('_', ' ');
      card.append(make('h3', `Batch ${batch.id.slice(0, 8)} · ${state}`),
        make('p', `${batch.jobs.length} prints · Prepared ${when(batch.created * 1000)}`));
      if (batch.note) card.append(make('p', batch.note, 'batch-note'));
      card.append(make('p', batch.auto_continue
        ? 'Automatic sequence: the next print starts after confirmed completion of the print and embedded swap.'
        : 'Manual handoff: check the plate between prints.', 'fine'));
      if (batch.sequence_sha256) card.append(make('small', `Swap sequence SHA-256 ${batch.sequence_sha256}`));
      if (batch.runner_stale) card.append(make('p', 'The print runner has not reported for over five minutes. Inspect the printer before taking another action.', 'batch-note'));
      for (const job of batch.jobs) {
        const row = make('div', undefined, 'batch-job');
        const progress = job.progress == null ? '' : ` · ${Math.round(job.progress)}%`;
        const feed = job.use_ams ? ` · AMS Lite slot ${job.ams_slot || 'automatic'}${job.backup_ams_slot ? ` → backup slot ${job.backup_ams_slot}` : ''}` : ' · external spool';
        row.append(make('strong', `${job.position + 1}. Print ${job.label}`),
          make('span', `${job.state}${progress}${feed}`));
        card.append(row);
        const artifact = make('div', undefined, 'batch-artifact');
        const download = make('a', 'Download prepared file');
        download.href = appPath(`/v1/printers/a1/batches/${batch.id}/jobs/${job.id}/prepared`);
        artifact.append(download, make('small', `SHA-256 ${job.sha256 || 'unavailable'}`));
        card.append(artifact);
        if (job.note) card.append(make('p', job.note, 'batch-note'));
        if (job.swap_enabled) {
          card.append(make('div', batch.auto_continue
            ? '↓ Swap inside print file · no homing · confirm completion · next print automatically'
            : '↓ Swap inside print file · no homing · check new plate · continue', 'batch-swap-event'));
        }
      }
      if (batch.state === 'staged' || batch.state === 'waiting_for_plate') {
        const modeLabel = make('label', 'Between prints ', 'field');
        const mode = make('select');
        mode.setAttribute('aria-label', `Between prints for batch ${batch.id.slice(0, 8)}`);
        for (const [value, label] of [['automatic', 'Continue automatically'], ['manual', 'Wait for a plate check']]) {
          const option = make('option', label); option.value = value; mode.append(option);
        }
        mode.value = batchModes.get(batch.id) || (batch.state === 'staged' || batch.auto_continue ? 'automatic' : 'manual');
        mode.onchange = () => batchModes.set(batch.id, mode.value);
        modeLabel.append(mode);
        card.append(modeLabel, make('p', 'Automatic mode uses printer completion reports; plate seating is not detected by a sensor. Load enough plates and filament for the whole sequence.', 'fine'));
        const check = make('label', undefined, 'batch-check');
        const checkbox = make('input'); checkbox.type = 'checkbox';
        const checkKey = `${batch.id}:${batch.state}`;
        checkbox.checked = batchChecks.get(checkKey) || false;
        checkbox.onchange = () => batchChecks.set(checkKey, checkbox.checked);
        check.append(checkbox, make('span', batch.state === 'staged'
          ? 'The first plate is seated, the fixture is clear, and plates and filament are ready for this sequence.'
          : 'The swap motion has finished. The new plate is seated and the fixture is clear.'));
        card.append(check);
        const actions = make('div', undefined, 'card-actions');
        const startLabel = batch.state === 'waiting_for_plate' ? 'Continue to next print'
          : batch.note?.startsWith('upload failed before print start:') ? 'Retry upload and start' : 'Start batch';
        actions.append(button(startLabel, async () => {
          if (!checkbox.checked) throw Error('Complete the operator check before starting.');
          if (startingBatches.has(batch.id)) return;
          startingBatches.add(batch.id);
          batchRevision += 1;
          try {
            await api(`/v1/printers/a1/batches/${batch.id}/start`, {
              operator_present: true, plate_checked: batch.state === 'waiting_for_plate',
              auto_continue: mode.value === 'automatic',
            });
            batchChecks.delete(checkKey);
            batch.state = 'running';
            batch.auto_continue = mode.value === 'automatic';
          } finally { startingBatches.delete(batch.id); batchRevision += 1; }
          renderBatches();
          note('Runner started. Watch printer status and this batch.'); await refresh();
        }, 'primary'));
        actions.append(button('Cancel waiting prints', async () => {
          await api(`/v1/printers/a1/batches/${batch.id}/control`, {action: 'cancel'});
          await refresh();
        }));
        card.append(actions);
      } else if (batch.state === 'running') {
        const actions = make('div', undefined, 'card-actions');
        for (const action of ['pause', 'resume', 'cancel']) {
          actions.append(button(action === 'cancel' ? 'Stop print and batch' : `${action[0].toUpperCase()}${action.slice(1)} print`, async () => {
            await api(`/v1/printers/a1/batches/${batch.id}/control`, {action});
            note(`${action} requested. Watch the printer for the actual state.`); await refresh();
          }));
        }
        card.append(actions);
      } else if (batch.state === 'needs_attention') {
        const check = make('label', undefined, 'batch-check');
        const checkbox = make('input'); checkbox.type = 'checkbox';
        check.append(checkbox, make('span', 'I inspected the printer and plate and know whether this print finished or stopped.'));
        card.append(check);
        const actions = make('div', undefined, 'card-actions');
        for (const [label, outcome] of [['Print and swap finished', 'finished'], ['Print stopped; cancel batch', 'stopped']]) {
          actions.append(button(label, async () => {
            if (!checkbox.checked) throw Error('Inspect the printer and plate before resolving.');
            await api(`/v1/printers/a1/batches/${batch.id}/resolve`, {outcome, inspected: true});
            note('Batch updated after your inspection.'); await refresh();
          }));
        }
        card.append(actions);
      }
      if (['done', 'canceled'].includes(batch.state) && batch.jobs.length) {
        const repeat = make('div', undefined, 'batch-repeat');
        const count = make('input'); count.type = 'number'; count.min = '1';
        count.max = String(Math.floor(8 / batch.jobs.length));
        count.value = String(batchRepeatCounts.get(batch.id) || 1);
        count.id = `repeat-count-${batch.id}`;
        count.oninput = () => batchRepeatCounts.set(batch.id, count.value);
        const label = make('label', 'Sequence runs'); label.htmlFor = count.id;
        const status = make('p', '', 'batch-form-status'); status.setAttribute('role', 'status');
        const prepare = button('Repeat sequence', async () => {
          const runs = Number(count.value);
          if (!Number.isInteger(runs) || runs < 1 || runs * batch.jobs.length > 8) {
            throw Error(`Choose 1 to ${count.max} sequence runs, up to eight prints total.`);
          }
          status.textContent = `Preparing ${runs * batch.jobs.length} prints from saved files...`;
          status.classList.remove('error');
          try {
            const result = await api(`/v1/printers/a1/batches/${batch.id}/repeat`, {count: runs});
            await refresh();
            note(`Prepared ${result.jobs.length} prints. Review the new batch before starting.`);
            $(`batch-${result.id}`)?.scrollIntoView({behavior: 'smooth', block: 'start'});
          } catch (error) {
            status.textContent = error.message; status.classList.add('error'); throw error;
          }
        }, 'primary');
        prepare.disabled = !session?.scopes.includes('jobs:write');
        repeat.append(label, count, prepare,
          make('span', `${batch.jobs.length} prints per run · keeps file order and AMS slots · uses the active swap sequence`, 'fine'));
        card.append(repeat, status);
      }
      if (['staged', 'done', 'canceled'].includes(batch.state)) {
        const actions = make('div', undefined, 'card-actions');
        actions.append(button('Delete batch', async () => {
          await api(`/v1/printers/a1/batches/${batch.id}`, undefined, 'DELETE');
          note('Print batch deleted.'); await refresh();
        }));
        card.append(actions);
      }
      return card;
    }) : [make('p', 'No print batches yet. Build a print sequence above.', 'muted')]));
  }
  async function loadSwapSequence(resetEditor = false) {
    const value = await api('/v1/printers/a1/swap-sequence');
    $('swap-gcode').textContent = value.gcode;
    if (resetEditor || !$('swap-test-gcode').value || $('swap-test-gcode').value === loadedSwapGcode) $('swap-test-gcode').value = value.gcode;
    loadedSwapGcode = value.gcode;
    activeSwapId = value.active_trial_id || '';
    activeSwapHash = value.sequence_sha256 || '';
    $('active-swap').textContent = value.active_trial_id
      ? `Active sequence ${value.active_trial_id.slice(0, 8)} · commands ${activeSwapHash.slice(0, 12)}. Swap plate, exact active print tests, and newly prepared batches use this same block. Older saved tests may use different moves.`
      : 'No swap sequence is active. Run and verify a swap-only test before preparing multiple prints.';
    renderSwapTrials();
    updateDirectButtons();
  }
  function renderSwapPrints() {
    const tests = batches.filter(batch => batch.jobs.some(job => job.kind === 'swap_test'));
    $('swap-print-list').replaceChildren(...tests.map(test => {
      const job = test.jobs[0];
      const card = make('article', undefined, 'card swap-review');
      card.id = `swap-print-${test.id}`;
      const status = test.state === 'done' ? 'File finished — inspect the physical swap'
        : test.state === 'running' && job.state === 'starting' ? 'Starting test — waiting for printer'
        : test.state.replaceAll('_', ' ');
      card.append(make('h3', `${job.label} · ${status}`),
        make('p', `No material used · ${job.state}${job.progress == null ? '' : ` · ${Math.round(job.progress)}%`}`, 'fine'));
      if (job.note || test.note) card.append(make('p', job.note || test.note, 'batch-note'));
      const download = make('a', 'Download test file');
      download.href = appPath(`/v1/printers/a1/batches/${test.id}/jobs/${job.id}/prepared`);
      card.append(download);
      const actions = make('div', undefined, 'card-actions');
      if (test.state === 'staged') {
        const start = button('Start print-mode test', async () => {
          await api(`/v1/printers/a1/batches/${test.id}/start`, {operator_present: true});
          await refresh();
        }, 'primary');
        start.disabled = !printerReady || !printerHomed || !session?.scopes.includes('jobs:write');
        actions.append(start);
      }
      if (test.state === 'running') {
        for (const action of (job.state === 'running' ? ['pause', 'resume', 'cancel'] : ['cancel'])) {
          actions.append(button(action === 'cancel' ? 'Stop test' : `${action[0].toUpperCase()}${action.slice(1)} test`, async () => {
            await api(`/v1/printers/a1/batches/${test.id}/control`, {action}); await refresh();
          }));
        }
      }
      if (test.state === 'needs_attention') {
        actions.append(button('Printer stopped — close test', async () => {
          await api(`/v1/printers/a1/batches/${test.id}/resolve`, {outcome: 'stopped', inspected: true});
          await refresh();
        }));
      }
      if (['staged', 'done', 'canceled'].includes(test.state)) {
        actions.append(button('Delete test', async () => {
          await api(`/v1/printers/a1/batches/${test.id}`, undefined, 'DELETE'); await refresh();
        }));
      }
      card.append(actions);
      return card;
    }));
  }
  function renderSwapTrials() {
    const list = swapTrials.slice(0, 6).map(trial => {
      const card = make('article', undefined, 'card swap-review');
      if (activeSwapHash) card.append(make('p', trial.sha256 === activeSwapHash ? 'Commands match the active print-batch sequence.' : 'Different commands from the active print-batch sequence.', 'fine'));
      card.append(make('strong', `Test ${trial.id.slice(0, 8)} · ${trial.state.replaceAll('_', ' ')}${trial.active ? ' · active' : ''}`));
      card.append(make('p', `Saved ${new Date(trial.created * 1000).toLocaleString()} · SHA-256 ${trial.sha256.slice(0, 12)}${trial.note ? ` · ${trial.note}` : ''}`, 'fine'));
      const actions = make('div', undefined, 'card-actions');
      if (trial.state === 'validated') {
        const run = button('Run swap-only test', async () => {
          await api(`/v1/printers/a1/swap-trials/${trial.id}/start`, {operator_present: true});
          note('Swap-only test requested. Watch the A1 and inspect the plate motion.'); await refresh();
        }, 'primary');
        run.disabled = !printerReady || !printerHomed || !session?.scopes.includes('jobs:write');
        actions.append(run);
      }
      if (['accepted_unverified', 'unknown'].includes(trial.state)) {
        for (const [label, outcome] of [['Motion worked', 'verified'], ['Motion failed', 'failed']]) {
          actions.append(button(label, async () => {
            await api(`/v1/printers/a1/swap-trials/${trial.id}/resolve`, {outcome, inspected: true});
            note(`Swap test marked ${outcome}.`); await refresh();
          }, outcome === 'verified' ? 'primary' : 'secondary'));
        }
      }
      if (['verified', 'configured'].includes(trial.state) && !trial.active) {
        actions.append(button('Use for print batches', async () => {
          await api(`/v1/printers/a1/swap-trials/${trial.id}/activate`, {});
          await loadSwapSequence(); await refresh(); note('Swap sequence activated.');
        }, 'primary'));
      }
      if (['validated', 'verified', 'configured', 'failed'].includes(trial.state)) {
        actions.append(button('Delete test', async () => {
          await api(`/v1/printers/a1/swap-trials/${trial.id}`, undefined, 'DELETE');
          await loadSwapSequence(); await refresh(); note('Swap test deleted.');
        }));
      }
      if (actions.children.length) card.append(actions);
      return card;
    });
    $('swap-test-list').replaceChildren(...(list.length ? list : [make('p', 'Save a swap test to begin.', 'muted')]));
  }
  async function showRun(run) {
    selectedRun = run.id;
    const [current, events] = await Promise.all([api(`/v1/workflow-runs/${run.id}`), api(`/v1/workflow-runs/${run.id}/events`)]);
    $('detail-title').textContent = current.name;
    $('detail-status').textContent = `${current.status.replaceAll('_', ' ')} · Started ${when(current.start_at)}${current.error ? ` · ${current.error}` : ''}`;
    $('detail-steps').replaceChildren(...current.steps.map(step => make('li', `${step.step.id}: ${step.step.kind === 'wait' ? 'Wait' : step.step.kind === 'condition' ? 'Wait for print finish' : step.step.action} · ${step.status}${step.due_at ? ` · Due ${when(step.due_at)}` : ''}`)));
    $('detail-events').replaceChildren(...events.map(event => make('li', `${when(event.created_at)} · ${event.type.replaceAll('_', ' ')}${event.step_id ? ` · ${event.step_id}` : ''}`)));
    $('run-detail').hidden = false; $('run-detail').scrollIntoView({behavior: 'smooth'});
  }
  async function refresh() {
    if (busy || document.hidden) return;
    busy = true; $('refresh').disabled = true;
    const revision = batchRevision;
    const results = await Promise.allSettled([api('/v1/printers/a1/status'), api('/v1/workflows'), api('/v1/workflow-runs'), api('/v1/workflows/health'), api('/v1/home/devices'), api('/v1/printers/a1/batches'), api('/v1/printers/a1/swap-trials'), api('/v1/workflow-schedules'), api('/v1/workflow-triggers')]);
    const [p, f, r, h, d, b, t, s, g] = results;
    if (p.status === 'fulfilled') printer(p.value); else printer({configured: true, online: false, message: p.reason.message});
    if (s.status === 'fulfilled') schedules = s.value;
    if (g.status === 'fulfilled') triggers = g.value;
    if (d.status === 'fulfilled') devices = d.value;
    if (f.status === 'fulfilled') flows = f.value;
    if (r.status === 'fulfilled') { runs = r.value; renderRuns(); }
    if (f.status === 'fulfilled') renderFlows();
    if (h.status === 'fulfilled') $('worker-metric').textContent = h.value.worker_online ? 'Online' : 'Offline';
    if (b.status === 'fulfilled' && revision === batchRevision && !startingBatches.size) { batches = b.value; renderBatches(); }
    if (t.status === 'fulfilled') { swapTrials = t.value; renderSwapTrials(); }
    const failed = results.filter((result, index) => index !== 0 && result.status === 'rejected');
    if (failed.length) note(failed[0].reason.message, true);
    else if (p.status === 'rejected') note('Printer status could not be loaded; workflows are still available.', true);
    else note(`Updated ${new Date().toLocaleTimeString()}.`);
    $('refresh').disabled = false; busy = false;
  }
  $('actions-heading').closest('.builder-section-head').querySelector('p').textContent = 'Build timeline steps. Add multiple actions to a step to run them together.';
  $('add-step').textContent = '+ Add timeline step';
  $('add-step').onclick = () => stageRow();
  const printCleanupTemplate = button('Use print air-cleaning example', () => {
    const purifier = devices.find(device => device.control_enabled && device.load_type === 'air_purifier');
    if (!purifier) throw Error('Set up an enabled air purifier device before using this example.');
    const hasDraft = $('flow-name').value.trim() || $('steps').children.length > 1 || $('schedule-drafts').children.length;
    if (hasDraft && !window.confirm('Replace the current workflow draft with the print air-cleaning example?')) return;
    editingFlow = null;
    $('flow-name').value = 'Print air cleaning';
    $('steps').replaceChildren(); $('trigger-drafts').replaceChildren(); $('schedule-drafts').replaceChildren();
    stageRow([{kind: 'action', action: 'home.set', inputs: {device_id: purifier.id, on: true}, delay_seconds: 0}]);
    stageRow([{kind: 'condition', condition: 'printer.not_printing', inputs: {}, delay_seconds: 0}]);
    stageRow([{kind: 'wait', inputs: {}, delay_seconds: 600}]);
    stageRow([{kind: 'action', action: 'home.set', inputs: {device_id: purifier.id, on: false}, delay_seconds: 0}]);
    triggerDraftRow({kind: 'printer.print_started'});
    $('builder-heading').textContent = 'Build a workflow'; $('save-workflow').textContent = 'Save complete workflow'; $('cancel-edit').hidden = true;
    note(`Example added: print starts → ${purifier.name} on → print stops → wait 10 minutes → ${purifier.name} off.`);
  });
  printCleanupTemplate.id = 'print-cleanup-template';
  $('add-step').insertAdjacentElement('afterend', printCleanupTemplate);
  $('add-trigger').onclick = () => triggerDraftRow();
  $('add-schedule').onclick = () => scheduleDraftRow();
  $('cancel-edit').onclick = resetBuilder;
  async function directCommand(action) {
    if (directBusy) return;
    directBusy = true; updateDirectButtons();
    const label = action === 'home' ? 'Homing' : 'Plate swap';
    $('direct-command-status').textContent = `${label} requested. Waiting for the printer…`;
    try {
      const result = await api('/v1/printers/a1/control', {action});
      const message = `${label} ${result.state} by printer. Check physical motion.`;
      $('direct-command-status').textContent = message;
      note(message);
      await refresh();
    } catch (error) {
      $('direct-command-status').textContent = error.message;
      note(error.message, true);
    } finally { directBusy = false; updateDirectButtons(); }
  }
  $('home-axes').onclick = () => directCommand('home');
  $('direct-swap').onclick = () => directCommand('swap');
  $('run-swap-print').onclick = async () => {
    if (swapPrintBusy) return;
    swapPrintBusy = true; updateDirectButtons();
    $('swap-print-status').textContent = 'Preparing a motion-only print file...';
    try {
      const test = await api('/v1/printers/a1/swap-print-tests', {
        source: $('swap-print-source').value,
        ...($('swap-print-source').value === 'editor' ? {gcode: $('swap-test-gcode').value} : {}),
        motion_profile: $('swap-print-profile').value,
      });
      $('swap-print-status').textContent = 'Prepared. Requesting print-mode test...';
      await api(`/v1/printers/a1/batches/${test.id}/start`, {operator_present: true});
      $('swap-print-status').textContent = 'Uploading and starting the test. Watch the motion; use Stop test below if needed.';
    } catch (error) {
      $('swap-print-status').textContent = error.message; note(error.message, true);
    } finally {
      swapPrintBusy = false; await refresh(); updateDirectButtons();
    }
  };
  $('load-active-swap').onclick = async () => {
    try { await loadSwapSequence(true); renderSwapTrials(); note('Active sequence loaded into the editor.'); }
    catch (error) { note(error.message, true); }
  };
  $('save-swap-test').onclick = async () => {
    const control = $('save-swap-test'); control.disabled = true;
    $('swap-test-status').textContent = 'Validating and saving the moves...';
    try {
      const trial = await api('/v1/printers/a1/swap-trials', {gcode: $('swap-test-gcode').value});
      $('swap-test-status').textContent = `Test ${trial.id.slice(0, 8)} saved with ${trial.moves} moves. Run it below when the printer is ready.`;
      await refresh();
    } catch (error) { $('swap-test-status').textContent = error.message; note(error.message, true); }
    finally { control.disabled = false; }
  };
  $('batch-files').onchange = batchFileRows;
  $('build-repeats').onclick = () => {
    const count = Number($('batch-repeat').value);
    if ($('batch-files').files.length !== 1 || !Number.isInteger(count) || count < 1 || count > 8) {
      note('Choose one sliced file and a repeat count from 1 to 8.', true); return;
    }
    printSteps = Array.from({length: count}, () => ({fileIndex: 0, useAms: false, amsSlot: 1, backupAmsSlot: null}));
    renderPrintSteps();
  };
  $('repeat-sequence').onclick = () => {
    const runs = Number($('sequence-repeat').value);
    if (!printSteps.length || !Number.isInteger(runs) || runs < 2 || printSteps.length * runs > 8) {
      note('Choose at least two sequence runs, up to eight prints total.', true); return;
    }
    const original = printSteps;
    printSteps = Array.from({length: runs}, () => original.map(step => ({...step}))).flat();
    renderPrintSteps();
    note(`Built ${runs} sequence runs with ${printSteps.length} prints. Each step can be edited.`);
  };
  $('batch-add-print').onclick = () => {
    if (!$('batch-files').files.length || printSteps.length >= 8) {
      note('Choose a sliced file and keep the sequence to eight prints or fewer.', true); return;
    }
    printSteps.push({fileIndex: 0, useAms: false, amsSlot: 1, backupAmsSlot: null}); renderPrintSteps();
  };
  $('batch-form').onsubmit = async event => {
    event.preventDefault();
    const files = [...$('batch-files').files];
    if (!files.length || files.length > 8 || !printSteps.length || printSteps.length > 8) {
      note('Choose one to eight sliced files and build one to eight print steps.', true); return;
    }
    const metadata = printSteps.map(step => ({
      file_index: step.fileIndex, use_ams: step.useAms,
      ams_slot: step.useAms ? step.amsSlot : null,
      backup_ams_slot: step.useAms ? step.backupAmsSlot : null,
    }));
    const form = new FormData();
    files.forEach(file => form.append('files', file, file.name));
    form.append('metadata', JSON.stringify(metadata));
    $('stage-batch').disabled = true;
    $('batch-form-status').textContent = `Preparing ${printSteps.length} print${printSteps.length === 1 ? '' : 's'} for review…`;
    $('batch-form-status').classList.remove('error');
    try {
      const response = await fetch(appPath('/v1/printers/a1/batches'), {
        method: 'POST', credentials: 'same-origin',
        headers: {'X-CSRF-Token': session.csrf_token}, body: form,
      });
      if (response.status === 401) { location.assign(appPath('/login')); return; }
      const result = await response.json();
      if (!response.ok) throw Error(result.error?.message || 'Could not prepare print batch.');
      $('batch-form').reset(); printSteps = []; $('batch-file-settings').replaceChildren(); renderPrintSteps();
      $('build-repeats').disabled = true;
      await refresh();
      $('batch-form-status').textContent = `Prepared ${result.jobs.length} prints. Review the sequence below before starting.`;
      $('batch-list').scrollIntoView({behavior: 'smooth', block: 'start'});
    } catch (error) {
      $('batch-form-status').textContent = error.message;
      $('batch-form-status').classList.add('error');
      note(error.message, true);
    }
    finally { $('stage-batch').disabled = false; }
  };
  $('builder').onsubmit = async event => {
    event.preventDefault();
    const submit = event.submitter || $('save-workflow'); submit.disabled = true;
    try {
      const spec = specFromForm();
      const triggerSpecs = [...$('trigger-drafts').children].map(row => row.definition());
      const scheduleSpecs = [...$('schedule-drafts').children].map(row => row.definition());
      const triggerKey = item => {
        const trigger = item.trigger || item;
        return JSON.stringify([trigger.kind, trigger.device_id || '', trigger.field || '', trigger.operator || 'equals', trigger.value, item.start_step_id || '']);
      };
      if (new Set(triggerSpecs.map(triggerKey)).size !== triggerSpecs.length) throw Error('Remove duplicate state triggers before saving.');
      const previousDefinition = editingFlow;
      let definition;
      if (previousDefinition) {
        definition = await api(`/v1/workflows/${previousDefinition.id}/versions`, {
          spec, expected_version: previousDefinition.version, idempotency_key: crypto.randomUUID(),
        });
      } else {
        definition = await api('/v1/workflows', {spec, expected_version: 0, idempotency_key: crypto.randomUUID()});
      }
      editingFlow = definition;
      const currentTriggers = previousDefinition ? (await api('/v1/workflow-triggers')).filter(item => item.definition_id === definition.id) : [];
      const desiredTriggerKeys = new Set(triggerSpecs.map(triggerKey));
      for (const item of currentTriggers) {
        if (!desiredTriggerKeys.has(triggerKey(item))) await api(`/v1/workflow-triggers/${item.id}?expected_version=${item.version}`, undefined, 'DELETE');
      }
      const existingTriggerKeys = new Set(currentTriggers.map(triggerKey));
      for (const item of triggerSpecs) {
        if (!existingTriggerKeys.has(triggerKey(item))) await api(`/v1/workflows/${definition.id}/triggers`, {
          definition_version: definition.version, trigger: item.trigger, start_step_id: item.start_step_id,
          idempotency_key: crypto.randomUUID(),
        });
      }
      if (previousDefinition) {
        const currentSchedules = (await api('/v1/workflow-schedules')).filter(item => item.definition_id === definition.id);
        for (const item of currentSchedules) await api(`/v1/workflow-schedules/${item.id}?expected_version=${item.version}`, undefined, 'DELETE');
      }
      for (const schedule of scheduleSpecs) {
        if (schedule.frequency === 'once') await api(`/v1/workflows/${definition.id}/runs`, {
          definition_version: definition.version, start_at: schedule.start_at, idempotency_key: crypto.randomUUID(),
        });
        else await api(`/v1/workflows/${definition.id}/schedules`, {
          definition_version: definition.version, frequency: schedule.frequency, start_at: schedule.start_at,
          time_zone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC', idempotency_key: crypto.randomUUID(),
        });
      }
      const launchCount = triggerSpecs.length + scheduleSpecs.length;
      resetBuilder();
      note(`${definition.spec.name} saved with ${definition.spec.steps.length} action${definition.spec.steps.length === 1 ? '' : 's'} and ${launchCount} launch rule${launchCount === 1 ? '' : 's'}.`); await refresh();
    } catch (error) { note(error.message, true); }
    finally { submit.disabled = false; }
  };
  $('close-detail').onclick = () => { $('run-detail').hidden = true; selectedRun = null; };
  $('refresh').onclick = refresh;
  async function init() {
    try {
      session = await api('/auth/session');
      await refresh(); resetBuilder();
      loadSwapSequence().catch(error => { $('swap-gcode').textContent = error.message; });
      setInterval(refresh, 20000);
      document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    } catch (error) { note(error.message, true); }
  }
  init();
})();
