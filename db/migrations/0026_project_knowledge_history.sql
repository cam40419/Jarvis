-- Activity is immutable. Keyset paging stays stable while new entries arrive.
-- The conditional expression also avoids casting unrelated Job payloads.
CREATE INDEX jobs_project_activity_history
ON jobs (household_id, created_by, kind,
    ((input->'initial_state'->>'sequence')::bigint) DESC)
WHERE kind LIKE 'platform.project_activity.%';
