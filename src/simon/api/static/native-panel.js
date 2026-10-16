'use strict';
(() => {
  const bridge = () => window.SimonNativeProjects;
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const button = (text, action, owner = false) => {
    const item = node('button', text, 'np-button np-quiet');
    item.type = 'button';
    if (owner) item.dataset.owner = 'true';
    item.onclick = action;
    return item;
  };
  const precise = (value) => {
    if (typeof value === 'number')
      return Number.isFinite(value) && (!Number.isInteger(value) || Number.isSafeInteger(value));
    if (Array.isArray(value)) return value.every(precise);
    return !value || typeof value !== 'object' || Object.values(value).every(precise);
  };
  const parseValue = (text) => {
    const value = JSON.parse(text);
    if (!precise(value)) throw Error('Use finite JSON values within browser numeric precision.');
    return value;
  };
  const encodedBytes = (buffer) => {
    const bytes = new Uint8Array(buffer);
    let raw = '';
    for (let offset = 0; offset < bytes.length; offset += 8192)
      raw += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
    return btoa(raw);
  };

  function create(options) {
    const $ = (suffix) => document.getElementById(options.prefix + '-' + suffix);
    const dialog = $('dialog');
    const state = {
      generation: 0,
      projectId: null,
      workspaceId: null,
      busy: false,
      uncertain: false,
      receiptAbsent: false,
      dirty: false,
      conflict: false,
      canManage: false,
      changed: false,
      pending: null,
    };
    const active = (ticket) =>
      ticket === state.generation &&
      dialog.open &&
      bridge().getProject()?.id === state.projectId &&
      bridge().getWorkspaceId() === state.workspaceId;
    const path = (suffix = '') => options.path(state) + suffix;
    const feedback = (message = '', tone = '') => {
      $('feedback').textContent = message;
      $('feedback').dataset.tone = tone;
    };
    const lock = () => {
      for (const control of dialog.querySelectorAll('input, textarea, select, button'))
        control.disabled = state.busy || state.uncertain;
      for (const control of dialog.querySelectorAll('[data-owner="true"]'))
        control.disabled ||= !state.canManage;
      if ($('close')) $('close').disabled = state.busy || state.uncertain || Boolean(state.editing);
      if ($('refresh')) $('refresh').disabled = state.busy || state.uncertain || state.dirty;
      if ($('check')) $('check').disabled = state.busy || !state.uncertain;
      if ($('retry')) {
        $('retry').disabled = state.busy || !state.uncertain || !state.receiptAbsent;
        $('retry').hidden = !state.receiptAbsent;
      }
      if ($('uncertain')) $('uncertain').hidden = !state.uncertain;
      if ($('conflict')) $('conflict').hidden = !state.conflict;
      if ($('reload')) $('reload').disabled = state.busy || state.uncertain || !state.conflict;
      options.lock?.(state);
      dialog.setAttribute('aria-busy', String(state.busy));
    };
    const reset = () => {
      state.generation++;
      state.projectId = state.workspaceId = state.pending = null;
      state.busy =
        state.uncertain =
        state.receiptAbsent =
        state.dirty =
        state.conflict =
        state.changed =
          false;
      state.canManage = false;
      state.editing = null;
      options.reset?.(state);
      feedback();
      if (dialog.open) dialog.close();
    };
    const failure = (error) => {
      if ([401, 403, 404].includes(error.status)) reset();
      else feedback(error.message, 'error');
    };
    const run = async (operation) => {
      if (state.busy || state.uncertain) return;
      const ticket = state.generation;
      state.busy = true;
      lock();
      try {
        await operation(ticket);
      } catch (error) {
        if (active(ticket)) failure(error);
      } finally {
        if (active(ticket)) {
          state.busy = false;
          lock();
        }
      }
    };
    const afterSaved = async (operation, result) => {
      const ticket = state.generation;
      state.pending = null;
      state.uncertain = state.receiptAbsent = state.conflict = false;
      state.changed = true;
      try {
        await options.saved(operation, result, ticket);
      } catch (error) {
        if (active(ticket)) {
          if ([401, 403, 404].includes(error.status)) failure(error);
          else
            feedback(
              'The request was saved. Refresh to inspect its result. ' + error.message,
              'warning',
            );
        }
      }
    };
    const perform = async () => {
      if (state.busy || !state.pending) return;
      const ticket = state.generation,
        operation = state.pending;
      state.busy = true;
      lock();
      feedback(operation.message || 'Saving exact request…');
      try {
        const result = await bridge().request(
          operation.url,
          operation.body,
          operation.method || 'POST',
        );
        if (!active(ticket)) return;
        await afterSaved(operation, result);
      } catch (error) {
        if (!active(ticket)) return;
        if (!error.status || error.status >= 500) {
          state.uncertain = true;
          state.receiptAbsent = false;
          feedback(
            'The response is uncertain. Check the saved receipt before making another change.',
            'warning',
          );
        } else {
          state.pending = null;
          state.uncertain = false;
          if ([401, 403, 404].includes(error.status)) {
            failure(error);
            return;
          }
          state.conflict = error.status === 409;
          feedback(error.message + (state.editing ? ' Your draft is preserved.' : ''), 'error');
        }
      } finally {
        if (active(ticket)) {
          state.busy = false;
          lock();
        }
      }
    };
    const save = async (operation) => {
      if (state.busy || state.uncertain || state.conflict) return;
      state.pending = {
        ...operation,
        url: path(operation.suffix || ''),
        receiptUrl: path('/operations/' + encodeURIComponent(operation.body.idempotency_key || '')),
        body: structuredClone({
          ...operation.body,
          idempotency_key: operation.body.idempotency_key || crypto.randomUUID(),
        }),
      };
      state.pending.receiptUrl = path(
        '/operations/' + encodeURIComponent(state.pending.body.idempotency_key),
      );
      await perform();
    };
    const check = async () => {
      if (state.busy || !state.uncertain || !state.pending) return;
      const ticket = state.generation,
        operation = state.pending;
      state.busy = true;
      lock();
      try {
        const result = await bridge().request(operation.receiptUrl);
        if (!active(ticket)) return;
        await afterSaved(operation, result);
      } catch (error) {
        if (!active(ticket)) return;
        if (error.status === 404) {
          state.receiptAbsent = true;
          feedback(
            'No receipt is recorded yet. Retry the same saved request to resolve it safely.',
            'warning',
          );
        } else if ([401, 403].includes(error.status)) failure(error);
        else feedback('The request remains uncertain. ' + error.message, 'warning');
      } finally {
        if (active(ticket)) {
          state.busy = false;
          lock();
        }
      }
    };
    const open = async (target = null) => {
      const project = bridge().getProject();
      if (!project || dialog.open) return;
      reset();
      state.projectId = project.id;
      state.workspaceId = bridge().getWorkspaceId();
      $('project').textContent = project.name;
      dialog.showModal();
      $('heading').focus();
      await run(async (ticket) => options.load(target, ticket));
    };
    const close = () => {
      if (state.busy || state.uncertain || state.editing) return;
      const changed = state.changed;
      reset();
      if (changed && bridge().getProject()) bridge().onTeamChanged();
    };
    if ($('close')) $('close').onclick = close;
    if ($('refresh')) $('refresh').onclick = () => run((ticket) => options.refresh(ticket));
    if ($('check')) $('check').onclick = check;
    if ($('retry')) $('retry').onclick = perform;
    if ($('reload')) $('reload').onclick = () => run((ticket) => options.reload(ticket));
    dialog.addEventListener('input', () => {
      if (state.editing) state.dirty = true;
      lock();
    });
    dialog.addEventListener('cancel', (event) => {
      event.preventDefault();
      close();
    });
    window.addEventListener('beforeunload', (event) => {
      if (state.busy || state.uncertain || state.dirty) event.preventDefault();
    });
    window.addEventListener('native-project-loading', (event) => {
      if (
        state.projectId &&
        (event.detail.id !== state.projectId || bridge().getWorkspaceId() !== state.workspaceId)
      )
        reset();
    });
    window.addEventListener('native-project-unavailable', reset);
    return {
      $,
      state,
      dialog,
      active,
      path,
      feedback,
      lock,
      reset,
      failure,
      run,
      save,
      open,
      close,
    };
  }

  window.SimonNativePanel = { create, node, button, precise, parseValue, encodedBytes };
})();
