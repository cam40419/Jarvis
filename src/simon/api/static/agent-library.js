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
    assistance = 0,
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
        <p>Reusable teammates with clear roles and working instructions.</p>
      </div>
      <div class="al-heading-actions">
        <button id="al-describe-agent" type="button" class="al-button">Describe an agent</button>
        <button id="al-new-agent" type="button" class="al-button primary">New agent</button>
      </div>
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
        <p>
          Define the role and concrete working instructions. Every agent can use all connected
          workspace tools.
        </p>
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
      <div class="al-fields-scroll">
        <fieldset id="al-fields">
          <div class="al-assistant-entry">
            <p>Describe what you need to get a role recommendation. Review it before saving.</p>
            <button id="al-assist" type="button" class="al-button">Describe this agent</button>
          </div>
          <label for="al-name">Role title</label
          ><input
            id="al-name"
            required
            maxlength="160"
            placeholder="e.g. Research &amp; documentation lead"
          /><label for="al-description">Working instructions</label
          ><textarea
            id="al-description"
            required
            maxlength="4000"
            rows="4"
            placeholder="Describe what this agent should own, how it should work, and what a good result looks like."
          ></textarea>
          <div class="al-skill-heading">
            <div>
              <h3 id="al-skills-heading">Individual skills</h3>
              <p>
                Choose up to 128 capabilities. Connected services need an authorized account before
                use.
              </p>
            </div>
            <span id="al-selected-count" role="status" aria-live="polite">0 selected</span>
          </div>
          <div class="al-skill-filters">
            <div>
              <label for="al-skill-search">Find a skill</label
              ><input id="al-skill-search" type="search" placeholder="Search skills or services" />
            </div>
            <div>
              <label for="al-skill-category">Skill category</label
              ><select id="al-skill-category">
                <option value="">All categories</option>
              </select>
            </div>
          </div>
          <div id="al-skills" role="group" aria-labelledby="al-skills-heading"></div>
        </fieldset>
      </div>
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
    const tools = [...(catalog?.tool_statuses || [])].sort((a, b) => b.id.length - a.id.length);
    for (const tool of tools) {
      const skill = skills.find(
        (item) => item.tool_ids?.length === 1 && item.tool_ids[0] === tool.id,
      );
      result = result.replaceAll(tool.id, skill?.name || tool.description || 'An integration');
    }
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
      catalog.individual_skills,
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
      const head = node('div', undefined, 'al-agent-heading');
      head.append(node('h4', agent.name), badge(agent.state || 'configured'));
      row.append(
        head,
        node('p', agent.description),
        node('small', 'All connected workspace tools'),
      );
      if (agent.blocked_reasons?.length)
        row.append(node('p', agent.blocked_reasons.map(readableReason).join(' '), 'al-blocker'));
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
    const category = $('al-skill-category').value;
    target.replaceChildren();
    const groups = new Map();
    for (const skill of skills.filter(
      (skill) =>
        (!category || (skill.category || 'General') === category) &&
        (skill.name + ' ' + skill.description + ' ' + (skill.category || 'General'))
          .toLocaleLowerCase()
          .includes(query),
    )) {
      const name = skill.category || 'General';
      if (!groups.has(name)) {
        const group = node('fieldset', undefined, 'al-skill-group');
        group.append(node('legend', name));
        const grid = node('div', undefined, 'al-skill-grid');
        group.append(grid);
        target.append(group);
        groups.set(name, grid);
      }
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
      groups.get(name).append(row);
    }
    if (!target.childElementCount)
      target.append(
        node(
          'p',
          query || category
            ? 'No skills match this search.'
            : 'No skills are available for this account. Ask your server operator to configure agent capabilities.',
          'al-empty',
        ),
      );
    updateSelection();
  }
  function updateSelection() {
    $('al-selected-count').textContent = 'All workspace tools';
    $('al-save').disabled = busy;
    $('al-save-note').textContent =
      'Roles define responsibilities. Connected accounts and workspace permissions apply to every agent. Changes apply to future work.';
    for (const selector of ['.al-skill-heading', '.al-skill-filters', '#al-skills'])
      dialog.querySelector(selector).hidden = true;
  }
  function draftFingerprint() {
    return JSON.stringify([
      record?.id,
      record?.version,
      $('al-name').value,
      $('al-description').value,
      [...selected].sort(),
    ]);
  }
  function assistantSkills() {
    const individual = catalog?.individual_skills || [];
    const result = new Set();
    for (const id of selected) {
      if (individual.some((skill) => skill.id === id)) {
        result.add(id);
        continue;
      }
      // Translate saved bundles for the recommendation only. The form keeps its
      // saved selections until a person explicitly applies and saves a draft.
      const previous = skills.find((skill) => skill.id === id);
      for (const toolId of previous?.tool_ids || []) {
        const skill = individual.find((item) => item.tool_ids?.includes(toolId));
        if (skill) result.add(skill.id);
      }
    }
    return [...result].sort();
  }
  function assist() {
    if (busy || !dialog.open || $('al-fields').disabled) return;
    if (!window.SimonSetupAssistant?.open) {
      message('The setup assistant is unavailable. You can continue editing this form.', true);
      return;
    }
    const request = opening;
    const recommendation = ++assistance;
    const fingerprint = draftFingerprint();
    const draft = {
      name: $('al-name').value.trim(),
      description: $('al-description').value.trim(),
      skill_ids: assistantSkills(),
      is_lead: false,
      rationale: '',
    };
    const complete = draft.name && draft.description;
    try {
      window.SimonSetupAssistant.open({
        mode: 'agent',
        projectId: null,
        initialPrompt: complete
          ? ''
          : [draft.name, draft.description].filter(Boolean).join('\n\n').slice(0, 4000),
        currentDraft: {
          team_name: '',
          roles: complete ? [draft] : [],
        },
        onApply(proposal) {
          if (request !== opening || recommendation !== assistance || !dialog.open || busy)
            return false;
          if (fingerprint !== draftFingerprint()) {
            message(
              'Your form changed while the assistant was open. Your edits are preserved; reopen the assistant to use the latest draft.',
              true,
            );
            return false;
          }
          const role = proposal?.roles?.length === 1 ? proposal.roles[0] : null;
          const available = new Set((catalog?.individual_skills || []).map((skill) => skill.id));
          if (
            !role ||
            typeof role.name !== 'string' ||
            !role.name.trim() ||
            role.name.length > 160 ||
            typeof role.description !== 'string' ||
            !role.description.trim() ||
            role.description.length > 4000 ||
            !Array.isArray(role.skill_ids) ||
            role.skill_ids.length > 128 ||
            role.skill_ids.some((id) => !available.has(id))
          ) {
            message(
              'This recommendation cannot be applied to the available skills. Your draft is preserved; ask the assistant to revise it.',
              true,
            );
            return false;
          }
          ++assistance;
          $('al-name').value = role.name.trim();
          $('al-description').value = role.description.trim();
          selected = new Set(role.skill_ids);
          $('al-skill-search').value = '';
          $('al-skill-category').value = '';
          submission = null;
          renderSkills();
          message(
            'Recommendation applied. Review the role and working instructions, then save when ready.',
          );
          requestAnimationFrame(() => {
            if (request === opening && dialog.open) $('al-name').focus();
          });
          return true;
        },
      });
    } catch (error) {
      if (request === opening && dialog.open)
        message(
          error.message + ' Your draft is preserved. You can continue editing the form.',
          true,
        );
    }
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
    $('al-assist').textContent = record ? 'Describe changes' : 'Describe this agent';
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
      skills = [...(updated.individual_skills || [])];
      // Existing bundles keep their exact saved grants; only individual skills
      // are offered for new selections.
      for (const id of selected) {
        const legacy = updated.skills?.find((skill) => skill.id === id);
        if (legacy && !skills.some((skill) => skill.id === id))
          skills.push({ ...legacy, category: 'Saved skill groups' });
      }
      for (const skill of record?.skills || [])
        if (!skills.some((item) => item.id === skill.id))
          skills.push({
            ...skill,
            category: 'Saved skills',
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
            category: 'Saved skills',
            state: 'unavailable',
            blocked_reasons: ['Remove this skill or ask your operator to restore access.'],
          });
      $('al-skill-category').replaceChildren(
        new Option('All categories', ''),
        ...[...new Set(skills.map((skill) => skill.category || 'General'))]
          .sort((a, b) => a.localeCompare(b))
          .map((category) => new Option(category, category)),
      );
      renderSkills();
      $('al-fields').disabled = false;
      message('');
      $('al-name').focus();
      if (options.assisted) assist();
    } catch (error) {
      if (request === opening && dialog.open) message(error.message, true);
    }
  }
  $('al-form').onsubmit = async (event) => {
    event.preventDefault();
    if (busy) return;
    const payload = {
      name: $('al-name').value.trim(),
      description: $('al-description').value.trim(),
      skill_ids: [],
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
  local('al-describe-agent').onclick = () => open({ assisted: true });
  $('al-assist').onclick = assist;
  $('al-skill-search').oninput = renderSkills;
  $('al-skill-category').onchange = renderSkills;
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
