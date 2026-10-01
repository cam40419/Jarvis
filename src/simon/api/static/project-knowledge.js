'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const node = (tag, text, className) => { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; };
  const canWrite = () => typeof session !== 'undefined' && session?.scopes?.includes('jobs:write');
  const date = value => value ? new Date(value).toLocaleString([], {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}) : '';
  let projectId = null, epoch = 0, record = null, recordLoading = false, recordError = '';
  let page = null, historyLoading = false, historyError = '', historyRequest = 0, activityCount = null;
  let appliedQuery = '', appliedKind = '';
  let edit = null, busy = false;
  const panel = node('section', undefined, 'pk-panel'); panel.id = 'project-knowledge';
  panel.innerHTML = '<div id="pk-record"></div><section class="pk-history"><div class="pc-section-heading"><div><h3>Project history</h3><p>Search saved findings, decisions, and progress across the whole project.</p></div><button id="pk-refresh" type="button" class="pc-button">Refresh record</button></div><form id="pk-search-form" class="pk-search-form"><div><label for="pk-query">Search project history</label><input id="pk-query" type="search" maxlength="200" placeholder="Find a decision, result, or detail"></div><div><label for="pk-kind">Entry type</label><select id="pk-kind"><option value="">All entries</option><option value="finding">Findings</option><option value="decision">Decisions</option><option value="note">Notes</option><option value="blocked">Blockers</option><option value="progress">Progress</option><option value="task">Tasks</option><option value="cycle">Cycles</option><option value="configuration">Configuration</option></select></div><button class="pc-button" type="submit">Search history</button></form><p id="pk-history-status" role="status" aria-live="polite"></p><div id="pk-history-list"></div><button id="pk-more" type="button" class="pc-button" hidden>Load earlier entries</button></section>';
  const local = id => panel.querySelector('#' + id);
  const editor = node('dialog', undefined, 'pc-dialog pk-editor'); editor.id = 'pk-editor'; editor.setAttribute('aria-labelledby', 'pk-edit-heading');
  editor.innerHTML = '<div class="pc-dialog-header"><div><h2 id="pk-edit-heading">Project brief</h2><p id="pk-edit-help">Keep the goal, constraints, and current context available to your team.</p></div><button id="pk-edit-close" type="button" class="pc-button pc-dialog-close" aria-label="Close project record editor">×</button></div><form id="pk-edit-form"><fieldset id="pk-edit-fields"><div id="pk-title-wrap"><label for="pk-title">Decision title</label><input id="pk-title" maxlength="160"></div><label id="pk-text-label" for="pk-text">Project brief</label><textarea id="pk-text" rows="9" maxlength="8000"></textarea><p id="pk-source-note" class="pc-help"></p></fieldset><p id="pk-edit-error" class="pc-error" role="alert" hidden></p><div class="pc-dialog-footer"><p>Your draft stays open if another update needs review.</p><button id="pk-edit-save" type="submit" class="pc-button primary">Save brief</button></div></form>';
  document.body.append(editor);
  const sourceDialog = node('dialog', undefined, 'pc-dialog'); sourceDialog.id = 'pk-source-dialog'; sourceDialog.setAttribute('aria-labelledby', 'pk-source-heading');
  sourceDialog.innerHTML = '<div class="pc-dialog-header"><h2 id="pk-source-heading">Original project entry</h2><button type="button" class="pc-button" aria-label="Close source entry">Close</button></div><div id="pk-source-content"></div>';
  sourceDialog.querySelector('button').onclick = () => sourceDialog.close(); document.body.append(sourceDialog); let sourceRequest = 0; sourceDialog.addEventListener('close', () => { ++sourceRequest; });
  function action(label, callback, className = '') { const item = node('button', label, 'pc-button ' + className); item.type = 'button'; item.onclick = async () => { const current = epoch; item.disabled = true; try { await callback(); } catch (error) { if (current === epoch) { recordError = error.message; renderRecord(); } } finally { if (item.isConnected) item.disabled = false; } }; return item; }
  function path(suffix = '') { return '/v1/projects/' + encodeURIComponent(projectId) + '/knowledge' + suffix; }
  function select(id) {
    if (id === projectId) return;
    projectId = id; ++epoch; ++historyRequest; record = null; page = null; recordLoading = false; historyLoading = false; recordError = ''; historyError = ''; activityCount = null; appliedQuery = ''; appliedKind = '';
    local('pk-query').value = ''; local('pk-kind').value = ''; renderRecord(); renderHistory(); renderSummary();
  }
  async function loadRecord() {
    if (!projectId || recordLoading) return;
    const current = epoch; recordLoading = true;
    try { const value = await api(path()); if (current !== epoch) return; record = value; recordError = ''; }
    catch (error) { if (current === epoch) recordError = error.message; }
    finally { if (current === epoch) { recordLoading = false; renderRecord(); renderSummary(); renderHistory(); } }
  }
  async function loadHistory(cursor = null, useDraft = true) {
    if (!projectId) return;
    const current = epoch, request = ++historyRequest, previous = page;
    if (!cursor && useDraft) { appliedQuery = local('pk-query').value.trim(); appliedKind = local('pk-kind').value; }
    const params = new URLSearchParams({limit: '20', query: appliedQuery}); if (appliedKind) params.set('kind', appliedKind); if (cursor) params.set('cursor', cursor);
    historyLoading = true; historyError = ''; if (!cursor) page = null; renderHistory();
    try { const result = await api(path('/history?' + params)); if (current !== epoch || request !== historyRequest) return; page = cursor ? {...result, items: [...(previous?.items || []), ...result.items]} : result; }
    catch (error) { if (current === epoch && request === historyRequest) historyError = error.message; }
    finally { if (current === epoch && request === historyRequest) { historyLoading = false; renderHistory(); } }
  }
  function renderSummary() {
    const target = $('pc-knowledge-summary'); if (!target) return;
    const key = JSON.stringify([projectId, record, recordError, canWrite()]); if (target.dataset.fingerprint === key) return; target.dataset.fingerprint = key;
    target.replaceChildren();
    const head = node('div', undefined, 'pc-section-heading'); const copy = node('div'); copy.append(node('h3', 'Project brief'));
    const count = record?.pinned_decisions?.length || 0; copy.append(node('p', count ? count + ' pinned decision' + (count === 1 ? '' : 's') + ' in the project record' : 'The goal, constraints, and context your team should keep.'));
    head.append(copy, action('Open knowledge', () => window.SimonProjectCommand.navigate('knowledge'), 'pc-text-button')); target.append(head);
    if (record?.brief) target.append(node('p', record.brief, 'pk-brief-preview'));
    else target.append(node('p', recordError ? 'Project record could not be loaded. Open Knowledge to try again.' : record ? 'Add a brief so important context carries into future work.' : 'Loading the project brief…', 'pc-section-empty'));
    if (record && canWrite()) target.append(action(record.brief ? 'Edit brief' : 'Add brief', () => openEditor(), 'pc-text-button'));
  }
  function renderRecord() {
    const target = local('pk-record'); const key = JSON.stringify([projectId, record, recordError, canWrite()]); if (target.dataset.fingerprint === key) return; target.dataset.fingerprint = key; target.replaceChildren();
    if (recordError) { const notice = node('p', recordError, 'pc-notice error'); notice.setAttribute('role', 'status'); target.append(notice); }
    if (!record) { target.append(node('p', recordError ? 'Refresh the project record to try again.' : 'Loading project knowledge…', 'pc-section-empty')); return; }
    if (!canWrite()) target.append(node('p', 'You can read this project record. Editing requires project write access.', 'pc-help'));
    const brief = node('section', undefined, 'pk-brief'); const heading = node('div', undefined, 'pc-section-heading'); const copy = node('div'); copy.append(node('h3', 'Project brief'), node('p', 'Shared context for the lead and future tasks.')); heading.append(copy); if (canWrite()) heading.append(action(record.brief ? 'Edit brief' : 'Add brief', () => openEditor())); brief.append(heading);
    if (record.brief) { const content = node('div', undefined, 'pc-markdown'); content.append(SimonMarkdown.render(record.brief)); brief.append(content); } else brief.append(node('p', 'Keep the project goal, constraints, and current direction here.', 'pc-section-empty')); target.append(brief);
    const pinned = node('section', undefined, 'pk-pinned'); const header = node('div', undefined, 'pc-section-heading'); const title = node('div'); title.append(node('h3', 'Pinned decisions'), node('p', 'Choices the team should carry forward. The original history stays available.')); header.append(title); if (canWrite()) { const add = action('Pin a decision', () => openEditor({})); add.disabled = record.pinned_decisions.length >= 20; header.append(add); } pinned.append(header);
    for (const decision of record.pinned_decisions || []) {
      const row = node('article', undefined, 'pk-decision'); row.dataset.decision = decision.id; row.append(node('h4', decision.title)); const body = node('div', undefined, 'pc-markdown'); body.append(SimonMarkdown.render(decision.text)); row.append(body);
      const controls = node('div', undefined, 'pc-resource-actions');
      if (decision.source_activity_id) controls.append(action('View source entry', () => openSource(decision.source_activity_id), 'pc-text-button'));
      if (canWrite()) controls.append(action('Edit decision', () => openEditor(decision), 'pc-text-button'), action('Unpin', () => unpin(decision), 'pc-text-button'));
      row.append(controls); pinned.append(row);
    }
    if (!record.pinned_decisions.length) pinned.append(node('p', 'No pinned decisions yet. Pin a finding from the history below or add a decision.', 'pc-section-empty'));
    target.append(pinned);
  }
  function renderHistory() {
    local('pk-history-status').textContent = historyError || (historyLoading ? 'Searching saved project history…' : page ? page.items.length + ' entr' + (page.items.length === 1 ? 'y' : 'ies') + ' loaded' : '');
    local('pk-history-status').classList.toggle('error', Boolean(historyError));
    local('pk-more').hidden = !page?.next_cursor; local('pk-more').disabled = historyLoading || local('pk-query').value.trim() !== appliedQuery || local('pk-kind').value !== appliedKind;
    const target = local('pk-history-list'); const fingerprint = JSON.stringify([projectId, page, record, canWrite()]); if (target.dataset.fingerprint === fingerprint) return; target.dataset.fingerprint = fingerprint; target.replaceChildren();
    for (const entry of page?.items || []) {
      const row = node('article', undefined, 'pk-history-entry'); row.dataset.activityId = entry.id;
      row.append(node('small', entry.kind.replaceAll('_', ' ') + ' · ' + date(entry.created_at)));
      const body = node('div', undefined, 'pc-markdown'); body.append(SimonMarkdown.render(entry.text)); row.append(body);
      const controls = node('div', undefined, 'pc-resource-actions');
      if (canWrite() && record && ['finding', 'decision', 'note'].includes(entry.kind)) { const pinned = record.pinned_decisions.some(item => item.source_activity_id === entry.id); const pin = action(pinned ? 'Pinned' : 'Pin as decision', () => openEditor({}, entry), 'pc-text-button'); pin.disabled = pinned || record.pinned_decisions.length >= 20; controls.append(pin); }
      if (entry.run_id) controls.append(action('Open run history', () => window.SimonProjectCommand.navigate('history'), 'pc-text-button'));
      if (entry.action_id && window.simonExternalActions?.open) controls.append(action('Review external action', () => window.simonExternalActions.open(entry.action_id), 'pc-text-button'));
      if (controls.childElementCount) row.append(controls); target.append(row);
    }
    if (page && !page.items.length && !historyLoading) target.append(node('p', local('pk-query').value || local('pk-kind').value ? 'No entries match this search. Try a different phrase or entry type.' : 'Findings, decisions, and progress will be kept here as the project moves forward.', 'pc-section-empty'));
  }
  function openEditor(decision = null, source = null) {
    if (!record || !canWrite()) return;
    edit = {projectId, epoch, record: structuredClone(record), decision, source, decisionId: decision?.id || crypto.randomUUID(), key: crypto.randomUUID(), fingerprint: null};
    $('pk-edit-heading').textContent = decision ? decision.id ? 'Edit pinned decision' : 'Pin a decision' : 'Edit project brief';
    $('pk-edit-help').textContent = decision ? 'Record the choice and why it matters for future work.' : 'Keep the goal, constraints, and current context available to your team.';
    $('pk-title-wrap').hidden = !decision; $('pk-title').required = Boolean(decision); $('pk-title').value = decision?.title || '';
    $('pk-text-label').textContent = decision ? 'Decision and context' : 'Project brief'; $('pk-text').maxLength = decision ? 2000 : 8000; $('pk-text').required = Boolean(decision);
    $('pk-text').value = decision ? decision.text || source?.text?.slice(0, 2000) || '' : record.brief;
    $('pk-source-note').textContent = source ? 'Source: ' + source.kind + ' · ' + date(source.created_at) + (source.text.length > 2000 ? '. The first 2,000 characters are copied here; review and summarize before saving.' : '. The original entry will remain linked.') : decision?.source_activity_id ? 'The original project history entry remains linked.' : '';
    $('pk-edit-save').textContent = decision ? 'Save decision' : 'Save brief'; $('pk-edit-error').hidden = true; editor.showModal(); (decision ? $('pk-title') : $('pk-text')).focus();
  }
  async function unpin(decision) {
    const current = epoch, id = projectId, snapshot = record;
    const updated = await api('/v1/projects/' + encodeURIComponent(id) + '/knowledge', {expected_version: snapshot.version, idempotency_key: crypto.randomUUID(), brief: snapshot.brief, pinned_decisions: snapshot.pinned_decisions.filter(item => item.id !== decision.id)}, 'PATCH');
    if (current !== epoch) return; record = updated; recordError = ''; renderRecord(); renderSummary(); renderHistory();
  }
  async function openSource(id) {
    const current = epoch, request = ++sourceRequest; $('pk-source-content').replaceChildren(node('p', 'Loading original entry…')); sourceDialog.showModal();
    try {
      const entry = await api(path('/history/' + encodeURIComponent(id))); if (current !== epoch || request !== sourceRequest) return;
      const body = node('div', undefined, 'pc-markdown'); body.append(SimonMarkdown.render(entry.text)); $('pk-source-content').replaceChildren(node('p', entry.kind + ' · ' + date(entry.created_at), 'pc-help'), body);
      if (entry.run_id) $('pk-source-content').append(action('Open run history', () => { sourceDialog.close(); window.SimonProjectCommand.navigate('history'); }));
    } catch (error) { if (current === epoch && request === sourceRequest) $('pk-source-content').replaceChildren(node('p', error.message, 'pc-notice error')); }
  }
  $('pk-edit-form').onsubmit = async event => {
    event.preventDefault(); if (!edit || busy) return; busy = true; $('pk-edit-fields').disabled = true; $('pk-edit-save').disabled = true; $('pk-edit-error').hidden = true;
    const snapshot = edit;
    try {
      const body = {expected_version: snapshot.record.version, brief: snapshot.record.brief, pinned_decisions: structuredClone(snapshot.record.pinned_decisions)};
      if (snapshot.decision) {
        const decision = {id: snapshot.decisionId, title: $('pk-title').value.trim(), text: $('pk-text').value.trim(), source_activity_id: snapshot.decision.source_activity_id || snapshot.source?.id || null};
        if (!decision.title || !decision.text) throw Error('Enter a decision title and its context.');
        body.pinned_decisions = body.pinned_decisions.filter(item => item.id !== decision.id); body.pinned_decisions.push(decision);
      } else body.brief = $('pk-text').value.trim();
      const fingerprint = JSON.stringify(body); if (snapshot.fingerprint && snapshot.fingerprint !== fingerprint) snapshot.key = crypto.randomUUID(); snapshot.fingerprint = fingerprint;
      const updated = await api('/v1/projects/' + encodeURIComponent(snapshot.projectId) + '/knowledge', {...body, idempotency_key: snapshot.key}, 'PATCH');
      editor.close(); if (snapshot.epoch === epoch) { record = updated; recordError = ''; renderRecord(); renderSummary(); renderHistory(); }
    } catch (error) { $('pk-edit-error').textContent = error.message + ' Your draft is preserved. Close and reopen this editor to load the latest saved record.'; $('pk-edit-error').hidden = false; }
    finally { busy = false; $('pk-edit-fields').disabled = false; $('pk-edit-save').disabled = false; }
  };
  $('pk-edit-close').onclick = () => { if (!busy) editor.close(); }; editor.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  local('pk-search-form').onsubmit = event => { event.preventDefault(); loadHistory(); };
  local('pk-query').oninput = () => { local('pk-more').disabled = historyLoading || local('pk-query').value.trim() !== appliedQuery; };
  local('pk-kind').onchange = () => loadHistory(); local('pk-more').onclick = () => loadHistory(page?.next_cursor);
  local('pk-refresh').onclick = () => Promise.all([loadRecord(), loadHistory()]);
  function mount(target, id) { select(id); if (panel.parentElement !== target) target.replaceChildren(panel); if (!record && !recordLoading) loadRecord(); if (!page && !historyLoading) loadHistory(); }
  window.addEventListener('simon-project-changing', event => { projectId = null; select(event.detail.projectId); });
  window.addEventListener('simon-project-command-update', event => {
    select(event.detail.projectId); const count = event.detail.detail.state.activity_count;
    if (activityCount !== null && count !== activityCount && panel.isConnected) loadHistory(null, false); activityCount = count;
    loadRecord(); renderSummary();
  });
  window.SimonProjectKnowledge = {mount, refresh: () => Promise.all([loadRecord(), loadHistory()])};
})();
