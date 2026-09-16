CREATE TABLE google_connections (
    household_id uuid NOT NULL REFERENCES households(id),
    actor_id uuid NOT NULL REFERENCES users(id),
    snapshot jsonb NOT NULL,
    PRIMARY KEY (household_id, actor_id)
);

CREATE TABLE google_oauth_states (
    state_hash text PRIMARY KEY,
    binding_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL
);

CREATE TABLE action_proposals (
    id uuid PRIMARY KEY,
    household_id uuid NOT NULL REFERENCES households(id),
    actor_id uuid NOT NULL REFERENCES users(id),
    run_id uuid NOT NULL REFERENCES runs(id),
    snapshot jsonb NOT NULL
);
CREATE INDEX action_proposals_run ON action_proposals(run_id);
