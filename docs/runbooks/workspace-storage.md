# Workspace storage authority

Implemented October 12, 2026. Open a project's **Storage** panel to inspect the
workspace allowance, retained content, pending upload reservations and that project's
usage. These are logical retained-content bytes, not physical database or disk capacity.

## Allowances and authority

An installation starts with a synthetic 256 MiB allowance per workspace. The configured
Simon account administrator can issue a versioned plan with a limit from zero to 1 TiB.
Tenant workspace owners and project owners can inspect usage but cannot enlarge their
own entitlement. No subscription billing or payment-provider integration is implied.

Every change requires the current entitlement version, a durable command key and a
reason. The prior version is preserved. The administrator can add an exact temporary
limit expiring within 30 days, remove it early, or change the base allowance. An expired
override automatically falls back to the base allowance at admission time. Existing
source-upload reservations remain liabilities and may finish after expiry or downgrade.

A downgrade below retained usage succeeds without deleting work. The panel shows the
overage; new positive-byte admissions fail with `storage_quota_exceeded`. Existing
authorized downloads, exact successful-command retries, revocation, review and cleanup
remain available. Archives and revocations retain their storage charge.

Project-authorized readers see aggregate workspace totals and their own project total.
Entitlement reasons and issuer history are restricted to workspace owners and the
administrator. Other projects' identifiers and source contents are not in this view.
History displays the most recent 50 entitlement versions; the database retains all versions.

## What counts

| Retained item                                            | Charge                                                                      |
| -------------------------------------------------------- | --------------------------------------------------------------------------- |
| Each intake source revision, including revoked revisions | Original bytes plus UTF-8 extracted text                                    |
| Each artifact revision, including superseded revisions   | Decoded original bytes                                                      |
| Each delivery packet                                     | Canonical manifest bytes plus declared generated-preview bytes              |
| Each delivery profile assignment and package validation  | Canonical immutable evidence bytes                                          |
| Each package acceptance                                  | Canonical owner decision bytes                                              |
| Each continuity handoff                                  | Canonical manifest bytes; existing artifact originals remain referenced     |
| An unfinished source upload                              | Its full original-plus-extraction reservation until finalized or reconciled |

Identical source bytes uploaded as separate revisions count separately even when disk
storage deduplicates them. Delivery components reference existing artifact revisions, so
packet creation does not charge those originals twice. Generated previews are charged
as a retained delivery obligation even though exports regenerate them deterministically.
Exports and exact retries do not add charges. Database rows/indexes/base64 overhead,
execution checkpoints, audit records, backups, temporary export buffers and network
transfer are outside this meter. Their existing bounds still apply; measured physical
capacity, compute admission and hosted tenant scheduling remain open core requirements.
Migration 0040 adds the package-evidence and continuity charges. A downgrade can block
new evidence or handoff capture while retaining existing records and authorized exports.

## Interrupted source uploads

Source admission commits a reservation before writing files. A second transaction
rechecks authority, the original intake version and the reservation before publishing the source,
consuming the reservation and saving its successful receipt. If the process stops or
the database transaction fails after writing, the reservation remains visible. An exact
retry can finish while its original intake version and authority remain valid.

An authorized project owner can reconcile an unfinished upload with a reason and the
current intake version. Reconciliation checks retained source references, other pending
uploads and the actual file/staging state before releasing the hold. Shared bytes stay
while another retained revision or pending upload accounts for them; the last abandoned
upload removes unreferenced bytes before release. A filesystem failure leaves the hold
in place. Cleanup is allowed for archived projects. A released upload command is terminal;
use a new command key for a new upload. Revocation of a completed source is not cleanup.

An artifact publication denied by quota leaves its completed model checkpoint and known
model charge intact. After capacity is restored, a permitted execution retry can publish
that checkpoint without another model call. Original deadline, attempt and authority
checks still apply.

## Persistence and API

Migration `0037_native_storage_entitlements.sql` backfills retained source, artifact and delivery
charges, including content above the default allowance. It never deletes that content.
Workspace locks and PostgreSQL insert triggers enforce atomic admission; independent
processes and direct retained-record inserts share the same ledger. Policies and retained
charges cannot be rewritten or deleted. Apply migrations through the normal operator
procedure; test results do not migrate an installed database.

- `GET /v2/projects/{project_id}/storage` — authorized usage and allowance.
- `POST /v2/projects/{project_id}/storage/entitlement` — administrator versioned change.
- `GET /v2/projects/{project_id}/storage/operations/{key}` — issuer's durable receipt.
- `GET /v2/admin/workspaces/{workspace_id}/storage` and corresponding
  `POST .../entitlement` — configured administrator management across tenants.
- `GET /v2/admin/workspaces/{workspace_id}/storage/operations/{key}` — administrator's
  durable receipt for a change in the target workspace.
- `POST /v2/projects/{project_id}/intake/sources/{source_id}/reconcile` — owner cleanup
  with `expected_version`, `reason` and `idempotency_key`.

Cross-tenant administration checks the issuer's current membership and records the real
issuer and issuer workspace in the target workspace's audit chain. Session, CSRF, origin
and workspace-selection checks apply. Retries recheck authority before reading receipts.

Legacy untracked filesystem orphans are not reconstructed by a metadata backfill. Verify
database/file backups together and inspect pre-existing source directories during rollout.
The reservation tests cover process interruption and database rollback around upload and
cleanup. File data is flushed, but directory link/unlink metadata is not explicitly made
durable across power loss on all supported platforms. Power-loss-safe publication/deletion,
matching filesystem/database restoration, remote object-store replication and a complete
physical-capacity meter remain open BASE-04 work. The supported native-core restore path
now has [enforced quarantine](storage-recovery.md#enforced-native-core-restoration-quarantine)
with an external generation witness and explicit evidenced release.

## Verification recorded October 12, 2026

The relevant existing memory/API workflow suite passed 270 cases; the final targeted
storage/source/API/domain suite passed 88 cases (these suites overlap). All 17 selected
Edge browser cases passed, covering storage administration, URL prefixes, lost responses,
version conflicts, live access changes and pending-upload cleanup, including archived and
over-quota projects. The source filesystem suite also exercised failure and staging cleanup;
the Windows symlink case requires host privileges and was skipped on this host.

Disposable PostgreSQL 16.15 with pgvector 0.8.6 passed 11 new storage/migration/restart
checks. The existing database regression suite passed 74 cases and exposed one outdated
error-class expectation: source immutability now rejects a rewrite before its foreign key
check. The corrected case separately verifies both protections and passed its rerun.
Every test cluster was stopped. Python lint/format/type checks and frontend lint/format
checks passed. These are synthetic checks, not live-model quality trials, PostgreSQL 17
acceptance, a fresh full-coverage measurement or evidence of CORE-ACCEPT.
