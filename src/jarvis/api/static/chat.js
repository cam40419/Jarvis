'use strict';
const el = id => document.getElementById(id);
let session;
let stream;
let pending;
let pendingMemory;
async function api(path, body) {
  const response = await fetch(path, {method: body ? 'POST' : 'GET',
    headers: body ? {'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token} : {},
    body: body ? JSON.stringify(body) : undefined});
  if (response.status === 401) { location.assign('/login'); throw Error('Sign in to continue.'); }
  const result = await response.json();
  if (!response.ok) {
    const error = Error(result.error?.message || 'Request failed.');
    error.code = result.error?.code;
    throw error;
  }
  return result;
}
async function messages() {
  const id = el('threads').value;
  if (!id) return;
  const rows = [];
  let after = 0;
  while (true) {
    const page = await api(`/v1/threads/${id}/messages?after=${after}&limit=100`);
    if (el('threads').value !== id) return;
    rows.push(...page);
    if (page.length < 100) break;
    after = page[page.length - 1].sequence;
  }
  if (el('threads').value !== id) return;
  el('messages').replaceChildren(...rows.map(row => {
    const p = document.createElement('p');
    p.textContent = `${row.role === 'user' ? 'You' : 'Jarvis'}: ${row.text}`;
    return p;
  }));
}
async function threads(selected) {
  const rows = await api('/v1/threads?limit=100');
  el('threads').replaceChildren(...rows.map(row => new Option(row.title, row.id)));
  if (selected) el('threads').value = selected;
  await messages();
}
function report(error) { el('status').textContent = error.message; }
el('create').onsubmit = async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const title = el('title').value;
    pending = pending?.kind === 'thread' && pending.text === title ? pending :
      {kind: 'thread', text: title, key: crypto.randomUUID()};
    const row = await api('/v1/threads', {title, idempotency_key: pending.key});
    pending = null;
    await threads(row.id);
    el('title').value = '';
    el('status').textContent = 'Conversation created.';
  } catch (error) { report(error); } finally { button.disabled = false; }
};
el('threads').onchange = () => { stream?.close(); messages().catch(report); };
el('send').onsubmit = async event => {
  event.preventDefault();
  el('send-button').disabled = true;
  el('workspace').disabled = true;
  el('threads').disabled = true;
  el('status').textContent = 'Jarvis is thinking...';
  try {
    const thread = el('threads').value;
    if (!thread) throw Error('Create a conversation first.');
    const text = el('text').value;
    pending = pending?.kind === 'run' && pending.text === text && pending.thread === thread ? pending :
      {kind: 'run', text, thread, key: crypto.randomUUID()};
    const run = await api(`/v1/threads/${thread}/runs`, {text, idempotency_key: pending.key});
    el('context-details').textContent = JSON.stringify({
      policy: run.context_policy, messages: run.context,
      memories: run.memory_context, summary: run.summary_context,
    }, null, 2);
    pending = null;
    el('text').value = '';
    stream?.close();
    const source = new EventSource(`/v1/runs/${run.id}/events`);
    stream = source;
    source.addEventListener('run.completed', () => {
      source.close();
      messages().catch(report);
      el('status').textContent = 'Run complete. Messages saved.';
    });
    source.onerror = () => { el('status').textContent = 'Stream interrupted. Reload to recover saved messages.'; };
    await messages();
  } catch (error) {
    if (error.code === 'model_error') pending = null;
    report(error);
  } finally {
    el('workspace').disabled = false;
    el('send-button').disabled = false;
    el('threads').disabled = false;
  }
};
el('text').onkeydown = event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!el('send-button').disabled) el('send').requestSubmit();
  }
};
async function memories() {
  const rows = await api('/v1/memories');
  el('memories').replaceChildren(...rows.map(row => {
    const p = document.createElement('p');
    const text = document.createElement('span');
    text.textContent = `${row.subject}: ${row.content} `;
    p.append(text);
    if (row.created_by === session.actor_id || session.scopes.includes('memories:manage')) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = 'Retract';
      button.onclick = async () => {
        button.disabled = true;
        try { await api(`/v1/memories/${row.id}/retract`, {}); await memories(); }
        catch (error) { report(error); button.disabled = false; }
      };
      p.append(button);
    }
    return p;
  }));
}
el('memory-form').onsubmit = async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const subject = el('memory-subject').value;
    const content = el('memory-content').value;
    pendingMemory = pendingMemory?.subject === subject && pendingMemory.content === content ?
      pendingMemory : {subject, content, idempotency_key: crypto.randomUUID()};
    await api('/v1/memories', pendingMemory);
    pendingMemory = null;
    el('memory-content').value = '';
    await memories();
    el('status').textContent = 'Shared memory saved.';
  } catch (error) { report(error); } finally { button.disabled = false; }
};
api('/auth/session').then(async value => {
  session = value;
  const assistant = await api('/v1/assistant');
  el('assistant-mode').textContent = assistant.provider === 'openai' ?
    'Ask Jarvis for help with questions, writing, planning, and code. Selected context and shared memories are sent to OpenAI.' :
    'Test runner: messages are saved and echoed. Start without -TestRunner for AI answers.';
  el('household').textContent = value.memberships.find(m => m.household_id === value.household_id)?.household_name || '';
  await threads();
  await memories();
  el('workspace').disabled = false;
}).catch(report);
