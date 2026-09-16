CREATE TABLE voice_sessions (
    id uuid PRIMARY KEY,
    household_id uuid NOT NULL REFERENCES households(id),
    actor_id uuid NOT NULL REFERENCES users(id),
    thread_id uuid NOT NULL REFERENCES threads(id),
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL
);
CREATE INDEX voice_sessions_owner ON voice_sessions (household_id, actor_id, created_at DESC);
