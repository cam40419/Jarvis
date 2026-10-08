# Native project foundation

The platform foundation provides shared projects and boards at `/projects`, backed by `/v2/projects`. It uses explicit records and project membership, independent of personal memory, Drive and external boards. The former Work interface and separate project/task authorities have been removed.

Read with the [platform architecture](../architecture/autonomous-work-platform-plan.md) and [roadmap](../next-phases.md). [AI-assisted intake and automatic staffing](project-intake.md) provide evidence ingestion and reviewed team/work proposals. [Project models and resource limits](project-models.md) provide encrypted keys, qualified routes and shared inference ceilings. [Native execution](native-execution.md) adds dependencies, leased workflows, checkpoints, waits, schedules and candidate output. The initial core is not complete: artifact review, compute/storage entitlements and hosted SaaS administration remain subsequent work.

## Using Projects

Open **Projects** from the conversation sidebar or account page. **New project** saves a name and brief: the intended outcome, existing context, constraints and unresolved decisions. It then opens **Intake** for source files, model settings, clarifying questions and reviewed staffing proposals. Existing brief text can be revised through **Edit project**; reopen **Intake** to build or refine the plan. No model is called until an owner explicitly requests planning with a configured endpoint.

**New task** adds work to the board, either pooled or assigned to an eligible human project member or active project agent. **Pick up task** atomically claims pooled to-do work for the current person. Open a task to edit its description, assignment and status. Search projects/tasks and filter by active/archive status or assignment. The interface loads all visible list pages in batches of 100; server-side search and board virtualization remain capacity work for very large installations.

**People** shows human project membership. Project and workspace owners can select existing workspace members, change roles and remove membership. Workspace invitation/account setup remains in the existing identity administration. Guest members can read the brief and task details; the UI omits write controls. Owners can archive/restore through **Edit project**. Archived boards remain readable, and writable users can still revise the brief.

Edits carry the version originally opened. A concurrent change preserves the draft and shows the current saved text, status and assignment. **Use latest version and keep my draft** explicitly updates the expected version; review the whole draft before saving, because this is replacement rather than automatic merging. A lost response retains the exact command and idempotency key; **Retry save** or **Retry claim** safely replays it. Writes time out after 30 seconds into that uncertain state. Editing/closing a pending form is disabled until the outcome is resolved. Drafts and retry receipts are tab-local, not persisted in browser storage; refreshing or closing discards unsaved text after the browser's leave-page warning.

**Refresh** fetches current project access and board state. No background poll overwrites an open editor. Refreshing after access revocation clears the visible project; subsequent writes are always checked by the server. The page supports keyboard-operated dialogs, narrow screens and installations under a configured URL prefix. Project content is rendered as text, not HTML.

## Storage and authority

Migration `0030_native_projects.sql` provides shared projects, members and tasks.
`0031_native_agents.sql` adds scoped roles, team policies and hashed credentials, allows
agent task creators/assignees and drops the retired `project_artifacts`,
`project_file_operations` and `project_drive` tables. Historical migration files are
immutable. `0032_native_intake.sql` adds saved intake, source revisions and durable planning
attempts. `0033_project_models.sql` adds encrypted model credentials, resource policies
and per-call usage; it retains prior intake liabilities once without a legacy runtime.
`0034_native_execution.sql` adds workflow grants, dependencies, execution history,
waits/signals, schedules and separate runner credentials.
This is a clean cutover with no legacy-project backfill or compatibility reader.
Apply pending migrations through the normal development database workflow. Automated
verification uses disposable databases and does not migrate an operator's database.

PostgreSQL persists records; the memory adapter is a transactional reference for tests
and nonpersistent development. Native projects have one native board authority. No v1
project/manifest dispatcher or assistant-task board remains. Independent queued chat
sessions remain available. Creating a project/role does not call a model, start a worker,
create cloud folders or operate a business tool.

## Access rules

- Workspace identity comes from the authenticated session, never request fields or workspace-selection headers.
- The browser supplies `X-Workspace-ID` as an expectation guard. A mismatch after another tab switches workspace returns 403 before reads/writes; the header cannot select or grant workspace access. Other native API clients may omit it.
- Workspace owners administer all native projects in their workspace. This is workspace administration; platform-wide support and subscription administration are not implemented by this slice.
- Other users need explicit project membership. Project owners manage membership and archive/restore; project members can edit the shared project description and board.
- Workspace guests may read a native project explicitly shared with them. They cannot write, own projects, or receive task assignments.
- Disabled accounts and current workspace roles are checked by the service as well as session authentication. Current access is checked before returning an idempotent receipt.
- Human assignees must be active, eligible project members. Removing a project member requires reassigning their tasks first. Removing the last eligible project owner is rejected.
- Operator workspace revocation releases that person's task assignments in the revoked workspace, advances affected project and task versions, and returns in-progress tasks to `todo`. Project and task creation history remain intact. Workspace administrators can recover a project whose owner lost workspace access.

## API contract

Human requests require the existing session cookie; mutations also require the same-origin CSRF token. Machine requests use a scoped bearer credential on native routes only. Ambiguous human-cookie plus bearer requests are rejected. Validation rejects extra fields. New native request models do not accept historical household aliases.

| Method and path                                            | Behavior                                                          |
| ---------------------------------------------------------- | ----------------------------------------------------------------- |
| `GET /v2/projects`                                         | List visible native projects, with offset and limit               |
| `POST /v2/projects`                                        | Create a project and its owner membership atomically              |
| `GET /v2/projects/{project_id}`                            | Retrieve an authorized project                                    |
| `GET /v2/projects/{project_id}/access`                     | Current permissions, named members and owner-only candidates      |
| `PUT /v2/projects/{project_id}`                            | Replace editable name/objective/status with a version check       |
| `GET /v2/projects/{project_id}/members`                    | List project membership                                           |
| `PUT /v2/projects/{project_id}/members`                    | Add a current workspace member or change a project role           |
| `POST /v2/projects/{project_id}/members/{actor_id}/remove` | Remove membership after ownership and assignment checks           |
| `GET, POST /v2/projects/{project_id}/tasks`                | List or create shared tasks                                       |
| `GET, PUT /v2/projects/{project_id}/tasks/{task_id}`       | Read or replace editable task fields with a version check         |
| `POST /v2/projects/{project_id}/tasks/{task_id}/claim`     | Atomically claim pooled to-do work for the calling human or agent |

Every mutation carries an `idempotency_key`. Updates, membership edits and claims also carry `expected_version`. Membership changes use the project version and advance it. Task edits advance the task version. A stale version or reused key with changed input returns HTTP 409; invisible project/task identifiers return 404. List pages are bounded to 100 projects/tasks. A replay returns the original command receipt; use GET to retrieve the latest state.

The access response contains `project_version`, the current `actor_id`, `permissions` (`can_edit`, `can_manage_members`, `can_archive`, `can_claim`), named `members` with assignment eligibility, and `member_candidates`. Candidates are visible only to owners with active management authority, omit existing project members and disabled accounts, and contain no email addresses. `candidates_offset`/`candidates_limit` page workspace rows in stable actor-ID order; follow `candidates_next_offset` even if a filtered page is empty. Assignment eligibility does not itself authorize writes to archived boards. Permissions are advisory UI state; mutation services always recheck authority.

Native project, membership and task timestamps serialize in UTC. Database connection timezone settings cannot change the JSON representation between the original write, a later read and a saved receipt.

Task assignments support `{"kind":"pool"}`, `{"kind":"human","actor_id":"..."}` or `{"kind":"agent","agent_id":"..."}`. Exactly one matching identity is allowed. Human and agent task creators are recorded separately; an agent never impersonates a user. A pool claim assigns the caller and changes `todo` to `in_progress`; concurrent claims cannot both succeed. This is board ownership, not a worker execution lease.

Statuses are `todo`, `in_progress`, `in_review`, `blocked`, `done` and `cancelled`. A manual status change does not certify an artifact, approve spending or authorize publication. Those contracts belong to later core work. Archived projects remain readable and reject new board work until restored.

## Scoped roles and team management

Open **Agents** to inspect role cards and create/edit roles. Each role has an immutable,
project-unique `role_key`, display name, instructions/responsibilities, success criteria,
staffing rationale, lifecycle state and revision. Stable keys prevent duplicate roles from
repeated staffing proposals. Retired role keys remain reserved for history and reuse.
There is no fixed executive hierarchy or model call attached to an idle role.

Project/workspace owners can manage all roles and the policy. The default policy permits
up to eight active roles and allows explicitly granted manager roles to staff subordinates.
The active limit is configurable from 1 to 100; pause roles before reducing it below the
current active count. This limit counts persistent active identities, not running compute.

A manager agent needs all of: active role, `can_manage_team`, a credential with `team:manage`,
and a project policy allowing agent management. It can create non-manager roles, and revise
only non-manager roles it created. It cannot edit itself, alter a human-created role,
grant management, modify project policy/human access or issue credentials. Delegation
therefore stops at one management level in this slice. Deeper delegation requires an
explicit authority policy in the workflow phase. Board write authority still permits
full task coordination within the project.

Every role edit increments its revision and invalidates prior worker credentials.
Pausing/retiring also releases nonterminal tasks to the pool, returns in-progress tasks to
`todo`, and advances their versions atomically. Completed/cancelled assignment history
is retained. Reactivating a role requires available capacity and newly issued credentials.
Archiving blocks all agent requests while preserving human inspection and owner revocation.

The browser shows concurrent edits before allowing an explicit rebase. Unknown saves
retry the same request; edits and closing are disabled while an outcome is uncertain.
Team lists follow paginated results including inactive roles. Task assignment offers only
active agents; historical assignments remain readable.

## Temporary worker credentials

Owners open **Worker access** on a role to issue/list/revoke access. Select board read,
board write and (where permitted) team management, with a lifetime of 1 to 60 minutes
(default 15). Board read is required. The token is displayed once and cleared on dialog
close; it is never saved in browser storage. Transfer it to a trusted worker through its
secret mechanism, never a prompt or repository file.

Tokens contain 256 random bits. Only SHA-256 hashes and nonsecret metadata persist. The
initial response is the sole plaintext delivery. Retrying a mint after a lost response
returns `token: null` and `replayed: true`: revoke that credential and issue a new one.
Receipts, audit events and outbox payloads never store the secret. Listing exposes validity,
expiry and revocation, not hashes. Editing a role requires rotating its worker access.

Send `Authorization: Bearer <token>` without browser cookies. A credential is bound to
its workspace, project, agent revision and scopes. Every operation checks current active
role/project, expiry, revocation and the issuing human's current owner authority before
reading a receipt or mutating state. Loss of that owner's access disables its credentials. Restoring project/issuer authority before expiry can make an unchanged credential usable again; explicit revocation and a role revision change permanently fence that credential.
Disabling agent management blocks team operations but preserves otherwise valid board
scopes. Bearer credentials do not authenticate existing human-only account/chat/tool APIs.

| Method and path suffix under `/v2/projects/{project_id}`     | Behavior                                                     |
| ------------------------------------------------------------ | ------------------------------------------------------------ |
| `GET /team`                                                  | Policy, role page, permission flags and `agents_next_offset` |
| `POST /agents`                                               | Create a role within current authority and capacity          |
| `PUT /agents/{agent_id}`                                     | Versioned role/charter/lifecycle replacement                 |
| `PUT /team/policy`                                           | Human-owner policy edit; initial `expected_version` is 0     |
| `GET /agents/{agent_id}/credentials`                         | Owner-only array of credential metadata                      |
| `POST /agents/{agent_id}/credentials`                        | Owner-only issue, current agent version required             |
| `POST /agents/{agent_id}/credentials/{credential_id}/revoke` | Owner-only revocation, current agent version required        |

All writes require idempotency keys. Role changes compare role versions; policy changes
compare policy versions; credential writes compare the role version. Separate role/policy
versions avoid unnecessary conflicts with project brief edits. Team views expose
`can_manage`, `can_set_policy`, `can_manage_credentials`, `can_issue_credentials`; they remain advisory. Owners can inspect/revoke credentials on archived projects while issuance stays blocked.

These are board/team credentials, not execution leases or provider keys. Task-specific
worker enrollment, general paid usage, tool grants, execution model routing, role history
browsing and artifact review are future core slices. Reviewed intake staffing proposals
use a human-authorized planning API under the shared model/resource policy. A task marked done is not an
accepted artifact or publishing approval.

## Transactions and visibility

Project/task mutations, audit events, outbox events and idempotency receipts commit together. SQL updates compare the expected version and retain immutable creator metadata. Composite foreign keys prevent attaching a member or task to a different workspace's project, or assigning a task to someone outside that project.

Mutations reuse the existing identity lock, then acquire the workspace transaction. The lock contains bounded database work, without models or external tools. This conservative first implementation serializes native writes through the identity boundary; replacing that coarse locking with measured, revocation-safe concurrency is a later capacity task. Audit append also currently reads workspace history to compute its next chain entry. Replace that with a transactional head/sequence lookup before scaling long-lived autonomous workloads. It is not a public SaaS throughput claim.

Audit records identify the actor, project/task, resulting version and state; membership events identify the changed member and role. The Execution panel exposes per-run activity and checkpoints; full board revision browsing remains pending. Workspace revocation is an operator identity action rather than a new native HTTP endpoint.

## Verification and current limits

October 7 board/team acceptance, before the intake extension: the consolidated non-live suite passed **2,074 tests**, with
14 host-dependent symlink/POSIX skips and 8 live-provider tests excluded. It included
all 46 retained browser cases and used a fresh PostgreSQL 16.15/pgvector 0.8.6 cluster,
which shut down successfully. Subsequent authority and backup checks passed 55 tests,
including 18 additional boundary cases. Combined fresh branch coverage is **90.08%**;
the unchanged 90% gate now uses two-decimal precision. Ruff, strict mypy, ESLint,
Prettier and the 10 connector tests also pass.

The complete local run uses the isolated runner with browser opt-in:

```powershell
$env:SIMON_BROWSER_TESTS = "1"
$env:SIMON_BROWSER_CHANNEL = "msedge"
.\venv\Scripts\python.exe scripts\test_postgres.py --bin-dir .local\db-phase\pg16\Library\bin -- -q -m "not live" --cov=simon --cov-report=term:skip-covered
```

For focused native checks:

```powershell
.\venv\Scripts\python.exe -m pytest tests/unit/test_native_projects.py tests/api/test_native_project_api.py tests/contract/test_native_project_store.py tests/unit/test_native_teams.py tests/api/test_native_team_api.py tests/contract/test_native_agent_store.py -q -m "not postgres"
.\venv\Scripts\python.exe -m pytest tests/contract/test_native_project_store.py -q -m postgres
$env:SIMON_BROWSER_TESTS = "1"
$env:SIMON_BROWSER_CHANNEL = "msedge" # Or use installed Playwright Chromium.
.\venv\Scripts\python.exe -m pytest tests/integration/test_native_projects_browser.py tests/integration/test_native_project_races_browser.py tests/integration/test_native_agent_browser.py -q
```

The direct PostgreSQL pytest command requires an explicitly supplied disposable `SIMON_TEST_DATABASE_URL` whose database name ends in `_test`. Alternatively, use the [isolated database runner](database-testing.md) to create, test and stop a fresh local cluster. Contract tests cover persistence through a new adapter instance, raw SQL foreign-key failures, concurrent updates, rollback and workspace revocation.

Local memory/service/API checks cover shared work, two-workspace isolation, guest access, CSRF, current-role revocation, stale edits, retry conflicts and simultaneous claims. Native PostgreSQL acceptance passed on a portable PostgreSQL 16.15 server with pgvector 0.8.6. It additionally covers fresh migration/replay, native-record preservation and legacy table removal, failed-DDL rollback/retry, persisted sessions and receipts, independent app/store instances, claim/revocation races and non-UTC database sessions. Docker remains unavailable locally. PostgreSQL 17 is still the CI/container target; its actual run remains a separate validation requirement.

The browser suite uses a real isolated application, authentication and memory-backed services. It covers CRUD/reload, shared member/guest access, archive/restore, both editor conflicts, lost-response retries, pagination, text injection, mobile/keyboard behavior and a URL prefix. Delayed-response scenarios check navigation during claims/team loading and editor locking during conflict recovery. It prohibits legacy project/worker calls. Local browser verification uses installed Edge 154.0.4258.62; CI installs Chromium. The workspace directory additionally passes its paired memory/PostgreSQL contract. Browser checks do not certify PostgreSQL UI end-to-end operation or the future AI intake/review workflow.

The test harness establishes isolated settings before test-module collection. Optional browser, PostgreSQL and live-model checks remain separately selected. The [intake runbook](project-intake.md#verification-boundary) records the extension's contracts and focused verification; the earlier board/team totals are not evidence for that new workflow. No paid model calls, new business integrations or deployment changes are part of these slices.
