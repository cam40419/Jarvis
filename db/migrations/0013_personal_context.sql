-- Existing explicitly shared conversations retain their visibility.
ALTER TABLE threads ADD COLUMN visibility text NOT NULL DEFAULT 'household'
    CHECK (visibility IN ('household', 'personal'));
CREATE INDEX threads_personal_recall ON threads (household_id, created_by, created_at DESC);
CREATE INDEX memories_personal_owner ON memories
    (household_id, (explicit_snapshot->>'created_by'))
    WHERE explicit_snapshot IS NOT NULL AND review_status = 'accepted';
