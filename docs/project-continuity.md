# Project continuity and calendar schedules

Project requests, questions and calendar rules are durable, owner-scoped records in
PostgreSQL. They share the existing project coordinator and dispatcher. They do not
require a browser tab to remain open. Agent execution and project autonomy must be
enabled on the server for queued work to advance.

## Requests and questions

The project inbox accepts follow-up requests while another request is active.
Requests run one at a time per project; individual assignments within a run retain
the team's configured parallelism. Submitting the same idempotency key and content
does not create another request. Cancelling an unstarted request does not cancel a
running cycle. A blocked queued request can be retried after its access/setup issue
is corrected, with its original scoped authorization.

A planning response that needs owner input becomes a saved question. It releases
the active cycle and does not pause unrelated queued requests. Replying retains the
original instruction and exact reply, creates one linked request atomically, and
rejects stale or duplicate-with-different-content replies. Cancelling an unanswered
question preserves its history. An optional question deadline is an attention
deadline; expiry does not invent a reply or authorize an action.

This first slice supports planning questions and explicit project waits. It does
not yet suspend an individual worker mid-tool, correlate email/provider webhooks,
or resume an arbitrary interrupted controller step automatically.

## Continue from saved work

The Continue action creates a new immutable plan containing only the unfinished
assignments of a settled failed execution. It retains the original objectives,
tools, member/profile grants, dependency graph among remaining tasks, and reserved
model budget. Completed tasks and their original runs are never rewritten or
executed again. Their saved output, artifact references and verified project-file
receipts are supplied to the remaining dependents.

An unfinished task with any dispatched write, an unknown outcome, missing tool
completion evidence, an active environment lease or unresolved external action
requires reconciliation instead. The Continue action cannot override those checks.
Large predecessor output requires an already granted scoped output/journal reader;
it does not silently add that skill. A previous partial candidate is supplied as a
clearly unaccepted excerpt (up to 1,200 characters). Full archived context remains
available through its run/project records and individually granted retrieval tools.
This is continuation at assignment boundaries, not replay of a worker process.

## Bounded automatic execution

Existing projects retain their review policy. A project can explicitly select
`execution_policy: "bounded"` with a finite `model_budget_usd`. This lets ready
plans containing routine reads and a small allowlist of local project/file edits
run without another plan-approval click. Generic browser writes, shell operations,
cloud writes and external commitments are not approved by this policy. Existing
tool permissions, scoped access, model routing and budget admission still apply.
Plans outside the policy remain ready for explicit review.

Legacy interval-based scheduled projects retain their existing behavior. The new
calendar schedules use bounded routine execution and do not silently change an
existing project's policy.

## Calendar behavior

Create one-time, daily or weekly schedules with an IANA timezone, a standing
instruction, a finite per-run model budget and an explicit maximum run count.
The target can be the project team or one member of that team. Single-member
schedules use that member as lead and do not delegate to other members.

- Daily and weekly schedules preserve local wall-clock time across daylight saving
  changes. Weekdays use Monday = 0 through Sunday = 6.
- A nonexistent spring-forward time is skipped for that day. A repeated fall-back
  time runs at its first occurrence only.
- After downtime, one missed occurrence is queued; the next occurrence advances
  into the future. The scheduler does not issue a burst for every missed interval.
- Occurrence insertion and its schedule cursor commit together. Concurrent
  schedulers cannot enqueue the same occurrence twice.
- A project pause prevents calendar claims and request starts without consuming
  schedule allowance. Pausing an individual schedule stops future occurrences;
  already queued requests remain visible and can be cancelled separately.
- A schedule's run allowance counts queued occurrences, including cancelled ones.
  It does not represent successfully completed deliverables. Re-enabling a schedule
  cannot reset this allowance.

Period-wide spending ledgers, reusable event subscriptions, standalone agents
outside projects and inbox digests remain roadmap work.

## API entry points

All routes are under `/v1/projects/{project_id}` and use normal authentication,
project ownership/visibility, CSRF protection and current permissions.

| Route | Purpose |
| --- | --- |
| `GET /continuity` | Queue, questions, schedules and safe continuation readiness |
| `POST /requests` | Save a follow-up, optionally targeting one team member |
| `POST /requests/{id}/cancel` or `/retry` | Version-checked queue controls |
| `POST /waits` | Save a question and its continuation instruction |
| `POST /waits/{id}/reply` or `/cancel` | Resolve a question without erasing it |
| `POST /schedules` | Create a bounded calendar rule |
| `PATCH /schedules/{id}` | Version-checked pause/resume |
| `POST /continue` | Continue a safe settled execution using current project version |

`GET /command` includes the same continuity snapshot for the project workspace.
Permission/team changes remain explicit configuration changes; autosaving ordinary
drafts or results does not authorize additional tools.
