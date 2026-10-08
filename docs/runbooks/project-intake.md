# Project intake and automatic staffing

Project intake turns a saved brief and selected evidence into a reviewed proposal for
the next milestone, its roles and its board work. Open **Intake** on a project; new
projects also open intake after their brief is saved. Saved context, evidence revisions
and planning attempts survive reloads when PostgreSQL is configured.

This slice invokes a configured text model, validates its structured proposal, then
makes a separate review call with fresh context. The review uses the same selected
endpoint with a different assessment prompt; it is not a claim that a different model
or provider independently verified the work. Accepted proposals can create native roles
and tasks. They do not execute those tasks, invoke business tools, accept artifacts or
authorize publication.

Read with [native projects and scoped teams](native-projects.md), the
[delivery roadmap](../next-phases.md), and [storage recovery](storage-recovery.md).

## Project workflow

1. Describe the current situation, desired outcomes, constraints and unresolved decisions.
   The project objective remains the main goal. Later planning questions appear as answer
   fields; save the answers before requesting another plan.
2. Choose an administrator-configured model. Cloud processing is off by default and needs
   explicit project consent. Set the project's total planning allowance and whether
   reviewed plans should add roles/work automatically; automatic staffing defaults on.
3. Save intake, then add files or a folder. Folder uploads preserve relative display labels.
   Inspect extracted previews, redactions, omitted files and revision history before
   selecting evidence for planning.
4. Generate the plan. Inspect its milestone, facts with evidence quotes, assumptions,
   conflicts, consequential questions, role reuse/new-role rationale and proposed work.
   Every new task includes acceptance criteria and creates a separate review task.
5. In manual mode, use **Apply reviewed plan** after review. In automatic mode, a valid,
   review-approved plan without blocking questions applies immediately. Adjust saved
   context and generate a new attempt when revision is needed; there is no silent
   repair loop or unlimited background staffing.

Blocking questions stop staffing and produce human decision tasks, including in manual
mode. Answer in intake and request a new plan. Repeated open questions reuse their decision
task. An applied plan can be steered through the board and team editors; cancelling a
planning attempt does not undo already-applied records.

Intake owners can configure, upload, revoke, generate, apply and cancel. Current project
readers can inspect saved intake, proposals and available source previews/originals.
Intake APIs require a human session and reject agent bearer credentials. Project and
workspace authority, archive state and versions are rechecked when a result is accepted
and again when changes are applied. Changing source inventory, project context, staffing
policy, roles or board work can make a saved proposal stale.

Unsaved edits remain in the open tab during conflict recovery. Review the saved version
before explicitly retaining a draft against it. Unknown responses keep the exact request
and idempotency key; **Retry same request** retrieves the attempt rather than dispatching
another model call. Browser storage never receives source bytes or intake drafts. Reloading
discards unsaved edits but retrieves durable intake and attempts.

## Intake API

All paths below are relative to `/v2/projects/{project_id}/intake`. Reads use a current
human session; writes additionally require same-origin CSRF and active owner authority.
The optional `X-Workspace-ID` header is an expectation guard, never a way to select
another workspace or grant access.

| Method and suffix                  | Behavior                                                                                                         |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `GET` base                         | Saved/default intake, source metadata without text, latest 20 attempts, model options, authority and cost totals |
| `PUT` base                         | Replace editable intake/context/settings with intake version comparison                                          |
| `POST /sources`                    | Store bounded base64 original bytes and a new immutable source revision                                          |
| `POST /sources/{source_id}/revoke` | Revoke evidence, clear affected stored proposals and advance context version                                     |
| `GET /sources/{source_id}/content` | Authorized original attachment, with byte-size/hash integrity verification                                       |
| `GET /sources/{source_id}/text`    | Extracted text and redaction/truncation/extraction metadata                                                      |
| `POST /analyze`                    | Persist reservation and attempt, generate, review and optionally apply                                           |
| `GET /runs/{run_id}`               | Current durable attempt, including unknown/cancelled/stale results                                               |
| `POST /runs/{run_id}/apply`        | Apply a reviewed ready proposal against current context and run version                                          |
| `POST /runs/{run_id}/cancel`       | Cancel an unapplied attempt without erasing possible usage                                                       |

Every write carries an `idempotency_key`. Intake's initial unsaved version is zero;
saving creates version one. Updating intake, adding a source and revoking a source
compare and advance the intake version. Analyze compares that context version but does
not change it. Applying/cancelling compare the attempt's separate `version`.

The same analyze key returns the current recorded attempt. Reusing it with different
input or another requester returns 409. No timed-out attempt is recreated under that
key. An API caller can select up to 12 distinct source revision UUIDs in `source_ids`;
every selected revision must still be the current parsed, unrevoked revision.

## Evidence and limits

Original bytes are stored under
`SIMON_LOCAL_FILES_DIR/.project-sources/<workspace UUID>/<project UUID>/<SHA-256>`.
User filenames are display labels, never host paths. A matching source label creates a
new immutable revision. Each original is limited to 5 MiB; a project allows 500 source
labels, 2,000 revision records and 100 MiB of original bytes counted across revisions.
Uploading does not reorganize, move or delete the external source archive.

UTF-8 text and DOCX text can be extracted. Supported text extensions include TXT, Markdown,
CSV, JSON, YAML, TOML, XML and HTML; HTML is evidence text, never rendered or executed.
Unknown or malformed formats remain inventoried as **Unread original**. PDF, images,
scans, video and spreadsheet workbooks do not gain OCR, visual understanding or workbook
semantics from being uploaded. Supply an appropriate text summary until those capabilities
are accepted separately.

Extraction applies bounded credential-pattern redaction and keeps at most 20,000
characters. Redaction is a heuristic, not a general privacy classifier. Original downloads
retain the exact bytes, including any secrets in the original, and require project access.
Intake, role/task descriptions and source labels are also redacted when composing model
context. Documents and model output remain untrusted data and cannot grant permissions.

Each attempt includes at most 12 current, parsed, unrevoked sources and the first 3,000
characters of each selected extracted text. The API's empty selection chooses up to 12
eligible sources in source-label order. Included/omitted IDs are recorded and the UI shows
coverage; the model has not read every uploaded original. Review the previews and choose
the relevant evidence explicitly for larger archives. Older revisions remain visible for
history but cannot be selected for a new attempt. Revoking the current revision does not
fall back to an older one. Revoked originals/previews are unavailable to application reads,
and stored proposal/review text that used them is cleared; existing board tasks are not
silently erased.

The planner receives current role instructions/status and up to the first 80 board tasks,
with an omitted count. Deterministic checks inspect the full supported board for duplicate
work and stale state. This slice supports up to 1,000 role records and 1,000 task records
per planning snapshot, and 1,000 planning attempts per project. Large archives and boards
need later retrieval, pagination and context-selection work rather than a claim of
whole-company comprehension.

## Staffing and review rules

Proposals contain at most 12 reused/new roles, 16 work items, 16 findings and six questions.
At most three roles may be newly created in an attempt, within the project's current
active-role limit. Each new role must own or independently review concrete new work and
explain why an existing role is insufficient. No fixed executive team is created.

Existing active roles and existing work must be reused when suitable. Applying a reuse
proposal preserves the saved role instructions and task contents. A proposed role cannot
grant team-management authority or credentials, change policy or assign itself broader
tools. Current role/task identity, duplicate names/keys, source citations and exact quoted
passages are checked locally. The review call separately assesses grounding, meaningful
reuse, excessive staffing, assignment suitability and the next milestone.

Each new work item has a separate review task assigned to a proposed independent reviewer
or an eligible human. Human makers are excluded from their own fallback review assignment;
if another human is unavailable, review remains pooled for reassignment. A review board
item is an obligation, not an accepted artifact, a completed review or publishing consent.
Creative artifact review policies remain later core work.

Roles, work, review/decision tasks, provenance, audit events and application receipt commit
together. Failure rolls back the whole application. A completed provider charge remains
recorded independently of a denied or failed application. Paid calls never hold database
locks. Model output must pass a strict bounded JSON schema before it can enter durable
proposal fields; raw rejected output is not saved as a proposal.

## Administrator model catalog

Set `SIMON_INTAKE_MODELS_FILE` to a private administrator-managed JSON file. Catalog rows
bind endpoint configurations to allowed workspace UUIDs; a project can select only those
available to its workspace. API keys remain environment-variable references, not JSON
values, browser fields or prompt content. The file is limited to 1 MiB and 100 rows.
An invalid catalog disables planning while preserving access to saved project intake.

For a separately installed and tested local OpenAI-compatible text endpoint, the catalog
shape is:

```json
[
  {
    "workspace_ids": ["11111111-1111-4111-8111-111111111111"],
    "endpoint": {
      "id": "local-intake",
      "provider": "openai_compatible",
      "model": "your-tested-model-id",
      "base_url": "http://127.0.0.1:1234/v1",
      "local": true,
      "capabilities": ["text"],
      "context_window_tokens": 131072,
      "max_output_tokens": 8192,
      "input_cost_per_million_usd": 0,
      "output_cost_per_million_usd": 0
    }
  }
]
```

Replace the workspace, model, address and capacity declarations with your actual tested
deployment. This file does not install or start a model server. A `local` flag is an
administrator trust assertion: verify that the server does not forward requests to cloud
models. Existing chat's `SIMON_MODEL_PROVIDER=local` is an offline chat provider and does
not supply AI intake. No configured endpoint means no model call and no canned fallback.

Available text transports are `openai_responses`, `openai_compatible`, `anthropic` and
`gemini`. A hosted endpoint needs `local: false`, its approved HTTPS base URL, an
`api_key_env` name and both input/output prices in USD per million tokens. Set the named
secret in the service's environment through the existing private configuration mechanism.
Operators must verify provider prices and model limits before configuration. There is no
project BYOK/key-enrollment UI, automatic hosted free-tier selection or account-wide
billing ledger in this slice.

Catalog readiness checks configuration, declared text capability, prices and the presence
of referenced credentials. It does not probe the server or certify model quality. Review
dispatch rechecks the endpoint configuration and referenced credential; changing
either while generation is in flight stops the second call. Source revocation also fences
late proposal/review text, including an attempt whose outcome was already marked unknown.
The planner also checks context capacity before dispatch: a conservative UTF-8-byte input
estimate plus framing, bounded by 64,000 input tokens per call; up to 8,192 generation
output tokens and 2,048 review output tokens, clamped to endpoint limits. Proposal JSON is
limited to 24 KiB. A model needs enough context for the evidence, schema and a complete
review candidate; overlong input is rejected instead of silently truncated by the planner.

## Allowance, interruptions and recovery

The project planning allowance is a lifetime ceiling for this intake slice, recorded as
integer micro-USD (1 USD = 1,000,000 micro-USD), default zero and maximum 1,000 USD.
Before dispatch, one durable attempt reserves conservative generation **and** review
costs. Concurrent attempts cannot both consume the same allowance; a project permits
one running attempt. Each paid generation and review is explicit, with no automatic
provider fallback, retry or schema-repair loop.

Known token usage settles at configured prices, rounded upward. Calls from local/free
endpoints can declare zero API cost, but local GPU time, electricity and hosted compute
are not measured by this allowance. It is not a provider-enforced billing guarantee,
subscription entitlement or the future ledger for execution, images, retries and tools.
The administrator's price/capacity declarations must remain accurate.

A missing provider usage record or uncertain dispatch keeps its reservation; cancellation
does not release potentially spent money. Cancelling stops application and subsequent
planning steps, but cannot promise cancellation of an already-dispatched provider call.
An expired five-minute running attempt becomes `unknown` on the next read and never
redispatches on reload/restart. If its original in-flight request returns definitive usage,
that result can still settle costs without applying a cancelled plan. There is no operator
UI for reconciling a permanently unknown reservation yet.

Migration `0032_native_intake.sql` stores context, source metadata/text and run/proposal
history with project-scoped references, immutable attempt identity and version checks.
Use the normal migration workflow for a selected development installation; development
acceptance itself only creates disposable databases. PostgreSQL and source originals must
be recovered together. Backup bundles include `.project-sources` through the managed files
root and the configured catalog as `configuration/intake-models.json`. During restoration,
stage the bundle into a fresh directory, then explicitly set `SIMON_INTAKE_MODELS_FILE`
and re-establish its named credentials. Process-only secrets need separate recovery.

## Verification boundary

Tests use synthetic transport responses and disposable workspaces to exercise actual
service/model-adapter calls, strict schemas, evidence grounding, staffing reuse, limits,
authorization changes, source revisions/revocation, duplicate requests, cost settlement,
uncertainty, cancellation and atomic application. Domain/memory checks passed 41 cases;
the first PostgreSQL persistence/migration acceptance passed 16 cases on PostgreSQL
16.15/pgvector 0.8.6 and stopped its cluster. The October 8 broader non-live regression
passed 2,263 tests, including 59 browser cases, with 15 host-dependent skips and eight
live-model cases excluded. The roadmap records consolidated evidence and remaining limits.

The final focused acceptance run passed 194 tests, including 13 real-browser cases and
PostgreSQL application restart/replay, with one Windows symlink-privilege skip. The five
new intake API/domain/service modules achieved 95.81% branch coverage. Fresh combined
coverage is 90.52%, above the unchanged 90% repository gate. Before combination, stale
measurements for the three services updated during review were discarded and replaced
with the final focused run. The broader and focused test counts overlap. Both disposable
database clusters stopped; no coverage threshold was lowered for this phase.

```powershell
.\venv\Scripts\python.exe -m pytest tests/unit/test_native_intake_models.py tests/unit/test_native_intake.py tests/unit/test_intake_sources.py tests/unit/test_intake_planner.py tests/contract/test_native_intake_store.py tests/unit/test_backup_intake.py -q -m "not postgres"
.\venv\Scripts\python.exe scripts/test_postgres.py --bin-dir .local/db-phase/pg16/Library/bin -- tests/contract/test_native_intake_store.py tests/integration/test_native_intake_migration.py tests/api/test_native_intake_postgres_api.py -q
$env:SIMON_BROWSER_TESTS = "1"
$env:SIMON_BROWSER_CHANNEL = "msedge"
.\venv\Scripts\python.exe -m pytest tests/integration/test_native_intake_browser.py -q
```

No paid model call, real pilot archive ingestion, provider account enrollment or operator
database/deployment change was made during this implementation. Capped real-model quality
evaluation, full provider/key/resource authority, durable native execution and artifact
review remain required before the core acceptance gate.
