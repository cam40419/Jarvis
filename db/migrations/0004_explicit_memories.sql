ALTER TABLE memories ADD COLUMN explicit_snapshot jsonb;
CREATE INDEX memories_explicit_household_idx ON memories(household_id, created_at, id)
WHERE explicit_snapshot IS NOT NULL AND review_status = 'accepted';
