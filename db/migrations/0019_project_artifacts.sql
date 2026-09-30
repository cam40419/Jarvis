CREATE TABLE project_artifacts (
    id uuid PRIMARY KEY,
    household_id uuid NOT NULL REFERENCES households(id),
    actor_id uuid NOT NULL REFERENCES users(id),
    project_id uuid NOT NULL REFERENCES memories(id),
    task_id uuid NOT NULL REFERENCES jobs(id),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
    media_type text NOT NULL CHECK (char_length(media_type) BETWEEN 1 AND 100),
    byte_count integer NOT NULL CHECK (byte_count BETWEEN 0 AND 524288),
    sha256 char(64) NOT NULL,
    content bytea NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (task_id, name)
);

CREATE INDEX project_artifacts_owner_project
ON project_artifacts (household_id, actor_id, project_id, created_at DESC);
