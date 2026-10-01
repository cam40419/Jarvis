# Persistent project teams and delegated work

Open a project in Work to give it a team, a lead, a backlog and a continuing activity history.
Describe the outcome you want in the project command box. The lead proposes a bounded set of
assignments; Simon validates them, preserves task dependencies and runs
the selected agents through the existing execution system. The lead then reviews their results
and saves a project finding.

The advanced agent planner remains available for writing an exact task graph yourself. Project
commands add automatic decomposition and delegation on top of that same planner and dispatcher.
They do not need a separate model provider or a permanently running agent for each project.

## Find project information

Open **Work**, select a project, and use its workspace tabs:

| Tab             | Contents                                                                                                                |
| --------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Overview        | Goal, task counts, current cycle, lead request, recent tasks and project resource shortcuts                             |
| Execution tasks | Saved agent assignments, dependencies, progress and completed-task archive                                              |
| Files           | Project files on the local server, configured Google Drive folder and generated outputs                                 |
| Run history     | All saved project runs, including earlier planning and execution cycles, with expandable results and artifact downloads |
| Board           | Primary ClickUp board connection, import/publish review, synchronization and uncertain update recovery                  |
| Findings        | Saved conclusions, research and project notes                                                                           |
| Activity        | Continuing project updates and older activity pages                                                                     |
| Sessions        | Project conversations and background tasks                                                                              |
| Team            | Team membership, responsibilities, lead and autonomy settings                                                           |

Run history loads newest first. Use **Load earlier runs** to continue through previous pages.
Each record keeps its original run status and saved results. Removing an agent from the current
configuration does not hide its historical results; current project and account permissions still
apply. Starting new work requires a valid current team and configuration.

The local file browser supports folder breadcrumbs, upload, download, previews and a file-name
filter for the current page. Files remain on the server and are available through an authenticated
remote Simon session. The Drive section shows its actual connection state; configure an account
and project folder before expecting cloud files there. The **Board** tab holds the task-board
connection and synchronization controls. Once connected, a direct board link stays visible in
the project header across all sections; see [project boards](project-boards.md) for setup.

## Choose the team and lead

Every team member has its own role and individual skill selection within the project.
Open **Team → Edit team**, then use **Configure** on the member you want to change. Edit
its title and description, and check each skill it needs. For example, enable file search,
file reading and file writing for a research-and-document owner, while a second member
has only file reading. Changing the first member does not change the second.

Use **Add agent** to create a member directly in the team draft. An existing profile can
provide a starting point; its capabilities are individually editable for the project.
Save the member, then save the team settings. When creating a project, the members are
saved with the project. Cancelling the project draft does not create global agents.
The **Agents** library remains available for reusable starting profiles.

1. Create or open the project and name its team.
2. Add the members it needs. Configure each member's title, description and individual skills.
3. Choose any member as project lead. A team can contain just one capable agent.
4. Choose a concurrency limit within the server's configured maximum and save the team.
5. Send a concrete request with an outcome and constraints. For example: "Prepare a launch
   brief for a small clothing collection. Research before drafting, record sources, identify
   decisions I still need to make, and do not contact suppliers."

Skill choices are saved independently per member and per project. They survive restart
and are used by the planner and executing worker. Integration readiness is shown for each
skill; saving a selection does not connect its provider. Changed or removed server grants
block affected capabilities. Reopening the member configuration allows correcting them.

The lead can assign up to eight tasks per cycle. Related research, writing and verification can
belong to one agent and one task. Separate tasks when dependencies, independent deliverables or
different permissions make that useful. The server adds a final lead review.
Only selected team members and their currently configured tools are eligible. A task with no
selected tools can reason from its supplied context, but cannot access a tool implicitly.
Existing unfinished backlog tasks may be reused. Their saved prerequisites remain mandatory:
an unfinished prerequisite must be included in the plan, while a completed prerequisite stays
linked without being executed again. Invalid graphs and unavailable grants block delegation.

Team changes use the current state version to detect conflicting edits. A team's revision
changes when its membership, member roles or skills, lead, responsibilities, name or concurrency
changes. Finish or discard the current cycle before changing those choices. Provider connections
and the server's maximum available grants remain in the operator configuration.

## Manual review and ongoing schedules

Projects start in **manual** mode. Sending a command runs the lead's planning task and saves
the delegated plan for review. Inspect its assignments, tools and estimates, then use the
reviewed-plan action to start the specialists. Discarding an unstarted plan marks its tasks
cancelled. Sending a command is an execution request: the initial lead model call can incur
provider cost even before you approve the specialist plan.

For continuing work, select **scheduled** mode and save all of these limits:

| Setting            | Meaning                                                                               |
| ------------------ | ------------------------------------------------------------------------------------- |
| Standing objective | The goal sent to the lead on subsequent scheduled cycles, with saved project context  |
| Cadence            | At least 5 minutes and at most 7 days between completed cycles and the next due cycle |
| Maximum cycles     | A finite allowance of 1–100 scheduled cycles for this scheduled session               |
| Model budget       | A positive per-cycle token-cost reservation covering planning and delegated execution |
| Paused             | Prevents new planning and delegation while preserving saved work                      |

Scheduled cycles automatically advance a valid delegated plan, within these limits. A command
sent explicitly while scheduled mode is selected starts an immediate scheduled cycle and
counts toward its allowance. Only one cycle can be active per project. An idle scheduled
project is first due one cadence after configuration; later due dates are measured after the
previous cycle settles. There is no burst replay of every missed interval after downtime.

The model budget covers the initial lead call, specialists and final lead review. The runtime
reserves conservative maximum model usage before each run. Unknown cloud-model token prices
block budget-constrained execution; they are never treated as zero. The cycle's reservation
cannot decrease, and unused planning allowance is not silently refunded to later work. The
finite scheduled allowance is bounded by maximum cycles multiplied by the per-cycle budget.
Changing scheduled settings requires an idle cycle. Switching from manual back to scheduled
starts a new explicitly configured allowance.

These are model-token admission estimates. Tool-provider charges, image generation,
transcription, purchases, storage and machine time are separate and may be unknown. External
commitments still require an external-action proposal and the user's confirmation of that
exact draft; a project request, task note or successful planning result does not approve one.

For the private deployment's `gpt-5.4-mini` endpoint, the prepared standard token rates are
`input_cost_per_million_usd: 0.75` and `output_cost_per_million_usd: 4.5`; they take effect when
the prepared manifest is activated. Those rates were checked for this deployment against the
[official model documentation](https://developers.openai.com/api/docs/models/gpt-5.4-mini)
at implementation time. Recheck pricing before changing a deployment; account access and regional
pricing can differ. The generic starter manifest keeps cloud prices unknown until the
operator configures the endpoint. No paid provider calls are required by the project tests.

## Progress, findings, files and durability

The project view shows its current cycle, task status and dependencies, findings, chronological
activity, and linked plans and runs. Task progress follows persisted run state; a model-authored
note cannot mark a worker task complete. The lead's final synthesis is saved as a finding.
Agents with the reporting tools can read a bounded snapshot, record a finding or progress note,
and propose a follow-up todo. Those tools derive the project from the executing run; an agent
cannot supply another project's ID.

Project state, task snapshots and immutable activity entries use the existing application
database. Use PostgreSQL for a persistent installation; the development memory backend loses
its state when its process exits. Work is scoped to the current actor and workspace, even when
the underlying project description is visible to other household members. Disabled accounts,
removed memberships and revoked write scopes stop future scheduled work.

Up to 500 todos can remain in the live backlog. Archive completed or cancelled tasks to free
space. Archive dependent tasks before their prerequisites so live dependencies remain valid.
Archived snapshots remain available through the archive endpoint; activity is never silently
truncated. Activity pagination returns the newest entries first. Use the immutable sequence
number to order or deduplicate history while new entries arrive.

Generated files continue to use the configured local agent artifact directory and authorized
run-artifact downloads. Project and managed files use the existing local Files storage.
Back up the database and local file/artifact directories together. A browser connection from
another device can view authorized files through Simon; it does not need access to server
filesystem paths. Google Drive and other cloud integrations remain separately configured tools
or sync workflows. Merely enabling a project schedule does not upload its history or artifacts
to a cloud storage account. Cloud model or tool calls use the data explicitly supplied to them.

## Pause and recover

Pause prevents new lead and delegated runs. Already dispatched work drains normally, and the
project continues to record its outcome. Pause does not undo an external write or instantly
abort a provider call. Use the linked run's cancellation control if the current run should stop
at its next safe checkpoint.

A blocked or unknown cycle pauses future scheduling. A pending external proposal or uncertain
external action also stops the project from advancing. Review its saved run, task results,
provider receipts and actions. If execution was interrupted, stop the original worker before
using the existing run reconciliation procedure; do not release its reservation while it may
still act. Project acknowledgment checks linked run reservations and environment leases and
requires a review note. Manual evidence reconciliation records the operator's observation;
it must not be presented as a verified provider receipt. Acknowledgment clears the blocker but
leaves the project paused;
resume explicitly after review. The system never automatically replays an uncertain cycle or
an external action whose outcome is unknown.

If a limit is reached, review the project's history before changing the schedule. If a model
or tool is unavailable, configure it or choose a permitted alternative. Repeatedly resuming a
blocked project does not repair credentials, restore account permission or create capacity.

## Worker and deployment requirements

The API and continuous agent dispatcher must share PostgreSQL, the operator manifest,
credentials and the same resolved local agent state directory on one manager host. Enable
`SIMON_AGENT_EXECUTION_ENABLED=true`; otherwise project commands report that execution is
disabled. Model endpoints, optional providers and required Docker images or leased machines
must be configured before their agents can execute.

Run the existing launcher or the continuous dispatcher:

```powershell
.\venv\Scripts\python.exe -m simon.agent_dispatcher
```

The continuous dispatcher reconciles active projects and queues due cycles alongside ordinary
agent runs. Its `--once` mode handles a bounded pass and is not an ongoing schedule. An API
process alone does not execute queued project work. The feature does not install a Windows
boot service or keep the server awake. Use the existing explicit task/service setup if the
machine must run unattended, and preserve intentional stop/maintenance markers when recovering
local services. See [agent execution](agent-execution.md) for run recovery and deployment details.

## API contracts

All routes below are relative to `/v1/projects/{project_id}` and any configured public path.
They use the existing authenticated session, project access checks and mutation CSRF checks.

| Method and path          | Purpose                                                                                                                               |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /command`           | Project state, recent activity, linked plans/runs and current blockers                                                                |
| `GET /runs`              | All saved project runs, newest first, using `limit` (1–50, default 20) and an opaque `cursor`; follow `next_cursor` for older records |
| `GET /runs`              | All owned project runs, newest first, using `limit` and `cursor`                                                                      |
| `POST /command`          | Queue an instruction with a caller-generated `idempotency_key`                                                                        |
| `PATCH /team`            | Save `team` and/or `autonomy` using `expected_version`                                                                                |
| `POST /control`          | `pause`, `resume`, `run_ready`, `discard` or `acknowledge`, with `expected_version`                                                   |
| `POST /todos`            | Add `{todo, idempotency_key}`                                                                                                         |
| `PATCH /todos/{todo_id}` | Edit or archive `{todo, expected_version}`                                                                                            |
| `GET /todos/archived`    | Read archived snapshots using `offset` and `limit`                                                                                    |
| `GET /activity`          | Read immutable history using `offset` and `limit`                                                                                     |
| `POST /activity`         | Append `{entry, idempotency_key}`                                                                                                     |

Reuse the same idempotency key when retrying the same command, task or finding. A different
payload with that key is a conflict. Generated default todo IDs are derived from the key;
retries cannot create duplicate todos. A stale `expected_version` requires a fresh read before
editing. Agent cycle updates also carry their saved cycle revision. Plan/run creation and
cycle links are committed in one database transaction so a worker restart can resume safely.

Run history accepts `limit=1..50` (default 20) and returns
`{project_id, items, next_cursor}`. Each item includes the run and plan IDs, status,
creation/start/finish times, task summaries, artifact counts, and authenticated `run_url`
and `plan_url` links. Open the run to read task outputs and artifact descriptors; use
`/v1/agent-platform/runs/{run_id}/artifacts/{artifact_id}` to download an artifact.
The listing excludes prompts, outputs, event payloads and provider configuration.

Coordinator runs include `phase` (`planning` or `execution`) and `cycle_id`; directly
created runs use `phase: run` and a null cycle ID. Old cycle numbers were not retained
in plan snapshots, so the API does not reconstruct or invent them. Pass `next_cursor`
unchanged for the next page. It is scoped to the account, workspace and project.
An empty page can still have a cursor when a formerly visible work context has been
revoked. New runs arriving during pagination do not shift the existing pages.

Migration `0025_project_run_history.sql` indexes existing plan-to-run links, including
history created before this endpoint. No backfill or global capped job scan is needed.
Completed history remains readable after agents or execution configuration change,
while current project/context access is still required. Starting new work continues
to validate the current team and configuration.

## Verification

Focused tests use synthetic models and isolated storage; they make no paid provider calls:

```powershell
.\venv\Scripts\python.exe -m pytest tests/contract/test_project_work.py tests/unit/test_project_coordinator.py -q
```

The storage contracts run on memory and on a disposable PostgreSQL database when
`SIMON_TEST_DATABASE_URL` is configured with a database name ending in `_test`. They cover
concurrent schedulers, restart recovery, transactional rollback, tenant isolation, revocation,
idempotency, cadence, finite budgets, pause/drain and full-backlog archival. Coordinator tests
exercise lead planning, review, dependency preservation, specialist execution and final findings.
The browser workflow is covered in `tests/integration/test_project_command_browser.py`, using
`SIMON_BROWSER_TESTS=1` and an installed Playwright browser or configured Edge channel.
