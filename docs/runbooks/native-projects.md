# Native project foundation

The first platform implementation slice provides a shared project and task API at `/v2/projects`. It uses explicit records and project membership, independent of personal memory, Drive and external boards. This is a backend foundation; the existing browser Work interface still uses the earlier project system.

Read with the [platform architecture](../architecture/autonomous-work-platform-plan.md) and [roadmap](../next-phases.md). The initial core is not complete. Agent principals and automatic staffing, task dependencies, execution leases, artifact review, model budgets, native UI and hosted SaaS administration remain subsequent work.

## Storage and authority

Migration `0030_native_projects.sql` adds `native_projects`, `native_project_members` and `native_tasks`. Apply pending migrations to the selected development database through the normal migration workflow before using this version. Historical migration bytes remain unchanged. This implementation does not migrate or delete existing projects or production data.

The PostgreSQL adapter provides durable storage; the memory adapter is the transactional reference used for tests and nonpersistent development. Native IDs never resolve through the legacy memory-project or ClickUp execution paths. The new API does not create cloud folders, dispatch agents, call a model, or operate business tools.

Each project has one native board authority. The v1 project interface remains for its existing consumers until the native UI, artifacts and execution paths are ready for a tested cutover. Do not mirror a project into both authorities. Retire each legacy route/service and its UI references when its replacement passes acceptance; do not change actor-derived identifiers in place.

## Access rules

- Workspace identity comes from the authenticated session, never request fields or workspace-selection headers.
- Workspace owners administer all native projects in their workspace. This is workspace administration; platform-wide support and subscription administration are not implemented by this slice.
- Other users need explicit project membership. Project owners manage membership and archive/restore; project members can edit the shared project description and board.
- Workspace guests may read a native project explicitly shared with them. They cannot write, own projects, or receive task assignments; legacy job permissions stay unchanged.
- Disabled accounts and current workspace roles are checked by the service as well as session authentication. Current access is checked before returning an idempotent receipt.
- Human assignees must be active, eligible project members. Removing a project member requires reassigning their tasks first. Removing the last eligible project owner is rejected.
- Operator workspace revocation releases that person's task assignments in the revoked workspace, advances affected project and task versions, and returns in-progress tasks to `todo`. Project and task creation history remain intact. Workspace administrators can recover a project whose owner lost workspace access.

## API contract

All routes require the existing session cookie; mutations also require the same-origin CSRF token. Validation rejects extra fields. New native request models do not accept historical household aliases.

| Method and path                                            | Behavior                                                    |
| ---------------------------------------------------------- | ----------------------------------------------------------- |
| `GET /v2/projects`                                         | List visible native projects, with offset and limit         |
| `POST /v2/projects`                                        | Create a project and its owner membership atomically        |
| `GET /v2/projects/{project_id}`                            | Retrieve an authorized project                              |
| `PUT /v2/projects/{project_id}`                            | Replace editable name/objective/status with a version check |
| `GET /v2/projects/{project_id}/members`                    | List project membership                                     |
| `PUT /v2/projects/{project_id}/members`                    | Add a current workspace member or change a project role     |
| `POST /v2/projects/{project_id}/members/{actor_id}/remove` | Remove membership after ownership and assignment checks     |
| `GET, POST /v2/projects/{project_id}/tasks`                | List or create shared tasks                                 |
| `GET, PUT /v2/projects/{project_id}/tasks/{task_id}`       | Read or replace editable task fields with a version check   |
| `POST /v2/projects/{project_id}/tasks/{task_id}/claim`     | Atomically claim pooled to-do work for the calling human    |

Every mutation carries an `idempotency_key`. Updates, membership edits and claims also carry `expected_version`. Membership changes use the project version and advance it. Task edits advance the task version. A stale version or reused key with changed input returns HTTP 409; invisible project/task identifiers return 404. List pages are bounded to 100 projects/tasks. A replay returns the original command receipt; use GET to retrieve the latest state.

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
```

The PostgreSQL command requires an explicitly supplied disposable `SIMON_TEST_DATABASE_URL` whose database name ends in `_test`. It must not point at the deployed database. Contract tests cover persistence through a new adapter instance, raw SQL foreign-key failures, concurrent updates, rollback and workspace revocation.

Local memory/service/API checks cover shared work, two-workspace isolation, guest access, CSRF, current-role revocation, stale edits, retry conflicts and simultaneous claims. The local Docker engine was unavailable during this implementation, so PostgreSQL runtime acceptance remains outstanding. CI's disposable PostgreSQL service is configured to exercise those cases; no local skip establishes that result.

The test harness now establishes isolated settings before test-module collection. Optional browser, PostgreSQL and live-model checks remain separately selected. No paid model calls, new business integrations or deployment changes are part of this slice.
