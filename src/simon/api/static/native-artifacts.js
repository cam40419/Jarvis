'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = () => window.SimonNativeProjects;
  const dialog = $('na-dialog');
  const forms = ['policy', 'upload', 'review', 'decision'];
  const imageTypes = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
  const textTypes = new Map([
    ['txt', 'text/plain'],
    ['md', 'text/markdown'],
    ['markdown', 'text/markdown'],
    ['json', 'application/json'],
    ['csv', 'text/csv'],
    ['xml', 'application/xml'],
    ['html', 'text/html'],
    ['js', 'text/javascript'],
    ['ts', 'text/plain'],
    ['py', 'text/x-python'],
  ]);
  const fileType = (file) =>
    file.type ||
    textTypes.get(file.name.split('.').pop().toLowerCase()) ||
    'application/octet-stream';
  let generation = 0,
    viewGeneration = 0,
    previewGeneration = 0,
    projectId = null,
    workspaceId = null,
    snapshot = null,
    detail = null,
    editing = null,
    pending = null,
    latest = null,
    busy = false,
    uncertain = false,
    dirty = false,
    imageUrl = null,
    artifacts = [],
    more = false;
  const active = (ticket) =>
    ticket === generation &&
    dialog.open &&
    bridge().getProject()?.id === projectId &&
    bridge().getWorkspaceId() === workspaceId;
  const path = (suffix = '') => '/v2/projects/' + projectId + '/artifacts' + suffix;
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const date = (value) => (value ? new Date(value).toLocaleString() : 'Not recorded');
  const money = (value) =>
    new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: 'USD',
      maximumFractionDigits: 6,
    }).format((value || 0) / 1000000);
  const feedback = (message = '', tone = '') => {
    $('na-feedback').textContent = message;
    $('na-feedback').dataset.tone = tone;
  };
  const revision = () => detail?.revisions.find((item) => item.id === $('na-revision').value);
  const isCurrent = () => Boolean(detail && revision()?.id === detail.artifact.current_revision_id);
  const reviewable = () =>
    isCurrent() &&
    !detail.artifact.pending_repair_task_id &&
    detail.task.status !== 'cancelled' &&
    detail.status !== 'accepted';
  const taskName = (id) => snapshot?.tasks.find((item) => item.id === id)?.title || 'Task ' + id;
  const uploadTasks = () =>
    (snapshot?.tasks || []).filter(
      (task) =>
        task.status !== 'cancelled' &&
        !task.artifact_id &&
        !artifacts.some(
          (artifact) => artifact.task_id === task.id || artifact.pending_repair_task_id === task.id,
        ),
    );
  const agentName = (id) => snapshot?.agents.find((item) => item.id === id)?.name || 'Role ' + id;
  const currentReviews = () =>
    (detail?.reviews || []).filter(
      (item) => item.revision_id === revision()?.id && item.sha256 === revision()?.sha256,
    );
  const currentChecks = () =>
    (detail?.checks || []).filter(
      (item) => item.revision_id === revision()?.id && item.sha256 === revision()?.sha256,
    );
  const canAccept = () => {
    if (!detail || !reviewable() || !detail.can_manage || detail.disclosure_allowed === false)
      return false;
    if (!currentChecks().length || currentChecks().some((item) => item.status !== 'passed'))
      return false;
    return (
      !detail.policy.require_agent_review ||
      currentReviews().some(
        (item) =>
          item.status === 'completed' &&
          item.verdict === 'approved' &&
          item.policy_version === detail.policy.version &&
          item.task_version === detail.task.version &&
          item.artifact_version === detail.artifact.version,
      )
    );
  };

  function clearPreview() {
    previewGeneration++;
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null;
    $('na-preview').replaceChildren();
  }

  function encodedBytes(buffer) {
    const bytes = new Uint8Array(buffer);
    let raw = '';
    for (let offset = 0; offset < bytes.length; offset += 8192)
      raw += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
    return btoa(raw);
  }

  function lock() {
    const managed = Boolean(snapshot?.can_manage && (!detail || detail.can_manage));
    for (const control of dialog.querySelectorAll('input, textarea, select, button'))
      control.disabled = busy || uncertain;
    for (const control of dialog.querySelectorAll('[data-manage="true"]'))
      control.disabled ||= !managed;
    for (const kind of forms) {
      const form = $('na-' + kind + '-form');
      form.setAttribute('aria-busy', String(busy));
      for (const control of form.querySelectorAll('input, textarea, select'))
        control.disabled ||= !managed;
    }
    $('na-refresh').disabled = busy || dirty;
    $('na-check').disabled = busy || !uncertain;
    $('na-retry').disabled = busy || !uncertain;
    $('na-close').disabled = busy || uncertain;
    $('na-uncertain').hidden = !uncertain;
    $('na-save').disabled ||= !managed || Boolean(latest);
    $('na-rebase').disabled = busy || uncertain || !latest;
    $('na-upload-new').disabled ||= !uploadTasks().length;
    $('na-revision-new').disabled ||= !isCurrent();
    $('na-review-new').disabled ||=
      !reviewable() ||
      detail?.disclosure_allowed === false ||
      currentReviews().some((item) => item.status === 'running');
    $('na-changes').disabled ||= !reviewable();
    $('na-accept').disabled ||= !canAccept();
    $('na-download').disabled ||= !revision() || detail?.disclosure_allowed === false;
    $('na-more').hidden = !more;
    dialog.setAttribute('aria-busy', String(busy));
  }

  function overview() {
    viewGeneration++;
    clearPreview();
    detail = editing = latest = null;
    dirty = false;
    $('na-overview').hidden = false;
    $('na-detail').hidden = true;
    $('na-editor').hidden = true;
    $('na-conflict').hidden = true;
    $('na-back').hidden = true;
    $('na-editor-cancel').hidden = true;
    $('na-save').hidden = true;
    $('na-close').hidden = false;
    lock();
  }

  function renderOverview() {
    const policy = snapshot.policy;
    $('na-policy-summary').textContent =
      (policy.require_agent_review
        ? 'Independent agent review required. '
        : 'Independent agent review optional. ') +
      (policy.require_human_approval
        ? 'Owner approval required. '
        : 'Automatic acceptance allowed after required checks. ') +
      'Up to ' +
      policy.max_repair_cycles +
      ' repair cycles.';
    $('na-access').textContent = snapshot.can_manage
      ? 'Project owners can upload outputs, request review and record decisions.'
      : 'You can inspect outputs and their review history. Project owners control decisions.';
    const cards = artifacts.map((artifact) => {
      const card = node('li', undefined, 'ni-card');
      card.dataset.artifactId = artifact.id;
      card.append(
        node('h4', artifact.name || artifact.filename || 'Artifact'),
        node('p', taskName(artifact.task_id), 'np-muted'),
        node(
          'p',
          (artifact.status || 'awaiting_review').replaceAll('_', ' ') +
            ' · Updated ' +
            date(artifact.updated_at),
          'ni-run-meta',
        ),
      );
      const button = node('button', 'Inspect artifact', 'np-button np-quiet');
      button.type = 'button';
      button.onclick = () => openDetail(artifact.id);
      card.append(button);
      return card;
    });
    $('na-artifacts').replaceChildren(
      ...(cards.length
        ? cards
        : [
            node(
              'li',
              'No artifacts yet. Completed execution candidates appear here automatically, or upload an output for a task.',
              'np-empty',
            ),
          ]),
    );
    lock();
  }

  function historyCards(items, empty, render) {
    return items.length
      ? items.map((item) => {
          const card = node('li', undefined, 'ni-card');
          render(card, item);
          return card;
        })
      : [node('li', empty, 'np-empty')];
  }

  function renderEvidence() {
    const selected = revision();
    if (!selected) return;
    const difference = detail.difference;
    $('na-difference-section').hidden = !isCurrent() || !difference?.from_revision_id;
    $('na-difference').textContent = difference?.supported ? difference.text : '';
    $('na-difference-note').textContent = !difference?.supported
      ? 'A text comparison is unavailable. Select each version to inspect its preview or download.'
      : difference.truncated
        ? 'This comparison is truncated. Inspect the complete versions before acceptance.'
        : '';
    $('na-revision-summary').textContent =
      selected.filename +
      ' · ' +
      selected.media_type +
      ' · ' +
      selected.size_bytes +
      ' bytes · SHA-256 ' +
      selected.sha256;
    $('na-checks').replaceChildren(
      ...historyCards(
        currentChecks(),
        'No validation checks recorded for this version.',
        (card, check) => {
          card.append(
            node('h4', check.check_name.replaceAll('_', ' ') + ': ' + check.status),
            node(
              'p',
              typeof check.details === 'string'
                ? check.details
                : check.details.message ||
                    (check.check_name === 'content_integrity'
                      ? check.details.size_bytes + ' bytes; saved content hash matches.'
                      : JSON.stringify(check.details)),
            ),
          );
        },
      ),
    );
    $('na-reviews').replaceChildren(
      ...historyCards(
        currentReviews(),
        'No independent review recorded for this version.',
        (card, review) => {
          card.dataset.reviewId = review.id;
          card.append(
            node('h4', agentName(review.reviewer_agent_id) + ' · ' + review.status),
            node('p', review.verdict?.replaceAll('_', ' ') || 'No verdict'),
            node('p', review.summary || 'Review outcome is pending.'),
            node(
              'p',
              'Policy version ' + review.policy_version + ' · ' + date(review.created_at),
              'ni-run-meta',
            ),
          );
          if (review.findings?.length) {
            const findings = node('ul');
            findings.append(
              ...review.findings.map((finding) =>
                node('li', typeof finding === 'string' ? finding : JSON.stringify(finding)),
              ),
            );
            card.append(findings);
          }
          if (review.source_authority)
            card.append(
              node(
                'p',
                'Source excerpt coverage: ' +
                  review.source_authority.coverage +
                  ' · ' +
                  review.source_authority.omitted_count +
                  ' sources omitted from the review text. Source changes are checked across the recorded inventory.',
                'np-field-help',
              ),
            );
          if (review.usage)
            card.append(
              node(
                'p',
                'Charged: ' +
                  money(review.usage.charged_microusd) +
                  ' · Held: ' +
                  money(review.usage.held_microusd) +
                  ' · ' +
                  review.usage.status +
                  ' · ' +
                  (review.usage.input_tokens ?? 'unreported') +
                  ' input / ' +
                  (review.usage.output_tokens ?? 'unreported') +
                  ' output tokens',
                'ni-run-meta',
              ),
            );
          if (review.error_code)
            card.append(node('p', review.error_code.replaceAll('_', ' '), 'np-field-help'));
          if (review.review_task_id)
            card.append(
              node('p', 'Review task: ' + taskName(review.review_task_id), 'np-field-help'),
            );
        },
      ),
    );
    const decisions = detail.decisions.filter(
      (item) => item.revision_id === selected.id && item.sha256 === selected.sha256,
    );
    $('na-decisions').replaceChildren(
      ...historyCards(
        decisions,
        'No owner decision recorded for this version.',
        (card, decision) => {
          card.append(
            node(
              'h4',
              decision.decision === 'approved'
                ? decision.automatic
                  ? 'Accepted by review policy'
                  : 'Accepted by owner'
                : 'Changes requested',
            ),
            node('p', decision.feedback || 'No additional feedback.'),
            node('p', date(decision.created_at), 'ni-run-meta'),
          );
          if (decision.repair_task_id)
            card.append(node('p', 'Linked repair task: ' + taskName(decision.repair_task_id)));
        },
      ),
    );
    $('na-decision-reason').textContent = !isCurrent()
      ? 'This is a historical version. Select the current version to request review or make a decision.'
      : detail.artifact.pending_repair_task_id
        ? 'The linked repair task needs a revised output before another review or decision.'
        : detail.status === 'accepted'
          ? 'This exact version is accepted. Uploading a new version requires fresh review.'
          : canAccept()
            ? 'Required checks and review are satisfied for this exact output version.'
            : 'Acceptance requires passing checks and any independent review required by the current policy.';
    lock();
  }

  async function preview() {
    clearPreview();
    const selected = revision();
    if (!selected) return;
    if (detail.disclosure_allowed === false) {
      $('na-preview').append(
        node(
          'p',
          'Source access was revoked. Retained content is withheld; request a repair using current permitted evidence.',
          'np-muted',
        ),
      );
      return;
    }
    const ticket = generation,
      viewTicket = viewGeneration,
      previewTicket = previewGeneration;
    const current = () =>
      active(ticket) && viewTicket === viewGeneration && previewTicket === previewGeneration;
    const suffix = '/' + detail.artifact.id + '/revisions/' + selected.id;
    $('na-preview').append(node('p', 'Loading preview…', 'np-muted'));
    try {
      if (imageTypes.has(selected.media_type)) {
        const blob = await bridge().download(path(suffix + '/content'));
        if (!current()) return;
        imageUrl = URL.createObjectURL(blob);
        const image = node('img');
        image.src = imageUrl;
        image.alt = selected.filename;
        $('na-preview').replaceChildren(image);
      } else {
        const result = await bridge().request(path(suffix + '/preview'));
        if (!current()) return;
        $('na-preview').replaceChildren(
          typeof result.text === 'string'
            ? node('pre', result.text)
            : node(
                'p',
                'A text preview is unavailable for this file. Download this version to inspect it.',
                'np-muted',
              ),
        );
      }
    } catch (error) {
      if (current()) {
        if ([401, 403, 404].includes(error.status)) failure(error);
        else
          $('na-preview').replaceChildren(
            node('p', 'Preview unavailable. ' + error.message, 'np-muted'),
          );
      }
    }
  }

  function renderDetail(selectedId = null) {
    viewGeneration++;
    editing = latest = null;
    dirty = false;
    $('na-overview').hidden = true;
    $('na-editor').hidden = true;
    $('na-detail').hidden = false;
    $('na-conflict').hidden = true;
    $('na-back').hidden = false;
    $('na-editor-cancel').hidden = true;
    $('na-save').hidden = true;
    $('na-close').hidden = false;
    $('na-detail-heading').textContent = detail.artifact.name;
    $('na-detail-summary').textContent =
      detail.task.title + ' · ' + detail.status.replaceAll('_', ' ');
    $('na-revision').replaceChildren(
      ...detail.revisions.map((item) => {
        const option = node(
          'option',
          'Version ' +
            item.sequence +
            (item.id === detail.artifact.current_revision_id ? ' (current)' : '') +
            ' · ' +
            date(item.created_at),
        );
        option.value = item.id;
        return option;
      }),
    );
    $('na-revision').value = detail.revisions.some((item) => item.id === selectedId)
      ? selectedId
      : detail.artifact.current_revision_id;
    renderEvidence();
    preview();
  }

  async function refresh() {
    const ticket = generation;
    const selectedId = revision()?.id;
    const artifactId = detail?.artifact.id;
    const data = await bridge().request(path('?offset=0&limit=50'));
    if (!active(ticket)) return;
    snapshot = data;
    artifacts = data.artifacts.items;
    more = data.artifacts.has_more;
    renderOverview();
    if (artifactId && !editing) {
      const record = await bridge().request(path('/' + artifactId));
      if (!active(ticket)) return;
      detail = record;
      renderDetail(selectedId);
    }
  }

  async function openDetail(id) {
    if (busy || uncertain) return;
    const ticket = generation,
      viewTicket = ++viewGeneration;
    busy = true;
    lock();
    feedback('Loading artifact…');
    try {
      const record = await bridge().request(path('/' + id));
      if (!active(ticket) || viewTicket !== viewGeneration) return;
      detail = record;
      renderDetail();
      $('na-detail-heading').focus();
      feedback();
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  function editor(kind, decision = null) {
    if (busy || uncertain || !snapshot?.can_manage || (detail && !detail.can_manage)) return false;
    clearPreview();
    viewGeneration++;
    editing = { kind, policy: snapshot.policy, detail, revision: revision(), decision };
    latest = null;
    dirty = false;
    $('na-overview').hidden = true;
    $('na-detail').hidden = true;
    $('na-editor').hidden = false;
    $('na-conflict').hidden = true;
    $('na-back').hidden = true;
    $('na-close').hidden = true;
    $('na-editor-cancel').hidden = false;
    $('na-save').hidden = false;
    for (const form of forms) $('na-' + form + '-form').hidden = form !== kind;
    $('na-save').setAttribute('form', 'na-' + kind + '-form');
    feedback();
    lock();
    return true;
  }

  function roleOptions(select, { optional = false, exclude = null } = {}) {
    const choices = snapshot.agents
      .filter((item) => item.status === 'active' && item.id !== exclude)
      .map((item) => {
        const option = node('option', item.name);
        option.value = item.id;
        return option;
      });
    if (optional) {
      const option = node('option', 'Choose a reviewer when requesting review');
      option.value = '';
      choices.unshift(option);
    }
    select.replaceChildren(...choices);
  }

  function editPolicy() {
    if (!editor('policy')) return;
    $('na-editor-heading').textContent = 'Review settings';
    $('na-save').textContent = 'Save review settings';
    $('na-human-required').checked = snapshot.policy.require_human_approval;
    $('na-agent-required').checked = snapshot.policy.require_agent_review;
    $('na-repair-limit').value = snapshot.policy.max_repair_cycles;
    roleOptions($('na-policy-reviewer'), { optional: true });
    $('na-policy-reviewer').value = snapshot.policy.reviewer_agent_id || '';
    $('na-human-required').focus();
  }

  function editUpload(revise = false) {
    if (!editor('upload')) return;
    editing.revise = revise;
    $('na-editor-heading').textContent = revise ? 'Upload revised output' : 'Upload artifact';
    $('na-save').textContent = revise ? 'Save new version' : 'Upload artifact';
    $('na-upload-form').reset();
    $('na-upload-metadata').hidden = revise;
    $('na-task').replaceChildren(
      ...uploadTasks().map((task) => {
        const option = node('option', task.title);
        option.value = task.id;
        return option;
      }),
    );
    $('na-task').required = !revise;
    $('na-upload-context').textContent = revise
      ? detail.artifact.name +
        ': the current version stays in history and the revised file needs new review.'
      : 'The filename identifies this artifact. Its output is linked to the selected task.';
    $('na-upload-limit').textContent =
      'Choose a file up to 1 MiB. Text and common raster images have safe previews; other files can be downloaded.';
    (revise ? $('na-file') : $('na-task')).focus();
  }

  function editReview() {
    if (!editor('review')) return;
    $('na-editor-heading').textContent = 'Request independent review';
    $('na-save').textContent = 'Request review';
    $('na-review-context').textContent =
      detail.artifact.name +
      ' · Version ' +
      editing.revision.sequence +
      ' · SHA-256 ' +
      editing.revision.sha256;
    roleOptions($('na-reviewer'), {
      exclude: editing.revision.created_by_agent_id || detail.task.assignment.agent_id,
    });
    if (snapshot.policy.reviewer_agent_id)
      $('na-reviewer').value = snapshot.policy.reviewer_agent_id;
    $('na-reviewer').focus();
  }

  function editDecision(decision) {
    if (decision === 'approved' && !canAccept()) return;
    if (!editor('decision', decision)) return;
    $('na-editor-heading').textContent =
      decision === 'approved' ? 'Accept this version' : 'Request changes';
    $('na-save').textContent =
      decision === 'approved' ? 'Confirm acceptance' : 'Create repair task';
    $('na-decision-context').textContent =
      detail.artifact.name +
      ' · Version ' +
      editing.revision.sequence +
      ' · SHA-256 ' +
      editing.revision.sha256;
    $('na-comment').value = '';
    $('na-comment').required = decision === 'changes_requested';
    $('na-comment-help').textContent =
      decision === 'approved'
        ? 'Optional acceptance notes. This decision applies only to the exact output version shown above.'
        : 'Describe the changes needed. A linked repair task records this feedback.';
    $('na-comment').focus();
  }

  function exactVersion(record) {
    return {
      expected_version: record.detail.artifact.version,
      expected_policy_version: record.detail.policy.version,
      expected_task_version: record.detail.task.version,
      revision_id: record.revision.id,
      sha256: record.revision.sha256,
    };
  }

  async function buildOperation() {
    const record = editing;
    if (
      record.kind === 'policy' &&
      !$('na-human-required').checked &&
      !$('na-agent-required').checked
    )
      throw Error('Require owner approval or independent agent review before accepting outputs.');
    if (record.kind === 'policy')
      return {
        suffix: '/policy',
        method: 'PUT',
        body: {
          expected_version: record.policy.version,
          require_human_approval: $('na-human-required').checked,
          require_agent_review: $('na-agent-required').checked,
          reviewer_agent_id: $('na-policy-reviewer').value || null,
          max_repair_cycles: Number($('na-repair-limit').value),
        },
      };
    if (record.kind === 'upload') {
      const file = $('na-file').files[0];
      if (!file || file.size > 1048576) throw Error('Choose a file up to 1 MiB.');
      const encoded = encodedBytes(await file.arrayBuffer());
      const task = record.revise
        ? record.detail.repair_task ||
          (record.detail.artifact.pending_repair_task_id
            ? snapshot.tasks.find(
                (item) => item.id === record.detail.artifact.pending_repair_task_id,
              )
            : record.detail.task)
        : snapshot.tasks.find((item) => item.id === $('na-task').value);
      if (!task) throw Error('Select a current project task.');
      return {
        suffix: '/upload',
        method: 'POST',
        body: {
          artifact_id: record.revise ? record.detail.artifact.id : null,
          task_id: task.id,
          expected_version: record.revise ? record.detail.artifact.version : 0,
          expected_task_version: task.version,
          filename: file.name,
          media_type: fileType(file),
          content_base64: encoded,
        },
      };
    }
    return {
      suffix:
        '/' + record.detail.artifact.id + (record.kind === 'review' ? '/reviews' : '/decisions'),
      method: 'POST',
      body: {
        ...exactVersion(record),
        ...(record.kind === 'review'
          ? { reviewer_agent_id: $('na-reviewer').value || null }
          : { decision: record.decision, feedback: $('na-comment').value }),
      },
    };
  }

  async function showConflict() {
    const ticket = generation;
    const record = editing;
    if (!record) return;
    const value = await bridge().request(
      path(record.kind === 'policy' || !record.detail ? '' : '/' + record.detail.artifact.id),
    );
    if (!active(ticket) || editing !== record) return;
    latest = value;
    $('na-latest').textContent = JSON.stringify(
      record.kind === 'policy'
        ? value.policy
        : record.detail
          ? {
              artifact: value.artifact,
              task: value.task,
              policy: value.policy,
            }
          : {
              task: value.tasks.find((item) => item.id === $('na-task').value),
              policy: value.policy,
            },
      null,
      2,
    );
    $('na-conflict').hidden = false;
    lock();
  }

  async function afterSuccess(operation, result) {
    const ticket = generation;
    const artifactId =
      result.artifact?.id ||
      operation.artifactId ||
      (operation.kind === 'upload' ? result.artifact_id : null);
    overview();
    await refresh();
    if (!active(ticket)) return;
    if (artifactId) {
      const record = await bridge().request(path('/' + artifactId));
      if (!active(ticket)) return;
      detail = record;
      renderDetail();
    }
    feedback(
      operation.kind === 'review'
        ? 'Review request recorded. Its outcome and any findings are shown below.'
        : operation.kind === 'decision' && operation.body.decision === 'changes_requested'
          ? 'Feedback recorded and linked repair work created. Close this panel to see it on the board.'
          : 'Saved. The current state is shown below.',
      'success',
    );
  }

  async function perform() {
    if (busy || !pending) return;
    const ticket = generation,
      operation = pending;
    busy = true;
    lock();
    feedback(operation.kind === 'review' ? 'Requesting independent review…' : 'Saving…');
    try {
      const result = await bridge().request(operation.url, operation.body, operation.method);
      if (!active(ticket)) return;
      pending = null;
      uncertain = false;
      try {
        await afterSuccess(operation, result);
      } catch (error) {
        if (active(ticket) && [401, 403, 404].includes(error.status)) failure(error);
        else if (active(ticket))
          feedback(
            'The request was saved. Refresh status to reload the result. ' + error.message,
            'warning',
          );
      }
    } catch (error) {
      if (!active(ticket)) return;
      if (!error.status || error.status >= 500) {
        uncertain = true;
        feedback(
          'The response could not be confirmed. Resolve the saved request before making another change.',
          'warning',
        );
      } else {
        pending = null;
        uncertain = false;
        if ([401, 403, 404].includes(error.status)) {
          failure(error);
          return;
        }
        feedback(error.message + ' Your draft is preserved.', 'error');
        if (error.status === 409) {
          try {
            await showConflict();
          } catch {
            feedback(
              error.message + ' Refresh the current state before submitting again.',
              'error',
            );
          }
        }
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
            'No saved receipt was found. Retry the same request; its original key is preserved.',
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

  function failure(error) {
    if ([401, 403, 404].includes(error.status)) reset();
    else feedback(error.message, 'error');
  }

  function reset() {
    generation++;
    viewGeneration++;
    clearPreview();
    projectId = workspaceId = snapshot = detail = editing = pending = latest = null;
    busy = uncertain = dirty = false;
    artifacts = [];
    more = false;
    for (const kind of forms) $('na-' + kind + '-form').reset();
    for (const id of [
      'na-project',
      'na-access',
      'na-policy-summary',
      'na-artifacts',
      'na-detail-heading',
      'na-detail-summary',
      'na-revision',
      'na-checks',
      'na-reviews',
      'na-decisions',
      'na-latest',
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
    $('na-project').textContent = project.name;
    dialog.showModal();
    overview();
    busy = true;
    lock();
    $('na-heading').focus();
    feedback('Loading artifacts…');
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
    if (bridge().getProject()) bridge().onTeamChanged();
  }

  for (const kind of forms) {
    const form = $('na-' + kind + '-form');
    form.addEventListener('input', () => {
      dirty = true;
      lock();
    });
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (
        busy ||
        uncertain ||
        latest ||
        !editing ||
        !snapshot?.can_manage ||
        !event.currentTarget.reportValidity()
      )
        return;
      const ticket = generation;
      // Reading a file is asynchronous; hold the editor until its exact request is captured.
      busy = true;
      lock();
      try {
        const operation = await buildOperation();
        if (!active(ticket)) return;
        pending = {
          ...operation,
          kind: editing.kind,
          artifactId: editing.detail?.artifact.id,
          url: path(operation.suffix),
          body: { ...operation.body, idempotency_key: crypto.randomUUID() },
        };
        busy = false;
        await perform();
      } catch (error) {
        if (active(ticket)) feedback(error.message, 'error');
      } finally {
        if (active(ticket)) {
          busy = false;
          lock();
        }
      }
    };
  }
  $('np-artifacts').onclick = open;
  $('na-close').onclick = close;
  $('na-policy-edit').onclick = editPolicy;
  $('na-upload-new').onclick = () => editUpload();
  $('na-revision-new').onclick = () => editUpload(true);
  $('na-review-new').onclick = editReview;
  $('na-accept').onclick = () => editDecision('approved');
  $('na-changes').onclick = () => editDecision('changes_requested');
  $('na-retry').onclick = perform;
  $('na-check').onclick = checkOperation;
  $('na-revision').onchange = () => {
    renderEvidence();
    preview();
  };
  $('na-back').onclick = () => {
    if (!busy && !uncertain) {
      overview();
      feedback();
    }
  };
  $('na-editor-cancel').onclick = () => {
    if (!busy && !uncertain) {
      if (detail) renderDetail();
      else overview();
      feedback();
    }
  };
  $('na-rebase').onclick = () => {
    if (busy || uncertain || !latest) return;
    if (editing.kind === 'policy') {
      editing.policy = latest.policy;
      snapshot = latest;
      latest = null;
      $('na-conflict').hidden = true;
      feedback('Your draft now uses the latest policy version. Review the settings before saving.');
      lock();
    } else if (editing.kind === 'upload' && !editing.detail) {
      snapshot = latest;
      artifacts = latest.artifacts.items;
      more = latest.artifacts.has_more;
      latest = null;
      $('na-conflict').hidden = true;
      if (!uploadTasks().some((item) => item.id === $('na-task').value)) {
        overview();
        renderOverview();
        feedback(
          'This task can no longer receive a first output. Inspect its saved artifact or choose another task.',
        );
      } else feedback('Your file is preserved. Review the updated task before saving its output.');
      lock();
    } else {
      detail = latest;
      renderDetail();
      feedback('Review the current output version and task before making a new request.');
    }
  };
  $('na-refresh').onclick = async () => {
    if (busy || dirty) return;
    const ticket = generation;
    busy = true;
    lock();
    try {
      await refresh();
      if (active(ticket))
        feedback(
          uncertain
            ? 'Status refreshed. Resolve the saved request to continue.'
            : 'Status refreshed.',
        );
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  };
  $('na-more').onclick = async () => {
    if (busy || uncertain) return;
    const ticket = generation;
    busy = true;
    lock();
    try {
      const result = await bridge().request(path('?offset=' + artifacts.length + '&limit=50'));
      if (!active(ticket)) return;
      const known = new Set(artifacts.map((item) => item.id));
      if (result.artifacts.items.some((item) => known.has(item.id)))
        throw Error('The artifact list changed while loading. Refresh status before continuing.');
      artifacts.push(...result.artifacts.items);
      more = result.artifacts.has_more;
      renderOverview();
    } catch (error) {
      if (active(ticket)) failure(error);
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  };
  $('na-download').onclick = async () => {
    if (busy || uncertain || !revision()) return;
    const selected = revision(),
      ticket = generation,
      viewTicket = viewGeneration;
    busy = true;
    lock();
    try {
      const blob = await bridge().download(
        path('/' + detail.artifact.id + '/revisions/' + selected.id + '/content'),
      );
      if (!active(ticket) || viewTicket !== viewGeneration) return;
      const url = URL.createObjectURL(blob);
      const link = node('a');
      link.href = url;
      link.download = selected.filename;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      feedback('Downloaded the selected immutable output version.');
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
  window.SimonNativeArtifacts = { open };
})();
