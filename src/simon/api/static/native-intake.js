'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const dialog = $('ni-dialog');
  const bridge = () => window.SimonNativeProjects;
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const button = (text, callback, className = 'np-button np-quiet') => {
    const item = node('button', text, className);
    item.type = 'button';
    item.onclick = callback;
    return item;
  };
  const money = (value) => '$' + (Number(value || 0) / 1000000).toFixed(6).replace(/0{1,4}$/, '');
  const date = (value) => new Date(value).toLocaleString();
  const feedback = (message = '', tone = '') => {
    $('ni-feedback').textContent = message;
    $('ni-feedback').dataset.tone = tone;
  };
  let projectId, workspaceId, snapshot, modelSnapshot, pending, latest, timer;
  let generation = 0,
    version = 0,
    busy = false,
    dirty = false,
    uncertain = false;
  let currentRunId = null;
  let uploadQueue = [];
  let answers = {};
  const selection = new Set();
  const refreshedRuns = new Set();
  const path = (suffix = '') => '/v2/projects/' + projectId + '/intake' + suffix;
  const active = (ticket) =>
    ticket === generation &&
    dialog.open &&
    bridge().getProject()?.id === projectId &&
    bridge().getWorkspaceId() === workspaceId;
  const selectedRun = () => snapshot?.runs.find((run) => run.id === currentRunId);
  const running = () => snapshot?.runs.some((run) => run.status === 'running');

  function lock() {
    const writable = Boolean(snapshot?.can_manage);
    dialog.setAttribute('aria-busy', String(busy));
    $('ni-fields').disabled = busy || uncertain || !writable;
    for (const control of dialog.querySelectorAll('button')) control.disabled = busy;
    $('ni-save').hidden = !writable;
    $('ni-save').disabled = busy || uncertain || Boolean(latest) || !writable;
    $('ni-upload-controls').hidden = !writable;
    for (const control of [$('ni-files'), $('ni-folder')])
      control.disabled = busy || uncertain || Boolean(latest);
    $('ni-retry').disabled = busy;
    $('ni-uncertain').hidden = !uncertain;
    $('ni-close').disabled = busy || uncertain;
    $('ni-history').disabled = busy || uncertain;
    $('ni-rebase').disabled = busy || uncertain;
    for (const control of $('ni-sources').querySelectorAll('button, input'))
      control.disabled = busy || uncertain;
    const modelReady = Boolean(modelSnapshot?.routing?.ready);
    $('ni-models').disabled = busy || uncertain;
    $('ni-analyze').hidden = !writable;
    $('ni-analyze').disabled =
      busy || uncertain || Boolean(latest) || !writable || !modelReady || running();
    const run = selectedRun();
    $('ni-apply').hidden = !writable || run?.status !== 'ready';
    $('ni-apply').disabled = busy || uncertain || dirty || Boolean(latest);
    $('ni-cancel-run').hidden =
      !writable ||
      !run ||
      !['running', 'ready', 'questions', 'needs_revision', 'unknown'].includes(run.status);
    $('ni-cancel-run').disabled = busy || uncertain;
    $('ni-access').textContent = writable
      ? `Saved context version ${version}${dirty ? ' · Unsaved changes' : ''}`
      : 'Read only · An active project owner can update intake and plan the team.';
  }

  function modelStatus() {
    const routing = modelSnapshot?.routing;
    $('ni-model-status').textContent = routing?.ready
      ? `Planning: ${routing.planning_label || routing.planning_model_id}. Review: ${routing.review_label || routing.review_model_id}. Model calls use the project and workspace resource limits.`
      : routing?.reason ||
        'No planning models are ready. Open Models & usage to add and qualify a model, then configure project permissions and limits.';
    lock();
  }

  function collectAnswers() {
    for (const input of $('ni-answers').querySelectorAll('textarea[data-question]')) {
      if (input.value.trim()) answers[input.dataset.question] = input.value;
      else delete answers[input.dataset.question];
    }
  }

  function renderAnswers() {
    collectAnswers();
    const questions = snapshot.runs.find((run) => run.proposal)?.proposal.questions || [];
    $('ni-answers').replaceChildren();
    if (!questions.length && !Object.keys(answers).length) return;
    $('ni-answers').append(node('h4', 'Owner decisions and answers'));
    const allQuestions = [...questions];
    for (const key of Object.keys(answers))
      if (!questions.some((question) => question.key === key))
        allQuestions.push({
          key,
          question: 'Earlier answer: ' + key,
          why: 'Kept in project context. Clear this answer if it no longer applies.',
          blocking: false,
        });
    for (const question of allQuestions) {
      const field = node('div', undefined, 'np-field');
      const label = node(
        'label',
        question.question + (question.blocking ? ' (decision needed)' : ''),
      );
      const input = node('textarea');
      input.id = 'ni-answer-' + question.key;
      input.dataset.question = question.key;
      input.maxLength = 2000;
      input.rows = 3;
      input.value = answers[question.key] || '';
      label.htmlFor = input.id;
      field.append(label, node('p', question.why, 'np-field-help'), input);
      $('ni-answers').append(field);
    }
  }

  function populate() {
    const intake = snapshot.intake;
    for (const key of ['background', 'outcomes', 'constraints']) $('ni-' + key).value = intake[key];
    $('ni-auto').checked = intake.auto_staff;
    $('ni-answers').replaceChildren();
    answers = { ...intake.answers };
    version = intake.version;
    dirty = false;
    latest = null;
    $('ni-conflict').hidden = true;
    renderAnswers();
    modelStatus();
  }

  function showConflict() {
    latest = snapshot.intake;
    $('ni-latest').textContent =
      `Version ${latest.version}\nBackground: ${latest.background}\nOutcomes: ${latest.outcomes}\nConstraints: ${latest.constraints}\nAnswers: ${JSON.stringify(latest.answers, null, 2)}\nAutomatic staffing: ${latest.auto_staff ? 'On' : 'Off'}`;
    $('ni-conflict').hidden = false;
  }

  function sourceName(id) {
    const source = snapshot.sources.find((item) => item.id === id);
    return source ? `${source.source_key} · revision ${source.revision}` : id;
  }

  async function previewSource(source) {
    if (busy || uncertain) return;
    const ticket = generation;
    try {
      const result = await bridge().request(path('/sources/' + source.id + '/text'));
      if (!active(ticket)) return;
      $('ni-preview-heading').textContent =
        sourceName(source.id) + (result.truncated ? ' · text truncated' : '');
      $('ni-preview-text').textContent =
        result.text || 'No readable text was extracted from this file.';
      $('ni-preview').hidden = false;
    } catch (error) {
      if (active(ticket)) feedback(error.message, 'error');
    }
  }

  async function downloadSource(source) {
    const ticket = generation;
    try {
      const response = await fetch(appPath(path('/sources/' + source.id + '/content')), {
        credentials: 'same-origin',
        headers: { 'X-Workspace-ID': workspaceId },
      });
      if (!response.ok)
        throw Error('The original could not be downloaded. Refresh access and try again.');
      const blob = await response.blob();
      if (!active(ticket)) return;
      const url = URL.createObjectURL(blob);
      const link = node('a');
      link.href = url;
      link.download = source.filename;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (active(ticket)) feedback(error.message, 'error');
    }
  }

  function renderSources() {
    const latestIds = new Map();
    for (const source of snapshot.sources) {
      const previous = latestIds.get(source.source_key);
      if (!previous || previous.revision < source.revision)
        latestIds.set(source.source_key, source);
    }
    const eligible = new Set(
      [...latestIds.values()]
        .filter((source) => !source.revoked_at && source.extraction_status === 'text')
        .map((source) => source.id),
    );
    for (const id of selection) if (!eligible.has(id)) selection.delete(id);
    $('ni-sources').replaceChildren(
      ...[...snapshot.sources].reverse().map((source) => {
        const current = latestIds.get(source.source_key).id === source.id;
        const row = node('li', undefined, 'ni-card');
        row.dataset.sourceId = source.id;
        row.append(node('h4', source.source_key));
        row.append(
          node(
            'p',
            `Revision ${source.revision} · ${(source.size_bytes / 1024).toFixed(1)} KiB · ${source.revoked_at ? 'Revoked' : !current ? 'Historical revision' : 'Current'} · ${source.extraction_status === 'text' ? 'Readable text' : 'Unread original'}${source.truncated ? ' · Text truncated' : ''}${source.redactions ? ' · ' + source.redactions + ' secret patterns redacted' : ''}`,
            'np-muted',
          ),
        );
        if (eligible.has(source.id) && snapshot.can_manage) {
          const label = node('label', undefined, 'nt-checkbox');
          const checkbox = node('input');
          checkbox.type = 'checkbox';
          checkbox.checked = selection.has(source.id);
          checkbox.onchange = () => {
            if (checkbox.checked && selection.size >= 12) {
              checkbox.checked = false;
              feedback('Select at most 12 readable source files per planning attempt.', 'warning');
            } else if (checkbox.checked) selection.add(source.id);
            else selection.delete(source.id);
          };
          label.append(checkbox, node('span', 'Include in next plan'));
          row.append(label);
        }
        if (!source.revoked_at) {
          const actions = node('div', undefined, 'np-actions');
          actions.append(button('Download original', () => downloadSource(source)));
          if (source.extraction_status === 'text')
            actions.append(button('Preview text', () => previewSource(source)));
          if (current && snapshot.can_manage)
            actions.append(
              button(
                'Revoke from context',
                async () => {
                  if (busy || uncertain || !(await saveDraft())) return;
                  await command('revoke', '/sources/' + source.id + '/revoke', {
                    expected_version: version,
                  });
                },
                'np-button np-danger',
              ),
            );
          row.append(actions);
        }
        return row;
      }),
    );
    if (!snapshot.sources.length)
      $('ni-sources').append(
        node(
          'li',
          'No source files yet. You can begin with the brief and add files as the project develops.',
          'np-empty',
        ),
      );
  }

  function addDetails(parent, values) {
    const list = node('dl');
    for (const [label, value] of values) list.append(node('dt', label), node('dd', value));
    parent.append(list);
  }

  function renderRun() {
    $('ni-run').replaceChildren();
    const run = selectedRun();
    if (!run) {
      lock();
      return;
    }
    const status = {
      running: 'Planning and independent review are in progress.',
      ready: 'Reviewed and ready to apply.',
      questions: 'Your answers are needed before this plan can proceed.',
      needs_revision:
        'The independent review requested changes. Refine the intake before creating another plan.',
      applied: 'Applied. The agent roles and board tasks are ready.',
      stale:
        'Project context, team, or board changed. Create a new plan against the current project.',
      failed:
        'Planning did not complete. Review the configuration and context before trying a new attempt.',
      unknown:
        'The provider outcome is uncertain. No new call will be sent for this attempt. Its reserved cost remains held until usage can be determined.',
      cancelled: 'This attempt was cancelled. Any unsettled provider usage remains reserved.',
    };
    $('ni-run').append(node('p', status[run.status] || run.status, 'ni-objective'));
    $('ni-run').append(
      node(
        'p',
        `${date(run.started_at)} · Planning: ${run.model} · Review: ${run.review_model || 'Not recorded for this historical attempt'} · Context version ${run.intake_version} · Charged ${money(run.charged_microusd)} · Reserved ${money(run.reserved_microusd)}${run.input_tokens == null ? '' : ' · Input tokens ' + run.input_tokens}${run.output_tokens == null ? '' : ' · Output tokens ' + run.output_tokens}`,
        'ni-run-meta',
      ),
    );
    if (run.error_code)
      $('ni-run').append(node('p', 'Status detail: ' + run.error_code, 'np-muted'));
    const inventory = node('details');
    inventory.append(
      node(
        'summary',
        `Source coverage: ${run.included_source_ids.length} included · ${run.omitted_source_ids.length} omitted`,
      ),
    );
    addDetails(inventory, [
      ['Included', run.included_source_ids.map(sourceName).join('\n') || 'No source files'],
      ['Omitted', run.omitted_source_ids.map(sourceName).join('\n') || 'None'],
    ]);
    $('ni-run').append(inventory);
    const proposal = run.proposal;
    if (proposal) {
      $('ni-run').append(
        node('h3', 'Direction and next milestone'),
        node('p', proposal.summary),
        node('p', proposal.next_milestone),
      );
      if (proposal.findings.length) {
        $('ni-run').append(node('h3', 'Evidence, assumptions, and conflicts'));
        const list = node('ul', undefined, 'ni-list');
        for (const finding of proposal.findings) {
          const item = node('li', undefined, 'ni-card');
          item.append(
            node('h4', finding.kind[0].toUpperCase() + finding.kind.slice(1)),
            node('p', finding.statement),
          );
          for (const evidence of finding.evidence)
            item.append(
              node('blockquote', evidence.quote),
              node('p', sourceName(evidence.source_id), 'np-muted'),
            );
          list.append(item);
        }
        $('ni-run').append(list);
      }
      if (proposal.questions.length) {
        $('ni-run').append(node('h3', 'Questions for you'));
        const list = node('ul', undefined, 'ni-list');
        for (const question of proposal.questions) {
          const item = node('li', undefined, 'ni-card');
          item.append(
            node('h4', question.question),
            node('p', question.why),
            node(
              'p',
              question.blocking
                ? 'Blocking decision · Answer in the brief above.'
                : 'Open question · Answer in the brief above.',
              'np-muted',
            ),
          );
          list.append(item);
        }
        $('ni-run').append(list);
      }
      $('ni-run').append(node('h3', 'Staffing proposal'));
      const roles = node('ul', undefined, 'ni-list');
      for (const role of proposal.roles) {
        const item = node('li', undefined, 'ni-card');
        item.append(
          node(
            'h4',
            `${role.name} · ${role.action === 'reuse' ? 'Reuse existing role' : 'New role'}`,
          ),
        );
        addDetails(item, [
          ['Responsibilities', role.instructions],
          ['Success criteria', role.success_criteria],
          ['Why needed', role.rationale],
          ['Reuse assessment', role.reuse_assessment],
          ['Staffing reason', role.need.replaceAll('_', ' ')],
        ]);
        roles.append(item);
      }
      if (!proposal.roles.length)
        roles.append(node('li', 'No additional agent roles are needed.', 'np-empty'));
      $('ni-run').append(roles, node('h3', 'Board work and review obligations'));
      const tasks = node('ul', undefined, 'ni-list');
      for (const task of proposal.tasks) {
        const item = node('li', undefined, 'ni-card');
        item.append(node('h4', task.title + (task.existing_task_id ? ' · Existing task' : '')));
        addDetails(item, [
          ['Work', task.description],
          ['Acceptance criteria', task.acceptance],
          [
            'Assignment',
            task.assignment === 'agent'
              ? task.role_key
              : task.assignment === 'human'
                ? 'Human project owner'
                : 'Available to claim',
          ],
          ['Independent review', task.review_role_key || 'Human review'],
        ]);
        tasks.append(item);
      }
      $('ni-run').append(tasks);
    }
    if (run.review) {
      $('ni-run').append(
        node('h3', 'Independent AI review'),
        node(
          'p',
          run.review.approved
            ? 'Approved for the controller’s policy and version checks.'
            : 'Changes requested. This proposal cannot be applied.',
        ),
      );
      const issues = node('ul');
      for (const issue of run.review.issues) issues.append(node('li', issue));
      $('ni-run').append(issues);
    }
    if (run.status === 'applied')
      $('ni-run').append(
        node(
          'p',
          `${run.applied_agent_ids.length} role records and ${run.applied_task_ids.length} task records are linked to this plan.`,
          'np-muted',
        ),
      );
    if (run.status === 'questions' && run.applied_task_ids.length)
      $('ni-run').append(
        node(
          'p',
          `${run.applied_task_ids.length} human decision ${run.applied_task_ids.length === 1 ? 'task is' : 'tasks are'} tracked on the board. Answer the questions above, save, and create a new plan.`,
          'np-muted',
        ),
      );
    lock();
  }

  function render() {
    const total = modelSnapshot?.project_totals;
    $('ni-spending').textContent =
      `Project model usage · Spent ${money(total?.charged_lifetime)} · Reserved ${money(total?.held_microusd)} · Lifetime limit ${money(modelSnapshot?.project_policy.lifetime_limit_microusd)}`;
    $('ni-history').replaceChildren(
      ...snapshot.runs.map(
        (run) => new Option(`${date(run.started_at)} · ${run.status.replaceAll('_', ' ')}`, run.id),
      ),
    );
    if (!snapshot.runs.length) $('ni-history').append(new Option('No planning attempts yet', ''));
    if (!snapshot.runs.some((run) => run.id === currentRunId))
      currentRunId = snapshot.runs[0]?.id || null;
    $('ni-history').value = currentRunId || '';
    renderAnswers();
    renderSources();
    renderRun();
    modelStatus();
  }

  async function refresh({ preserve = true } = {}) {
    const ticket = generation;
    let found;
    try {
      const results = await Promise.all([
        bridge().request(path()),
        bridge().request('/v2/projects/' + projectId + '/models'),
      ]);
      if (!active(ticket)) return false;
      [found, modelSnapshot] = results;
    } catch (error) {
      if ([401, 403, 404].includes(error.status) && active(ticket)) {
        reset();
        $('np-status').textContent = error.message;
        $('np-status').dataset.tone = 'error';
      }
      throw error;
    }
    if (!active(ticket)) return false;
    // Keep a recovered/selected attempt visible even after it leaves the recent-history page.
    if (currentRunId && !found.runs.some((run) => run.id === currentRunId)) {
      const pinned = await bridge().request(path('/runs/' + currentRunId));
      if (!active(ticket)) return false;
      found.runs.push(pinned);
    }
    snapshot = found;
    if (pending?.kind === 'analyze') {
      const foundRun = found.runs.find(
        (run) => run.idempotency_key === pending.body.idempotency_key,
      );
      if (foundRun) {
        currentRunId = foundRun.id;
        pending = null;
        uncertain = false;
        feedback('Planning attempt recovered. Its current status is shown below.', 'success');
      }
    }
    if (!preserve || !dirty) populate();
    else if (version !== found.intake.version) showConflict();
    render();
    for (const run of found.runs)
      if (
        (run.status === 'applied' || (run.status === 'questions' && run.applied_task_ids.length)) &&
        !refreshedRuns.has(run.id)
      ) {
        refreshedRuns.add(run.id);
        await bridge().onTeamChanged();
      }
    schedulePoll();
    return true;
  }

  function schedulePoll() {
    clearTimeout(timer);
    if (!dialog.open || (!running() && pending?.kind !== 'analyze')) return;
    timer = setTimeout(async () => {
      if (busy) {
        schedulePoll();
        return;
      }
      try {
        await refresh();
      } catch (error) {
        feedback('Status could not be refreshed. ' + error.message, 'warning');
        schedulePoll();
      }
    }, 4000);
  }

  function payload() {
    collectAnswers();
    if (Object.keys(answers).length > 12)
      throw Error(
        'Keep at most 12 answers in current context. Clear any earlier answers that no longer apply.',
      );
    return {
      background: $('ni-background').value,
      outcomes: $('ni-outcomes').value,
      constraints: $('ni-constraints').value,
      answers: { ...answers },
      auto_staff: $('ni-auto').checked,
      expected_version: version,
    };
  }

  async function perform() {
    if (busy || !pending) return false;
    const ticket = generation;
    const operation = pending;
    busy = true;
    lock();
    feedback(
      operation.kind === 'analyze'
        ? 'Planning and independent review can take a few minutes. The attempt is saved so it can be resumed safely.'
        : 'Saving…',
    );
    let succeeded = false;
    try {
      const result = await bridge().request(operation.url, operation.body, operation.method);
      if (!active(ticket)) return false;
      pending = null;
      uncertain = false;
      succeeded = true;
      if (operation.kind === 'save') {
        snapshot.intake = result;
        populate();
      }
      if (operation.kind === 'upload') uploadQueue.shift();
      if (['analyze', 'apply', 'cancel'].includes(operation.kind)) currentRunId = result.id;
      feedback(
        operation.kind === 'save'
          ? 'Intake saved.'
          : operation.kind === 'upload'
            ? 'Source file saved.'
            : 'Saved. The current status is shown below.',
        'success',
      );
      await refresh();
      return true;
    } catch (error) {
      if (!active(ticket)) return false;
      if (succeeded)
        feedback(
          'Saved, but the current state could not be refreshed. Refresh status before continuing. ' +
            error.message,
          'warning',
        );
      else if (!error.status || error.status >= 500) {
        uncertain = true;
        feedback(
          'The response could not be confirmed. Check status or retry the same request before making another change.',
          'warning',
        );
        if (operation.kind === 'analyze') {
          try {
            await refresh();
          } catch {
            /* Keep the exact pending request for recovery. */
          }
        }
      } else {
        pending = null;
        uncertain = false;
        uploadQueue = [];
        feedback(
          error.message + (operation.kind === 'save' ? ' Your draft is preserved.' : ''),
          'error',
        );
        if (error.status === 409) {
          try {
            await refresh();
            if (operation.kind === 'save' && dirty) showConflict();
          } catch {
            feedback(
              error.message +
                ' Refresh status to compare the latest version. Your draft is preserved.',
              'error',
            );
          }
        }
      }
      return false;
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
        schedulePoll();
      }
    }
  }

  async function command(kind, suffix, body, method = 'POST') {
    if (busy || uncertain || !snapshot?.can_manage) return false;
    pending = {
      kind,
      url: path(suffix),
      method,
      body: { ...body, idempotency_key: crypto.randomUUID() },
    };
    return perform();
  }

  async function saveDraft() {
    if (busy || uncertain || latest || !snapshot?.can_manage) return false;
    if (!dirty && version >= 1) return true;
    if (!$('ni-form').reportValidity()) return false;
    try {
      return await command('save', '', payload(), 'PUT');
    } catch (error) {
      feedback(error.message, 'error');
      return false;
    }
  }

  const encodeFile = (file) =>
    new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',')[1]);
      reader.onerror = () => reject(Error('Could not read ' + file.name + '. Select it again.'));
      reader.readAsDataURL(file);
    });

  async function uploadNext() {
    const ticket = generation;
    while (uploadQueue.length && active(ticket) && !busy && !uncertain) {
      const file = uploadQueue[0];
      $('ni-upload-progress').textContent =
        `Uploading ${file.webkitRelativePath || file.name} · ${uploadQueue.length} remaining`;
      try {
        busy = true;
        lock();
        const data = await encodeFile(file);
        if (!active(ticket)) return;
        busy = false;
        if (
          !(await command('upload', '/sources', {
            source_key: file.webkitRelativePath || file.name,
            filename: file.name,
            media_type: file.type || 'application/octet-stream',
            content_base64: data,
            expected_version: version,
          }))
        )
          break;
      } catch (error) {
        if (active(ticket)) {
          busy = false;
          uploadQueue = [];
          feedback(error.message, 'error');
          lock();
        }
        break;
      }
    }
    if (active(ticket) && !uploadQueue.length)
      $('ni-upload-progress').textContent = 'All selected files have been processed.';
  }

  for (const id of ['ni-files', 'ni-folder'])
    $(id).onchange = async (event) => {
      const files = Array.from(event.target.files || []);
      event.target.value = '';
      if (!files.length || busy || uncertain) return;
      if (files.length > 500 || files.some((file) => file.size > 5 * 1024 * 1024)) {
        feedback(
          'Select at most 500 files, each no larger than 5 MiB. No files were uploaded.',
          'error',
        );
        return;
      }
      if (!(await saveDraft())) return;
      uploadQueue = files;
      await uploadNext();
    };

  $('ni-form').addEventListener('input', () => {
    dirty = true;
    modelStatus();
  });
  $('ni-form').onsubmit = async (event) => {
    event.preventDefault();
    await saveDraft();
  };
  $('ni-analyze').onclick = async () => {
    if (busy || uncertain || running() || !(await saveDraft())) return;
    await command('analyze', '/analyze', { expected_version: version, source_ids: [...selection] });
  };
  $('ni-apply').onclick = async () => {
    const run = selectedRun();
    if (!run || dirty || latest) return;
    await command('apply', '/runs/' + run.id + '/apply', { expected_version: run.version });
  };
  $('ni-cancel-run').onclick = async () => {
    const run = selectedRun();
    if (!run || uncertain || busy) return;
    await command('cancel', '/runs/' + run.id + '/cancel', { expected_version: run.version });
  };
  $('ni-retry').onclick = async () => {
    if (await perform()) await uploadNext();
  };
  $('ni-refresh').onclick = async () => {
    if (busy) return;
    busy = true;
    lock();
    try {
      await refresh();
    } catch (error) {
      feedback(error.message + ' Your draft is preserved.', 'error');
    } finally {
      busy = false;
      lock();
    }
  };
  $('ni-rebase').onclick = () => {
    if (busy || uncertain || !latest) return;
    version = latest.version;
    latest = null;
    $('ni-conflict').hidden = true;
    feedback(
      'Your draft is preserved against the latest version. Review it before saving.',
      'warning',
    );
    lock();
  };
  $('ni-history').onchange = () => {
    currentRunId = $('ni-history').value;
    renderRun();
  };
  $('ni-preview-close').onclick = () => {
    $('ni-preview').hidden = true;
    $('ni-preview-text').textContent = '';
  };

  function reset() {
    generation++;
    clearTimeout(timer);
    pending = null;
    snapshot = null;
    modelSnapshot = null;
    latest = null;
    projectId = null;
    workspaceId = null;
    busy = false;
    dirty = false;
    uncertain = false;
    version = 0;
    currentRunId = null;
    uploadQueue = [];
    answers = {};
    selection.clear();
    refreshedRuns.clear();
    $('ni-form').reset();
    for (const id of ['ni-answers', 'ni-sources', 'ni-run', 'ni-preview-text'])
      $(id).replaceChildren();
    $('ni-preview').hidden = true;
    $('ni-conflict').hidden = true;
    $('ni-uncertain').hidden = true;
    $('ni-upload-progress').textContent = '';
    feedback();
    if (dialog.open) dialog.close();
  }

  async function open() {
    const project = bridge().getProject();
    if (!project) return;
    if (projectId !== project.id || workspaceId !== bridge().getWorkspaceId()) reset();
    projectId = project.id;
    workspaceId = bridge().getWorkspaceId();
    const ticket = ++generation;
    $('ni-project').textContent = project.name;
    $('ni-objective').textContent = project.objective;
    dialog.showModal();
    $('ni-heading').focus();
    busy = true;
    lock();
    feedback('Loading saved intake…');
    try {
      if (await refresh()) feedback(dirty ? 'Your unsaved draft is still here.' : '');
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
    generation++;
    clearTimeout(timer);
    $('ni-preview').hidden = true;
    $('ni-preview-text').textContent = '';
    dialog.close();
  }
  $('np-intake').onclick = open;
  $('ni-models').onclick = () => {
    if (busy || uncertain) return;
    close();
    window.SimonNativeModels?.open();
  };
  $('ni-close').onclick = close;
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
  window.SimonNativeIntake = { open };
})();
