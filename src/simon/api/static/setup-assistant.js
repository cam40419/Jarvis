'use strict';
(() => {
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const dialog = node('dialog', undefined, 'sa-dialog');
  dialog.id = 'setup-assistant';
  dialog.setAttribute('aria-labelledby', 'sa-heading');
  dialog.innerHTML = /* HTML */ `<header class="sa-header">
      <div>
        <p class="sa-eyebrow">SETUP ASSISTANT</p>
        <h2 id="sa-heading">Describe the team you need</h2>
      </div>
      <button id="sa-close" type="button" class="sa-button" aria-label="Close setup assistant">
        Close
      </button>
    </header>
    <p id="sa-purpose" class="sa-purpose"></p>
    <div
      id="sa-transcript"
      class="sa-transcript"
      role="log"
      aria-label="Setup conversation"
      aria-live="polite"
      tabindex="0"
    ></div>
    <div class="sa-footer">
      <p id="sa-error" class="sa-error" role="alert" hidden></p>
      <p id="sa-status" class="sa-status" role="status"></p>
      <form id="sa-form">
        <div class="sa-model-choice">
          <label for="sa-privacy">Use models</label
          ><select id="sa-privacy">
            <option value="allow_cloud">Configured models (local or cloud)</option>
            <option value="local_only">Local models only</option>
          </select>
        </div>
        <label for="sa-message">Message to setup assistant</label>
        <textarea
          id="sa-message"
          rows="3"
          maxlength="4000"
          placeholder="Describe the work, the outcomes, and any tools or limits that matter."
        ></textarea>
        <div class="sa-send-row">
          <button id="sa-reset" type="button" class="sa-button">New conversation</button
          ><button id="sa-send" type="submit" class="sa-button primary">Send message</button>
        </div>
      </form>
      <div id="sa-apply-row" class="sa-apply-row" hidden>
        <p id="sa-apply-note"></p>
        <button id="sa-apply" type="button" class="sa-button primary">Use recommendation</button>
      </div>
    </div>`;
  document.body.append(dialog);
  const $ = (id) => dialog.querySelector('#' + id);
  let options = null,
    origin = null,
    generation = 0,
    catalog = null,
    messages = [],
    currentDraft = null,
    proposal = null,
    proposalWarnings = [],
    pending = false,
    applying = false,
    catalogLoading = false,
    proposalValid = false;
  const skillById = (id) =>
    catalog?.individual_skills?.find((item) => item.id === id) ||
    catalog?.skills?.find((item) => item.id === id);
  function readableReason(reason) {
    let text = String(reason);
    for (const skill of catalog?.individual_skills || [])
      for (const id of skill.tool_ids || []) text = text.replaceAll(id, skill.name);
    return text;
  }
  const failure = (message = '') => {
    $('sa-error').textContent = message;
    $('sa-error').hidden = !message;
  };
  function updateControls() {
    $('sa-send').disabled = pending || applying || catalogLoading || !catalog;
    $('sa-send').textContent = pending ? 'Thinking…' : 'Send message';
    $('sa-reset').disabled = pending || applying;
    $('sa-privacy').disabled = pending || applying;
    $('sa-apply').disabled = pending || applying || !proposalValid;
    $('sa-apply-row').hidden = !proposal;
    $('sa-form').setAttribute('aria-busy', String(pending));
  }
  function close(restoreFocus = true) {
    ++generation;
    pending = false;
    applying = false;
    dialog.close();
    if (restoreFocus && origin?.isConnected) origin.focus({ preventScroll: true });
  }
  function introduction() {
    const intro = node('div', undefined, 'sa-introduction');
    intro.append(
      node('h3', 'Start with the work you want done'),
      node(
        'p',
        'Describe the outcome in your own words. Ask follow-up questions or refine the recommendation. One agent can cover several responsibilities.',
      ),
      node(
        'p',
        'Only skills available to your account can be recommended. Connections and permissions still apply.',
      ),
    );
    $('sa-transcript').replaceChildren(intro);
  }
  function message(role, content) {
    const item = node('article', undefined, 'sa-message sa-' + role);
    item.append(node('h3', role === 'user' ? 'You' : 'Simon'), node('p', content));
    $('sa-transcript').append(item);
    return item;
  }
  function renderProposal(warnings = proposalWarnings) {
    $('sa-transcript').querySelector('.sa-proposal')?.remove();
    $('sa-transcript').querySelector(':scope > .sa-warnings')?.remove();
    proposalValid = false;
    if (!proposal) {
      if (warnings.length) {
        const notes = node('ul', undefined, 'sa-warnings');
        warnings.forEach((text) => notes.append(node('li', text)));
        $('sa-transcript').append(notes);
      }
      return;
    }
    const review = node('section', undefined, 'sa-proposal');
    review.setAttribute('aria-label', 'Recommendation to review');
    review.append(
      node(
        'h3',
        options.mode === 'team' ? proposal.team_name || 'Recommended team' : 'Recommended role',
      ),
    );
    const roles = Array.isArray(proposal.roles) ? proposal.roles : [];
    proposalValid =
      roles.length > 0 &&
      roles.length <= 8 &&
      (options.mode === 'team'
        ? Boolean(proposal.team_name?.trim()) && roles.filter((role) => role.is_lead).length === 1
        : roles.length === 1);
    for (const role of roles) {
      const card = node('article', undefined, 'sa-role');
      const title = node('div', undefined, 'sa-role-title');
      title.append(node('h4', role.name || 'Unnamed role'));
      if (role.is_lead && options.mode === 'team')
        title.append(node('span', 'Project lead', 'sa-lead'));
      card.append(title, node('p', role.description || ''));
      if (role.rationale) card.append(node('p', role.rationale, 'sa-rationale'));
      const skills = node('ul', undefined, 'sa-skills');
      const ids = Array.isArray(role.skill_ids) ? role.skill_ids : [];
      if (!role.name?.trim() || !role.description?.trim() || !ids.length || ids.length > 128)
        proposalValid = false;
      for (const id of ids) {
        const skill = skillById(id);
        const item = node('li');
        item.append(node('span', skill?.name || 'Unavailable skill'));
        if (!skill) proposalValid = false;
        if (skill?.state !== 'configured') {
          const reason =
            skill?.blocked_reasons?.map(readableReason).join(' ') ||
            'Set up this capability before using it.';
          item.append(node('small', reason, 'sa-skill-blocker'));
        }
        skills.append(item);
      }
      card.append(skills);
      review.append(card);
    }
    if (warnings.length) {
      const notes = node('ul', undefined, 'sa-warnings');
      warnings.forEach((text) => notes.append(node('li', text)));
      review.append(notes);
    }
    if (!proposalValid)
      review.append(
        node(
          'p',
          'This recommendation is incomplete or includes skills that are no longer available. Ask Simon to revise it before applying.',
          'sa-error',
        ),
      );
    $('sa-transcript').append(review);
  }
  async function loadCatalog(opening) {
    catalogLoading = true;
    $('sa-status').textContent = 'Checking available skills…';
    updateControls();
    try {
      const result = await api('/v1/agent-platform/catalog');
      if (generation !== opening || !dialog.open) return;
      catalog = result;
      $('sa-status').textContent =
        'Recommendations are drafts. Nothing is saved from this conversation.';
    } catch (error) {
      if (generation === opening && dialog.open) {
        failure(error.message + ' Close and reopen the assistant to check skills again.');
        $('sa-status').textContent = '';
      }
    } finally {
      if (generation === opening) {
        catalogLoading = false;
        updateControls();
      }
    }
  }
  function open(configuration) {
    if (
      !['team', 'member', 'agent'].includes(configuration.mode) ||
      typeof configuration.onApply !== 'function'
    )
      throw Error('Choose an editor before opening the setup assistant.');
    if (dialog.open) close(false);
    const opening = ++generation;
    options = configuration;
    origin = document.activeElement;
    messages = [];
    currentDraft = configuration.currentDraft ? structuredClone(configuration.currentDraft) : null;
    proposal = null;
    proposalWarnings = [];
    proposalValid = false;
    catalog = null;
    pending = false;
    applying = false;
    failure();
    $('sa-heading').textContent =
      configuration.mode === 'team' ? 'Describe the team you need' : 'Describe the agent you need';
    $('sa-purpose').textContent =
      configuration.mode === 'team'
        ? 'Plan the roles, choose a lead, and match each member with individual skills.'
        : 'Turn a role description into a title, responsibilities, and individual skills.';
    $('sa-apply-note').textContent =
      configuration.mode === 'team'
        ? 'Replaces the team in this editor. Review the editable draft, then save the project to apply it.'
        : 'Fills this role’s title, description, and skills. Review the editable draft, then save it.';
    $('sa-message').value = String(configuration.initialPrompt || '').slice(0, 4000);
    introduction();
    dialog.showModal();
    $('sa-message').focus();
    loadCatalog(opening);
  }
  $('sa-form').onsubmit = async (event) => {
    event.preventDefault();
    if (!dialog.open || pending || applying || !catalog || catalogLoading) return;
    const original = $('sa-message').value;
    const content = original.trim();
    if (!content) {
      failure('Describe what you need or ask a follow-up question.');
      $('sa-message').focus();
      return;
    }
    const requestMessages = [...messages, { role: 'user', content }];
    if (
      requestMessages.length > 16 ||
      requestMessages.reduce((total, item) => total + item.content.length, 0) > 24000
    ) {
      failure(
        'This conversation has reached its limit. Start a new conversation to continue; your editor draft will stay unchanged.',
      );
      return;
    }
    const opening = generation;
    pending = true;
    failure();
    $('sa-status').textContent = 'Preparing a recommendation…';
    $('sa-transcript').querySelector('.sa-introduction')?.remove();
    const sent = message('user', content);
    sent.classList.add('sa-pending');
    $('sa-transcript').scrollTop = $('sa-transcript').scrollHeight;
    updateControls();
    try {
      const reply = await api('/v1/agent-platform/setup-assistant', {
        mode: options.mode,
        privacy: $('sa-privacy').value,
        project_id: options.projectId || null,
        messages: requestMessages,
        current_draft: currentDraft,
      });
      if (generation !== opening || !dialog.open) return;
      if (typeof reply.message !== 'string')
        throw Error('The assistant returned an unreadable reply. Please try again.');
      messages = [...requestMessages, { role: 'assistant', content: reply.message }];
      sent.classList.remove('sa-pending');
      const response = message('assistant', reply.message);
      proposal = reply.proposal || null;
      proposalWarnings = Array.isArray(reply.warnings) ? reply.warnings : [];
      if (proposal) currentDraft = structuredClone(proposal);
      renderProposal();
      if ($('sa-message').value === original) $('sa-message').value = '';
      $('sa-status').textContent = proposal
        ? 'Review the recommendation or ask Simon to refine it.'
        : 'Reply to continue shaping the recommendation.';
      $('sa-transcript').scrollTop = Math.max(0, response.offsetTop - 12);
    } catch (error) {
      if (generation !== opening || !dialog.open) return;
      sent.remove();
      failure(error.message + ' Your message and editor draft have been kept.');
      $('sa-status').textContent = proposal
        ? 'The earlier recommendation is still available. Your latest message was not completed.'
        : '';
    } finally {
      if (generation === opening) {
        pending = false;
        updateControls();
      }
    }
  };
  $('sa-apply').onclick = async () => {
    if (pending || applying || !proposal || !proposalValid) return;
    const callback = options.onApply,
      reviewed = structuredClone(proposal);
    close();
    const applyingGeneration = generation;
    applying = true;
    try {
      const applied = await callback(reviewed);
      if (applied === false)
        throw Error(
          'The editor or available skills changed. Close the assistant and reopen it from your current draft before applying a recommendation.',
        );
    } catch (error) {
      if (generation !== applyingGeneration) return;
      dialog.showModal();
      failure(error.message);
    } finally {
      if (generation === applyingGeneration) {
        applying = false;
        updateControls();
      }
    }
  };
  $('sa-close').onclick = () => close();
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    close();
  });
  $('sa-reset').onclick = () => {
    if (pending || applying) return;
    messages = [];
    introduction();
    renderProposal();
    failure();
    $('sa-status').textContent = 'Conversation restarted. Your editor draft is unchanged.';
    $('sa-message').focus();
  };
  $('sa-message').addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      $('sa-form').requestSubmit();
    }
  });
  window.SimonSetupAssistant = { open };
})();
