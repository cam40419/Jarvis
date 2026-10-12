'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = () => window.SimonNativeProjects;
  const dialog = $('nd-dialog');
  let generation = 0,
    viewGeneration = 0,
    previewGeneration = 0,
    projectId = null,
    workspaceId = null,
    snapshot = null,
    detail = null,
    editing = null,
    pending = null,
    busy = false,
    uncertain = false,
    dirty = false,
    conflict = false,
    packets = [],
    candidates = [],
    selectedFiles = new Map(),
    artifactOffset = 0,
    packetMore = false,
    artifactMore = false;
  const path = (suffix = '') => '/v2/projects/' + projectId + '/delivery-packets' + suffix;
  const active = (ticket) =>
    ticket === generation &&
    dialog.open &&
    bridge().getProject()?.id === projectId &&
    bridge().getWorkspaceId() === workspaceId;
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const button = (label, callback) => {
    const item = node('button', label, 'np-button np-quiet');
    item.type = 'button';
    item.onclick = callback;
    return item;
  };
  const date = (value) => (value ? new Date(value).toLocaleString() : 'Not recorded');
  const available = (record) => record && record.disclosure !== 'withheld';
  const feedback = (message = '', tone = '') => {
    $('nd-feedback').textContent = message;
    $('nd-feedback').dataset.tone = tone;
  };
  const selections = () =>
    [...$('nd-candidates').querySelectorAll('input:checked')].map((input) => ({
      artifact: candidates.find((item) => item.id === input.value),
      role: $('nd-role-' + input.value).value,
    }));
  const validSelection = () => {
    const selected = selections();
    return (
      selected.length > 0 &&
      selected.length <= 20 &&
      selectedMap().size === selected.length &&
      selected.some((item) => item.role === 'source')
    );
  };
  const hasSuccessor = (id) => {
    const record =
      detail?.packet.id === id ? detail : packets.find((item) => item.packet.id === id);
    return (
      Boolean(record?.successor_id) || packets.some((item) => item.packet.previous_packet_id === id)
    );
  };
  const hasSuppliedPreview = (record) =>
    (record?.packet.manifest.components || []).some((component) => component.role === 'preview');
  const previewEditingReason =
    'This packet includes preview files linked to source revisions. Editing those links is unavailable in this composer. You can inspect and download the saved packet.';
  const statusText = (applicability) =>
    (applicability.current
      ? 'Current files: matches the project. '
      : 'Current files: this snapshot differs from the project. ') +
    (applicability.components_accepted
      ? 'Component acceptance: accepted.'
      : 'Component acceptance: pending for one or more files.');

  function clearPreview() {
    previewGeneration++;
    $('nd-source-preview').hidden = true;
    $('nd-source-heading').textContent = '';
    $('nd-source-text').textContent = '';
  }

  function lock() {
    const managed = Boolean(snapshot?.can_manage && (!detail || detail.can_manage));
    for (const control of dialog.querySelectorAll('input, textarea, select, button'))
      control.disabled = busy || uncertain;
    for (const control of dialog.querySelectorAll('[data-manage="true"]'))
      control.disabled ||= !managed;
    for (const control of $('nd-form').querySelectorAll('input, textarea, select'))
      control.disabled ||= !managed;
    for (const input of $('nd-candidates').querySelectorAll('input')) {
      input.disabled ||= !candidates.find((item) => item.id === input.value)?.current_revision;
      $('nd-role-' + input.value).disabled ||= !input.checked;
    }
    $('nd-save').disabled ||= !managed || conflict || !validSelection();
    $('nd-check').disabled = busy || !uncertain;
    $('nd-retry').disabled = busy || !uncertain;
    $('nd-close').disabled = busy || uncertain;
    $('nd-refresh').disabled = busy || Boolean(editing);
    $('nd-reload').disabled = busy || uncertain || !conflict;
    $('nd-download').disabled ||= !available(detail);
    $('nd-revise').disabled ||=
      !available(detail) || hasSuccessor(detail.packet.id) || hasSuppliedPreview(detail);
    $('nd-create').disabled ||= !artifactMore && !candidates.some((item) => item.current_revision);
    $('nd-more').hidden = !packetMore;
    $('nd-artifacts-more').hidden = !artifactMore;
    $('nd-uncertain').hidden = !uncertain;
    $('nd-conflict').hidden = !conflict;
    $('nd-form').setAttribute('aria-busy', String(busy));
    dialog.setAttribute('aria-busy', String(busy));
  }

  function renderPackets() {
    $('nd-access').textContent = snapshot.can_manage
      ? 'Project owners create packet versions. Project members can inspect and download available packets.'
      : 'You can inspect and download available packets. Project owners create packet versions.';
    $('nd-packets').replaceChildren(
      ...(packets.length
        ? packets.map((record) => {
            const packet = record.packet;
            const card = node('li', undefined, 'ni-card');
            card.dataset.packetId = packet.id;
            card.append(
              node('h4', packet.manifest.title),
              node(
                'p',
                'Version ' + packet.sequence + ' · ' + date(packet.created_at),
                'ni-run-meta',
              ),
            );
            if (available(record))
              card.append(
                node('p', statusText(record.applicability)),
                button('Inspect packet', () => openDetail(packet.id)),
              );
            else card.append(node('p', record.applicability.reasons.join(' '), 'np-muted'));
            return card;
          })
        : [
            node(
              'li',
              'No delivery packets yet. Select project outputs to collect the first handoff.',
              'np-empty',
            ),
          ]),
    );
    lock();
  }

  function overview() {
    viewGeneration++;
    clearPreview();
    detail = editing = null;
    dirty = conflict = false;
    $('nd-overview').hidden = false;
    $('nd-detail').hidden = true;
    $('nd-form').hidden = true;
    $('nd-back').hidden = true;
    $('nd-cancel').hidden = true;
    $('nd-save').hidden = true;
    $('nd-close').hidden = false;
    lock();
  }

  function renderDetail() {
    viewGeneration++;
    clearPreview();
    editing = null;
    dirty = conflict = false;
    $('nd-overview').hidden = true;
    $('nd-detail').hidden = false;
    $('nd-form').hidden = true;
    $('nd-back').hidden = false;
    $('nd-cancel').hidden = true;
    $('nd-save').hidden = true;
    $('nd-close').hidden = false;
    const packet = detail.packet,
      manifest = packet.manifest;
    $('nd-detail-heading').textContent = manifest.title;
    $('nd-detail-summary').textContent =
      'Packet version ' + packet.sequence + ' · Saved ' + date(packet.created_at);
    $('nd-applicability').textContent = statusText(detail.applicability);
    $('nd-applicability').dataset.current = String(detail.applicability.current);
    $('nd-applicability').dataset.accepted = String(detail.applicability.components_accepted);
    $('nd-reasons').replaceChildren(
      ...detail.applicability.reasons.map((reason) => node('li', reason)),
    );
    $('nd-components').replaceChildren(
      ...manifest.components.map((component) => {
        const card = node('li', undefined, 'ni-card');
        card.dataset.componentId = component.revision_id;
        card.append(
          node('h4', component.filename),
          node(
            'p',
            component.role + ' · ' + component.media_type + ' · ' + component.size_bytes + ' bytes',
            'np-muted',
          ),
          node(
            'p',
            'At packet creation: ' + component.status_at_creation.replaceAll('_', ' '),
            'np-field-help',
          ),
          node(
            'p',
            'Saved checks: ' +
              component.checks.length +
              ' · Decisions: ' +
              component.decisions.length +
              ' · Reviews: ' +
              component.review_ids.length,
            'np-field-help',
          ),
        );
        return card;
      }),
    );
    $('nd-previews').replaceChildren(
      ...(manifest.previews.length
        ? manifest.previews.map((preview) => {
            const source = manifest.components.find(
              (item) => item.revision_id === preview.revision_id,
            );
            const card = node('li', undefined, 'ni-card');
            card.append(
              node('h4', source?.filename || 'Source preview'),
              node(
                'p',
                preview.supported
                  ? 'A safe text preview is included in the ZIP.'
                  : 'This file has no supported text preview.',
              ),
              node(
                'p',
                preview.limitation ||
                  'Inspect the original file for its complete content and formatting.',
                'np-field-help',
              ),
            );
            return card;
          })
        : [node('li', 'No generated previews are recorded.', 'np-empty')]),
    );
    const limitations = [...new Set([...manifest.limitations, ...(detail.limitations || [])])];
    $('nd-limitations').replaceChildren(...limitations.map((value) => node('li', value)));
    $('nd-manifest-hash').textContent = 'Manifest SHA-256: ' + packet.manifest_sha256;
    $('nd-version-reason').textContent = hasSuppliedPreview(detail) ? previewEditingReason : '';
    lock();
  }

  function selectedMap() {
    for (const input of $('nd-candidates').querySelectorAll('input')) {
      if (input.checked) selectedFiles.set(input.value, $('nd-role-' + input.value).value);
      else selectedFiles.delete(input.value);
    }
    return new Map(selectedFiles);
  }

  function renderCandidates(selected = new Map()) {
    selectedFiles = new Map(selected);
    if (!artifactMore) {
      const availableIds = new Set(candidates.map((item) => item.id));
      for (const id of selectedFiles.keys()) {
        if (!availableIds.has(id)) selectedFiles.delete(id);
      }
    }
    $('nd-candidates').replaceChildren(
      ...(candidates.length
        ? candidates.map((artifact) => {
            const revision = artifact.current_revision;
            const card = node('li', undefined, 'ni-card');
            card.dataset.artifactId = artifact.id;
            const heading = node('div', undefined, 'nd-file-heading nt-checkbox');
            const pick = node('input');
            pick.type = 'checkbox';
            pick.value = artifact.id;
            pick.id = 'nd-pick-' + artifact.id;
            pick.checked = selectedFiles.has(artifact.id) && Boolean(revision);
            const label = node('label', revision?.filename || artifact.name);
            label.htmlFor = pick.id;
            heading.append(pick, label);
            card.append(
              heading,
              node(
                'p',
                revision
                  ? 'Version ' +
                      revision.sequence +
                      ' · ' +
                      artifact.status.replaceAll('_', ' ') +
                      ' · ' +
                      revision.media_type +
                      ' · ' +
                      revision.size_bytes +
                      ' bytes'
                  : 'This artifact has no saved output version.',
                'np-field-help',
              ),
            );
            const field = node('div', undefined, 'np-field');
            const roleLabel = node('label', 'Include as');
            roleLabel.htmlFor = 'nd-role-' + artifact.id;
            const role = node('select');
            role.id = roleLabel.htmlFor;
            for (const value of ['source', 'evidence', 'attachment']) {
              const option = node('option', value[0].toUpperCase() + value.slice(1));
              option.value = value;
              role.append(option);
            }
            role.value = selectedFiles.get(artifact.id) || 'source';
            pick.disabled = !revision;
            field.append(roleLabel, role);
            card.append(field);
            if (revision) card.append(button('Inspect text', () => previewSource(artifact)));
            return card;
          })
        : [
            node(
              'li',
              artifactMore
                ? 'No available files on this page. Load more project files to continue.'
                : 'No saved output files are available. Complete a task or upload an artifact first.',
              'np-empty',
            ),
          ]),
    );
    selectionSummary();
  }

  function selectionSummary() {
    const selected = selections(),
      unloaded = selectedMap().size - selected.length;
    $('nd-selection-reason').textContent = unloaded
      ? unloaded +
        ' previously selected files are on later pages. Load more files before creating this packet.'
      : selected.length > 20
        ? 'Select at most 20 files.'
        : !selected.some((item) => item.role === 'source')
          ? 'Select at least one file as a source.'
          : selected.length +
            ' file' +
            (selected.length === 1 ? '' : 's') +
            ' selected. Review the exact versions and limitations before creating the packet.';
    lock();
  }

  async function previewSource(artifact) {
    if (busy || uncertain) return;
    clearPreview();
    const ticket = generation,
      viewTicket = viewGeneration,
      previewTicket = previewGeneration;
    const current = () =>
      active(ticket) && viewTicket === viewGeneration && previewTicket === previewGeneration;
    $('nd-source-preview').hidden = false;
    $('nd-source-heading').textContent = artifact.current_revision.filename;
    $('nd-source-text').textContent = 'Loading preview…';
    try {
      const result = await bridge().request(
        '/v2/projects/' +
          projectId +
          '/artifacts/' +
          artifact.id +
          '/revisions/' +
          artifact.current_revision.id +
          '/preview',
      );
      if (current())
        $('nd-source-text').textContent =
          typeof result.text === 'string'
            ? result.text
            : 'A text preview is unavailable. The packet includes the original file and its stated preview limitations.';
    } catch (error) {
      if (current()) {
        if ([401, 403, 404].includes(error.status)) failure(error);
        else $('nd-source-text').textContent = 'Preview unavailable. ' + error.message;
      }
    }
  }

  function editor(previous = null) {
    if (
      busy ||
      uncertain ||
      !snapshot?.can_manage ||
      (previous && (!previous.can_manage || hasSuppliedPreview(previous)))
    )
      return;
    viewGeneration++;
    clearPreview();
    editing = { previous };
    dirty = conflict = false;
    $('nd-overview').hidden = true;
    $('nd-detail').hidden = true;
    $('nd-form').hidden = false;
    $('nd-back').hidden = true;
    $('nd-cancel').hidden = false;
    $('nd-save').hidden = false;
    $('nd-close').hidden = true;
    $('nd-title').value = previous?.packet.manifest.title || '';
    $('nd-notes').value = (previous?.packet.manifest.declared_limitations || []).join('\n');
    editorContext();
    renderCandidates(
      new Map(
        (previous?.packet.manifest.components || []).map((item) => [item.artifact_id, item.role]),
      ),
    );
    feedback();
    $('nd-title').focus();
  }

  function editorContext() {
    const previous = editing.previous?.packet;
    $('nd-editor-heading').textContent = previous
      ? 'Create a new packet version'
      : 'Create a delivery packet';
    $('nd-save').textContent = previous ? 'Create new version' : 'Create packet';
    $('nd-editor-context').textContent = previous
      ? 'Version ' +
        (previous.sequence + 1) +
        ' follows “' +
        previous.manifest.title +
        '”. Select the current files for this new snapshot; the saved earlier packet stays available.'
      : 'The ZIP contains original files, a manifest, safe text previews and a separate status report. Review its limitations before handing it off.';
  }

  async function refresh() {
    const ticket = generation,
      id = detail?.packet.id;
    const data = await bridge().request(
      path('?offset=0&limit=50&artifact_offset=0&artifact_limit=50'),
    );
    if (!active(ticket)) return;
    snapshot = data;
    packets = data.packets.items;
    packetMore = data.packets.has_more;
    candidates = data.artifacts.items;
    artifactMore = data.artifacts.has_more;
    artifactOffset = data.artifacts.offset + data.artifacts.limit;
    renderPackets();
    if (id && !editing) {
      const record = await bridge().request(path('/' + id));
      if (!active(ticket)) return;
      detail = record;
      renderDetail();
    }
  }

  async function openDetail(id) {
    if (busy || uncertain) return;
    const ticket = generation,
      viewTicket = ++viewGeneration;
    busy = true;
    lock();
    feedback('Loading packet…');
    try {
      const record = await bridge().request(path('/' + id));
      if (!active(ticket) || viewTicket !== viewGeneration) return;
      detail = record;
      renderDetail();
      feedback();
      $('nd-detail-heading').focus();
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  async function afterSuccess(operation, result) {
    const ticket = generation;
    const id = result.packet?.id || result.packet_id;
    overview();
    await refresh();
    if (!active(ticket)) return;
    if (id) {
      detail = await bridge().request(path('/' + id));
      if (!active(ticket)) return;
      renderDetail();
    }
    feedback(
      operation.previous
        ? 'Saved a new immutable packet version.'
        : 'Saved the delivery packet. Inspect its current status and limitations before handing it off.',
      'success',
    );
  }

  async function perform() {
    if (busy || !pending) return;
    const ticket = generation,
      operation = pending;
    busy = true;
    lock();
    feedback('Saving packet…');
    try {
      const result = await bridge().request(operation.url, operation.body, 'POST');
      if (!active(ticket)) return;
      pending = null;
      uncertain = false;
      try {
        await afterSuccess(operation, result);
      } catch (error) {
        if (active(ticket) && [401, 403, 404].includes(error.status)) failure(error);
        else if (active(ticket))
          feedback(
            'The packet was saved. Refresh status to reload it. ' + error.message,
            'warning',
          );
      }
    } catch (error) {
      if (!active(ticket)) return;
      if (!error.status || error.status >= 500) {
        uncertain = true;
        feedback(
          'The response could not be confirmed. Resolve this request before creating another packet.',
          'warning',
        );
      } else {
        pending = null;
        uncertain = false;
        if ([401, 403, 404].includes(error.status)) {
          failure(error);
          return;
        }
        conflict = error.status === 409;
        feedback(error.message + ' Your title, notes and selection are preserved.', 'error');
      }
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  async function checkOperation() {
    if (busy || !pending || !uncertain) return;
    const ticket = generation,
      operation = pending;
    busy = true;
    lock();
    try {
      const receipt = await bridge().request(
        path('/operations/' + encodeURIComponent(operation.body.idempotency_key)),
      );
      if (!active(ticket)) return;
      pending = null;
      uncertain = false;
      await afterSuccess(operation, receipt);
    } catch (error) {
      if (!active(ticket)) return;
      if (error.status === 404) {
        try {
          const current = await bridge().request(path());
          if (!active(ticket)) return;
          if (!current.can_manage) {
            reset();
            return;
          }
          feedback(
            'No saved receipt was found. Retry the same request; its original key and file versions are preserved.',
            'warning',
          );
        } catch (accessError) {
          if (active(ticket)) failure(accessError);
        }
      } else failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  async function reloadConflict() {
    if (busy || uncertain || !editing || !conflict) return;
    const ticket = generation,
      selected = selectedMap();
    busy = true;
    lock();
    try {
      const data = await bridge().request(
        path('?offset=0&limit=50&artifact_offset=0&artifact_limit=100'),
      );
      if (!active(ticket)) return;
      if (!data.can_manage) {
        reset();
        return;
      }
      snapshot = data;
      packets = data.packets.items;
      packetMore = data.packets.has_more;
      candidates = data.artifacts.items;
      artifactMore = data.artifacts.has_more;
      artifactOffset = data.artifacts.offset + data.artifacts.limit;
      if (editing.previous) {
        const seen = new Set();
        let current = await bridge().request(path('/' + editing.previous.packet.id));
        if (!active(ticket)) return;
        if (!available(current) || !current.can_manage) {
          reset();
          return;
        }
        while (current.successor_id) {
          if (seen.has(current.packet.id) || seen.size >= 100)
            throw Error('The packet history could not be resolved. Refresh before continuing.');
          seen.add(current.packet.id);
          current = await bridge().request(path('/' + current.successor_id));
          if (!active(ticket)) return;
          if (!available(current) || !current.can_manage) {
            reset();
            return;
          }
        }
        if (hasSuppliedPreview(current)) {
          detail = current;
          feedback(previewEditingReason + ' Cancel this draft to inspect that version.', 'warning');
          return;
        }
        editing.previous = current;
      }
      conflict = false;
      clearPreview();
      editorContext();
      renderCandidates(selected);
      feedback(
        'Your title, notes and roles are preserved. Review the current file versions before creating this packet.',
      );
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  async function loadMore(kind) {
    if (busy || uncertain) return;
    const ticket = generation,
      existing = kind === 'packets' ? packets : candidates;
    const selected = kind === 'artifacts' ? selectedMap() : null;
    busy = true;
    lock();
    try {
      const data = await bridge().request(
        path(
          kind === 'packets'
            ? '?offset=' + existing.length + '&limit=50&artifact_limit=1'
            : '?limit=1&artifact_offset=' + artifactOffset + '&artifact_limit=50',
        ),
      );
      if (!active(ticket)) return;
      const result = data[kind],
        known = new Set(existing.map((item) => (kind === 'packets' ? item.packet.id : item.id)));
      if (result.items.some((item) => known.has(kind === 'packets' ? item.packet.id : item.id)))
        throw Error(
          'The list changed while loading. Reload the current files or refresh packet status before continuing.',
        );
      existing.push(...result.items);
      if (kind === 'packets') {
        packetMore = result.has_more;
        renderPackets();
      } else {
        artifactMore = result.has_more;
        artifactOffset = result.offset + result.limit;
        renderCandidates(selected);
      }
    } catch (error) {
      if (active(ticket)) {
        if (kind === 'artifacts') conflict = true;
        failure(error);
      }
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  async function download() {
    if (busy || uncertain || !available(detail)) return;
    const ticket = generation,
      viewTicket = viewGeneration,
      packetId = detail.packet.id;
    busy = true;
    lock();
    try {
      const blob = await bridge().download(path('/' + packetId + '/download'));
      if (!active(ticket) || viewTicket !== viewGeneration) return;
      const url = URL.createObjectURL(blob),
        link = node('a');
      link.href = url;
      link.download = 'delivery-packet-' + packetId + '.zip';
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      feedback(
        'Downloaded the saved packet with its original files and separate current-status report.',
      );
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  function failure(error) {
    if ([401, 403, 404].includes(error.status)) reset();
    else feedback(error.message, 'error');
  }

  function reset() {
    generation++;
    viewGeneration++;
    clearPreview();
    projectId = workspaceId = snapshot = detail = editing = pending = null;
    busy = uncertain = dirty = conflict = false;
    packets = [];
    candidates = [];
    selectedFiles = new Map();
    artifactOffset = 0;
    packetMore = artifactMore = false;
    $('nd-form').reset();
    for (const id of [
      'nd-project',
      'nd-access',
      'nd-packets',
      'nd-detail-heading',
      'nd-detail-summary',
      'nd-applicability',
      'nd-reasons',
      'nd-components',
      'nd-previews',
      'nd-limitations',
      'nd-manifest-hash',
      'nd-version-reason',
      'nd-editor-context',
      'nd-candidates',
      'nd-selection-reason',
    ])
      $(id).replaceChildren();
    feedback();
    if (dialog.open) dialog.close();
  }

  async function open() {
    const project = bridge().getProject();
    if (!project || dialog.open) return;
    reset();
    projectId = project.id;
    workspaceId = bridge().getWorkspaceId();
    const ticket = ++generation;
    $('nd-project').textContent = project.name;
    dialog.showModal();
    overview();
    busy = true;
    lock();
    $('nd-heading').focus();
    feedback('Loading delivery packets…');
    try {
      await refresh();
      if (active(ticket)) feedback();
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  function close() {
    if (busy || uncertain) return;
    reset();
  }

  $('nd-form').addEventListener('input', () => {
    dirty = true;
    selectionSummary();
  });
  $('nd-form').addEventListener('change', selectionSummary);
  $('nd-form').onsubmit = async (event) => {
    event.preventDefault();
    if (
      busy ||
      uncertain ||
      conflict ||
      !editing ||
      !snapshot?.can_manage ||
      !validSelection() ||
      !event.currentTarget.reportValidity()
    )
      return;
    const limitations = $('nd-notes')
      .value.split('\n')
      .map((value) => value.trim())
      .filter(Boolean);
    if (limitations.length > 20 || limitations.some((value) => value.length > 1000)) {
      feedback('Use at most 20 limitation lines, with at most 1,000 characters per line.', 'error');
      return;
    }
    const components = selections().map(({ artifact, role }) => ({
      artifact_id: artifact.id,
      revision_id: artifact.current_revision.id,
      sha256: artifact.current_revision.sha256,
      role,
      derived_from_revision_id: null,
    }));
    const previous = editing.previous?.packet.id || null;
    pending = {
      url: path(),
      previous,
      body: {
        title: $('nd-title').value,
        components,
        limitations,
        previous_packet_id: previous,
        idempotency_key: crypto.randomUUID(),
      },
    };
    await perform();
  };
  $('np-delivery').onclick = open;
  $('nd-close').onclick = close;
  $('nd-create').onclick = () => editor();
  $('nd-revise').onclick = () => editor(detail);
  $('nd-download').onclick = download;
  $('nd-retry').onclick = perform;
  $('nd-check').onclick = checkOperation;
  $('nd-reload').onclick = reloadConflict;
  $('nd-more').onclick = () => loadMore('packets');
  $('nd-artifacts-more').onclick = () => loadMore('artifacts');
  $('nd-back').onclick = () => {
    if (!busy && !uncertain) {
      overview();
      feedback();
    }
  };
  $('nd-cancel').onclick = () => {
    if (!busy && !uncertain) {
      if (detail) renderDetail();
      else overview();
      feedback();
    }
  };
  $('nd-refresh').onclick = async () => {
    if (busy || editing) return;
    const ticket = generation;
    busy = true;
    lock();
    try {
      await refresh();
      if (active(ticket)) feedback('Status refreshed.');
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  };
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    if (!editing) close();
  });
  window.addEventListener('beforeunload', (event) => {
    if (busy || uncertain || dirty) event.preventDefault();
  });
  window.addEventListener('native-project-loading', (event) => {
    if (projectId && event.detail.id !== projectId) reset();
  });
  window.addEventListener('native-project-unavailable', reset);
  window.SimonNativeDelivery = { open };
})();
