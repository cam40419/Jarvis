# Execute configurable agent teams

Simon can now execute saved agent plans through a separate dispatcher. Independent tasks run concurrently, dependent tasks receive their declared predecessors' final outputs, and successful text/JSON answers become immutable downloadable artifacts. Tasks, projects and companies use the same runtime; RobbinsHome remains an optional external tool.

This is a bounded first runtime. It supports the existing text model transports, HTTP JSON tools, and commands in leased Docker/machine environments. The worker uses a validated JSON controller protocol over text generation for tool selection. Native provider function calling, multimodal worker inputs, desktop/video application adapters, arbitrary file artifact bundles, and chat/Work team editing remain later integrations.

## Agent configuration

Edit the operator manifest selected by `SIMON_AGENT_MANIFEST_FILE`; see [agent profiles](agent-profiles.md). Each profile configures:

- Version, display name, description, system instructions, prompt template, and default variables.
- Output instructions and text/JSON format.
- Depth, importance, local-only policy, and optional model endpoint override.
- Granted tools/scopes, allowed environments, and maximum action level (`read` or `write`).
- Model-step/tool-call limits, total input characters, output tokens, and task deadline.

Individual tasks can override declared prompt variables and append task instructions in the user prompt. They cannot replace the system instructions, invent template variables, or grant themselves tools. The prompt renderer performs plain `${name}` substitution with no expression evaluation. Instructions and configuration are saved in the plan snapshot. Restart API and worker after manifest changes and create a new plan: existing plans/runs reject changed configuration before further dispatch. Profile version is an operator label; changes are also detected by a normalized manifest hash.

Inspect a visible profile with `GET /v1/agent-platform/agents/{id}`. Preview its rendered prompt with the following authenticated, CSRF-protected request; this makes no model calls:

```http
POST /v1/agent-platform/agents/researcher/prompt-preview
Content-Type: application/json

{
  "task": {
    "id": "market",
    "agent_id": "researcher",
    "objective": "Compare three candidate clothing niches.",
    "prompt_variables": {"audience": "the brand founders"},
    "additional_instructions": "Clearly identify assumptions."
  },
  "dependency_outputs": {}
}
```

The example manifest includes this `audience` variable. For tasks with dependencies, preview requires exactly the declared dependency IDs. Input character limits include system instructions, rendered prompt, worker protocol and growing tool history. Conservative UTF-8 byte estimates additionally revalidate model context capacity before each text request; they are not exact tokenizer counts.

## Run the API and dispatcher

Configure PostgreSQL using the existing application setup/migrations. Both processes must use the same database, manifest, credentials and resolved absolute `SIMON_AGENT_STATE_DIR` on **one execution-manager host**. Relative paths below assume both start in the repository root. PostgreSQL coordinates run claims, while the local SQLite environment journal and artifact directory must be shared by dispatcher processes on that host. This is not a distributed execution scheduler. Stop all processes using the old configuration before changing the manifest or resource definitions.

```dotenv
SIMON_STORAGE_BACKEND=postgres
SIMON_AGENT_MANIFEST_FILE=.local/agent-platform.json
SIMON_AGENT_STATE_DIR=.local/agents
SIMON_AGENT_EXECUTION_ENABLED=true
```

All bundled integration examples remain disabled. Configure real model IDs, credentials and tools before enabling their entries. Credentials named by model, HTTP-tool and machine entries are read consistently from `.env` plus process environment; process values take precedence.

Restart the API, then start a separate dispatcher terminal or service:

```powershell
.\venv\Scripts\python.exe -m simon.agent_dispatcher
```

`--once` handles one queued run and exits. The default loop polls for work with bounded concurrency; `--poll-seconds` accepts 0.2 through 10. Shutdown stops scheduling new runs and waits for current work. It does not cancel run contents; use the cancellation endpoint first when that is intended. Waiting runs may depend on resource capacity held by another interrupted run, so graceful shutdown can wait until that run is cancelled/reconciled. The worker is not automatically installed as a Windows task by this change. API startup itself never launches jobs or model calls.

Create a plan through the existing planning endpoint, inspect it, then queue execution:

```http
POST /v1/agent-platform/plans/PLAN_UUID/runs
Content-Type: application/json

{"idempotency_key":"brand-research-run-001","model_budget_usd":1.00}
```

Normal authentication, Origin and CSRF headers are required for mutations. This explicit start can incur provider/tool costs when the dispatcher processes it. A successful response means queued, not completed. Reusing the same key and settings returns the same run; changing settings with that key returns a conflict. A new key deliberately creates another run and a new execution attempt/workspace for each task.

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/agent-platform/runs` | List the caller's visible runs |
| `GET /v1/agent-platform/runs/{id}` | Status, per-task progress/events, final outputs and artifact descriptors |
| `POST /v1/agent-platform/runs/{id}/cancel` | Stop queued work and prevent subsequent calls/tasks in active work |
| `GET /v1/agent-platform/runs/{id}/artifacts/{artifact_id}` | Download an authorized, hash-checked final answer |
| `POST /v1/agent-platform/runs/{id}/reconcile` | Owner-only reconciliation of an owned interrupted run after its worker is stopped |

The generic jobs API cannot expose or mutate these records. Membership, account status, context/team visibility, selected configuration and tool scopes are checked again during execution. Independent tasks continue after an ordinary sibling failure; dependents of failed tasks become blocked. An unknown external outcome stops new tasks in that run and requires inspection. Already-running independent calls drain normally.

## Concurrency, costs and cancellation

Each run reserves its parallel slots durably before starting. A short global store transaction serializes reservations across dispatcher processes. The sum of active reservations cannot exceed the manifest platform cap. A run reserves its maximum permitted slots even when its current dependency phase cannot use all of them; this is conservative admission, not a throughput optimizer. Shared environment capacity is reserved across runs as well as within each run. Existing standalone/quarantined leases can block a task without executing its model.

Before queueing, the runtime reserves a conservative model-cost estimate for all tasks: permitted model steps multiplied by the configured endpoint context capacity and output cap at configured token prices. Text-only tasks reserve one model call. `model_budget_usd` bounds the aggregate estimate; an individual task's `budget_usd` must cover its permitted loop. Unknown cloud prices prevent budget-constrained execution. Set realistic prices and narrow loop/output limits for useful estimates.

Reservations are stored with the run and are not refunded automatically after unknown calls. They are per-run planning/admission estimates, not a provider billing guarantee, monthly company quota, or organization-wide spending ledger. External tool charges, storage, GPU/machine time and provider price changes are outside this token estimate. Reported token usage and model dispatch/completion events are retained for inspection.

Cancellation is cooperative. The runtime checks before new calls and records completion receipts for calls already in flight. It cannot undo a completed write or instantly terminate a remote HTTP request. Model/HTTP/command adapters also impose bounded request/command timeouts. The profile deadline is checked between those calls; a call can finish after the deadline. Cleanup releases the owned environment and preserves the task workspace.

## Tools and artifacts

HTTP tools execute through their configured endpoint. The worker receives only the selected catalog definitions and actor/profile scope intersection; model output never grants authority. Tools declaring `external_commitment` are not executable by this worker version. Increasing prompt authority does not bypass that policy.

The `native` transport connects the worker to the same local-file and Google services used by Files. Copy the required entries from [native-tools.example.json](../../examples/agents/native-tools.example.json) into the operator manifest and explicitly enable/configure them. Add their IDs and required scopes to the selected agent. Local mutations and project cloud edits also require `max_action: "write"`. API and dispatcher normalize native definitions to their code-owned schemas, scopes and action policies; manifest configuration cannot turn a write into a read.

Native tools support workspace/project file listing, filename search, bounded UTF-8 reads, revision-checked edits, moves, folders and ZIP operations; project listing; connected Google account listing; Drive browsing/search/read; Gmail and calendar reads; and linked-project file/Docs/Sheets creation, editing and rename. Google tools require server OAuth configuration and the caller's own connected account and provider scopes. No email sending or calendar mutation is exposed through this bridge. Readiness distinguishes a missing server configuration from a missing connection or OAuth grant; checking readiness does not contact a provider.

Agents receive only their own `workspace` and authorized `project:<id>` local roots. Host Desktop/Documents/Downloads roots remain unavailable to this transport even for the server owner. Project roots additionally require `memories:read` in the profile scopes. Paths and archive members use the existing traversal, credential-file, symlink/junction and size checks. Native project cloud operations require an already linked Drive folder; reading a project never provisions one. Write receipts use the run and invocation IDs, preserving local deduplication, cloud operation history and unknown-outcome handling. Live membership, assigned scopes, cancellation, run ownership and selected Google connection are rechecked before results are released.

When embedding the dispatcher, pass `transport_factory=native_transport_factory(connected)` to `AgentDispatcher` and bind the manifest with `with_native_tools(manifest, connected)`. These helpers are in `simon.adapters.native_tools`. The standalone dispatcher performs the same service wiring as the API. A definition remains unavailable if its transport is not registered; listing a template does not install it.

The `mcp` transport supports explicitly configured tools on Streamable HTTP servers implementing protocol `2025-11-25`. [mcp-tool.example.json](../../examples/agents/mcp-tool.example.json) is a disabled starting point. Set a fixed HTTPS (or loopback HTTP) endpoint, exact server tool name and matching input schema; put its bearer credential in the named server environment variable. Grant only the required tool IDs/scopes to the relevant profiles/teams. This enables operator-managed external storage, productivity, browser or creative services that expose compatible tools; it does not create cloud accounts, authorize OAuth, install an MCP server, or make a shared server credential user-specific. Configure the upstream service's access boundaries accordingly. Simon sends the actor, household, run and invocation IDs as request headers.

Each invocation performs initialization, initialized notification, and one `tools/call`, then attempts session deletion if a session was allocated. Responses can be bounded JSON or SSE, including progress notifications. The client does not follow resource URLs, accept sampling/elicitation requests, execute server instructions, reconnect, or replay interrupted tool calls. Calls stop at the invocation deadline/byte/event limits; a lost or invalid write response is an unknown outcome requiring inspection. No tool call follows a failed handshake or a revoked assignment. Session cleanup failure cannot erase a successful receipt. Other protocol versions, stdio servers, OAuth discovery and task-augmented asynchronous calls require separate adapters. Protocol details: [Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports), [lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle), and [tool calls](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).

The `environment` transport binds a command tool to the task's actual lease. [environment-tool.example.json](../../examples/agents/environment-tool.example.json) shows a disabled Python entry. Add its definition to the manifest, explicitly grant its ID and `jobs:write` scope to the profile, set `max_action: "write"`, and select an environment with `process.execute`. The selected model must declare `tools` capability. The configured image/runner must already contain the executable. `argv_prefix` is operator-controlled; arguments remain separate argv entries, and there is no host-shell fallback. Granting Python grants code execution inside that environment; an executable prefix alone does not restrict what the application can do.

Custom MCP/browser/media handlers can still be injected through a worker factory. The stock dispatcher registers HTTP and leased environment transports. A registered machine requires the previously documented runner service; that daemon, cloud provisioning, native applications and their licensing/installations are not supplied here.

The controller publishes final text or JSON answers under workspace/actor/run/task identifiers with immutable metadata and SHA-256 checks. Model text cannot supply a host path to publish. Artifact storage rejects redirects and keeps data outside container-writable workspaces. Source files created inside an execution workspace remain there; automatic source-tree collection, shared object storage, artifact revisions and review/approval publication are separate work.

## Interrupted runs

Crashes never automatically requeue a running plan or replay a model/tool action. A crashed run retains its running reservation until reconciled. First stop the original dispatcher and its workers, then inspect the run events and environment lease journal. Do not infer that a side effect failed merely because its completion receipt is missing.

The authenticated reconcile endpoint requires `expected_version` and `worker_stopped: true`; it revokes the dispatcher identity, marks unfinished work uncertain/cancelled and releases the run's scheduling slots. It does not repeat actions or release quarantined physical resources.

For member-created runs or runs whose owner/account/context was revoked, a trusted server operator can recover without depending on that user's current access:

```powershell
.\venv\Scripts\python.exe -m simon.agent_dispatcher --recover-run RUN_UUID --expected-version 7 --worker-stopped
```

The command prints only run ID/status/version and records an audit event attributed to the configured administrative actor. Environment leases still require ownership-checked cleanup using the [environment runbook](agent-environments.md), including `recover_interrupted=True` only after stopping the original manager. An allocation that failed after reserving a lease remains associated with the run's task for this inspection. A new run is an explicit new attempt, not automatic resumption.
