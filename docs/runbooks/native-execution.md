# Native execution and durable workflows

Open **Execution** in a project to queue agent work, configure dependencies and schedules,
follow its checkpoints and costs, answer questions, and cancel or safely retry interrupted
work. A completed run produces a **candidate**, moves its task to **In review**, and keeps
its trace. It does not accept an artifact, publish content or operate a business account.

This is a bounded controller backed by the native PostgreSQL records, with
`engine=postgres` and `definition_version=1`. It is the small-deployment alternative in
the [platform plan](../architecture/autonomous-work-platform-plan.md), not a general
workflow language or a Temporal deployment. Memory storage supports disposable tests;
use PostgreSQL when execution must survive a server restart.

Read with [project models](project-models.md), [native projects and teams](native-projects.md)
and the [roadmap](../next-phases.md). Existing board credentials remain separate from
execution authority.

## Start a project run

1. Create an active agent role and a task with a clear description. Assign the task to
   that role, or leave it pooled and choose an agent when queuing it. Human-assigned work
   must be reassigned before agent execution.
2. Enroll and qualify a project model in **Models & usage**. Execution currently uses
   the qualified **planning** route selected at admission. Local zero-fee models can
   run under zero monetary ceilings. Paid processing requires project consent, sufficient
   project/workspace limits and a nonzero execution ceiling.
3. Open **Execution → Execution settings** and enable the owner-issued project grant.
   Choose limits for concurrent work, steps, attempts, delegation, model calls and cost.
   Automatic queuing is separately configurable and off by default.
4. Choose the task. **Dependencies & timing** saves versioned prerequisite tasks and
   an optional earliest start. **Run task → Queue task** saves admission immediately;
   it does not hold the browser request open while a model runs.
5. Enroll and connect a runner as described below. Without one, admitted tasks stay queued.
   The server checks the current project, role, task, dependencies, model, grant and
   resource limits before dispatching each step.
6. **View run** shows checkpoints, chronological activity, delegated work, charged and
   held model costs, and pending input. **Answer question** resolves a current human wait.
   **Provide input** can save an early answer for the next correlated step. It is used
   only if that step asks a human question; it does not change a model request already sent.

The browser polls active execution while its panel is visible and no editor is open.
Unsaved drafts are kept in the current tab only. Concurrent policy/workflow edits show the
latest record and require an explicit rebase. An uncertain write keeps its original
payload and operation identifier; check its saved receipt or retry that same request.
Navigation and access loss discard stale responses and clear sensitive fields.

## Owner grants and inherited limits

Only a current human project/workspace owner can enable execution, start or steer a run,
change workflow definitions, configure schedules, or enroll/revoke runners. Project
readers can inspect public execution state. The issuing owner's current authority is
checked again during admission, claims, dispatch and checkpoint application. A client
cannot choose an arbitrary owner identifier to fund work.

The persistent agent is the accountable role; the runner is a replaceable process that
holds a temporary lease. Neither ordinary board access nor possession of a role's board
credential grants execution, model-key, publishing or spending privileges.

| Setting                                 | Default        | Allowed range            |
| --------------------------------------- | -------------- | ------------------------ |
| Execution / automatic queuing           | Both off       | Independently configured |
| Active worker runs                      | 4              | 1–32                     |
| Unfinished runs admitted to the project | 100            | 1–500                    |
| Steps per run                           | 8              | 1–32                     |
| Attempts per run                        | 3              | 1–8                      |
| Delegation depth                        | 2              | 0–4                      |
| Descendant tasks per root run           | 8              | 0–32                     |
| Model calls across the root graph       | 16             | 1–128                    |
| Model cost across the root graph        | USD 0          | USD 0–1,000,000          |
| Worker lease                            | 300 seconds    | 180–600 seconds          |
| Run deadline                            | 86,400 seconds | 300–604,800 seconds      |

The project queue limit counts unfinished queued, running, waiting and unknown runs.
Waiting releases the worker lease. It retains its place in the unfinished-work allowance.
Children inherit the root's bounds and model; they cannot multiply its total cost,
model-call or descendant allowance. The model may delegate to existing active project
roles, at most four children per decision. This action does not create roles, issue
credentials or enlarge policy. Team growth remains the separate staffing capability.

Execution policy changes fence existing runs, including changes that increase a limit.
Task edits, reassignment, role revisions, project revisions, workflow-definition changes,
archive/revocation and expired authority also fence further dispatch/application. An
in-flight call can still settle its charge after revocation; its result cannot bypass the
changed authority. Keep run inputs stable while work is in progress, or deliberately
cancel and start new work against the revised inputs.

## Dependencies, actions and schedules

Dependencies are scoped to the same project, versioned and cycle-checked. A task can be
queued before its prerequisites finish. Dispatch requires a completed native candidate
for each prerequisite matching its current title, description and assignment. Manually
moving a task to **Done** does not fabricate that candidate. The selected candidate and
input digest are captured for the step and rechecked before applying its result.
Dependency readiness is an execution contract, not artifact acceptance or publication
permission; exact accepted artifact revisions belong to the next review slice.

Each bounded model decision chooses one supported action:

- Produce a text candidate for review.
- Ask a human question and release the worker while waiting for its correlated answer.
- Wait until a specified time without keeping a worker occupied.
- Delegate bounded child work to existing roles and wait for their candidates.
- Exercise the built-in reference operation: echo text, delay, or record a deterministic
  test receipt. These operations validate the execution/recovery boundary and have no
  external business effect.

JSON structure, allowed action fields, output size and delegation constraints are checked
before application. These checks do not establish factual correctness or creative quality.
The model has no arbitrary shell, browser, connector, network-address or file-write action.
The current context consists of the project/task/role snapshot, bounded execution history
and completed dependency candidates; broad retrieval from intake originals is not yet wired
into specialist execution.

**Schedule task** supports a one-time instant or an elapsed-seconds interval with a bounded
occurrence count. The form labels its input as your browser's local time, shows the saved
UTC instant, and retains a valid IANA timezone. Intervals are fixed elapsed durations;
they are not cron or calendar rules and may shift local clock time at daylight saving.

Each occurrence creates a separate board task copied from the scheduled template, with
its own execution history. Prerequisites are copied; the occurrence uses the schedule's
time rather than the template's earliest-start value. Missed intervals coalesce into one
occurrence and the controller prevents overlapping unfinished occurrences of that schedule.
Capacity/model outages retain the pending occurrence for a later check. Manual schedule
edits or disabling it advance its definition version and fence activity admitted under the
old definition. Advancing an occurrence naturally does not revoke its already-started run.

Automatic queuing and schedule checks run when connected workers claim work. These are
durable recorded waits with periodic bounded worker polling, not an always-on independent
scheduler or an external webhook service. No connected worker means no automatic progress.
Automatic queuing considers agent-assigned **To do** tasks. It compares the latest run's
task-input digest with the current title, description and assignment, so unchanged work
does not continually restart. Revising those inputs and returning the task to **To do**
can admit a new run. A status change alone does not create a new input revision.

## Connect an optional runner

Open **Execution → Enroll runner**, choose a name, concurrent-run ceiling and credential
lifetime, and copy its one-time token. The server stores its hash, project scope, issuer
and expiry. It never returns that token again or puts it in receipts. If the response is
lost, check the receipt, revoke the unusable runner record and enroll a replacement.

Provide the token to the process through its private environment as
`SIMON_EXECUTION_RUNNER_TOKEN`. Do not put it in a command-line argument, committed file,
prompt or shared browser storage. Then start the installed package:

```powershell
.\venv\Scripts\python.exe -m simon.native_worker --server-url http://localhost:8000
```

Use the complete public base URL, including a configured prefix such as
`https://simon.example/simon`. The CLI accepts HTTPS or loopback HTTP, refuses credentials
and query strings in the URL, disables redirects and environment proxy inheritance, and
does not adopt session cookies. `--once` processes one claim then exits;
`--poll-seconds` accepts 1–60 seconds and defaults to 5.

The runner connects outward, claims work and heartbeats while advancing bounded server
steps. A lease includes a run ID, fence generation and separate lease token. Old results
cannot use a superseded lease. The CLI waits out uncertain lease ownership before claiming
again rather than blindly redispatching a timed-out step. Revocation and credential expiry
prevent further claims/heartbeats/steps. Stop with Ctrl+C; a step already sent may finish
and will remain subject to normal lease and accounting checks.

**This runner coordinates server-side model work.** It does not download provider keys,
prompts or arbitrary execution commands, run local shell/CAD/GPU jobs, or transfer artifact
files. Hosting it on another computer does not move inference to that computer. The approved
model endpoint controls where inference occurs. One CLI process handles one claim at a time;
runner enrollment permits a ceiling for concurrent processes, not automatic process creation.
Measured CPU/GPU/storage capacity and isolated local tool execution remain later core work.

## Checkpoints, retries and unknown outcomes

Admission, leases, step identities, usage reservations and dispatch markers persist before
network work. Model calls happen outside database locks. Their known usage is settled even
if cancellation, access loss or a stale lease prevents result application. A completed
checkpoint persists before its candidate, children or wait are applied; recovery can apply
that checkpoint without repeating the model call.

Expired leases with no uncertain dispatched step can return to the queue within their
attempt allowance. A sent step with an unresolved outcome becomes **unknown**. Its ledger
liability and model-call slot remain held, including for zero-fee models. A cancelled run
releases only unsent reservations and stops its descendants; it cannot undo a provider
request already sent. Cancellation is distinct from a known failed step.

During recovery, an expired workflow deadline becomes a recorded failure and pending
waits time out.
A parent with a failed or uncertain child keeps waiting so the owner can reconcile or
retry that child within the original deadline and shared allowance.

**Retry run** is offered only when current authority, attempt bounds and usage state allow
it. It keeps the run's identity, recorded checkpoints and costs, while issuing a new fence
on the next claim. Unknown model liability must first be settled or reconciled by a
workspace owner with evidence in **Models & usage**. Reconciliation resolves accounting;
it does not turn an uncertain response into an accepted draft. An explicitly admitted
retry may incur a new charge, and every new call uses the same shared ledger and root limits.
The attempt limit counts the initial attempt and recovery/retry attempts. Ordinary
resumption after a human, timer or child wait does not consume another attempt merely
because a worker obtains a new lease; each lease still receives a new fence generation.

Reference-operation receipts can be looked up by stable operation ID, so a lost response
does not duplicate that test effect. This does not establish exactly-once behavior for
future external providers; each business connector will need its own effect/reconciliation
contract. Unknown, failed, cancelled and stale states remain distinguishable in history.

## Storage, API and restoration

Migration `0034_native_execution.sql` stores policies, task definitions, runs, immutable
step inputs, events, waits/signals, schedules and hashed runner credentials. Composite
scope constraints and compare-and-swap versions apply in both stores. Command receipts,
reference receipts and model usage live alongside the native records. Apply migrations
through the installation workflow; development acceptance does not alter an operator DB.

Human endpoints are under `/v2/projects/{project_id}/execution`. Reads require current
project access; writes require owner authority, session/CSRF and idempotency keys. Workflow,
policy, schedule and run mutations also check their expected versions.

| Method and suffix                              | Purpose                                                                  |
| ---------------------------------------------- | ------------------------------------------------------------------------ |
| `GET` base                                     | Current policy, task eligibility, initial run/schedule pages and runners |
| `PUT /policy`                                  | Issue or revise the execution grant                                      |
| `GET/PUT /tasks/{task_id}/workflow`            | Read or revise prerequisites and earliest start                          |
| `POST /tasks/{task_id}/runs`                   | Admit a run and return HTTP 202 immediately                              |
| `GET /runs`, `GET /schedules`                  | Paginated history, up to 100 items per page                              |
| `GET /runs/{id}`, `GET /runs/{id}/events`      | Candidate, checkpoints, waits, children, costs and activity              |
| `POST /runs/{id}/cancel`, `/retry`, `/signals` | Cancel, safely retry or supply correlated input                          |
| `POST /schedules`, `PUT /schedules/{id}`       | Create or revise a schedule                                              |
| `POST /runners`, `POST /runners/{id}/revoke`   | Enroll or revoke a project runner                                        |
| `GET /operations/{key}`                        | Recover a safe receipt without retrieving a credential                   |

Worker endpoints under `/v2/execution-worker` accept only the separate execution bearer
credential, without cookies or CSRF/session credentials: `POST /claim`, `/heartbeat`,
`/step`, and `GET /runs/{id}`. Workers cannot submit their own model, prompt, tool result
or target project. Provider keys stay on the server.

Follow [storage recovery](storage-recovery.md), including the model catalog and encryption
master key. Keep runners stopped and isolated from a restored server. Before reconnecting
them, disable restored execution grants/schedules, revoke pre-restore runner credentials,
and reconcile model/provider work since the backup point. Re-enable reviewed projects
with newly enrolled runners. A database restore cannot undo a provider call. This is an
operator recovery procedure; automated recovery-mode enforcement and measured production
restoration acceptance remain open.

## Verification and remaining scope

Execution browser cases use a real isolated HTTP server, real sessions and synthetic model
transport. They cover asynchronous admission, candidate-to-review board state, human waits,
failed-step retry, dependency/schedule editing, lost responses, exact-command replay,
one-time runner tokens/revocation, read-only access, conflict recovery, navigation fences,
51-run pagination, prefixed hosting and mobile layout. All 11 cases passed within the full
October 8 non-live regression: 2,750 passed, 16 platform skips, eight live cases excluded,
and 91.26% fresh branch-enabled coverage. The unchanged coverage gate is 90%. The disposable
PostgreSQL 16.15/pgvector 0.8.6 cluster stopped after the successful run. Desktop and mobile
panels were also inspected visually.

```powershell
.\venv\Scripts\python.exe -m pytest tests/unit/test_native_execution_models.py tests/unit/test_native_execution.py tests/unit/test_native_execution_recovery.py tests/unit/test_workflow_executor.py tests/unit/test_native_worker.py tests/contract/test_native_execution_store.py tests/api/test_native_execution_api.py -q -m "not postgres"
.\venv\Scripts\python.exe scripts/test_postgres.py --bin-dir .local/db-phase/pg16/Library/bin -- tests/contract/test_native_execution_store.py tests/integration/test_native_execution_migration.py tests/api/test_native_execution_postgres_api.py -q
$env:SIMON_BROWSER_TESTS = "1"
$env:SIMON_BROWSER_CHANNEL = "msedge"
.\venv\Scripts\python.exe -m pytest tests/integration/test_native_execution_browser.py -q
```

With those browser settings enabled, run the complete acceptance gate against a fresh
disposable database:

```powershell
.\venv\Scripts\python.exe scripts/test_postgres.py --bin-dir .local/db-phase/pg16/Library/bin --test-timeout 3600 -- -q -m "not live" --cov=simon --cov-report=term-missing
```

The [roadmap](../next-phases.md#verification-and-remaining-limits) owns consolidated test
counts and coverage. No paid inference, real company action or operator database/deployment
change was performed in implementation. Remaining gates include independent artifact
review and exact-version acceptance, broad retrieval/quality evaluation, real tool adapters,
physical resource meters, remote tool isolation, hosted fairness/capacity, PostgreSQL 17,
production restore drills and any Temporal transition. This slice does not complete core
acceptance or claim unattended company operation.
