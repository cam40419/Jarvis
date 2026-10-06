CREATE TABLE integration_connections (
    household_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    id text NOT NULL,
    snapshot jsonb NOT NULL,
    PRIMARY KEY (household_id, actor_id, id)
);
