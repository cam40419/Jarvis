# Schema authority

The numbered files in `../migrations` are the schema authority. Applied migration
checksums are immutable; add a new migration for schema changes. Earlier files form
an installation ledger, not a supported compatibility API or alternate schema.

| Migration                          | Current boundary                                                                                                                           |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `0028_workspace_identity.sql`      | Workspace identity and membership naming                                                                                                   |
| `0029_email_password_recovery.sql` | Account recovery records                                                                                                                   |
| `0030_native_projects.sql`         | Shared projects, human project members and tasks                                                                                           |
| `0031_native_agents.sql`           | Project roles, team policy, hashed credentials and human/agent/pool tasks; removal of retired project-file tables                          |
| `0032_native_intake.sql`           | Versioned intake, immutable source revisions and planning attempts with reservations, validated proposals and current-state snapshots      |
| `0033_project_models.sql`          | Scoped project models/encrypted credentials, project/workspace policies, immutable per-call usage identity and retained intake liabilities |

Composite foreign keys bind roles, task creators, task assignees and credentials to
one workspace/project. Creator metadata and role keys are immutable in the adapters.
Updates compare revisions; service transactions atomically persist records, audit,
outbox and command receipts. Team-size admission and current credential authority are
service invariants, not claims that arbitrary direct SQL is authorized.

Intake context and attempt updates compare independent versions. Attempt request identity,
selected context and dispatch metadata cannot be rewritten by settlement. JSON snapshots
must match indexed identity/version/status/reservation columns; source labels and request
keys are unique within their projects. Source originals live in managed file storage and
must be backed up with the database.

Project model credentials are immutable encrypted revisions with composite model/project/
workspace references. Model qualification points to usage in the same project. Separate
workspace and project policy tables preserve shared versus project-only authority.
Usage entries bind each operation phase to its original model/configuration, credential
revision, prices and reservation; settlement changes outcome fields through versioned
updates. Indexed columns and JSON snapshots must agree. Unknown calls retain both
monetary holds and occupied call slots until settled or reconciled with evidence.

Migration 0033 imports any earlier intake charge/hold once and removes the three retired
intake settings from current context. That retained financial history is not a legacy
model catalog, environment-key fallback or second runtime allowance. Restore the matching
encryption master key and administrator catalog with the database to use enrolled keys.

Migration 0031 drops `project_artifacts`, `project_file_operations` and `project_drive`.
There is no legacy-project backfill. The old formatting-only foundation snapshot was
removed; Git retains historical documentation and all original migration bytes.

See the [native project contract](../../docs/runbooks/native-projects.md) and
[project model/resource contract](../../docs/runbooks/project-models.md), plus the
[isolated database test workflow](../../docs/runbooks/database-testing.md).
