# Local Git for agent workers

The `git` transport provides real local repository operations inside the current
task's leased Docker container. It supports status, staged/unstaged diff, bounded
history, branch listing/creation/switching, initialization, explicit staging, and
committing. Results include the command exit code, stdout, stderr, and whether
output was truncated. A nonzero exit code is an operation failure, not a success.

## Provision and grant access

Build the supplied image on the worker host:

```powershell
docker build --file deploy/Dockerfile.git-worker --tag simon-git-worker:local .
```

Use a Docker environment definition with `container_image` set to
`simon-git-worker:local`, `capabilities` including `git` and `python`, and
`network: "none"`. The image supplies `/usr/local/bin/python3` and `/usr/bin/git`.
Keep the standard dedicated workspace mount and worker UID/GID permissions from
[agent environments](agent-environments.md).

The [example definitions](../../examples/agents/git-tools.example.json) are
disabled until provisioned. Copy selected definitions into the platform manifest's
`tools` array and set `enabled` and `configured` to `true`. Grant their IDs to the
agent's `tool_ids`, the environment ID to `environment_ids`, and `jobs:read` and/or
`jobs:write` to `tool_scopes`. The signed-in actor must have the corresponding
scope, and writes require the profile's `max_action` to permit `write`. Existing
role permissions apply; adding a manifest entry alone does not authorize a user.
You can also generate definitions in Python with
`simon.adapters.git_tools.git_tool_definitions(enabled=True)`.

`author_name` and `author_email` in each write tool's settings set the local commit
identity. Defaults are `Simon Agent` and `agent@simon.local`; use an explicit bot
identity if revisions will later be published. No host Git identity or credentials
are inherited.

## Operations

All paths are relative to the lease workspace. `repository` defaults to `.`.

| Tool           | Example arguments                                 | Effect                       |
| -------------- | ------------------------------------------------- | ---------------------------- |
| `git.init`     | `{"repository":"source","branch":"main"}`         | Initialize a new repository  |
| `git.status`   | `{"repository":"source"}`                         | Inspect changes              |
| `git.diff`     | `{"repository":"source","staged":true}`           | Read a patch                 |
| `git.log`      | `{"repository":"source","limit":20}`              | Read recent commit summaries |
| `git.branches` | `{"repository":"source"}`                         | List local branches          |
| `git.branch`   | `{"repository":"source","name":"feature/report"}` | Create a branch              |
| `git.switch`   | `{"repository":"source","name":"feature/report"}` | Switch to that branch        |
| `git.add`      | `{"repository":"source","paths":["report.md"]}`   | Stage listed paths           |
| `git.commit`   | `{"repository":"source","message":"Add report"}`  | Commit staged changes        |

Populate the workspace through approved assignment tooling or an administrator's
staging process. Git tooling does not mount the Simon checkout or project library.
Task workspaces survive lease release for artifact collection; a later task gets
its own workspace and does not implicitly reuse the previous task's repository.

## Execution boundaries

Commands use fixed operations and structured arguments, with no shell or arbitrary
Git flags. Repository paths cannot escape the workspace or target `.git`. Linked
worktrees, alternate object stores, metadata symlinks, executable configuration
(including filters and config includes), and oversized metadata are rejected.
Ordinary repositories using unsupported custom config need a clean copy before use.

The guard runs in an isolated Python interpreter inside Docker. It suppresses
system/global Git configuration, inherited environment credentials, hooks, signing,
external diffs, text conversion, automatic maintenance, and network protocols.
Git's protections for conflicting uncommitted files remain in force when switching
branches. Use a maintained Git image; this guard does not replace container
isolation or Git security updates.

The command timeout defaults to 60 seconds and retained output to 64 KiB. Interrupted
writes have an uncertain outcome and are not automatically replayed. Inspect the
repository and reconcile the lease before retrying a commit. The trusted runner
still enforces lease ownership, fencing, and current resource state.

Remote clone/fetch/push, authentication, submodule updates, linked worktrees,
destructive reset/clean, rebasing, and automatic merge conflict resolution are not
implemented. Publishing requires a future explicit remote destination and network/
credential grant. The current tools can prepare and review local revisions without
granting repository publishing access.

Git's [environment controls](https://git-scm.com/docs/git),
[configuration reference](https://git-scm.com/docs/git-config), and
[path staging behavior](https://git-scm.com/docs/git-add) describe the underlying
command controls.
