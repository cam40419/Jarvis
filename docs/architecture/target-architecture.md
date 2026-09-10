# Jarvis and Home OS target architecture

Status: proposed architecture for review  
Date: 2026-09-09  
Scope: replacement architecture; the current implementation is not a migration baseline

## 1. Executive decision

Build one product in one monorepo, composed of independently deployable services.

- **Jarvis** is the user-facing assistant and control plane. It owns identity, conversations, context assembly, memory, tool discovery, permissions, model routing, notifications, and channel adapters.
- **Home OS** is a bounded physical-automation subsystem. It owns device drivers, telemetry, deterministic workflows, actuator policy, interlocks, and the station control plane.
- **Workers** perform environment-specific work such as Codex repository tasks, slicing, KiCad operations, vision inference, and desktop automation.
- **Channels** such as web chat, mobile, and voice are thin clients. They do not own separate memories or separate tool registries.

Home OS should not be embedded inside the conversational agent, and it should not initially be split into another repository. The safety and deployment boundary matters now; a repository boundary does not.

## 2. Why this boundary

Home OS has different correctness requirements from an assistant:

- A conversation may be retried or abandoned; a physical command must be idempotent and auditable.
- An agent can propose a plan; deterministic state machines must own long-running physical execution.
- Assistant failures should not stop ordinary home automations or corrupt device state.
- Home OS must remain usable through a direct operations UI and API when OpenAI, the internet, or Jarvis is unavailable.
- Home OS needs stricter release, simulation, and safety gates than calendar summaries or chat features.

Keeping both systems in a monorepo still provides shared types, atomic contract changes, one developer workflow, and simple deployment during the early phases.

## 3. System map

```text
 voice satellite     PWA / phone       CLI / webhook       scheduled event
       |                  |                  |                    |
       +------------------+------------------+--------------------+
                                  |
                         Jarvis Gateway/API
                    auth, sessions, streaming, rate limits
                                  |
                         Assistant Runtime
             context assembly, routing, tool selection, runs
                    /             |               \
          Context service    Capability broker    Job service
          memory/retrieval   authz/tool catalog   durable workflows
                    \             |               /
                     \        Policy engine      /
                      +------------+-------------+
                                   |
             +---------------------+-----------------------+
             |                     |                       |
        SaaS adapters         Home OS API             Worker manager
      calendar/mail/etc.   jobs/devices/telemetry   Codex/KiCad/vision
                                   |
                         Home OS orchestration
                      state machines + safety policy
                                   |
                       HA hub and native drivers
                                   |
                   printer / Shelly / arm / cameras
```

The dependency arrow always points downward. A device driver never imports assistant code. Home OS never depends on a chat session. A channel never talks directly to a device or SaaS provider.

## 4. Shared context: what it means

“Shared context” should mean shared durable records and a common retrieval policy, not a single endlessly growing prompt.

Each interaction creates a **run** with an immutable context snapshot. The runtime assembles that snapshot from:

1. identity and active persona;
2. channel and device metadata;
3. the current thread and a compact thread summary;
4. relevant semantic memories;
5. active projects, tasks, and recent job results;
6. live data fetched from authoritative systems;
7. the permitted capability manifest for this run;
8. applicable policies, grants, and confirmation state.

This gives chat and voice the same knowledge without pretending they are the same interaction. A spoken follow-up can target the same thread, while a kitchen satellite can default to a short-lived voice thread. The user can explicitly say “continue the PCB conversation” to bind a new channel to an existing thread.

### Context categories

| Category | Authority | Storage | Retrieval rule |
|---|---|---|---|
| Conversation | Jarvis | Postgres | recent turns plus summary |
| User facts/preferences | Jarvis memory | Postgres + vector index | relevance, confidence, scope |
| Project knowledge | Git/files and indexed artifacts | object store + index | project-scoped retrieval |
| Device state | Home OS/HA | source system | fetch live; do not memorize as fact |
| Calendar/mail | provider | source system | fetch live with account scope |
| Job state | job owner | Postgres | fetch by job/thread/project |
| Procedures/persona | versioned `brain/` | Git | pinned release per run |
| Secrets | secret manager | never in prompt or memory | injected only at adapter boundary |

Every memory record needs `subject`, `scope`, `kind`, `content`, `source`, `confidence`, `valid_from`, `valid_until`, `sensitivity`, and provenance links. Inferred memories should remain proposals until accepted by policy or user review.

## 5. One capability plane for every assistant

Do not give each channel its own tools. Create a central **capability broker** with a canonical catalog.

Each catalog entry describes:

- stable capability name and version;
- JSON input/output schema;
- owning adapter or service;
- risk class: `read`, `write_soft`, `write_hard`, or `dangerous`;
- required scopes and eligible identities;
- idempotency behavior;
- timeout and retry semantics;
- confirmation requirements;
- network and execution location;
- audit and redaction rules.

At run time, the broker computes a small capability manifest using identity, channel, location, task classification, grants, service health, and policy. The model sees only that manifest. Tool search can expose additional schemas lazily, but authorization is checked again when executing a call.

Use three adapter types behind the broker:

- **Internal typed APIs** for Home OS and core Jarvis services. These are the strongest contracts and should be preferred for safety-critical actions.
- **MCP clients** for third-party or rapidly evolving tool ecosystems.
- **Worker jobs** for long-running or environment-bound work. The apparent tool returns a job ID immediately rather than holding a model tool call open.

MCP is an interoperability boundary, not the internal event bus and not the security policy. MCP servers never receive broad credentials merely because the model can discover them.

## 6. Codex integration

Codex is a specialized worker, not the master copy of Jarvis memory.

The assistant submits a `coding_task` containing a repository ID, worktree/branch policy, objective, selected context artifacts, acceptance criteria, allowed commands/network policy, and approval mode. The Codex worker streams normalized events and finishes with a structured result: changed files, diff/commit reference, tests, unresolved risks, and resumable thread/session reference.

Use the Codex SDK first for bounded background work. Evaluate App Server when the PWA needs a first-class interactive Codex surface with richer event streaming, approvals, and thread controls. Put either implementation behind a `CodingWorker` port so Jarvis does not depend on Codex protocol details.

Codex context should come from:

- repository files and local `AGENTS.md` instructions;
- the task envelope generated by Jarvis;
- explicitly attached architecture decisions and project memories;
- scoped tool credentials supplied by the worker environment.

Do not copy the entire personal memory store into a coding session. Codex writes results back through the job service; the memory pipeline later extracts durable decisions from those results.

## 7. Voice and chat

Voice and chat share the gateway, identity, thread service, context service, runtime, and capability broker.

Voice adds a real-time media path:

```text
wake/VAD -> speech-to-text -> intent/runtime -> tools -> response text -> speech
```

Use a fast conversational path for ordinary voice turns and hand off durable work to the same job service used by chat. The voice response should say that a job started and provide a concise status; the PWA receives the detailed artifact.

Physical confirmation must never be inferred from a spoken “yes.” Voice may initiate a confirmation request, but `write_hard` and `dangerous` actions require a signed confirmation bound to the exact action digest, user, expiry, and nonce. The PWA/passkey flow issues that token.

Channel-specific presentation belongs after the core result: concise speech, richer Markdown for chat, and structured cards for the PWA. The underlying run and audit record remains one canonical object.

## 8. Home OS internals

Home OS exposes a narrow versioned API to Jarvis:

- list device capabilities and health;
- query current and historical state;
- validate a proposed job;
- create/cancel/inspect a Home OS job;
- request and consume a confirmation;
- subscribe to normalized events;
- perform explicitly approved emergency actions.

Internally it contains:

- **drivers** for Bambu, Shelly, Moveo, cameras, sensors, and Home Assistant;
- a **device registry** mapping stable logical IDs to driver endpoints;
- **workflow definitions** for printing and plate recycling;
- a **policy kernel** that wraps every actuator call;
- an **event/telemetry pipeline**;
- a **simulation layer** for drivers and workflows;
- an **operator API/UI** that works without Jarvis.

Home Assistant remains the commodity-device hub. Native Home OS drivers are appropriate where HA lacks the required fidelity or where the workflow needs a tighter local contract. Avoid duplicate ownership: each logical device has one authoritative command path.

## 9. Jobs and eventing

Use durable jobs for anything that can outlive a single request, has side effects, waits on the world, or needs recovery. Model calls are steps inside jobs, never the workflow engine.

Every job records:

- type and schema version;
- owner identity and originating channel/thread/run;
- state, step, progress, timestamps, and deadline;
- input snapshot and referenced artifacts;
- idempotency key;
- policy decisions and confirmation tokens;
- attempts, leases, heartbeats, and cancellation state;
- output artifacts and failure classification;
- parent/child relationships.

Start with Postgres as the source of truth and a transactional outbox for events. This avoids introducing a separate event broker before load requires it. Workers lease jobs with `SELECT ... FOR UPDATE SKIP LOCKED`, heartbeat them, and make step transitions transactionally. Add a broker later for fan-out or high-rate telemetry; do not make it authoritative.

Store high-volume telemetry and images outside ordinary job rows: time-series tables or a later time-series store for measurements, and S3-compatible object storage for images, models, logs, and generated artifacts.

## 10. Security and trust boundaries

The application, not the model, enforces authorization.

- Authenticate humans at the gateway; propagate a signed internal identity context.
- Use workload identities between services and short-lived credentials where possible.
- Separate personal, professional, and household scopes in data and credentials.
- Keep provider refresh tokens in a secret manager or encrypted credential store.
- Treat email, web pages, uploaded models, and retrieved documents as untrusted content.
- Never allow untrusted content alone to widen a capability manifest or authorize a hard write.
- Require idempotency keys for external writes and action digests for confirmations.
- Audit requested action, policy decision, effective identity, adapter result, and redacted arguments.
- Default-deny network access from workers; explicitly allow the destinations a task needs.
- Put Home OS and workers on the appropriate VLANs; expose no public inbound ports.

Emergency behavior is deterministic and local. E-stops, thermal/power limits, and device safety scripts do not depend on an LLM, Jarvis, or internet connectivity.

## 11. Deployment shape

Begin as a **modular monolith plus workers**, not a fleet of microservices.

One server deployment can initially run:

- `jarvis-api`: gateway, assistant runtime, context, capabilities, notifications;
- `jarvis-worker`: general durable jobs and scheduled tasks;
- `homeos`: device API, policy, workflows, low-rate events;
- Postgres with pgvector;
- Redis only if needed for transient streaming/presence, never durable truth;
- S3-compatible object storage;
- reverse proxy and Tailscale access.

Machine-specific workers run separately:

- `worker-codex` near repositories and development credentials;
- `worker-fabrication` near OrcaSlicer/KiCad and project files;
- `worker-vision` near the GPU/cameras;
- future room voice services/satellites.

Split a module into its own process only when it needs an independent failure domain, hardware/network placement, scaling profile, privilege set, or release cadence.

## 12. Recommended monorepo

```text
Jarvis/
  apps/
    api/                    # HTTP/WebSocket/SSE gateway
    web/                    # PWA
    voice/                  # voice session gateway
    worker/                 # general job runner/scheduler
    homeos/                 # separately deployable Home OS service
  packages/
    assistant/              # run orchestration and model adapters
    context/                # context assembly and retrieval policy
    memory/                 # memory records, extraction, review
    capabilities/           # catalog, manifests, authorization bridge
    jobs/                   # job contracts and persistence
    policy/                 # common policy primitives; no device logic
    integrations/           # calendar, mail, drive, MCP adapters
    contracts/              # API/event schemas and generated clients
    observability/          # logging, tracing, audit helpers
  homeos/
    domain/                 # device and workflow domain types
    drivers/                # HA, Bambu, Shelly, Moveo, cameras
    workflows/              # deterministic state machines
    safety/                 # interlocks and actuator policy
    simulation/             # fake hardware and scenario fixtures
  workers/
    codex/
    fabrication/
    vision/
  brain/
    personas/
    routing/
    grants/
    triage/
    skills/
    evals/
  db/
    migrations/
    seeds/
  deploy/
    compose/
    systemd/
    network/
  docs/
    architecture/
    decisions/              # ADRs
    runbooks/
  tests/
    contract/
    integration/
    scenarios/
```

Use one primary implementation language for domain services—Python is reasonable given AI, vision, and hardware libraries—and TypeScript for the web client. Generate clients/types from OpenAPI or JSON Schema instead of maintaining duplicate handwritten shapes.

## 13. Core data model

The first schema should include:

- `users`, `identities`, `households`, `projects`, `memberships`;
- `channels`, `threads`, `messages`, `runs`, `run_context_items`;
- `memories`, `memory_sources`, `memory_reviews`;
- `capabilities`, `grants`, `policy_decisions`, `confirmations`;
- `jobs`, `job_steps`, `job_events`, `job_artifacts`, `outbox_events`;
- `providers`, `connections`, encrypted credential references;
- `devices`, `device_capabilities`, `device_state_current`;
- `audit_events` with append-only retention.

Use UUIDv7-style identifiers for externally referenced objects. Put `tenant/household`, user, project, and sensitivity scope on records that may cross contexts. Add row-level access tests even if the first deployment has one user; retrofitting scope later is dangerous.

## 14. API and contract rules

- Version external and cross-process contracts from the beginning.
- Commands express intent (`start_print_job`), not low-level device sequences.
- Events describe completed facts (`print_job_started`) and carry event IDs, source, schema version, timestamp, correlation ID, and causation ID.
- All mutating commands accept an idempotency key.
- Use structured error codes: retryable, permanent, policy-denied, confirmation-required, unavailable, and needs-human.
- Never expose driver-specific identifiers above Home OS without a stable logical wrapper.
- Persist the exact prompt/configuration release, model, tool manifest, and context provenance for every agent run.

## 15. Observability and evaluation

Correlate channel request -> assistant run -> tool call -> job -> driver operation with one trace/correlation ID. Keep operational logs, security audits, model traces, and raw sensitive content under separate retention/redaction policies.

Build scenario tests before autonomy:

- prompt-injected email asks the printer to start;
- duplicate job delivery and process restart mid-step;
- printer goes offline after upload;
- stale confirmation or altered action digest;
- voice identity ambiguity;
- bad vision verdict and contradictory physical sensor;
- Home Assistant unavailable while native safety still operates;
- Codex worker changes unexpected files or fails verification.

The `brain/evals` suite should measure task success, tool selection, permission compliance, memory precision, latency, and cost. Home OS simulation tests should verify invariants, not model behavior.

## 16. What to retain from the old repository

Retain only concepts and history:

- the repository and commit history;
- the idea of a tool registry, redesigned as the capability catalog;
- the basic distinction between conversations, messages, and tools.

Replace the implementation. The current synchronous six-step loop, global current conversation, dynamic import of every tool, MySQL-only memory, direct environment-secret loading, and lack of jobs/policy/scopes are incompatible with the target system. Archive the old tree on a tag or branch before replacement; do not incrementally evolve it into the new architecture.

## 17. Phased delivery plan

### Phase 0: decisions and executable skeleton

Deliverables:

- ADRs for the product boundary, monorepo, job engine, identity/scope model, memory policy, and confirmations;
- threat model and action risk matrix;
- contract package with one read tool and one durable job;
- local Compose environment, migrations, CI, linting, typing, tests, and secret-handling baseline;
- archive tag for the legacy implementation.

Exit test: a PWA request creates a run, invokes a fake capability, persists full provenance, and streams a result.

### Phase 1: assistant vertical slice

- gateway, passkey authentication, threads, runs, and SSE/WebSocket updates;
- context assembler with conversation summaries and explicit user memories;
- capability catalog and deny-by-default authorization;
- Google Calendar read-only integration;
- general job service, scheduler, notifications, and audit UI;
- model adapter abstraction with one cloud implementation and fake test model.

Exit test: the same calendar question asked by chat and voice receives context-consistent answers, with no duplicated memory or channel-specific tools.

### Phase 2: Home OS read and soft control

- Home OS service, HA adapter, Shelly driver, device registry, simulator;
- live state queries and historical telemetry;
- soft-write routines with idempotency and policy enforcement;
- direct Home OS operations page independent of chat.

Exit test: Jarvis and the operator UI use the same typed Home OS API, while a denied command cannot reach a driver.

### Phase 3: print-from-link

- URL ingestion and trust labeling;
- artifact scanning/storage and OrcaSlicer worker;
- Bambu LAN driver and deterministic print state machine;
- review thresholds, signed confirmation, AutoSwap integration;
- recovery tests for service restarts and device disconnects.

Exit test: an approved link becomes a traceable print job; duplicate delivery cannot start a second print.

### Phase 4: Codex and engineering workers

- CodingWorker contract and Codex SDK pilot;
- repository/worktree isolation, event normalization, result artifacts;
- interactive App Server evaluation for the PWA;
- KiCad connector spike behind the fabrication worker contract.

Exit test: Jarvis launches a bounded repository task, streams progress, receives verified results, and resumes it without leaking unrelated personal context.

### Phase 5: voice and proactive operation

- phone/laptop voice first, then room satellites;
- speaker/location metadata and voice-safe response rendering;
- proactive event triage limited to reads and soft writes;
- morning brief and review inbox;
- local speech and local fallback paths.

Exit test: loss of cloud inference degrades gracefully; physical confirmation still requires the signed UI flow.

### Phase 6: vision and robotic station

- camera capture service, labeled dataset, verdict contracts;
- inspection workflow and operator review;
- allowlisted arm routines, station sensors, power gating, e-stop integration;
- plate recycle state machine introduced one physical transition at a time.

Exit test: every unexpected reading goes to `NEEDS_HUMAN`; no model output directly commands motion.

## 18. Decisions to make before implementation

These do not block the architecture, but they should become ADRs during Phase 0:

1. Server OS, container strategy, backup target, and GPU placement.
2. Router/VLAN capability and whether HA runs on the same or separate host.
3. Workflow library: a Postgres-native implementation first versus Temporal later. Prefer the former until operational needs justify another control plane.
4. Secret storage suitable for the chosen OS and backup/recovery model.
5. Retention periods for conversation content, camera media, traces, and audit events.
6. Household/multi-user behavior, guest access, and voice identity expectations.
7. Professional-data boundary: accounts and projects allowed to mix, if any.
8. Exact confirmation matrix and emergency-stop behavior.
9. Local model roles: outage fallback only, privacy-sensitive routing, or routine cost reduction.
10. Whether the first Codex UI needs App Server interactivity or only background SDK jobs.

## 19. Near-term backlog

The next planning/build sequence should be:

1. Recover or recreate the missing detailed `homeos-phase1-design.md` and `homeos-agent-design.md`; only their summary was available for this plan.
2. Write the six Phase 0 ADRs and threat model.
3. Inventory hardware, host OS, network, accounts, repositories, and current HA state.
4. Define canonical capability, job, event, memory, and confirmation schemas.
5. Build a simulator-first vertical slice before connecting real credentials or devices.
6. Validate Google Calendar, Home Assistant, Bambu LAN, and Codex integrations independently.
7. Establish restore tests and audit review before allowing any hard write.

## 20. Explicit non-goals for the first release

- general autonomous physical planning;
- an LLM-owned control loop;
- one universal prompt containing all personal data;
- exposing every MCP tool on every turn;
- rewriting Home Assistant device integrations;
- cross-account professional/personal search by default;
- distributed microservices without an identified failure or privilege boundary;
- training a custom local model before the core data and eval pipelines exist.

