-- Native bounded execution authority, append-only checkpoints and durable wakeups.

ALTER TABLE native_tasks ADD CONSTRAINT native_tasks_execution_scope UNIQUE (workspace_id,project_id,id);

CREATE TABLE native_execution_policies (
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL PRIMARY KEY,
    version integer NOT NULL CHECK (version>0),
    issued_by uuid NOT NULL REFERENCES users(id),
    updated_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id),
    FOREIGN KEY (workspace_id,project_id) REFERENCES native_projects(workspace_id,id),
    CHECK (snapshot @> jsonb_build_object('workspace_id',workspace_id,'project_id',project_id,'version',version,'issued_by',issued_by)),
    CHECK ((snapshot->>'updated_at')::timestamptz IS NOT DISTINCT FROM updated_at)
);

CREATE INDEX native_execution_policies_scope_order ON native_execution_policies(workspace_id,project_id,updated_at,project_id);

CREATE TABLE native_task_workflows (
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    task_id uuid NOT NULL PRIMARY KEY,
    version integer NOT NULL CHECK (version>0),
    updated_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,task_id),
    FOREIGN KEY (workspace_id,project_id) REFERENCES native_projects(workspace_id,id),
    FOREIGN KEY (workspace_id,project_id,task_id) REFERENCES native_tasks(workspace_id,project_id,id),
    CHECK (snapshot @> jsonb_build_object('workspace_id',workspace_id,'project_id',project_id,'task_id',task_id,'version',version)),
    CHECK ((snapshot->>'updated_at')::timestamptz IS NOT DISTINCT FROM updated_at)
);

CREATE INDEX native_task_workflows_scope_order ON native_task_workflows(workspace_id,project_id,updated_at,task_id);

CREATE TABLE native_execution_runners (
    id uuid NOT NULL PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    issued_by uuid NOT NULL REFERENCES users(id),
    version integer NOT NULL CHECK (version>0),
    token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('active','revoked')),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id) REFERENCES native_projects(workspace_id,id),
    CHECK (expires_at>created_at),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'issued_by',issued_by,'version',version,'token_hash',token_hash,'status',status)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at),
    CHECK ((snapshot->>'expires_at')::timestamptz IS NOT DISTINCT FROM expires_at)
);

CREATE INDEX native_execution_runners_scope_order ON native_execution_runners(workspace_id,project_id,created_at,id);

CREATE TABLE native_execution_schedules (
    id uuid NOT NULL PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    task_id uuid NOT NULL,
    issued_by uuid NOT NULL REFERENCES users(id),
    version integer NOT NULL CHECK (version>0),
    enabled boolean NOT NULL,
    next_run_at timestamptz,
    last_run_id uuid,
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id) REFERENCES native_projects(workspace_id,id),
    FOREIGN KEY (workspace_id,project_id,task_id) REFERENCES native_tasks(workspace_id,project_id,id),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'task_id',task_id,'issued_by',issued_by,'version',version,'enabled',enabled,'last_run_id',last_run_id)),
    CHECK ((snapshot->>'next_run_at')::timestamptz IS NOT DISTINCT FROM next_run_at),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);

CREATE INDEX native_execution_schedules_scope_order ON native_execution_schedules(workspace_id,project_id,created_at,id);

CREATE TABLE native_execution_runs (
    id uuid NOT NULL PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    task_id uuid NOT NULL,
    agent_id uuid NOT NULL,
    model_id uuid NOT NULL,
    issued_by uuid NOT NULL REFERENCES users(id),
    root_run_id uuid NOT NULL,
    parent_run_id uuid,
    schedule_id uuid,
    runner_id uuid,
    version integer NOT NULL CHECK (version>0),
    status text NOT NULL CHECK (status IN ('queued','running','waiting','completed','failed','unknown','cancelled','stale')),
    created_at timestamptz NOT NULL,
    deadline_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id) REFERENCES native_projects(workspace_id,id),
    FOREIGN KEY (workspace_id,project_id,task_id) REFERENCES native_tasks(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,agent_id) REFERENCES native_agents(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,model_id) REFERENCES project_models(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,root_run_id) REFERENCES native_execution_runs(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,parent_run_id) REFERENCES native_execution_runs(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,runner_id) REFERENCES native_execution_runners(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,schedule_id) REFERENCES native_execution_schedules(workspace_id,project_id,id),
    CHECK (deadline_at>created_at),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'task_id',task_id,'agent_id',agent_id,'model_id',model_id,'issued_by',issued_by,'root_run_id',root_run_id,'parent_run_id',parent_run_id,'schedule_id',schedule_id,'runner_id',runner_id,'version',version,'status',status)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at),
    CHECK ((snapshot->>'deadline_at')::timestamptz IS NOT DISTINCT FROM deadline_at)
);

CREATE INDEX native_execution_runs_scope_order ON native_execution_runs(workspace_id,project_id,created_at,id);

CREATE TABLE native_execution_steps (
    id uuid NOT NULL PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    run_id uuid NOT NULL,
    root_run_id uuid NOT NULL,
    sequence integer NOT NULL CHECK (sequence>0),
    operation_id uuid NOT NULL,
    usage_id uuid,
    version integer NOT NULL CHECK (version>0),
    status text NOT NULL CHECK (status IN ('prepared','dispatched','completed','failed','unknown')),
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id) REFERENCES native_projects(workspace_id,id),
    FOREIGN KEY (workspace_id,project_id,run_id) REFERENCES native_execution_runs(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,root_run_id) REFERENCES native_execution_runs(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,usage_id) REFERENCES model_usage(workspace_id,project_id,id),
    UNIQUE (workspace_id,project_id,run_id,sequence),
    UNIQUE (workspace_id,project_id,operation_id),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'run_id',run_id,'root_run_id',root_run_id,'sequence',sequence,'operation_id',operation_id,'usage_id',usage_id,'version',version,'status',status)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);

CREATE INDEX native_execution_steps_scope_order ON native_execution_steps(workspace_id,project_id,sequence,id);

CREATE TABLE native_execution_events (
    id uuid NOT NULL PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    run_id uuid NOT NULL,
    sequence integer NOT NULL CHECK (sequence>0),
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id) REFERENCES native_projects(workspace_id,id),
    FOREIGN KEY (workspace_id,project_id,run_id) REFERENCES native_execution_runs(workspace_id,project_id,id),
    UNIQUE (workspace_id,project_id,run_id,sequence),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'run_id',run_id,'sequence',sequence)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);

CREATE INDEX native_execution_events_scope_order ON native_execution_events(workspace_id,project_id,sequence,id);

CREATE TABLE native_execution_waits (
    id uuid NOT NULL PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    run_id uuid NOT NULL,
    correlation_id uuid NOT NULL,
    version integer NOT NULL CHECK (version>0),
    status text NOT NULL CHECK (status IN ('pending','resolved','timed_out','cancelled')),
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id) REFERENCES native_projects(workspace_id,id),
    FOREIGN KEY (workspace_id,project_id,run_id) REFERENCES native_execution_runs(workspace_id,project_id,id),
    UNIQUE (workspace_id,project_id,correlation_id),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'run_id',run_id,'correlation_id',correlation_id,'version',version,'status',status)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);

CREATE INDEX native_execution_waits_scope_order ON native_execution_waits(workspace_id,project_id,created_at,id);

CREATE TABLE native_execution_signals (
    id uuid NOT NULL PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    run_id uuid NOT NULL,
    correlation_id uuid NOT NULL,
    received_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id) REFERENCES native_projects(workspace_id,id),
    FOREIGN KEY (workspace_id,project_id,run_id) REFERENCES native_execution_runs(workspace_id,project_id,id),
    UNIQUE (workspace_id,project_id,run_id,correlation_id),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'run_id',run_id,'correlation_id',correlation_id,'received_by',received_by)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);

CREATE INDEX native_execution_signals_scope_order ON native_execution_signals(workspace_id,project_id,created_at,id);

ALTER TABLE native_execution_schedules ADD CONSTRAINT native_schedule_last_run_scope
    FOREIGN KEY (workspace_id,project_id,last_run_id) REFERENCES native_execution_runs(workspace_id,project_id,id);
CREATE TABLE native_task_dependencies (
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    task_id uuid NOT NULL,
    dependency_id uuid NOT NULL,
    PRIMARY KEY (workspace_id,project_id,task_id,dependency_id),
    FOREIGN KEY (workspace_id,project_id,task_id) REFERENCES native_task_workflows(workspace_id,project_id,task_id),
    FOREIGN KEY (workspace_id,project_id,dependency_id) REFERENCES native_tasks(workspace_id,project_id,id),
    CHECK (task_id<>dependency_id)
);
CREATE UNIQUE INDEX native_execution_one_live_task ON native_execution_runs(workspace_id,project_id,task_id)
    WHERE status IN ('queued','running','waiting','unknown');
CREATE INDEX native_execution_active_runs ON native_execution_runs(workspace_id,project_id,created_at,id)
    WHERE status IN ('queued','running','waiting','unknown');
CREATE INDEX native_execution_task_runs ON native_execution_runs(workspace_id,project_id,task_id,created_at DESC,id DESC);
CREATE INDEX native_execution_root_runs ON native_execution_runs(workspace_id,project_id,root_run_id,created_at,id);
CREATE INDEX native_execution_root_steps ON native_execution_steps(workspace_id,project_id,root_run_id,sequence,id);
CREATE INDEX native_execution_run_steps ON native_execution_steps(workspace_id,project_id,run_id,sequence,id);
CREATE INDEX native_execution_run_waits ON native_execution_waits(workspace_id,project_id,run_id,created_at,id);
CREATE UNIQUE INDEX native_execution_one_pending_wait ON native_execution_waits(workspace_id,project_id,run_id) WHERE status='pending';
CREATE UNIQUE INDEX native_execution_one_enabled_schedule ON native_execution_schedules(workspace_id,project_id,task_id)
    WHERE enabled AND next_run_at IS NOT NULL;
