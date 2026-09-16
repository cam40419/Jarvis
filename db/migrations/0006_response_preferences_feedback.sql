CREATE TABLE response_preferences (
    household_id uuid NOT NULL REFERENCES households(id),
    actor_id uuid NOT NULL REFERENCES users(id),
    snapshot jsonb NOT NULL,
    PRIMARY KEY (household_id, actor_id)
);

CREATE TABLE run_feedback (
    household_id uuid NOT NULL REFERENCES households(id),
    actor_id uuid NOT NULL REFERENCES users(id),
    run_id uuid NOT NULL REFERENCES runs(id),
    snapshot jsonb NOT NULL,
    PRIMARY KEY (household_id, actor_id, run_id)
);
