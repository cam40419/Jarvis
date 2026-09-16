'use strict';
const el = id => document.getElementById(id);
const make = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};
let session, assistant, activeThread = null, latestRun = null, pending = null, pendingThread = null;
let controller, activeRun, busy = false, ready = false, rows = [], loading = 0, pendingMemory;
let preferences, pendingPreferences, savingPreferences = false;
const drafts = new Map();
const googleReturn = new URLSearchParams(location.search).get('google');
const titleCase = text => text ? text[0].toUpperCase() + text.slice(1) : '';
function status(text = '', error = false) { el('status').textContent = text; el('status').className = error ? 'error' : ''; }
async function api(path, body) {
  const response = await fetch(appPath(path), {
    method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
    headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401) { location.assign(appPath('/login')); throw Error('Sign in to continue.'); }
  if (response.status === 204) return null;
  const result = await response.json();
  if (!response.ok) throw Object.assign(Error(result.error?.message || 'Request failed. Please try again.'), {code: result.error?.code});
  return result;
}
function report(error) { status(error.message, true); }
function autosize() {
  el('text').style.height = 'auto';
  el('text').style.height = Math.min(el('text').scrollHeight, 180) + 'px';
  el('send-button').disabled = !ready || busy || savingPreferences || !el('text').value.trim();
}
function scrollDown(force = false) {
  const view = el('conversation');
  const near = view.scrollHeight - view.scrollTop - view.clientHeight < 180;
  if (force || near) view.scrollTop = view.scrollHeight;
  el('jump-latest').hidden = force || near;
}
el('conversation').onscroll = () => {
  const view = el('conversation');
  el('jump-latest').hidden = view.scrollHeight - view.scrollTop - view.clientHeight < 180;
};
el('jump-latest').onclick = () => scrollDown(true);
function navigation(open) {
  document.body.classList.toggle('nav-open', open);
  el('sidebar-scrim').hidden = !open;
  el('menu-toggle').setAttribute('aria-expanded', String(open));
  el('sidebar').inert = matchMedia('(max-width: 760px)').matches && !open;
}
matchMedia('(max-width: 760px)').addEventListener('change', () => navigation(false));
navigation(false);
el('menu-toggle').onclick = () => navigation(!document.body.classList.contains('nav-open'));
el('sidebar-scrim').onclick = () => navigation(false);
function setBusy(value) {
  busy = value;
  el('new-chat').disabled = value || !ready;
  el('profile').disabled = value || savingPreferences || assistant?.provider !== 'openai';
  el('answer-length').disabled = value || savingPreferences || assistant?.provider !== 'openai';
  el('auto-deep').disabled = value || !ready || !assistant?.auto_deep_enabled || savingPreferences;
  el('save-preferences').disabled = value || !ready || savingPreferences;
  el('stop').hidden = !value || assistant?.provider !== 'openai';
  el('send-button').hidden = value && assistant?.provider === 'openai';
  document.querySelectorAll('.thread-button, [data-prompt], #deepen').forEach(button => button.disabled = value);
  el('messages').setAttribute('aria-busy', String(value));
  autosize();
}
function renderThreads() {
  const query = el('search').value.trim().toLowerCase();
  const matches = rows.filter(row => row.title.toLowerCase().includes(query));
  el('thread-count').textContent = rows.length || '';
  const nodes = [];
  let group = '';
  for (const row of matches) {
    const day = new Date(row.created_at).toLocaleDateString() === new Date().toLocaleDateString() ? 'Today' : 'Earlier';
    if (group !== day) { nodes.push(make('div', day, 'thread-day')); group = day; }
    const button = make('button', row.title, 'thread-button');
    button.title = row.title; button.dataset.threadId = row.id;
    button.setAttribute('aria-current', String(row.id === activeThread));
    button.disabled = busy;
    button.onclick = () => selectThread(row.id).catch(report);
    nodes.push(button);
  }
  el('threads').replaceChildren(...(nodes.length ? nodes : [make('p', query ? 'No matching conversations.' : 'Your conversations will appear here.', 'muted')]));
}
async function refreshThreads() {
  const found = [];
  for (let offset = 0; ; offset += 100) {
    const page = await api('/v1/threads?limit=100&offset=' + offset);
    found.push(...page);
    if (page.length < 100) break;
  }
  rows = found; renderThreads();
}
el('search').oninput = renderThreads;
function message(row, provisional = false) {
  const article = make('article', undefined, 'message ' + row.role + (provisional ? ' draft' : ''));
  article.dataset.messageId = row.id || '';
  const header = make('div', undefined, 'message-header');
  if (row.role === 'assistant') {
    const logo = document.createElement('img'); logo.src = appPath('/assets/simon.svg'); logo.alt = '';
    header.append(logo, make('span', 'Simon'));
  } else header.append(make('span', 'You'), make('span', el('user-avatar').textContent, 'avatar'));
  const body = make('div', undefined, 'message-body');
  if (row.role === 'assistant' && !provisional) body.append(SimonMarkdown.render(row.text));
  else body.textContent = row.text;
  article.append(header, body);
  if (row.role === 'assistant' && !provisional) {
    const tools = make('div', undefined, 'message-tools'), copy = make('button', 'Copy', 'text-button');
    copy.type = 'button'; copy.setAttribute('aria-label', 'Copy answer');
    copy.onclick = () => SimonMarkdown.copy(row.text, copy);
    tools.append(copy); article.append(tools);
  }
  return article;
}
function showRun(run) {
  latestRun = run;
  const article = run && [...el('messages').children].find(node => node.dataset.messageId === run.output_message_id);
  if (!article) return;
  const tools = article.querySelector('.message-tools');
  if (assistant.provider === 'openai') {
    const deepen = make('button', 'Think deeper', 'text-button'); deepen.id = 'deepen'; deepen.disabled = busy;
    deepen.onclick = () => {
      const original = run.context.find(item => item.source_message_id === run.input_message_id);
      if (original) sendMessage(original.text, run.id);
    };
    tools.append(deepen);
  }
  const info = make('details', undefined, 'run-info');
  const selected = run.profile;
  const summary = make('summary', selected ? (selected.requested === 'auto' ? 'Auto · ' : '') +
    titleCase(selected.selected) + ' · ' + (run.total_ms / 1000).toFixed(1) + 's' : 'Run details');
  summary.id = 'run-profile';
  const pre = make('pre', JSON.stringify({
    model: run.model_name, profile: run.profile, reasoning: run.model_request?.reasoning_effort,
    timing: {first_text_ms: run.first_text_ms, total_ms: run.total_ms},
    usage: {input: run.input_tokens, output: run.output_tokens, reasoning: run.reasoning_tokens},
    policy: run.context_policy, messages: run.context, memories: run.memory_context, summary: run.summary_context,
  }, null, 2));
  pre.id = 'context-details';
  info.append(summary, pre); tools.append(info);
}
function showFeedback(answer) {
  const article = [...el('messages').children].find(node => node.dataset.messageId === answer.output_message_id);
  if (!article) return;
  article.querySelector('.answer-feedback')?.remove();
  const group = make('div', undefined, 'answer-feedback');
  group.setAttribute('role', 'group'); group.setAttribute('aria-label', 'Answer feedback');
  let pendingFeedback;
  for (const [rating, label] of [['helpful', 'Helpful'], ['too_slow', 'Too slow'], ['needs_depth', 'Needs more depth']]) {
    const button = make('button', label, 'feedback-button'); button.type = 'button';
    button.setAttribute('aria-pressed', String(answer.feedback?.rating === rating));
    button.onclick = async () => {
      const value = answer.feedback?.rating === rating ? null : rating;
      const signature = JSON.stringify({rating: value, expected_version: answer.feedback?.version || 0});
      if (pendingFeedback?.signature !== signature) pendingFeedback = {signature, key: crypto.randomUUID()};
      group.querySelectorAll('button').forEach(item => item.disabled = true);
      try {
        const feedback = await api('/v1/runs/' + answer.run_id + '/feedback', {
          ...JSON.parse(signature), idempotency_key: pendingFeedback.key,
        });
        if (article.isConnected) {
          showFeedback({...answer, feedback});
          status(feedback.rating ? 'Feedback saved. Thank you.' : 'Feedback removed.');
        }
      } catch (error) {
        if (article.isConnected) report(error);
        group.querySelectorAll('button').forEach(item => item.disabled = false);
      }
    };
    group.append(button);
  }
  article.append(group);
}
async function messages(forceScroll = true) {
  const id = activeThread, ticket = ++loading;
  if (!id) return;
  const found = [];
  for (let after = 0; ;) {
    const page = await api('/v1/threads/' + id + '/messages?after=' + after + '&limit=100');
    if (activeThread !== id || ticket !== loading) return;
    found.push(...page);
    if (page.length < 100) break;
    after = page[page.length - 1].sequence;
  }
  const run = await api('/v1/threads/' + id + '/latest-run');
  const answers = [];
  for (let offset = 0; ; offset += 100) {
    const page = await api('/v1/threads/' + id + '/answers?offset=' + offset + '&limit=100');
    if (activeThread !== id || ticket !== loading) return;
    answers.push(...page);
    if (page.length < 100) break;
  }
  if (activeThread !== id || ticket !== loading) return;
  el('messages').replaceChildren(...found.map(row => message(row)));
  el('welcome').hidden = found.length > 0;
  showRun(run); answers.forEach(answer => { showFeedback(answer); showConnectedResults(answer); }); scrollDown(forceScroll);
}
async function selectThread(id) {
  if (busy) return;
  drafts.set(activeThread || 'new', el('text').value);
  activeThread = id; latestRun = null; pending = null; pendingThread = null; loading++;
  el('text').value = drafts.get(id || 'new') || ''; autosize();
  el('chat-title').textContent = rows.find(row => row.id === id)?.title || 'New conversation';
  el('messages').replaceChildren(); el('welcome').hidden = !!id; status(id ? 'Loading conversation…' : '');
  history.replaceState(null, '', appPath(id ? '/chat#' + id : '/chat'));
  renderThreads(); navigation(false);
  if (id) await messages();
  status(); el('text').focus();
}
el('new-chat').onclick = () => selectThread(null).catch(report);
window.addEventListener('hashchange', () => {
  if (!ready) return;
  if (busy) {
    history.replaceState(null, '', appPath(activeThread ? '/chat#' + activeThread : '/chat'));
    return;
  }
  const wanted = location.hash.slice(1);
  selectThread(rows.some(row => row.id === wanted) ? wanted : null).catch(report);
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') { navigation(false); el('response-settings').open = false; }
  if (event.altKey && event.key.toLowerCase() === 'n') { event.preventDefault(); selectThread(null).catch(report); }
});
document.querySelectorAll('[data-prompt]').forEach(button => {
  button.onclick = () => { el('text').value = button.dataset.prompt; autosize(); el('text').focus(); };
});
function settingsLabel() {
  el('setting-label').textContent = titleCase(el('profile').value);
  el('composer-hint').textContent = el('profile').value === 'auto' ? 'You ask. Simon handles the details.' : 'Using your response settings';
}
el('profile').onchange = settingsLabel;
function loadPreferenceControls() {
  el('profile').value = preferences.profile;
  el('answer-length').value = preferences.answer_length;
  el('auto-deep').checked = preferences.auto_deep_enabled && assistant.auto_deep_enabled;
  settingsLabel();
}
el('save-preferences').onclick = async () => {
  if (savingPreferences || busy) return;
  const body = {profile: el('profile').value, answer_length: el('answer-length').value,
    auto_deep_enabled: el('auto-deep').checked, expected_version: preferences.version};
  const signature = JSON.stringify(body);
  if (pendingPreferences?.signature !== signature) pendingPreferences = {signature, key: crypto.randomUUID()};
  savingPreferences = true; setBusy(busy);
  el('preferences-status').textContent = 'Saving…';
  try {
    preferences = await api('/v1/preferences', {...body, idempotency_key: pendingPreferences.key});
    pendingPreferences = null;
    loadPreferenceControls();
    el('preferences-status').textContent = 'Saved for you in this household. Applies to future requests.';
  } catch (error) {
    el('preferences-status').textContent = error.message;
  } finally { savingPreferences = false; setBusy(busy); }
};
el('text').oninput = () => { drafts.set(activeThread || 'new', el('text').value); autosize(); };
el('text').onkeydown = event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!el('send-button').disabled && !busy) el('send').requestSubmit();
  }
};
el('send').onsubmit = event => { event.preventDefault(); sendMessage(el('text').value.trim()); };
async function sendMessage(text, parentRun = null) {
  if (busy || savingPreferences || !text.trim()) return;
  setBusy(true); status(); el('response-settings').open = false;
  controller = new AbortController(); activeRun = null;
  const startingDraft = el('text').value;
  let preview, userPreview, thought, run;
  try {
    if (!activeThread) {
      pendingThread ||= {title: text.replace(/\s+/g, ' ').slice(0, 65), idempotency_key: crypto.randomUUID()};
      const thread = await api('/v1/threads', pendingThread);
      activeThread = thread.id; pendingThread = null; drafts.delete('new');
      history.replaceState(null, '', appPath('/chat#' + activeThread));
      el('chat-title').textContent = thread.title;
      await refreshThreads();
    }
    // Stop may arrive while the new conversation is being saved.
    if (controller.signal.aborted) throw new DOMException('Stopped', 'AbortError');
    const payload = {text, profile: parentRun ? 'deep' : el('profile').value,
      answer_length: el('answer-length').value, parent_run_id: parentRun,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone};
    const signature = JSON.stringify({thread: activeThread, ...payload});
    if (pending?.signature !== signature) pending = {signature, key: crypto.randomUUID()};
    payload.idempotency_key = pending.key;
    el('welcome').hidden = true;
    userPreview = message({role: 'user', text}, true);
    preview = message({role: 'assistant', text: ''}, true);
    thought = make('div', undefined, 'thinking');
    thought.append(make('i'), make('i'), make('i'), make('span', 'Finding the right approach…'));
    preview.append(thought); el('messages').append(userPreview, preview); scrollDown(true);
    if (assistant.provider === 'openai') {
      const response = await fetch(appPath('/v1/threads/' + activeThread + '/runs/stream'), {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token},
        body: JSON.stringify(payload), signal: controller.signal,
      });
      if (!response.ok) {
        const result = await response.json();
        if (response.status === 401) location.assign(appPath('/login'));
        throw Object.assign(Error(result.error?.message || 'Unable to start an answer.'), {code: result.error?.code});
      }
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, {stream: true});
        let boundary;
        while ((boundary = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
          const kind = frame.split('\n').find(line => line.startsWith('event: '))?.slice(7);
          const data = JSON.parse(frame.split('\n').filter(line => line.startsWith('data: ')).map(line => line.slice(6)).join('\n'));
          if (kind === 'run.started') {
            activeRun = data.id;
            thought.querySelector('span').textContent = data.profile.selected === 'deep' ? 'Thinking this through carefully…' : 'Thinking…';
          } else if (kind === 'text.delta') {
            thought.hidden = true;
            preview.querySelector('.message-body').append(document.createTextNode(data.text)); scrollDown();
          } else if (kind === 'run.completed') run = data;
          else if (kind === 'run.error') throw Object.assign(Error(data.message), {code: data.code});
        }
      }
      if (!run) throw Error('The connection ended early. Send again to check this request, or reload for saved answers.');
    } else {
      run = await api('/v1/threads/' + activeThread + '/runs', payload);
    }
    pending = null;
    if (!parentRun && el('text').value === startingDraft) { el('text').value = ''; drafts.delete(activeThread); }
    await messages(); status('Run complete. Messages saved.');
  } catch (error) {
    if (error.code === 'model_error' || error.name === 'AbortError') pending = null;
    if (error.name === 'AbortError') status('Stopped. Draft kept. Any completed answer is saved.');
    else report(error);
    userPreview?.remove(); preview?.remove();
    // Recover a completion that won a race with Stop or a lost connection.
    if (activeThread) {
      try { await messages(); } catch { /* Keep the actionable original error. */ }
    }
  } finally {
    userPreview?.remove(); preview?.remove(); activeRun = null;
    setBusy(false); el('text').focus(); autosize();
  }
}
el('stop').onclick = () => {
  const runId = activeRun;
  controller?.abort();
  if (runId) api('/v1/runs/' + runId + '/cancel', {}).catch(report);
};
function openPanel(id) { navigation(false); el(id).showModal(); }
el('memory-open').onclick = () => openPanel('memory-panel');
el('about-open').onclick = () => openPanel('about-panel');
document.querySelectorAll('[data-close]').forEach(button => {
  button.onclick = () => el(button.dataset.close).close();
});
document.querySelectorAll('dialog').forEach(dialog => {
  dialog.addEventListener('click', event => { if (event.target === dialog && event.offsetX < 0) dialog.close(); });
});
async function memories() {
  const list = [];
  for (let offset = 0; ; offset += 100) {
    const page = await api('/v1/memories?limit=100&offset=' + offset); list.push(...page);
    if (page.length < 100) break;
  }
  el('memory-count').textContent = list.length;
  el('memories').replaceChildren(...(list.length ? list.map(row => {
    const card = make('div', undefined, 'memory-card');
    card.append(make('strong', row.subject), make('p', row.content));
    if (row.created_by === session.actor_id || session.scopes.includes('memories:manage')) {
      const button = make('button', 'Retract', 'text-button'); button.type = 'button';
      button.onclick = async () => {
        button.disabled = true;
        try { await api('/v1/memories/' + row.id + '/retract', {}); await memories(); el('memory-status').textContent = 'Memory removed from future answers.'; }
        catch (error) { el('memory-status').textContent = error.message; button.disabled = false; }
      };
      card.append(button);
    }
    return card;
  }) : [make('p', 'A fresh start. Save a preference or fact below.', 'muted')]));
}
el('memory-form').onsubmit = async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const subject = el('memory-subject').value, content = el('memory-content').value;
    if (pendingMemory?.subject !== subject || pendingMemory?.content !== content) pendingMemory = {subject, content, idempotency_key: crypto.randomUUID()};
    await api('/v1/memories', pendingMemory); pendingMemory = null;
    el('memory-content').value = ''; el('memory-subject').value = ''; await memories();
    el('memory-status').textContent = 'Shared memory saved.';
  } catch (error) { el('memory-status').textContent = error.message; }
  finally { button.disabled = false; }
};
function theme(value) {
  document.documentElement.dataset.theme = value;
  el('theme-toggle').setAttribute('aria-label', 'Switch to ' + (value === 'dark' ? 'light' : 'dark') + ' theme');
  try { localStorage.setItem('simon-theme', value); } catch { /* Storage may be disabled. */ }
}
try { theme(localStorage.getItem('simon-theme') || 'light'); } catch { theme('light'); }
el('theme-toggle').onclick = () => theme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
document.addEventListener('DOMContentLoaded', async () => {
  setBusy(false);
  try {
    session = await api('/auth/session'); assistant = await api('/v1/assistant');
    preferences = await api('/v1/preferences');
    loadPreferenceControls();
    const member = session.memberships.find(m => m.household_id === session.household_id);
    el('user-name').textContent = member.display_name;
    el('user-avatar').textContent = member.display_name.split(/\s+/).map(word => word[0]).slice(0, 2).join('').toUpperCase();
    el('household').textContent = member.household_name;
    const hour = new Date().getHours();
    el('greeting').textContent = hour < 12 ? 'GOOD MORNING. LET’S MAKE SOME SPACE.' : hour < 18 ? 'GOOD AFTERNOON. ROOM FOR A NEW IDEA?' : 'GOOD EVENING. LET’S THINK IT THROUGH.';
    el('connection').replaceChildren(make('i'), document.createTextNode(assistant.provider === 'openai' ? 'Ready when you are' : 'Offline test mode'));
    el('assistant-mode').textContent = assistant.provider === 'openai' ? 'Simon · Thoughtfully automatic' : 'Test runner · Responses are echoed';
    el('mode-help').textContent = assistant.auto_deep_enabled ? 'Save to apply the Deep preference. Deep uses more time and API credits; it remains available manually.' : 'Automatic Deep is disabled on this server. Deep remains available manually.';
    await refreshThreads();
    const wanted = location.hash.slice(1);
    ready = true; setBusy(false);
    el('connections-open').disabled = false;
    await selectThread(rows.some(row => row.id === wanted) ? wanted : null);
    await memories();
    if (googleReturn) { openPanel('connections-panel'); await connections(); }
  } catch (error) { report(error); }
});
