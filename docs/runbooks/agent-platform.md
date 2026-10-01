# Agent platform foundation

Simon now includes a configurable foundation for teams, tool discovery, isolated execution and semi-automatic model selection. It works independently of RobbinsHome. A company/project context is optional; standalone work requires neither one nor a home connection.

The [project workspace](project-teams.md) adds a visual team/lead editor, natural-language
decomposition, durable backlog and findings, and bounded ongoing work. The explicit planner
described here remains available for constructing an exact task graph.

The authenticated API compiles and stores plans. Explicit run requests can now be processed by a separate dispatcher with concurrent tasks, dependency handoffs, bounded model/tool loops, cancellation and final-answer artifacts. See [agent execution](agent-execution.md) and [agent profiles](agent-profiles.md). Plan creation itself remains side-effect free and returns `execution_started: false`; the API catalog reports whether execution is enabled. Existing chat behavior is unchanged.

## Configure and inspect

Install the project dependencies, then copy the example into private configuration:

```powershell
.\venv\Scripts\python.exe -m pip install -e '.[dev,postgres]'
New-Item -ItemType Directory -Force .local | Out-Null
Copy-Item examples/agents/platform.example.json .local/agent-platform.json
.\venv\Scripts\python.exe -m simon.agent_platform --manifest .local/agent-platform.json validate
```

All example model endpoints, tools and environments are disabled. Set the actual installed local model name or accessible API model ID, enable the entries you intend to use, and configure credentials through the named environment variables. Keys belong in `.env` or the process environment; manifests contain references, not credential values. Process variables take precedence over `.env` for platform models, HTTP tools and machine runners.

Set `SIMON_AGENT_MANIFEST_FILE=.local/agent-platform.json` in `.env`, optionally set `SIMON_AGENT_STATE_DIR`, then restart the API. Leaving the manifest setting empty preserves an unconfigured catalog. Invalid configuration fails startup with a bounded error; it is not silently ignored.

The manifest contains versioned team templates, worker profiles, model endpoints, tools, environments and optional work contexts. Profiles constrain permitted tool and environment IDs and set model defaults. Teams select profiles and concurrency ceilings. `allowed_workspace_ids` can restrict a team; an empty list makes that template available to authenticated users with job permissions. Company/project contexts bind to a specific workspace and optionally selected actor IDs. These are initial configuration records, not a full company-management database or visual editor.

Tool permission checks intersect the authenticated actor's scopes with the profile's `tool_scopes`. Templates do not grant new account permissions. Built-in tool templates deliberately use explicit capability scopes and remain disabled until an operator configures both the integration and authority. Do not enable a template merely because its name appears in the catalog.

Run an offline preview:

```powershell
.\venv\Scripts\python.exe -m simon.agent_platform --manifest .local/agent-platform.json plan --request examples/agents/research-request.example.json --workspace-id 22222222-2222-4222-8222-222222222222 --actor-id 11111111-1111-4111-8111-111111111111
```

The example UUIDs are development identifiers; use the actual authenticated workspace/actor when configuring access. CLI planning is a trusted operator preview in memory. It does not create accounts, grant API permissions, persist business plans, contact providers, create containers or allocate machines. Missing providers/resources appear as blocked reasons. A successful preview checks declared configuration and credentials, not remote health or model quality.

## Persist plans through Simon

Use the normal login session. POST requests require the existing Origin/CSRF checks.

| Endpoint | Behavior |
| --- | --- |
| `GET /v1/agent-platform/catalog` | Visible teams/profiles, model and environment summaries, available HTTP tool configurations and disabled capability templates |
| `POST /v1/agent-platform/plans` | Validate and persist the request and resolved plan; returns 201 |
| `GET /v1/agent-platform/plans` | List the current actor's visible plans |
| `GET /v1/agent-platform/plans/{id}` | Retrieve one plan with current ownership/context checks |

The request format is demonstrated in `examples/agents/research-request.example.json`. Tasks have IDs, assigned profiles, objectives, dependency IDs, optional tool/environment subsets, depth/importance, privacy, a model override and estimated token/budget requirements. Dependency cycles and missing references are rejected. Effective parallelism is the minimum of task-request, team and platform caps. Preview waves respect dependency order and each environment's configured capacity.

Plans use Simon's existing job storage as `platform.plan` records in a waiting state, with the compiled plan in the persisted input snapshot. They survive API restart when PostgreSQL is configured; the memory backend is ephemeral. Repeating an idempotency key for the same actor/request returns the original plan. Changed requests require a new key. Fingerprints normalize unordered capability sets across processes. The generic jobs API cannot create or expose platform records, whose selected configuration snapshots are internal.

The saved plan captures the selected configuration and model explanation. Dispatch revalidates current permissions, credentials, resource ownership, costs and configuration. Preview waves are not reservations or evidence that work ran. An execution run has its own persistent state and unique environment attempts.

## Route local and API models

Depth and importance each range from 1 to 5. The higher value sets the minimum declared quality tier:

| Demand | Minimum tier | Default selection |
| --- | --- | --- |
| 1–2 | economy | Prefer a suitable local economy model |
| 3 | standard | Use a suitable standard model, local when configured |
| 4–5 | frontier | Require a configured frontier endpoint; do not silently downgrade |

Manual endpoint overrides retain capability, privacy, context, credential, budget and quality checks. `local_only` cannot fall back to a cloud endpoint. Routing does not identify difficulty from the prompt or benchmark a model: it uses task metadata and profile defaults. Model tiers and capabilities are administrator declarations to validate against real workloads. Arbitrary model IDs let the inventory adopt newer models without source changes.

```powershell
.\venv\Scripts\python.exe -m simon.agent_platform --manifest .local/agent-platform.json route --depth 2 --importance 2 --local-only
.\venv\Scripts\python.exe -m simon.agent_platform --manifest .local/agent-platform.json route --depth 5 --importance 5
```

Both commands only inspect configuration. `run-text --prompt-file PATH` explicitly performs one text-generation request and can incur API charges. It accepts the same routing flags. The supported transports are OpenAI Responses, OpenAI-compatible endpoints such as configured local servers, Anthropic Messages and Gemini `generateContent`. The adapter does not run tools, process images/video or implement provider-specific agent loops. Those require their dedicated worker/transport integration.

Local inference has no model-provider token charge when the selected server is self-hosted, but hardware, electricity and model licensing still apply. Router costs are estimates. The dispatcher now reserves the aggregate estimated model cost of permitted task loops before queueing a run; provider billing, tool charges and company-wide spending ledgers are outside this estimate. Unknown cloud prices block requests that specify an estimated budget ceiling. See [model routing](model-routing.md) for provider details and examples.

## Tools and individual execution environments

The catalog's string-based capability/category/transport names are extensible. Twenty-two disabled templates cover image, video, audio, graphics, CAD, PCB, 3D, rendering, simulation, writing, files, storage, web, browser, desktop, data, software and home tools. They describe integrations to configure, not installed application support.

`ToolCatalog` validates schemas and resolves grants/environment requirements. `TransportRegistry` invokes registered handlers. `HttpJsonTransport` implements fixed-endpoint authenticated JSON calls; `MCPTransport` wraps an injected MCP client. The stock dispatcher binds the `environment` command transport to each task's actual lease. Desktop, browser, media-job and application handlers must be registered explicitly. Unbound handlers fail before execution. Tool outputs include actor, agent, run and invocation provenance. The worker applies profile/actor grants and records dispatch/completion events; uncertain actions are never automatically replayed.

`EnvironmentManager` allocates a separate Docker workspace/container per execution attempt or exclusively leases a configured machine runner. It supports ownership-checked heartbeat, bounded argv execution and release. Resource allocation is journaled in a local SQLite file on one execution-manager host; this journal is separate from Simon's PostgreSQL business plans and is not a multi-host scheduler. Uncertain allocations/commands remain reserved for reconciliation rather than being retried automatically.

Docker allocation and command execution are implemented. The machine client protocol is implemented, but the remote runner daemon, VM/cloud provisioning and application installation are not included. A Windows/macOS/Linux machine must already run a service implementing the documented lease and enforcement contract. See [agent environments](agent-environments.md) for exact examples and prerequisites.

## Next integration boundary

The dispatcher, worker loops and text/JSON artifact delivery are implemented; [execution setup](agent-execution.md) describes their current operational limits. Next are chat/Work team controls, richer source-file artifacts and review, native media/CAD/PCB/desktop integrations, provider-native multimodal/tool loops, remote runner provisioning, and organization-wide resource/cost policy. Existing chat and assistant tasks continue through their current runtime until connected to this platform.
