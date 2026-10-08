-- Planning is durable before dispatch. Evidence revisions never overwrite originals.
CREATE TABLE native_intakes (
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    PRIMARY KEY (workspace_id, project_id),
    FOREIGN KEY (workspace_id, project_id) REFERENCES native_projects(workspace_id, id),
    CHECK (snapshot @> jsonb_build_object(
        'workspace_id', workspace_id, 'project_id', project_id, 'version', version))
);

CREATE TABLE native_intake_sources (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    source_key text NOT NULL CHECK (length(btrim(source_key)) BETWEEN 1 AND 240),
    revision integer NOT NULL CHECK (revision > 0),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes integer NOT NULL CHECK (size_bytes BETWEEN 0 AND 5242880),
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL,
    revoked_at timestamptz,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    UNIQUE (workspace_id, project_id, id),
    UNIQUE (workspace_id, project_id, source_key, revision),
    FOREIGN KEY (workspace_id, project_id) REFERENCES native_projects(workspace_id, id),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at),
    CHECK (snapshot @> jsonb_build_object(
        'id', id, 'workspace_id', workspace_id, 'project_id', project_id,
        'source_key', source_key, 'revision', revision, 'sha256', sha256,
        'size_bytes', size_bytes, 'created_by', created_by)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at),
    CHECK ((snapshot->>'revoked_at')::timestamptz IS NOT DISTINCT FROM revoked_at)
);
CREATE INDEX native_intake_sources_project_order_idx
    ON native_intake_sources (workspace_id, project_id, created_at, id);
CREATE INDEX native_intake_sources_latest_idx
    ON native_intake_sources (workspace_id, project_id, source_key, revision DESC);

CREATE TABLE native_intake_runs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    requested_by uuid NOT NULL REFERENCES users(id),
    idempotency_key text NOT NULL CHECK (length(btrim(idempotency_key)) BETWEEN 8 AND 200),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    intake_version integer NOT NULL CHECK (intake_version > 0),
    version integer NOT NULL CHECK (version > 0),
    status text NOT NULL CHECK (status IN (
        'running', 'ready', 'questions', 'needs_revision', 'applied', 'stale',
        'failed', 'unknown', 'cancelled'
    )),
    reserved_microusd bigint NOT NULL CHECK (reserved_microusd >= 0),
    charged_microusd bigint NOT NULL CHECK (charged_microusd >= 0),
    started_at timestamptz NOT NULL,
    deadline_at timestamptz NOT NULL CHECK (deadline_at > started_at),
    finished_at timestamptz CHECK (finished_at IS NULL OR finished_at >= started_at),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    UNIQUE (workspace_id, project_id, id),
    UNIQUE (workspace_id, project_id, idempotency_key),
    FOREIGN KEY (workspace_id, project_id) REFERENCES native_intakes(workspace_id, project_id),
    CHECK (snapshot @> jsonb_build_object(
        'id', id, 'workspace_id', workspace_id, 'project_id', project_id,
        'requested_by', requested_by, 'idempotency_key', idempotency_key,
        'request_digest', request_digest, 'intake_version', intake_version, 'version', version,
        'status', status, 'reserved_microusd', reserved_microusd, 'charged_microusd', charged_microusd)),
    CHECK ((snapshot->>'started_at')::timestamptz IS NOT DISTINCT FROM started_at),
    CHECK ((snapshot->>'deadline_at')::timestamptz IS NOT DISTINCT FROM deadline_at),
    CHECK ((snapshot->>'finished_at')::timestamptz IS NOT DISTINCT FROM finished_at)
);
CREATE INDEX native_intake_runs_project_order_idx
    ON native_intake_runs (workspace_id, project_id, started_at DESC, id DESC);
CREATE INDEX native_intake_runs_unsettled_idx
    ON native_intake_runs (workspace_id, project_id, deadline_at)
    WHERE status = 'running' OR reserved_microusd > 0;
