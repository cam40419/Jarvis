'use strict';
(() => {
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const mounts = new WeakMap();
  const active = (status) => ['queued', 'running'].includes(status);
  const endpoint = (runId, artifactId) =>
    '/v1/agent-platform/runs/' +
    encodeURIComponent(runId) +
    '/artifacts/' +
    encodeURIComponent(artifactId);
  const textArtifact = (artifact) =>
    artifact.media_type?.startsWith('text/') ||
    ['application/json', 'application/xml'].includes(artifact.media_type);
  const rasterArtifact = (artifact) =>
    ['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(artifact.media_type);
  const readable = (task) => Boolean(task.output?.trim() || task.artifacts?.some(textArtifact));
  function button(label, callback) {
    const result = node('button', label, 'sr-button');
    result.type = 'button';
    result.onclick = callback;
    return result;
  }
  function answer(target, text, format = 'markdown', copyLabel = 'Copy answer') {
    const body = node('div', undefined, 'sr-answer-body pc-markdown');
    if (format === 'markdown') body.append(SimonMarkdown.render(text));
    else body.append(node('pre', text, 'sr-source'));
    const actions = node('div', undefined, 'sr-answer-actions');
    const copy = button(copyLabel, async () => {
      try {
        await navigator.clipboard.writeText(text);
        feedback.textContent = 'Copied';
      } catch (_) {
        feedback.textContent = 'Select the answer to copy it.';
      }
    });
    const feedback = node('span', undefined, 'sr-feedback');
    feedback.setAttribute('role', 'status');
    actions.append(copy, feedback);
    target.append(body, actions);
  }
  function planningResponse(text) {
    try {
      const result = JSON.parse(text);
      return result &&
        ['plan', 'waiting', 'complete'].includes(result.status) &&
        typeof result.summary === 'string' &&
        Array.isArray(result.tasks)
        ? {
            ...result,
            tasks: result.tasks.filter((item) => item && typeof item === 'object').slice(0, 32),
          }
        : null;
    } catch (_) {
      return null;
    }
  }
  function planningText(text) {
    try {
      const parsed = JSON.parse(text);
      if (typeof parsed?.summary === 'string' && parsed.summary.trim()) return parsed.summary;
    } catch (_) {
      /* Plain language can still explain a failed planning response. */
    }
    if (/^\s*(?:[\[{]|```)/.test(text))
      return 'The lead returned a planning record that could not be read. Retry with current access to create a fresh plan. The original response is saved under technical details.';
    return text;
  }
  function technicalRecord(text) {
    const details = node('details', undefined, 'sr-plan-source');
    details.append(node('summary', 'Technical planning record'));
    if (text) details.append(node('pre', text, 'sr-source'));
    return details;
  }
  function planningAnswer(target, text, force = false) {
    const decision = planningResponse(text);
    if (!decision && !force) return false;
    target.append(
      node(
        'p',
        decision?.status === 'waiting'
          ? 'Lead response · Needs information'
          : decision?.status === 'plan'
            ? 'Lead response · Proposed work'
            : 'Lead response',
        'sr-byline',
      ),
    );
    answer(target, planningText(text));
    if (decision?.tasks.length) {
      const list = node('ol', undefined, 'sr-proposed-tasks');
      for (const task of decision.tasks) {
        const item = node('li');
        item.append(node('strong', task.title || task.id || 'Proposed task'));
        if (typeof task.objective === 'string') item.append(node('p', task.objective));
        list.append(item);
      }
      target.append(list);
    }
    target.append(technicalRecord(text));
    return true;
  }
  function artifactCard(runId, artifact, options = {}) {
    const card = node('section', undefined, 'sr-artifact');
    card.dataset.artifact = artifact.id;
    const row = node('div', undefined, 'sr-file-row');
    if (!options.hideName) row.append(node('strong', artifact.name));
    const controls = node('div', undefined, 'sr-file-actions');
    const download = node('a', options.answer ? 'Download answer' : 'Download', 'sr-button');
    download.href = appPath(endpoint(runId, artifact.id));
    download.download = artifact.name;
    download.setAttribute('aria-label', 'Download ' + artifact.name);
    const projectUrl = (value, suffix) =>
      typeof value === 'string' &&
      /^\/v1\/projects\/[a-zA-Z0-9-]+\/outputs\/[a-zA-Z0-9-]+\/[a-zA-Z0-9-]+\/(preview|download)$/.test(
        value,
      ) &&
      value.endsWith('/' + suffix)
        ? value
        : null;
    const previewUrl =
      projectUrl(artifact.preview_url, 'preview') || endpoint(runId, artifact.id) + '/preview';
    const downloadUrl = projectUrl(artifact.download_url, 'download');
    if (downloadUrl) download.href = appPath(downloadUrl);
    const content = node('div', undefined, 'sr-file-preview');
    content.hidden = true;
    let loaded = false,
      loading = false;
    async function load() {
      if (loaded || loading) return;
      loading = true;
      content.replaceChildren(node('p', 'Loading preview…', 'sr-feedback'));
      try {
        if (rasterArtifact(artifact)) {
          const image = node('img');
          image.alt = artifact.name;
          image.src = download.href;
          image.loading = 'lazy';
          image.onerror = () =>
            content.replaceChildren(
              node('p', 'Preview unavailable. Download the file to view it.', 'sr-feedback'),
            );
          content.replaceChildren(image);
        } else {
          const preview = await api(previewUrl);
          content.replaceChildren();
          if (!planningAnswer(content, preview.text)) answer(content, preview.text, preview.format);
        }
        loaded = true;
      } catch (error) {
        content.replaceChildren(
          node('p', error.message, 'sr-feedback'),
          button('Retry preview', load),
        );
      } finally {
        loading = false;
      }
    }
    if (!options.answer && (textArtifact(artifact) || rasterArtifact(artifact))) {
      const preview = button('Read here', () => {
        content.hidden = !content.hidden;
        preview.textContent = content.hidden ? 'Read here' : 'Hide preview';
        preview.setAttribute('aria-expanded', String(!content.hidden));
        if (!content.hidden) load();
      });
      preview.setAttribute('aria-label', 'Preview ' + artifact.name);
      preview.setAttribute('aria-expanded', 'false');
      controls.append(preview);
      if (options.expanded) {
        content.hidden = false;
        preview.textContent = 'Hide preview';
        preview.setAttribute('aria-expanded', 'true');
        load();
      }
    }
    if (!options.hideDownload) controls.append(download);
    if (options.projectId && options.canSave && window.SimonProjectOutputs)
      controls.append(window.SimonProjectOutputs.saveButton(options.projectId, runId, artifact));
    row.append(controls);
    card.append(row, content);
    return card;
  }
  function renderTask(target, run, task, options = {}) {
    const body = node('section', undefined, 'sr-task-result');
    body.dataset.taskResult = task.id;
    const planning = options.phase
      ? options.phase === 'planning'
      : Boolean(
          planningResponse(task.output) ||
            (options.projectId && run.tasks?.length === 1 && task.id === 'lead-plan'),
        );
    if (task.output?.trim()) {
      if (planning) planningAnswer(body, task.output, true);
      else answer(body, task.output);
    } else if (!task.artifacts?.some(textArtifact))
      body.append(
        node(
          'p',
          active(task.status)
            ? 'The answer will appear here as soon as it is ready.'
            : 'This task did not save an answer.',
          'sr-feedback',
        ),
      );
    for (const [index, artifact] of (task.artifacts || []).entries()) {
      const card = artifactCard(run.id, artifact, {
        projectId: options.projectId,
        canSave:
          task.status === 'succeeded' &&
          !planning &&
          typeof session !== 'undefined' &&
          session?.scopes?.includes('jobs:write'),
        answer: Boolean(task.output && artifact.name === 'answer.txt'),
        expanded: !task.output && index === 0 && textArtifact(artifact),
      });
      if (planning && artifact.name === 'answer.txt') {
        card.querySelector('a').textContent = 'Download planning record';
        if (!body.querySelector('.sr-plan-source')) body.append(technicalRecord(task.output));
        body.querySelector('.sr-plan-source').append(card);
      } else body.append(card);
    }
    target.append(body);
    return body;
  }
  function resultTasks(run) {
    return (run?.tasks || []).filter(readable);
  }
  function mountLatest(target, projectId, options = {}) {
    const fingerprint = JSON.stringify([
      projectId,
      options.runs,
      options.state?.active_cycle,
      options.state?.last_cycle,
      options.presentation,
    ]);
    if (mounts.get(target)?.fingerprint === fingerprint) return;
    const current = { fingerprint, projectId };
    mounts.set(target, current);
    const valid = () => mounts.get(target) === current;
    const heading = node('div', undefined, 'sr-heading');
    heading.append(node('h3', 'Latest answer'));
    if (options.onHistory) heading.append(button('All results', options.onHistory));
    const content = node('div', undefined, 'sr-latest-content');
    target.classList.add('sr-latest');
    target.replaceChildren(heading, content);
    const cycles = [options.state?.active_cycle, options.state?.last_cycle].filter(Boolean);
    const response = options.presentation?.response;
    if (response) {
      heading.querySelector('h3').textContent =
        response.title || (response.kind === 'answer' ? 'Latest answer' : 'Lead response');
      target.dataset.responseKind = response.kind;
      const sourceRun = options.runs?.find((run) => run.id === response.run_id);
      const task = sourceRun?.tasks?.find((item) => item.id === response.task_id);
      const byline = [
        response.source === 'system'
          ? 'Simon status'
          : task
            ? options.profileName?.(task.agent_id) || task.agent_id
            : null,
        response.created_at ? new Date(response.created_at).toLocaleString() : null,
      ]
        .filter(Boolean)
        .join(' · ');
      if (byline) content.append(node('p', byline, 'sr-byline'));
      answer(
        content,
        response.text,
        'markdown',
        response.source === 'system' ? 'Copy error details' : 'Copy answer',
      );
      if (task && sourceRun) {
        const planning = response.phase === 'planning';
        const decision = planning ? planningResponse(task.output) : null;
        if (decision?.tasks.length) {
          const proposed = node('details', undefined, 'sr-supporting');
          proposed.append(node('summary', 'Proposed tasks (' + decision.tasks.length + ')'));
          const list = node('ol', undefined, 'sr-proposed-tasks');
          for (const item of decision.tasks) {
            const row = node('li');
            row.append(node('strong', item.title || item.id || 'Proposed task'));
            if (typeof item.objective === 'string') row.append(node('p', item.objective));
            list.append(row);
          }
          proposed.append(list);
          content.append(proposed);
        }
        const technical = planning || response.failure ? technicalRecord(task.output) : null;
        if (response.failure && technical) {
          technical.querySelector('summary').textContent = 'Technical error details';
          const diagnostics = node('dl');
          for (const [label, value] of [
            ['Error code', response.failure.code || 'Not recorded'],
            ['Task status', response.failure.status],
            ['Model steps', response.failure.steps],
            ['Recorded tool calls', response.failure.tool_calls],
            ['Run', sourceRun.id],
            ['Task', task.id],
          ])
            diagnostics.append(node('dt', label), node('dd', String(value)));
          technical.append(diagnostics);
        }
        for (const artifact of task.artifacts || []) {
          const card = artifactCard(sourceRun.id, artifact, {
            projectId,
            answer: Boolean(task.output && artifact.name === 'answer.txt'),
            canSave:
              task.status === 'succeeded' &&
              !planning &&
              typeof session !== 'undefined' &&
              session?.scopes?.includes('jobs:write'),
          });
          if (planning && artifact.name === 'answer.txt') {
            const link = card.querySelector('a');
            if (link) link.textContent = 'Download planning record';
            technical.append(card);
          } else content.append(card);
        }
        if (technical) content.append(technical);
        if (!planning) {
          const others = resultTasks(sourceRun).filter((item) => item.id !== task.id);
          if (others.length) {
            const supporting = node('details', undefined, 'sr-supporting');
            supporting.append(node('summary', 'Supporting results (' + others.length + ')'));
            for (const item of others) {
              const section = node('section');
              section.append(node('h4', options.profileName?.(item.agent_id) || item.agent_id));
              renderTask(section, sourceRun, item, { projectId, phase: 'execution' });
              supporting.append(section);
            }
            content.append(supporting);
          }
        }
      }
      return;
    }
    delete target.dataset.responseKind;
    if (options.presentation) {
      heading.querySelector('h3').textContent = 'Team response';
      content.append(
        node(
          'p',
          cycles.length
            ? 'The team’s response will appear here. Follow the current request for progress and next steps.'
            : 'Give the lead a task to get started. Your team’s answers will appear here.',
          'sr-feedback',
        ),
      );
      return;
    }
    const planningIds = new Set(cycles.map((cycle) => cycle.planning_run_id).filter(Boolean));
    const available = [...(options.runs || [])]
      .filter((run) => !planningIds.has(run.id))
      .sort((left, right) =>
        String(right.finished_at || right.started_at || '').localeCompare(
          String(left.finished_at || left.started_at || ''),
        ),
      );
    function display(run, earlier = false) {
      if (!valid()) return;
      const tasks = resultTasks(run);
      if (!tasks.length) return;
      content.replaceChildren();
      heading.querySelector('h3').textContent = earlier ? 'Previous answer' : 'Latest answer';
      const primary = tasks.find((task) => task.id === 'lead-summary') || tasks.at(-1);
      const plan = options.plans?.find((item) => item.id === run.plan_id);
      const taskPlan = plan?.tasks?.find((item) => item.id === primary.id);
      const cycle = cycles.find((item) => item.execution_run_id === run.id);
      if (cycle?.instruction) content.append(node('p', cycle.instruction, 'sr-request'));
      const byline = [
        options.profileName?.(primary.agent_id) || primary.agent_id,
        run.finished_at || run.started_at
          ? new Date(run.finished_at || run.started_at).toLocaleString()
          : 'Saved answer',
      ]
        .filter(Boolean)
        .join(' · ');
      content.append(node('p', byline, 'sr-byline'));
      if (earlier)
        content.append(
          node(
            'p',
            'The latest request has no saved answer yet. This answer is from an earlier run.',
            'sr-result-notice',
          ),
        );
      if (run.status !== 'succeeded')
        content.append(
          node(
            'p',
            active(run.status)
              ? 'Work is still in progress. These are the results saved so far.'
              : 'This run stopped before all tasks finished. The saved work below is still available.',
            'sr-result-notice',
          ),
        );
      if (taskPlan?.objective && !cycle?.instruction)
        content.append(node('p', taskPlan.objective, 'sr-request'));
      renderTask(content, run, primary, { projectId });
      const others = tasks.filter((task) => task !== primary);
      if (others.length) {
        const details = node('details', undefined, 'sr-supporting');
        details.append(node('summary', 'Supporting results (' + others.length + ')'));
        for (const task of others) {
          const section = node('section');
          section.append(node('h4', options.profileName?.(task.agent_id) || task.agent_id));
          renderTask(section, run, task, { projectId });
          details.append(section);
        }
        content.append(details);
      }
    }
    const recent = available.find((run) => resultTasks(run).length);
    if (recent) {
      const latestCycle = cycles[0];
      display(recent, Boolean(latestCycle && latestCycle.execution_run_id !== recent.id));
      return;
    }
    const currentCycle = cycles[0];
    if (currentCycle?.phase === 'completed') {
      for (const run of options.runs || []) {
        if (run.id !== currentCycle.planning_run_id) continue;
        for (const task of run.tasks || []) {
          try {
            const decision = JSON.parse(task.output);
            if (decision.status === 'complete' && typeof decision.summary === 'string') {
              content.append(node('p', currentCycle.instruction, 'sr-request'));
              answer(content, decision.summary);
              return;
            }
          } catch (_) {
            /* Planning documents are kept in run history. */
          }
        }
      }
    }
    content.append(node('p', 'Finding saved answers…', 'sr-feedback'));
    async function findEarlier() {
      try {
        const page = await api('/v1/projects/' + encodeURIComponent(projectId) + '/runs?limit=20');
        if (!valid()) return;
        const candidates = page.items
          .filter((run) => run.phase !== 'planning' && run.artifact_count > 0)
          .slice(0, 4);
        for (const summary of candidates) {
          const run = await api('/v1/agent-platform/runs/' + encodeURIComponent(summary.id));
          if (!valid()) return;
          if (resultTasks(run).length) {
            display(run, Boolean(currentCycle && currentCycle.execution_run_id !== run.id));
            return;
          }
        }
        content.replaceChildren(
          node(
            'p',
            currentCycle
              ? 'No answer has been saved for this request yet. Follow its progress below.'
              : 'Your team’s answers will appear here. Give the lead a task to get started.',
            'sr-feedback',
          ),
        );
      } catch (error) {
        if (valid())
          content.replaceChildren(
            node('p', error.message, 'sr-feedback'),
            button('Retry loading results', findEarlier),
          );
      }
    }
    findEarlier();
  }
  window.SimonResults = { renderTask, artifactCard, mountLatest };
})();
