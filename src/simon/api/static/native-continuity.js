'use strict';
(() => {
  const { create, node, button, encodedBytes, precise } = window.SimonNativePanel;
  const bridge = () => window.SimonNativeProjects;
  let snapshot = null,
    detail = null,
    items = [],
    offset = 0;
  const available = () => detail?.disclosure_allowed === true;
  const money = (amount) =>
    Number.isSafeInteger(amount)
      ? new Intl.NumberFormat(undefined, {
          style: 'currency',
          currency: 'USD',
          maximumFractionDigits: 6,
        }).format(amount / 1000000)
      : 'Exact amount requires API inspection';
  const panel = create({
    prefix: 'nh',
    path: (state) => '/v2/projects/' + state.projectId + '/handoffs',
    load: async (_target, ticket) => {
      await load(ticket, true);
      if (panel.active(ticket)) overview();
    },
    refresh: async (ticket) => {
      const id = detail?.handoff.id;
      await load(ticket, true);
      if (!panel.active(ticket)) return;
      if (id) await loadDetail(id, ticket);
      else overview();
    },
    saved: async (operation, result, ticket) => {
      const id = result.handoff?.id || result.handoff_id || result.id || operation.handoffId;
      await load(ticket, true);
      if (!panel.active(ticket)) return;
      panel.state.editing = null;
      panel.state.dirty = false;
      await loadDetail(id, ticket);
      if (!panel.active(ticket)) return;
      panel.feedback(
        operation.kind === 'resume'
          ? 'Exact continuation recorded. Prior effects and completed checkpoints were not redispatched. Use the enrolled native worker for any next claim.'
          : 'Authorized handoff snapshot saved. Current permissions govern inspection, export and continuation.',
        'success',
      );
    },
    reload: async (ticket) => {
      const editing = panel.state.editing;
      const selectedRun = panel.$('run').value,
        selectedRunner = panel.$('runner').value;
      await load(ticket, true);
      if (!panel.active(ticket)) return;
      if (editing?.kind === 'resume') {
        await loadDetail(editing.handoffId, ticket, true);
        if (!panel.active(ticket)) return;
        if (!available() || !detail.can_resume) {
          renderDetail();
          return;
        }
        fillActions(selectedRun, selectedRunner);
      }
      panel.state.conflict = false;
      panel.feedback(
        'Current authority reviewed. Your selection is preserved where it remains supported. Review the current checkpoint before submitting again.',
      );
    },
    reset: () => {
      snapshot = detail = null;
      items = [];
      offset = 0;
      for (const id of [
        'items',
        'detail-heading',
        'applicability',
        'reasons',
        'snapshot',
        'recipient',
        'run',
        'runner',
        'action-reason',
      ])
        panel.$(id).replaceChildren();
      for (const id of ['create-form', 'resume-form', 'verify-form']) panel.$(id).reset();
      panel.$('overview').hidden = false;
      panel.$('detail').hidden =
        panel.$('create-form').hidden =
        panel.$('resume-form').hidden =
          true;
      panel.$('back').hidden = panel.$('cancel').hidden = true;
    },
    lock: (state) => {
      for (const control of document.querySelectorAll(
        '#nh-create-form input, #nh-create-form select, #nh-resume-form select',
      ))
        control.disabled ||= !state.canManage;
      panel.$('create-save').disabled ||= state.conflict;
      panel.$('resume-save').disabled ||=
        state.conflict ||
        !detail?.can_resume ||
        !selectedAction()?.eligible ||
        !panel.$('runner').value;
      panel.$('resume-edit').disabled ||=
        !available() || !detail?.can_resume || Boolean(state.editing);
      panel.$('download').disabled ||= !available() || !detail?.can_export;
      panel.$('verify').disabled ||= !available() || !detail?.can_export;
      panel.$('archive').disabled ||= !available() || !detail?.can_export;
      panel.$('more').hidden = !snapshot?.has_more;
      panel.$('back').disabled ||= state.busy || state.uncertain;
      panel.$('cancel').disabled ||= state.busy || state.uncertain;
    },
  });

  function overview() {
    detail = null;
    panel.state.editing = null;
    panel.state.dirty = panel.state.conflict = false;
    panel.$('overview').hidden = false;
    panel.$('detail').hidden = panel.$('create-form').hidden = panel.$('resume-form').hidden = true;
    panel.$('back').hidden = panel.$('cancel').hidden = true;
    panel.lock();
  }

  function renderItems() {
    panel.state.canManage = snapshot.can_manage;
    panel.$('items').replaceChildren(
      ...(items.length
        ? items.map((item) => {
            const card = node('li', undefined, 'ni-card');
            card.dataset.handoffId = item.id;
            card.append(
              node('h4', item.title),
              node(
                'p',
                new Date(item.created_at).toLocaleString() +
                  ' · ' +
                  (item.current
                    ? 'Matches the current project'
                    : 'Historical or withheld snapshot'),
                'np-field-help',
              ),
            );
            for (const reason of item.reasons) card.append(node('p', reason, 'np-field-help'));
            card.append(
              button('Inspect authorized handoff', () =>
                panel.run((ticket) => loadDetail(item.id, ticket)),
              ),
            );
            return card;
          })
        : [
            node(
              'li',
              snapshot.has_more
                ? 'No authorized handoffs on this page. Load more saved records.'
                : 'No saved handoffs visible to your current creator or recipient scope.',
              'np-empty',
            ),
          ]),
    );
    panel.lock();
  }

  async function load(ticket, reset = false) {
    if (reset) offset = 0;
    const result = await bridge().request(panel.path('?offset=' + offset + '&limit=50'));
    if (!panel.active(ticket)) return;
    snapshot = result;
    if (reset) items = [];
    const known = new Map(items.map((item) => [item.id, item]));
    for (const item of result.items) known.set(item.id, item);
    items = [...known.values()];
    renderItems();
  }

  function section(title, cards) {
    const wrapper = node('section', undefined, 'ni-section');
    wrapper.append(node('h4', title));
    const list = node('ul', undefined, 'ni-list');
    list.append(
      ...(cards.length ? cards : [node('li', 'None recorded in this snapshot.', 'np-empty')]),
    );
    wrapper.append(list);
    return wrapper;
  }

  function renderDetail() {
    panel.state.editing = null;
    panel.state.dirty = panel.state.conflict = false;
    panel.$('overview').hidden =
      panel.$('create-form').hidden =
      panel.$('resume-form').hidden =
        true;
    panel.$('detail').hidden = false;
    panel.$('back').hidden = false;
    panel.$('cancel').hidden = true;
    panel.$('snapshot').replaceChildren();
    panel.$('verify-form').reset();
    panel.$('reasons').replaceChildren(...detail.reasons.map((reason) => node('li', reason)));
    if (!available()) {
      panel.$('detail-heading').textContent = 'Retained handoff';
      panel.$('applicability').textContent =
        'Source authority no longer permits disclosure. Retained work, costs and checkpoint content are withheld.';
      panel.$('run').replaceChildren();
      panel.$('runner').replaceChildren();
      panel.lock();
      return;
    }
    const handoff = detail.handoff,
      manifest = handoff.manifest,
      data = manifest.snapshot;
    panel.$('detail-heading').textContent = manifest.title;
    panel.$('applicability').textContent = detail.current
      ? 'Snapshot matches current project evidence. Each continuation still rechecks live authority.'
      : 'This is a historical snapshot. Inspect the current reasons and supported actions before continuing.';
    panel
      .$('snapshot')
      .append(node('p', 'Manifest SHA-256 ' + handoff.manifest_sha256, 'np-field-help'));
    panel.$('snapshot').append(
      section(
        'Captured outputs and decisions',
        data.artifacts.map((output) => {
          const card = node('li', undefined, 'ni-card');
          const current = output.revisions.find(
            (revision) => revision.id === output.artifact.current_revision_id,
          );
          card.append(
            node('h5', current?.filename || output.artifact.name),
            node('p', output.status.replaceAll('_', ' ')),
            node(
              'p',
              'Checks: ' +
                output.checks.length +
                ' · Owner decisions: ' +
                output.decisions.length +
                ' · Exact revision ' +
                output.artifact.current_revision_id,
              'np-field-help',
            ),
          );
          if (output.dependency_snapshot)
            card.append(
              node(
                'p',
                'Dependency coverage: ' + (output.dependency_snapshot.coverage || 'unknown'),
                'np-field-help',
              ),
            );
          return card;
        }),
      ),
    );
    panel.$('snapshot').append(
      section(
        'Captured facts and requirements',
        (data.facts || []).map((item) => {
          const card = node('li', undefined, 'ni-card');
          card.append(node('h5', item.fact.key));
          card.append(
            node(
              'p',
              item.disclosure === 'available' && item.revision
                ? (precise(item.revision.value)
                    ? JSON.stringify(item.revision.value)
                    : 'Exact value requires API inspection') +
                    (item.revision.unit ? ' ' + item.revision.unit : '')
                : 'Captured fact evidence is withheld.',
              'np-field-help',
            ),
          );
          return card;
        }),
      ),
      section(
        'Captured delivery packages',
        (data.delivery_packets || []).map((packet) => {
          const card = node('li', undefined, 'ni-card');
          card.append(
            node('h5', packet.manifest.title),
            node(
              'p',
              (packet.acceptance?.applicability?.accepted
                ? 'Package acceptance applied at capture.'
                : 'Package acceptance did not apply at capture.') +
                ' Validation reports: ' +
                packet.validations.length +
                '. Package decisions: ' +
                packet.package_decisions.length +
                '.',
              'np-field-help',
            ),
            button('Inspect current package evidence', () => {
              const target = { packetId: packet.packet_id };
              panel.reset();
              window.SimonNativeAcceptance?.open(target);
            }),
          );
          return card;
        }),
      ),
      section(
        'Captured change assessments and repair previews',
        (data.repair_proposals || []).map((proposal) => {
          const card = node('li', undefined, 'ni-card');
          card.append(
            node('h5', 'Repair preview ' + proposal.id.slice(0, 8)),
            node(
              'p',
              proposal.plan.outputs.length +
                ' proposed outputs. Remaining stale outputs: ' +
                proposal.plan.remaining_stale.length +
                '.',
              'np-field-help',
            ),
          );
          return card;
        }),
      ),
      node(
        'p',
        'Captured change assessments: ' + (data.dependency_assessments || []).length + '.',
        'np-field-help',
      ),
      section(
        'Remaining owner decisions',
        data.remaining_decisions.map((decision) => {
          const output = data.artifacts.find((item) => item.artifact.id === decision.artifact_id);
          const card = node('li', undefined, 'ni-card');
          card.append(
            node('h5', output?.artifact.name || decision.artifact_id),
            node('p', decision.status.replaceAll('_', ' ')),
          );
          card.append(
            button('Inspect current output decision', () => {
              const target = { artifactId: decision.artifact_id };
              panel.reset();
              window.SimonNativeArtifacts?.open(target);
            }),
          );
          return card;
        }),
      ),
    );
    panel.$('snapshot').append(
      section(
        'Active captured tasks',
        data.pending_tasks.map((task) => {
          const card = node('li', undefined, 'ni-card');
          card.append(
            node('h5', task.title),
            node('p', task.status.replaceAll('_', ' ')),
            node('p', task.description, 'np-field-help'),
          );
          return card;
        }),
      ),
    );
    panel.$('snapshot').append(
      section(
        'Checkpoint and effect evidence',
        data.runs.map((run) => {
          const card = node('li', undefined, 'ni-card');
          card.dataset.capturedRun = run.id;
          card.append(
            node('h5', 'Run ' + run.id),
            node(
              'p',
              run.status +
                ' · Checkpoint ' +
                run.applied_step +
                ' applied / ' +
                run.step_number +
                ' recorded',
              'np-field-help',
            ),
          );
          for (const step of run.checkpoints)
            card.append(
              node(
                'p',
                'Checkpoint ' +
                  step.sequence +
                  ' · ' +
                  step.kind +
                  ' · ' +
                  step.status +
                  ' · Operation ' +
                  step.operation_id,
                'np-field-help',
              ),
            );
          for (const effect of run.effects)
            card.append(
              node(
                'p',
                'Provider effect ' +
                  effect.operation_id +
                  ' · ' +
                  effect.status +
                  ' · Receipt ' +
                  (effect.receipt_digest || 'unresolved'),
                'np-field-help',
              ),
            );
          const action = detail.actions.find((item) => item.run_id === run.id);
          if (action)
            card.append(
              node(
                'p',
                action.eligible ? 'Currently eligible: ' + action.action : action.reason,
                'np-field-help',
              ),
            );
          return card;
        }),
      ),
    );
    const costs = node('section', undefined, 'ni-section');
    costs.append(
      node('h4', 'Known charges and held liabilities'),
      node(
        'p',
        'Charged: ' +
          money(data.costs.charged_microusd) +
          ' · Held: ' +
          money(data.costs.held_microusd),
        'ni-objective',
      ),
    );
    for (const entry of data.costs.entries)
      costs.append(
        node(
          'p',
          entry.phase +
            ' · ' +
            entry.status +
            ' · Charged ' +
            money(entry.charged_microusd) +
            ' · Held ' +
            money(entry.held_microusd) +
            (entry.error_code ? ' · ' + entry.error_code : ''),
          'np-field-help',
        ),
      );
    panel.$('snapshot').append(costs);
    const grants = node('section', undefined, 'ni-section');
    grants.append(
      node('h4', 'Captured grants and storage'),
      node(
        'p',
        'Execution grant: ' +
          (data.grants.execution_policy.enabled ? 'enabled at capture' : 'disabled at capture') +
          '. Current grants determine every continuation.',
        'np-field-help',
      ),
      node(
        'p',
        'Storage used: ' +
          data.storage.usage.used_bytes +
          ' bytes · Retained: ' +
          data.storage.usage.retained_bytes +
          ' bytes · Reserved: ' +
          data.storage.usage.reserved_bytes +
          ' bytes.',
        'np-field-help',
      ),
    );
    panel.$('snapshot').append(grants);
    if (detail.current_resources) {
      const live = detail.current_resources,
        current = node('section', undefined, 'ni-section');
      current.dataset.currentResources = 'true';
      current.append(
        node('h4', 'Current resource status'),
        node(
          'p',
          'Live status checked during inspection; these values are separate from the captured snapshot.',
          'np-field-help',
        ),
        node(
          'p',
          'Current storage used: ' +
            live.storage_usage.used_bytes +
            ' bytes. Current reserved storage: ' +
            live.storage_usage.reserved_bytes +
            ' bytes.',
          'np-field-help',
        ),
        node(
          'p',
          'Recovery quarantine: ' + (live.recovery_active ? 'inactive' : 'active') + '.',
          'np-field-help',
        ),
      );
      for (const [key, label] of [
        ['charged_lifetime', 'Current known charges'],
        ['held_microusd', 'Current held liabilities'],
      ])
        if (live.costs[key] !== undefined)
          current.append(node('p', label + ': ' + money(live.costs[key]), 'np-field-help'));
      panel.$('snapshot').append(current);
    }
    const limitations = node('ul', undefined, 'np-field-help');
    limitations.append(...manifest.limitations.map((limitation) => node('li', limitation)));
    panel.$('snapshot').append(node('h4', 'Supported scope and limitations'), limitations);
    panel.lock();
  }

  async function loadDetail(id, ticket, preserveEditor = false) {
    const result = await bridge().request(panel.path('/' + id));
    if (!panel.active(ticket)) return;
    detail = result;
    if (!preserveEditor) renderDetail();
    panel.$('detail-heading').focus();
  }

  function createHandoff() {
    if (panel.state.busy || panel.state.uncertain || !panel.state.canManage) return;
    panel.state.editing = { kind: 'create' };
    panel.state.dirty = panel.state.conflict = false;
    panel.$('create-form').reset();
    panel.$('overview').hidden = panel.$('detail').hidden = true;
    panel.$('create-form').hidden = false;
    panel.$('resume-form').hidden = true;
    panel.$('cancel').hidden = false;
    panel.$('back').hidden = true;
    panel.$('recipient').replaceChildren(
      ...snapshot.recipients
        .filter((recipient) => recipient.active)
        .map((recipient) => {
          const option = node('option', recipient.display_name + ' · ' + recipient.role);
          option.value = recipient.actor_id;
          return option;
        }),
    );
    panel.$('title').focus();
    panel.lock();
  }

  function selectedAction() {
    return detail?.actions.find(
      (action) => action.run_id + ':' + action.action === panel.$('run').value,
    );
  }

  function fillActions(run = null, runner = null) {
    panel.$('run').replaceChildren(
      ...detail.actions.map((action) => {
        const option = node(
          'option',
          'Run ' +
            action.run_id.slice(0, 8) +
            ' · ' +
            action.action +
            (action.eligible ? '' : ' · unavailable'),
        );
        option.value = action.run_id + ':' + action.action;
        option.disabled = !action.eligible;
        return option;
      }),
    );
    if (
      run &&
      detail.actions.some(
        (action) => action.run_id + ':' + action.action === run && action.eligible,
      )
    )
      panel.$('run').value = run;
    else
      panel.$('run').value =
        detail.actions.find((action) => action.eligible)?.run_id +
        ':' +
        detail.actions.find((action) => action.eligible)?.action;
    const runners = detail.runners.filter(
      (worker) => worker.status === 'active' && new Date(worker.expires_at) > new Date(),
    );
    panel.$('runner').replaceChildren(
      ...runners.map((worker) => {
        const option = node(
          'option',
          worker.name + ' · expires ' + new Date(worker.expires_at).toLocaleString(),
        );
        option.value = worker.id;
        return option;
      }),
    );
    if (runner && runners.some((worker) => worker.id === runner)) panel.$('runner').value = runner;
    panel.$('action-reason').textContent =
      selectedAction()?.reason ||
      (runners.length
        ? 'Live grants, source authority and the exact checkpoint are rechecked before continuation.'
        : 'Enroll a current native worker in Execution before authorizing continuation.');
    panel.lock();
  }

  function resumeEditor() {
    if (
      !available() ||
      !detail.can_resume ||
      !panel.state.canManage ||
      panel.state.busy ||
      panel.state.uncertain
    )
      return;
    panel.state.editing = { kind: 'resume', handoffId: detail.handoff.id };
    panel.state.dirty = panel.state.conflict = false;
    panel.$('detail').hidden = panel.$('overview').hidden = true;
    panel.$('resume-form').hidden = false;
    panel.$('cancel').hidden = false;
    panel.$('back').hidden = true;
    fillActions();
    panel.$('run').focus();
  }

  panel.$('create').onclick = createHandoff;
  panel.$('resume-edit').onclick = resumeEditor;
  panel.$('run').onchange = () => {
    panel.$('action-reason').textContent =
      selectedAction()?.reason || 'The exact current checkpoint will be rechecked.';
    panel.lock();
  };
  panel.$('runner').onchange = panel.lock;
  panel.$('back').onclick = () => {
    if (!panel.state.busy && !panel.state.uncertain) {
      overview();
      panel.feedback();
    }
  };
  panel.$('cancel').onclick = () => {
    if (!panel.state.busy && !panel.state.uncertain) {
      if (detail) renderDetail();
      else overview();
      panel.feedback();
    }
  };
  panel.$('more').onclick = () =>
    panel.run(async (ticket) => {
      offset += 50;
      await load(ticket);
    });
  panel.$('create-form').onsubmit = async (event) => {
    event.preventDefault();
    if (!panel.state.canManage || !event.currentTarget.reportValidity()) return;
    await panel.save({
      kind: 'create',
      body: { recipient_id: panel.$('recipient').value, title: panel.$('title').value },
    });
  };
  panel.$('resume-form').onsubmit = async (event) => {
    event.preventDefault();
    const action = selectedAction();
    if (
      !panel.state.canManage ||
      !detail.can_resume ||
      !action?.eligible ||
      !event.currentTarget.reportValidity()
    )
      return;
    await panel.save({
      kind: 'resume',
      handoffId: detail.handoff.id,
      suffix: '/' + detail.handoff.id + '/resume',
      body: {
        manifest_sha256: detail.handoff.manifest_sha256,
        run_id: action.run_id,
        expected_run_version: action.expected_run_version,
        runner_id: panel.$('runner').value,
        action: action.action,
        runtime: 'native',
      },
    });
  };
  panel.$('download').onclick = () =>
    panel.run(async (ticket) => {
      if (!available() || !detail.can_export) return;
      const id = detail.handoff.id;
      const blob = await bridge().download(panel.path('/' + id + '/download'));
      if (!panel.active(ticket)) return;
      const url = URL.createObjectURL(blob),
        link = node('a');
      link.href = url;
      link.download = 'handoff-' + id.slice(0, 8) + '.zip';
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      panel.feedback(
        'Authorized handoff exported with exact captured originals and separate current status.',
      );
    });
  panel.$('verify-form').onsubmit = async (event) => {
    event.preventDefault();
    if (!available() || !detail.can_export || !event.currentTarget.reportValidity()) return;
    const file = panel.$('archive').files[0];
    if (!file || file.size > 33554432) {
      panel.feedback('Choose a ZIP archive up to 32 MiB.', 'error');
      return;
    }
    await panel.run(async (ticket) => {
      const id = detail.handoff.id;
      const content = encodedBytes(await file.arrayBuffer());
      if (!panel.active(ticket)) return;
      const result = await bridge().request(
        panel.path('/' + id + '/verify'),
        { content_base64: content },
        'POST',
      );
      if (!panel.active(ticket)) return;
      panel.feedback(
        result.valid
          ? 'Archive verified against the exact captured manifest and original file hashes.'
          : 'Archive verification did not pass.',
        result.valid ? 'success' : 'error',
      );
    });
  };
  document.getElementById('np-handoffs').onclick = () => panel.open();
  window.SimonNativeContinuity = { open: panel.open };
})();
