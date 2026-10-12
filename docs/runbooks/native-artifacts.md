# Native artifacts and review

Open **Artifacts & review** on a project to inspect candidate files, compare revision
metadata, request independent review, accept an exact revision or request corrections.
Completed native execution candidates are saved automatically. File publication, a
passing model review and owner acceptance are separate recorded operations.

This slice implements ART-01 and REVIEW-01 for one logical deliverable per task. Generic
files are supported; the independent model reviewer currently reads bounded UTF-8 text.
Accepted work is not publishing, email, billing or permission to invoke a business tool.

**Delivery packets** groups those existing deliverables into an immutable, downloadable
snapshot. This is partial BASE-02 delivery, with BASE-01/05 authority safeguards; the
complete baseline acceptance gate remains open.

## Delivery packets

Open **Delivery packets**, select current files, assign source/evidence/attachment roles,
and record unresolved limitations. At least one source is required. An owner can create
a packet; current project members can inspect and download permitted packets. Creating
a packet does not approve its components or complete board tasks.

The browser composer supports source, evidence and attachment roles. The API also accepts
supplied preview files with explicit `derived_from_revision_id` links to a source inside
the same packet. Those packets remain inspectable/downloadable in the browser; creating
their next version uses the API until the composer supports editing those links. The
browser blocks that operation instead of converting a linked preview into an attachment.

Each packet captures exact artifact/revision IDs and hashes, original filenames and media
types, component task/artifact versions, check records, immutable decisions, review IDs
and their version/status/verdict, source authority and review-policy/project versions.
Its canonical UTF-8 JSON manifest has a SHA-256 checksum. A subsequent version names its
immutable predecessor; concurrent attempts to create its successor have one winner.
Repeated requests recover the same packet through a durable owner-scoped operation receipt.

The ZIP contains:

- `manifest.json`: the original manifest, unchanged by later project events.
- `files/{revision-id}/{safe-filename}`: byte-exact originals, retaining their editable
  formats. `export-paths.json` maps revision IDs to safe archive paths; display filenames
  remain recorded separately.
- `previews/{revision-id}.html`: deterministic escaped UTF-8 source views, bound to source
  and preview hashes and renderer `simon.safe-text.v1`. HTML/SVG source is shown as text.
  Text views truncate at 48,000 bytes with an explicit limitation. Unsupported formats and
  invalid UTF-8 produce a labeled placeholder; the full original remains included.
- `current-status.json`: current applicability, component acceptance and limitations,
  evaluated at download. This separate file may change while historical packet bytes stay
  fixed. A still-current snapshot is not evidence that its components have been accepted.

Limits are 20 files per packet, 1 MiB per original, a 1 MiB manifest, 100 versions per
packet chain and 1,000 snapshots per project. Up to 20 owner-declared limitations are
preserved separately from generated limitations so a new version does not silently drop
owner notes. Unsupported native rendering/edit/reopen checks remain explicit. Generic
source selection does not establish that an arbitrary binary format is editable in an
installed application. Required-component schemas, package-level acceptance and native
format validators remain core implementation work.

Changes to a component head, task/review state, check/decision/review evidence, project,
policy or source authority mark the captured packet stale. Old originals and decisions
remain intact. Missing field-level dependency coverage uses conservative project-source
invalidation, not a claim of selective fact repair. If source inventory later exceeds the
512-record authority bound, existing packets remain inspectable/exportable with current
applicability marked unknown; admission of new packets/reviews is rejected safely.

Every detail/export rechecks current project access and known source revocations. Revoked
packet source evidence withholds its contents, while the list retains a minimal withheld
record. A downloaded copy cannot be recalled. Explicit source-to-claim derivation,
unrecorded provenance, full cross-runtime continuation, and metadata erasure are not
established by this slice.

The API lives at `/v2/projects/{project_id}/delivery-packets`: `GET` lists snapshots and
current file choices, `POST` creates an exact snapshot, `GET /{packet_id}` reads detail,
`GET /{packet_id}/download` exports it, and `GET /operations/{key}` recovers a receipt.
Packet pagination (`offset`, `limit`) and candidate pagination (`artifact_offset`,
`artifact_limit`) are independent. The browser retains the exact command/key across an
uncertain request and fences responses after project/workspace changes.

## Candidate files and immutable revisions

Every completed execution draft is saved in the same transaction as its fenced checkpoint
application. Its revision records the producing run, specialist identity, exact bytes,
SHA-256, size, filename and media type. Reapplying that checkpoint retrieves the same
revision instead of creating another file. A failed, cancelled or stale execution does
not publish an unfenced result.

Owners can upload a generic file to a task or submit a revision to its existing artifact.
Each file is bounded to **1 MiB**, including binary files. An artifact retains up to
**100 immutable revisions**, with sequence numbers and parent links. Originals remain
downloadable after corrections. Filenames are display names without directory paths or
header separators; no tenant filename selects a server filesystem path.

Uploading or producing a new revision moves the task to **In review** and runs recorded
content-integrity and nonempty-content checks. Those mechanical checks establish byte
integrity and nonempty output; they do not prove factual, creative or application quality.
An empty candidate cannot be reviewed or accepted.

The browser text preview renders escaped text. The API provides text as JSON; downloaded
bytes use an authenticated attachment response with `nosniff`, a sandbox content policy
and no-store caching. HTML is displayed as text; PNG, JPEG, GIF and WebP images can be
previewed through authenticated browser blobs. Generic binary files remain available
as downloads. File bytes and private model prompts are omitted from inventory/history
responses. There is no external object-store backend, streaming/resumable upload, model
image inspection, document rendering, OCR, malware scanning or compute/storage quota meter
in this slice.

## Configure the review policy

The project owner chooses an active project reviewer role and whether each candidate
requires independent agent review and human approval. Both are required by default.
The reviewer must be separate from the artifact's maker and current specialist task
assignment. A different qualified model route is supported, but model identity alone is
not independence: the separate role receives a fresh review context and no maker
conversation or tool authority. Each admitted review creates separate board work assigned
to that reviewer, with the exact target revision and hash. Its task becomes Done after
approval, Blocked for defects/uncertainty, or Cancelled when authority becomes stale.
Cancelling or editing that work fences a late result while preserving incurred cost.

Human approval can be disabled deliberately only while independent agent review remains
required. A passing current review then creates a recorded automatic acceptance decision.
An owner can instead select human-only review for files requiring their own expertise;
this permits an explicit owner decision without claiming model validation of a binary or
visual deliverable. Policy changes advance a version and invalidate earlier acceptance
under that policy.

The policy also limits repair cycles, defaulting to **three** and configurable from zero
to eight. Exhausting that allowance blocks another repair task until the owner deliberately
changes policy. These are bounds on review workflow, not permission to spend money or
publish externally.

## Independent review and exact acceptance

**Request review** binds the operation to the current artifact version, revision ID,
SHA-256, task version and policy version. A durable review attempt and one model-usage
reservation are saved before dispatch. The review call uses the qualified project
**review route** and the shared [model ledger](project-models.md), including project and
workspace spending ceilings, hosted/paid consent and concurrent-call limits.

The reviewer receives the current candidate's supported text and exact identity, project objective,
task requirements, current maker task, the latest three owner repair feedback entries
(up to 2,000 characters each), reviewer instructions and
success criteria, deterministic check results, and up to eight current intake source
excerpts of 2,000 characters each. Credential-like values are redacted before model context
dispatch. This is bounded supplied evidence, not broad retrieval
or independent verification of every claim. Embedded approval or publishing instructions
in artifact content are treated as untrusted data.

Excerpt limits do not bound authority checks. Review metadata records all current source
IDs, omitted IDs/counts and coverage, with a fingerprint covering every source revision
and revocation (up to 512 records). A ninth omitted source changing therefore invalidates
the review. Coverage describes supplied excerpts, not verification of every fact.

Human-only approval also captures project, source, owner and producing-task authority.
Those changes invalidate current acceptance without rewriting the historical decision.
A stale accepted task can reopen through a successful re-review or repair command; a
currently accepted revision cannot be accepted again under a new request key. Known
revoked review/acceptance evidence blocks artifact content and preview disclosure,
suppresses descriptions/diffs/findings/feedback in artifact detail, and blocks dependent
execution dispatch/application. Identity/hash/file-name audit metadata is retained.

The detail view and review context include a text difference between the current revision
and its immutable parent, bounded to 4,000 characters and marked when truncated or
unsupported. The full supported current candidate remains a separate review input;
that bounded difference is an aid to inspecting corrections.

Text review and preview support UTF-8 text media types, JSON and XML up to **48,000 bytes**.
The complete review request must also fit its bounded context admission: 64,000 estimated
input tokens using a conservative byte-based estimate, including schema/framing. Output
is bounded to the lower of 4,096 tokens and the configured model limit. Unsupported or
oversized candidates are stored but cannot dispatch a text reviewer; use a supported
specialist review or deliberately configured human-only review.

The provider must echo the exact revision ID and hash, give an approved or changes-requested
verdict, and supply bounded findings. Approval with blocking findings, a changed identity,
malformed/duplicate JSON, refusal, truncation or unresolved usage cannot enable acceptance.
Each artifact admits at most 100 review attempts across its revision history; a revision
admits one running attempt at a time.

A passing review still leaves a human-required project waiting for the owner. **Accept
revision** records the owner, exact revision/hash, task and policy versions and review
reference, then marks the task **Done**. Revisions, changed requirements, included source excerpts, roles,
model configuration, project state or review policy fence obsolete review authority.
Current acceptance is derived from those records; manually moving a board card to **Done**
does not create an accepted artifact. A recorded acceptance can become stale when its
underlying authority changes.

## Owner feedback and repairs

**Request changes** requires concrete feedback. It records the rejected revision/hash,
creates one linked repair task with that feedback and original brief, and blocks the
original task. Replay of the same command retrieves the same decision and task.

The repair normally keeps the original specialist assignment. If that assignment is no
longer authorized, the repair is pooled for deliberate reassignment. A pending repair
must submit its correction through its linked task; editing the original task's file
cannot silently bypass that repair. Its completed execution candidate becomes the next
revision of the same artifact and returns the original task to **In review**. A fresh
review includes the repair brief and earlier owner feedback. Final acceptance completes
the original task and the producing repair task.

## Uncertain writes, interrupted calls and access changes

All mutations use command receipts and expected versions. Retry an uncertain write with
its original command identifier and payload. `GET /operations/{key}` retrieves a safe
receipt under current owner authority. A replay does not repeat an upload, model review,
owner decision or repair task.

Known provider charges settle even when changed authority prevents applying a result.
Timeouts or interrupted dispatched reviews remain **unknown**, retaining both their money
reservation and concurrent-call slot, including with zero-fee models. Review history and
receipt replay recover an expired dispatched attempt without redispatching it. Use
**Models & usage** for evidenced workspace-owner reconciliation before admitting new paid
work. Reconciliation resolves accounting; it does not approve an uncertain candidate.

Project/workspace membership, active project state and authorizing owner authority are
checked before commands and receipt replay and again when accepting an inference result.
Read-only project members can inspect previews/history and download authorized revisions;
they cannot authorize model spending, change review policy or accept work. Board-agent and
execution-runner credentials do not grant artifact-owner control. Existing historical
records remain durable after an author loses current access.

## Storage and API

Migration `0035_native_artifacts_review.sql` persists artifacts, revision bytes, mechanical checks,
review policy, durable model attempts and owner decisions alongside native execution and
model usage. Composite scope constraints and compare-and-swap writes apply to both stores.
Migration `0036_native_delivery_packets.sql` adds immutable checksummed packet manifests,
single-successor lineage and scoped component/evidence validation. Historical artifact
decisions lacking the additive authority snapshot remain readable with stale applicability;
packet capture normalizes only that missing default, without editing the decision.
Follow [storage recovery](storage-recovery.md) for the matching database, model catalog and
model-credential master key. No operator database or deployment is changed by development
test runs. Physical storage metering and production restoration acceptance remain open.

Human endpoints are under `/v2/projects/{project_id}/artifacts`. Reads require current
project access. Mutations require current owner authority, a human session, Origin/CSRF,
idempotency keys and the relevant expected versions.

| Method and suffix                                    | Purpose                                                       |
| ---------------------------------------------------- | ------------------------------------------------------------- |
| `GET` base                                           | Policy, task/role references and paginated artifact inventory |
| `PUT /policy`                                        | Change review and repair requirements                         |
| `POST /upload`                                       | Upload a new logical artifact or immutable revision           |
| `GET /{artifact_id}`                                 | Revisions, checks, reviews, decisions and current acceptance  |
| `GET /{artifact_id}/revisions/{revision_id}/preview` | Supported text as JSON                                        |
| `GET /{artifact_id}/revisions/{revision_id}/content` | Exact authenticated file attachment                           |
| `POST /{artifact_id}/reviews`                        | Run one independent review of the exact current target        |
| `POST /{artifact_id}/decisions`                      | Accept or request corrections to the exact current target     |
| `GET /operations/{key}`                              | Recover an authorized mutation receipt                        |

Review dispatch is synchronous at the HTTP boundary after durable admission. It is not
a separate background review queue; an uncertain HTTP response is resolved through its
receipt and attempt history. The execution worker publishes candidates internally but
does not receive arbitrary upload, review-model or approval authority.

## Verification and remaining scope

October 10 delivery/authority verification passed **83 final focused domain, service,
API and memory-contract tests**. The broader non-browser/non-PostgreSQL/non-live run
passed 2,644 tests with 16 Windows capability skips; one owned-process cleanup test
failed because the restricted environment denied `taskkill`/WMI access, then passed
when rerun outside that restriction. The cleanup failure was not suppressed in code.
Targeted PostgreSQL 16.15 runs passed 22 cases plus 3 compatibility/current-head/guard
follow-ups and 1 successor-lookup check. Every disposable cluster stopped. These runs
do not establish fresh global coverage, PostgreSQL 17 acceptance or live-model quality.

Edge browser verification passed 25 combined delivery/artifact scenarios, then 3 focused
checks after the final preview-link/context-clearing fixes, covering 26 unique scenarios.
Coverage includes exact ZIP downloads, responsive owner/member views, stale approval
repair, omitted-source revocation, uncertain requests, independent pagination, successors
outside the loaded history page and project/workspace response fencing. Desktop/mobile
screenshots were inspected. Ruff, formatting, mypy across 160 source files, frontend
ESLint/Prettier, documentation links and whitespace checks passed.

Delivery packets now have an indexed successor lookup, so a later version outside the
current history page cannot cause repeated attempts to extend an obsolete predecessor.
Lists carry bounded summaries; full evidence manifests are fetched only for packet detail.

October 9 repository verification covered **2,955 passing tests** with **91.29% fresh
branch-enabled coverage**, exceeding the unchanged 90% gate. The initial full non-live
run passed 2,951 tests, with three failures and one fixture error. The historical migration
test's expected ledger was updated for 0035, and the identity browser test now waits for
its deferred Markdown renderer before evaluating it. The other two cases encountered a
PostgreSQL connection timeout and browser connection reset. All four cases passed together
on a fresh retry in 53.69 seconds. Production source stayed fixed across these runs;
coverage began fresh for the full run and appended only its four current retries, without
historical coverage. Sixteen Windows symlink/POSIX FIFO cases were skipped and eight live
model cases excluded. Both disposable PostgreSQL 16.15/pgvector 0.8.6 clusters stopped.

The full run passed all **11 artifact browser scenarios**, including authenticated image
previews, desktop/mobile access, exact acceptance, linked repairs, immutable history,
conflicts, pagination and uncertain-write recovery. It also passed all 11 execution browser
scenarios. Ruff/formatting, typing across 157 source files, ESLint/Prettier and all ten
connector checks pass.

On October 9, the focused artifact command/service/API suite plus the existing paid
dependency-change race regression passed **88 tests in 29.19 seconds**. This run covered
automatic candidate publication, byte/revision integrity, separate review work and model
routes, seeded defects, exact owner decisions, bounded repair, current corrected dependency
content, unknown outcomes, interruption recovery, late charge settlement, task cancellation,
receipt replay, safe transfers, project/workspace access and context admission without
durable work or charges. Responses were synthetic;
this focused run did not measure global coverage or PostgreSQL/browser acceptance.

Run the focused synthetic service/API checks from the repository root:

```powershell
.\venv\Scripts\python.exe -m pytest tests/unit/test_native_artifacts.py tests/unit/test_native_artifact_commands.py tests/api/test_native_artifacts_api.py tests/unit/test_native_execution_recovery.py::test_dependency_changed_during_model_call_prevents_stale_candidate_but_keeps_charge -q
```

Delivery and source-authority regression commands:

```powershell
.\venv\Scripts\python.exe -m pytest tests/unit/test_native_delivery.py tests/unit/test_native_delivery_domain.py tests/unit/test_native_artifact_authority.py tests/api/test_native_delivery_api.py tests/contract/test_native_delivery_store.py -m 'not postgres' -q
.\venv\Scripts\python.exe scripts/test_postgres.py --bin-dir .local/db-phase/pg16/Library/bin -- tests/contract/test_native_delivery_store.py tests/api/test_native_delivery_postgres_api.py tests/integration/test_workspace_migration.py -m postgres -q
$env:SIMON_BROWSER_TESTS = '1'
$env:SIMON_BROWSER_CHANNEL = 'msedge'
.\venv\Scripts\python.exe -m pytest tests/integration/test_native_delivery_browser.py tests/integration/test_native_artifacts_browser.py -q
```

Run the full suite with a fresh disposable PostgreSQL installation that already has
pgcrypto and pgvector available, using the actual local bin directory:

```powershell
$env:SIMON_BROWSER_TESTS = '1'
$env:SIMON_BROWSER_CHANNEL = 'msedge'
.\venv\Scripts\python.exe scripts/test_postgres.py --bin-dir .local/db-phase/pg16/Library/bin --test-timeout 4800 -- -q -m "not live" --cov=simon --cov-report=term-missing
```

Storage contracts, migration tests and authenticated browser scenarios are separate
checks; enable only disposable PostgreSQL/browser fixtures as described in the
[native project runbook](native-projects.md). Provider responses in these checks are
synthetic. No live inference, paid model call or operator database change is needed.

This text reviewer is an enforcing workflow with recorded evidence, not broad quality
certification. Visual/application validators, real business tools, broad retrieval,
live pilot-quality evaluation, subscription entitlements, measured storage/compute
admission and the complete core acceptance matrix remain subsequent work.
