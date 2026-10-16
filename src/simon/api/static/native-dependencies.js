'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = () => window.SimonNativeProjects;
  const dialog = $('ng-dialog');
  let generation = 0,
    viewGeneration = 0,
    projectId = null,
    workspaceId = null,
    snapshot = null,
    sources = [],
    detail = null,
    editing = null,
    pending = null,
    busy = false,
    uncertain = false,
    receiptAbsent = false,
    dirty = false,
    conflict = false,
    latest = null;
  const path = (suffix = '') => '/v2/projects/' + projectId + '/dependencies' + suffix;
  const active = (ticket) =>
    ticket === generation &&
    dialog.open &&
    bridge().getProject()?.id === projectId &&
    bridge().getWorkspaceId() === workspaceId;
  const available = (record) => record && record.disclosure !== 'withheld';
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const button = (text, action, managed = false) => {
    const item = node('button', text, 'np-button np-quiet');
    item.type = 'button';
    if (managed) item.dataset.manage = 'true';
    item.onclick = action;
    return item;
  };
  const date = (value) => new Date(value).toLocaleString();
  const selectorName = (value) =>
    value?.kind === 'json_pointer' ? 'JSON field ' + (value.path || '(root)') : 'Whole file';
  const precise = (value) => {
    if (typeof value === 'number')
      return Number.isFinite(value) && (!Number.isInteger(value) || Number.isSafeInteger(value));
    if (Array.isArray(value)) return value.every(precise);
    return !value || typeof value !== 'object' || Object.values(value).every(precise);
  };
  const valueText = (revision) =>
    !precise(revision.value)
      ? 'This value exceeds browser numeric precision. Inspect its exact value through the API.'
      : (revision.value_type === 'string' ? revision.value : JSON.stringify(revision.value)) +
        (revision.unit ? ' ' + revision.unit : '');
  const factName = (id) => snapshot?.facts.find((record) => record.fact.id === id)?.fact.key || id;
  const outputName = (id) =>
    snapshot?.outputs.find((record) => record.artifact.id === id)?.revision?.filename || id;
  const feedback = (message = '', tone = '') => {
    $('ng-feedback').textContent = message;
    $('ng-feedback').dataset.tone = tone;
  };
  const field = (label, input, help = '') => {
    const wrapper = node('div', undefined, 'np-field');
    const caption = node('label', label);
    if (!input.id) input.id = 'ng-input-' + crypto.randomUUID();
    caption.htmlFor = input.id;
    wrapper.append(caption, input);
    if (help) wrapper.append(node('p', help, 'np-field-help'));
    return wrapper;
  };
  const input = (id, value = '', attributes = {}) => {
    const control = node(attributes.rows ? 'textarea' : 'input');
    control.id = id;
    control.value = value;
    Object.assign(control, attributes);
    return control;
  };
  const select = (id, values, selected = null) => {
    const control = node('select');
    control.id = id;
    for (const [value, label] of values) {
      const option = node('option', label);
      option.value = value;
      control.append(option);
    }
    if (selected !== null) control.value = selected;
    return control;
  };
  const empty = (text) => node('li', text, 'np-empty');
  const coverage = (record) => record.declaration?.coverage || 'unknown';
  const selectorFields = (label, initial = {}) => {
    const wrapper = node('div');
    const kind = select(
      '',
      [
        ['whole_file', 'Whole file'],
        ['json_pointer', 'JSON field'],
      ],
      initial.kind,
    );
    const pointer = input('', initial.path || '', { maxLength: 512 });
    kind.dataset.selectorKind = 'true';
    pointer.dataset.selectorPath = 'true';
    const update = () => {
      pointer.parentElement.hidden = kind.value !== 'json_pointer';
      pointer.disabled = kind.value !== 'json_pointer';
    };
    kind.onchange = update;
    wrapper.append(
      field(label + ' selector', kind),
      field(
        label + ' JSON Pointer',
        pointer,
        'For example /unit_price. An empty path selects the JSON root.',
      ),
    );
    update();
    return wrapper;
  };
  const readSelector = (wrapper) => {
    const kind = wrapper.querySelector('[data-selector-kind]').value;
    return {
      kind,
      path: kind === 'json_pointer' ? wrapper.querySelector('[data-selector-path]').value : '',
    };
  };

  function lock() {
    for (const control of dialog.querySelectorAll('input, textarea, select, button'))
      control.disabled = busy || uncertain;
    for (const control of dialog.querySelectorAll(
      '[data-manage="true"], #ng-form input, #ng-form textarea, #ng-form select, #ng-form button',
    ))
      control.disabled ||= !snapshot?.can_manage;
    for (const pointer of dialog.querySelectorAll('[data-selector-path]'))
      pointer.disabled ||= pointer.parentElement.hidden;
    for (const control of dialog.querySelectorAll('[data-fixed="true"]')) control.disabled = true;
    $('ng-close').disabled = busy || uncertain;
    $('ng-refresh').disabled = busy || uncertain || dirty;
    $('ng-save').disabled ||= conflict || !snapshot?.can_manage;
    $('ng-rebase').disabled = busy || uncertain || !conflict;
    $('ng-check').disabled = busy || !uncertain;
    $('ng-retry').disabled = busy || !uncertain || !receiptAbsent;
    $('ng-retry').hidden = !receiptAbsent;
    $('ng-uncertain').hidden = !uncertain;
    $('ng-conflict').hidden = !conflict;
    $('ng-form').setAttribute('aria-busy', String(busy));
    dialog.setAttribute('aria-busy', String(busy));
  }

  function overview() {
    viewGeneration++;
    detail = editing = latest = null;
    dirty = conflict = false;
    $('ng-overview').hidden = false;
    $('ng-detail').hidden = $('ng-form').hidden = true;
    $('ng-back').hidden = $('ng-cancel').hidden = $('ng-save').hidden = true;
    $('ng-close').hidden = false;
    $('ng-fields').replaceChildren();
    $('ng-editor-context').textContent = '';
    $('ng-detail-content').replaceChildren();
    $('ng-detail-actions').replaceChildren();
    $('ng-latest').replaceChildren();
    lock();
  }

  function renderOverview() {
    $('ng-access').textContent = snapshot.can_manage
      ? 'Project owners register input revisions, declare coverage, assess changes and commit reviewed repair previews.'
      : 'You can inspect available facts, output dependencies and change assessments. Project owners make changes.';
    $('ng-facts').replaceChildren(
      ...(snapshot.facts.length
        ? snapshot.facts.map((record) => {
            const card = node('li', undefined, 'ni-card');
            card.dataset.factId = record.fact.id;
            card.append(
              node('h4', record.fact.key),
              node(
                'p',
                available(record) && record.revision
                  ? valueText(record.revision)
                  : 'Source access was revoked. Fact values are withheld.',
                'ni-objective',
              ),
            );
            card.append(button('Inspect fact revisions', () => loadDetail('fact', record.fact.id)));
            return card;
          })
        : [empty('No typed facts or requirements registered yet.')]),
    );
    $('ng-outputs').replaceChildren(
      ...(snapshot.outputs.length
        ? snapshot.outputs.map((record) => {
            const card = node('li', undefined, 'ni-card');
            card.dataset.outputId = record.artifact.id;
            card.append(
              node('h4', record.revision?.filename || record.artifact.name),
              node('p', 'Declared coverage: ' + coverage(record), 'np-field-help'),
            );
            if (!available(record))
              card.append(
                node('p', 'Retained source content and dependency details are withheld.'),
              );
            card.append(button('Inspect output dependencies', () => showOutput(record)));
            return card;
          })
        : [empty('Upload or produce artifacts to declare their dependencies.')]),
    );
    $('ng-assessments').replaceChildren(
      ...(snapshot.assessments.length
        ? snapshot.assessments.map((record) => {
            const card = node('li', undefined, 'ni-card');
            card.dataset.assessmentId = record.id;
            card.append(
              node('h4', 'Change to ' + factName(record.fact_id)),
              node('p', date(record.created_at), 'np-field-help'),
              button('Inspect change assessment', () => loadDetail('assessment', record.id)),
            );
            return card;
          })
        : [empty('Assess a change between two revisions of a registered fact.')]),
    );
    $('ng-repairs').replaceChildren(
      ...((snapshot.repairs || []).length
        ? snapshot.repairs.map((record) => {
            const assessment = snapshot.assessments.find(
              (item) => item.id === record.assessment_id,
            );
            const card = node('li', undefined, 'ni-card');
            card.dataset.proposalId = record.id;
            card.append(
              node(
                'h4',
                assessment
                  ? 'Repair preview: ' + factName(assessment.fact_id)
                  : 'Retained repair preview',
              ),
              node('p', date(record.created_at), 'np-field-help'),
              button('Inspect saved repair preview', () => loadDetail('repair', record.id)),
            );
            return card;
          })
        : [empty('No saved repair previews yet.')]),
    );
    lock();
  }

  async function refresh() {
    const ticket = generation;
    const [view, intake] = await Promise.all([
      bridge().request(path()),
      bridge().request('/v2/projects/' + projectId + '/intake'),
    ]);
    if (!active(ticket)) return false;
    snapshot = view;
    sources = intake.sources;
    renderOverview();
    return true;
  }

  function detailView(kind, record, title) {
    viewGeneration++;
    detail = { kind, record };
    editing = latest = null;
    dirty = conflict = false;
    $('ng-overview').hidden = $('ng-form').hidden = true;
    $('ng-detail').hidden = false;
    $('ng-back').hidden = false;
    $('ng-close').hidden = false;
    $('ng-cancel').hidden = $('ng-save').hidden = true;
    $('ng-detail-heading').textContent = title;
    $('ng-detail-summary').textContent = '';
    $('ng-detail-content').replaceChildren();
    $('ng-detail-actions').replaceChildren();
    $('ng-fields').replaceChildren();
    $('ng-editor-context').textContent = '';
    if (!available(record)) {
      $('ng-detail-content').append(
        node(
          'p',
          'Source access was revoked or is unavailable. Retained values, selectors and repair content are withheld.',
        ),
      );
      lock();
      return false;
    }
    return true;
  }

  function showFact(record) {
    if (!detailView('fact', record, record.fact?.key || 'Retained fact')) return;
    $('ng-detail-summary').textContent =
      record.fact.kind + ' · Current version ' + record.fact.version;
    const list = node('ul', undefined, 'ni-list');
    list.append(
      ...record.revisions.map((revision) => {
        const card = node('li', undefined, 'ni-card');
        card.dataset.factRevisionId = revision.id;
        card.append(
          node(
            'h4',
            'Version ' +
              revision.version +
              (revision.id === record.fact.current_revision_id ? ' (current)' : ''),
          ),
          node('p', valueText(revision), 'ni-objective'),
          node('p', 'SHA-256 ' + revision.sha256, 'np-field-help'),
        );
        for (const binding of revision.sources) {
          const source = sources.find((item) => item.id === binding.source_revision_id);
          card.append(
            node(
              'p',
              (source?.filename || 'Source revision ' + binding.source_revision_id) +
                ' · ' +
                selectorName(binding.selector),
              'np-field-help',
            ),
          );
        }
        if (!revision.sources.length)
          card.append(
            node('p', 'Owner-asserted input without a bound source revision.', 'np-field-help'),
          );
        return card;
      }),
    );
    $('ng-detail-content').append(list);
    const current = record.revisions.find(
      (revision) => revision.id === record.fact.current_revision_id,
    );
    if (precise(current.value))
      $('ng-detail-actions').append(
        button('Record new fact revision', () => editFact(record), true),
      );
    if (record.revisions.length > 1)
      $('ng-detail-actions').append(
        button('Assess a fact change', () => editAssessment(record), true),
      );
    lock();
  }

  function showOutput(record, selectedRevisionId = null) {
    if (!detailView('output', record, record.revision?.filename || record.artifact.name)) return;
    const declaration = record.declaration;
    $('ng-detail-summary').textContent =
      'Declared coverage: ' + coverage(record) + ' · Output SHA-256 ' + record.revision.sha256;
    if (selectedRevisionId && selectedRevisionId !== record.revision.id)
      $('ng-detail-content').append(
        node(
          'p',
          'The artifact shortcut selected a historical version. This panel shows the current output head and its declaration.',
          'np-field-help',
        ),
      );
    if (declaration?.notes)
      $('ng-detail-content').append(node('p', declaration.notes, 'ni-objective'));
    const list = node('ul', undefined, 'ni-list');
    list.append(
      ...(declaration?.dependencies.length
        ? declaration.dependencies.map((binding) => {
            const card = node('li', undefined, 'ni-card');
            card.append(
              node(
                'h4',
                binding.input_kind === 'fact'
                  ? 'Fact: ' + factName(binding.input_id)
                  : 'Output: ' + outputName(binding.input_id),
              ),
              node(
                'p',
                selectorName(binding.input_selector) +
                  ' → ' +
                  selectorName(binding.output_selector),
              ),
              node(
                'p',
                'Input revision ' + binding.revision_id + ' · SHA-256 ' + binding.sha256,
                'np-field-help',
              ),
            );
            return card;
          })
        : [
            empty(
              declaration
                ? 'No input dependencies were declared.'
                : 'No dependency declaration. Coverage remains unknown.',
            ),
          ]),
    );
    $('ng-detail-content').append(list);
    $('ng-detail-actions').append(
      button('Declare output dependencies', () => editDeclaration(record), true),
    );
    lock();
  }

  function showAssessment(record) {
    if (
      !detailView(
        'assessment',
        record,
        'Change assessment: ' + factName(record.assessment?.fact_id),
      )
    )
      return;
    const assessment = record.assessment;
    $('ng-detail-summary').textContent = record.current
      ? 'This assessment matches the current dependency graph and source authority.'
      : 'This assessment is historical. Reassess current inputs and output heads before repair.';
    const list = node('ul', undefined, 'ni-list');
    list.append(
      ...assessment.outputs.map((output) => {
        const card = node('li', undefined, 'ni-card');
        card.dataset.impactStatus = output.status;
        card.dataset.outputId = output.artifact_id;
        card.append(
          node('h4', output.filename + ' · ' + output.status),
          node('p', 'Coverage: ' + output.coverage, 'np-field-help'),
        );
        for (const reason of output.reasons) card.append(node('p', reason));
        for (const selector of output.selectors)
          card.append(node('p', selectorName(selector), 'np-field-help'));
        return card;
      }),
    );
    $('ng-detail-content').append(list);
    if (record.current && assessment.outputs.some((output) => output.status === 'affected'))
      $('ng-detail-actions').append(
        button('Prepare bounded repair', () => editRepair(record), true),
      );
    if (!record.current)
      $('ng-detail-actions').append(
        button('Reassess these fact revisions', () => submitAssessment(assessment), true),
      );
    lock();
  }

  function showRepair(record) {
    if (!detailView('repair', record, 'Repair preview')) return;
    const proposal = record.proposal;
    $('ng-detail-summary').textContent = record.committed_revision_ids?.length
      ? 'Repair committed. New output versions require current validation and review.'
      : record.current
        ? 'Review these exact changes before committing new output versions.'
        : 'This preview is stale and cannot be committed.';
    $('ng-detail-content').append(node('p', proposal.plan.rationale || '', 'ni-objective'));
    for (const output of proposal.plan.outputs) {
      const card = node('section', undefined, 'ni-preview');
      card.dataset.repairOutput = output.artifact_id;
      card.append(
        node('h4', output.filename),
        node(
          'p',
          'Before SHA-256 ' + output.before_sha256 + ' · After SHA-256 ' + output.after_sha256,
          'np-field-help',
        ),
      );
      for (const [label, text] of [
        ['Before', output.before_text],
        ['After', output.after_text],
      ]) {
        const disclosure = node('details');
        disclosure.open = true;
        disclosure.append(node('summary', label), node('pre', text));
        card.append(disclosure);
      }
      $('ng-detail-content').append(card);
    }
    const remaining = proposal.plan.remaining_stale || [];
    $('ng-detail-content').append(
      node(
        'p',
        remaining.length
          ? 'Affected outputs remaining stale: ' +
              remaining
                .map((item) => outputName(typeof item === 'string' ? item : item.artifact_id))
                .join(', ') +
              '.'
          : 'All intended repairs are included. Each new version still requires its own checks and review.',
        'np-field-help',
      ),
    );
    for (const reason of record.reasons || [])
      $('ng-detail-content').append(node('p', reason, 'np-field-help'));
    if (record.can_commit)
      $('ng-detail-actions').append(
        button('Commit these exact repairs', () => commit(record), true),
      );
    lock();
  }

  async function loadDetail(kind, id) {
    if (busy || uncertain) return;
    const ticket = generation,
      viewTicket = ++viewGeneration;
    busy = true;
    lock();
    feedback('Loading current evidence…');
    try {
      const record = await bridge().request(
        path(
          '/' + { fact: 'facts', assessment: 'assessments', repair: 'repairs' }[kind] + '/' + id,
        ),
      );
      if (!active(ticket) || viewTicket !== viewGeneration) return;
      ({ fact: showFact, assessment: showAssessment, repair: showRepair })[kind](record);
      $('ng-detail-heading').focus();
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

  function editor(kind, title, context, record = null) {
    if (busy || uncertain || !snapshot?.can_manage || (record && !available(record))) return false;
    viewGeneration++;
    editing = { kind, record };
    latest = null;
    dirty = conflict = false;
    $('ng-overview').hidden = $('ng-detail').hidden = true;
    $('ng-form').hidden = false;
    $('ng-close').hidden = $('ng-back').hidden = true;
    $('ng-cancel').hidden = $('ng-save').hidden = false;
    $('ng-editor-heading').textContent = title;
    $('ng-editor-context').textContent = context;
    $('ng-fields').replaceChildren();
    feedback();
    return true;
  }

  function removeRow(row) {
    row.remove();
    dirty = true;
    lock();
  }

  function addSource(binding = null) {
    if ($('ng-sources').children.length >= 16) return;
    const row = node('div', undefined, 'ni-card');
    row.dataset.sourceBinding = 'true';
    const choices = [
      ['', 'No source binding'],
      ...sources
        .filter((source) => !source.revoked_at)
        .map((source) => [source.id, source.filename + ' · revision ' + source.revision]),
    ];
    const source = select('', choices, binding?.source_revision_id || '');
    source.dataset.sourceId = 'true';
    const selector = selectorFields('Source', binding?.selector);
    selector.dataset.sourceSelector = 'true';
    const preview = node('pre');
    preview.hidden = true;
    const inspect = button('Inspect permitted source text', async () => {
      if (!source.value || busy || uncertain) return;
      const ticket = generation,
        viewTicket = viewGeneration,
        id = source.value;
      busy = true;
      lock();
      try {
        const value = await bridge().request(
          '/v2/projects/' + projectId + '/intake/sources/' + id + '/text',
        );
        if (!active(ticket) || viewTicket !== viewGeneration || id !== source.value) return;
        preview.textContent = value.text;
        preview.hidden = false;
      } catch (error) {
        if (active(ticket)) {
          preview.textContent = '';
          preview.hidden = true;
          feedback(error.message, 'error');
        }
      } finally {
        if (active(ticket)) {
          busy = false;
          lock();
        }
      }
    });
    source.onchange = () => {
      preview.textContent = '';
      preview.hidden = true;
    };
    row.append(
      field('Source revision', source),
      selector,
      inspect,
      button('Remove source binding', () => removeRow(row)),
      preview,
    );
    $('ng-sources').append(row);
    lock();
  }

  function editFact(record = null) {
    const current = record?.revisions.find(
      (revision) => revision.id === record.fact.current_revision_id,
    );
    if (current && !precise(current.value)) return;
    if (
      !editor(
        'fact',
        record ? 'Record new fact revision' : 'Register fact or requirement',
        'Whole-file bindings record owner-asserted provenance. JSON field bindings validate the typed value against the exact source revision.',
        record,
      )
    )
      return;
    editing.expectedVersion = record?.fact.version || 0;
    const fields = $('ng-fields');
    fields.append(
      field(
        'Stable input key',
        input('ng-fact-key', record?.fact.key || '', {
          required: true,
          maxLength: 96,
          pattern: '[a-z][a-z0-9_.-]{0,95}',
          readOnly: Boolean(record),
        }),
      ),
      field(
        'Input kind',
        select(
          'ng-fact-kind',
          [
            ['fact', 'Fact'],
            ['requirement', 'Requirement'],
          ],
          record?.fact.kind || 'fact',
        ),
      ),
      field(
        'Value type',
        select(
          'ng-value-type',
          [
            ['string', 'Text'],
            ['number', 'Number'],
            ['boolean', 'Boolean'],
            ['json', 'JSON data'],
          ],
          current?.value_type || 'number',
        ),
      ),
      field(
        'Fact value',
        input(
          'ng-fact-value',
          current
            ? current.value_type === 'string'
              ? current.value
              : JSON.stringify(current.value)
            : '',
          { rows: 3, maxLength: 8192 },
        ),
        'Text values are entered directly. Numbers, booleans, and JSON data use JSON notation.',
      ),
      field(
        'Unit',
        input('ng-fact-unit', current?.unit || '', { maxLength: 64 }),
        'For example USD or days.',
      ),
      node('h4', 'Source provenance'),
      node('div'),
    );
    fields.lastChild.id = 'ng-sources';
    if (record) $('ng-fact-kind').dataset.fixed = 'true';
    fields.append(
      button('Add source binding', () => {
        addSource();
        dirty = true;
      }),
    );
    for (const binding of current?.sources || []) addSource(binding);
    $('ng-save').textContent = 'Save fact revision';
    $('ng-fact-key').focus();
    lock();
  }

  function inputChoices(kind) {
    return kind === 'fact'
      ? snapshot.facts
          .filter((record) => available(record) && record.revision)
          .map((record) => ({
            input_kind: 'fact',
            input_id: record.fact.id,
            revision_id: record.revision.id,
            sha256: record.revision.sha256,
            label: record.fact.key + ' · version ' + record.revision.version,
          }))
      : snapshot.outputs
          .filter((record) => available(record) && record.revision)
          .map((record) => ({
            input_kind: 'artifact',
            input_id: record.artifact.id,
            revision_id: record.revision.id,
            sha256: record.revision.sha256,
            label: record.revision.filename + ' · current version',
          }));
  }

  function addDependency(binding = null) {
    if ($('ng-dependency-rows').children.length >= 32) return;
    const row = node('div', undefined, 'ni-card');
    row.dataset.dependencyBinding = 'true';
    const kind = select(
      '',
      [
        ['fact', 'Fact or requirement'],
        ['artifact', 'Output revision'],
      ],
      binding?.input_kind || 'fact',
    );
    kind.dataset.inputKind = 'true';
    const identity = select('', []);
    identity.dataset.inputIdentity = 'true';
    const fill = () => {
      const choices = inputChoices(kind.value);
      if (
        binding?.input_kind === kind.value &&
        !choices.some(
          (item) => item.revision_id === binding.revision_id && item.input_id === binding.input_id,
        )
      )
        choices.push({
          ...binding,
          label:
            (kind.value === 'fact' ? factName(binding.input_id) : outputName(binding.input_id)) +
            ' · recorded historical revision',
        });
      identity.replaceChildren(
        ...choices.map((item) => {
          const option = node('option', item.label);
          option.value = item.input_id + ':' + item.revision_id;
          option.dataset.binding = JSON.stringify({
            input_kind: item.input_kind,
            input_id: item.input_id,
            revision_id: item.revision_id,
            sha256: item.sha256,
          });
          return option;
        }),
      );
      if (binding?.input_kind === kind.value)
        identity.value = binding.input_id + ':' + binding.revision_id;
    };
    fill();
    identity.required = true;
    kind.onchange = fill;
    const inputSelector = selectorFields('Input', binding?.input_selector);
    const outputSelector = selectorFields('Output', binding?.output_selector);
    inputSelector.dataset.inputSelector = 'true';
    outputSelector.dataset.outputSelector = 'true';
    row.append(
      field('Dependency input kind', kind),
      field('Exact input revision', identity),
      inputSelector,
      outputSelector,
      button('Remove dependency', () => removeRow(row)),
    );
    $('ng-dependency-rows').append(row);
    lock();
  }

  function editDeclaration(record) {
    if (
      !editor(
        'declaration',
        'Declare output dependencies',
        record.revision.filename + ' · SHA-256 ' + record.revision.sha256,
        record,
      )
    )
      return;
    const fields = $('ng-fields');
    fields.append(
      field(
        'Declared coverage',
        select(
          'ng-coverage',
          [
            ['unknown', 'Unknown'],
            ['partial', 'Partial'],
            ['complete', 'Complete'],
          ],
          coverage(record),
        ),
        'Complete coverage is an explicit owner attestation that every relevant input dependency is declared.',
      ),
      field(
        'Coverage notes',
        input('ng-coverage-notes', record.declaration?.notes || '', { rows: 3, maxLength: 2000 }),
      ),
      node('h4', 'Exact input bindings'),
      node('div'),
    );
    fields.lastChild.id = 'ng-dependency-rows';
    fields.append(
      button('Add dependency', () => {
        addDependency();
        dirty = true;
      }),
    );
    for (const binding of record.declaration?.dependencies || []) addDependency(binding);
    $('ng-save').textContent = 'Save dependency declaration';
    $('ng-coverage').focus();
    lock();
  }

  function editAssessment(record) {
    if (
      !editor(
        'assessment',
        'Assess a fact change',
        record.fact.key + ': compare exact old and new revisions.',
        record,
      )
    )
      return;
    const revisions = [...record.revisions].sort((first, second) => first.version - second.version);
    const values = revisions.map((revision) => [
      revision.id,
      'Version ' + revision.version + ' · ' + valueText(revision),
    ]);
    $('ng-fields').append(
      field(
        'Previous fact revision',
        select('ng-fact-before', values, revisions[revisions.length - 2].id),
      ),
      field(
        'Changed fact revision',
        select('ng-fact-after', values, record.fact.current_revision_id),
      ),
    );
    $('ng-save').textContent = 'Assess change impact';
    $('ng-fact-before').focus();
    lock();
  }

  function readPointer(text, pointer) {
    let value = JSON.parse(text);
    if (pointer && (!pointer.startsWith('/') || /~(?![01])/.test(pointer)))
      throw Error('Enter a valid JSON Pointer.');
    for (const token of pointer ? pointer.slice(1).split('/') : []) {
      const key = token.replaceAll('~1', '/').replaceAll('~0', '~');
      if (!value || typeof value !== 'object' || !Object.hasOwn(value, key))
        throw Error('The JSON field does not exist in this output.');
      value = value[key];
    }
    if (!precise(value))
      throw Error(
        'This field exceeds browser numeric precision. Use the API to preserve its exact value.',
      );
    return value;
  }

  function addReplacement(card, initial = null) {
    const rows = card.querySelector('[data-replacements]');
    if (rows.children.length >= 16) return;
    const row = node('div', undefined, 'ni-card');
    row.dataset.replacement = 'true';
    const selector = selectorFields(
      'Repair',
      initial?.selector ||
        JSON.parse(card.dataset.allowedSelectors)[0] || { kind: 'whole_file', path: '' },
    );
    selector.dataset.replacementSelector = 'true';
    const before = input('', '', { rows: 3, required: true });
    const after = input('', '', { rows: 3, required: true });
    before.dataset.before = 'true';
    after.dataset.after = 'true';
    const fill = () => {
      try {
        const target = readSelector(selector);
        const text = card.dataset.originalText;
        if (text === undefined) return;
        const value =
          target.kind === 'whole_file' ? text : JSON.stringify(readPointer(text, target.path));
        before.value = value;
        if (!after.value) after.value = value;
        feedback();
      } catch (error) {
        feedback(error.message, 'error');
      }
    };
    selector.addEventListener('change', fill);
    selector.querySelector('[data-selector-path]').addEventListener('input', fill);
    row.append(
      selector,
      field(
        'Exact value before repair',
        before,
        'Whole-file repairs replace the complete text. JSON field repairs use JSON values.',
      ),
      field('Replacement value', after),
      button('Remove replacement', () => removeRow(row)),
    );
    if (initial) {
      before.value =
        initial.selector.kind === 'whole_file' ? initial.before : JSON.stringify(initial.before);
      after.value =
        initial.selector.kind === 'whole_file' ? initial.after : JSON.stringify(initial.after);
    } else fill();
    rows.append(row);
    lock();
  }

  async function selectRepairOutput(card) {
    const checked = card.querySelector('[data-repair-selected]').checked;
    card.querySelector('[data-repair-fields]').hidden = !checked;
    for (const control of card.querySelectorAll('textarea')) control.required = checked;
    if (!checked || card.dataset.originalText !== undefined || busy || uncertain) return;
    const output = snapshot.outputs.find(
      (record) => record.artifact.id === card.dataset.artifactId,
    );
    const ticket = generation,
      viewTicket = viewGeneration;
    busy = true;
    lock();
    try {
      const preview = await bridge().request(
        '/v2/projects/' +
          projectId +
          '/artifacts/' +
          output.artifact.id +
          '/revisions/' +
          output.revision.id +
          '/preview',
      );
      if (!active(ticket) || viewTicket !== viewGeneration) return;
      if (typeof preview.text !== 'string')
        throw Error(
          'This browser composer requires a supported complete text preview. Use the API for other supported repairs.',
        );
      card.dataset.originalText = preview.text;
      if (!card.querySelector('[data-replacement]')) addReplacement(card);
      feedback();
    } catch (error) {
      if (active(ticket)) {
        card.querySelector('[data-repair-selected]').checked = false;
        card.querySelector('[data-repair-fields]').hidden = true;
        feedback(error.message, 'error');
      }
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  function editRepair(record) {
    if (
      !record.current ||
      !editor(
        'preview',
        'Prepare bounded repair',
        'Select affected outputs and specify exact replacements. A preview makes no file changes. JSON repairs preserve other data values; the edited file’s formatting may change.',
        record,
      )
    )
      return;
    $('ng-fields').append(
      field(
        'Repair rationale',
        input('ng-repair-rationale', '', {
          rows: 3,
          minLength: 10,
          maxLength: 2000,
          required: true,
        }),
      ),
      node('div'),
    );
    $('ng-fields').lastChild.id = 'ng-repair-outputs';
    for (const impact of record.assessment.outputs.filter(
      (output) => output.status === 'affected',
    )) {
      const output = snapshot.outputs.find((item) => item.artifact.id === impact.artifact_id);
      if (!output || !available(output) || !output.revision) continue;
      const card = node('div', undefined, 'ni-card');
      card.dataset.artifactId = output.artifact.id;
      card.dataset.allowedSelectors = JSON.stringify(impact.selectors);
      const checkbox = input('', '', { type: 'checkbox' });
      checkbox.dataset.repairSelected = 'true';
      const fields = node('div');
      fields.dataset.repairFields = 'true';
      fields.hidden = true;
      const rows = node('div');
      rows.dataset.replacements = 'true';
      fields.append(
        rows,
        button('Add replacement', () => {
          addReplacement(card);
          dirty = true;
        }),
      );
      card.append(
        field('Repair ' + output.revision.filename, checkbox),
        node('p', impact.reasons.join(' '), 'np-field-help'),
        fields,
      );
      checkbox.onchange = () => selectRepairOutput(card);
      $('ng-repair-outputs').append(card);
    }
    $('ng-save').textContent = 'Preview exact repairs';
    $('ng-repair-rationale').focus();
    lock();
  }

  function typedValue(text, type) {
    const value = type === 'string' ? text : JSON.parse(text);
    if (
      (type === 'number' && typeof value !== 'number') ||
      (type === 'boolean' && typeof value !== 'boolean')
    )
      throw Error('The fact value must match its selected type.');
    if (!precise(value)) throw Error('Use the API for integers outside browser numeric precision.');
    return value;
  }

  function buildOperation() {
    const kind = editing.kind;
    if (kind === 'fact')
      return {
        kind,
        suffix: '/facts',
        body: {
          fact_id: editing.record?.fact.id || null,
          key: $('ng-fact-key').value,
          kind: $('ng-fact-kind').value,
          expected_version: editing.expectedVersion,
          value_type: $('ng-value-type').value,
          value: typedValue($('ng-fact-value').value, $('ng-value-type').value),
          unit: $('ng-fact-unit').value,
          sources: [...$('ng-sources').querySelectorAll('[data-source-binding]')]
            .filter((row) => row.querySelector('[data-source-id]').value)
            .map((row) => {
              const source = sources.find(
                (item) => item.id === row.querySelector('[data-source-id]').value,
              );
              if (!source || source.revoked_at) throw Error('Choose a permitted source revision.');
              return {
                source_revision_id: source.id,
                sha256: source.sha256,
                selector: readSelector(row.querySelector('[data-source-selector]')),
              };
            }),
        },
      };
    if (kind === 'declaration')
      return {
        kind,
        suffix: '/outputs/' + editing.record.artifact.id,
        body: {
          expected_version: editing.record.declaration?.version || 0,
          expected_artifact_version: editing.record.artifact.version,
          revision_id: editing.record.revision.id,
          sha256: editing.record.revision.sha256,
          coverage: $('ng-coverage').value,
          notes: $('ng-coverage-notes').value,
          dependencies: [
            ...$('ng-dependency-rows').querySelectorAll('[data-dependency-binding]'),
          ].map((row) => {
            const option = row.querySelector('[data-input-identity]').selectedOptions[0];
            if (!option)
              throw Error('Select a current or explicitly recorded historical input revision.');
            return {
              ...JSON.parse(option.dataset.binding),
              input_selector: readSelector(row.querySelector('[data-input-selector]')),
              output_selector: readSelector(row.querySelector('[data-output-selector]')),
            };
          }),
        },
      };
    if (kind === 'assessment') {
      if ($('ng-fact-before').value === $('ng-fact-after').value)
        throw Error('Select two distinct fact revisions.');
      return {
        kind,
        suffix: '/assessments',
        body: {
          fact_id: editing.record.fact.id,
          from_revision_id: $('ng-fact-before').value,
          to_revision_id: $('ng-fact-after').value,
        },
      };
    }
    const cards = [...$('ng-repair-outputs').children].filter(
      (card) => card.querySelector('[data-repair-selected]').checked,
    );
    if (!cards.length || cards.length > 10) throw Error('Select one to ten affected outputs.');
    return {
      kind: 'preview',
      suffix: '/repairs',
      body: {
        assessment_id: editing.record.assessment.id,
        rationale: $('ng-repair-rationale').value,
        edits: cards.map((card) => {
          const output = snapshot.outputs.find(
            (record) => record.artifact.id === card.dataset.artifactId,
          );
          return {
            artifact_id: output.artifact.id,
            expected_version: output.artifact.version,
            revision_id: output.revision.id,
            sha256: output.revision.sha256,
            replacements: [...card.querySelectorAll('[data-replacement]')].map((row) => {
              const selector = readSelector(row.querySelector('[data-replacement-selector]'));
              return {
                selector,
                before: typedValue(
                  row.querySelector('[data-before]').value,
                  selector.kind === 'whole_file' ? 'string' : 'json',
                ),
                after: typedValue(
                  row.querySelector('[data-after]').value,
                  selector.kind === 'whole_file' ? 'string' : 'json',
                ),
              };
            }),
          };
        }),
      },
    };
  }

  async function afterSuccess(operation, result) {
    const ticket = generation;
    const identifier =
      result.id ||
      result.fact_id ||
      result.assessment_id ||
      result.proposal_id ||
      result.fact?.id ||
      result.assessment?.id ||
      result.proposal?.id;
    if (operation.kind === 'reassess') {
      if (!(await refresh()) || !active(ticket) || editing !== operation.editor) return;
      const record = await bridge().request(path('/assessments/' + identifier));
      if (!active(ticket) || editing !== operation.editor) return;
      if (!available(record)) {
        showAssessment(record);
        return;
      }
      editing.record = record;
      conflict = false;
      $('ng-latest').replaceChildren(
        node(
          'p',
          'A new current assessment is recorded. Your replacement values are preserved; verify their exact before values against the updated heads before previewing.',
        ),
      );
      feedback(
        'Current state reviewed. Your draft remains available for verification before submitting again.',
      );
      return;
    }
    overview();
    if (!(await refresh()) || !active(ticket)) return;
    const kind =
      operation.kind === 'preview' || operation.kind === 'commit' ? 'repair' : operation.kind;
    const id = kind === 'repair' && operation.kind === 'commit' ? operation.targetId : identifier;
    if (['fact', 'assessment', 'repair'].includes(kind) && id) {
      const record = await bridge().request(
        path(
          '/' + { fact: 'facts', assessment: 'assessments', repair: 'repairs' }[kind] + '/' + id,
        ),
      );
      if (!active(ticket)) return;
      ({ fact: showFact, assessment: showAssessment, repair: showRepair })[kind](record);
    }
    feedback(
      operation.kind === 'commit'
        ? 'Repair committed atomically. New revisions need current checks and review; remaining affected outputs stay stale.'
        : 'Saved. The current evidence is shown below.',
      'success',
    );
  }

  async function perform() {
    if (busy || !pending) return;
    const ticket = generation,
      operation = pending;
    busy = true;
    lock();
    feedback(
      operation.kind === 'commit' ? 'Committing exact repairs…' : 'Saving current evidence…',
    );
    try {
      const result = await bridge().request(operation.url, operation.body, 'POST');
      if (!active(ticket)) return;
      pending = null;
      uncertain = receiptAbsent = false;
      try {
        await afterSuccess(operation, result);
      } catch (error) {
        if (active(ticket))
          feedback(
            'The request was saved. Refresh to inspect its result. ' + error.message,
            'warning',
          );
      }
    } catch (error) {
      if (!active(ticket)) return;
      if (!error.status || error.status >= 500) {
        uncertain = true;
        receiptAbsent = false;
        feedback(
          'The response is uncertain. Check the saved request before making another change.',
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
        feedback(error.message + ' Your draft is preserved.', 'error');
        if (conflict && operation.kind === 'commit')
          $('ng-conflict-note').textContent =
            'This immutable preview cannot overwrite newer work. Its proposed changes remain visible; create a current assessment and a new preview before committing.';
      }
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  async function submitOperation(operation) {
    if (busy || uncertain || !snapshot?.can_manage) return;
    pending = {
      ...operation,
      url: path(operation.suffix),
      body: { ...operation.body, idempotency_key: crypto.randomUUID() },
    };
    await perform();
  }

  async function submitAssessment(assessment) {
    await submitOperation({
      kind: 'assessment',
      suffix: '/assessments',
      body: {
        fact_id: assessment.fact_id,
        from_revision_id: assessment.from_revision_id,
        to_revision_id: assessment.to_revision_id,
      },
    });
  }

  async function commit(record) {
    if (!record.can_commit || !available(record) || !record.current) return;
    await submitOperation({
      kind: 'commit',
      suffix: '/repairs/' + record.proposal.id + '/commit',
      targetId: record.proposal.id,
      body: { proposal_sha256: record.proposal.sha256 },
    });
  }

  async function checkOperation() {
    if (busy || !pending || !uncertain) return;
    const ticket = generation,
      operation = pending;
    busy = true;
    lock();
    try {
      const result = await bridge().request(
        path('/operations/' + encodeURIComponent(operation.body.idempotency_key)),
      );
      if (!active(ticket)) return;
      pending = null;
      uncertain = receiptAbsent = false;
      await afterSuccess(operation, result);
    } catch (error) {
      if (!active(ticket)) return;
      if (error.status === 404) {
        receiptAbsent = true;
        feedback(
          'No receipt is recorded yet. Retry the same saved request to resolve it safely.',
          'warning',
        );
      } else if ([401, 403].includes(error.status)) failure(error);
      else feedback('The request remains uncertain. ' + error.message, 'warning');
    } finally {
      if (active(ticket)) {
        busy = false;
        lock();
      }
    }
  }

  async function rebase() {
    if (busy || uncertain || !conflict) return;
    const ticket = generation,
      original = editing;
    busy = true;
    lock();
    try {
      if (!(await refresh()) || !active(ticket)) return;
      if (!original) {
        const record = await bridge().request(path('/repairs/' + detail.record.proposal.id));
        if (!active(ticket)) return;
        showRepair(record);
        feedback(
          'Inspect the retained changes. Reassess the input change before creating a new preview.',
          'warning',
        );
        return;
      }
      if (original.kind === 'fact') {
        latest = original.record
          ? await bridge().request(path('/facts/' + original.record.fact.id))
          : null;
        if (!active(ticket)) return;
        if (latest && !available(latest)) {
          showFact(latest);
          return;
        }
        original.expectedVersion = latest?.fact.version || 0;
        $('ng-latest').replaceChildren(
          node(
            'p',
            latest
              ? 'Current fact version ' +
                  latest.fact.version +
                  '. Your typed draft remains unchanged.'
              : 'Review the current input keys before saving your new fact.',
          ),
        );
      } else if (original.kind === 'declaration') {
        const current = snapshot.outputs.find(
          (record) => record.artifact.id === original.record.artifact.id,
        );
        if (!current || !available(current)) {
          overview();
          renderOverview();
          return;
        }
        original.record = current;
        $('ng-editor-context').textContent =
          current.revision.filename + ' · Current SHA-256 ' + current.revision.sha256;
        $('ng-latest').replaceChildren(
          node(
            'p',
            'Your dependency draft now targets the current output version. Review every selector against its current content.',
          ),
        );
      } else if (original.kind === 'preview') {
        const assessment = original.record.assessment;
        busy = false;
        await submitOperation({
          kind: 'reassess',
          editor: original,
          suffix: '/assessments',
          body: {
            fact_id: assessment.fact_id,
            from_revision_id: assessment.from_revision_id,
            to_revision_id: assessment.to_revision_id,
          },
        });
        return;
      } else throw Error('Create another assessment from the current fact revisions.');
      conflict = false;
      feedback(
        'Current state reviewed. Your draft remains available for verification before submitting again.',
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
    projectId = workspaceId = snapshot = detail = editing = pending = latest = null;
    busy = uncertain = receiptAbsent = dirty = conflict = false;
    sources = [];
    $('ng-form').reset();
    for (const id of [
      'ng-project',
      'ng-access',
      'ng-facts',
      'ng-outputs',
      'ng-assessments',
      'ng-repairs',
      'ng-detail-heading',
      'ng-detail-summary',
      'ng-detail-content',
      'ng-detail-actions',
      'ng-editor-context',
      'ng-fields',
      'ng-latest',
    ])
      $(id).replaceChildren();
    feedback();
    if (dialog.open) dialog.close();
  }

  async function open(target = null) {
    const project = bridge().getProject();
    if (!project || dialog.open) return;
    reset();
    projectId = project.id;
    workspaceId = bridge().getWorkspaceId();
    const ticket = ++generation;
    $('ng-project').textContent = project.name;
    dialog.showModal();
    overview();
    busy = true;
    lock();
    $('ng-heading').focus();
    feedback('Loading facts and exact output dependencies…');
    try {
      if (!(await refresh()) || !active(ticket)) return;
      if (target?.artifactId) {
        const record = snapshot.outputs.find((item) => item.artifact.id === target.artifactId);
        if (record) showOutput(record, target.revisionId);
      }
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

  function close() {
    if (busy || uncertain || editing) return;
    reset();
    if (bridge().getProject()) bridge().onTeamChanged();
  }

  $('ng-form').addEventListener('input', () => {
    dirty = true;
    lock();
  });
  $('ng-form').onsubmit = async (event) => {
    event.preventDefault();
    if (
      busy ||
      uncertain ||
      conflict ||
      !editing ||
      !snapshot?.can_manage ||
      !event.currentTarget.reportValidity()
    )
      return;
    try {
      await submitOperation(buildOperation());
    } catch (error) {
      feedback(error.message, 'error');
    }
  };
  $('np-dependencies').onclick = () => open();
  $('ng-close').onclick = close;
  $('ng-back').onclick = () => {
    if (!busy && !uncertain) {
      overview();
      feedback();
    }
  };
  $('ng-cancel').onclick = () => {
    if (!busy && !uncertain) {
      overview();
      renderOverview();
      feedback();
    }
  };
  $('ng-fact-new').onclick = () => editFact();
  $('ng-check').onclick = checkOperation;
  $('ng-retry').onclick = perform;
  $('ng-rebase').onclick = rebase;
  $('ng-refresh').onclick = async () => {
    if (busy || uncertain || dirty) return;
    const ticket = generation;
    busy = true;
    lock();
    try {
      await refresh();
      if (active(ticket)) feedback('Facts and dependencies refreshed.');
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
    close();
  });
  window.addEventListener('beforeunload', (event) => {
    if (busy || uncertain || dirty) event.preventDefault();
  });
  window.addEventListener('native-project-loading', (event) => {
    if (projectId && event.detail.id !== projectId) reset();
  });
  window.addEventListener('native-project-unavailable', reset);
  window.SimonNativeDependencies = { open };
})();
