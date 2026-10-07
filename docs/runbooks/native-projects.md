# Native project foundation

The platform foundation provides shared projects and boards at `/projects`, backed by `/v2/projects`. It uses explicit records and project membership, independent of personal memory, Drive and external boards. The older interface remains under **Legacy work** for its existing projects and execution paths.

Read with the [platform architecture](../architecture/autonomous-work-platform-plan.md) and [roadmap](../next-phases.md). The initial core is not complete. Agent principals and automatic staffing, document intake, task dependencies, execution leases, artifact review, model budgets and hosted SaaS administration remain subsequent work.

## Using Projects

Open **Projects** from the conversation sidebar or account page. **New project** saves a name and brief: the intended outcome, existing context, constraints and unresolved decisions. This is basic brief intake; it does not ingest documents or generate a plan/team yet. Existing brief text can be revised through **Edit project**.

**New task** adds work to the board, either pooled or assigned to an eligible human project member. **Pick up task** atomically claims pooled to-do work for the current person. Open a task to edit its description, assignment and status. Search projects/tasks and filter by active/archive status or assignment. The interface loads all visible list pages in batches of 100; server-side search and board virtualization remain capacity work for very large installations.

**Team** shows project membership. Project and workspace owners can select existing workspace members, change roles and remove membership. Workspace invitation/account setup remains in the existing identity administration. Guest members can read the brief and task details; the UI omits write controls. Owners can archive/restore through **Edit project**. Archived boards remain readable, and writable users can still revise the brief.

Edits carry the version originally opened. A concurrent change preserves the draft and shows the current saved text, status and assignment. **Use latest version and keep my draft** explicitly updates the expected version; review the whole draft before saving, because this is replacement rather than automatic merging. A lost response retains the exact command and idempotency key; **Retry save** or **Retry claim** safely replays it. Writes time out after 30 seconds into that uncertain state. Editing/closing a pending form is disabled until the outcome is resolved. Drafts and retry receipts are tab-local, not persisted in browser storage; refreshing or closing discards unsaved text after the browser's leave-page warning.

**Refresh** fetches current project access and board state. No background poll overwrites an open editor. Refreshing after access revocation clears the visible project; subsequent writes are always checked by the server. The page supports keyboard-operated dialogs, narrow screens and installations under a configured URL prefix. Project content is rendered as text, not HTML.

## Storage and authority

Migration `0030_native_projects.sql` adds `native_projects`, `native_project_members` and `native_tasks`. Apply pending migrations to the selected development database through the normal migration workflow before using this version. Historical migration bytes remain unchanged. This implementation does not migrate or delete existing projects or production data.

The PostgreSQL adapter provides durable storage; the memory adapter is the transactional reference used for tests and nonpersistent development. Native IDs never resolve through the legacy memory-project or ClickUp execution paths. The new API does not create cloud folders, dispatch agents, call a model, or operate business tools.

Each project has one native board authority. The v1 project interface remains for its existing consumers until the native UI, artifacts and execution paths are ready for a tested cutover. Do not mirror a project into both authorities. Retire each legacy route/service and its UI references when its replacement passes acceptance; do not change actor-derived identifiers in place.

## Access rules

- Workspace identity comes from the authenticated session, never request fields or workspace-selection headers.
- The browser supplies `X-Workspace-ID` as an expectation guard. A mismatch after another tab switches workspace returns 403 before reads/writes; the header cannot select or grant workspace access. Other native API clients may omit it.
- Workspace owners administer all native projects in their workspace. This is workspace administration; platform-wide support and subscription administration are not implemented by this slice.
- Other users need explicit project membership. Project owners manage membership and archive/restore; project members can edit the shared project description and board.
- Workspace guests may read a native project explicitly shared with them. They cannot write, own projects, or receive task assignments; legacy job permissions stay unchanged.
- Disabled accounts and current workspace roles are checked by the service as well as session authentication. Current access is checked before returning an idempotent receipt.
- Human assignees must be active, eligible project members. Removing a project member requires reassigning their tasks first. Removing the last eligible project owner is rejected.
- Operator workspace revocation releases that person's task assignments in the revoked workspace, advances affected project and task versions, and returns in-progress tasks to `todo`. Project and task creation history remain intact. Workspace administrators can recover a project whose owner lost workspace access.

## API contract

All routes require the existing session cookie; mutations also require the same-origin CSRF token. Validation rejects extra fields. New native request models do not accept historical household aliases.

| Method and path                                            | Behavior                                                     |
| ---------------------------------------------------------- | ------------------------------------------------------------ |
| `GET /v2/projects`                                         | List visible native projects, with offset and limit          |
| `POST /v2/projects`                                        | Create a project and its owner membership atomically         |
| `GET /v2/projects/{project_id}`                            | Retrieve an authorized project                               |
| `GET /v2/projects/{project_id}/access`                     | Current permissions, named members and owner-only candidates |
| `PUT /v2/projects/{project_id}`                            | Replace editable name/objective/status with a version check  |
| `GET /v2/projects/{project_id}/members`                    | List project membership                                      |
| `PUT /v2/projects/{project_id}/members`                    | Add a current workspace member or change a project role      |
| `POST /v2/projects/{project_id}/members/{actor_id}/remove` | Remove membership after ownership and assignment checks      |
| `GET, POST /v2/projects/{project_id}/tasks`                | List or create shared tasks                                  |
| `GET, PUT /v2/projects/{project_id}/tasks/{task_id}`       | Read or replace editable task fields with a version check    |
| `POST /v2/projects/{project_id}/tasks/{task_id}/claim`     | Atomically claim pooled to-do work for the calling human     |

Every mutation carries an `idempotency_key`. Updates, membership edits and claims also carry `expected_version`. Membership changes use the project version and advance it. Task edits advance the task version. A stale version or reused key with changed input returns HTTP 409; invisible project/task identifiers return 404. List pages are bounded to 100 projects/tasks. A replay returns the original command receipt; use GET to retrieve the latest state.

The access response contains `project_version`, the current `actor_id`, `permissions` (`can_edit`, `can_manage_members`, `can_archive`, `can_claim`), named `members` with assignment eligibility, and `member_candidates`. Candidates are visible only to owners with active management authority, omit existing project members and disabled accounts, and contain no email addresses. `candidates_offset`/`candidates_limit` page workspace rows in stable actor-ID order; follow `candidates_next_offset` even if a filtered page is empty. Assignment eligibility does not itself authorize writes to archived boards. Permissions are advisory UI state; mutation services always recheck authority.

Native project, membership and task timestamps serialize in UTC. Database connection timezone settings cannot change the JSON representation between the original write, a later read and a saved receipt.

Task assignments currently support `{"kind":"pool"}` or `{"kind":"human","actor_id":"..."}`. Agent assignment is rejected until a scoped agent-principal registry and execution authority exist. A pool claim assigns the caller and changes `todo` to `in_progress`; concurrent claims cannot both succeed. This is board ownership, not a worker execution lease.

Statuses are `todo`, `in_progress`, `in_review`, `blocked`, `done` and `cancelled`. A manual status change does not certify an artifact, approve spending or authorize publication. Those contracts belong to later core work. Archived projects remain readable and reject new board work until restored.

## Transactions and visibility

Project/task mutations, audit events, outbox events and idempotency receipts commit together. SQL updates compare the expected version and retain immutable creator metadata. Composite foreign keys prevent attaching a member or task to a different workspace's project, or assigning a task to someone outside that project.

Mutations reuse the existing identity lock, then acquire the workspace transaction. The lock contains bounded database work, without models or external tools. This conservative first implementation serializes native writes through the identity boundary; replacing that coarse locking with measured, revocation-safe concurrency is a later capacity task. It is not a public SaaS throughput claim.

Audit records identify the actor, project/task, resulting version and state; membership events identify the changed member and role. Full revision browsing and a native activity UI are still pending. Workspace revocation is an operator identity action rather than a new native HTTP endpoint.

## Verification and current limits

```powershell
.\venv\Scripts\python.exe -m pytest tests/unit/test_native_projects.py tests/api/test_native_project_api.py tests/contract/test_native_project_store.py -q -m "not postgres"
.\venv\Scripts\python.exe -m pytest tests/contract/test_native_project_store.py -q -m postgres
$env:SIMON_BROWSER_TESTS = "1"
$env:SIMON_BROWSER_CHANNEL = "msedge" # Or use installed Playwright Chromium.
.\venv\Scripts\python.exe -m pytest tests/integration/test_native_projects_browser.py tests/integration/test_native_project_races_browser.py -q
```

The direct PostgreSQL pytest command requires an explicitly supplied disposable `SIMON_TEST_DATABASE_URL` whose database name ends in `_test`. Alternatively, use the [isolated database runner](database-testing.md) to create, test and stop a fresh local cluster. Contract tests cover persistence through a new adapter instance, raw SQL foreign-key failures, concurrent updates, rollback and workspace revocation.

Local memory/service/API checks cover shared work, two-workspace isolation, guest access, CSRF, current-role revocation, stale edits, retry conflicts and simultaneous claims. Native PostgreSQL acceptance passed on a portable PostgreSQL 16.15 server with pgvector 0.8.6. It additionally covers fresh migration/replay, upgrade preservation of legacy records, failed-DDL rollback/retry, persisted sessions and receipts, independent app/store instances, claim/revocation races and non-UTC database sessions. Docker remains unavailable locally. PostgreSQL 17 is still the CI/container target; its actual run remains a separate validation requirement.

The browser suite uses a real isolated application, authentication and memory-backed services. It covers CRUD/reload, shared member/guest access, archive/restore, both editor conflicts, lost-response retries, pagination, text injection, mobile/keyboard behavior and a URL prefix. Delayed-response scenarios check navigation during claims/team loading and editor locking during conflict recovery. It prohibits legacy project/worker calls. Local browser verification uses installed Edge 154.0.4258.62; CI installs Chromium. The workspace directory additionally passes its paired memory/PostgreSQL contract. Browser checks do not certify PostgreSQL UI end-to-end operation or the future AI intake/review workflow.

The retained legacy workspace/navigation and draft-focus scenarios are checked separately. Their synthetic planner fixture now supplies its advertised transport and the existing bounded-controller response format; it fails if a real tool is unexpectedly invoked. The full legacy browser suite was not run for this slice. Source review found older **Edit skills** expectations in `test_calendar_clickup_skills_browser.py`, `test_project_continuity_browser.py` and `test_project_members_browser.py`; reconcile those workflows with the current role editor before claiming full browser CI acceptance.

The test harness establishes isolated settings before test-module collection. Optional browser, PostgreSQL and live-model checks remain separately selected. No paid model calls, new business integrations or deployment changes are part of this slice.
