# Schema authority

The numbered files in `../migrations` are the schema authority. Applied migration
checksums are immutable; add a new migration for schema changes. Earlier files form
an installation ledger, not a supported compatibility API or alternate schema.

| Migration                          | Current boundary                                                                                                  |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `0028_workspace_identity.sql`      | Workspace identity and membership naming                                                                          |
| `0029_email_password_recovery.sql` | Account recovery records                                                                                          |
| `0030_native_projects.sql`         | Shared projects, human project members and tasks                                                                  |
| `0031_native_agents.sql`           | Project roles, team policy, hashed credentials and human/agent/pool tasks; removal of retired project-file tables |

Composite foreign keys bind roles, task creators, task assignees and credentials to
one workspace/project. Creator metadata and role keys are immutable in the adapters.
Updates compare revisions; service transactions atomically persist records, audit,
outbox and command receipts. Team-size admission and current credential authority are
service invariants, not claims that arbitrary direct SQL is authorized.

Migration 0031 drops `project_artifacts`, `project_file_operations` and `project_drive`.
There is no legacy-project backfill. The old formatting-only foundation snapshot was
removed; Git retains historical documentation and all original migration bytes.

See the [native project contract](../../docs/runbooks/native-projects.md) and
[isolated database test workflow](../../docs/runbooks/database-testing.md).
