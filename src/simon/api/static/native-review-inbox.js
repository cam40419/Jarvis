'use strict';
(() => {
  const { create, node, button, precise } = window.SimonNativePanel;
  const bridge = () => window.SimonNativeProjects;
  let snapshot = null,
    groups = new Map(),
    artifactOffset = 0,
    packetOffset = 0;
  const panel = create({
    prefix: 'nr',
    path: (state) => '/v2/projects/' + state.projectId + '/review-inbox',
    load: async (_target, ticket) => {
      await load(ticket, true);
    },
    refresh: async (ticket) => {
      await load(ticket, true);
    },
    reset: () => {
      snapshot = null;
      groups = new Map();
      artifactOffset = packetOffset = 0;
      for (const id of ['summary', 'warning', 'groups', 'limitations'])
        panel.$(id).replaceChildren();
    },
    lock: (state) => {
      panel.$('artifacts-more').hidden = !snapshot?.pagination.artifacts_has_more;
      panel.$('packets-more').hidden = !snapshot?.pagination.packets_has_more;
      panel.$('policy').disabled ||= !state.canManage;
    },
  });

  function valueText(value) {
    return typeof value === 'string'
      ? value
      : precise(value)
        ? JSON.stringify(value)
        : 'Exact numeric value requires API inspection.';
  }

  function navigate(item) {
    if (panel.state.busy || item.disclosure !== 'available') return;
    const action = item.next_action;
    panel.reset();
    if (action.kind === 'artifact')
      window.SimonNativeArtifacts?.open({
        artifactId: action.target_id || item.id,
        revisionId: item.revision_id,
      });
    else if (action.kind === 'dependencies')
      window.SimonNativeDependencies?.open({
        artifactId: action.target_id || item.id,
        revisionId: item.revision_id,
      });
    else if (action.kind === 'packet')
      window.SimonNativeAcceptance?.open({ packetId: action.target_id || item.id });
    else if (action.kind === 'model_usage') window.SimonNativeModels?.open();
    else if (action.kind === 'policy') window.SimonNativeArtifacts?.open({ policy: true });
  }

  function itemCard(item) {
    const card = node('li', undefined, 'ni-card');
    card.dataset.inboxKind = item.kind;
    card.dataset.inboxId = item.id;
    card.dataset.inboxStatus = item.status;
    card.append(
      node('h4', item.title),
      node('p', item.status.replaceAll('_', ' '), 'ni-objective'),
    );
    if (item.disclosure !== 'available') {
      card.append(
        node(
          'p',
          item.disclosure === 'withheld'
            ? 'Source authority no longer permits disclosure. Retained differences and findings are withheld.'
            : 'Current evidence is unavailable. Resolve its blocker before making a decision.',
        ),
      );
      return card;
    }
    if (item.revision_id)
      card.append(
        node(
          'p',
          'Exact revision ' + item.revision_id + ' · SHA-256 ' + item.sha256,
          'np-field-help',
        ),
      );
    const difference = item.difference;
    if (difference?.field_changes_limitation)
      card.append(node('p', difference.field_changes_limitation, 'np-field-help'));
    if (difference?.supported && difference.text) {
      const changes = node('details');
      changes.open = true;
      changes.append(node('summary', 'Substantive changes'), node('pre', difference.text));
      if (difference.truncated)
        changes.append(
          node(
            'p',
            'This difference is bounded. Inspect complete versions before deciding.',
            'np-field-help',
          ),
        );
      card.append(changes);
    }
    if (difference?.field_changes?.length) {
      const fields = node('ul');
      fields.append(
        ...difference.field_changes.map((change) =>
          node(
            'li',
            change.path +
              ': ' +
              (change.before_present === false ? '(absent)' : valueText(change.before)) +
              ' → ' +
              (change.after_present === false ? '(absent)' : valueText(change.after)),
          ),
        ),
      );
      card.append(fields);
      if (difference.fields_truncated)
        card.append(
          node('p', 'Additional changed fields are omitted from this summary.', 'np-field-help'),
        );
    }
    if (item.checks) {
      card.append(
        node(
          'p',
          'Checks: ' +
            item.checks.passed +
            ' passed, ' +
            item.checks.failed +
            ' failed, ' +
            item.checks.total +
            ' recorded.',
          'np-field-help',
        ),
      );
      for (const check of item.checks.items || [])
        card.append(
          node(
            'p',
            check.name +
              ': ' +
              check.status +
              (check.details
                ? ' · ' +
                  (typeof check.details === 'string'
                    ? check.details
                    : JSON.stringify(check.details))
                : ''),
            'np-field-help',
          ),
        );
    }
    if (item.findings?.length) {
      const findings = node('ul');
      findings.append(...item.findings.map((finding) => node('li', finding)));
      card.append(node('h5', 'Findings and blockers'), findings);
    }
    if (item.coverage) {
      card.append(
        node(
          'p',
          'Evidence coverage: ' +
            item.coverage.kind +
            ' · ' +
            item.coverage.omitted_source_count +
            ' source excerpts omitted.',
          'np-field-help',
        ),
      );
      for (const reason of item.coverage.reasons || [])
        card.append(node('p', reason, 'np-field-help'));
    }
    if (item.repair)
      card.append(
        node(
          'p',
          item.repair.escalated
            ? 'Repair escalation requires owner attention. ' +
                item.repair.used +
                ' of ' +
                item.repair.limit +
                ' repair cycles used.'
            : 'Repair cycles: ' + item.repair.used + ' of ' + item.repair.limit + ' used.',
          'np-field-help',
        ),
      );
    if (item.next_action && item.next_action.kind !== 'none')
      card.append(button(item.next_action.label, () => navigate(item)));
    return card;
  }

  function render() {
    const counts = Object.fromEntries(
      ['needs_review', 'needs_decision', 'needs_repair', 'blocked', 'accepted'].map((key) => [
        key,
        groups.get(key)?.items.size || 0,
      ]),
    );
    panel.$('summary').textContent =
      'Displayed items: ' +
      counts.needs_review +
      ' need review · ' +
      counts.needs_decision +
      ' need a decision · ' +
      counts.needs_repair +
      ' need repair · ' +
      counts.blocked +
      ' blocked · ' +
      counts.accepted +
      ' accepted.';
    panel.$('warning').textContent =
      snapshot.summary.warning ||
      'This summary covers the loaded page. Open each exact current record before making a decision.';
    panel.$('groups').replaceChildren(
      ...[...groups.values()]
        .filter((group) => group.items.size)
        .map((group) => {
          const section = node('section', undefined, 'ni-section');
          section.dataset.inboxGroup = group.key;
          const list = node('ul', undefined, 'ni-list');
          list.append(...[...group.items.values()].map(itemCard));
          section.append(node('h3', group.label), list);
          return section;
        }),
    );
    if (!panel.$('groups').children.length)
      panel.$('groups').append(node('p', 'No decisions or blockers on this page.', 'np-empty'));
    panel
      .$('limitations')
      .replaceChildren(...snapshot.limitations.map((limitation) => node('li', limitation)));
    panel.lock();
  }

  async function load(ticket, reset = false) {
    if (reset) artifactOffset = packetOffset = 0;
    panel.feedback('Loading current decisions and review evidence…');
    const result = await bridge().request(
      panel.path(
        '?artifact_offset=' + artifactOffset + '&packet_offset=' + packetOffset + '&limit=20',
      ),
    );
    if (!panel.active(ticket)) return;
    if (reset) groups = new Map();
    snapshot = result;
    panel.state.canManage = result.can_manage;
    for (const incoming of result.groups) {
      if (!groups.has(incoming.key))
        groups.set(incoming.key, { key: incoming.key, label: incoming.label, items: new Map() });
      for (const item of incoming.items) {
        const id = item.kind + ':' + item.id;
        for (const group of groups.values()) group.items.delete(id);
        groups.get(incoming.key).items.set(id, item);
      }
    }
    render();
    panel.feedback();
  }

  document.getElementById('np-review-inbox').onclick = () => panel.open();
  panel.$('policy').onclick = () => {
    if (!panel.state.canManage || panel.state.busy) return;
    panel.reset();
    window.SimonNativeArtifacts?.open({ policy: true });
  };
  panel.$('artifacts-more').onclick = () =>
    panel.run(async (ticket) => {
      artifactOffset = snapshot.pagination.artifact_offset + snapshot.pagination.limit;
      await load(ticket);
    });
  panel.$('packets-more').onclick = () =>
    panel.run(async (ticket) => {
      packetOffset = snapshot.pagination.packet_offset + snapshot.pagination.limit;
      await load(ticket);
    });
  window.SimonNativeReviewInbox = { open: panel.open };
})();
