# ClickUp project board provider

Connect ClickUp from **Account & access ? Connections ? ClickUp & service accounts**.
Choose ClickUp, enter a name and your personal API token, then select **Connect account**.
In ClickUp, find the token under **Settings ? Apps**. No configuration file,
Workspace ID, List ID, environment variable, or server restart is needed.

Simon saves the account connection in the backend with an encrypted credential,
scoped to the signed-in Simon account and workspace. The connection discovers
all ClickUp Workspaces permitted by the token and all accessible Spaces,
folder Lists, folderless Lists and Lists in Shared with me. Individually shared
tasks are discoverable through `shared_tasks_list` and readable through
`account_task_read` without granting access to their whole List. New Lists are discovered on the next board
selection; Workspace discovery refreshes within 30 seconds. Actual reads and
writes still verify the current ClickUp permissions. Use **Reconnect** to rotate
credentials without losing project bindings, and **Disconnect** to remove the
stored connection. API and dispatcher processes resolve saved credentials live.

Under a project's **Board** tab, choose the connected Workspace and the named
List for that project's execution mirror. This selection identifies the task
source and write destination; it does not limit the account's ClickUp access.
Separate `clickup.connections_list`, `boards_list`, `account_tasks_list` and
`account_task_read` agent skills browse accessible Lists without a project List
binding. Existing publishing and synchronization skills use the saved project
binding and its write settings.

Personal tokens inherit the associated ClickUp user's permissions.
See [ClickUp authentication](https://developer.clickup.com/docs/authentication).
Simon supports reading tasks, creating marked tasks, status updates, dependencies
and progress comments. Account administration, deletion and arbitrary-field
editing are not implemented.

## Backend storage and compatibility

Migration `0027_integration_connections.sql` adds the private connection store.
Credentials never appear in public API views. The backend reuses an existing
Google encryption key when configured; otherwise it generates
`%LOCALAPPDATA%/Simon/credentials.key` automatically (or the configured
`integration_key_file`). Private full recovery bundles include this file along
with `.env`. Restore the key with the database to preserve connected accounts.

Existing operator files still work for compatibility. A legacy connection can
use `discover_lists: true` instead of explicit `list_ids`. File-managed
connections can also use a provisioned OAuth token; the UI currently connects
ClickUp personal tokens. OAuth consent for distributed ClickUp apps is not
implemented. The API base address remains fixed to
`https://api.clickup.com/api/v2`.

## Provider operations and boundaries

Read operations require `jobs:read`; writes require `jobs:write`, in addition to
the saved workspace, actor and List access settings. Before an operation, the
adapter checks [authorized Workspaces](https://developer.clickup.com/reference/getauthorizedteams),
the configured Workspace's [Spaces](https://developer.clickup.com/reference/getspaces),
and the [shared hierarchy](https://developer.clickup.com/reference/sharedhierarchy)
when automatic discovery is enabled, plus the [List metadata](https://developer.clickup.com/reference/getlist). The
List must belong to one of those Spaces. Returned tasks must match that List,
Workspace and Space. Archived Lists cannot receive task operations.

Task reads return a bounded snapshot: name, description, status/type, update
timestamp, assignee IDs/names and prerequisite IDs. Attachment URLs, sharing
tokens, member emails and arbitrary response fields are not returned. Browser
links are constructed for `app.clickup.com`. The adapter treats task descriptions
as external content, not execution instructions.

[Task listing](https://developer.clickup.com/reference/gettasks) uses zero-based
pages of at most 100, includes closed tasks and subtasks, and excludes tasks
whose home List differs from the allowed List. The result explicitly supplies
`next_page`; callers must finish or report incomplete synchronization. Requests
are bounded to page 1000. External content can change during pagination, so the
caller must reconcile snapshots rather than assume a provider-wide transaction.

[Task creation](https://developer.clickup.com/reference/createtask) sends only
the requested name, description and a status present in the List, with
`check_required_custom_fields: true`. A List that requires unsupported Custom
Fields will reject creation; Simon does not bypass those requirements. The
description includes a durable `[Simon reference: OPERATION_ID]` marker for
identifying a creation after an uncertain response. The returned task must
preserve that marker, name and status. The marker is not an upstream idempotency
guarantee and can be edited by a human.

[Status updates](https://developer.clickup.com/reference/updatetask) first fetch
the current task and compare its `date_updated` with the caller's saved value.
A difference blocks the update. The PUT body contains only `status`; the
adapter does not rewrite human-authored descriptions, names, assignments or
dates. **The documented endpoint has no atomic conditional-update contract.**
A human edit between the preflight GET and PUT can still race with a status
change. Treat this as conflict detection, not a remote lock or guaranteed CAS.

[Dependencies](https://developer.clickup.com/reference/adddependency) are added
with `depends_on`, after verifying both tasks belong to the bound List. Existing
dependencies are detected without another write; self and direct two-task
cycles are rejected. Larger graph validation belongs to the project service.
This operation does not remove or rewrite existing dependencies.

[Progress comments](https://developer.clickup.com/reference/createtaskcomment)
append text with a durable operation marker; they never replace task content.
`notify_all` is false, but ClickUp documents that task assignees and watchers can
still receive notifications. The project service must explicitly authorize
these externally visible progress writes. No comments are posted by the test
suite or configuration loader.

## Failure and duplicate handling

ClickUp currently applies a [per-token request limit](https://developer.clickup.com/docs/rate-limits)
of 100/minute on Free Forever, Unlimited and Business. A single task read uses
four requests for fresh hierarchy verification; status/comment writes typically
use five. `get_tasks` shares verification only inside one read batch of up to 25
tasks, reducing the cost to three hierarchy requests plus one per task. It does
not retain authorization caches between requests. Synchronize small batches,
stagger projects that share a token, and preserve existing mappings on a 429
response while scheduling a later attempt. A single large project or multiple
projects sharing a token can still hit the provider's limit.

Every network request uses bounded HTTP with a timeout of at most 60 seconds,
at most 2 MB of response data, and no automatic retries. Rate-limit and explicit
validation rejections are reported to the caller. Timeout, server failure,
redirect or invalid write receipt produces an unknown outcome. Error messages
do not echo provider tokens or response bodies.

The adapter deliberately does not invent an `Idempotency-Key` feature that the
documented ClickUp endpoints do not provide. The project service owns durable
operation claims and recovery. Claim a creation/comment before dispatch, retain
its marker, and do not automatically replay a write after an unknown result.
Look for an existing task with the exact marker and inspect its List and
content before resolving an uncertain creation. A missing result in a partial
task scan does not prove that creation failed. There is no task deletion,
workspace creation, member management or arbitrary-field editing operation.

## Adapter contract and tests

`ClickUpAdapter(BoundedHTTP(...))` uses the models in
`simon.domain.project_boards`. Public methods receive `(connection, actor, ...)`:
`boards`, `list_metadata`, `tasks(page=0)`, `get_task`, `get_tasks`, `create_task`,
`update_status(expected_updated=...)`, `add_dependency`, and `append_comment`.
`load_board_connections` and `board_configuration_reasons` provide bounded
configuration parsing and honest setup blockers.

Run `python -m pytest tests/unit/test_clickup.py -q`. The suite checks the actual
HTTP path, auth mode, required-custom-field flag, pagination, home-List and
Workspace isolation, human edit conflicts, narrow write payloads, dependency
direction, operation markers and unknown outcomes. All requests use
`httpx.MockTransport`; no ClickUp account is contacted and no remote task or
comment is created.
