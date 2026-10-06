'use strict';
(() => {
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const panel = node('section', undefined, 'pc-continuity');
  panel.id = 'pc-continuity';
  panel.innerHTML = `<div class="pc-section-heading"><div><h3>Upcoming work & decisions</h3><p>Questions, queued requests, and scheduled work stay with this project.</p></div></div>
    <p class="pc-continuity-status" role="status"></p><div class="pc-waits"></div><div class="pc-queued"></div><div class="pc-continue"></div><div class="pc-schedules"></div>
    <details class="pc-schedule-editor"><summary>Schedule work</summary><form class="pc-schedule-form">
      <label>Schedule name<input name="name" required maxlength="160" placeholder="Weekly supplier review"></label>
      <label>Request<textarea name="instruction" required maxlength="16000" rows="3"></textarea></label><div class="pc-schedule-draft pc-draft-status"></div>
      <div class="pc-schedule-fields"><label>Repeat<select name="kind" aria-label="Repeat"><option value="once">Once</option><option value="daily">Daily</option><option value="weekly">Weekly</option></select></label>
      <label class="pc-once">Run at<input name="run_at" type="datetime-local" required></label>
      <label class="pc-recurring" hidden>Local time<input name="local_time" type="time" value="09:00"></label>
      <label class="pc-recurring" hidden>Time zone<input name="timezone" required maxlength="100"></label></div>
      <fieldset class="pc-weekdays" hidden><legend>Days</legend></fieldset>
      <div class="pc-schedule-fields"><label>Assign to<select name="agent_id" aria-label="Assign to"><option value="">Project team</option></select></label>
      <label>Model budget per run ($)<input name="model_budget_usd" type="number" required min="0.01" step="0.01"></label>
      <label>Maximum runs<input name="max_runs" type="number" min="1" max="100" value="1" required></label></div>
      <p class="pc-schedule-help">Recurring times use the selected time zone; one-time dates use your device time zone. A skipped daylight-saving time runs on the next matching day; repeated times run once. Work waits for the current request to finish. Each schedule has a finite run limit and model budget; configured model rates are required.</p>
      <button class="pc-button" type="submit">Create schedule</button><p class="pc-schedule-status" role="status"></p>
    </form></details>`;
  const local = (selector) => panel.querySelector(selector);
  const waits = local('.pc-waits');
  const form = local('form');
  const field = (name) => form.elements.namedItem(name);
  let projectId = null,
    snapshot = null,
    refresh = null,
    profileName = (id) => id;
  let epoch = 0,
    busy = false,
    draftBinding = null;
  const replies = new Map();
  const replyBindings = new Map();
  let scheduleSubmission = null;
  const date = (value) => (value ? new Date(value).toLocaleString() : '');
  const writable = () => session?.scopes?.includes('jobs:write');
  const endpoint = (path) => '/v1/projects/' + encodeURIComponent(projectId) + path;
  function button(label, callback) {
    const item = node('button', label, 'pc-button');
    item.type = 'button';
    item.disabled = !writable();
    item.onclick = async () => {
      if (busy) return;
      const current = epoch;
      busy = true;
      item.disabled = true;
      local('.pc-continuity-status').textContent = '';
      try {
        await callback();
        if (current === epoch) await refresh?.();
      } catch (error) {
        if (current === epoch) local('.pc-continuity-status').textContent = error.message;
      } finally {
        busy = false;
        if (item.isConnected) item.disabled = !writable();
      }
    };
    return item;
  }
  function render() {
    const data = snapshot?.continuity;
    panel.hidden = !data;
    if (!data) return;
    const fingerprint = JSON.stringify([projectId, data, snapshot.state?.version, writable()]);
    if (panel.dataset.fingerprint === fingerprint) return;
    panel.dataset.fingerprint = fingerprint;
    const currentWaits = (data.waits || []).filter((item) => item.status === 'waiting');
    for (const child of [...waits.children])
      if (!currentWaits.some((item) => item.id === child.dataset.wait)) {
        replyBindings.get(child.dataset.wait)?.detach();
        replyBindings.delete(child.dataset.wait);
        child.remove();
      }
    for (const wait of currentWaits) {
      let card = [...waits.children].find((item) => item.dataset.wait === wait.id);
      if (card) continue; // Keep an in-progress answer and its focus across polling.
      card = node('article', undefined, 'pc-wait');
      card.dataset.wait = wait.id;
      card.append(node('strong', 'The team needs your input'), node('p', wait.question));
      const replyForm = node('form');
      const label = node('label', 'Your reply');
      const input = node('textarea');
      input.required = true;
      input.maxLength = 8000;
      input.rows = 3;
      input.value = replies.get(wait.id) || '';
      label.append(input);
      const draftStatus = node('div', undefined, 'pc-draft-status');
      const submit = node('button', 'Send reply', 'pc-button primary');
      submit.type = 'submit';
      submit.disabled = !writable();
      const status = node('p');
      status.setAttribute('role', 'status');
      replyForm.append(label, draftStatus, submit, status);
      input.oninput = () => replies.set(wait.id, input.value);
      const binding = window.SimonProjectDrafts?.bind(input, {
        projectId,
        key: 'reply-' + wait.id,
        statusTarget: draftStatus,
      });
      replyBindings.set(wait.id, binding);
      const id = projectId,
        current = epoch;
      let replySubmission = null;
      replyForm.onsubmit = async (event) => {
        event.preventDefault();
        if (submit.disabled) return;
        const text = input.value;
        submit.disabled = true;
        let accepted = false;
        try {
          if (replySubmission?.text !== text) replySubmission = { text, key: crypto.randomUUID() };
          await api('/v1/projects/' + encodeURIComponent(id) + '/waits/' + wait.id + '/reply', {
            expected_version: wait.version,
            message: text.trim(),
            idempotency_key: replySubmission.key,
          });
          accepted = true;
          replySubmission = null;
          window.SimonProjectDrafts?.clearAccepted(id, 'reply-' + wait.id, text);
          binding?.detach();
          replies.delete(wait.id);
          if (current === epoch) {
            status.textContent = 'Reply saved. The follow-up is queued.';
            await refresh?.();
          }
        } catch (error) {
          if (current === epoch)
            status.textContent = accepted
              ? 'Reply saved. Refresh the project to see its status.'
              : error.message + ' Your reply has been kept.';
        } finally {
          if (current === epoch && submit.isConnected) submit.disabled = accepted || !writable();
        }
      };
      card.append(replyForm);
      waits.append(card);
    }
    const queued = local('.pc-queued');
    queued.replaceChildren();
    const requests = (data.requests || []).filter((item) =>
      ['queued', 'blocked'].includes(item.status),
    );
    if (requests.length) {
      queued.append(node('h4', 'Queued requests'));
      for (const request of requests) {
        const row = node('div', undefined, 'pc-upcoming-row');
        const instruction = request.instruction.replace(
          /Reply to saved question [0-9a-f-]{36}:/gi,
          'Your reply:',
        );
        row.append(
          node('p', instruction.length > 500 ? instruction.slice(0, 500) + '...' : instruction),
          node(
            'small',
            (request.agent_id ? profileName(request.agent_id) : 'Project team') +
              ' · Queued ' +
              date(request.created_at),
          ),
        );
        if (request.status === 'blocked') {
          row.append(
            node(
              'p',
              request.last_error || 'This request needs attention before it can start.',
              'pc-skill-readiness',
            ),
          );
          if (writable())
            row.append(
              button('Retry queued request', () =>
                api(endpoint('/requests/' + request.id + '/retry'), {
                  expected_version: request.version,
                }),
              ),
            );
        }
        if (instruction.length > 500) {
          const full = node('details');
          full.append(node('summary', 'Read full request'), node('p', instruction));
          row.append(full);
        }
        if (writable())
          row.append(
            button('Cancel queued request', () =>
              api(endpoint('/requests/' + request.id + '/cancel'), {
                expected_version: request.version,
              }),
            ),
          );
        queued.append(row);
      }
    }
    const continuation = local('.pc-continue');
    continuation.replaceChildren();
    if (data.continuation?.available && writable()) {
      continuation.append(
        node('p', 'Continue unfinished work using the completed results already saved.'),
        button('Continue unfinished work', () =>
          api(endpoint('/continue'), {
            expected_version: snapshot.state.version,
            idempotency_key: crypto.randomUUID(),
          }),
        ),
      );
    }
    const schedules = local('.pc-schedules');
    schedules.replaceChildren();
    if (data.schedules?.length) schedules.append(node('h4', 'Scheduled work'));
    for (const schedule of data.schedules || []) {
      const row = node('article', undefined, 'pc-upcoming-row');
      const finished =
        schedule.runs_used >= schedule.max_runs ||
        (schedule.kind === 'once' && schedule.runs_used >= 1);
      row.append(
        node('strong', schedule.name),
        node(
          'small',
          [
            finished ? 'Finished' : schedule.enabled ? 'Enabled' : 'Paused',
            schedule.runs_used + ' of ' + schedule.max_runs + ' runs',
            schedule.next_run_at ? 'Next: ' + date(schedule.next_run_at) : null,
          ]
            .filter(Boolean)
            .join(' · '),
        ),
      );
      if (schedule.last_error) row.append(node('p', schedule.last_error, 'pc-skill-readiness'));
      if (!finished)
        row.append(
          button(schedule.enabled ? 'Pause schedule' : 'Enable schedule', () =>
            api(
              endpoint('/schedules/' + schedule.id),
              { expected_version: schedule.version, enabled: !schedule.enabled },
              'PATCH',
            ),
          ),
        );
      schedules.append(row);
    }
    if (
      !currentWaits.length &&
      !requests.length &&
      !data.schedules?.length &&
      !data.continuation?.available
    )
      queued.append(node('p', 'No queued requests or pending questions.', 'pc-upcoming-empty'));
    local('.pc-schedule-editor').hidden = !writable();
    const currentAgent = field('agent_id').value;
    field('agent_id').replaceChildren(new Option('Project team', ''));
    for (const id of snapshot.state?.team?.agent_ids || [])
      field('agent_id').append(new Option(profileName(id), id));
    if (currentAgent && !(snapshot.state?.team?.agent_ids || []).includes(currentAgent)) {
      const unavailable = new Option('Previously selected member (unavailable)', currentAgent);
      unavailable.disabled = true;
      field('agent_id').append(unavailable);
    }
    field('agent_id').value = currentAgent;
  }
  for (const [index, day] of ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].entries()) {
    const label = node('label', day);
    const input = node('input');
    input.type = 'checkbox';
    input.value = index;
    input.name = 'weekdays';
    label.prepend(input);
    local('.pc-weekdays').append(label);
  }
  function updateKind() {
    const once = field('kind').value === 'once';
    local('.pc-once').hidden = !once;
    field('run_at').required = once;
    for (const item of panel.querySelectorAll('.pc-recurring')) item.hidden = once;
    field('timezone').required = !once;
    local('.pc-weekdays').hidden = field('kind').value !== 'weekly';
    if (once) field('max_runs').value = '1';
  }
  field('kind').onchange = updateKind;
  form.onsubmit = async (event) => {
    event.preventDefault();
    const submit = form.querySelector('[type=submit]');
    if (submit.disabled) return;
    const current = epoch,
      id = projectId,
      text = field('instruction').value;
    const payload = {
      name: field('name').value.trim(),
      instruction: text.trim(),
      kind: field('kind').value,
      timezone: field('timezone').value,
      local_time: field('local_time').value,
      weekdays:
        field('kind').value === 'weekly'
          ? [...form.querySelectorAll('[name=weekdays]:checked')].map((item) => Number(item.value))
          : [],
      run_at: field('kind').value === 'once' ? new Date(field('run_at').value).toISOString() : null,
      agent_id: field('agent_id').value || null,
      model_budget_usd: Number(field('model_budget_usd').value),
      max_runs: Number(field('max_runs').value),
    };
    submit.disabled = true;
    local('.pc-schedule-status').textContent = 'Saving schedule…';
    let accepted = false;
    try {
      const content = JSON.stringify(payload);
      if (scheduleSubmission?.content !== content || scheduleSubmission?.projectId !== id)
        scheduleSubmission = { projectId: id, content, key: crypto.randomUUID() };
      await api(endpoint('/schedules'), { ...payload, idempotency_key: scheduleSubmission.key });
      scheduleSubmission = null;
      accepted = true;
      window.SimonProjectDrafts?.clearAccepted(id, 'schedule-request', text);
      if (current === epoch) {
        local('.pc-schedule-status').textContent = 'Schedule saved.';
        await refresh?.();
      }
    } catch (error) {
      if (current === epoch)
        local('.pc-schedule-status').textContent = accepted
          ? 'Schedule saved. Refresh the project to see its status.'
          : error.message + ' Your settings have been kept.';
    } finally {
      if (current === epoch) submit.disabled = false;
    }
  };
  function mount(target, id, detail, options = {}) {
    if (id !== projectId) {
      projectId = id;
      ++epoch;
      panel.dataset.fingerprint = '';
      for (const binding of replyBindings.values()) binding?.detach();
      replyBindings.clear();
      waits.replaceChildren();
      local('.pc-continuity-status').textContent = '';
      local('.pc-schedule-status').textContent = '';
      local('.pc-schedule-editor').open = false;
      form.reset();
      field('timezone').value = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
      draftBinding?.detach();
      draftBinding = window.SimonProjectDrafts?.bind(field('instruction'), {
        projectId: id,
        key: 'schedule-request',
        statusTarget: local('.pc-schedule-draft'),
      });
      updateKind();
    }
    snapshot = detail;
    refresh = options.refresh;
    profileName = options.profileName || ((value) => value);
    if (panel.parentElement !== target) target.append(panel);
    render();
    if (options.questionsTarget) {
      if (waits.parentElement !== options.questionsTarget) options.questionsTarget.append(waits);
      options.questionsTarget.hidden = !waits.childElementCount;
    }
  }
  window.SimonProjectContinuity = { mount };
})();
