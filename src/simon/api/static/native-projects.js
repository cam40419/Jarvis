'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const statuses = {
    todo: 'To do',
    in_progress: 'In progress',
    in_review: 'In review',
    blocked: 'Blocked',
    done: 'Done',
    cancelled: 'Cancelled',
  };
  let session, project, access, projectDraft, taskDraft;
  let projects = [],
    tasks = [],
    teamAgents = [],
    generation = 0,
    loading = false;
  const pending = new Map();
  const uncertain = new Set();
  const formsBusy = new Set();
  const say = (id, message = '', tone = '') => {
    $(id).textContent = message;
    $(id).dataset.tone = tone;
  };
  const button = (text, callback, className = 'np-button') => {
    const item = node('button', text, className);
    item.type = 'button';
    item.onclick = callback;
    return item;
  };
  const date = (value) =>
    new Date(value).toLocaleString(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short',
    });
  const path = (suffix = '') => '/v2/projects/' + project.id + suffix;
  const currentActorName = () =>
    access?.members.find((m) => m.actor_id === session.actor_id)?.display_name;

  async function request(url, body, method = body === undefined ? 'GET' : 'POST') {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(appPath(url), {
        signal: controller.signal,
        method,
        credentials: 'same-origin',
        headers: {
          ...(session && url.startsWith('/v2/') ? { 'X-Workspace-ID': session.workspace_id } : {}),
          ...(body === undefined
            ? {}
            : { 'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (response.status === 401) {
        location.assign(appPath('/login'));
        throw Object.assign(Error('Sign in to continue.'), { status: 401 });
      }
      const data = await response.json();
      if (!response.ok)
        throw Object.assign(Error(data.error?.message || 'Request failed. Please try again.'), {
          status: response.status,
        });
      return data;
    } catch (error) {
      if (controller.signal.aborted) throw Error('The request timed out. Please try again.');
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  async function pages(url, ticket) {
    const result = [];
    for (let offset = 0; offset <= 1000000; offset += 100) {
      const page = await request(url + '?limit=100&offset=' + offset);
      if (ticket !== generation) return [];
      result.push(...page);
      if (page.length < 100) return result;
    }
    throw Error('This board is too large to load. Narrow its scope before continuing.');
  }

  async function agents(id, ticket) {
    const result = [],
      seen = new Set();
    let offset = 0;
    while (offset !== null) {
      const page = await request('/v2/projects/' + id + '/team?limit=100&offset=' + offset);
      if (ticket !== generation) return [];
      for (const agent of page.agents) {
        if (seen.has(agent.id))
          throw Error('The team changed while loading. Refresh to try again.');
        seen.add(agent.id);
        result.push(agent);
      }
      if (page.agents_next_offset !== null && page.agents_next_offset <= offset)
        throw Error('The team could not be loaded. Refresh to try again.');
      offset = page.agents_next_offset;
    }
    return result;
  }

  // An uncertain response retains the exact payload and key until acknowledged.
  // Drafts stay in this tab, never in shared browser storage.
  async function command(slot, url, body, method) {
    const signature = JSON.stringify([url, method, body]);
    let saved = pending.get(slot);
    if (!saved || (!uncertain.has(slot) && saved.signature !== signature)) {
      saved = { signature, url, method, body: { ...body, idempotency_key: crypto.randomUUID() } };
      pending.set(slot, saved);
    }
    try {
      const result = await request(saved.url, saved.body, saved.method);
      pending.delete(slot);
      uncertain.delete(slot);
      return result;
    } catch (error) {
      if (!error.status || error.status >= 500) {
        uncertain.add(slot);
        error.message =
          'The save could not be confirmed. Retry the same save before editing or closing; it will not create a duplicate.';
      } else {
        uncertain.delete(slot);
        pending.delete(slot);
      }
      throw error;
    }
  }

  function lockForm(kind, busy) {
    const form = $('np-' + kind + '-form');
    if (busy) formsBusy.add(kind);
    else formsBusy.delete(kind);
    for (const control of form.querySelectorAll('input, textarea, select, button'))
      control.disabled = busy || (uncertain.has(kind) && control.id !== 'np-' + kind + '-save');
    $('np-' + kind + '-save').textContent = uncertain.has(kind)
      ? 'Retry save'
      : kind === 'member'
        ? 'Save member'
        : kind === 'task'
          ? 'Save task'
          : 'Save project';
    form.setAttribute('aria-busy', String(busy));
    if (kind === 'project')
      $('np-project-status').disabled =
        busy || uncertain.has(kind) || (projectDraft && !access?.permissions.can_archive);
    if (kind === 'member') {
      for (const control of $('np-team-dialog').querySelectorAll('button'))
        control.disabled = busy || (uncertain.has(kind) && control.id !== 'np-member-save');
    }
    if (!busy && kind !== 'member' && !$('np-' + kind + '-conflict').hidden)
      $('np-' + kind + '-save').disabled = true;
  }

  function closeDialog(id) {
    const kind =
      id === 'np-team-dialog' ? 'member' : id === 'np-project-dialog' ? 'project' : 'task';
    if (formsBusy.has(kind) || uncertain.has(kind)) return;
    $(id).close();
    pending.delete(kind);
  }
  document.querySelectorAll('[data-close]').forEach((item) => {
    item.onclick = () => closeDialog(item.dataset.close);
  });
  for (const kind of ['project', 'task', 'member']) {
    const dialog = $(kind === 'member' ? 'np-team-dialog' : 'np-' + kind + '-dialog');
    dialog.addEventListener('cancel', (event) => {
      event.preventDefault();
      closeDialog(dialog.id);
    });
  }
  window.addEventListener('beforeunload', (event) => {
    if (
      uncertain.size ||
      formsBusy.size ||
      document.querySelector('dialog[open] form[data-editing="true"]')
    )
      event.preventDefault();
  });

  function renderProjects() {
    if (loading) return;
    const query = $('np-search').value.trim().toLowerCase(),
      filter = $('np-filter').value;
    const matches = projects.filter(
      (p) =>
        (filter === 'all' || p.status === filter) &&
        (p.name + '\n' + p.objective).toLowerCase().includes(query),
    );
    const cards = matches.map((p) => {
      const card = node('article', undefined, 'np-project-card');
      const heading = node('div', undefined, 'np-project-card-head');
      const title = node('a', p.name, 'np-project-title');
      title.href = appPath('/projects?project=' + p.id);
      title.onclick = (event) => {
        if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        navigate(p.id);
      };
      heading.append(
        title,
        node('span', p.status === 'archived' ? 'Archived' : 'Active', 'np-badge'),
      );
      card.append(
        heading,
        node('p', p.objective, 'np-card-objective'),
        node('p', 'Updated ' + date(p.updated_at), 'np-card-meta'),
      );
      return card;
    });
    $('np-projects').replaceChildren(
      ...(cards.length
        ? cards
        : [
            node(
              'p',
              projects.length
                ? 'No projects match these filters.'
                : 'Start with an outcome. Create your first project to organize its work.',
              'np-empty',
            ),
          ]),
    );
  }

  function renderDetail() {
    $('np-name').textContent = project.name;
    $('np-objective').textContent = project.objective;
    document.title = project.name + ' — Simon';
    $('np-meta').textContent =
      `${project.status === 'archived' ? 'Archived · ' : ''}${tasks.length} ${tasks.length === 1 ? 'task' : 'tasks'} · ${access.members.length} ${access.members.length === 1 ? 'person' : 'people'} · Updated ${date(project.updated_at)}`;
    $('np-edit-project').hidden = !access.permissions.can_edit;
    $('np-new-task').hidden = !access.permissions.can_edit || project.status !== 'active';
    $('np-account').textContent = currentActorName() || 'Account';
    renderBoard();
  }

  function assignmentName(task) {
    if (task.assignment.kind === 'pool')
      return task.status === 'todo' ? 'Available to pick up' : 'Unassigned';
    if (task.assignment.kind === 'agent') {
      const agent = teamAgents.find((a) => a.id === task.assignment.agent_id);
      return agent
        ? `${agent.name} (agent${agent.status === 'active' ? '' : ', ' + agent.status})`
        : 'Agent';
    }
    const member = access.members.find((m) => m.actor_id === task.assignment.actor_id);
    return (
      (member?.display_name || 'Former member') +
      (task.assignment.actor_id === session.actor_id ? ' (you)' : '')
    );
  }
  function renderBoard() {
    if (!project || !access) return;
    const query = $('np-task-search').value.trim().toLowerCase(),
      filter = $('np-task-filter').value;
    const matches = tasks.filter(
      (t) =>
        (t.title + '\n' + t.description).toLowerCase().includes(query) &&
        (filter === 'all' ||
          (filter === 'pool'
            ? t.assignment.kind === 'pool'
            : t.assignment.actor_id === session.actor_id)),
    );
    $('np-board').replaceChildren(
      ...Object.entries(statuses).map(([status, label]) => {
        const column = node('section', undefined, 'np-column');
        column.dataset.status = status;
        column.setAttribute('aria-label', label);
        const heading = node('div', undefined, 'np-column-heading');
        const rows = matches.filter((t) => t.status === status);
        heading.append(node('h2', label), node('span', String(rows.length), 'np-column-count'));
        const list = node('div', undefined, 'np-column-tasks');
        for (const task of rows) {
          const card = node('article', undefined, 'np-task-card');
          card.dataset.taskId = task.id;
          const open = button(task.title, () => editTask(task), 'np-task-open np-task-title');
          open.disabled = uncertain.has('claim:' + task.id);
          card.append(open);
          if (task.description) card.append(node('p', task.description, 'np-task-description'));
          card.append(node('p', assignmentName(task), 'np-task-meta'));
          if (
            uncertain.has('claim:' + task.id) ||
            (access.permissions.can_claim && status === 'todo' && task.assignment.kind === 'pool')
          ) {
            const actions = node('div', undefined, 'np-task-actions');
            actions.append(
              button(
                uncertain.has('claim:' + task.id) ? 'Retry claim' : 'Pick up task',
                (event) => claimTask(task, event.currentTarget),
                'np-button np-quiet',
              ),
            );
            card.append(actions);
          }
          list.append(card);
        }
        if (!rows.length) list.append(node('p', 'No tasks', 'np-empty'));
        column.append(heading, list);
        return column;
      }),
    );
  }

  async function load(id, focus = false) {
    window.dispatchEvent(new CustomEvent('native-project-loading', { detail: { id } }));
    const ticket = ++generation;
    loading = true;
    $('np-refresh').disabled = true;
    say('np-status', 'Loading projects…');
    $('np-detail').hidden = true;
    $('np-list').hidden = Boolean(id);
    project = null;
    access = null;
    tasks = [];
    teamAgents = [];
    document.title = 'Projects — Simon';
    if (!id) $('np-projects').replaceChildren(node('p', 'Loading projects…', 'np-empty'));
    try {
      if (id) {
        if (!/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(id))
          throw Error('This project link is invalid. Open Projects to choose a project.');
        const [record, permissions, board, roles] = await Promise.all([
          request('/v2/projects/' + id),
          request('/v2/projects/' + id + '/access'),
          pages('/v2/projects/' + id + '/tasks', ticket),
          agents(id, ticket),
        ]);
        if (ticket !== generation) return;
        project = record;
        access = permissions;
        tasks = board;
        teamAgents = roles;
        $('np-detail').hidden = false;
        renderDetail();
        if (focus) $('np-name').focus();
      } else {
        const found = await pages('/v2/projects', ticket);
        if (ticket !== generation) return;
        projects = found;
        loading = false;
        renderProjects();
        if (focus) $('np-list-heading').focus();
      }
      say('np-status');
    } catch (error) {
      if (ticket !== generation) return;
      window.dispatchEvent(new CustomEvent('native-project-unavailable'));
      projects = [];
      // Never leave a previous project's data or controls visible after access fails.
      $('np-list').hidden = false;
      $('np-projects').replaceChildren(
        node('p', 'Projects could not be loaded.', 'np-empty'),
        button('Try again', () => load(id)),
      );
      say('np-status', error.message, 'error');
    } finally {
      if (ticket === generation) {
        loading = false;
        $('np-refresh').disabled = false;
      }
    }
  }
  function navigate(id) {
    history.pushState(null, '', appPath('/projects' + (id ? '?project=' + id : '')));
    return load(id, true);
  }
  const selected = () => new URLSearchParams(location.search).get('project');
  window.addEventListener('popstate', () => {
    if (document.querySelector('dialog[open]')) {
      history.pushState(null, '', appPath('/projects' + (project ? '?project=' + project.id : '')));
      return;
    }
    load(selected());
  });
  $('np-back').onclick = () => navigate(null);
  $('np-search').oninput = renderProjects;
  $('np-filter').onchange = renderProjects;
  $('np-task-search').oninput = renderBoard;
  $('np-task-filter').onchange = renderBoard;
  $('np-refresh').onclick = () => load(project?.id || selected());

  function editProject(record) {
    $('np-project-intake-choice').hidden = Boolean(record);
    projectDraft = record ? { ...record } : null;
    $('np-project-form').reset();
    $('np-project-form').dataset.editing = 'true';
    $('np-project-heading').textContent = record ? 'Project brief' : 'Start a project';
    $('np-project-name').value = record?.name || '';
    $('np-project-objective').value = record?.objective || '';
    $('np-project-status').value = record?.status || 'active';
    $('np-project-state').hidden = !record || !access.permissions.can_archive;
    $('np-project-conflict').hidden = true;
    say('np-project-feedback');
    lockForm('project', false);
    $('np-project-dialog').showModal();
  }
  $('np-new').onclick = () => editProject(null);
  $('np-edit-project').onclick = () => editProject(project);

  function assignees(selectedActor) {
    const options = [new Option('Available to pick up', '')];
    for (const member of access.members.filter((m) => m.can_assign))
      options.push(
        new Option(
          member.display_name + (member.actor_id === session.actor_id ? ' (you)' : ''),
          member.actor_id,
        ),
      );
    for (const agent of teamAgents.filter((a) => a.status === 'active'))
      options.push(new Option(agent.name + ' (agent)', 'agent:' + agent.id));
    if (selectedActor && !options.some((o) => o.value === selectedActor)) {
      const option = new Option('Unavailable assignee — choose another assignment', selectedActor);
      option.disabled = true;
      options.push(option);
    }
    $('np-task-assignee').replaceChildren(...options);
    $('np-task-assignee').value = selectedActor || '';
  }
  function editTask(record) {
    taskDraft = record ? { ...record } : null;
    $('np-task-form').reset();
    const editable = access.permissions.can_edit && project.status === 'active';
    $('np-task-form').dataset.editing = String(editable);
    $('np-task-heading').textContent = record
      ? editable
        ? 'Edit task'
        : 'Task details'
      : 'Add a task';
    $('np-task-title').value = record?.title || '';
    $('np-task-description').value = record?.description || '';
    $('np-task-status').value = record?.status || 'todo';
    $('np-task-status').closest('.np-field').hidden = !record;
    assignees(
      record?.assignment.kind === 'agent'
        ? 'agent:' + record.assignment.agent_id
        : record?.assignment.actor_id,
    );
    $('np-task-conflict').hidden = true;
    say('np-task-feedback');
    lockForm('task', false);
    for (const control of $('np-task-form').querySelectorAll('input, textarea, select'))
      control.disabled = !editable;
    $('np-task-save').hidden = !editable;
    $('np-task-dialog').showModal();
  }
  $('np-new-task').onclick = () => editTask(null);

  async function conflict(kind, id) {
    const latest = await request(
      '/v2/projects/' + (kind === 'project' ? id : project.id + '/tasks/' + id),
    );
    const draft = kind === 'project' ? projectDraft : taskDraft;
    // Other 409s (archival, assignment eligibility) need their original remedy,
    // not a misleading offer to overwrite an unchanged record.
    if (latest.version === draft.version) return;
    $('np-' + kind + '-latest').textContent =
      kind === 'project'
        ? `${latest.name}\n${latest.objective}\nStatus: ${latest.status}\nVersion ${latest.version}`
        : `${latest.title}\n${latest.description}\nStatus: ${statuses[latest.status]}\nAssignment: ${assignmentName(latest)}\nVersion ${latest.version}`;
    $('np-' + kind + '-conflict').hidden = false;
    $('np-' + kind + '-save').disabled = true;
    $('np-' + kind + '-rebase').onclick = () => {
      if (kind === 'project') {
        projectDraft = latest;
        if (!access.permissions.can_archive) $('np-project-status').value = latest.status;
      } else taskDraft = latest;
      $('np-' + kind + '-conflict').hidden = true;
      $('np-' + kind + '-save').disabled = false;
      say(
        'np-' + kind + '-feedback',
        'Your draft is preserved. Review it against the latest saved version, then save to apply your changes.',
        'warning',
      );
    };
  }

  for (const kind of ['project', 'task']) {
    $('np-' + kind + '-form').onsubmit = async (event) => {
      event.preventDefault();
      if (formsBusy.has(kind) || !$('np-' + kind + '-conflict').hidden) return;
      const record = kind === 'project' ? projectDraft : taskDraft;
      const body =
        kind === 'project'
          ? {
              name: $('np-project-name').value.trim(),
              objective: $('np-project-objective').value.trim(),
              ...(record
                ? { status: $('np-project-status').value, expected_version: record.version }
                : {}),
            }
          : {
              title: $('np-task-title').value.trim(),
              description: $('np-task-description').value.trim(),
              assignment: $('np-task-assignee').value
                ? $('np-task-assignee').value.startsWith('agent:')
                  ? { kind: 'agent', agent_id: $('np-task-assignee').value.slice(6) }
                  : { kind: 'human', actor_id: $('np-task-assignee').value }
                : { kind: 'pool' },
              ...(record
                ? { status: $('np-task-status').value, expected_version: record.version }
                : {}),
            };
      lockForm(kind, true);
      say('np-' + kind + '-feedback', 'Saving…');
      try {
        const result = await command(
          kind,
          kind === 'project'
            ? '/v2/projects' + (record ? '/' + record.id : '')
            : path('/tasks' + (record ? '/' + record.id : '')),
          body,
          record ? 'PUT' : 'POST',
        );
        $('np-' + kind + '-dialog').close();
        if (kind === 'project' && !record) {
          await navigate(result.id);
          if ($('np-project-intake').checked) window.SimonNativeIntake?.open();
        } else await load(kind === 'project' ? result.id : project.id, true);
      } catch (error) {
        say('np-' + kind + '-feedback', error.message, 'error');
        if (error.status === 409 && record) {
          try {
            await conflict(kind, record.id);
          } catch (refreshError) {
            say(
              'np-' + kind + '-feedback',
              refreshError.message + ' Your draft is still here.',
              'error',
            );
          }
        }
      } finally {
        lockForm(kind, false);
      }
    };
  }

  async function claimTask(task, control) {
    const id = project.id,
      ticket = generation;
    control.disabled = true;
    try {
      await command(
        'claim:' + task.id,
        '/v2/projects/' + id + '/tasks/' + task.id + '/claim',
        { expected_version: task.version },
        'POST',
      );
      if (ticket === generation) await load(id, true);
    } catch (error) {
      if (ticket !== generation) return;
      if (error.status === 409) await load(id);
      else renderBoard();
      say('np-status', error.message, 'error');
    }
  }

  function renderTeam() {
    const canManage = access.permissions.can_manage_members;
    $('np-member-form').hidden = !canManage;
    $('np-member-form').dataset.editing = 'false';
    $('np-members').replaceChildren(
      ...access.members.map((member) => {
        const row = node('li', undefined, 'np-member');
        const info = node('div', undefined, 'np-member-info');
        info.append(
          node(
            'strong',
            member.display_name + (member.actor_id === session.actor_id ? ' (you)' : ''),
          ),
          node(
            'p',
            `${member.role === 'owner' ? 'Project owner' : 'Project member'}${!member.active ? ' · Inactive' : member.workspace_role === 'guest' ? ' · Read only' : ''}`,
            'np-muted',
          ),
        );
        row.append(info);
        if (canManage) {
          const actions = node('div', undefined, 'np-task-actions');
          actions.append(
            button(
              'Change role',
              () => {
                $('np-member-actor').replaceChildren(
                  new Option(member.display_name, member.actor_id),
                );
                $('np-member-role').value = member.role;
                $('np-member-role').querySelector('option[value="owner"]').disabled =
                  !member.can_assign;
                $('np-member-save').disabled = false;
                $('np-member-form').dataset.editing = 'true';
                $('np-member-role').focus();
              },
              'np-button np-quiet',
            ),
            button(
              'Remove',
              (event) => removeMember(member, event.currentTarget),
              'np-button np-quiet np-danger',
            ),
          );
          row.append(actions);
        }
        return row;
      }),
    );
    const options = access.member_candidates.map(
      (m) =>
        new Option(
          m.display_name + (m.workspace_role === 'guest' ? ' (read only)' : ''),
          m.actor_id,
        ),
    );
    $('np-member-actor').replaceChildren(new Option('Choose a workspace member', ''), ...options);
    $('np-member-role').value = 'member';
    $('np-member-role').querySelector('option[value="owner"]').disabled = false;
    $('np-member-save').disabled = true;
    $('np-member-form').querySelector('[data-more-members]')?.remove();
    if (canManage && access.candidates_next_offset !== null) {
      const more = button('Load more people', async () => {
        lockForm('member', true);
        try {
          const found = await request(
            path(
              '/access?candidates_offset=' +
                access.candidates_next_offset +
                '&candidates_limit=100',
            ),
          );
          if (found.project_version !== access.project_version) {
            await refreshTeam();
          } else {
            access = {
              ...found,
              member_candidates: [...access.member_candidates, ...found.member_candidates],
            };
          }
        } catch (error) {
          say('np-team-feedback', error.message, 'error');
        } finally {
          lockForm('member', false);
          renderTeam();
        }
      });
      more.dataset.moreMembers = 'true';
      $('np-member-form').append(more);
    }
  }
  $('np-member-actor').onchange = () => {
    const candidate = access.member_candidates.find(
      (m) => m.actor_id === $('np-member-actor').value,
    );
    $('np-member-role').value = 'member';
    $('np-member-role').querySelector('option[value="owner"]').disabled =
      candidate?.workspace_role === 'guest';
    $('np-member-save').disabled = !candidate;
    $('np-member-form').dataset.editing = String(Boolean(candidate));
  };
  async function refreshTeam() {
    const ticket = generation;
    const [updated, permissions] = await Promise.all([request(path()), request(path('/access'))]);
    if (ticket !== generation) return false;
    project = updated;
    access = permissions;
    renderDetail();
    renderTeam();
    return true;
  }
  $('np-team').onclick = async () => {
    $('np-team').disabled = true;
    try {
      if (await refreshTeam()) {
        say('np-team-feedback');
        $('np-team-dialog').showModal();
      }
    } catch (error) {
      say('np-status', error.message, 'error');
    } finally {
      $('np-team').disabled = false;
    }
  };
  $('np-member-form').onsubmit = async (event) => {
    event.preventDefault();
    if (formsBusy.has('member')) return;
    lockForm('member', true);
    let saved = false;
    const removing = uncertain.has('member') && pending.get('member')?.method === 'POST';
    const id = project.id;
    try {
      await command(
        'member',
        path('/members'),
        {
          actor_id: $('np-member-actor').value,
          role: $('np-member-role').value,
          expected_version: access.project_version,
        },
        'PUT',
      );
      saved = true;
      say('np-team-feedback', removing ? 'Member removed.' : 'Member saved.', 'success');
    } catch (error) {
      say('np-team-feedback', error.message, 'error');
    }
    try {
      if (saved && removing) {
        $('np-team-dialog').close();
        await load(id, true);
      } else if (!uncertain.has('member')) {
        await refreshTeam();
      }
    } catch (error) {
      say('np-team-feedback', (saved ? 'Member saved. ' : '') + error.message, 'error');
    } finally {
      lockForm('member', false);
      if (!uncertain.has('member')) $('np-member-save').disabled = !$('np-member-actor').value;
    }
  };
  async function removeMember(member, control) {
    if (formsBusy.has('member')) return;
    lockForm('member', true);
    control.disabled = true;
    try {
      // Reuse the member slot to prevent closing or changing roles after an uncertain removal.
      await command(
        'member',
        path('/members/' + member.actor_id + '/remove'),
        { expected_version: access.project_version },
        'POST',
      );
      $('np-team-dialog').close();
      await load(project.id, true);
    } catch (error) {
      say('np-team-feedback', error.message, 'error');
    }
    try {
      if ($('np-team-dialog').open && !uncertain.has('member')) {
        await refreshTeam();
      }
    } catch (error) {
      say('np-team-feedback', error.message, 'error');
    } finally {
      lockForm('member', false);
      if (!uncertain.has('member')) $('np-member-save').disabled = !$('np-member-actor').value;
    }
  }

  async function start() {
    try {
      session = await request('/auth/session');
      $('np-new').disabled = !session.scopes.includes('jobs:write');
      await load(selected());
    } catch (error) {
      say('np-status', error.message, 'error');
      $('np-projects').replaceChildren(
        node('p', 'Your workspace could not be loaded.', 'np-empty'),
        button('Try again', start),
      );
    }
  }
  // Keep refresh explicit so an active draft is never replaced by a background poll.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && session && !loading && !document.querySelector('dialog[open]'))
      say(
        'np-status',
        project
          ? 'Use Refresh to check for updates from other people.'
          : 'Reopen Projects to check for updates from other people.',
      );
  });
  window.SimonNativeProjects = {
    request,
    getProject: () => project,
    getWorkspaceId: () => session?.workspace_id,
    onTeamChanged: async () => load(project.id),
  };
  start();
})();
