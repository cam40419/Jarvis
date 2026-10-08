-- Project credentials and one shared accounting authority for every native model call.
CREATE TABLE project_models (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    credential_revision integer NOT NULL CHECK (credential_revision >= 0),
    qualification_usage_id uuid,
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    UNIQUE (workspace_id, project_id, id),
    FOREIGN KEY (workspace_id, project_id) REFERENCES native_projects(workspace_id, id),
    CHECK (snapshot @> jsonb_build_object('id', id, 'workspace_id', workspace_id,
        'project_id', project_id, 'version', version, 'credential_revision', credential_revision,
        'created_by', created_by, 'qualification_usage_id', qualification_usage_id)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);
CREATE INDEX project_models_scope_idx ON project_models(workspace_id, project_id, created_at, id);

CREATE TABLE project_model_credentials (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    model_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    created_by uuid NOT NULL REFERENCES users(id),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    UNIQUE (workspace_id, project_id, model_id, revision),
    FOREIGN KEY (workspace_id, project_id, model_id)
        REFERENCES project_models(workspace_id, project_id, id),
    CHECK (snapshot @> jsonb_build_object('id', id, 'workspace_id', workspace_id,
        'project_id', project_id, 'model_id', model_id, 'revision', revision,
        'created_by', created_by))
);

CREATE TABLE workspace_model_policies (
    workspace_id uuid PRIMARY KEY REFERENCES workspaces(id),
    version integer NOT NULL CHECK (version > 0),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    CHECK (snapshot @> jsonb_build_object('workspace_id', workspace_id,
        'project_id', NULL, 'version', version, 'allow_paid', false, 'allow_cloud', false,
        'planning_model_id', NULL, 'review_model_id', NULL))
);
CREATE TABLE project_model_policies (
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    planning_model_id uuid,
    review_model_id uuid,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    PRIMARY KEY (workspace_id, project_id),
    FOREIGN KEY (workspace_id, project_id) REFERENCES native_projects(workspace_id, id),
    FOREIGN KEY (workspace_id, project_id, planning_model_id)
        REFERENCES project_models(workspace_id, project_id, id),
    FOREIGN KEY (workspace_id, project_id, review_model_id)
        REFERENCES project_models(workspace_id, project_id, id),
    CHECK (snapshot @> jsonb_build_object('workspace_id', workspace_id, 'project_id', project_id,
        'version', version, 'planning_model_id', planning_model_id, 'review_model_id', review_model_id))
);

CREATE TABLE model_usage (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    phase text NOT NULL CHECK (phase ~ '^[a-z][a-z0-9_]{0,63}$'),
    requested_by uuid NOT NULL REFERENCES users(id),
    model_id uuid,
    model_version integer NOT NULL CHECK (model_version >= 0),
    credential_revision integer NOT NULL CHECK (credential_revision >= 0),
    credential_reference_revision integer GENERATED ALWAYS AS (NULLIF(credential_revision, 0)) STORED,
    version integer NOT NULL CHECK (version > 0),
    status text NOT NULL CHECK (status IN ('reserved','dispatched','settled','unknown','reconciled','released')),
    reserved_microusd bigint NOT NULL CHECK (reserved_microusd >= 0),
    held_microusd bigint NOT NULL CHECK (held_microusd BETWEEN 0 AND reserved_microusd),
    charged_microusd bigint NOT NULL CHECK (charged_microusd >= 0),
    started_at timestamptz NOT NULL,
    deadline_at timestamptz NOT NULL CHECK (deadline_at > started_at),
    finished_at timestamptz CHECK (finished_at IS NULL OR finished_at >= started_at),
    reconciliation_by uuid REFERENCES users(id),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    UNIQUE (workspace_id, project_id, id),
    UNIQUE (workspace_id, project_id, operation_id, phase),
    FOREIGN KEY (workspace_id, project_id) REFERENCES native_projects(workspace_id, id),
    FOREIGN KEY (workspace_id, project_id, model_id)
        REFERENCES project_models(workspace_id, project_id, id),
    FOREIGN KEY (workspace_id, project_id, model_id, credential_reference_revision)
        REFERENCES project_model_credentials(workspace_id, project_id, model_id, revision),
    CHECK ((model_id IS NULL AND model_version=0 AND credential_revision=0)
        OR (model_id IS NOT NULL AND model_version>0)),
    CHECK (status NOT IN ('settled','reconciled','released') OR held_microusd=0),
    CHECK (status<>'released' OR charged_microusd=0),
    CHECK ((status='reconciled') = (reconciliation_by IS NOT NULL)),
    CHECK (snapshot @> jsonb_build_object('id', id, 'workspace_id', workspace_id,
        'project_id', project_id, 'operation_id', operation_id, 'phase', phase,
        'requested_by', requested_by, 'model_id', model_id, 'model_version', model_version,
        'credential_revision', credential_revision, 'version', version, 'status', status,
        'reserved_microusd', reserved_microusd, 'held_microusd', held_microusd,
        'charged_microusd', charged_microusd, 'reconciliation_by', reconciliation_by)),
    CHECK ((snapshot->>'started_at')::timestamptz IS NOT DISTINCT FROM started_at),
    CHECK ((snapshot->>'deadline_at')::timestamptz IS NOT DISTINCT FROM deadline_at),
    CHECK ((snapshot->>'finished_at')::timestamptz IS NOT DISTINCT FROM finished_at)
);
CREATE INDEX model_usage_scope_order_idx ON model_usage(workspace_id, project_id, started_at DESC, id DESC);
CREATE INDEX model_usage_workspace_order_idx ON model_usage(workspace_id, started_at DESC, id DESC);
CREATE INDEX model_usage_expired_idx ON model_usage(workspace_id, deadline_at, id)
    WHERE status IN ('reserved','dispatched');
ALTER TABLE project_models ADD CONSTRAINT project_models_qualification_usage_fk
    FOREIGN KEY (workspace_id, project_id, qualification_usage_id)
    REFERENCES model_usage(workspace_id, project_id, id);

-- Existing planning liabilities become immutable historical usage once, without retaining
-- a second runtime budget/catalog authority. No provider secret or URL is inferred.
INSERT INTO model_usage (
    id,workspace_id,project_id,operation_id,phase,requested_by,model_id,model_version,
    credential_revision,version,status,reserved_microusd,held_microusd,charged_microusd,
    started_at,deadline_at,finished_at,reconciliation_by,snapshot
)
SELECT id,workspace_id,project_id,id,'intake_import',requested_by,NULL,0,0,1,
    CASE WHEN reserved_microusd>0 THEN 'unknown' ELSE 'settled' END,
    reserved_microusd+charged_microusd,reserved_microusd,charged_microusd,
    started_at,deadline_at,finished_at,NULL,
    jsonb_build_object(
        'id',id,'workspace_id',workspace_id,'project_id',project_id,'operation_id',id,
        'phase','intake_import','requested_by',requested_by,'model_id',NULL,'model_version',0,
        'credential_revision',0,'template_id',snapshot->>'endpoint_id','model',snapshot->>'model',
        'reported_model',NULL,
        'endpoint_fingerprint',repeat('0',64),'endpoint_snapshot','{}'::jsonb,
        'input_rate','0','output_rate','0','reserved_microusd',reserved_microusd+charged_microusd,
        'held_microusd',reserved_microusd,'charged_microusd',charged_microusd,
        'status',CASE WHEN reserved_microusd>0 THEN 'unknown' ELSE 'settled' END,
        'input_tokens',snapshot->'input_tokens','output_tokens',snapshot->'output_tokens',
        'started_at',snapshot->'started_at','deadline_at',snapshot->'deadline_at',
        'finished_at',snapshot->'finished_at','version',1,'error_code',NULL,
        'reconciliation_by',NULL,'reconciliation_at',NULL,'reconciliation_reason',NULL,
        'reconciliation_evidence',NULL
    )
FROM native_intake_runs;
UPDATE native_intakes SET snapshot=snapshot-'endpoint_id'-'allow_cloud'-'budget_microusd';
UPDATE native_intake_runs SET snapshot=snapshot || jsonb_build_object(
    'usage_ids',jsonb_build_array(id),'review_endpoint_id',NULL,'review_model',NULL);
