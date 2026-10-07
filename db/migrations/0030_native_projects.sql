-- Native projects own shared work independently of personal memory and job snapshots.
CREATE TABLE native_projects (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
    objective text NOT NULL CHECK (length(btrim(objective)) BETWEEN 1 AND 8000),
    status text NOT NULL CHECK (status IN ('active', 'archived')),
    board_authority text NOT NULL CHECK (board_authority = 'native'),
    created_by uuid NOT NULL REFERENCES users(id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (workspace_id, id)
);
CREATE INDEX native_projects_workspace_order_idx
    ON native_projects (workspace_id, created_at DESC, id DESC);

CREATE TABLE native_project_members (
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('owner', 'member')),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, project_id, actor_id),
    FOREIGN KEY (workspace_id, project_id)
        REFERENCES native_projects(workspace_id, id),
    FOREIGN KEY (workspace_id, actor_id)
        REFERENCES memberships(workspace_id, user_id) ON DELETE CASCADE
);
CREATE INDEX native_project_members_actor_idx
    ON native_project_members (workspace_id, actor_id, project_id);

CREATE TABLE native_tasks (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200),
    description text NOT NULL CHECK (length(description) <= 8000),
    status text NOT NULL CHECK (
        status IN ('todo', 'in_progress', 'in_review', 'blocked', 'done', 'cancelled')
    ),
    assignment_kind text NOT NULL CHECK (assignment_kind IN ('human', 'pool')),
    assignee_actor_id uuid,
    created_by uuid NOT NULL REFERENCES users(id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    FOREIGN KEY (workspace_id, project_id)
        REFERENCES native_projects(workspace_id, id),
    FOREIGN KEY (workspace_id, project_id, assignee_actor_id)
        REFERENCES native_project_members(workspace_id, project_id, actor_id),
    CHECK ((assignment_kind = 'human') = (assignee_actor_id IS NOT NULL))
);
CREATE INDEX native_tasks_project_order_idx
    ON native_tasks (workspace_id, project_id, created_at, id);
CREATE INDEX native_tasks_assignee_idx
    ON native_tasks (workspace_id, project_id, assignee_actor_id)
    WHERE assignee_actor_id IS NOT NULL;
