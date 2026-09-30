'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const node = (tag, text, className) => {
    const value = document.createElement(tag);
    if (text !== undefined) value.textContent = text;
    if (className) value.className = className;
    return value;
  };
  let pendingProject = null, pendingWorkflow = null, editingTask = null;
  let snapshot = {projects: [], project_artifacts: [], tasks: [], workflows: [], workflow_runs: [], workflow_schedules: [], print_batches: []};
  let refreshTimer = null, currentProject = null;
  const initialProject = new URLSearchParams(location.search).get('project');
  const projectPage = node('section', undefined, 'project-page'); projectPage.id = 'project-page'; projectPage.hidden = true;
  $('work-view').append(projectPage);
  projectPage.innerHTML = '<button type="button" id="project-back">Back to Work</button><p class="eyebrow">PROJECT</p><h1 id="project-page-title"></h1><p id="project-page-description"></p><p id="project-page-drive" class="muted"></p><div id="project-page-actions" class="work-item-actions"></div><div class="project-page-grid"><section class="work-card"><h2>Sessions</h2><p class="muted">Sessions run in the background. Return here to view saved progress.</p><div id="project-page-sessions"></div></section><section class="work-card"><h2>Files and outputs</h2><div id="project-page-files"></div></section></div><section class="work-card"><h2>Project work</h2><div id="project-page-tasks"></div><form id="project-work-form"><label>Task title<input id="project-work-title" required maxlength="120"></label><label>Instructions<textarea id="project-work-instructions" required maxlength="4000" rows="3"></textarea></label><button class="primary">Start background work</button><p id="project-work-feedback" role="status"></p></form></section><section class="work-card"><h2>Recent file activity</h2><div id="project-page-activity"></div></section>';
  function projectLayout(id) {
    currentProject = id;
    for (const child of $('work-view').children) if (child !== projectPage) child.hidden = Boolean(id);
    projectPage.hidden = !id;
  }
  async function openProject(id) {
    projectLayout(id); showView('work');
    history.replaceState(null, '', appPath('/chat?project=' + id));
    await loadProject(); scheduleRefresh();
  }
  async function loadProject() {
    const id = currentProject; if (!id) return;
    try {
      const project = await api('/v1/projects/' + id);
      if (currentProject !== id) return;
      $('chat-title').textContent = project.subject;
      $('project-page-title').textContent = project.subject;
      $('project-page-description').textContent = project.content;
      $('project-page-drive').textContent = project.drive.enabled ? (project.drive.google_email || '') + ' / ' + (project.drive.error || 'Drive linked') : 'Drive unlinked / local files available';
      const actions = $('project-page-actions'); actions.replaceChildren();
      addActions(actions, [['Start a session', async () => { await window.SimonBackground.project(id); showView('chat'); el('chat-title').textContent = project.subject; el('text').focus(); }], ['Drive files', () => browseFiles(project)], ['Local files', () => window.SimonLocalFiles.open('project:' + id)], ['Choose Drive folder', () => linkProjectFolder(project)]]);
      const sessions = $('project-page-sessions'); sessions.replaceChildren();
      const unique = new Map(); for (const item of project.sessions) if (!unique.has(item.thread_id)) unique.set(item.thread_id, item);
      for (const item of unique.values()) {
        const row = node('article', undefined, 'work-item'); row.append(node('strong', item.text.slice(0,120)), node('p', item.status + ' / ' + new Date(item.updated_at).toLocaleString()));
        if (item.error) row.append(node('p', item.error, 'error'));
        addActions(row, [['Open session', async () => { showView('chat'); await selectThread(item.thread_id); }]]); sessions.append(row);
      }
      if (!unique.size) sessions.append(node('p', 'No project sessions yet.', 'muted'));
      renderTasks(project.tasks, $('project-page-tasks'));
      $('project-page-files').replaceChildren(...(project.artifacts.length ? project.artifacts.map(artifactLink) : [node('p', 'Generated outputs will appear here. Browse Drive or local files above.', 'muted')]));
      $('project-page-activity').replaceChildren(...(project.activity.length ? project.activity.map(item => node('p', item.kind.replaceAll('_',' ') + ' / ' + item.status + ' / ' + new Date(item.created_at).toLocaleString())) : [node('p', 'No file activity yet.', 'muted')]));
    } catch (error) { $('project-page-description').textContent = error.message; }
  }
  $('project-back').onclick = () => { projectLayout(null); history.replaceState(null, '', appPath('/chat')); loadWork(); };
  $('project-work-form').onsubmit = async event => {
    event.preventDefault(); const button = event.submitter; button.disabled = true;
    try { await api('/v1/assistant-tasks', {title: $('project-work-title').value.trim(), instructions: $('project-work-instructions').value.trim(), project_id: currentProject, task_type: 'work', priority: 3, idempotency_key: crypto.randomUUID()}); event.target.reset(); $('project-work-feedback').textContent = 'Saved in the background queue.'; await loadProject(); }
    catch (error) { $('project-work-feedback').textContent = error.message; } finally { button.disabled = false; }
  };
  $('work-open').after($('voice-open'));

  const say = (message, error = false) => {
    $('work-status').textContent = message;
    $('work-status').classList.toggle('error', error);
  };
  const terminal = status => ['succeeded', 'failed', 'cancelled', 'expired'].includes(status);
  const showView = name => {
    const work = name === 'work';
    $('work-view').hidden = !work;
    $('conversation').hidden = work;
    document.querySelector('.composer-area').hidden = work;
    $('chat-open').setAttribute('aria-current', work ? 'false' : 'page');
    $('work-open').setAttribute('aria-current', work ? 'page' : 'false');
    $('chat-title').textContent = work ? 'Work with Simon' : rows.find(row => row.id === activeThread)?.title || 'New conversation';
    if (work) navigation(false);
    scheduleRefresh();
  };
  const discuss = prompt => {
    showView('chat'); $('text').value = prompt; autosize(); $('text').focus();
  };
  const addActions = (parent, choices) => {
    if (!choices.length) return;
    const bar = node('div', undefined, 'work-item-actions');
    for (const [label, callback] of choices) {
      const button = node('button', label); button.type = 'button';
      button.onclick = async () => {
        button.disabled = true;
        try { await callback(); } catch (error) { say(error.message, true); }
        finally { button.disabled = false; }
      };
      bar.append(button);
    }
    parent.append(bar);
  };
  const resetTaskForm = () => {
    editingTask = null; $('work-task-form').reset(); $('work-task-priority').value = '3';
    $('work-task-form-title').textContent = 'Start background work';
    $('work-task-submit').textContent = 'Queue task'; $('work-task-cancel-edit').hidden = true;
  };
  const editTask = task => {
    if (currentProject) { projectLayout(null); loadWork().then(() => editTask(task)); return; }
    editingTask = task; $('work-task-name').value = task.title;
    $('work-task-kind').value = task.task_type; $('work-task-project').value = task.project_id || '';
    $('work-task-priority').value = String(task.priority); $('work-task-detail').value = task.instructions;
    $('work-task-form-title').textContent = 'Edit queued task'; $('work-task-submit').textContent = 'Save changes';
    $('work-task-cancel-edit').hidden = false; $('work-task-name').focus();
  };
  const taskControl = async (task, action) => {
    await api(`/v1/assistant-tasks/${task.id}/control`, {action, expected_version: task.version});
    await loadWork(); say(`${task.title}: ${action.replace('_', ' ')} requested.`);
  };
  const steerTask = async task => {
    const message = window.prompt('What should Simon change or focus on?');
    if (!message?.trim()) return;
    await api(`/v1/assistant-tasks/${task.id}/steer`, {message: message.trim(), expected_version: task.version});
    await loadWork(); say(`${task.title} was re-queued with your guidance.`);
  };
  const humanSize = bytes => bytes < 1024 ? `${bytes} B` : `${Math.ceil(bytes / 1024)} KB`;
  const artifactLink = artifact => {
    const link = node('a', `${artifact.name} · ${humanSize(artifact.byte_count)}`);
    link.href = appPath(`/v1/assistant-tasks/artifacts/${artifact.id}/download`);
    return link;
  };
  const openTaskResult = async task => {
    const [current, artifacts] = await Promise.all([
      api(`/v1/assistant-tasks/${task.id}`), api(`/v1/assistant-tasks/${task.id}/artifacts`),
    ]);
    $('task-result-title').textContent = current.title;
    $('task-result-meta').textContent = [current.project_name, current.task_type, new Date(current.updated_at).toLocaleString()].filter(Boolean).join(' · ');
    $('task-result-body').replaceChildren(SimonMarkdown.render(current.result || 'No result was saved.'));
    $('task-result-files').replaceChildren(...(artifacts.length ? artifacts.map(artifact => {
      const row = node('div', undefined, 'work-item project-output'); row.append(artifactLink(artifact));
      row.append(node('small', artifact.media_type)); return row;
    }) : [node('p', current.project_id ? 'No project files were created.' : 'Link a task to a project to keep its result and generated files there.', 'work-empty')]));
    $('task-result-panel').showModal();
  };
  const renderTasks = (tasks, target = $("work-tasks")) => {
    const active = tasks.filter(task => !terminal(task.status)).length;
    $('task-count').textContent = `${active} active`;
    target.replaceChildren(...(tasks.length ? tasks.map(task => {
      const card = node('article', undefined, 'work-item task-item');
      const head = node('div', undefined, 'task-head');
      head.append(node('strong', task.title), node('span', task.status.replaceAll('_', ' '), `task-badge ${task.status}`));
      card.append(head, node('p', [task.task_type, task.project_name, `priority ${task.priority}`].filter(Boolean).join(' · ')));
      const progress = node('div', undefined, 'task-progress');
      const fill = node('span'); fill.style.width = `${task.progress}%`; progress.append(fill); card.append(progress);
      card.append(node('small', `${task.phase} · ${task.progress}% · updated ${new Date(task.updated_at).toLocaleString()}`));
      if (task.error) card.append(node('p', task.error, 'task-error'));
      if (task.result) card.append(node('p', task.result, 'task-result'));
      const choices = [];
      if (task.status === 'queued') choices.push(['Edit', () => editTask(task)], ['Move up', () => taskControl(task, 'move_up')], ['Move down', () => taskControl(task, 'move_down')]);
      if (['queued', 'running'].includes(task.status)) choices.push(['Steer', () => steerTask(task)], ['Pause', () => taskControl(task, 'pause')]);
      if (task.status === 'paused') choices.push(['Resume', () => taskControl(task, 'resume')], ['Edit', () => editTask(task)]);
      if (!terminal(task.status)) choices.push(['Cancel', () => taskControl(task, 'cancel')]);
      if (task.status === 'succeeded' && task.result) choices.push(['Open result', () => openTaskResult(task)]);
      if (task.thread_id) choices.push(['Discuss result', () => discuss(`Let's discuss the background task “${task.title}” and its result.`)]);
      addActions(card, choices); return card;
    }) : [node('p', 'No background work yet. Queue a task here or ask Simon in chat or voice.', 'work-empty')]));
  };
  const filesPanel = node('dialog', undefined, 'project-files-panel');
  filesPanel.id = 'project-files-panel'; document.body.append(filesPanel);
  let fileTimer = null, fileGeneration = 0;
  filesPanel.addEventListener('close', () => { clearTimeout(fileTimer); fileGeneration++; });
  const driveLink = (label, url) => {
    const link = node('a', label); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; return link;
  };
  async function browseFiles(project, folder = null, pageToken = '') {
    clearTimeout(fileTimer);
    const generation = ++fileGeneration;
    if (!filesPanel.open) filesPanel.showModal();
    const title = node('h2', project.subject + ' — Files');
    const content = node('div', 'Loading Drive files…');
    const feedback = node('p'); feedback.setAttribute('role', 'status');
    const close = node('button', 'Close'); close.type = 'button'; close.onclick = () => filesPanel.close();
    filesPanel.replaceChildren(title, close, feedback, content);
    try {
      const params = new URLSearchParams({page_token: pageToken}); if (folder) params.set('folder_id', folder);
      const data = await api(`/v1/projects/${project.id}/files?${params}`);
      if (generation !== fileGeneration) return;
      content.replaceChildren(node('p', data.folder.name));
      addActions(content, [
        ['Project root', () => browseFiles(project)],
        ['Refresh files', () => browseFiles(project, folder)],
        ['Ask Simon to work on files', () => { filesPanel.close(); discuss(`Work on the files in project "${project.subject}" (project ID ${project.id}). List its Drive files first.`); }],
      ]);
      const uploadLabel = node('label', 'Upload a file (up to 10 MB)');
      const upload = node('input'); upload.type = 'file'; upload.id = 'project-file-upload'; uploadLabel.append(upload); content.append(uploadLabel);
      upload.onchange = async () => {
        const file = upload.files[0]; if (!file) return;
        if (file.size > 10 * 1024 * 1024) { feedback.textContent = 'Use Drive to upload files larger than 10 MB.'; return; }
        upload.disabled = true; clearTimeout(fileTimer); feedback.textContent = 'Uploading…';
        try {
          const encoded = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.onerror = reject; reader.readAsDataURL(file); });
          const receipt = await api(`/v1/projects/${project.id}/upload`, {name: file.name, content_base64: encoded, folder_id: folder, idempotency_key: crypto.randomUUID()});
          if (receipt.status !== 'succeeded') throw Error(receipt.error || 'Upload is pending. Check Drive before retrying.');
          await browseFiles(project, folder);
        } catch (error) { feedback.textContent = error.message; upload.disabled = false; }
      };
      if (!data.files.length) content.append(node('p', 'This folder is empty. Add files here, in Drive, or ask Simon to create them.'));
      for (const file of data.files) {
        const row = node('article', undefined, 'work-item'); row.append(driveLink(file.name, file.url));
        row.append(node('small', file.modifiedTime ? `Updated ${new Date(file.modifiedTime).toLocaleString()}` : file.mimeType));
        if (file.mimeType === 'application/vnd.google-apps.folder') addActions(row, [['Browse folder', () => browseFiles(project, file.id)]]);
        else addActions(row, [['Read here', async () => {
          try {
            const result = await api(`/v1/projects/${project.id}/files/${file.id}`);
            const old = row.querySelector('pre'); if (old) old.remove();
            row.append(node('pre', result.text || result.content_note || 'Open this file in Drive.'));
          } catch (error) { feedback.textContent = error.message; }
        }], ['Edit with Simon', () => { filesPanel.close(); discuss(`Edit "${file.name}" in project "${project.subject}" (project ID ${project.id}, file ID ${file.id}). Read its latest content first. The change I want is: `); }]]);
        if (file.capabilities?.canTrash) {
          const key = crypto.randomUUID();
          addActions(row, [['Move to trash', async () => {
            clearTimeout(fileTimer);
            try {
              const receipt = await api('/v1/projects/trash-drive-item', {project_id: project.id, file_id: file.id, revision: String(file.version), account: project.drive?.google_email || '', idempotency_key: key});
              if (receipt.status !== 'succeeded') throw Error(receipt.error || 'Check Drive before retrying.');
              await browseFiles(project, folder);
            } catch (error) { feedback.textContent = error.message; }
          }]]);
        }
        content.append(row);
      }
      if (data.next_page_token) addActions(content, [['Next files', () => browseFiles(project, folder, data.next_page_token)]]);
      feedback.textContent = 'Live files from Drive. Changes are saved in the linked folder.';
      fileTimer = setTimeout(() => { if (filesPanel.open && !document.hidden) browseFiles(project, folder, pageToken); }, 30000);
    } catch (error) { feedback.textContent = error.message; content.replaceChildren(); }
  }
  async function linkProjectFolder(project) {
    clearTimeout(fileTimer); ++fileGeneration;
    if (!filesPanel.open) filesPanel.showModal();
    const title = node('h2', 'Choose a Drive folder for ' + project.subject);
    const close = node('button', 'Close'); close.onclick = () => filesPanel.close();
    const account = node('select'); account.setAttribute('aria-label', 'Google account');
    const search = node('input'); search.type = 'search'; search.placeholder = 'Find a folder by name'; search.setAttribute('aria-label', 'Find Drive folder');
    const find = node('button', 'Find folders');
    const content = node('div'); const feedback = node('p'); feedback.setAttribute('role', 'status');
    filesPanel.replaceChildren(title, close, account, search, find, feedback, content);
    let current = 'root', generation = 0;
    async function useFolder(folder) {
      feedback.textContent = 'Linking folder...';
      try {
        await api('/v1/projects/link-drive', {project_id: project.id, account: account.value, folder_id: folder, expected_version: project.drive?.version || 0});
        await loadWork(); filesPanel.close(); say('Project folder linked. Existing files stay in their current locations.');
      } catch (error) { feedback.textContent = error.message; }
    }
    async function browse(folder = 'root', page = '') {
      current = folder; const request = ++generation; feedback.textContent = 'Loading folders...';
      try {
        const query = search.value.trim();
        const data = await api('/v1/projects/drive/browse', {account: account.value, folder_id: folder, query, page_token: page, folders_only: true, search_all: Boolean(query)});
        if (request !== generation || !filesPanel.open) return;
        content.replaceChildren(node('h3', query ? 'Matching folders' : data.folder.name));
        addActions(content, [['My Drive', () => { search.value = ''; browse('root'); }], ['Use My Drive', () => useFolder('root')]]);
        if (!query) {
          addActions(content, [['Use this folder', () => useFolder(data.folder.id)]]);
          if (data.folder.parents?.length) addActions(content, [['Up', () => browse(data.folder.parents[0])]]);
        }
        for (const folder of data.files) {
          const row = node('article', undefined, 'work-item'); row.append(node('strong', folder.name));
          addActions(row, [['Browse', () => { search.value = ''; browse(folder.id); }], ['Use folder', () => useFolder(folder.id)]]);
          content.append(row);
        }
        if (!data.files.length) content.append(node('p', 'No folders found.'));
        if (data.next_page_token) addActions(content, [['Next folders', () => browse(current, data.next_page_token)]]);
        feedback.textContent = data.account_email;
      } catch (error) { feedback.textContent = error.message; }
    }
    try {
      const status = await api('/v1/connections/google');
      for (const item of status.accounts || []) if (item.drive_read) account.append(new Option(item.email, item.email));
      account.value = project.drive?.google_email || status.email || '';
      if (!account.value && account.options.length) account.selectedIndex = 0;
      if (!account.value) { feedback.textContent = 'Connect Google with Drive access in Connections.'; return; }
      account.onchange = () => { search.value = ''; browse('root'); };
      find.onclick = () => browse('root');
      search.onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); browse('root'); } };
      await browse();
    } catch (error) { feedback.textContent = error.message; }
  }
  const renderProjects = (projects, artifacts) => {
    $('work-task-project').replaceChildren(new Option('No linked project', ''), ...projects.map(project => new Option(project.subject, project.id)));
    $('work-projects').replaceChildren(...(projects.length ? projects.map(project => {
      const card = node('article', undefined, 'work-item'); const title = node('button', project.subject, 'project-title-link'); title.onclick = () => openProject(project.id); card.append(title, node('p', project.content));
      addActions(card, [['Open project', () => openProject(project.id)]]);
      addActions(card, [['Local files', () => window.SimonLocalFiles.open('project:' + project.id)]]);
      const drive = project.drive;
      if (drive) {
        card.append(node('p', drive.error || (drive.status === 'unlinked' ? 'Drive unlinked. Local project files are available.' : drive.status === 'ready' ? 'Drive connected' : 'Preparing Drive files…')));
        if (drive.url) card.append(driveLink('Open project folder in Drive', drive.url));
        addActions(card, [['Browse files', () => browseFiles(project)], ['Link existing folder', () => linkProjectFolder(project)], ['Unlink Drive', async () => { await api('/v1/projects/unlink-drive', {project_id: project.id, expected_version: drive.version}); await loadWork(); say('Drive unlinked. Files are preserved and Drive sync is stopped.'); }], ['Sync files', async () => { const result = await api(`/v1/projects/${project.id}/sync`, {}); await loadWork(); say(result.error || 'Project sync checked.', Boolean(result.error)); }]]);
      }
      const outputs = artifacts.filter(artifact => artifact.project_id === project.id);
      if (outputs.length) {
        const library = node('details', undefined, 'project-outputs');
        library.append(node('summary', `${outputs.length} saved output${outputs.length === 1 ? '' : 's'}`));
        for (const artifact of outputs) library.append(artifactLink(artifact));
        card.append(library);
      }
      addActions(card, [['Discuss with Simon', () => discuss(`Help me make progress on ${project.subject}. Use my saved project context.`)],
        ...(project.created_by === session.actor_id ? [['Remove', async () => { await api(`/v1/memories/${project.id}/retract`, {}); await loadWork(); await memories(); }]] : [])]);
      return card;
    }) : [node('p', 'No project context saved yet.', 'work-empty')]));
  };
  const controlRun = async (run, action) => {
    await api(`/v1/workflow-runs/${run.id}/control`, {action, expected_version: run.version}); await loadWork();
  };
  const scheduleControl = async (schedule, action) => {
    await api(`/v1/workflow-schedules/${schedule.id}/control`, {action, expected_version: schedule.version}); await loadWork();
  };
  const removeSchedule = async schedule => {
    if (!window.confirm(`Remove the ${schedule.frequency} schedule for “${schedule.name}”?`)) return;
    await api(`/v1/workflow-schedules/${schedule.id}?expected_version=${schedule.version}`, undefined, 'DELETE'); await loadWork();
  };
  const renderWorkflows = (workflows, schedules) => {
    $('work-workflows').replaceChildren(...(workflows.length ? workflows.map(flow => {
      const card = node('article', undefined, 'work-item');
      card.append(node('strong', flow.spec.name), node('p', `${flow.spec.steps.length} steps · version ${flow.version}`));
      for (const schedule of schedules.filter(item => item.definition_id === flow.id)) {
        card.append(node('p', `${schedule.frequency} · ${schedule.enabled ? `next ${new Date(schedule.next_run_at).toLocaleString()}` : 'paused'} · ${schedule.time_zone}`));
        addActions(card, [
          [schedule.enabled ? 'Pause schedule' : 'Resume schedule', () => scheduleControl(schedule, schedule.enabled ? 'pause' : 'resume')],
          ['Remove schedule', () => removeSchedule(schedule)],
        ]);
      }
      addActions(card, [
        ['Run now', async () => { await api(`/v1/workflows/${flow.id}/runs`, {definition_version: flow.version, idempotency_key: crypto.randomUUID()}); await loadWork(); }],
        ['Delete', async () => {
          if (!window.confirm(`Delete “${flow.spec.name}”? Completed run history will remain.`)) return;
          await api(`/v1/workflows/${flow.id}?expected_version=${flow.version}`, undefined, 'DELETE');
          await loadWork(); say(`${flow.spec.name} deleted.`);
        }],
      ]);
      return card;
    }) : [node('p', 'No scheduled workflows yet.', 'work-empty')]));
  };
  const renderOperations = (runs, batches) => {
    const values = [];
    for (const run of runs) {
      const card = node('article', undefined, 'work-item operation-item');
      card.append(node('strong', run.name), node('p', `Automation · ${run.status.replaceAll('_', ' ')} · ${run.steps.filter(step => step.status === 'succeeded').length}/${run.steps.length} steps`), node('small', new Date(run.created_at).toLocaleString()));
      const choices = [];
      if (['queued', 'running', 'waiting', 'needs_attention'].includes(run.status)) choices.push(['Pause', () => controlRun(run, 'pause')]);
      if (run.status === 'paused') choices.push(['Resume', () => controlRun(run, 'resume')]);
      if (!terminal(run.status)) choices.push(['Cancel', () => controlRun(run, 'cancel')]);
      addActions(card, choices); values.push(card);
    }
    for (const batch of batches) {
      const complete = batch.jobs.filter(job => ['done', 'completed'].includes(job.state)).length;
      const card = node('article', undefined, 'work-item operation-item');
      card.append(node('strong', `Print batch ${batch.id.slice(0, 8)}`), node('p', `Print · ${batch.state.replaceAll('_', ' ')} · ${complete}/${batch.jobs.length} jobs complete`), node('small', new Date(batch.created * 1000).toLocaleString()));
      if (batch.note) card.append(node('p', batch.note));
      addActions(card, [['Manage prints', () => { location.assign(appPath('/automations')); }]]); values.push(card);
    }
    $('work-runs').replaceChildren(...(values.length ? values : [node('p', 'No automation runs or print batches yet.', 'work-empty')]));
  };
  const scheduleRefresh = () => {
    clearTimeout(refreshTimer); refreshTimer = null;
    if ($('work-view').hidden || document.hidden) return;
    const active = Boolean(currentProject) || snapshot.tasks.some(task => !terminal(task.status)) || snapshot.workflow_runs.some(run => !terminal(run.status)) || snapshot.print_batches.some(batch => !['done', 'canceled'].includes(batch.state));
    if (active || snapshot.projects.length) refreshTimer = setTimeout(() => loadWork(true), 15000);
  };
  async function loadWork(quiet = false) {
    if (!ready || $('work-view').hidden) return;
    if (!quiet) say('Loading operations…');
    try {
      if (currentProject) { await loadProject(); return; }
      snapshot = await api('/v1/work/overview');
      renderProjects(snapshot.projects, snapshot.project_artifacts || []); renderTasks(snapshot.tasks); renderWorkflows(snapshot.workflows, snapshot.workflow_schedules || []);
      renderOperations(snapshot.workflow_runs, snapshot.print_batches);
      const online = Boolean(snapshot.workflow_health.worker_online);
      $('work-worker').textContent = online ? 'Worker online' : 'Worker offline'; $('work-worker').classList.toggle('online', online);
      if (!quiet) say('Work overview refreshed.');
    } catch (error) { say(error.message, true); }
    finally { scheduleRefresh(); }
  }

  $('work-open').onclick = () => { projectLayout(null); showView('work'); history.replaceState(null, '', appPath('/chat')); loadWork(); };
  $('chat-open').onclick = () => showView('chat'); $('work-refresh').onclick = () => loadWork();
  $('work-plan').onclick = () => discuss('Help me plan a project. Ask about the goal, deadline, available time, and first concrete step.');
  $('work-review').onclick = () => discuss('Review my current projects and background jobs, then help me reprioritize them.');
  $('work-task-cancel-edit').onclick = resetTaskForm;
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !$('work-view').hidden) loadWork(true); else scheduleRefresh(); });
  window.addEventListener('simon-ready', () => { if (initialProject) openProject(initialProject); else if (!$('work-view').hidden) loadWork(); });
  $('threads').addEventListener('click', event => { if (event.target.closest('.thread-button')) showView('chat'); });
  $('new-chat').addEventListener('click', () => showView('chat')); window.addEventListener('hashchange', () => showView('chat'));
  $('work-task-form').onsubmit = async event => {
    event.preventDefault(); const button = event.submitter; button.disabled = true;
    try {
      const body = {title: $('work-task-name').value.trim(), instructions: $('work-task-detail').value.trim(), task_type: $('work-task-kind').value, project_id: $('work-task-project').value || null, priority: Number($('work-task-priority').value)};
      const wasEditing = Boolean(editingTask);
      if (editingTask) await api(`/v1/assistant-tasks/${editingTask.id}`, {...body, change_project: true, expected_version: editingTask.version}, 'PATCH');
      else await api('/v1/assistant-tasks', {...body, idempotency_key: crypto.randomUUID()});
      resetTaskForm(); await loadWork(); say(wasEditing ? 'Task updated.' : 'Task queued.');
    } catch (error) { say(error.message, true); } finally { button.disabled = false; }
  };
  $('work-project-form').onsubmit = async event => {
    event.preventDefault(); const button = event.submitter; button.disabled = true;
    const subject = $('work-project-name').value.trim(), content = $('work-project-detail').value.trim();
    if (!pendingProject || pendingProject.name !== subject || pendingProject.description !== content) pendingProject = {name: subject, description: content, idempotency_key: crypto.randomUUID()};
    try { await api('/v1/projects', pendingProject); pendingProject = null; event.target.reset(); await loadWork(); await memories(); say('Project saved. Its Drive folder is managed automatically.'); }
    catch (error) { say(error.message, true); } finally { button.disabled = false; }
  };
  $('work-flow-action').onchange = () => {
    const echo = $('work-flow-action').value === 'system.echo'; $('work-flow-message').hidden = $('work-flow-message-label').hidden = !echo; $('work-flow-message').required = echo;
  };
  $('work-flow-form').onsubmit = async event => {
    event.preventDefault(); const button = event.submitter; button.disabled = true;
    const action = $('work-flow-action').value, spec = {name: $('work-flow-name').value.trim(), steps: [{id: 'check', action, inputs: action === 'system.echo' ? {message: $('work-flow-message').value.trim()} : {}}]};
    if (!pendingWorkflow || JSON.stringify(pendingWorkflow.spec) !== JSON.stringify(spec)) pendingWorkflow = {spec, expected_version: 0, idempotency_key: crypto.randomUUID()};
    try { await api('/v1/workflows', pendingWorkflow); pendingWorkflow = null; event.target.reset(); await loadWork(); say('Workflow created.'); }
    catch (error) { say(error.message, true); } finally { button.disabled = false; }
  };
})();
