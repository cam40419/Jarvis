CREATE TABLE workflow_schedules (
    id uuid PRIMARY KEY,
    household_id uuid NOT NULL REFERENCES households(id),
    actor_id uuid NOT NULL REFERENCES users(id),
    definition_id uuid NOT NULL,
    definition_version integer NOT NULL,
    version integer NOT NULL,
    next_run_at timestamptz,
    snapshot jsonb NOT NULL,
    FOREIGN KEY (definition_id, definition_version)
        REFERENCES workflow_versions(id, version)
);

CREATE INDEX workflow_schedules_owner
ON workflow_schedules (household_id, actor_id);

CREATE INDEX workflow_schedules_due
ON workflow_schedules (next_run_at)
WHERE next_run_at IS NOT NULL;
