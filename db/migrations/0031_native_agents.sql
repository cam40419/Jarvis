-- Project agents are independent principals, never synthetic human memberships.
CREATE TABLE native_agents (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
    role_key text NOT NULL CHECK (role_key ~ '^[a-z][a-z0-9-]{0,63}$'),
    instructions text NOT NULL CHECK (length(btrim(instructions)) BETWEEN 1 AND 8000),
    success_criteria text NOT NULL CHECK (length(btrim(success_criteria)) BETWEEN 1 AND 4000),
    rationale text NOT NULL CHECK (length(btrim(rationale)) BETWEEN 1 AND 2000),
    status text NOT NULL CHECK (status IN ('active', 'paused', 'retired')),
    can_manage_team boolean NOT NULL,
    created_by uuid REFERENCES users(id),
    created_by_agent_id uuid,
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (workspace_id, project_id, id),
    UNIQUE (workspace_id, project_id, role_key),
    FOREIGN KEY (workspace_id, project_id) REFERENCES native_projects(workspace_id, id),
    FOREIGN KEY (workspace_id, project_id, created_by_agent_id)
        REFERENCES native_agents(workspace_id, project_id, id),
    CHECK (num_nonnulls(created_by, created_by_agent_id) = 1)
);
CREATE INDEX native_agents_project_order_idx
    ON native_agents (workspace_id, project_id, created_at, id);
CREATE INDEX native_agents_active_idx
    ON native_agents (workspace_id, project_id) WHERE status = 'active';

CREATE TABLE native_team_policies (
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    max_active_agents integer NOT NULL CHECK (max_active_agents BETWEEN 1 AND 100),
    agents_can_manage_team boolean NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, project_id),
    FOREIGN KEY (workspace_id, project_id) REFERENCES native_projects(workspace_id, id)
);

CREATE TABLE native_agent_credentials (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    agent_id uuid NOT NULL,
    agent_version integer NOT NULL CHECK (agent_version > 0),
    token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    scopes text[] NOT NULL CHECK (scopes <@ ARRAY['board:read','board:write','team:manage']::text[]),
    issued_by uuid NOT NULL REFERENCES users(id),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL,
    FOREIGN KEY (workspace_id, project_id, agent_id)
        REFERENCES native_agents(workspace_id, project_id, id),
    CHECK (expires_at > created_at),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);
CREATE INDEX native_agent_credentials_agent_idx
    ON native_agent_credentials (workspace_id, project_id, agent_id, created_at, id);

-- Replace only the original assignment checks, regardless of generated constraint names.
DO $$
DECLARE
    constraint_row record;
BEGIN
    FOR constraint_row IN
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
        WHERE c.conrelid = 'native_tasks'::regclass
          AND c.contype = 'c'
          AND a.attname = 'assignment_kind'
    LOOP
        EXECUTE format('ALTER TABLE native_tasks DROP CONSTRAINT %I', constraint_row.conname);
    END LOOP;
END $$;

ALTER TABLE native_tasks ADD COLUMN assignee_agent_id uuid;
ALTER TABLE native_tasks ALTER COLUMN created_by DROP NOT NULL;
ALTER TABLE native_tasks ADD COLUMN created_by_agent_id uuid;
ALTER TABLE native_tasks ADD CONSTRAINT native_tasks_agent_assignee_fk
    FOREIGN KEY (workspace_id, project_id, assignee_agent_id)
    REFERENCES native_agents(workspace_id, project_id, id);
ALTER TABLE native_tasks ADD CONSTRAINT native_tasks_agent_creator_fk
    FOREIGN KEY (workspace_id, project_id, created_by_agent_id)
    REFERENCES native_agents(workspace_id, project_id, id);
ALTER TABLE native_tasks ADD CONSTRAINT native_tasks_assignment_target_check CHECK (
    (assignment_kind = 'pool' AND assignee_actor_id IS NULL AND assignee_agent_id IS NULL)
    OR (assignment_kind = 'human' AND assignee_actor_id IS NOT NULL AND assignee_agent_id IS NULL)
    OR (assignment_kind = 'agent' AND assignee_actor_id IS NULL AND assignee_agent_id IS NOT NULL)
);
ALTER TABLE native_tasks ADD CONSTRAINT native_tasks_creator_check
    CHECK (num_nonnulls(created_by, created_by_agent_id) = 1);
CREATE INDEX native_tasks_agent_assignee_idx
    ON native_tasks (workspace_id, project_id, assignee_agent_id)
    WHERE assignee_agent_id IS NOT NULL;

-- Retire the old project-file authority. Historical migrations remain immutable.
DROP TABLE project_artifacts;
DROP TABLE project_file_operations;
DROP TABLE project_drive;
