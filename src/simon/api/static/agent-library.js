'use strict';
(() => {
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const $ = (id) => document.getElementById(id);
  let catalog = null,
    parent = null,
    record = null,
    skills = [],
    selected = new Set(),
    onSaved = null;
  let busy = false,
    opening = 0,
    submission = null;
  const states = {
    configured: 'Configured',
    disabled: 'Disabled',
    unconfigured: 'Needs setup',
    unavailable: 'Unavailable',
    permission_required: 'Permission needed',
    blocked: 'Needs attention',
  };
  const library = node('section', undefined, 'al-library');
  library.id = 'agent-library';
  library.setAttribute('aria-labelledby', 'al-library-heading');
  library.innerHTML = /* HTML */ `<div class="al-heading">
      <div>
        <h3 id="al-library-heading">Your agents</h3>
        <p>Reusable teammates with the skills and responsibilities you choose.</p>
      </div>
      <button id="al-new-agent" type="button" class="al-button primary">New agent</button>
    </div>
    <p id="al-library-status" role="status" aria-live="polite"></p>
    <div id="al-agent-list"></div>
    <details id="al-included">
      <summary>Included agents</summary>
      <div id="al-included-list"></div>
    </details>`;
  const local = (id) => library.querySelector('#' + id);
  const dialog = node('dialog', undefined, 'al-dialog');
  dialog.id = 'agent-editor';
  dialog.setAttribute('aria-labelledby', 'al-editor-heading');
  dialog.innerHTML = /* HTML */ `<div class="al-heading">
      <div>
        <h2 id="al-editor-heading">New agent</h2>
        <p>Choose skills and describe the role. One agent can own the whole outcome.</p>
      </div>
      <button
        id="al-close"
        type="button"
        class="al-button al-close"
        aria-label="Close agent editor"
      >
        ×
      </button>
    </div>
    <form id="al-form">
      <fieldset id="al-fields">
        <label for="al-name">Role title</label
        ><input
          id="al-name"
          required
          maxlength="160"
          placeholder="e.g. Research &amp; documentation lead"
        /><label for="al-description">Role description</label
        ><textarea
          id="al-description"
          required
          maxlength="4000"
          rows="4"
          placeholder="Describe what this agent should own, how it should work, and what a good result looks like."
        ></textarea>
        <div class="al-skill-heading">
          <div>
            <h3 id="al-skills-heading">Skills</h3>
            <p>Combine up to 16 skills. Setup requirements are shown for each one.</p>
          </div>
          <span id="al-selected-count" role="status" aria-live="polite">0 selected</span>
        </div>
        <label for="al-skill-search" class="sr-only">Find a skill</label
        ><input id="al-skill-search" type="search" placeholder="Find a skill" />
        <div id="al-skills" role="group" aria-labelledby="al-skills-heading"></div>
      </fieldset>
      <p id="al-feedback" class="al-feedback" role="status" aria-live="polite"></p>
      <div class="al-footer">
        <p id="al-save-note">
          Saved agents are reusable across your projects. Saving does not start work.
        </p>
        <button id="al-save" type="submit" class="al-button primary">Save agent</button>
      </div>
    </form>`;
  document.body.append(dialog);
  const message = (text, error = false) => {
    $('al-feedback').textContent = text;
    $('al-feedback').classList.toggle('error', error);
  };
  const readableReason = (text) => {
    let result = String(text);
    for (const tool of catalog?.tool_statuses || [])
      result = result.replaceAll(tool.id, tool.description || 'An integration');
    return result;
  };
  function action(label, callback) {
    const result = node('button', label, 'al-button');
    result.type = 'button';
    result.onclick = callback;
    return result;
  }
  function badge(state) {
    return node(
      'span',
      states[state] || 'Needs setup',
      'al-badge ' + (state === 'configured' ? 'configured' : 'blocked'),
    );
  }
  function renderLibrary() {
    if (!catalog) return;
    const fingerprint = JSON.stringify([
      catalog.custom_agents,
      catalog.skills,
      (catalog.agents || []).map((agent) => [agent.id, agent.name, agent.description]),
      catalog.tool_statuses,
    ]);
    if (library.dataset.fingerprint === fingerprint) return;
    library.dataset.fingerprint = fingerprint;
    const rows = local('al-agent-list');
    rows.replaceChildren();
    for (const agent of catalog.custom_agents || []) {
      const row = node('article', undefined, 'al-agent');
      row.dataset.agentId = agent.id;
      const granted = (agent.skill_ids || []).map(
        (id) =>
          catalog.skills?.find((skill) => skill.id === id) ||
          agent.skills?.find((skill) => skill.id === id),
      );
      const reasons = [
        ...new Set([
          ...(agent.blocked_reasons || []),
          ...granted.flatMap((skill) => skill?.blocked_reasons || []),
        ]),
      ];
      const state =
        agent.state === 'configured' &&
        granted.every((skill) => skill?.state === 'configured') &&
        !reasons.length
          ? 'configured'
          : 'blocked';
      const head = node('div', undefined, 'al-agent-heading');
      head.append(node('h4', agent.name), badge(state));
      row.append(head, node('p', agent.description));
      const names = (agent.skill_ids || []).map(
        (id) =>
          catalog.skills?.find((skill) => skill.id === id)?.name ||
          agent.skills?.find((skill) => skill.id === id)?.name ||
          'Skill no longer available',
      );
      row.append(node('small', names.join(' · ')));
      if (reasons.length)
        row.append(node('p', reasons.map(readableReason).join(' '), 'al-blocker'));
      if (agent.editable !== false) {
        const edit = action('Edit agent', () => open({ agent }));
        edit.setAttribute('aria-label', 'Edit agent: ' + agent.name);
        row.append(edit);
      }
      rows.append(row);
    }
    if (!rows.childElementCount) {
      const empty = node('div', undefined, 'al-empty');
      empty.append(
        node('h4', 'Create your first agent'),
        node(
          'p',
          'Give an agent a role and combine the skills it needs. You can use it on any of your project teams.',
        ),
      );
      rows.append(empty);
    }
    const customIds = new Set((catalog.custom_agents || []).map((agent) => agent.id));
    const included = local('al-included-list');
    included.replaceChildren();
    for (const agent of catalog.agents || [])
      if (!customIds.has(agent.id)) {
        const row = node('article', undefined, 'al-agent');
        row.append(
          node('h4', agent.name || 'Included agent'),
          node('p', agent.description || 'An agent configured by your server operator.'),
        );
        included.append(row);
      }
    local('al-included').hidden = !included.childElementCount;
  }
  function mount(target, updated) {
    parent = target;
    if (updated) catalog = updated;
    if (library.parentElement !== target) target.replaceChildren(library);
    renderLibrary();
  }
  async function refresh() {
    const updated = await api('/v1/agent-platform/catalog');
    catalog = updated;
    if (parent?.isConnected) renderLibrary();
    return updated;
  }
  function renderSkills() {
    const target = $('al-skills');
    const query = $('al-skill-search').value.trim().toLocaleLowerCase();
    target.replaceChildren();
    for (const skill of skills.filter((skill) =>
      (skill.name + ' ' + skill.description).toLocaleLowerCase().includes(query),
    )) {
      const row = node('label', undefined, 'al-skill');
      const input = node('input');
      input.type = 'checkbox';
      input.value = skill.id;
      input.checked = selected.has(skill.id);
      input.setAttribute('aria-label', skill.name);
      const copy = node('span');
      const heading = node('span', undefined, 'al-skill-name');
      heading.append(node('strong', skill.name), badge(skill.state));
      copy.append(heading, node('span', skill.description, 'al-skill-description'));
      if (skill.blocked_reasons?.length)
        copy.append(
          node('small', skill.blocked_reasons.map(readableReason).join(' '), 'al-blocker'),
        );
      input.onchange = () => {
        if (input.checked) selected.add(skill.id);
        else selected.delete(skill.id);
        updateSelection();
      };
      row.append(input, copy);
      target.append(row);
    }
    if (!target.childElementCount)
      target.append(
        node(
          'p',
          query
            ? 'No skills match this search.'
            : 'No skills are available for this account. Ask your server operator to configure agent capabilities.',
          'al-empty',
        ),
      );
    updateSelection();
  }
  function updateSelection() {
    $('al-selected-count').textContent = selected.size + ' selected';
    $('al-save').disabled = busy || !selected.size || selected.size > 16;
    const unavailable = skills.filter(
      (skill) => selected.has(skill.id) && skill.state !== 'configured',
    );
    const note = record
      ? 'Changes apply to future plans. Recreate existing plans that use this agent before running them.'
      : 'Saved agents are reusable across your projects. Saving does not start work.';
    $('al-save-note').textContent =
      note +
      (unavailable.length
        ? ' Skills that need setup remain blocked until their requirements are met.'
        : '');
    if (selected.size > 16) message('Choose at most 16 skills for one agent.', true);
    else if ($('al-feedback').textContent === 'Choose at most 16 skills for one agent.')
      message('');
  }
  async function open(options = {}) {
    if (busy) return;
    const request = ++opening;
    record = options.agent || null;
    onSaved = options.onSaved || null;
    submission = null;
    selected = new Set(record?.skill_ids || []);
    skills = [];
    $('al-form').reset();
    $('al-skills').replaceChildren(node('p', 'Loading available skills…', 'al-empty'));
    $('al-selected-count').textContent = selected.size + ' selected';
    $('al-name').value = record?.name || '';
    $('al-description').value = record?.description || '';
    $('al-editor-heading').textContent = record ? 'Edit agent' : 'New agent';
    $('al-save').textContent = record ? 'Save changes' : 'Save agent';
    $('al-fields').disabled = true;
    $('al-save').disabled = true;
    message('Loading available skills…');
    if (!dialog.open) dialog.showModal();
    try {
      const updated = await refresh();
      if (request !== opening || !dialog.open) return;
      // Preserve unavailable saved skills so a person can inspect and remove them.
      const current = record
        ? updated.custom_agents?.find((agent) => agent.id === record.id)
        : null;
      if (current) {
        record = current;
        $('al-name').value = current.name;
        $('al-description').value = current.description;
        selected = new Set(current.skill_ids);
      }
      skills = [...(updated.skills || [])];
      for (const skill of record?.skills || [])
        if (!skills.some((item) => item.id === skill.id))
          skills.push({
            ...skill,
            state: 'unavailable',
            blocked_reasons: skill.blocked_reasons?.length
              ? skill.blocked_reasons
              : ['This skill is no longer available. Remove it before saving changes.'],
          });
      for (const id of selected)
        if (!skills.some((skill) => skill.id === id))
          skills.push({
            id,
            name: 'Previously selected skill',
            description: 'This skill is no longer offered to your account.',
            state: 'unavailable',
            blocked_reasons: ['Remove this skill or ask your operator to restore access.'],
          });
      renderSkills();
      $('al-fields').disabled = false;
      message('');
      $('al-name').focus();
    } catch (error) {
      if (request === opening && dialog.open) message(error.message, true);
    }
  }
  $('al-form').onsubmit = async (event) => {
    event.preventDefault();
    if (busy || selected.size < 1 || selected.size > 16) return;
    const payload = {
      name: $('al-name').value.trim(),
      description: $('al-description').value.trim(),
      skill_ids: [...selected].sort(),
    };
    if (!payload.name || !payload.description) {
      message('Enter a role title and describe what the agent should own.', true);
      return;
    }
    const fingerprint = JSON.stringify([record?.id, record?.version, payload]);
    if (submission?.fingerprint !== fingerprint)
      submission = { fingerprint, key: crypto.randomUUID() };
    busy = true;
    $('al-fields').disabled = true;
    $('al-save').disabled = true;
    $('al-close').disabled = true;
    message('Saving agent…');
    try {
      const saved = await api(
        '/v1/agent-platform/agents' + (record ? '/' + encodeURIComponent(record.id) : ''),
        {
          ...payload,
          ...(record ? { expected_version: record.version } : {}),
          idempotency_key: submission.key,
        },
        record ? 'PATCH' : 'POST',
      );
      const updated = await refresh();
      const callback = onSaved;
      dialog.close();
      submission = null;
      if (callback) await callback(saved, updated);
      window.dispatchEvent(
        new CustomEvent('simon-agent-library-updated', {
          detail: { agent: saved, catalog: updated },
        }),
      );
      local('al-library-status').textContent =
        saved.name + ' saved. It is available to your project teams.';
    } catch (error) {
      message(
        error.message +
          (record
            ? ' Your draft is preserved. Close and reopen the editor to load the latest saved version if another change was made.'
            : ''),
        true,
      );
    } finally {
      busy = false;
      $('al-fields').disabled = false;
      $('al-close').disabled = false;
      updateSelection();
    }
  };
  local('al-new-agent').onclick = () => open();
  $('al-skill-search').oninput = renderSkills;
  $('al-close').onclick = () => {
    if (!busy) dialog.close();
  };
  dialog.addEventListener('cancel', (event) => {
    if (busy) event.preventDefault();
  });
  dialog.addEventListener('close', () => {
    ++opening;
    if (!onSaved && library.isConnected)
      (record
        ? local('al-agent-list').querySelector('[data-agent-id="' + record.id + '"] button')
        : local('al-new-agent')
      )?.focus();
    onSaved = null;
  });
  window.SimonAgentLibrary = {
    mount,
    refresh,
    create: (options) => open(options),
    edit: (agent) => open({ agent }),
  };
})();
