# Project intake and automatic staffing

Project intake turns a saved brief and selected evidence into a reviewed proposal for
the next milestone, its roles and its board work. Open **Intake** on a project; new
projects also open intake after their brief is saved. Saved context, evidence revisions
and planning attempts survive reloads when PostgreSQL is configured.

This slice invokes a configured text model, validates its structured proposal, then
makes a separate review call with fresh context. Planning and review use the project's
configured routes, which may select the same or different models. A separate assessment
prompt is not a claim that another provider independently verified the work. Accepted
proposals can create native roles
and tasks. They do not execute those tasks, invoke business tools, accept artifacts or
authorize publication.

Read with [native projects and scoped teams](native-projects.md),
[project models and resource limits](project-models.md), the
[delivery roadmap](../next-phases.md), and [storage recovery](storage-recovery.md).

## Project workflow

1. Describe the current situation, desired outcomes, constraints and unresolved decisions.
   The project objective remains the main goal. Later planning questions appear as answer
   fields; save the answers before requesting another plan.
2. Open **Models & usage** to enroll and qualify a model, choose planning/review routes
   and configure project permissions and spending limits. Hosted and paid processing are
   off by default. In intake, choose whether reviewed plans should add roles/work
   automatically; automatic staffing defaults on.
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

| Method and suffix                  | Behavior                                                                                                        |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `GET` base                         | Saved/default intake, source metadata without text, latest 20 attempts, authority and project model cost totals |
| `PUT` base                         | Replace editable intake/context/settings with intake version comparison                                         |
| `POST /sources`                    | Store bounded base64 original bytes and a new immutable source revision                                         |
| `POST /sources/{source_id}/revoke` | Revoke evidence, clear affected stored proposals and advance context version                                    |
| `GET /sources/{source_id}/content` | Authorized original attachment, with byte-size/hash integrity verification                                      |
| `GET /sources/{source_id}/text`    | Extracted text and redaction/truncation/extraction metadata                                                     |
| `POST /analyze`                    | Persist reservation and attempt, generate, review and optionally apply                                          |
| `GET /runs/{run_id}`               | Current durable attempt, including unknown/cancelled/stale results                                              |
| `POST /runs/{run_id}/apply`        | Apply a reviewed ready proposal against current context and run version                                         |
| `POST /runs/{run_id}/cancel`       | Cancel an unapplied attempt without erasing possible usage                                                      |

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

## Models, limits and interruptions

[Project models and resource limits](project-models.md) owns current catalog setup,
write-only project credentials, qualification, routing, shared budgets and reconciliation.
Intake has no separate model selector, cloud grant or planning allowance. Its model
summary and spending totals use that shared project authority, including qualification
charges. The automatic-staffing choice remains an intake setting.

The planner checks context capacity before dispatch: a conservative UTF-8-byte input
estimate plus framing, bounded by 64,000 input tokens per call; up to 8,192 generation
output tokens and 2,048 review output tokens, clamped to the chosen endpoint limits.
Proposal JSON is limited to 24 KiB. A model needs enough context for the evidence,
schema and complete review candidate; overlong input is rejected before dispatch.
A small qualification pass does not establish reliable handling of that whole context.

Before dispatch, generation and review are reserved atomically against both workspace
and project ceilings, with separate durable usage entries. Each reservation consumes
a call slot, so intake needs at least two available slots. A project permits one running
planning attempt. There is no automatic provider fallback, retry or schema-repair loop.
Current authority, credentials, template and resource policy are rechecked before each
call; context changes and source revocation prevent stale proposal application.

Known usage settles independently even if another stage is uncertain. Unsent review
reservations can be released; cancellation cannot erase a possibly incurred generation
charge or promise cancellation at the provider. An expired five-minute attempt becomes
unknown on recovery without redispatch. Unknown usage retains both money and a call
slot until a definitive result or evidenced workspace-owner reconciliation resolves it.
Late usage can settle without applying cancelled work. Reconciliation and any later
provider-charge adjustment remain visible in **Models & usage**.

Migration `0032_native_intake.sql` stores context, source metadata/text and planning
history; `0033_project_models.sql` adds the shared model ledger, imports any prior
intake liabilities once, and removes the former intake-only model settings. Historical
attempts remain evidence, not a second runtime budget or model authority.

Recover the database, managed originals, administrator catalog and matching encryption
key together using [storage recovery](storage-recovery.md). Source originals remain
under the managed files root's `.project-sources` directory. The catalog is included as
`configuration/model-catalog.json`; `SIMON_MODEL_CATALOG_FILE` points to its restored
location. Enrolled provider keys reside as scoped ciphertext in the database and require
the original master key. No provider environment variable restores project enrollment.

## Verification boundary

Acceptance uses synthetic transport responses and disposable workspaces to exercise actual
service/model-adapter calls, strict schemas, evidence grounding, staffing reuse, limits,
authorization changes, source revisions/revocation, duplicate requests, cost settlement,
uncertainty, cancellation and atomic application. Database tests also cover application
restart and replay without redispatch. Browser cases exercise source management,
questions, review, application and changed authority with the shared model settings.
The [roadmap](../next-phases.md#verification-and-remaining-limits) owns current consolidated
test counts, coverage and remaining limits. Local database acceptance uses PostgreSQL
16.15/pgvector 0.8.6; PostgreSQL 17 remains the CI/container target.

```powershell
.\venv\Scripts\python.exe -m pytest tests/unit/test_native_intake_models.py tests/unit/test_native_intake.py tests/unit/test_intake_sources.py tests/unit/test_intake_planner.py tests/contract/test_native_intake_store.py tests/unit/test_backup_intake.py -q -m "not postgres"
.\venv\Scripts\python.exe scripts/test_postgres.py --bin-dir .local/db-phase/pg16/Library/bin -- tests/contract/test_native_intake_store.py tests/integration/test_native_intake_migration.py tests/api/test_native_intake_postgres_api.py -q
$env:SIMON_BROWSER_TESTS = "1"
$env:SIMON_BROWSER_CHANNEL = "msedge"
.\venv\Scripts\python.exe -m pytest tests/integration/test_native_intake_browser.py -q
```

No paid model call, real pilot archive ingestion, provider account enrollment or operator
database/deployment change was made during this implementation. Current project
enrollment, routing and inference limits are described in the model
runbook; the roadmap records the current validation results. Capped real-model quality
evaluation, broader resource authority and artifact review remain required before the
core acceptance gate. [Native execution](native-execution.md) now advances staffed tasks
through bounded leased steps, waits and candidate output; an intake proposal's review
does not accept those later deliverables.
