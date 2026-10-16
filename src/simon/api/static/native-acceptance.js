'use strict';
(() => {
  const { create, node, precise, parseValue } = window.SimonNativePanel;
  const bridge = () => window.SimonNativeProjects;
  const roles = [
    ['json_specification', 'JSON specification'],
    ['markdown_rationale', 'Markdown rationale'],
    ['schema_and_check_report', 'JSON schema and check report'],
  ];
  let record = null,
    packetId = null;
  const available = () => record?.disclosure === 'available';
  const panel = create({
    prefix: 'nv',
    path: (state) =>
      '/v2/projects/' + state.projectId + '/delivery-packets/' + packetId + '/acceptance',
    load: async (target, ticket) => {
      packetId = target?.packetId;
      if (!packetId) throw Error('Choose a saved delivery packet to inspect package evidence.');
      await load(ticket);
    },
    refresh: async (ticket) => {
      await load(ticket);
    },
    saved: async (_operation, _result, ticket) => {
      await load(ticket);
      if (!panel.active(ticket)) return;
      overview();
      panel.feedback(
        'Saved exact package evidence. Validation and acceptance remain separate.',
        'success',
      );
    },
    reload: async (ticket) => {
      const editing = panel.state.editing;
      await load(ticket, true);
      if (!panel.active(ticket)) return;
      if (!available()) {
        overview();
        return;
      }
      if (editing?.kind === 'profile') {
        editing.expectedVersion = record.assignment?.version || 0;
        panel.state.conflict = false;
        panel.feedback(
          'Your role and probe draft is preserved against assignment version ' +
            editing.expectedVersion +
            '. Review the current source versions before saving.',
        );
      } else {
        overview();
        panel.feedback(
          'Inspect the current validation report before reviewing acceptance again. Your notes remain in this tab.',
        );
      }
    },
    reset: () => {
      record = null;
      packetId = null;
      for (const id of [
        'packet',
        'applicability',
        'reasons',
        'assignment',
        'validations',
        'decisions',
        'limitations',
        'role-fields',
        'latest',
        'decision-context',
      ])
        panel.$(id).replaceChildren();
      panel.$('profile-form').reset();
      panel.$('decision-form').reset();
      panel.$('profile-form').hidden = panel.$('decision-form').hidden = true;
      panel.$('overview').hidden = false;
      panel.$('cancel').hidden = true;
    },
    lock: (state) => {
      for (const control of document.querySelectorAll(
        '#nv-profile-form input, #nv-profile-form textarea, #nv-profile-form select, #nv-decision-form textarea',
      ))
        control.disabled ||= !state.canManage;
      panel.$('validate').disabled ||=
        !available() || !record?.assignment || Boolean(state.editing);
      panel.$('profile-edit').disabled ||= !available() || Boolean(state.editing);
      panel.$('dependencies').disabled ||= !available() || Boolean(state.editing);
      panel.$('accept').disabled ||=
        !available() ||
        !record?.can_accept ||
        record?.applicability.accepted ||
        Boolean(state.editing);
      panel.$('profile-save').disabled ||= state.conflict;
      panel.$('decision-save').disabled ||= state.conflict || !record?.can_accept;
      panel.$('cancel').disabled ||= state.busy || state.uncertain;
    },
  });

  function overview() {
    panel.state.editing = null;
    panel.state.dirty = false;
    panel.state.conflict = false;
    panel.$('overview').hidden = false;
    panel.$('profile-form').hidden = panel.$('decision-form').hidden = true;
    panel.$('cancel').hidden = true;
    panel.lock();
  }

  function checkName(name) {
    if (name.startsWith('preview_')) {
      const component = record.packet.manifest.components.find(
        (item) => item.revision_id === name.slice(8),
      );
      return 'Safe preview: ' + (component?.filename || 'source file');
    }
    return name.replaceAll('_', ' ');
  }

  function validationCard(validation) {
    const card = node('li', undefined, 'ni-card');
    card.dataset.validationId = validation.id;
    card.append(
      node('h4', validation.passed ? 'Validation passed' : 'Validation failed'),
      node(
        'p',
        'Assignment version ' +
          validation.profile_version +
          ' · ' +
          new Date(validation.created_at).toLocaleString(),
        'np-field-help',
      ),
    );
    const checks = node('ul');
    checks.append(
      ...validation.checks.map((check) =>
        node(
          'li',
          checkName(check.name) +
            ': ' +
            check.status +
            (check.message ? ' · ' + check.message : ''),
        ),
      ),
    );
    card.append(checks);
    const probe = validation.evidence?.probe;
    if (probe)
      card.append(
        node(
          'p',
          'Edit / save / reopen / render: ' +
            probe.selector.path +
            ' · Unrelated data ' +
            (probe.unrelated_data_preserved ? 'preserved' : 'unverified') +
            '. Original source files remain unchanged.',
          'np-field-help',
        ),
        node(
          'p',
          'Edited copy SHA-256 ' +
            probe.edited_source_sha256 +
            ' · Reopened SHA-256 ' +
            probe.reopened_edited_sha256,
          'np-field-help',
        ),
      );
    for (const original of validation.evidence?.originals || [])
      card.append(
        node(
          'p',
          'Original ' +
            original.role.replaceAll('_', ' ') +
            ' · ' +
            original.size_bytes +
            ' bytes · SHA-256 ' +
            original.sha256,
          'np-field-help',
        ),
      );
    for (const limitation of validation.limitations || [])
      card.append(node('p', limitation, 'np-field-help'));
    card.append(
      node('p', 'Validation authority SHA-256 ' + validation.authority_sha256, 'np-field-help'),
    );
    return card;
  }

  function render() {
    panel.state.canManage = record.can_manage;
    if (!available()) {
      panel.$('packet').textContent = 'Retained packet evidence';
      panel.$('applicability').textContent = 'Source access no longer permits package disclosure.';
      for (const id of [
        'assignment',
        'validations',
        'decisions',
        'role-fields',
        'limitations',
        'decision-context',
      ])
        panel.$(id).replaceChildren();
      panel.$('profile-form').reset();
      panel.$('decision-form').reset();
      panel
        .$('reasons')
        .replaceChildren(...record.applicability.reasons.map((reason) => node('li', reason)));
      overview();
      return;
    }
    panel.$('packet').textContent =
      record.packet.manifest.title + ' · Manifest SHA-256 ' + record.packet.manifest_sha256;
    panel.$('applicability').textContent = record.applicability.accepted
      ? 'Package acceptance currently applies to these exact files and validation evidence.'
      : record.applicability.current
        ? 'Validation is current. Explicit owner package acceptance remains a separate decision.'
        : 'Package evidence is incomplete or no longer current. Resolve the reasons before acceptance.';
    panel
      .$('reasons')
      .replaceChildren(...record.applicability.reasons.map((reason) => node('li', reason)));
    const assignment = record.assignment;
    panel.$('assignment').replaceChildren();
    if (assignment) {
      panel
        .$('assignment')
        .append(
          node(
            'p',
            assignment.profile_name +
              ' · Profile ' +
              assignment.profile_version +
              ' · Validator ' +
              assignment.validator_version +
              ' · Assignment ' +
              assignment.version,
            'ni-objective',
          ),
        );
      for (const binding of assignment.roles) {
        const component = record.packet.manifest.components.find(
          (item) => item.revision_id === binding.revision_id,
        );
        panel
          .$('assignment')
          .append(
            node(
              'p',
              binding.role.replaceAll('_', ' ') +
                ': ' +
                (component?.filename || 'Missing source revision ' + binding.revision_id),
              'np-field-help',
            ),
          );
      }
      panel
        .$('assignment')
        .append(
          node(
            'p',
            'Edit probe: ' +
              assignment.probe.selector.path +
              ' · ' +
              (precise(assignment.probe.before) && precise(assignment.probe.after)
                ? JSON.stringify(assignment.probe.before) +
                  ' → ' +
                  JSON.stringify(assignment.probe.after)
                : 'Exact numeric values require API inspection.'),
            'np-field-help',
          ),
        );
    } else
      panel
        .$('assignment')
        .append(
          node(
            'p',
            'No registered profile assigned. A packet ZIP and accepted components alone do not establish package acceptance.',
            'np-empty',
          ),
        );
    panel
      .$('validations')
      .replaceChildren(
        ...(record.validations.length
          ? record.validations.map(validationCard)
          : [node('li', 'No deterministic validation report recorded.', 'np-empty')]),
      );
    panel.$('decisions').replaceChildren(
      ...(record.acceptances.length
        ? record.acceptances.map((acceptance) => {
            const card = node('li', undefined, 'ni-card');
            card.dataset.packageAcceptanceId = acceptance.id;
            card.append(
              node(
                'h4',
                record.applicability.accepted && acceptance.id === record.latest_acceptance?.id
                  ? 'Currently applicable owner package acceptance'
                  : 'Historical owner package acceptance',
              ),
              node('p', acceptance.feedback || 'No additional owner notes.'),
              node(
                'p',
                new Date(acceptance.created_at).toLocaleString() +
                  ' · Validation ' +
                  acceptance.validation_id,
                'np-field-help',
              ),
            );
            return card;
          })
        : [node('li', 'No owner package acceptance recorded.', 'np-empty')]),
    );
    panel
      .$('limitations')
      .replaceChildren(
        ...(record.profile?.limitations || []).map((limitation) => node('li', limitation)),
      );
    panel.lock();
  }

  async function load(ticket, preserveEditor = false) {
    panel.feedback('Loading exact package validation and acceptance evidence…');
    const data = await bridge().request(panel.path());
    if (!panel.active(ticket)) return;
    record = data;
    if (!preserveEditor) overview();
    render();
    panel.feedback();
  }

  function editProfile() {
    if (panel.state.busy || panel.state.uncertain || !panel.state.canManage || !available()) return;
    const assignment = record.assignment;
    if (assignment && (!precise(assignment.probe.before) || !precise(assignment.probe.after))) {
      panel.feedback(
        'This saved probe exceeds browser numeric precision. Use the API to preserve its exact values.',
        'warning',
      );
      return;
    }
    panel.state.editing = { kind: 'profile', expectedVersion: assignment?.version || 0 };
    panel.state.dirty = panel.state.conflict = false;
    panel.$('overview').hidden = true;
    panel.$('profile-form').hidden = false;
    panel.$('decision-form').hidden = true;
    panel.$('cancel').hidden = false;
    panel.$('role-fields').replaceChildren(
      ...roles.map(([role, title]) => {
        const wrapper = node('div', undefined, 'np-field');
        const select = node('select');
        select.id = 'nv-role-' + role;
        select.required = true;
        const label = node('label', title);
        label.htmlFor = select.id;
        select.append(node('option', 'Choose an exact source version'));
        select.firstChild.value = '';
        for (const component of record.packet.manifest.components.filter(
          (item) => item.role === 'source',
        )) {
          const option = node('option', component.filename + ' · ' + component.media_type);
          option.value = component.revision_id;
          select.append(option);
        }
        select.value =
          assignment?.roles.find((binding) => binding.role === role)?.revision_id || '';
        wrapper.append(label, select);
        return wrapper;
      }),
    );
    panel.$('probe-path').value = assignment?.probe.selector.path || '';
    panel.$('probe-before').value = assignment ? JSON.stringify(assignment.probe.before) : '';
    panel.$('probe-after').value = assignment ? JSON.stringify(assignment.probe.after) : '';
    if (
      assignment &&
      (assignment.roles.length !== 3 ||
        new Set(assignment.roles.map((binding) => binding.role)).size !== 3)
    )
      panel.feedback(
        'The saved assignment has missing or duplicate roles. This composer creates a corrected three-role assignment and retains the original in history.',
        'warning',
      );
    else panel.feedback();
    panel.$('probe-path').focus();
    panel.lock();
  }

  function exactTarget() {
    return {
      assignment_id: record.assignment.id,
      expected_profile_version: record.assignment.version,
      manifest_sha256: record.packet.manifest_sha256,
      validator_version: record.assignment.validator_version,
    };
  }

  function editDecision() {
    if (
      panel.state.busy ||
      panel.state.uncertain ||
      !record?.can_accept ||
      !available() ||
      record.applicability.accepted
    )
      return;
    panel.state.editing = {
      kind: 'decision',
      target: exactTarget(),
      validationId: record.latest_validation.id,
      authority: record.latest_validation.authority_sha256,
    };
    panel.state.dirty = panel.state.conflict = false;
    panel.$('overview').hidden = true;
    panel.$('profile-form').hidden = true;
    panel.$('decision-form').hidden = false;
    panel.$('cancel').hidden = false;
    panel.$('decision-context').textContent =
      record.packet.manifest.title +
      ' · Manifest SHA-256 ' +
      record.packet.manifest_sha256 +
      ' · Assignment ' +
      record.assignment.version +
      ' · Validation ' +
      record.latest_validation.id;
    panel.$('feedback-notes').focus();
    panel.lock();
  }

  panel.$('profile-edit').onclick = editProfile;
  panel.$('accept').onclick = editDecision;
  panel.$('dependencies').onclick = () => {
    if (!available() || panel.state.busy || panel.state.uncertain) return;
    panel.reset();
    window.SimonNativeDependencies?.open();
  };
  panel.$('cancel').onclick = () => {
    if (!panel.state.busy && !panel.state.uncertain) {
      overview();
      render();
      panel.feedback();
    }
  };
  panel.$('validate').onclick = async () => {
    if (!available() || !record.assignment || !panel.state.canManage) return;
    await panel.save({
      kind: 'validate',
      suffix: '/validate',
      body: exactTarget(),
      message: 'Running bounded local package validation…',
    });
  };
  panel.$('profile-form').onsubmit = async (event) => {
    event.preventDefault();
    if (
      !event.currentTarget.reportValidity() ||
      !panel.state.canManage ||
      panel.state.editing?.kind !== 'profile'
    )
      return;
    try {
      const body = {
        expected_version: panel.state.editing.expectedVersion,
        profile_name: record.profile.name,
        profile_version: record.profile.version,
        validator_version: record.profile.validator_version,
        roles: roles.map(([role]) => ({ role, revision_id: panel.$('role-' + role).value })),
        probe: {
          selector: { kind: 'json_pointer', path: panel.$('probe-path').value },
          before: parseValue(panel.$('probe-before').value),
          after: parseValue(panel.$('probe-after').value),
        },
      };
      await panel.save({ kind: 'profile', suffix: '/profile', method: 'PUT', body });
    } catch (error) {
      panel.feedback(error.message, 'error');
    }
  };
  panel.$('decision-form').onsubmit = async (event) => {
    event.preventDefault();
    if (!panel.state.canManage || panel.state.editing?.kind !== 'decision' || !record.can_accept)
      return;
    const editing = panel.state.editing;
    await panel.save({
      kind: 'accept',
      suffix: '/decisions',
      body: {
        ...editing.target,
        validation_id: editing.validationId,
        authority_sha256: editing.authority,
        feedback: panel.$('feedback-notes').value,
      },
    });
  };
  window.SimonNativeAcceptance = { open: panel.open };
})();
