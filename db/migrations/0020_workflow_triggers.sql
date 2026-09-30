CREATE TABLE workflow_triggers (
    id uuid PRIMARY KEY,
    household_id uuid NOT NULL REFERENCES households(id),
    actor_id uuid NOT NULL REFERENCES users(id),
    definition_id uuid NOT NULL UNIQUE,
    definition_version integer NOT NULL,
    version integer NOT NULL,
    next_check_at timestamptz,
    snapshot jsonb NOT NULL,
    FOREIGN KEY (definition_id, definition_version)
        REFERENCES workflow_versions(id, version)
);

CREATE INDEX workflow_triggers_owner
ON workflow_triggers (household_id, actor_id);

CREATE INDEX workflow_triggers_due
ON workflow_triggers (next_check_at)
WHERE next_check_at IS NOT NULL;
