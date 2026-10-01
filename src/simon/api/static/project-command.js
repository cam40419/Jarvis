'use strict';
(() => {
  const $ = id => document.getElementById(id);
  if (!$('work-view')) return;
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const labels = {draft: 'Draft', todo: 'To do', pending: 'To do', ready: 'Ready', planned: 'Ready', starting: 'Queued', queued: 'Queued', planning: 'Planning', executing: 'In progress', running: 'In progress', paused: 'Paused', blocked: 'Blocked', unknown: 'Outcome needs review', needs_review: 'Needs review', needs_human: 'Needs attention', succeeded: 'Done', completed: 'Done', done: 'Done', failed: 'Needs attention', cancelled: 'Cancelled', discarded: 'Discarded', archived: 'Archived'};
  const done = value => ['done', 'completed', 'succeeded', 'cancelled', 'discarded', 'archived'].includes(value);
  const active = value => ['starting', 'queued', 'planning', 'running', 'executing'].includes(value);
  const attention = value => ['blocked', 'unknown', 'failed', 'needs_review', 'needs_human'].includes(value);
  const human = value => String(value || '').replaceAll('_', ' ').replaceAll('-', ' ');
  const badge = value => node('span', labels[value] || human(value) || 'Draft', 'pc-status ' + value);
  const initials = value => String(value || 'Simon').split(/\s+/).slice(0, 2).map(part => part[0] || '').join('').toUpperCase();
  const date = value => value ? new Date(value).toLocaleString([], {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}) : '';
  const money = value => value == null ? 'No model cap set' : '$' + Number(value).toFixed(2);
  let projects = [], catalog = null, selected = null, detail = null, tab = 'overview';
  let resourceProject = null, localFiles = null, driveFiles = null, resourceLoading = false;
  let localError = '', driveError = '', resourceError = '', historyPage = null, historyLoading = false, historyError = '';
  let initialized = false, loading = false, timer = null, generation = 0, saving = false;
  let selectionEpoch = 0, historyGeneration = 0;
  let commandSubmission = null, projectSubmission = null, draftProject = null;
  let settingsSnapshot = null, todoSnapshot = null, noteSubmission = null;
  let olderActivity = [], nextActivityOffset = null, activityCount = null;
  let archivedTasks = null, nextArchiveOffset = null;
  const savedRuns = new Map();
  const drafts = new Map();
  const teamDrafts = new Map();
  let memberEdit = null, memberSkills = [], selectedMemberSkills = new Set();
  const root = node('section', undefined, 'project-command'); root.id = 'project-command'; root.setAttribute('aria-labelledby', 'pc-heading');
  root.innerHTML = `
    <div class="pc-heading"><div><h2 id="pc-heading">Projects</h2><p>Teams, tasks, files, and saved work.</p></div><button id="pc-new-project" type="button" class="pc-button primary">New project</button></div>
    <div class="pc-grid"><aside class="pc-sidebar"><div class="pc-sidebar-heading"><h3>Your projects</h3><span id="pc-project-count">0</span></div><label for="pc-project-search" class="sr-only">Find a project</label><input id="pc-project-search" type="search" class="pc-search" placeholder="Find a project"><nav id="pc-project-list" class="pc-project-list" aria-label="Projects"></nav></aside>
      <div id="pc-main" class="pc-main"><div id="pc-empty"></div><div id="pc-project-body" hidden>
        <header class="pc-project-header"><div><div id="pc-project-state"></div><h3 id="pc-project-title"></h3><p id="pc-project-description"></p></div><div class="pc-header-actions"><button id="pc-files" type="button" class="pc-button">Project files</button><button id="pc-settings" type="button" class="pc-button">Team &amp; autonomy</button></div></header>
        <div id="pc-blockers" class="pc-notice error" hidden></div>
        <div id="pc-metrics" class="pc-metrics" aria-label="Project progress"></div>
        <div class="pc-content-grid"><div class="pc-primary-content">
          <form id="pc-command-form" class="pc-lead-card"><div class="pc-lead-heading"><span id="pc-lead-avatar" class="pc-avatar" aria-hidden="true">S</span><div><strong id="pc-lead-name">Choose your project lead</strong><small id="pc-lead-subtitle">Coordinates the team and keeps the plan moving.</small></div></div><label id="pc-command-label" for="pc-command">What should we work on?</label><textarea id="pc-command" required maxlength="16000" rows="4" placeholder="Describe the outcome you want. Include constraints, decisions, or anything the team should know."></textarea><div class="pc-composer-footer"><p id="pc-command-note">Your lead turns the request into a plan with clear tasks and owners.</p><button id="pc-command-submit" type="submit" class="pc-button primary">Ask the lead</button></div><p id="pc-command-status" class="pc-local-status" role="status" aria-live="polite"></p></form>
          <div class="pc-tabs" role="tablist" aria-label="Project work"><button type="button" id="pc-tab-tasks" data-pc-tab="tasks" role="tab" aria-selected="true" aria-controls="pc-panel">Task list</button><button type="button" id="pc-tab-findings" data-pc-tab="findings" role="tab" aria-selected="false" tabindex="-1" aria-controls="pc-panel">Findings</button><button type="button" id="pc-tab-activity" data-pc-tab="activity" role="tab" aria-selected="false" tabindex="-1" aria-controls="pc-panel">Activity</button></div>
          <div id="pc-panel" role="tabpanel" aria-labelledby="pc-tab-tasks"><div class="pc-panel-toolbar"><p id="pc-task-summary"></p><label for="pc-task-filter" class="sr-only">Filter project tasks</label><select id="pc-task-filter"><option value="all">All tasks</option><option value="active">In progress</option><option value="attention">Needs attention</option><option value="done">Done</option></select></div><div id="pc-panel-content"></div></div>
        </div><aside id="pc-context" class="pc-context" aria-label="Project team and autonomy"></aside></div>
      </div></div></div><div class="pc-footer"><p id="pc-status" role="status" aria-live="polite">Loading your projects…</p><button id="pc-refresh" type="button" class="pc-button pc-text-button">Refresh projects</button></div>`;
  $('work-status').after(root);

  const projectDialog = node('dialog', undefined, 'pc-dialog'); projectDialog.id = 'pc-create-dialog'; projectDialog.setAttribute('aria-labelledby', 'pc-create-heading');
  projectDialog.innerHTML = `<div class="pc-dialog-header"><div><h2 id="pc-create-heading">Start a project.</h2><p>Give your team a clear goal and a place to keep its work.</p></div><button type="button" class="pc-button pc-dialog-close" aria-label="Close new project">×</button></div><form id="pc-create-form"><fieldset id="pc-create-fields"><label for="pc-create-name">Project name</label><input id="pc-create-name" required maxlength="200" placeholder="e.g. Home energy monitor"><label for="pc-create-goal">What does success look like?</label><textarea id="pc-create-goal" required maxlength="1000" rows="4" placeholder="The outcome, constraints, and context the team should keep in mind."></textarea><div id="pc-create-team-fields"></div></fieldset><p id="pc-create-error" class="pc-error" role="alert" hidden></p><div class="pc-dialog-footer"><p>The project starts with manual review. You can set up automatic cycles later.</p><button id="pc-create-submit" class="pc-button primary" type="submit">Create project</button></div></form>`;
  document.body.append(projectDialog);
  const settingsDialog = node('dialog', undefined, 'pc-dialog'); settingsDialog.id = 'pc-settings-dialog'; settingsDialog.setAttribute('aria-labelledby', 'pc-settings-heading');
  settingsDialog.innerHTML = `<div class="pc-dialog-header"><div><h2 id="pc-settings-heading">Team &amp; autonomy</h2><p id="pc-settings-project"></p></div><button type="button" class="pc-button pc-dialog-close" aria-label="Close project settings">×</button></div><form id="pc-settings-form"><fieldset id="pc-settings-fields"><div id="pc-settings-team-fields"></div><h3 class="pc-section-label">How the project moves forward</h3><label for="pc-autonomy-mode">Autonomy</label><select id="pc-autonomy-mode"><option value="manual">Manual — review each plan</option><option value="scheduled">Scheduled — continue within limits</option></select><p id="pc-autonomy-help" class="pc-help">Your lead prepares a plan. You choose when its tasks start.</p><div id="pc-schedule-fields" class="pc-form-grid" hidden><div><label for="pc-cadence">Check-in interval (minutes)</label><input id="pc-cadence" type="number" min="1" max="10080" step="1" value="60"></div><div><label for="pc-cycle-limit">Maximum cycles</label><input id="pc-cycle-limit" type="number" min="1" max="100" step="1" value="5"></div></div><label for="pc-budget">Model budget per cycle (USD, optional)</label><input id="pc-budget" type="number" min="0" step="0.01" placeholder="No cap set"><p class="pc-help">Model spending only. Estimates depend on configured model rates; external service charges are separate.</p></fieldset><p id="pc-settings-error" class="pc-error" role="alert" hidden></p><div class="pc-dialog-footer"><p>Saved policies apply to future work. Agent permissions still apply.</p><button id="pc-settings-save" class="pc-button primary" type="submit">Save settings</button></div></form>`;
  document.body.append(settingsDialog);
  const memberDialog = node('dialog', undefined, 'pc-dialog pc-member-dialog'); memberDialog.id = 'pc-member-dialog'; memberDialog.setAttribute('aria-labelledby', 'pc-member-heading');
  memberDialog.innerHTML = `<div class="pc-dialog-header"><div><h2 id="pc-member-heading">Add agent</h2><p>Configure this member for this project. Other projects and saved agents stay unchanged.</p></div><button id="pc-member-close" type="button" class="pc-button pc-dialog-close" aria-label="Close team member editor">×</button></div><form id="pc-member-form"><label for="pc-member-template">Start from an agent (optional)</label><select id="pc-member-template"></select><p class="pc-help">Use a starting point, then choose this member’s own skills. You can add the same starting point more than once.</p><label for="pc-member-name">Role title</label><input id="pc-member-name" required maxlength="160" placeholder="e.g. Research and documentation lead"><label for="pc-member-description">Role description</label><textarea id="pc-member-description" required maxlength="4000" rows="3" placeholder="Describe the outcomes this agent owns and how it should work."></textarea><div class="pc-skill-heading"><div><h3 id="pc-member-skills-heading">Individual skills</h3><p class="pc-help">Choose each capability this member needs. One agent can own several responsibilities.</p></div><span id="pc-member-count" role="status" aria-live="polite"></span></div><label for="pc-member-search" class="sr-only">Find an individual skill</label><input id="pc-member-search" type="search" placeholder="Find a skill"><div id="pc-member-skills" role="group" aria-labelledby="pc-member-skills-heading"></div><p id="pc-member-error" class="pc-error" role="alert" hidden></p><div class="pc-dialog-footer"><p id="pc-member-save-note">Added to your team draft. Save the project settings to apply changes.</p><button id="pc-member-save" type="submit" class="pc-button primary">Save team member</button></div></form>`;
  document.body.append(memberDialog);
  const memberFields = node('div', undefined, 'pc-member-fields');
  const memberForm = $('pc-member-form'), memberFooter = memberForm.querySelector('.pc-dialog-footer');
  while (memberForm.firstChild !== memberFooter) memberFields.append(memberForm.firstChild);
  memberForm.prepend(memberFields);

  $('pc-cadence').min = '5'; $('pc-budget').min = '0.01'; $('pc-budget').max = '10000';
  $('pc-budget').nextElementSibling.id = 'pc-budget-help';
  const objectiveWrap = node('div'); objectiveWrap.id = 'pc-objective-wrap';
  objectiveWrap.innerHTML = '<label for="pc-standing-objective">Standing objective</label><textarea id="pc-standing-objective" maxlength="16000" rows="3" placeholder="What should the lead keep working toward at each check-in?"></textarea>';
  $('pc-schedule-fields').before(objectiveWrap);
  const cyclePanel = node('section', undefined, 'pc-cycle'); cyclePanel.id = 'pc-cycle'; cyclePanel.hidden = true; cyclePanel.setAttribute('aria-label', 'Current project cycle');
  $('pc-command-form').before(cyclePanel);
  const boardSlot = node('div'); boardSlot.id = 'pc-board-slot'; root.querySelector('.pc-project-header').after(boardSlot);
  const navigation = root.querySelector('.pc-tabs');
  const sections = [['overview', 'Overview'], ['tasks', 'Execution tasks'], ['files', 'Files'], ['history', 'Run history'], ['board', 'Board'], ['knowledge', 'Knowledge'], ['activity', 'Activity'], ['sessions', 'Sessions'], ['team', 'Team']];
  navigation.replaceChildren(...sections.map(([id, label]) => {
    const item = node('button', label); item.id = 'pc-tab-' + id; item.type = 'button'; item.dataset.pcTab = id; item.setAttribute('role', 'tab'); item.setAttribute('aria-controls', ['tasks', 'knowledge', 'activity'].includes(id) ? 'pc-panel' : 'pc-' + id + '-panel'); item.setAttribute('aria-selected', String(id === tab)); item.tabIndex = id === tab ? 0 : -1; return item;
  }));
  boardSlot.before(navigation);
  const overviewPanel = node('div'); overviewPanel.id = 'pc-overview-panel'; overviewPanel.setAttribute('role', 'tabpanel'); overviewPanel.setAttribute('aria-labelledby', 'pc-tab-overview');
  const contentGrid = root.querySelector('.pc-content-grid');
  overviewPanel.append($('pc-metrics'), contentGrid); $('pc-project-body').append(overviewPanel);
  overviewPanel.after($('pc-panel'));
  const workspacePanels = {};
  for (const id of ['files', 'history', 'board', 'sessions', 'team']) {
    const panel = node('section', undefined, 'pc-section'); panel.id = 'pc-' + id + '-panel'; panel.hidden = true; panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', 'pc-tab-' + id); $('pc-project-body').append(panel); workspacePanels[id] = panel;
  }
  workspacePanels.board.append(boardSlot);
  $('pc-files').textContent = 'Project board';
  const directBoardLink = node('a', 'Open board ↗', 'pc-button pc-text-button'); directBoardLink.id = 'pc-direct-board'; directBoardLink.hidden = true; directBoardLink.target = '_blank'; directBoardLink.rel = 'noopener noreferrer'; $('pc-files').before(directBoardLink);
  new MutationObserver(() => {
    const link = boardSlot.querySelector('.pb-toolbar a[href]');
    directBoardLink.hidden = !link;
    if (link) { directBoardLink.href = link.href; directBoardLink.setAttribute('aria-label', 'Open ' + (boardSlot.querySelector('.pb-title')?.textContent || 'project board') + ' in ClickUp'); }
    else directBoardLink.removeAttribute('href');
  }).observe(boardSlot, {childList: true, subtree: true});
  const overviewLinks = node('div', undefined, 'pc-overview-links'); overviewLinks.id = 'pc-overview-links';
  $('pc-metrics').after(overviewLinks);
  const knowledgeSummary = node('section'); knowledgeSummary.id = 'pc-knowledge-summary'; overviewLinks.after(knowledgeSummary);
  const panelAction = button('Add task', () => openTodo()); panelAction.id = 'pc-panel-action';
  $('pc-task-filter').after(panelAction);
  $('pc-task-filter').append(new Option('Archived', 'archived'));
  const todoDialog = node('dialog', undefined, 'pc-dialog'); todoDialog.id = 'pc-todo-dialog'; todoDialog.setAttribute('aria-labelledby', 'pc-todo-heading');
  todoDialog.innerHTML = `<div class="pc-dialog-header"><div><h2 id="pc-todo-heading">Add a task</h2><p>Keep the next steps in the project backlog.</p></div><button type="button" class="pc-button pc-dialog-close" aria-label="Close task editor">×</button></div><form id="pc-todo-form"><fieldset id="pc-todo-fields"><label for="pc-todo-title">Task title</label><input id="pc-todo-title" required maxlength="240"><label for="pc-todo-objective">Task objective</label><textarea id="pc-todo-objective" required maxlength="16000" rows="3"></textarea><div class="pc-form-grid"><div><label for="pc-todo-agent">Owner</label><select id="pc-todo-agent"></select></div><div><label for="pc-todo-state">Task status</label><select id="pc-todo-state"><option value="todo">To do</option><option value="ready">Ready</option><option value="done">Done</option><option value="blocked">Blocked</option><option value="cancelled">Cancelled</option><option value="archived">Archived</option></select></div></div><fieldset id="pc-todo-dependencies" class="pc-members"><legend>Wait for these tasks</legend></fieldset><label for="pc-todo-result">Result or handoff notes (optional)</label><textarea id="pc-todo-result" maxlength="16000" rows="3"></textarea></fieldset><p id="pc-todo-error" class="pc-error" role="alert" hidden></p><div class="pc-dialog-footer"><p>The lead can incorporate this task into a future plan.</p><button id="pc-todo-save" type="submit" class="pc-button primary">Save task</button></div></form>`;
  document.body.append(todoDialog);
  const noteDialog = node('dialog', undefined, 'pc-dialog'); noteDialog.id = 'pc-note-dialog'; noteDialog.setAttribute('aria-labelledby', 'pc-note-heading');
  noteDialog.innerHTML = `<div class="pc-dialog-header"><div><h2 id="pc-note-heading">Add to the project record</h2><p>Share a finding, decision, or context for the team.</p></div><button type="button" class="pc-button pc-dialog-close" aria-label="Close project note">×</button></div><form id="pc-note-form"><fieldset id="pc-note-fields"><label for="pc-note-kind">Entry type</label><select id="pc-note-kind"><option value="note">Note</option><option value="finding">Finding</option><option value="decision">Decision</option></select><label for="pc-note-text">Project note</label><textarea id="pc-note-text" required maxlength="16000" rows="6"></textarea></fieldset><p id="pc-note-error" class="pc-error" role="alert" hidden></p><div class="pc-dialog-footer"><p>Entries are saved in this project's timeline.</p><button id="pc-note-save" type="submit" class="pc-button primary">Save entry</button></div></form>`;
  document.body.append(noteDialog);
  const reviewDialog = node('dialog', undefined, 'pc-dialog'); reviewDialog.id = 'pc-review-dialog'; reviewDialog.setAttribute('aria-labelledby', 'pc-review-heading');
  reviewDialog.innerHTML = `<div class="pc-dialog-header"><div><h2 id="pc-review-heading">Record your review</h2><p>Check task results and external actions before clearing the hold. Clearing the hold permits a new request; it does not retry previous work.</p></div><button type="button" class="pc-button pc-dialog-close" aria-label="Close outcome review">×</button></div><form id="pc-review-form"><label for="pc-review-note">What did you verify?</label><textarea id="pc-review-note" required maxlength="2000" rows="5"></textarea><p id="pc-review-error" class="pc-error" role="alert" hidden></p><div class="pc-dialog-footer"><button id="pc-review-save" type="submit" class="pc-button primary">Record review and clear hold</button></div></form>`;
  document.body.append(reviewDialog);

  const visible = () => initialized && !$('work-view').hidden && !root.hidden && !document.hidden;
  const work = () => detail?.state || {};
  const team = () => work().team || null;
  const autonomy = () => work().autonomy || {};
  const todos = () => work().todos || [];
  const activities = () => [...new Map([...(detail?.activity?.items || []), ...olderActivity].map(item => [item.id, item])).values()].sort((a, b) => b.sequence - a.sequence);
  const blockers = () => [...new Set([...(detail?.blocked_reasons || []), ...(work().blocked_reasons || [])])];
  const profile = id => { const resolved = detail?.member_profiles?.find(member => member.agent_id === id); return resolved ? resolved.profile : catalog?.agents?.find(agent => agent.id === id); };
  const profileName = id => team()?.members?.[id]?.name || profile(id)?.name || human(id) || 'Project lead';
  const projectName = project => project?.subject || project?.name || 'Untitled project';
  const driveReady = () => Boolean(resourceProject?.drive?.enabled && resourceProject.drive.folder_id && resourceProject.drive.status === 'ready');
  const endpoint = (suffix = '') => '/v1/projects/' + encodeURIComponent(selected) + suffix;
  const status = (message, error = false) => { $('pc-status').textContent = message; $('pc-status').classList.toggle('error', error); };
  const feedback = (id, message, error = false) => { $(id).textContent = message; $(id).classList.toggle('error', error); };
  function button(label, callback, className = '') {
    const result = node('button', label, 'pc-button ' + className); result.type = 'button';
    result.onclick = async () => { result.disabled = true; try { await callback(); } catch (error) { status(error.message, true); } finally { if (result.isConnected) result.disabled = false; } };
    return result;
  }
  function empty(title, description, action) {
    const result = node('div', undefined, 'pc-empty'); result.append(node('h3', title), node('p', description)); if (action) result.append(action); return result;
  }
  function schedule() { clearTimeout(timer); if (visible()) timer = setTimeout(() => refresh(true), 10000); }
  function acceptDetail(current) {
    detail = current;
    for (const run of current.runs || []) if (savedRuns.has(run.id)) savedRuns.set(run.id, run);
    // New entries shift offset pagination. Reset older pages rather than skip entries.
    if (activityCount !== work().activity_count) {
      olderActivity = []; nextActivityOffset = current.activity?.next_offset ?? null;
      activityCount = work().activity_count;
    }
    renderDetail();
    window.dispatchEvent(new CustomEvent('simon-project-command-update', {detail: {projectId: selected, detail: current}}));
    if (!resourceProject && !resourceLoading) loadResources();
  }
  async function refresh(quiet = false) {
    if (!visible() || loading) return;
    loading = true; const request = ++generation; const projectId = selected;
    $('pc-refresh').disabled = true;
    if (!quiet) status('Loading your projects…');
    try {
      const [list, configured, current] = await Promise.all([
        api('/v1/projects'), api('/v1/agent-platform/catalog'), projectId ? api(endpoint('/command')) : Promise.resolve(null),
      ]);
      if (request !== generation) return;
      projects = list; catalog = configured;
      renderProjects();
      if (projectId === selected && current) acceptDetail(current);
      else if (!selected && projects.length) { await selectProject(projects[0].id, false); }
      else if (!projects.length) renderEmpty();
      status('Saved work · updated ' + new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}));
    } catch (error) {
      status(error.message + ' Use Refresh projects to try again.', true);
      if (!detail) $('pc-empty').replaceChildren(empty('Your workspace is unavailable', 'Your saved work is still on the server. Refresh when the connection is restored.'));
    } finally { loading = false; $('pc-refresh').disabled = false; schedule(); }
  }
  function renderEmpty() {
    $('pc-project-body').hidden = true; $('pc-empty').hidden = false;
    const welcome = empty('Create a project', 'Choose a lead and team, then describe the work. Tasks, files, and findings stay with the project.', button('Create your first project', openCreate, 'primary'));
    welcome.prepend(node('span', '↗', 'pc-empty-mark')); $('pc-empty').replaceChildren(welcome);
  }
  function renderProjects() {
    $('pc-project-count').textContent = projects.length;
    const query = $('pc-project-search').value.trim().toLowerCase();
    const filtered = projects.filter(project => projectName(project).toLowerCase().includes(query));
    const list = $('pc-project-list'); const key = JSON.stringify([selected, query, projects]);
    if (list.dataset.fingerprint === key) return;
    list.dataset.fingerprint = key; list.replaceChildren();
    for (const project of filtered) {
      const item = button('', () => selectProject(project.id)); item.className = 'pc-project'; item.setAttribute('aria-current', String(project.id === selected));
      item.append(node('span', initials(projectName(project)), 'pc-project-mark'));
      const copy = node('span', undefined, 'pc-project-copy'); copy.append(node('strong', projectName(project)), node('small', project.content || 'Project workspace')); item.append(copy); list.append(item);
    }
    if (!filtered.length) list.append(node('p', query ? 'No matching projects.' : 'Your projects will appear here.', 'muted'));
  }
  async function selectProject(id, updateUrl = true) {
    if (selected) drafts.set(selected, $('pc-command').value);
    selected = id; ++selectionEpoch; detail = null; tab = 'overview'; resourceProject = null; localFiles = null; driveFiles = null; resourceLoading = false; historyPage = null; historyLoading = false; historyError = ''; resourceError = ''; localError = ''; driveError = ''; commandSubmission = null; olderActivity = []; nextActivityOffset = null; activityCount = null; archivedTasks = null; nextArchiveOffset = null; savedRuns.clear(); ++generation;
    window.dispatchEvent(new CustomEvent('simon-project-changing', {detail: {projectId: id}}));
    $('pc-project-body').dataset.fingerprint = ''; $('pc-panel-content').dataset.fingerprint = '';
    if (updateUrl && window.SimonWork) {
      window.SimonWork.prepareProject(id);
      $('project-page').insertBefore(root, $('project-resources')); root.hidden = false; root.classList.add('pc-embedded'); root.setAttribute('aria-labelledby', 'pc-project-title');
    }
    $('pc-command').value = drafts.get(id) || ''; feedback('pc-command-status', '');
    $('pc-project-body').hidden = true; $('pc-empty').hidden = false;
    $('pc-empty').replaceChildren(empty('Opening project…', 'Loading the team, task list, and saved progress.'));
    if (updateUrl) history.replaceState(null, '', appPath('/chat?project=' + encodeURIComponent(id)));
    renderProjects(); const request = generation;
    try {
      const [current, configured] = await Promise.all([api(endpoint('/command')), catalog ? Promise.resolve(catalog) : api('/v1/agent-platform/catalog')]);
      if (request !== generation) return;
      catalog = configured;
      acceptDetail(current);
      if (updateUrl) await window.SimonWork?.openProject(id);
    } catch (error) {
      if (request !== generation) return;
      status(error.message, true); $('pc-empty').replaceChildren(empty('Could not open this project', error.message, button('Try again', () => selectProject(id))));
    } finally { schedule(); }
  }
  function renderDetail() {
    if (!detail) return;
    const project = detail.project || projects.find(item => item.id === selected);
    const fingerprint = JSON.stringify([selected, work(), detail.member_profiles, detail.activity, detail.plans, detail.runs, detail.external_actions, blockers(), catalog]);
    $('pc-project-body').hidden = false; $('pc-empty').hidden = true;
    $('pc-project-title').textContent = projectName(project); $('pc-project-description').textContent = project?.content || project?.description || '';
    const mode = autonomy(); const records = todos(); const assigned = team();
    const currentState = mode.paused ? 'paused' : work().active_cycle?.phase || (blockers().length && assigned ? 'blocked' : records.some(item => active(item.status)) ? 'running' : records.some(item => attention(item.status)) ? 'blocked' : assigned ? 'ready' : 'draft');
    $('pc-project-state').replaceChildren(badge(currentState));
    const leadName = assigned ? profileName(assigned.lead_agent_id) : 'Choose your project lead';
    $('pc-lead-name').textContent = leadName; $('pc-lead-avatar').textContent = initials(leadName);
    $('pc-command-label').textContent = assigned ? 'What should ' + leadName + ' work on?' : 'What should the team work on?';
    $('pc-command-submit').disabled = saving || !assigned || mode.paused || Boolean(work().active_cycle) || blockers().length > 0;
    $('pc-command-note').textContent = !assigned ? 'Choose a team and lead in Team & autonomy to start.' : mode.paused ? 'Resume the project before asking the lead to start more work.' : work().active_cycle ? 'A cycle is underway. You can draft your next request here while it finishes.' : blockers().length ? 'Resolve the project blockers before starting another cycle.' : mode.mode === 'scheduled' ? 'The lead can start delegated work within the saved project limits.' : 'The lead prepares a plan for you to review before its tasks run.';
    const reasons = blockers();
    const notice = $('pc-blockers'); notice.hidden = !reasons.length;
    if (reasons.length) { notice.replaceChildren(node('strong', 'This project needs attention')); const list = node('ul'); reasons.forEach(reason => list.append(node('li', reason))); notice.append(list); }
    if ($('pc-project-body').dataset.fingerprint === fingerprint) return;
    $('pc-project-body').dataset.fingerprint = fingerprint;
    const metrics = [['Open tasks', records.filter(item => !done(item.status)).length], ['In progress', records.filter(item => active(item.status)).length], ['Need attention', records.filter(item => attention(item.status)).length], ['Completed', records.filter(item => ['done', 'completed', 'succeeded'].includes(item.status)).length]];
    $('pc-metrics').replaceChildren(...metrics.map(([label, count]) => { const cell = node('div'); cell.append(node('strong', count), node('span', label)); return cell; }));
    renderCycle(); renderPanel(); renderContext(); renderOverviewLinks(); renderSections();
  }
  function renderContext() {
    const assigned = team(); const policy = autonomy(); const aside = $('pc-context'); aside.replaceChildren();
    const people = node('section', undefined, 'pc-context-card'); people.append(node('h4', 'The project team'), node('strong', assigned?.name || 'Choose a team'));
    if (!assigned) people.append(node('p', 'Choose agents whose capabilities cover the outcome, then select one to lead. One agent can handle several responsibilities.'));
    for (const id of assigned?.agent_ids || []) {
      const person = node('div', undefined, 'pc-person'); person.dataset.memberId = id; const copy = node('div'); copy.append(node('strong', profileName(id)), node('small', id === assigned.lead_agent_id ? 'Project lead' : 'Team member'));
      const description = assigned.members?.[id]?.description || profile(id)?.description;
      if (description) copy.append(node('small', description, 'pc-person-description'));
      const ids = assigned.members?.[id]?.skill_ids || skillsForProfile(profile(id));
      copy.append(node('small', skillNames(ids).join(' · '), 'pc-person-skills'));
      const resolved = detail?.member_profiles?.find(member => member.agent_id === id);
      const reasons = [...new Set([...(resolved?.blocked_reasons || []), ...ids.flatMap(skillId => catalog?.individual_skills?.find(skill => skill.id === skillId)?.blocked_reasons || [])])];
      if (reasons.length) copy.append(node('small', reasons.map(memberReason).join(' '), 'pc-skill-readiness'));
      if (assigned.roles?.[id]) copy.append(node('small', assigned.roles[id], 'pc-person-responsibilities'));
      const configure = button('Edit skills', () => { openSettings(); editTeamMember('pc-settings', id); }, 'pc-text-button'); configure.setAttribute('aria-label', 'Edit skills: ' + profileName(id)); copy.append(configure);
      person.append(node('span', initials(profileName(id)), 'pc-avatar'), copy); people.append(person);
    }
    people.append(button(assigned ? 'Edit team' : 'Choose team', openSettings, 'pc-text-button')); aside.append(people);
    const control = node('section', undefined, 'pc-context-card'); control.append(node('h4', 'Autonomy'), node('strong', policy.paused ? 'Project paused' : policy.mode === 'scheduled' ? 'Scheduled progress' : 'You review each plan'));
    control.append(node('p', policy.paused ? 'Future starts are paused. Work already running may finish and its results will be saved.' : policy.mode === 'scheduled' ? 'Your lead checks for the next useful step within these limits.' : 'The lead proposes work. You decide when to start it.'));
    const rows = [['Cadence', policy.mode === 'scheduled' ? policy.cadence_minutes + ' min' : 'On request'], ['Cycles', policy.mode === 'scheduled' ? work().scheduled_cycles_used + ' / ' + policy.max_cycles : String(work().cycle_count || 0)], ['Model budget', money(policy.model_budget_usd)]];
    for (const [label, value] of rows) { const row = node('div', undefined, 'pc-policy-row'); row.append(node('span', label), node('strong', value)); control.append(row); }
    if (policy.objective && policy.mode === 'scheduled') control.append(node('p', policy.objective));
    if (work().next_cycle_at && !policy.paused) control.append(node('p', 'Next check-in ' + date(work().next_cycle_at)));
    if (policy.mode === 'scheduled' && work().scheduled_cycles_used >= policy.max_cycles) control.append(node('p', 'Cycle limit reached. Adjust the limit to continue scheduled work.'));
    if (assigned) { const toggle = button(policy.paused ? 'Resume project' : 'Pause project', () => controlProject(policy.paused ? 'resume' : 'pause')); toggle.disabled = policy.paused && Boolean(work().blocked_reasons?.length); control.append(toggle); }
    control.append(button('Adjust limits', openSettings, 'pc-text-button')); aside.append(control);
  }
  function renderCycle() {
    const cycle = work().active_cycle || work().last_cycle;
    cyclePanel.hidden = !cycle;
    if (!cycle) return;
    const summaryOpen = cyclePanel.querySelector('details')?.open;
    cyclePanel.replaceChildren();
    const top = node('div', undefined, 'pc-task-top'); top.append(node('h4', (work().active_cycle ? 'Current' : 'Last') + ' cycle · ' + cycle.number), badge(cycle.phase)); cyclePanel.append(top);
    const messages = {starting: 'Your request is saved and waiting for the coordinator.', planning: 'The lead is preparing a plan and assigning the work.', ready: cycle.execution_approved ? 'Approved. Waiting for the coordinator to start the team.' : 'Your plan is ready. Review the assigned tasks below, then start the team.', executing: 'The team is working through the plan. Results are saved as tasks finish.', completed: 'This cycle is complete. Review the findings or give the lead the next objective.', blocked: 'This cycle stopped. Review the reason and saved results before continuing.', unknown: 'An action may have completed. Verify its actual outcome before starting more work.', cancelled: 'This cycle was discarded. Its record remains available.'};
    cyclePanel.append(node('p', messages[cycle.phase] || human(cycle.phase)));
    if (cycle.error) cyclePanel.append(node('p', cycle.error, 'pc-error'));
    const request = node('details'); request.open = Boolean(summaryOpen); request.append(node('summary', 'Original request'), node('p', cycle.instruction)); cyclePanel.append(request);
    const plan = detail.plans?.find(item => item.id === cycle.execution_plan_id);
    if (plan) {
      const costs = (plan.tasks || []).map(task => task.model?.estimated_cost_usd);
      const estimate = costs.length && costs.every(value => value != null) ? costs.reduce((sum, value) => sum + value, 0) : null;
      cyclePanel.append(node('p', estimate == null ? 'Model estimate unavailable. ' + money(cycle.model_budget_usd) + '.' : 'Estimated model cost: $' + Number(estimate).toFixed(4) + '. ' + money(cycle.model_budget_usd) + '.'));
      for (const reason of [...new Set((plan.tasks || []).flatMap(task => task.blocked_reasons || []))]) cyclePanel.append(node('p', reason, 'pc-error'));
    }
    const actions = node('div', undefined, 'pc-task-actions');
    if (work().active_cycle?.phase === 'ready') {
      if (!cycle.execution_approved) { const run = button('Start delegated work', () => controlProject('run_ready'), 'primary'); run.disabled = Boolean(autonomy().paused || blockers().length); actions.append(run); }
      actions.append(button('Discard this plan', () => controlProject('discard')));
    } else if (work().active_cycle?.phase === 'starting' && !cycle.planning_run_id) actions.append(button('Discard request', () => controlProject('discard')));
    if (!work().active_cycle && work().blocked_reasons?.length) actions.append(button('Review and clear hold', openReview));
    cyclePanel.append(actions);
    if (detail.external_actions?.length) {
      const external = node('div', undefined, 'pc-external-actions'); external.append(node('h4', 'External actions for this project'));
      const states = {pending: 'Needs your review', executing: 'Submitting', accepted: 'Provider accepted', succeeded: 'Provider confirmed', failed: 'Not completed', unknown: 'Outcome needs review', cancelled: 'Cancelled', expired: 'Review expired'};
      for (const proposal of detail.external_actions) {
        const item = node('div', undefined, 'pc-external-action');
        const copy = node('div'); copy.append(node('strong', proposal.draft.summary), node('small', states[proposal.status] || human(proposal.status)));
        item.append(copy, button('Review external action', async () => {
          if (!window.simonExternalActions?.open) throw Error('The review panel is still loading. Refresh and try again.');
          await window.simonExternalActions.open(proposal.id);
        })); external.append(item);
      }
      cyclePanel.append(external);
    }
  }
  function setTab(value) {
    tab = value === 'findings' ? 'knowledge' : value;
    root.querySelectorAll('[data-pc-tab]').forEach(item => { const current = item.dataset.pcTab === tab; item.setAttribute('aria-selected', String(current)); item.tabIndex = current ? 0 : -1; });
    $('pc-panel').setAttribute('aria-labelledby', 'pc-tab-' + tab); renderPanel(); renderSections();
    if (tab === 'files') { loadResources(true); window.SimonProjectOutputs?.refresh(); }
    if (tab === 'history' && !historyPage) loadHistory();
    if (tab === 'sessions') window.SimonWork?.openProject(selected);
  }
  function renderSections() {
    root.querySelectorAll('[data-pc-tab]').forEach(item => { const current = item.dataset.pcTab === tab; item.setAttribute('aria-selected', String(current)); item.tabIndex = current ? 0 : -1; });
    overviewPanel.hidden = tab !== 'overview';
    $('pc-panel').hidden = !['overview', 'tasks', 'knowledge', 'activity'].includes(tab);
    $('pc-panel').setAttribute('aria-labelledby', 'pc-tab-' + (tab === 'overview' ? 'overview' : tab));
    for (const [id, panel] of Object.entries(workspacePanels)) panel.hidden = tab !== id;
    $('pc-context').hidden = !['overview', 'team'].includes(tab);
    (tab === 'team' ? workspacePanels.team : contentGrid).append($('pc-context'));
    if (tab === 'files') renderFiles();
    if (tab === 'history') renderHistory();
    if (tab === 'sessions') renderSessions();
  }
  function renderOverviewLinks() {
    const target = $('pc-overview-links'); target.replaceChildren();
    const entries = [
      ['files', 'Files & outputs', localFiles ? String(localFiles.total ?? localFiles.files.length) + ' local items' + (driveReady() ? ' · Drive linked' : ' · on your server') : 'Local storage, Drive, and saved outputs'],
      ['history', 'Run history', String(work().cycle_count || 0) + ' project cycles · every saved run'],
      ['sessions', 'Sessions & background work', resourceProject ? String(new Set((resourceProject.sessions || []).map(item => item.thread_id)).size) + ' conversations · ' + (resourceProject.tasks?.length || 0) + ' background tasks' : 'Saved conversations and background tasks'],
    ];
    for (const [section, title, caption] of entries) {
      const link = button('', () => setTab(section), 'pc-overview-link');
      link.append(node('strong', title), node('span', caption), node('span', '↗', 'pc-link-arrow')); target.append(link);
    }
  }
  async function loadResources(includeDrive = false) {
    if (!selected || resourceLoading) return;
    const epoch = selectionEpoch; resourceLoading = true; resourceError = ''; localError = '';
    try {
      const [project, listing] = await Promise.allSettled([
        api(endpoint()), api('/v1/local-files/list?' + new URLSearchParams({root: 'project:' + selected, path: '', offset: 0})),
      ]);
      if (selectionEpoch !== epoch) return;
      if (project.status === 'fulfilled') resourceProject = project.value;
      else resourceError = project.reason.message;
      if (listing.status === 'fulfilled') localFiles = listing.value;
      else localError = listing.reason.message;
      renderOverviewLinks();
      if ((includeDrive || tab === 'files') && driveReady()) {
        try { const result = await api(endpoint('/files')); if (selectionEpoch === epoch) { driveFiles = result; driveError = ''; } }
        catch (error) { if (selectionEpoch === epoch) driveError = error.message; }
      }
    } finally {
      if (selectionEpoch === epoch) { resourceLoading = false; if (tab === 'files') renderFiles(); }
    }
  }
  function sectionHeading(title, description, action) {
    const header = node('div', undefined, 'pc-section-heading'); const copy = node('div'); copy.append(node('h3', title), node('p', description)); header.append(copy); if (action) header.append(action); return header;
  }
  const fileSize = size => size == null ? '' : size < 1024 ? size + ' B' : size < 1048576 ? Math.ceil(size / 1024) + ' KB' : (size / 1048576).toFixed(1) + ' MB';
  function externalLink(label, url) {
    try { const value = new URL(url); if (value.protocol !== 'https:' || value.username || value.password) return node('span', label); }
    catch (_) { return node('span', label); }
    const link = node('a', label, 'pc-file-link'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; return link;
  }
  function fileRow(name, description, actions) {
    const row = node('article', undefined, 'pc-resource-row'); const copy = node('div'); copy.append(node('strong', name), node('small', description)); row.append(copy); const controls = node('div', undefined, 'pc-resource-actions'); controls.append(...actions); row.append(controls); return row;
  }
  function renderFiles() {
    const target = workspacePanels.files; target.replaceChildren(sectionHeading('Project files', 'Files on your server, linked Drive documents, and generated outputs.', button('Refresh project files', () => Promise.all([loadResources(true), window.SimonProjectOutputs?.refresh()]))));
    if (resourceError) target.append(node('p', resourceError, 'pc-notice error'));
    const local = node('section', undefined, 'pc-file-section'); local.id = 'pc-local-files';
    local.append(sectionHeading('Local storage', 'Stored on Simon’s server and available from your signed-in devices.', button('Browse local files', () => window.SimonLocalFiles?.open('project:' + selected))));
    if (localError) local.append(node('p', localError, 'pc-notice error'));
    else if (!localFiles) local.append(node('p', 'Loading local files…', 'muted'));
    else if (!localFiles.files.length) local.append(node('p', 'No local files yet. Open the file browser to upload files or create a folder.', 'pc-section-empty'));
    else for (const file of localFiles.files) {
      const params = new URLSearchParams({root: 'project:' + selected, path: file.path});
      const actions = [];
      if (file.kind === 'folder') actions.push(button('Open folder', () => window.SimonLocalFiles?.open('project:' + selected, file.path), 'pc-text-button'));
      else { const download = node('a', 'Download', 'pc-file-link'); download.href = appPath('/v1/local-files/download?' + params); download.download = file.name; actions.push(download); }
      local.append(fileRow(file.name, file.kind === 'folder' ? 'Folder' : [fileSize(file.bytes), file.modified_at ? date(file.modified_at * 1000) : ''].filter(Boolean).join(' · '), actions));
    }
    if (localFiles?.next_offset != null) local.append(button('Browse all local files', () => window.SimonLocalFiles?.open('project:' + selected), 'pc-text-button'));
    target.append(local);
    const drive = node('section', undefined, 'pc-file-section'); drive.id = 'pc-drive-files';
    const linked = driveReady();
    drive.append(sectionHeading('Google Drive', linked ? 'Live documents in the project’s linked Drive folder.' : 'Connect a Drive folder to keep shared documents with this project.', button(linked ? 'Browse Drive files' : 'Choose Drive folder', () => resourceProject && (linked ? window.SimonWork?.browseDrive(resourceProject) : window.SimonWork?.chooseDriveFolder(resourceProject)))));
    if (driveError) drive.append(node('p', driveError, 'pc-notice error'));
    else if (linked && !driveFiles) drive.append(node('p', 'Loading Drive files…', 'muted'));
    else if (!linked) drive.append(node('p', resourceProject?.drive?.error || 'Drive is not linked. Local storage is available.', 'pc-section-empty'));
    else if (!driveFiles.files?.length) drive.append(node('p', 'The linked Drive folder is empty.', 'pc-section-empty'));
    else for (const file of driveFiles.files) drive.append(fileRow(file.name, file.modifiedTime ? 'Updated ' + date(file.modifiedTime) : file.mimeType, [externalLink('Open in Drive', file.url)]));
    if (linked) drive.append(button('Change Drive folder', () => window.SimonWork?.chooseDriveFolder(resourceProject), 'pc-text-button'));
    target.append(drive);
    const generated = node('div'); generated.id = 'pc-generated-files'; target.append(generated); window.SimonProjectOutputs?.mount(generated, selected);
    const outputs = node('section', undefined, 'pc-file-section'); outputs.id = 'pc-background-files'; outputs.append(sectionHeading('Background work files', 'Files created by the project’s background tasks.', button('Browse background work', () => setTab('sessions'))));
    for (const artifact of resourceProject?.artifacts || []) {
      const link = node('a', 'Download', 'pc-file-link'); link.href = appPath('/v1/assistant-tasks/artifacts/' + encodeURIComponent(artifact.id) + '/download'); link.download = artifact.name;
      outputs.append(fileRow(artifact.name, fileSize(artifact.byte_count), [link]));
    }
    if (resourceProject?.artifacts?.length) target.append(outputs);
    if (resourceProject?.activity?.length) {
      const activity = node('details', undefined, 'pc-file-section'); activity.append(node('summary', 'Recent file activity'));
      for (const entry of resourceProject.activity) activity.append(fileRow(human(entry.kind), date(entry.created_at), [badge(entry.status)]));
      target.append(activity);
    }
  }
  function renderSessions() {
    const target = workspacePanels.sessions;
    if (!target.querySelector('.pc-section-heading')) target.prepend(sectionHeading('Sessions & background work', 'Continue a saved conversation or review work that runs independently of the project lead.', button('Start a session', () => resourceProject && window.SimonWork?.startProjectSession(resourceProject), 'primary')));
    let pending = target.querySelector('.pc-session-loading'); if (!pending) { pending = node('p', 'Loading project sessions…', 'pc-session-loading muted'); target.append(pending); }
    pending.hidden = Boolean(window.SimonWork?.mountSessions(target, selected));
    target.querySelector('.pc-section-heading button').disabled = !resourceProject || resourceProject.id !== selected;
  }
  async function loadHistory(cursor = null) {
    if (!selected || historyLoading) return;
    const epoch = selectionEpoch, previous = historyPage; historyLoading = true; historyError = '';
    if (!cursor) { savedRuns.clear(); ++historyGeneration; }
    renderHistory();
    try {
      const params = new URLSearchParams({limit: '20'}); if (cursor) params.set('cursor', cursor);
      const page = await api(endpoint('/runs?' + params));
      if (selectionEpoch !== epoch) return;
      historyPage = cursor ? {...page, items: [...(previous?.items || []), ...page.items]} : page;
    } catch (error) { if (selectionEpoch === epoch) historyError = error.message; }
    finally { if (selectionEpoch === epoch) { historyLoading = false; renderHistory(); } }
  }
  function renderHistory() {
    const target = workspacePanels.history;
    const expanded = [...target.querySelectorAll('details[open]')].map(item => item.dataset.run);
    target.replaceChildren(sectionHeading('Run history', 'Every planning and execution run for this project, with its saved results and files.', button('Refresh run history', () => loadHistory())));
    if (historyError) target.append(node('p', historyError, 'pc-notice error'));
    if (!historyPage && historyLoading) target.append(node('p', 'Loading project runs…', 'muted'));
    else if (historyPage && !historyPage.items.length) target.append(empty(historyPage.next_cursor ? 'No accessible runs on this page' : 'No project runs yet', historyPage.next_cursor ? 'Load earlier runs to continue through the project history.' : 'Ask the lead to begin. Each planning and execution run will be kept here.'));
    for (const item of historyPage?.items || []) {
      const row = node('article', undefined, 'pc-run-row'); const header = node('div', undefined, 'pc-task-top');
      const copy = node('div'); copy.append(node('h4', item.phase === 'planning' ? 'Lead planning' : item.phase === 'execution' ? 'Team execution' : 'Project run'), node('p', [date(item.created_at || item.started_at), item.task_count + ' task' + (item.task_count === 1 ? '' : 's'), item.artifact_count + ' file' + (item.artifact_count === 1 ? '' : 's')].join(' · ')));
      header.append(copy, badge(item.status)); row.append(header);
      const results = node('details'); results.dataset.run = item.id; results.append(node('summary', 'View results & files')); const content = node('div', undefined, 'pc-run-results'); results.append(content); row.append(results);
      const stored = savedRuns.get(item.id);
      if (stored) renderRunResults(content, stored);
      results.open = expanded.includes(item.id);
      results.ontoggle = async () => {
        if (!results.open || savedRuns.has(item.id) || content.dataset.loading) return;
        content.dataset.loading = 'true'; content.replaceChildren(node('p', 'Loading saved results…'));
        const epoch = selectionEpoch, historyRequest = historyGeneration;
        try { const run = await api('/v1/agent-platform/runs/' + encodeURIComponent(item.id)); if (selectionEpoch !== epoch || historyGeneration !== historyRequest) return; savedRuns.set(run.id, run); renderRunResults(content, run); }
        catch (error) { if (selectionEpoch === epoch && historyGeneration === historyRequest) content.replaceChildren(node('p', error.message, 'pc-notice error')); }
        finally { delete content.dataset.loading; }
      };
      target.append(row);
    }
    if (historyPage?.next_cursor) { const cursor = historyPage.next_cursor; const more = button(historyLoading ? 'Loading earlier runs…' : 'Load earlier runs', () => loadHistory(cursor)); more.disabled = historyLoading; target.append(more); }
  }
  function renderRunResults(target, run) {
    target.replaceChildren();
    for (const task of run.tasks || []) {
      const section = node('section', undefined, 'pc-run-task'); const header = node('div', undefined, 'pc-task-top'); header.append(node('strong', profileName(task.agent_id)), badge(task.status)); section.append(header);
      if (task.error_code) section.append(node('p', human(task.error_code), 'pc-error'));
      if (task.output) { const output = node('div', undefined, 'pc-markdown'); output.append(SimonMarkdown.render(task.output)); section.append(output); }
      else section.append(node('p', 'No output was saved for this task.', 'muted'));
      const files = node('div', undefined, 'pc-resource-actions');
      for (const artifact of task.artifacts || []) { const link = node('a', artifact.name, 'pc-file-link'); link.href = appPath('/v1/agent-platform/runs/' + encodeURIComponent(run.id) + '/artifacts/' + encodeURIComponent(artifact.id)); link.download = artifact.name; files.append(link); if (task.status === 'succeeded' && session?.scopes?.includes('jobs:write') && window.SimonProjectOutputs) files.append(window.SimonProjectOutputs.saveButton(selected, run.id, artifact)); }
      section.append(files); target.append(section);
    }
  }
  function renderPanel() {
    if (!detail) return;
    const target = $('pc-panel-content');
    const fingerprint = JSON.stringify([selected, tab, $('pc-task-filter').value, todos(), activities(), work().active_cycle, detail.runs, [...savedRuns.values()], nextActivityOffset, catalog?.agents, archivedTasks, nextArchiveOffset]);
    if (target.dataset.fingerprint === fingerprint) return;
    target.dataset.fingerprint = fingerprint;
    const expanded = [...target.querySelectorAll('[data-detail-key][open]')].map(item => item.dataset.detailKey);
    const focused = target.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
    target.replaceChildren();
    const taskView = ['overview', 'tasks'].includes(tab);
    $('pc-task-filter').hidden = !taskView; $('pc-task-summary').textContent = '';
    panelAction.textContent = taskView ? 'Add task' : 'Add entry';
    panelAction.onclick = () => taskView ? openTodo() : openNote();
    panelAction.hidden = tab === 'knowledge' && !session?.scopes?.includes('jobs:write');
    if (tab === 'knowledge') { window.SimonProjectKnowledge?.mount(target, selected); return; }
    if (taskView) {
      const filter = $('pc-task-filter').value;
      if (filter === 'archived') {
        $('pc-task-summary').textContent = 'Completed work kept for reference';
        if (archivedTasks === null) target.append(empty('Archived tasks', 'Load completed and cancelled tasks from the project archive.', button('Load archived tasks', () => loadArchive())));
        else if (!archivedTasks.length) target.append(empty('No archived tasks yet.', 'Finished tasks can be archived from the task editor.'));
        else target.append(...archivedTasks.map(taskCard));
        if (nextArchiveOffset != null) target.append(button('Load earlier archived tasks', () => loadArchive(nextArchiveOffset)));
        return;
      }
      const records = todos().filter(item => filter === 'all' || filter === 'active' && active(item.status) || filter === 'attention' && attention(item.status) || filter === 'done' && done(item.status));
      $('pc-task-summary').textContent = todos().length + ' execution task' + (todos().length === 1 ? '' : 's') + ' in this project';
      const list = node('div', undefined, 'pc-tasks');
      for (const task of (tab === 'overview' ? records.slice(0, 3) : records)) list.append(taskCard(task));
      if (!records.length) list.append(empty(filter === 'all' ? 'The next step starts with your lead.' : 'No tasks in this view.', filter === 'all' ? 'Describe what you want above. Your lead will break it down and assign the work.' : 'Choose another filter to see the rest of the project.'));
      target.append(list);
      if (tab === 'overview' && records.length > 3) target.append(button('View all execution tasks', () => setTab('tasks'), 'pc-text-button'));
      for (const item of target.querySelectorAll('[data-detail-key]')) item.open = expanded.includes(item.dataset.detailKey);
      if (focused) [...target.querySelectorAll('[data-focus-key]')].find(item => item.dataset.focusKey === focused)?.focus({preventScroll: true});
      return;
    }
    const records = tab === 'findings' ? activities().filter(item => ['finding', 'decision'].includes(item.kind)) : activities();
    if (!records.length) target.append(empty(tab === 'findings' ? 'The useful details will stay here.' : 'The project story starts here.', tab === 'findings' ? 'Findings and decisions from your team will be saved as work progresses.' : 'Requests, progress, blockers, and completed work will appear in this timeline.'));
    if (tab === 'findings') {
      for (const item of records) { const card = node('article', undefined, 'pc-finding'); card.append(node('small', human(item.kind) + ' · ' + date(item.created_at))); const body = node('div', undefined, 'pc-markdown'); body.append(SimonMarkdown.render(item.text || '')); card.append(body); appendActionLink(card, item); target.append(card); }
    } else {
      const list = node('ol', undefined, 'pc-timeline');
      for (const item of records) { const row = node('li'); row.append(node('strong', human(item.kind)), node('small', ' · ' + date(item.created_at)), node('p', item.text || '')); appendActionLink(row, item); list.append(row); }
      target.append(list);
    }
    if (nextActivityOffset != null) target.append(button('Load earlier activity', loadEarlierActivity));
  }
  function appendActionLink(target, item) {
    if (item.action_id && window.simonExternalActions?.open) target.append(button('Review external action', () => window.simonExternalActions.open(item.action_id)));
  }
  async function loadArchive(offset = 0) {
    const epoch = selectionEpoch;
    const page = await api(endpoint('/todos/archived?offset=' + offset + '&limit=50'));
    if (selectionEpoch !== epoch) return;
    archivedTasks = offset ? [...(archivedTasks || []), ...page.items] : page.items;
    nextArchiveOffset = page.next_offset; renderPanel();
  }
  async function loadEarlierActivity() {
    const epoch = selectionEpoch, count = work().activity_count;
    const page = await api(endpoint('/activity?offset=' + nextActivityOffset + '&limit=50'));
    if (selectionEpoch !== epoch || count !== work().activity_count) return;
    olderActivity.push(...page.items); nextActivityOffset = page.next_offset; renderPanel();
  }
  function taskCard(task) {
    const card = node('article', undefined, 'pc-task'); card.dataset.todoId = task.id;
    const top = node('div', undefined, 'pc-task-top'); top.append(node('h4', task.title || task.objective || 'Project task'), badge(task.status)); card.append(top);
    const meta = node('div', undefined, 'pc-task-meta'); meta.append(node('span', task.agent_id ? profileName(task.agent_id) : 'Awaiting assignment'));
    if (task.depends_on?.length) meta.append(node('span', task.depends_on.length + ' prerequisite' + (task.depends_on.length === 1 ? '' : 's')));
    card.append(meta);
    if (active(task.status)) { const progress = node('progress', undefined, 'pc-progress'); progress.max = 100; progress.value = Math.max(0, Math.min(100, Number(task.progress) || 0)); progress.setAttribute('aria-label', 'Progress for ' + (task.title || 'task')); card.append(progress); }
    if (task.error || task.blocked_reason) card.append(node('p', task.error || task.blocked_reason));
    if (task.objective && task.objective !== task.title || task.depends_on?.length) {
      const more = node('details'); more.dataset.detailKey = task.id + '-objective'; more.append(node('summary', 'Task details'));
      if (task.objective) more.append(node('p', task.objective));
      if (task.depends_on?.length) more.append(node('p', 'After: ' + task.depends_on.map(id => todos().find(item => item.id === id)?.title || 'Earlier project task').join(', ')));
      card.append(more);
    }
    if (task.result) { const more = node('details'); more.dataset.detailKey = task.id + '-result'; more.append(node('summary', 'Read result')); const result = node('div', undefined, 'pc-markdown'); result.append(SimonMarkdown.render(task.result)); more.append(result); card.append(more); }
    const actions = node('div', undefined, 'pc-task-actions');
    const run = detail.runs?.find(item => item.id === task.run_id) || savedRuns.get(task.run_id);
    const output = run?.tasks?.find(item => item.id === task.run_task_id);
    for (const artifact of output?.artifacts || []) {
      const link = node('a', artifact.name, 'pc-button');
      link.href = appPath('/v1/agent-platform/runs/' + encodeURIComponent(run.id) + '/artifacts/' + encodeURIComponent(artifact.id)); link.download = artifact.name; actions.append(link);
      if (output.status === 'succeeded' && session?.scopes?.includes('jobs:write') && window.SimonProjectOutputs) actions.append(window.SimonProjectOutputs.saveButton(selected, run.id, artifact));
    }
    if (task.run_id && !run) actions.append(button('Load saved files', async () => {
      const epoch = selectionEpoch;
      const stored = await api('/v1/agent-platform/runs/' + encodeURIComponent(task.run_id));
      if (selectionEpoch !== epoch) return;
      savedRuns.set(stored.id, stored); renderPanel();
    }));
    if (!['running', 'unknown', 'archived'].includes(task.status) && (!task.cycle_id || task.cycle_id !== work().active_cycle?.id)) {
      const edit = button('Edit task', () => openTodo(task)); edit.dataset.focusKey = task.id + '-edit'; edit.setAttribute('aria-label', 'Edit task: ' + task.title); actions.append(edit);
    }
    if (actions.childElementCount) card.append(actions);
    window.SimonProjectBoard?.decorateTask(card, task.id);
    return card;
  }
  function teamFields(prefix, value) {
    if (!value) { const initial = catalog?.teams?.[0]; value = {name: initial?.name || 'Project team', agent_ids: initial?.agent_ids || [], lead_agent_id: initial?.agent_ids?.[0], max_parallel: initial?.max_parallel || 2}; }
    teamDrafts.set(prefix, structuredClone(value.members || {}));
    const wrap = $(prefix + '-team-fields'); wrap.replaceChildren();
    const title = node('h3', 'Choose the team', 'pc-section-label'); wrap.append(title);
    const guidance = node('p', 'Give each member a role and choose its individual skills. One agent can research, write, and check the result. Each member is configured independently for this project.', 'pc-help'); guidance.id = prefix + '-team-guidance'; wrap.append(guidance);
    const templateLabel = node('label', 'Start from a team template'); templateLabel.htmlFor = prefix + '-template';
    const templates = node('select'); templates.id = prefix + '-template'; templates.append(new Option('Custom team', ''));
    for (const entry of catalog?.teams || []) templates.append(new Option(entry.name, entry.id));
    wrap.append(templateLabel, templates);
    const nameLabel = node('label', 'Team name'); nameLabel.htmlFor = prefix + '-team-name';
    const name = node('input'); name.id = prefix + '-team-name'; name.required = true; name.maxLength = 160; name.value = value?.name ?? 'Project team'; wrap.append(nameLabel, name);
    const members = node('fieldset', undefined, 'pc-members'); members.setAttribute('aria-describedby', guidance.id); members.append(node('legend', 'Team members'));
    const agents = [...new Set(value.agent_ids || [])].map(id => ({...((prefix === 'pc-create' ? catalog?.agents?.find(agent => agent.id === id) : profile(id)) || {}), ...(value.members?.[id] || {}), id}));
    for (const agent of agents) {
      const row = node('div', undefined, 'pc-team-member'); row.dataset.memberId = agent.id;
      const label = node('label', undefined, 'pc-member-option'); const input = node('input'); input.type = 'checkbox'; input.value = agent.id; input.checked = Boolean(value?.agent_ids?.includes(agent.id)); input.dataset.pcMember = prefix;
      const copy = node('span'); copy.append(node('strong', agent.name || human(agent.id)), node('small', agent.description || 'Configured agent profile'));
      label.append(input, copy); row.append(label);
      const ids = agent.skill_ids || skillsForProfile(agent); row.append(node('p', skillNames(ids).join(' · ') || 'No skills available', 'pc-team-skill-summary'));
      const configure = button('Configure', () => editTeamMember(prefix, agent.id), 'pc-text-button'); configure.setAttribute('aria-label', 'Configure agent: ' + (agent.name || human(agent.id))); row.append(configure); members.append(row); input.onchange = () => updateLeads(prefix);
    }
    const memberActions = node('div', undefined, 'pc-team-actions'); memberActions.append(node('p', 'Add a member and choose its skills for this project.'), button('Add agent', () => editTeamMember(prefix), 'pc-text-button'));
    wrap.append(memberActions, members);
    const grid = node('div', undefined, 'pc-form-grid'); const leadWrap = node('div'); const leadLabel = node('label', 'Project lead'); leadLabel.htmlFor = prefix + '-lead'; const lead = node('select'); lead.id = prefix + '-lead'; lead.required = true; leadWrap.append(leadLabel, lead);
    const parallelWrap = node('div'); const parallelLabel = node('label', 'Parallel tasks'); parallelLabel.htmlFor = prefix + '-parallel'; const parallel = node('input'); parallel.id = prefix + '-parallel'; parallel.type = 'number'; parallel.min = '1'; parallel.max = String(catalog?.max_parallel || 128); parallel.value = value?.max_parallel || Math.min(2, catalog?.max_parallel || 2); parallel.required = true; parallelWrap.append(parallelLabel, parallel); grid.append(leadWrap, parallelWrap); wrap.append(grid);
    const roles = node('details', undefined, 'pc-roles'); roles.append(node('summary', 'Project responsibilities (optional)'));
    const rolesHelp = node('p', 'Describe the outcomes each agent owns. Include several responsibilities in the same field when they belong together, such as researching sources, drafting a report, and checking its citations.', 'pc-help'); rolesHelp.id = prefix + '-roles-help'; roles.append(rolesHelp);
    for (const agent of agents) {
      const row = node('div'); row.dataset.pcRoleRow = agent.id;
      const label = node('label', (agent.name || human(agent.id)) + ' responsibilities'); label.htmlFor = prefix + '-role-' + agent.id;
      const field = node('textarea'); field.id = label.htmlFor; field.rows = 3; field.maxLength = 2000; field.value = value?.roles?.[agent.id] || ''; field.dataset.pcRole = agent.id; field.placeholder = 'e.g. Research the evidence, write the report, and verify the finished document.'; field.setAttribute('aria-describedby', rolesHelp.id);
      row.append(label, field); roles.append(row);
    }
    wrap.append(roles);
    templates.onchange = () => { const configured = catalog.teams.find(item => item.id === templates.value); if (!configured) return; teamFields(prefix, {...configured, lead_agent_id: configured.agent_ids[0], members: value.members || {}, roles: value.roles || {}}); $(prefix + '-template').value = configured.id; };
    updateLeads(prefix, value?.lead_agent_id);
  }
  function updateLeads(prefix, preferred) {
    const select = $(prefix + '-lead'); if (!select) return;
    const current = preferred || select.value;
    const ids = [...document.querySelectorAll('[data-pc-member="' + prefix + '"]:checked')].map(item => item.value);
    select.replaceChildren(...ids.map(id => new Option(teamDrafts.get(prefix)?.[id]?.name || catalog?.agents?.find(agent => agent.id === id)?.name || profileName(id), id)));
    if (ids.includes(current)) select.value = current;
    if (!ids.length) select.append(new Option('Select at least one team member', ''));
    $(prefix + '-team-fields').querySelectorAll('[data-pc-role-row]').forEach(row => { row.hidden = !ids.includes(row.dataset.pcRoleRow); });
  }
  function teamDraft(prefix) {
    const wrap = $(prefix + '-team-fields');
    return {name: $(prefix + '-team-name').value, agent_ids: [...wrap.querySelectorAll('[data-pc-member]:checked')].map(input => input.value), lead_agent_id: $(prefix + '-lead').value, max_parallel: $(prefix + '-parallel').value, roles: Object.fromEntries([...wrap.querySelectorAll('[data-pc-role]')].map(field => [field.dataset.pcRole, field.value])), members: structuredClone(teamDrafts.get(prefix) || {})};
  }
  const skillNames = ids => ids.map(id => catalog?.individual_skills?.find(skill => skill.id === id)?.name || catalog?.skills?.find(skill => skill.id === id)?.name || 'Unavailable saved skill');
  function skillsForProfile(agent) {
    if (!Array.isArray(agent?.tool_ids)) return [];
    const tools = agent?.tool_ids || [];
    return tools.length ? tools.map(id => catalog?.individual_skills?.find(skill => skill.tool_ids?.length === 1 && skill.tool_ids[0] === id)?.id || 'tool.' + id) : ['analysis'];
  }
  function memberReason(reason) {
    let value = String(reason);
    for (const tool of catalog?.tool_statuses || []) value = value.replaceAll(tool.id, tool.description || 'An integration');
    return value;
  }
  function editTeamMember(prefix, id = null) {
    const draft = teamDraft(prefix), base = prefix === 'pc-create' ? catalog?.agents?.find(agent => agent.id === id) : profile(id);
    const existing = id ? draft.members[id] || base || {} : {};
    memberEdit = {prefix, id: id || 'member-' + crypto.randomUUID().replaceAll('-', ''), draft, template: $(prefix + '-template').value, rolesOpen: $(prefix + '-team-fields').querySelector('.pc-roles').open};
    $('pc-member-heading').textContent = id ? 'Configure team member' : 'Add agent';
    $('pc-member-save-note').textContent = prefix === 'pc-create' ? 'Added to your team draft. Create the project to save it.' : 'Added to your team draft. Save settings to apply changes to this project.';
    const select = $('pc-member-template'); select.replaceChildren(new Option('Start with a blank role', ''), ...(catalog?.agents || []).map(agent => new Option(agent.name, agent.id))); select.value = '';
    $('pc-member-name').value = existing.name || ''; $('pc-member-description').value = existing.description || '';
    selectedMemberSkills = new Set(id ? existing.skill_ids || skillsForProfile(existing) : []);
    memberSkills = [...(catalog?.individual_skills || [])];
    for (const skillId of selectedMemberSkills) if (!memberSkills.some(skill => skill.id === skillId)) {
      const legacy = catalog?.skills?.find(skill => skill.id === skillId);
      memberSkills.push(legacy ? {...legacy, category: 'Saved skills'} : {id: skillId, name: 'Unavailable saved skill', description: 'This saved capability is no longer available. Remove it or ask your server operator to restore access.', category: 'Saved skills', state: 'permission_required', blocked_reasons: []});
    }
    $('pc-member-search').value = ''; $('pc-member-error').hidden = true; renderMemberSkills(); memberDialog.showModal(); $('pc-member-name').focus();
  }
  function renderMemberSkills() {
    const target = $('pc-member-skills'); target.replaceChildren(); const query = $('pc-member-search').value.trim().toLocaleLowerCase();
    const groups = new Map();
    for (const skill of memberSkills) {
      const category = skill.category || 'General';
      if (query && !(skill.name + ' ' + skill.description + ' ' + category).toLocaleLowerCase().includes(query)) continue;
      if (!groups.has(category)) groups.set(category, []); groups.get(category).push(skill);
    }
    for (const [category, skills] of groups) {
      const group = node('fieldset', undefined, 'pc-skill-group'); group.append(node('legend', category)); const grid = node('div', undefined, 'pc-skill-grid');
      for (const skill of skills) {
        const label = node('label', undefined, 'pc-skill-option'); const input = node('input'); input.type = 'checkbox'; input.value = skill.id; input.checked = selectedMemberSkills.has(skill.id); input.setAttribute('aria-label', skill.name);
        const copy = node('span'); copy.append(node('strong', skill.name), node('small', skill.description));
        if (skill.state !== 'configured') { const states = {disabled: 'Disabled', unconfigured: 'Needs setup', unavailable: 'Unavailable', permission_required: 'Permission needed'}; copy.append(node('small', states[skill.state] || 'Needs attention', 'pc-skill-readiness')); }
        if (skill.blocked_reasons?.length) copy.append(node('small', skill.blocked_reasons.map(memberReason).join(' '), 'pc-skill-readiness'));
        input.onchange = () => { if (input.checked) selectedMemberSkills.add(skill.id); else selectedMemberSkills.delete(skill.id); updateMemberSelection(); }; label.append(input, copy); grid.append(label);
      }
      group.append(grid); target.append(group);
    }
    if (!groups.size) target.append(node('p', query ? 'No skills match this search.' : 'No individual skills are available. Ask your server operator to configure capabilities.', 'pc-help'));
    updateMemberSelection();
  }
  function updateMemberSelection() {
    $('pc-member-count').textContent = selectedMemberSkills.size + ' selected';
    $('pc-member-save').disabled = !selectedMemberSkills.size || selectedMemberSkills.size > 128;
  }
  $('pc-member-search').oninput = renderMemberSkills;
  $('pc-member-template').onchange = () => {
    const agent = catalog?.agents?.find(item => item.id === $('pc-member-template').value);
    $('pc-member-name').value = agent?.name || ''; $('pc-member-description').value = agent?.description || '';
    selectedMemberSkills = new Set(agent ? skillsForProfile(agent) : []); renderMemberSkills();
  };
  $('pc-member-close').onclick = () => memberDialog.close();
  $('pc-member-form').onsubmit = event => {
    event.preventDefault();
    const name = $('pc-member-name').value.trim(), description = $('pc-member-description').value.trim();
    if (!name || !description || !selectedMemberSkills.size || selectedMemberSkills.size > 128) { $('pc-member-error').textContent = 'Enter a role title, description, and between 1 and 128 skills.'; $('pc-member-error').hidden = false; return; }
    const {prefix, id, draft, template, rolesOpen} = memberEdit;
    draft.members[id] = {name, description, skill_ids: [...selectedMemberSkills]};
    if (!draft.agent_ids.includes(id)) draft.agent_ids.push(id);
    if (!draft.lead_agent_id) draft.lead_agent_id = id;
    teamFields(prefix, draft); $(prefix + '-template').value = template; $(prefix + '-team-fields').querySelector('.pc-roles').open = rolesOpen;
    memberDialog.close(); $(prefix + '-team-fields').querySelector('[data-member-id="' + id + '"] button')?.focus();
  };
  function readTeam(prefix) {
    const ids = [...document.querySelectorAll('[data-pc-member="' + prefix + '"]:checked')].map(item => item.value);
    if (!ids.length) throw Error('Choose at least one team member.');
    const roles = Object.fromEntries([...$(prefix + '-team-fields').querySelectorAll('[data-pc-role]')].filter(field => ids.includes(field.dataset.pcRole) && field.value.trim()).map(field => [field.dataset.pcRole, field.value.trim()]));
    return {name: $(prefix + '-team-name').value.trim(), agent_ids: ids, lead_agent_id: $(prefix + '-lead').value, max_parallel: Number($(prefix + '-parallel').value), roles, members: Object.fromEntries(Object.entries(teamDrafts.get(prefix) || {}).filter(([id]) => ids.includes(id)))};
  }
  function openCreate() {
    if (!catalog) { status('Wait for the workspace to load, then create a project.', true); return; }
    $('pc-create-error').hidden = true; teamFields('pc-create', null); projectDialog.showModal(); $('pc-create-name').focus();
  }
  function openSettings() {
    if (!detail || !catalog) return;
    settingsSnapshot = {project: selected, version: work().version, paused: Boolean(autonomy().paused)};
    $('pc-settings-project').textContent = projectName(detail.project || projects.find(item => item.id === selected));
    if (work().active_cycle) $('pc-settings-project').textContent += ' · Let this cycle finish, or discard a ready plan, before changing the team or its limits.';
    $('pc-settings-error').hidden = true; teamFields('pc-settings', team());
    const policy = autonomy(); $('pc-autonomy-mode').value = policy.mode || 'manual'; $('pc-cadence').value = policy.cadence_minutes || 60; $('pc-cycle-limit').value = policy.max_cycles || 5; $('pc-budget').value = policy.model_budget_usd ?? ''; $('pc-standing-objective').value = policy.objective || '';
    updateMode(); settingsDialog.showModal();
  }
  function updateMode() {
    const scheduled = $('pc-autonomy-mode').value === 'scheduled'; $('pc-schedule-fields').hidden = !scheduled;
    $('pc-objective-wrap').hidden = !scheduled; $('pc-standing-objective').required = scheduled; $('pc-budget').required = scheduled;
    settingsDialog.querySelector('label[for="pc-budget"]').textContent = 'Model budget per cycle (USD' + (scheduled ? ', required)' : ', optional)');
    $('pc-budget-help').textContent = scheduled ? 'Scheduled work requires configured model prices so the budget can be enforced. The cap covers model usage; external service charges are separate.' : 'A model budget needs configured prices. Without prices, estimates are unavailable. External service charges are separate.';
    $('pc-autonomy-help').textContent = scheduled ? 'The project lead can schedule and start work within your cycle and model-spending limits.' : 'Your lead prepares a plan. You choose when its tasks start.';
  }
  async function reloadDetail(projectId = selected) {
    const request = ++generation;
    const current = await api('/v1/projects/' + encodeURIComponent(projectId) + '/command');
    if (selected === projectId && request === generation) acceptDetail(current);
    schedule();
  }
  async function controlProject(action, note = '', expectedVersion = work().version) {
    const projectId = selected;
    await api(endpoint('/control'), {action, note, expected_version: expectedVersion});
    await reloadDetail(projectId);
    const messages = {pause: 'Project paused. Work already running may finish.', resume: 'Project resumed.', run_ready: 'Plan approved. The coordinator will start the team.', discard: 'Plan discarded. You can give the lead a new request.', acknowledge: 'Review recorded. Resume the project when you are ready to continue.'};
    status(messages[action]);
  }
  function openTodo(todo = null) {
    if (!detail) return;
    todoSnapshot = {project: selected, version: work().version, todo: todo ? {...todo} : null, id: todo?.id || 'todo-' + crypto.randomUUID().replaceAll('-', ''), key: crypto.randomUUID()};
    $('pc-todo-heading').textContent = todo ? 'Edit task' : 'Add a task';
    $('pc-todo-title').value = todo?.title || ''; $('pc-todo-objective').value = todo?.objective || ''; $('pc-todo-state').value = todo?.status || 'todo'; $('pc-todo-result').value = todo?.result || '';
    $('pc-todo-state').querySelector('option[value="archived"]').disabled = !todo || !['done', 'cancelled'].includes(todo.status);
    $('pc-todo-agent').replaceChildren(new Option('Let the lead assign', ''), ...(team()?.agent_ids || []).map(id => new Option(profileName(id), id)));
    $('pc-todo-agent').value = todo?.agent_id || '';
    const dependencies = $('pc-todo-dependencies'); dependencies.replaceChildren(node('legend', 'Wait for these tasks'));
    for (const item of todos().filter(item => item.id !== todo?.id && item.status !== 'archived')) {
      const label = node('label', undefined, 'pc-member-option'); const check = node('input'); check.type = 'checkbox'; check.value = item.id; check.checked = Boolean(todo?.depends_on?.includes(item.id)); label.append(check, node('span', item.title)); dependencies.append(label);
    }
    if (!dependencies.querySelector('input')) dependencies.append(node('p', 'No other project tasks yet.', 'pc-help'));
    $('pc-todo-error').hidden = true; todoDialog.showModal(); $('pc-todo-title').focus();
  }
  function openNote() {
    if (!detail) return;
    noteDialog.dataset.project = selected; $('pc-note-kind').value = tab === 'knowledge' ? 'finding' : 'note'; $('pc-note-error').hidden = true;
    noteDialog.showModal(); $('pc-note-text').focus();
  }
  function openReview() {
    reviewDialog.dataset.version = work().version; $('pc-review-error').hidden = true; reviewDialog.showModal(); $('pc-review-note').focus();
  }
  $('pc-command-form').onsubmit = async event => {
    event.preventDefault(); if (!selected || saving) return;
    const instruction = $('pc-command').value.trim(); if (!instruction) return;
    if (!commandSubmission || commandSubmission.instruction !== instruction || commandSubmission.project !== selected) commandSubmission = {instruction, project: selected, idempotency_key: crypto.randomUUID()};
    const requestedProject = selected; saving = true; $('pc-command-submit').disabled = true; feedback('pc-command-status', 'Sending your request to the lead…');
    try {
      await api(endpoint('/command'), {instruction, idempotency_key: commandSubmission.idempotency_key});
      drafts.delete(requestedProject); commandSubmission = null;
      if (selected === requestedProject) { $('pc-command').value = ''; feedback('pc-command-status', 'Request saved. Your lead’s plan and progress will appear below.'); await reloadDetail(requestedProject); }
    } catch (error) { if (selected === requestedProject) feedback('pc-command-status', error.message, true); }
    finally { saving = false; renderDetail(); }
  };
  $('pc-create-form').onsubmit = async event => {
    event.preventDefault(); $('pc-create-submit').disabled = true; $('pc-create-fields').disabled = true; $('pc-create-error').hidden = true;
    try {
      const chosen = readTeam('pc-create'); const name = $('pc-create-name').value.trim(), description = $('pc-create-goal').value.trim();
      if (!projectSubmission || projectSubmission.name !== name || projectSubmission.description !== description) { projectSubmission = {name, description, idempotency_key: crypto.randomUUID()}; draftProject = null; }
      if (!draftProject) draftProject = await api('/v1/projects', projectSubmission);
      const current = await api('/v1/projects/' + draftProject.id + '/command');
      await api('/v1/projects/' + draftProject.id + '/team', {expected_version: current.state.version, team: chosen}, 'PATCH');
      const id = draftProject.id; projectSubmission = null; draftProject = null; event.target.reset(); projectDialog.close();
      projects = await api('/v1/projects'); await selectProject(id); status('Project created. Tell the lead what you want to work on first.');
    } catch (error) { $('pc-create-error').textContent = error.message; $('pc-create-error').hidden = false; }
    finally { $('pc-create-submit').disabled = false; $('pc-create-fields').disabled = false; }
  };
  $('pc-settings-form').onsubmit = async event => {
    event.preventDefault(); $('pc-settings-save').disabled = true; $('pc-settings-fields').disabled = true; $('pc-settings-error').hidden = true;
    try {
      await api('/v1/projects/' + encodeURIComponent(settingsSnapshot.project) + '/team', {expected_version: settingsSnapshot.version, team: readTeam('pc-settings'), autonomy: {mode: $('pc-autonomy-mode').value, objective: $('pc-standing-objective').value.trim(), cadence_minutes: Number($('pc-cadence').value), max_cycles: Number($('pc-cycle-limit').value), model_budget_usd: $('pc-budget').value === '' ? null : Number($('pc-budget').value), paused: settingsSnapshot.paused}}, 'PATCH');
      settingsDialog.close(); await reloadDetail(settingsSnapshot.project); status('Project team and limits saved.');
    } catch (error) { $('pc-settings-error').textContent = error.message + (error.message.includes('refresh before editing') ? ' Close and reopen settings to load the latest saved version.' : ''); $('pc-settings-error').hidden = false; await refresh(true); }
    finally { $('pc-settings-save').disabled = false; $('pc-settings-fields').disabled = false; }
  };
  $('pc-todo-form').onsubmit = async event => {
    event.preventDefault(); $('pc-todo-save').disabled = true; $('pc-todo-fields').disabled = true; $('pc-todo-error').hidden = true;
    try {
      const task = {...(todoSnapshot.todo || {}), id: todoSnapshot.id, title: $('pc-todo-title').value.trim(), objective: $('pc-todo-objective').value.trim(), agent_id: $('pc-todo-agent').value || null, status: $('pc-todo-state').value, depends_on: [...$('pc-todo-dependencies').querySelectorAll('input:checked')].map(item => item.value), result: $('pc-todo-result').value.trim()};
      task.progress = task.status === 'done' ? 100 : 0;
      const path = '/v1/projects/' + encodeURIComponent(todoSnapshot.project) + '/todos';
      if (todoSnapshot.todo) await api(path + '/' + encodeURIComponent(task.id), {expected_version: todoSnapshot.version, todo: task}, 'PATCH');
      else {
        const content = JSON.stringify(task);
        if (todoSnapshot.content && todoSnapshot.content !== content) todoSnapshot.key = crypto.randomUUID();
        todoSnapshot.content = content;
        await api(path, {todo: task, idempotency_key: todoSnapshot.key});
      }
      todoDialog.close(); archivedTasks = null; nextArchiveOffset = null; await reloadDetail(todoSnapshot.project); status('Project task saved.');
    } catch (error) { $('pc-todo-error').textContent = error.message + (error.message.includes('refresh before editing') ? ' Close and reopen the task to load the latest saved version.' : ''); $('pc-todo-error').hidden = false; }
    finally { $('pc-todo-save').disabled = false; $('pc-todo-fields').disabled = false; }
  };
  $('pc-note-form').onsubmit = async event => {
    event.preventDefault(); $('pc-note-save').disabled = true; $('pc-note-fields').disabled = true; $('pc-note-error').hidden = true;
    const entry = {kind: $('pc-note-kind').value, text: $('pc-note-text').value.trim()}, projectId = noteDialog.dataset.project;
    try {
      const content = JSON.stringify([projectId, entry]);
      if (noteSubmission?.content !== content) noteSubmission = {content, key: crypto.randomUUID()};
      await api('/v1/projects/' + encodeURIComponent(projectId) + '/activity', {entry, idempotency_key: noteSubmission.key});
      noteDialog.close(); $('pc-note-text').value = ''; noteSubmission = null; await reloadDetail(projectId); status('Project entry saved.');
    } catch (error) { $('pc-note-error').textContent = error.message; $('pc-note-error').hidden = false; }
    finally { $('pc-note-save').disabled = false; $('pc-note-fields').disabled = false; }
  };
  $('pc-review-form').onsubmit = async event => {
    event.preventDefault(); $('pc-review-save').disabled = true; $('pc-review-error').hidden = true;
    try { await controlProject('acknowledge', $('pc-review-note').value.trim(), Number(reviewDialog.dataset.version)); reviewDialog.close(); $('pc-review-note').value = ''; }
    catch (error) { $('pc-review-error').textContent = error.message; $('pc-review-error').hidden = false; }
    finally { $('pc-review-save').disabled = false; }
  };
  projectDialog.querySelector('.pc-dialog-close').onclick = () => projectDialog.close();
  settingsDialog.querySelector('.pc-dialog-close').onclick = () => settingsDialog.close();
  for (const dialog of [todoDialog, noteDialog, reviewDialog]) dialog.querySelector('.pc-dialog-close').onclick = () => dialog.close();
  $('pc-project-search').oninput = renderProjects; $('pc-task-filter').onchange = renderPanel; $('pc-autonomy-mode').onchange = updateMode;
  $('pc-new-project').onclick = openCreate; $('pc-settings').onclick = openSettings; $('pc-refresh').onclick = () => refresh();
  $('pc-files').onclick = () => { if (selected) setTab('board'); };
  root.querySelectorAll('[data-pc-tab]').forEach(item => {
    item.onclick = () => setTab(item.dataset.pcTab);
    item.onkeydown = event => {
      const items = [...root.querySelectorAll('[data-pc-tab]')]; let index = items.indexOf(item);
      if (event.key === 'ArrowRight') index = (index + 1) % items.length;
      else if (event.key === 'ArrowLeft') index = (index + items.length - 1) % items.length;
      else if (event.key === 'Home') index = 0; else if (event.key === 'End') index = items.length - 1; else return;
      event.preventDefault(); items[index].focus(); items[index].click();
    };
  });
  window.addEventListener('simon-project-open', event => {
    const page = $('project-page'); page.insertBefore(root, $('project-resources')); root.hidden = false; root.classList.add('pc-embedded'); root.setAttribute('aria-labelledby', 'pc-project-title');
    if (event.detail.id === selected) {
      const changed = resourceProject?.drive?.folder_id !== event.detail.project.drive?.folder_id;
      resourceProject = event.detail.project; if (changed) { driveFiles = null; driveError = ''; }
      renderOverviewLinks(); if (tab === 'files') { renderFiles(); if (changed) loadResources(true); }
      if (tab === 'sessions') renderSessions();
    }
    if (event.detail.id !== selected) selectProject(event.detail.id, false); else if (!loading) refresh(true);
  });
  window.addEventListener('simon-project-close', () => { $('work-status').after(root); root.hidden = false; root.classList.remove('pc-embedded'); root.setAttribute('aria-labelledby', 'pc-heading'); schedule(); });
  window.addEventListener('simon-ready', () => { initialized = true; refresh(true); });
  window.addEventListener('simon-agent-library-updated', event => { catalog = event.detail.catalog; if (detail) { renderContext(); $('pc-project-body').dataset.fingerprint = ''; } });
  window.addEventListener('simon-project-output-saved', event => { if (event.detail.projectId === selected) loadResources(true); });
  document.addEventListener('visibilitychange', () => { if (visible()) refresh(true); else clearTimeout(timer); });
  new MutationObserver(() => { if (visible()) refresh(true); else clearTimeout(timer); }).observe($('work-view'), {attributes: true, attributeFilter: ['hidden']});
  function reuseFile(projectId, path) {
    if (projectId !== selected) return;
    setTab('overview'); const field = $('pc-command'); const reference = 'Use the saved project file "' + path + '" in the next task.';
    field.value = field.value.trim() ? field.value.trimEnd() + '\n\n' + reference : reference + '\n'; drafts.set(selected, field.value); field.focus(); field.scrollIntoView({block: 'center'});
  }
  window.SimonProjectCommand = {refresh, open: selectProject, navigate: setTab, reuseFile, getSnapshot: () => selected && detail ? {projectId: selected, detail} : null};
})();
