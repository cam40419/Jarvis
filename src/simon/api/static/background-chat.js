'use strict';
(() => {
  const running = new Map(),
    attempts = new Map();
  let timer,
    syncing = false,
    submitting = false;
  const live = (job) => job && ['queued', 'running'].includes(job.status);
  function draw(job) {
    document.querySelectorAll('.background-preview').forEach((node) => node.remove());
    if (!job || job.thread_id !== activeThread) return;
    if (live(job) || job.status === 'failed' || job.status === 'cancelled') {
      const user = message({ role: 'user', text: job.text }, true);
      user.classList.add('background-preview');
      const answer = message(
        {
          role: 'assistant',
          text:
            job.partial_text ||
            (job.status === 'queued'
              ? 'Queued. Work will continue in the background.'
              : 'Working in the background...'),
        },
        true,
      );
      answer.classList.add('background-preview');
      el('messages').append(user, answer);
      el('welcome').hidden = true;
      if (job.error) answer.append(make('p', job.error, 'error'));
      if (job.status === 'cancelled')
        answer.append(make('p', 'Stopped. Partial progress is saved.'));
    }
    setBusy(live(job));
    if (live(job)) status('Saved. You can switch conversations or leave this page.');
    else if (job.status === 'failed')
      status(job.error || 'Work failed. Your request and progress are saved.', true);
    else status(job.status === 'cancelled' ? 'Stopped. Progress is saved.' : '');
  }
  async function sync() {
    clearTimeout(timer);
    if (!ready || !assistant?.background_sessions || syncing) return;
    syncing = true;
    const thread = activeThread;
    try {
      if (thread && !submitting) {
        const jobs = await api('/v1/work-sessions?thread_id=' + thread);
        if (activeThread !== thread) return;
        const job = jobs[0];
        const prior = running.get(thread);
        if (job) running.set(thread, job);
        if (job && !live(job) && (prior?.status !== job.status || prior?.id !== job.id))
          await messages(false);
        if (activeThread === thread) draw(job);
      }
    } catch (error) {
      if (activeThread === thread) status('Reconnecting to saved work...', true);
    } finally {
      syncing = false;
      timer = setTimeout(sync, document.hidden ? 5000 : 1000);
    }
  }
  async function send(text, parentRun) {
    if (busy || submitting || !text.trim()) return;
    submitting = true;
    const original = text,
      ticket = loading;
    let thread = activeThread;
    setBusy(true);
    el('text').value = '';
    drafts.delete(thread || 'new');
    autosize();
    try {
      if (!thread) {
        const created = await api('/v1/threads', {
          title: text.replace(/\s+/g, ' ').slice(0, 65),
          idempotency_key: crypto.randomUUID(),
        });
        thread = created.id;
        if (ticket === loading) {
          activeThread = thread;
          history.replaceState(null, '', appPath('/chat#' + thread));
          el('chat-title').textContent = created.title;
        }
        await refreshThreads();
      }
      const body = {
        text,
        profile: parentRun ? 'deep' : el('profile').value,
        answer_length: el('answer-length').value,
        parent_run_id: parentRun,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      };
      const signature = JSON.stringify(body);
      if (attempts.get(thread)?.signature !== signature)
        attempts.set(thread, { signature, key: crypto.randomUUID() });
      const job = await api('/v1/work-sessions/threads/' + thread, {
        ...body,
        idempotency_key: attempts.get(thread).key,
      });
      attempts.delete(thread);
      running.set(thread, job);
      if (activeThread === thread) draw(job);
      submitting = false;
      await sync();
    } catch (error) {
      if (activeThread === thread || ticket === loading) {
        setBusy(false);
        if (!el('text').value) el('text').value = original;
        autosize();
        report(error);
      }
    } finally {
      submitting = false;
    }
  }
  window.SimonBackground = {
    send,
    async stop() {
      const job = running.get(activeThread);
      if (!live(job)) return;
      try {
        await api('/v1/work-sessions/' + job.id + '/cancel', {});
        await sync();
      } catch (error) {
        report(error);
      }
    },
  };
  window.addEventListener('simon-thread-selected', () => {
    sync();
  });
  window.addEventListener('simon-ready', sync);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) sync();
  });
})();
