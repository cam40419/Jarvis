# Model management plan

Accepted September 13, 2026. Prioritize a responsive everyday assistant before tool integration.

## Phase A: profiles and interaction (implemented)

- Quick, Balanced, Deep, and transparent rule-based Auto selection per message.
- Separate Brief, Normal, and Detailed answer length.
- Server-approved model/settings combinations and immutable selection records.
- Live output streaming, Stop, and recovery through canonical saved history.
- Think deeper creates a linked new run and preserves the original answer.
- Record routing reason, token usage, reasoning usage, and latency measurements.
- Maintain session/household checks, bounded generation, and duplicate protection.

Verified September 13, 2026: 181 offline tests passed with 96.61% coverage, plus a separate live
browser check of Quick, saved memory, reload, and linked Deep generation. The offline browser test
also exercises Stop and draft retention; both stores cover stream disconnection and cancellation.
See [using the assistant](runbooks/assistant.md) for profile settings and repeatable checks.

## Phase B: budgets and evidence (in progress)

The Simon UI and automatic configuration slice was brought forward at the user's request:
full chat layout, searchable threads, theme/mobile support, safe Markdown, memory panel, and routing
v2 with automatic model, none/low/medium/high reasoning, and answer length. Auto Deep is now on by
default. Manual overrides remain available. A short follow-up can inherit cues from recent user
messages. No extra routing model call, automatic second generation, or spending limit was added.

Simon verification: 195 offline tests passed with 96.68% coverage, plus the live Auto/Deep browser
check. Desktop/mobile screenshots and the repeatable checks are listed in the assistant runbook.

- Per-household spending limits with reservations for concurrent requests.
- [x] Personal response defaults and an Auto-may-use-Deep preference, scoped to the active household.
- [x] Helpful / Too slow / Needs more depth feedback on saved answers, with change/clear controls.
- [ ] Visible cost estimates and no silent expensive fallback.
- Evaluate representative household planning, writing, coding, and factual tasks.
- Select and adjust profiles using observed quality, cost, and latency.
- Reuse provider connections and measure context selection/token-count overhead.

September 14 interaction slice: preferences and feedback are persisted with versions, idempotent
updates, audit/outbox records, and rollback tests in both stores. Saved defaults initialize the chat's
mode and answer-length controls; the personal Auto Deep preference is enforced by the model service
and cannot override a server-level automatic Deep restriction. Existing requests replay their original
snapshots. Feedback is personal to the active household and applies to any saved answer; it does not
trigger regeneration, change routing automatically, or enter model context. Migration 0006 adds two
tables without changing old snapshots. Spending reservations, cost estimates, and evaluation reports
remain the next work in this phase.

Verification: 207 offline tests passed with 96.89% coverage. The browser covered saved defaults,
the Auto Deep toggle, feedback changes/clearing, and reload; process-restart persistence also passed.
Migration 0006 is applied locally, and the wheel includes it. No paid model request was needed.

## Phase C: adaptive routing

- More selective context retrieval and task-aware routing informed by evaluations.
- Consider a classifier only if its quality benefit justifies the extra request latency.
- Escalate on measurable failures within an approved budget, not model confidence alone.
- Keep provider-failure fallback separate from quality escalation; disclose fallbacks.
- Advanced model controls and persisted per-user preferences once defaults are validated.

No automatic second generation, cross-provider fallback, or daily spending guarantees are introduced
in Phase A. Live partial text is provisional; completed answers remain the canonical durable records.


## Connected capabilities: brought forward September 14, 2026

At the user's request, the first capability slice now precedes the remaining budget work:

- [x] Public web search/open-page support through Responses, with saved citations and source links.
- [x] Bounded function rounds with token counting, no generation retries, and recorded tool calls.
- [x] Google OAuth, encrypted token storage, local setup import, and Connections UI.
- [x] Primary calendar reads; email and new-event previews with explicit confirmation and receipts.
- [x] Durable single-dispatch claims, account binding, expiry, cancellation, and unknown outcomes.
- [x] Browser timezone supplied automatically for scheduling.
- [x] Reservation discovery and email request preparation.
- [ ] Provider-specific booking adapters or controlled website form automation.
- [ ] Inbox reading, calendar edits/recurrence/invitations, attachments, and background execution.

The Google connection requires a user-configured OAuth client and account consent. Automated tests
simulate Google and do not send real emails or events. Web search is tested separately against the
live provider. Tool access does not add a spending guarantee; the remaining Phase B budget work is
still needed. See [Google setup](runbooks/google.md) for the concrete capability boundaries.

Verification: 264 offline cases verified, 96.18% regression coverage, two separate synthetic live
web/function-loop checks, and desktop/mobile review-card checks. Migration 0007 and a 26-table
backup/restore passed. The Google account has not been connected or exercised against real email
or calendar data; that requires the user's local OAuth configuration and consent.

## Custom home tools: brought forward September 14, 2026

The user's next priority is LIFX Beam, Smart Life hexagons, and planned Shelly Plug US Gen4
outlets. Adapters, automatic cloud discovery, persistent rooms/groups, device status UI, and
confirmed on/off or brightness previews are implemented. September 15 live read-only checks
found Beam, Up Arrow and Down Arrow. Physical switching remains pending.
See the [home integration plan](home-integration-plan.md)
for controls, limits, provider references, and the staged rollout. The remaining budget and
routing work above is still outstanding.

## September 16: live voice integration

GPT-Live handles the spoken conversation and delegates tasks through the existing Auto
response selection. It does not replace the text model or change saved text preferences.
Call duration limits and recorded usage are implemented; account-wide budgets and measured
latency tuning remain planned. See [next phases](next-phases.md).
