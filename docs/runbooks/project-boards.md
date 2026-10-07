# Managed project boards

Simon uses **ClickUp** for the shared business project board and keeps a local
execution mirror for the agents. ClickUp provides the company workspace, spaces,
folders, lists, tasks, owners, and board views; Simon supplies the project lead,
specialist agents, execution history, findings, and local artifacts.

## Product choice

| Product     | Best fit here                                                        | Integration approach                                                         |
| ----------- | -------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| ClickUp     | A small project growing into departments and company-wide operations | Implemented REST adapter; explicitly granted lists and selected tasks        |
| OpenProject | An organization that also wants its project board hosted locally     | Future API v3 adapter; deploy and pin a supported server version first       |
| Asana       | Cross-functional teams that already run their work in Asana          | Future adapter for tasks and dependencies; portfolio features depend on plan |
| Linear      | A software or product team already using Linear                      | Future GraphQL adapter; narrower fit for broad company operations            |

ClickUp is the initial choice, not a requirement to move an existing organization.
Its [hierarchy](https://help.clickup.com/hc/en-us/articles/13856392825367-Intro-to-the-Hierarchy)
supports company-level organization. Its
[task API](https://developer.clickup.com/reference/createtask) supports task creation,
while the [dependency API](https://developer.clickup.com/reference/adddependency)
preserves execution prerequisites. OpenProject is the self-hosted alternative:
[Community Edition](https://www.openproject.org/community-edition/) is free and
[17.3 added action boards to Community](https://www.openproject.org/blog/openproject-17-3-release/).

## Ownership and storage

- ClickUp owns the business task title, description, owners, status, and dependencies.
- Simon imports only tasks you select, or publishes execution tasks under a configured binding.
- Files, model runs, execution logs, and agent findings remain on the local server.
  The bridge does not upload local files to ClickUp.
- ClickUp receives task text and, when enabled, progress comments and mapped status changes.
  Do not enable publishing for content that must remain exclusively local.
- Open the full ClickUp board from the project to manage company-wide views and people.
  Simon's Execution tasks tab is the agents' working set.

## Connection setup

1. Create the company workspace and relevant space/folder/list in ClickUp.
2. Create a personal token or arrange OAuth access for an account that can access that list.
   Keep the token in a server environment variable, for example `CLICKUP_API_TOKEN`.
3. Copy `examples/agents/project-boards.example.json` into private local configuration.
   Set the Simon household and actor UUIDs, ClickUp workspace and allowed list IDs,
   the credential variable name, and `enabled: true`. Do not put the token in JSON.
4. Set `SIMON_PROJECT_BOARDS_FILE` to that file and restart the API and agent dispatcher.
5. In a Simon project, choose the configured connection and list, review synchronization
   options, and save the binding. Preview the list and select work to import.

Tokens and user access must be provisioned separately; a Codex plugin connection
does not connect the Simon server. Configuration alone does not create a ClickUp
account or organize an existing company workspace. The example is disabled.

## Agent skills

In **Work > Agents** or a project's member configuration, filter skills by **ClickUp**.
Grant each operation independently:

| Skill                            | Operation                                                    |
| -------------------------------- | ------------------------------------------------------------ |
| Read the linked ClickUp board    | Inspect the saved binding, mappings and sync state.          |
| List ClickUp tasks               | Read a bounded page from the linked List.                    |
| Read a ClickUp task              | Read one task after verifying its List.                      |
| Publish project tasks to ClickUp | Publish existing local todos and their dependencies.         |
| Import ClickUp tasks             | Import selected tasks while no project cycle is active.      |
| Synchronize the ClickUp board    | Pull a bounded batch and push a permitted update while idle. |
| Sync task status to ClickUp      | Push one saved execution status when status sync is enabled. |
| Post project progress to ClickUp | Post one saved progress update when comments are enabled.    |

Tools use the calling run's project and cannot select another project's board. They
recheck the run's current permissions before writes and use the bridge's durable
operation journal. Board setup and recovery remain user operations. Selecting a
skill does not enable a connection or change the project's synchronization options.
Publish and status/progress updates can operate during execution; import and full
sync preserve the active-cycle guard so an agent cannot rewrite its running backlog.

## Synchronization and recovery

The bridge polls outbound from the local server, so board synchronization does not
require public inbound HTTPS or a webhook endpoint. Remote viewing of Simon still
uses the server's separately configured authenticated HTTPS ingress.

Saving a binding authorizes its displayed synchronization options. Automatic task
publishing and status changes are opt-in; progress comments are a separate option.
Status mapping uses actual statuses from the selected list. Required ClickUp custom
fields are enforced when creating tasks; unsupported required fields must be handled
in ClickUp before importing those tasks. The first adapter does not configure custom
fields, company dashboards, billing, user permissions, or automations.

Execution state is retained when a board poll sees the same business status, so
a comment or timestamp change does not rerun completed agent work. Reopen a
completed task in ClickUp when it needs a new execution. An uncertain agent or
provider outcome still requires explicit recovery; changing the board alone does
not certify that the earlier action failed or completed.

Background polling has a 120-second default cadence, rotates through at most five
mapped tasks per batch, and sends at most one progress or status write per tick.
**Sync now** performs one pull batch and one authorized write. Large working sets
become current over several batches. Read failures receive three delayed retries,
then require manual refresh. Uncertain writes are never automatically retried.
Before execution, Simon rechecks the selected mapped tasks and their prerequisites;
a change during planning requires synchronization and a reviewed plan. A single
board-backed execution is bounded to 25 tasks including prerequisites.

Publish an initial backlog in small selections of **five tasks or fewer**, especially
when it has many dependencies. A 25-task manual publish can exceed a token's
100-requests/minute allowance because each write rechecks its resource grants and
dependencies require additional calls. A rate limit can leave a partially published
batch; inspect the durable operations and existing mappings before continuing.
An unknown creation must be reconciled, never blindly submitted again.

Attaching an existing remote task during reconciliation verifies the expected title
and Simon reference marker. Abandoning a creation records the user's review and
blocks automatic publication for that todo until a new explicit Publish request.
The mapped working set is limited to 500 tasks; this release has no workflow for
archiving mapped tasks locally or removing their project binding.

Writes are durably recorded before contacting the provider. An interrupted request
may have succeeded remotely: review and reconcile it instead of sending it again.
The integration rechecks ClickUp's `date_updated` before status writes, but the
[Update Task API](https://developer.clickup.com/reference/updatetask) does not document
an atomic compare-and-swap condition. A person can edit between that check and the
write. Keep automatic status updates off if that remaining race is unacceptable;
progress comments can communicate agent results without changing business status.

The connection's workspace and list allowlists are checked on provider responses,
and each local binding is scoped to a Simon account and project. Integration access
does not grant other local accounts access to private agent runs or files. Company
collaboration and provider permissions remain managed in ClickUp.

See [the ClickUp provider reference](clickup-provider.md) for exact authentication
formats, API bounds, home-list restrictions, operation markers, unsupported custom
fields and the adapter's status-update race boundary. Comment `notify_all: false`
does not suppress all notifications: ClickUp can still notify task assignees and
watchers. Enable progress comments only when those visible updates are wanted.

## Verification

Adapter, persistence, API, and browser tests use synthetic provider responses and
exercise tenant boundaries, selected imports, dependencies, synchronization, and
interrupted writes. A real-account smoke test requires a configured account and a
designated test list. No purchases, reservations, calls, or company-board writes
are performed by the test suite.
