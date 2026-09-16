-- Commands must survive a failed/cancelled model answer. Runs are saved only on success.
CREATE TABLE home_commands (
    id uuid PRIMARY KEY,
    household_id uuid NOT NULL REFERENCES households(id),
    actor_id uuid NOT NULL REFERENCES users(id),
    thread_id uuid NOT NULL REFERENCES threads(id),
    run_id uuid NOT NULL,
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL
);
CREATE INDEX home_commands_recent ON home_commands(household_id, actor_id, created_at DESC);
CREATE INDEX home_commands_run ON home_commands(run_id);
