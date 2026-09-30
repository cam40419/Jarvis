ALTER TABLE workflow_versions
ADD COLUMN deleted_at timestamptz;

CREATE INDEX workflow_versions_active_owner
ON workflow_versions (household_id, actor_id)
WHERE deleted_at IS NULL;
