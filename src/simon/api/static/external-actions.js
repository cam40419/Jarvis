'use strict';
(() => {
  const anchor = document.getElementById('work-status');
  if (!anchor) return;
  const node = (tag, text, className) => {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    if (className) result.className = className;
    return result;
  };
  const root = node('section', undefined, 'external-actions');
  root.id = 'external-actions';
  root.setAttribute('aria-labelledby', 'ea-heading');
  const heading = node('div', undefined, 'ea-heading');
  const introduction = node('div');
  introduction.append(
    node('p', 'BOOKINGS, ORDERS & PHONE MESSAGES', 'eyebrow'),
    node('h2', 'Review your next commitment.'),
  );
  introduction.lastChild.id = 'ea-heading';
  introduction.append(
    node(
      'p',
      'Your agents prepare the details. Review the recipient, price and terms before submitting.',
      'muted',
    ),
  );
  const reload = node('button', 'Refresh external actions');
  reload.type = 'button';
  const headingActions = node('div', undefined, 'ea-controls');
  const returnToProject = node('button', 'Return to project');
  returnToProject.type = 'button';
  returnToProject.hidden = true;
  headingActions.append(returnToProject, reload);
  heading.append(introduction, headingActions);
  const status = node('p', '', 'ea-status');
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  const split = node('div', undefined, 'ea-split');
  const list = node('nav', undefined, 'ea-list');
  list.setAttribute('aria-label', 'External action requests');
  const detail = node('article', undefined, 'ea-detail');
  detail.setAttribute('aria-label', 'Review selected request');
  split.append(list, detail);
  const providerSection = node('details', undefined, 'ea-providers');
  providerSection.append(node('summary', 'Provider connections'));
  const providerBody = node('div');
  providerSection.append(providerBody);
  root.append(heading, status, split, providerSection);
  (document.getElementById('project-command') || anchor).after(root);
  const labels = {
    pending: 'Needs your review',
    executing: 'Submitting',
    accepted: 'Provider accepted',
    succeeded: 'Confirmed by provider',
    failed: 'Not completed',
    unknown: 'Outcome needs review',
    cancelled: 'Proposal cancelled',
    expired: 'Review expired',
    resolved: 'User reconciled',
  };
  const kinds = {
    purchase: 'Order',
    booking: 'Booking',
    reservation: 'Reservation',
    call: 'Phone message',
  };
  let rows = [],
    providers = [],
    selected = null,
    loading = false,
    submitting = false,
    timer = null,
    fingerprint = '',
    generation = 0,
    pageOffset = 0;
  const visible = () => !document.hidden && !document.getElementById('work-view')?.hidden;
  const setStatus = (message, error = false) => {
    status.textContent = message;
    status.classList.toggle('error', error);
  };
  const button = (label, callback, className) => {
    const value = node('button', label, className);
    value.type = 'button';
    value.onclick = async () => {
      value.disabled = true;
      try {
        await callback();
      } catch (error) {
        setStatus(error.message, true);
      } finally {
        if (value.isConnected) value.disabled = false;
      }
    };
    return value;
  };
  const amount = (value) => {
    if (!value) return '';
    try {
      const formatter = new Intl.NumberFormat(undefined, {
        style: 'currency',
        currency: value.currency,
      });
      const digits = formatter.resolvedOptions().maximumFractionDigits;
      return formatter.format(value.amount_minor / 10 ** digits);
    } catch (_) {
      return value.amount_minor + ' minor units ' + value.currency;
    }
  };
  const time = (value) =>
    new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  const fact = (parent, label, value) => {
    const row = node('div', undefined, 'ea-fact');
    row.append(node('dt', label), node('dd', String(value)));
    parent.append(row);
  };
  function renderProviders() {
    providerBody.replaceChildren();
    if (!providers.length) {
      providerBody.append(
        node(
          'p',
          'No booking, ordering or calling providers are configured for this account. Requests can be saved for review.',
        ),
      );
      return;
    }
    providers.forEach((provider) => {
      const row = node('div', undefined, 'ea-provider');
      row.append(
        node('strong', provider.name),
        node('span', provider.available ? 'Ready' : 'Needs setup', 'ea-badge'),
      );
      if (provider.from_number) row.append(node('p', 'Calling number: ' + provider.from_number));
      (provider.blockers || []).forEach((message) => row.append(node('p', message, 'muted')));
      providerBody.append(row);
    });
  }
  function render() {
    list.replaceChildren();
    detail.replaceChildren();
    setStatus(
      rows.length
        ? rows.filter((row) => row.status === 'pending').length +
            ' request(s) waiting for review on this page.'
        : '',
    );
    renderProviders();
    root.classList.toggle('ea-empty', !rows.length);
    split.hidden = !rows.length;
    introduction.querySelector('h2').textContent = rows.length
      ? 'Review your next commitment.'
      : 'Bookings, orders and phone messages';
    introduction.querySelector('.muted').textContent = rows.length
      ? 'Your agents prepare the details. Review the recipient, price and terms before submitting.'
      : 'No requests to review. Provider setup is available below.';
    if (!rows.length) {
      list.append(node('p', 'No requests yet.', 'muted'));
      detail.append(
        node('h3', 'A clear review before you commit.'),
        node(
          'p',
          'Ask an agent to prepare an order, booking, reservation or phone message. Its exact details and provider status will appear here.',
        ),
      );
      return;
    }
    if (!rows.some((row) => row.id === selected)) selected = rows[0].id;
    rows.forEach((row) => {
      const item = button(
        '',
        () => {
          selected = row.id;
          render();
        },
        'ea-request',
      );
      item.setAttribute('aria-current', String(row.id === selected));
      item.append(
        node('span', kinds[row.draft.kind], 'ea-kind'),
        node('strong', row.draft.summary),
        node('span', row.draft.recipient_label, 'muted'),
        node('span', labels[row.status] || row.status, 'ea-badge ea-' + row.status),
      );
      list.append(item);
    });
    const pagination = node('div', undefined, 'ea-controls');
    if (pageOffset)
      pagination.append(
        button('Newer requests', async () => {
          pageOffset = Math.max(0, pageOffset - 50);
          fingerprint = '';
          await refresh();
        }),
      );
    if (rows.length === 50)
      pagination.append(
        button('Older requests', async () => {
          pageOffset += 50;
          fingerprint = '';
          await refresh();
        }),
      );
    list.append(pagination);
    const action = rows.find((row) => row.id === selected),
      draft = action.draft;
    detail.append(
      node('span', labels[action.status] || action.status, 'ea-badge ea-' + action.status),
      node('h3', draft.summary),
    );
    const facts = node('dl', undefined, 'ea-facts');
    fact(facts, 'Recipient', draft.recipient_label);
    fact(facts, draft.kind === 'call' ? 'Phone number' : 'Merchant identifier', draft.recipient_id);
    fact(facts, 'Provider', action.provider_name);
    if (action.from_number) fact(facts, 'Calling from', action.from_number);
    const total = draft.purchase?.total || draft.reservation?.total;
    if (total) fact(facts, 'Total including quoted charges', amount(total));
    if (draft.purchase) fact(facts, 'Delivery / collection', draft.purchase.fulfillment);
    if (draft.reservation) {
      fact(facts, 'Starts', time(draft.reservation.start_at));
      fact(facts, 'Ends', time(draft.reservation.end_at));
      fact(facts, 'Location', draft.reservation.location);
      fact(facts, 'Party size', draft.reservation.party_size);
      fact(facts, 'Display timezone', Intl.DateTimeFormat().resolvedOptions().timeZone);
    }
    if (draft.call)
      fact(facts, 'Maximum call duration', draft.call.max_duration_seconds + ' seconds');
    detail.append(facts);
    if (draft.purchase) {
      const items = node('ul', undefined, 'ea-items');
      draft.purchase.items.forEach((item) =>
        items.append(
          node('li', item.quantity + ' × ' + item.description + ' (' + item.item_id + ')'),
        ),
      );
      detail.append(node('h4', 'Items'), items);
    }
    if (draft.call) {
      detail.append(
        node('h4', 'Exact spoken message'),
        node('p', draft.call.message, 'ea-copy'),
        node(
          'p',
          'This is a prerecorded outbound message. Provider acceptance does not establish that a person answered or understood it. Voice charges may apply.',
          'muted',
        ),
      );
    }
    detail.append(node('h4', 'Terms'), node('p', draft.terms, 'ea-copy'));
    if (action.provider_id)
      detail.append(node('p', 'Provider reference: ' + action.provider_id, 'ea-reference'));
    if (action.outcome) detail.append(node('p', action.outcome, 'ea-outcome'));
    if (action.error) detail.append(node('p', action.error, 'ea-attention'));
    if (action.manual_resolution) {
      detail.append(
        node('h4', 'User reconciliation'),
        node('p', action.manual_resolution.evidence, 'ea-copy'),
        node('p', action.manual_resolution.note, 'ea-copy'),
        node(
          'p',
          'Recorded ' +
            time(action.manual_resolution.recorded_at) +
            '. No replacement request was submitted.',
          'muted',
        ),
      );
    }
    if (action.status === 'unknown') {
      detail.append(
        node(
          'p',
          'Check the provider records before preparing another request. This action will not be automatically repeated.',
          'ea-attention',
        ),
      );
      const form = node('form', undefined, 'ea-reconcile');
      const choice = node('select');
      choice.required = true;
      [
        ['', 'Choose the outcome verified with the provider'],
        ['completed', 'Provider records show it completed'],
        ['not_completed', 'Provider records show it did not complete'],
      ].forEach(([value, label]) => {
        const option = node('option', label);
        option.value = value;
        choice.append(option);
      });
      const evidence = node('textarea');
      evidence.required = true;
      evidence.minLength = 10;
      evidence.maxLength = 1500;
      evidence.rows = 3;
      const note = node('textarea');
      note.required = true;
      note.minLength = 10;
      note.maxLength = 1500;
      note.rows = 3;
      [
        ['Verified outcome', choice],
        ['Provider evidence or reference', evidence],
        ['Reconciliation note', note],
      ].forEach(([title, field]) => {
        const label = node('label', title);
        label.append(field);
        form.append(label);
      });
      const save = node('button', 'Record user reconciliation');
      save.type = 'submit';
      form.append(
        node(
          'p',
          'Record what you checked directly with the provider. This records your report and does not submit another order, booking or call.',
          'muted',
        ),
        save,
      );
      form.onsubmit = async (event) => {
        event.preventDefault();
        if (!form.reportValidity() || submitting) return;
        submitting = true;
        save.disabled = true;
        try {
          const updated = await api('/v1/external-actions/' + action.id + '/reconcile', {
            review_digest: action.review_digest,
            reported_outcome: choice.value,
            evidence: evidence.value,
            note: note.value,
          });
          rows = rows.map((row) => (row.id === updated.id ? updated : row));
          render();
          setStatus('User reconciliation recorded.');
        } catch (error) {
          setStatus(error.message, true);
        } finally {
          submitting = false;
          if (save.isConnected) save.disabled = false;
        }
      };
      detail.append(node('h4', 'Resolve after checking provider records'), form);
    }
    if (action.status === 'executing') {
      detail.append(
        node(
          'p',
          'Submission has been claimed. A delayed response is not permission to submit it again.',
          'muted',
        ),
      );
      if (action.claimed_at && Date.now() - Date.parse(action.claimed_at) >= 300000)
        detail.append(
          button('Mark interrupted submission for review', async () => {
            const updated = await api(
              '/v1/external-actions/' + action.id + '/mark-interrupted',
              {},
            );
            rows = rows.map((row) => (row.id === updated.id ? updated : row));
            render();
          }),
        );
    }
    if (action.status === 'accepted')
      detail.append(
        button('Check provider status', async () => {
          const updated = await api('/v1/external-actions/' + action.id + '/refresh', {});
          rows = rows.map((row) => (row.id === updated.id ? updated : row));
          render();
        }),
      );
    if (action.status !== 'pending') return;
    const expired = new Date(action.expires_at) <= new Date();
    if (expired)
      detail.append(
        node(
          'p',
          'This review has expired. Prepare a fresh request with current terms.',
          'ea-attention',
        ),
      );
    (action.blockers || []).forEach((message) => detail.append(node('p', message, 'ea-attention')));
    const approval = node('label', undefined, 'ea-approval'),
      check = node('input');
    check.type = 'checkbox';
    check.disabled = expired || !!action.blockers.length;
    approval.append(
      check,
      node('span', 'I reviewed these exact details and authorize this submission.'),
    );
    const submitLabel =
      draft.kind === 'call'
        ? 'Place reviewed call'
        : draft.kind === 'purchase'
          ? 'Place reviewed order'
          : 'Submit reviewed ' + draft.kind;
    const submit = button(
      submitLabel,
      async () => {
        if (!check.checked || submitting) return;
        submitting = true;
        check.disabled = true;
        try {
          const updated = await api('/v1/external-actions/' + action.id + '/confirm', {
            review_digest: action.review_digest,
          });
          rows = rows.map((row) => (row.id === updated.id ? updated : row));
          render();
          setStatus(labels[updated.status] || updated.status);
        } catch (error) {
          // A lost HTTP response may follow a committed provider submission.
          // Fetch durable state before allowing any further review interaction.
          try {
            const updated = await api('/v1/external-actions/' + action.id);
            rows = rows.map((row) => (row.id === updated.id ? updated : row));
            render();
          } catch (_) {
            check.checked = false;
          }
          throw error;
        } finally {
          submitting = false;
        }
      },
      'primary',
    );
    submit.disabled = true;
    check.onchange = () => {
      submit.disabled = !check.checked || submitting;
    };
    const controls = node('div', undefined, 'ea-controls');
    controls.append(
      submit,
      button('Cancel proposal', async () => {
        const updated = await api('/v1/external-actions/' + action.id + '/cancel', {
          review_digest: action.review_digest,
        });
        rows = rows.map((row) => (row.id === updated.id ? updated : row));
        render();
      }),
    );
    detail.append(
      approval,
      controls,
      node('p', 'Review expires ' + time(action.expires_at) + '.', 'muted'),
    );
  }
  async function refresh() {
    if (loading || submitting || typeof session === 'undefined' || !session) return;
    loading = true;
    reload.disabled = true;
    const revision = ++generation;
    try {
      const result = await Promise.all([
        api('/v1/external-actions?limit=50&offset=' + pageOffset),
        api('/v1/external-actions/providers'),
      ]);
      if (revision !== generation) return;
      if (!result[0].length && pageOffset) {
        pageOffset = Math.max(0, pageOffset - 50);
        result[0] = await api('/v1/external-actions?limit=50&offset=' + pageOffset);
      }
      const temporalState = result[0].map((row) =>
        row.status === 'pending'
          ? Date.parse(row.expires_at) <= Date.now()
          : row.status === 'executing' && Date.now() - Date.parse(row.claimed_at) >= 300000,
      );
      const next = JSON.stringify([result, temporalState]);
      if (next !== fingerprint) {
        [rows, providers] = result;
        fingerprint = next;
        render();
      }
      setStatus(
        rows.filter((row) => row.status === 'pending').length +
          ' request(s) waiting for review on this page.',
      );
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      loading = false;
      reload.disabled = false;
      clearTimeout(timer);
      if (visible()) timer = setTimeout(refresh, 20000);
    }
  }
  reload.onclick = refresh;
  window.addEventListener('simon-ready', () => {
    if (visible()) refresh();
  });
  document.getElementById('work-refresh')?.addEventListener('click', refresh);
  new MutationObserver(() => {
    if (visible()) refresh();
    else clearTimeout(timer);
  }).observe(document.getElementById('work-view'), {
    attributes: true,
    attributeFilter: ['hidden'],
  });
  document.addEventListener('visibilitychange', () => {
    if (visible()) refresh();
    else clearTimeout(timer);
  });
  window.simonExternalActions = {
    refresh,
    open: async (id) => {
      const projectId = new URL(window.location.href).searchParams.get('project');
      if (projectId) {
        returnToProject.hidden = false;
        returnToProject.onclick = () => {
          returnToProject.hidden = true;
          window.SimonWork?.openProject(projectId);
        };
      }
      window.SimonWork?.showOverview();
      root.hidden = false;
      const action = await api('/v1/external-actions/' + encodeURIComponent(id));
      selected = action.id;
      if (!rows.some((row) => row.id === action.id)) rows.unshift(action);
      else rows = rows.map((row) => (row.id === action.id ? action : row));
      render();
      root.scrollIntoView({ behavior: 'smooth' });
      detail.tabIndex = -1;
      detail.focus({ preventScroll: true });
    },
  };
  render();
})();
