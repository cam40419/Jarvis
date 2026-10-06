'use strict';
(() => {
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const canWrite = () => typeof session !== 'undefined' && session?.scopes?.includes('jobs:write');
  const button = (text, callback) => {
    const item = node('button', text, 'pc-button');
    item.type = 'button';
    item.onclick = callback;
    return item;
  };
  const kinds = ['supplier', 'product', 'quote', 'contact', 'decision', 'procedure', 'note'];
  const titleCase = (text) => text.charAt(0).toUpperCase() + text.slice(1).replaceAll('_', ' ');
  let projectId = null,
    epoch = 0,
    nextOffset = null,
    loading = false,
    activityCount = null;
  let items = [],
    editorState = null;
  let draftBindings = [];
  function detachDrafts() {
    draftBindings.forEach((binding) => binding.detach());
    draftBindings = [];
  }
  const panel = node('section', undefined, 'pw-panel');
  panel.id = 'project-workspace';
  const heading = node('div', undefined, 'pc-section-heading');
  const headingText = node('div');
  headingText.append(
    node('h3', 'Business records & procedures'),
    node(
      'p',
      'Keep reusable facts, sources, and ways of working together. Every saved change retains its earlier revision.',
    ),
  );
  const create = button('New record', () => openEditor());
  heading.append(headingText, create);
  const search = node('form', undefined, 'pw-search');
  const query = node('input');
  query.type = 'search';
  query.maxLength = 200;
  query.placeholder = 'Search suppliers, products, decisions…';
  query.setAttribute('aria-label', 'Search business records');
  const kind = node('select');
  kind.setAttribute('aria-label', 'Record type');
  kind.append(new Option('All record types', ''));
  kinds.forEach((value) => kind.append(new Option(titleCase(value), value)));
  const find = node('button', 'Search', 'pc-button');
  find.type = 'submit';
  const refresh = button('Refresh records', () => load(false));
  const archivedLabel = node('label', undefined, 'pw-check');
  const archived = node('input');
  archived.type = 'checkbox';
  archivedLabel.append(archived, document.createTextNode('Include archived'));
  search.append(query, kind, archivedLabel, find, refresh);
  const status = node('p', '', 'pw-status');
  status.setAttribute('role', 'status');
  const list = node('div', undefined, 'pw-records');
  const more = button('More records', () => load(true));
  more.hidden = true;
  panel.append(heading, search, status, list, more);
  const dialog = node('dialog', undefined, 'pc-dialog pw-dialog');
  dialog.id = 'pw-record-dialog';
  dialog.setAttribute('aria-labelledby', 'pw-dialog-title');
  document.body.append(dialog);

  const path = (suffix = '', id = projectId) =>
    '/v1/projects/' + encodeURIComponent(id) + '/workspace' + suffix;
  function message(text, error = false) {
    status.textContent = text;
    status.classList.toggle('pc-error', error);
  }
  function select(id) {
    if (id === projectId) return;
    projectId = id;
    ++epoch;
    loading = false;
    nextOffset = null;
    items = [];
    query.value = '';
    kind.value = '';
    archived.checked = false;
    activityCount = null;
    list.replaceChildren();
    message('');
    more.hidden = true;
    if (dialog.open && !editorState?.busy) dialog.close();
  }
  async function load(append = false) {
    if (!projectId || loading) return;
    const requestEpoch = epoch;
    const requestedQuery = query.value.trim(),
      requestedKind = kind.value;
    const parameters = new URLSearchParams({
      query: requestedQuery,
      offset: String(append ? nextOffset || 0 : 0),
      limit: '20',
    });
    if (requestedKind) parameters.set('kind', requestedKind);
    if (archived.checked) parameters.set('include_archived', 'true');
    loading = true;
    find.disabled = true;
    more.disabled = true;
    message('Loading saved records…');
    try {
      const page = await api(path('/records?' + parameters));
      if (requestEpoch !== epoch) return;
      items = append ? items.concat(page.items) : page.items;
      nextOffset = page.next_offset;
      render();
      message(
        items.length
          ? `${items.length} records shown. Changes are saved on this server.`
          : 'No matching records yet. Add a fact or a reusable procedure to get started.',
      );
    } catch (error) {
      if (requestEpoch === epoch) message(error.message, true);
    } finally {
      if (requestEpoch === epoch) {
        loading = false;
        find.disabled = false;
        more.disabled = false;
      }
    }
  }
  function render() {
    list.replaceChildren();
    items.forEach((record) => {
      const item = node('article', undefined, 'pw-record');
      item.dataset.recordId = record.id;
      item.append(
        node('span', `${titleCase(record.kind)} · Version ${record.version}`, 'pw-meta'),
        node('h4', record.title),
      );
      const confidence =
        record.confidence === 'supported' ? 'Source-linked' : titleCase(record.confidence);
      item.append(
        node(
          'p',
          confidence +
            (record.status === 'archived' ? ' · Archived' : '') +
            (record.needs_review ? ' · Review due' : ''),
          'pw-meta',
        ),
      );
      if (record.summary) item.append(node('p', record.summary.slice(0, 320), 'pw-excerpt'));
      const actions = node('div', undefined, 'pw-actions');
      actions.append(button('Open record', () => openRecord(record.id)));
      if (canWrite()) actions.append(button('Edit', () => openEditor(record)));
      item.append(actions);
      list.append(item);
    });
    more.hidden = nextOffset === null;
  }
  function dialogHeader(title) {
    const header = node('div', undefined, 'pc-dialog-header');
    const heading = node('h2', title);
    heading.id = 'pw-dialog-title';
    header.append(
      heading,
      button('Close', () => {
        if (!editorState?.busy) dialog.close();
      }),
    );
    return header;
  }
  function prose(text) {
    const content = node('div', undefined, 'pw-prose');
    if (window.SimonMarkdown) content.append(window.SimonMarkdown.render(text));
    else content.textContent = text;
    return content;
  }
  async function openRecord(id) {
    const requestedProject = projectId,
      requestEpoch = epoch;
    try {
      const record = await api(path('/records/' + encodeURIComponent(id)));
      if (requestEpoch !== epoch) return;
      editorState = null;
      dialog.replaceChildren(dialogHeader(record.title));
      const content = node('div', undefined, 'pw-detail');
      content.append(
        node(
          'p',
          `${titleCase(record.kind)} · Version ${record.version} · ${titleCase(record.confidence)}`,
          'pw-meta',
        ),
      );
      if (record.summary) content.append(prose(record.summary));
      if (Object.keys(record.fields).length) {
        const fields = node('dl', undefined, 'pw-fields');
        Object.entries(record.fields).forEach(([name, value]) =>
          fields.append(node('dt', name), node('dd', value)),
        );
        content.append(fields);
      }
      for (const [label, entries] of [
        ['Steps', record.steps],
        ['Completion checks', record.success_checks],
      ]) {
        if (!entries.length) continue;
        const entriesList = node('ol');
        entries.forEach((text) => entriesList.append(node('li', text)));
        content.append(node('h3', label), entriesList);
      }
      if (record.sources.length) {
        content.append(node('h3', 'Sources'));
        const sources = node('ul');
        record.sources.forEach((source) => {
          const row = node('li');
          if (source.url) {
            const link = node('a', source.label);
            link.href = source.url;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            row.append(link);
          } else {
            row.append(
              button(source.label, async () => {
                try {
                  const original = await api(
                    '/v1/projects/' +
                      encodeURIComponent(requestedProject) +
                      '/knowledge/history/' +
                      source.activity_id,
                  );
                  if (requestedProject === projectId) row.append(prose(original.text));
                } catch (error) {
                  row.append(node('p', error.message, 'pc-error'));
                }
              }),
            );
          }
          sources.append(row);
        });
        content.append(sources);
      }
      if (record.checked_at)
        content.append(
          node('p', 'Last checked: ' + new Date(record.checked_at).toLocaleString(), 'pw-meta'),
        );
      if (record.review_after)
        content.append(
          node('p', 'Review after: ' + new Date(record.review_after).toLocaleString(), 'pw-meta'),
        );
      const history = node('details');
      history.append(node('summary', 'Revision history'));
      const historyList = node('div');
      history.append(historyList);
      history.addEventListener('toggle', async () => {
        if (!history.open || historyList.childElementCount) return;
        try {
          const revisions = await api(path('/records/' + id + '/revisions', requestedProject));
          revisions.items.forEach((version) => {
            const item = node('details');
            item.append(
              node(
                'summary',
                `Version ${version.version} · ${new Date(version.updated_at).toLocaleString()}`,
              ),
              prose(version.summary),
            );
            item.append(
              node(
                'pre',
                JSON.stringify(
                  {
                    fields: version.fields,
                    sources: version.sources,
                    steps: version.steps,
                    success_checks: version.success_checks,
                  },
                  null,
                  2,
                ),
              ),
            );
            historyList.append(item);
          });
          if (revisions.next_before_version)
            historyList.append(
              node(
                'p',
                'Showing the 20 latest revisions. Earlier revisions remain in the archive.',
              ),
            );
        } catch (error) {
          historyList.append(node('p', error.message, 'pc-error'));
        }
      });
      content.append(history);
      if (canWrite()) content.append(button('Edit record', () => openEditor(record)));
      dialog.append(content);
      if (!dialog.open) dialog.showModal();
    } catch (error) {
      if (requestEpoch === epoch) message(error.message, true);
    }
  }
  function field(form, label, name, value = '', multiline = false) {
    const wrapper = node('div', undefined, 'pw-form-field');
    const caption = node('label', label);
    caption.htmlFor = 'pw-edit-' + name;
    const input = node(multiline ? 'textarea' : 'input');
    input.id = caption.htmlFor;
    input.name = name;
    input.value = value;
    if (multiline) input.rows = 4;
    wrapper.append(caption, input);
    form.append(wrapper);
    return input;
  }
  function openEditor(record = null) {
    if (!canWrite()) return;
    detachDrafts();
    const state = {
      record,
      projectId,
      epoch,
      id: record?.id || crypto.randomUUID(),
      key: crypto.randomUUID(),
      busy: false,
    };
    editorState = state;
    dialog.replaceChildren(dialogHeader(record ? 'Edit record' : 'New record'));
    const form = node('form', undefined, 'pw-form');
    const fields = node('fieldset');
    const title = field(fields, 'Title', 'title', record?.title);
    title.maxLength = 240;
    title.required = true;
    const kindLabel = node('label', 'Record type');
    kindLabel.htmlFor = 'pw-edit-kind';
    const type = node('select');
    type.id = kindLabel.htmlFor;
    kinds.forEach((kind) => type.append(new Option(titleCase(kind), kind)));
    type.value = record?.kind || 'note';
    fields.append(kindLabel, type);
    const summary = field(fields, 'Overview', 'summary', record?.summary, true);
    summary.maxLength = 8000;
    const properties = field(
      fields,
      'Details (one Name: value per line)',
      'fields',
      Object.entries(record?.fields || {})
        .map(([key, value]) => `${key}: ${value}`)
        .join('\n'),
      true,
    );
    const originalProperties = properties.value;
    const sources = field(
      fields,
      'Source URLs (one per line)',
      'sources',
      (record?.sources || [])
        .filter((source) => source.url)
        .map((source) => source.url)
        .join('\n'),
      true,
    );
    fields.append(
      node(
        'p',
        'Existing project activity references are retained. Source-linked records need at least one reference.',
        'pc-help',
      ),
    );
    const confidenceLabel = node('label', 'Confidence');
    confidenceLabel.htmlFor = 'pw-edit-confidence';
    const confidence = node('select');
    confidence.id = confidenceLabel.htmlFor;
    [
      ['unverified', 'Unverified'],
      ['supported', 'Source-linked'],
      ['owner_confirmed', 'Confirmed by me'],
    ].forEach(([value, label]) => confidence.append(new Option(label, value)));
    confidence.value = record?.confidence || 'unverified';
    fields.append(confidenceLabel, confidence);
    const steps = field(
      fields,
      'Procedure steps (one per line)',
      'steps',
      record?.steps?.join('\n'),
      true,
    );
    const checks = field(
      fields,
      'Completion checks (one per line)',
      'checks',
      record?.success_checks?.join('\n'),
      true,
    );
    const toggleProcedure = () => {
      steps.parentElement.hidden = checks.parentElement.hidden = type.value !== 'procedure';
    };
    type.onchange = toggleProcedure;
    toggleProcedure();
    const review = field(
      fields,
      'Review after (optional)',
      'review',
      record?.review_after?.slice(0, 10),
    );
    review.type = 'date';
    const archiveLabel = node('label', undefined, 'pw-check');
    const archive = node('input');
    archive.type = 'checkbox';
    archive.checked = record?.status === 'archived';
    archiveLabel.append(archive, document.createTextNode('Archive this record'));
    fields.append(archiveLabel);
    const error = node('p', '', 'pc-error');
    error.setAttribute('role', 'alert');
    const save = node('button', 'Save revision', 'pc-button primary');
    save.type = 'submit';
    form.append(fields, error, save);
    dialog.append(form);
    const draftFields = [title, summary, sources, properties, steps, checks];
    const draftPrefix = record ? `record-${record.id}-${record.version}` : 'record-new';
    draftFields.forEach((input) => {
      const draftStatus = node('div', undefined, 'pc-draft-status');
      input.after(draftStatus);
      const binding = window.SimonProjectDrafts?.bind(input, {
        projectId: state.projectId,
        key: draftPrefix + '-' + input.name,
        statusTarget: draftStatus,
        defaultText: input.value,
      });
      if (binding) draftBindings.push(binding);
    });
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (state.busy) return;
      state.busy = true;
      fields.disabled = save.disabled = true;
      error.textContent = '';
      try {
        const values = properties.value === originalProperties ? { ...record?.fields } : {};
        for (const line of (properties.value === originalProperties
          ? []
          : properties.value.split('\n')
        ).filter((line) => line.trim())) {
          const colon = line.indexOf(':');
          if (colon < 1) throw new Error('Each detail needs a name followed by a colon and value.');
          const name = line.slice(0, colon).trim();
          if (Object.hasOwn(values, name)) throw new Error('Detail names must be unique.');
          Object.defineProperty(values, name, {
            value: line.slice(colon + 1).trim(),
            enumerable: true,
          });
        }
        const references = (record?.sources || []).filter((source) => source.activity_id);
        sources.value
          .split('\n')
          .filter((line) => line.trim())
          .forEach((line) => {
            const url = line.trim();
            const existing = record?.sources.find((source) => source.url === url);
            references.push(existing || { label: url.slice(0, 240), url });
          });
        const payload = {
          kind: type.value,
          title: title.value,
          summary: summary.value,
          fields: values,
          sources: references,
          confidence: confidence.value,
          status: archive.checked ? 'archived' : 'active',
          checked_at: record?.checked_at || null,
          review_after:
            review.value === record?.review_after?.slice(0, 10)
              ? record.review_after
              : review.value
                ? review.value + 'T23:59:59Z'
                : null,
          steps:
            type.value === 'procedure'
              ? steps.value === record?.steps?.join('\n')
                ? record.steps
                : steps.value.split('\n').filter((line) => line.trim())
              : [],
          success_checks:
            type.value === 'procedure'
              ? checks.value === record?.success_checks?.join('\n')
                ? record.success_checks
                : checks.value.split('\n').filter((line) => line.trim())
              : [],
          expected_version: record?.version || 0,
        };
        const fingerprint = JSON.stringify(payload);
        if (state.fingerprint && state.fingerprint !== fingerprint) state.key = crypto.randomUUID();
        state.fingerprint = fingerprint;
        const submittedDrafts = draftFields.map((input) => [input.name, input.value]);
        await api(
          path('/records/' + state.id, state.projectId),
          { ...payload, idempotency_key: state.key },
          'PUT',
        );
        submittedDrafts.forEach(([name, text]) =>
          window.SimonProjectDrafts?.clearAccepted(state.projectId, draftPrefix + '-' + name, text),
        );
        dialog.close();
        if (state.epoch === epoch) await load(false);
      } catch (problem) {
        error.textContent =
          problem.message +
          ' Your edits remain here. If another revision was saved, open the latest record before applying changes.';
      } finally {
        state.busy = false;
        fields.disabled = save.disabled = false;
      }
    };
    if (!dialog.open) dialog.showModal();
    title.focus();
  }
  dialog.addEventListener('cancel', (event) => {
    if (editorState?.busy) event.preventDefault();
  });
  dialog.addEventListener('close', detachDrafts);
  search.onsubmit = (event) => {
    event.preventDefault();
    load(false);
  };
  query.oninput = () => {
    more.hidden = true;
  };
  kind.onchange = () => load(false);
  archived.onchange = () => load(false);
  function mount(target, id, snapshot) {
    select(id);
    create.hidden = !canWrite();
    if (panel.parentElement !== target) target.replaceChildren(panel);
    const count = snapshot?.state?.activity_count;
    if (activityCount !== count || (!items.length && !loading && !status.textContent)) {
      activityCount = count;
      load(false);
    }
  }
  window.SimonProjectWorkspace = { mount, refresh: () => load(false) };
  window.addEventListener('simon-project-changing', (event) => select(event.detail.projectId));
})();
