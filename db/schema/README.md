# Schema reference

`foundation.reference.sql` preserves the readable formatting of the initial foundation schema.
It is a reference snapshot, not an executable migration or the current complete schema.
The identity tables are defined in `../migrations/0002_identity.sql`.
Migration `../migrations/0028_workspace_identity.sql` renames the legacy tenant table and
columns to workspaces without changing their UUIDs. Current application identity uses
`workspace_id`; the historical reference below retains its original spelling.

Applied migrations are immutable: their recorded checksums must continue to match.
The original foundation migration remains byte-compatible with the database initialized
before the identity phase. Add a new numbered migration for schema changes.
