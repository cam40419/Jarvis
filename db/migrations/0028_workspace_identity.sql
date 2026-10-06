-- Rename the tenant boundary without changing UUIDs, snapshots or audit hash inputs.
-- Stop API and workers together before applying: old binaries use the previous columns.
ALTER TABLE households RENAME TO workspaces;

DO $$
DECLARE
    target record;
BEGIN
    FOR target IN
        SELECT table_schema, table_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND column_name = 'household_id'
    LOOP
        EXECUTE format('ALTER TABLE %I.%I RENAME COLUMN household_id TO workspace_id',
                       target.table_schema, target.table_name);
    END LOOP;
END $$;

ALTER TABLE threads DROP CONSTRAINT threads_visibility_check;
UPDATE threads SET visibility = 'workspace' WHERE visibility = 'household';
ALTER TABLE threads ALTER COLUMN visibility SET DEFAULT 'workspace';
ALTER TABLE threads ADD CONSTRAINT threads_visibility_check
    CHECK (visibility IN ('workspace', 'personal'));
UPDATE memories SET scope = 'workspace' WHERE scope = 'household';

-- Preserve user-selected names. Only replace known generated development labels.
UPDATE workspaces SET name = 'Development workspace' WHERE name = 'Development household';
