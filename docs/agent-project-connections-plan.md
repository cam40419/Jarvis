# Agent, project and connection redesign

Updated: 2026-10-05

## Direction

Keep the existing project data and execution history. Replace capability bundles and
per-agent integration grants with roles and concrete working instructions. Every role
can use workspace tools through the requesting user's connected accounts and current
permissions. A disconnected account is unavailable to every role; credentials never
become part of an agent prompt or a client-readable configuration.

Projects are durable workspaces. Their objective, team, files, findings, queued requests,
iteration policy and history survive individual runs. Plans and resource allocations
are internal scheduling details, rather than something the user reconstructs for every
iteration.

## Implemented foundation

- Agent and project member editors save a role title and working instructions without
  a skill-selection requirement. Shared roles receive the full authorized tool catalog;
  the scheduler selects ready tools and a compatible runtime for each task.
- Saved assignments no longer depend on a hash of the whole installation. Changes to
  unrelated tools, agents and model choices do not invalidate them. Used tool contracts,
  model endpoints, runtime isolation and live account permissions are still checked.
  A blocked plan can be refreshed on run admission after a connection is fixed, preserving
  its original objective and historical plan. Running work retains its selected tools,
  model and role instructions.
- Connections opens as a larger setup center. It lists the complete current tool catalog,
  service account setup, model readiness and execution runtimes, with search and refresh.
  Account tests are explicit, read-only, and save their result and timestamp in the
  encrypted connection's backend record. They never send messages or place orders.
- UI-managed service accounts cover ClickUp, GitHub, Box, Dropbox, OneDrive/SharePoint,
  WebDAV, RobbinsHome, Twilio and merchant gateways. Google application and account
  authorization and account email delivery retain their UI setup. Site administrators can
  save an OpenAI API key; live agent credentials and an already enabled OpenAI chat
  adapter use the backend key without users editing configuration files.
- Creating a project can save automatic iterations, a standing objective, cadence,
  iteration limit and finite model budget together. Its bounded policy admits ordinary
  project work through connected accounts. Irreversible external commitments retain
  their separate review and confirmation flow.
- Docker runtime tests inspect installed images without creating containers. Run
  admission checks the selected Docker runtimes before starting a worker.
- Untouched generic extension examples are excluded from the active manifest, including
  when loading older installations. New starter manifests contain real tool contracts
  only. Customized extension contracts remain intact. Connection readiness and worker
  planning therefore use the same catalog without advertising unfinished templates as
  services disabled by configuration.
- Project Files now includes persisted file-location selection, with multiple Google
  Drive folders and connected Box, Dropbox, OneDrive or WebDAV roots. Local project
  subfolders can also be selected. Existing linked Drive projects default to their
  Drive folder; other projects default to their local project folder. Choices are
  versioned separately from teams, tasks and run history.
- Shared-agent task assignments omit unconfigured tools, including explicit generated
  tool lists and tools that become unavailable between queuing and execution. Project
  planning uses the same location filter as worker admission. Account-wide cloud file
  tools are replaced by operations bound to the project's chosen locations, with
  Google folder ancestry checks and existing bounded cloud adapters. Missing accounts
  are omitted from agent location discovery without blocking work through other roots.
- Choosing a different set of locations also prevents automatic output copies into a
  removed primary Drive destination. Task evidence and original generated outputs
  retain their internal durable copies. Additional locations are available for explicit
  file operations; output replication currently targets the selected primary Drive link.

## Next architectural work

1. Build a genuine connection installer for additional providers. MCP servers need
   authenticated discovery, imported schemas,
   server-side grant storage and a read-only connection test. Unsupported desktop,
   simulation and rendering transports need real adapters before presenting them as
   usable integrations. Do not label installation placeholders as connected accounts.
2. Move resource provisioning and model inventory into administrator UI workflows.
   Saving an API key does not provision Docker, install images, enable a previously
   disabled chat adapter, or establish model cost rates. Machine runners need a defined
   health protocol. Model generation and paid third-party operations require explicit
   spending limits beyond read-only credential tests.
3. Add OAuth refresh and folder discovery for storage providers. Current token forms
   persist tokens safely but do not renew expiring Box, Dropbox or Microsoft tokens.
   OneDrive currently asks for drive, folder and download host identifiers in the UI.
   GitHub repository access and storage roots remain explicit account boundaries.
4. Consolidate connection readiness into one service shared by chat, planning, workers
   and UI. Distinguish configuration validity, authentication checks, expired credentials,
   provider outages and installed runtimes. Tests should show permission coverage rather
   than implying a read test proves write access. A gateway health check requires its
   provider to implement a health endpoint.
5. Replace heuristic runtime selection for standalone work with scheduler-owned resource
   requirements. Project planning already decomposes work across runtimes. A task uses
   one isolated runtime at a time; universal role access does not make incompatible CAD,
   browser and coding runtimes interchangeable inside a single container.

## Efficiency, security and cleanup notes

- The planner now sees similar tool rosters for every role. Store one shared catalog in
  planner context and reference it from role records to reduce repeated tokens and scans.
  Cache nonsecret readiness within a request; continue resolving credentials live at calls.
- Retire hidden skill-picker DOM and JavaScript after compatibility checks. Keep legacy
  saved role deserialization until existing projects and saved agents are migrated. The
  old bundle-composition path can then be removed rather than maintained beside shared
  roles indefinitely.
- Separate provider-specific connect, bind and test adapters from IntegrationService.
  Its growing provider branches and reuse of the ClickUp HTTP transport for unrelated
  test adapters should become an explicitly injected shared HTTP client.
- Preserve tenant and actor isolation, current membership checks, privacy restrictions,
  endpoint validation, fixed cloud API hosts, folder ancestry checks, secret encryption,
  CSRF protection, runtime isolation and commitment confirmations. Universal role tools
  must not mean another user's credentials or arbitrary machine access.
- Complete the hosting, shared rate-limiting, backup encryption-key custody and deployment
  checks in production-readiness-review.md before calling this a production deployment.
  Do not remove audit compatibility keys, project histories or legacy rows as a teardown.

## Verification

Focused tests cover role-only creation and project capture, optional connections not
blocking unrelated tasks, unrelated configuration changes, used contract fences, model
selection continuity, refreshed blocked plans, storage binding, owner isolation,
credential privacy and persisted read-only test results. Browser checks cover role edits,
legacy-role conversion, recommendation review, connection save/reconnect, and mobile
layout. The complete repository suite has pre-existing project recovery and artifact
review failures recorded in the production readiness review; it is not a green baseline.

Validation on this machine: 321 tests passed in the main regression sweep, with additional
focused account-test and browser checks passing. Ruff, ESLint and mypy passed. The local
API and updated assets returned HTTP 200 after restart; all three service tasks are running.
The live catalog resolves 22 roles to 117 authorized tools per role, and all six enabled
Docker runtime image checks passed. No new external account credentials were supplied or
connected, and no messages, purchases or bookings were sent as tests.

The follow-up catalog correction passed 163 regression tests and two connection browser
tests, plus Ruff, ESLint, Prettier and mypy. After restarting the idle local services,
the application catalog contains zero untouched extension templates and zero tools
disabled by server enable flags. Actual unlinked accounts retain their UI setup messages.

The file-location follow-up passed 26 focused storage/readiness/API checks and the
main 130-test platform regression sweep. Additional runtime and CLI regressions passed;
the optional PostgreSQL contract cases were skipped without a test database. Eleven
browser cases passed across file-location persistence, project navigation and account
connections, including two cases rerun after fixing run-history refresh state and a
test's ambiguous cadence field selector. Ruff, ESLint, Prettier and mypy passed.
The existing local project resolved to its linked Google Drive location with Box absent
from its ready tools. No external account credentials or project files were changed.
