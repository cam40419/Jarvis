CREATE TABLE project_drive (
    household_id uuid NOT NULL REFERENCES households(id),
    actor_id uuid NOT NULL REFERENCES users(id),
    project_id uuid NOT NULL,
    snapshot jsonb NOT NULL,
    PRIMARY KEY (household_id, actor_id, project_id)
);
CREATE TABLE project_file_operations (
    id uuid PRIMARY KEY,
    household_id uuid NOT NULL REFERENCES households(id),
    actor_id uuid NOT NULL REFERENCES users(id),
    project_id uuid NOT NULL,
    snapshot jsonb NOT NULL
);
CREATE INDEX project_file_operations_project
    ON project_file_operations(household_id, actor_id, project_id);
