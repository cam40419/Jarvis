CREATE TABLE workflow_versions (
    id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    household_id uuid NOT NULL REFERENCES households(id),
    actor_id uuid NOT NULL REFERENCES users(id),
    snapshot jsonb NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE INDEX workflow_versions_owner ON workflow_versions (household_id, actor_id);
CREATE TABLE workflow_runs (
    id uuid PRIMARY KEY,
    household_id uuid NOT NULL REFERENCES households(id),
    actor_id uuid NOT NULL REFERENCES users(id),
    definition_id uuid NOT NULL,
    definition_version integer NOT NULL,
    version integer NOT NULL,
    next_wake_at timestamptz,
    snapshot jsonb NOT NULL,
    FOREIGN KEY (definition_id, definition_version) REFERENCES workflow_versions(id, version)
);
CREATE INDEX workflow_runs_due ON workflow_runs (next_wake_at) WHERE next_wake_at IS NOT NULL;
CREATE INDEX workflow_runs_owner ON workflow_runs (household_id, actor_id);
CREATE TABLE workflow_events (
    run_id uuid NOT NULL REFERENCES workflow_runs(id),
    sequence integer NOT NULL,
    snapshot jsonb NOT NULL,
    PRIMARY KEY (run_id, sequence)
);
CREATE TABLE workflow_workers (id text PRIMARY KEY, seen_at timestamptz NOT NULL);
