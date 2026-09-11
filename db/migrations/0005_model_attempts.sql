CREATE TABLE model_attempts (
    id uuid PRIMARY KEY,
    household_id uuid NOT NULL REFERENCES households(id),
    thread_id uuid NOT NULL REFERENCES threads(id),
    status text NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed')),
    snapshot jsonb NOT NULL
);
CREATE UNIQUE INDEX model_attempts_active_thread_idx ON model_attempts(thread_id)
WHERE status = 'pending';
