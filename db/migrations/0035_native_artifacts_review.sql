-- Atomic artifact bytes, immutable revision history and exact-content review authority.
CREATE TABLE native_artifacts (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    task_id uuid NOT NULL,
    current_revision_id uuid,
    pending_repair_task_id uuid,
    version integer NOT NULL CHECK (version>0),
    created_by uuid REFERENCES users(id),
    created_by_agent_id uuid,
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    UNIQUE (workspace_id,project_id,task_id),
    FOREIGN KEY (workspace_id,project_id,task_id) REFERENCES native_tasks(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,pending_repair_task_id) REFERENCES native_tasks(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,created_by_agent_id) REFERENCES native_agents(workspace_id,project_id,id),
    CHECK ((created_by IS NULL)<>(created_by_agent_id IS NULL)),
    CHECK (pending_repair_task_id IS DISTINCT FROM task_id),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'task_id',task_id,'current_revision_id',current_revision_id,'pending_repair_task_id',pending_repair_task_id,'version',version,'created_by',created_by,'created_by_agent_id',created_by_agent_id)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);

CREATE TABLE native_artifact_revisions (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    artifact_id uuid NOT NULL,
    task_id uuid NOT NULL,
    run_id uuid,
    sequence integer NOT NULL CHECK (sequence>0),
    parent_revision_id uuid,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes integer NOT NULL CHECK (size_bytes BETWEEN 0 AND 1048576),
    created_by uuid REFERENCES users(id),
    created_by_agent_id uuid,
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    UNIQUE (workspace_id,project_id,artifact_id,id),
    UNIQUE (workspace_id,project_id,id,sha256),
    UNIQUE (workspace_id,project_id,artifact_id,id,sha256),
    UNIQUE (workspace_id,project_id,artifact_id,sequence),
    FOREIGN KEY (workspace_id,project_id,artifact_id) REFERENCES native_artifacts(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,task_id) REFERENCES native_tasks(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,run_id) REFERENCES native_execution_runs(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,created_by_agent_id) REFERENCES native_agents(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,artifact_id,parent_revision_id) REFERENCES native_artifact_revisions(workspace_id,project_id,artifact_id,id),
    CHECK ((created_by IS NULL)<>(created_by_agent_id IS NULL)),
    CHECK (run_id IS NULL OR created_by_agent_id IS NOT NULL),
    CHECK ((sequence=1)=(parent_revision_id IS NULL)),
    CHECK (parent_revision_id IS DISTINCT FROM id),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'artifact_id',artifact_id,'task_id',task_id,'run_id',run_id,'sequence',sequence,'parent_revision_id',parent_revision_id,'sha256',sha256,'size_bytes',size_bytes,'created_by',created_by,'created_by_agent_id',created_by_agent_id)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at),
    CHECK ((jsonb_typeof(snapshot->'content_base64')='string') IS TRUE),
    CHECK ((octet_length(decode(snapshot->>'content_base64','base64'))=size_bytes) IS TRUE),
    CHECK ((replace(encode(decode(snapshot->>'content_base64','base64'),'base64'),E'\n','')=snapshot->>'content_base64') IS TRUE),
    CHECK ((encode(digest(decode(snapshot->>'content_base64','base64'),'sha256'),'hex')=sha256) IS TRUE)
);
ALTER TABLE native_artifacts ADD CONSTRAINT native_artifact_head_scope
    FOREIGN KEY (workspace_id,project_id,id,current_revision_id)
    REFERENCES native_artifact_revisions(workspace_id,project_id,artifact_id,id);

CREATE TABLE native_review_policies (
    workspace_id uuid NOT NULL,
    project_id uuid PRIMARY KEY,
    reviewer_agent_id uuid,
    version integer NOT NULL CHECK (version>0),
    updated_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    FOREIGN KEY (workspace_id,project_id) REFERENCES native_projects(workspace_id,id),
    FOREIGN KEY (workspace_id,project_id,reviewer_agent_id) REFERENCES native_agents(workspace_id,project_id,id),
    CHECK (snapshot @> jsonb_build_object('workspace_id',workspace_id,'project_id',project_id,'reviewer_agent_id',reviewer_agent_id,'version',version)),
    CHECK ((snapshot->>'updated_at')::timestamptz IS NOT DISTINCT FROM updated_at),
    CHECK (((snapshot->>'require_human_approval')::boolean OR (snapshot->>'require_agent_review')::boolean) IS TRUE),
    CHECK ((snapshot->>'max_repair_cycles')::integer BETWEEN 0 AND 8)
);

CREATE TABLE native_artifact_checks (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    sha256 text NOT NULL,
    check_name text NOT NULL CHECK (check_name ~ '^[a-z][a-z0-9_.-]{0,95}$'),
    status text NOT NULL CHECK (status IN ('passed','failed')),
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,revision_id,check_name),
    FOREIGN KEY (workspace_id,project_id,revision_id,sha256) REFERENCES native_artifact_revisions(workspace_id,project_id,id,sha256),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'revision_id',revision_id,'sha256',sha256,'check_name',check_name,'status',status)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);

CREATE TABLE native_artifact_reviews (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    artifact_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    sha256 text NOT NULL,
    reviewer_agent_id uuid NOT NULL,
    review_task_id uuid,
    model_id uuid NOT NULL,
    usage_id uuid,
    requested_by uuid NOT NULL REFERENCES users(id),
    idempotency_key text NOT NULL,
    status text NOT NULL CHECK (status IN ('running','completed','failed','unknown','stale','cancelled')),
    version integer NOT NULL CHECK (version>0),
    created_at timestamptz NOT NULL,
    finished_at timestamptz,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    UNIQUE (workspace_id,project_id,artifact_id,revision_id,sha256,id),
    UNIQUE (workspace_id,project_id,requested_by,idempotency_key),
    FOREIGN KEY (workspace_id,project_id,artifact_id,revision_id,sha256) REFERENCES native_artifact_revisions(workspace_id,project_id,artifact_id,id,sha256),
    FOREIGN KEY (workspace_id,project_id,reviewer_agent_id) REFERENCES native_agents(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,review_task_id) REFERENCES native_tasks(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,model_id) REFERENCES project_models(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,usage_id) REFERENCES model_usage(workspace_id,project_id,id),
    CHECK ((status='running')=(finished_at IS NULL)),
    CHECK ((review_task_id IS NULL)=(snapshot->>'review_task_version' IS NULL)),
    CHECK (finished_at IS NULL OR finished_at>=created_at),
    CHECK ((status='completed')=(snapshot->>'verdict' IS NOT NULL)),
    CHECK ((status<>'completed' OR
        (snapshot->>'verdict'='approved' AND jsonb_array_length(snapshot->'findings')=0) OR
        (snapshot->>'verdict'='changes_requested' AND jsonb_array_length(snapshot->'findings')>0)) IS TRUE),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'artifact_id',artifact_id,'revision_id',revision_id,'sha256',sha256,'reviewer_agent_id',reviewer_agent_id,'review_task_id',review_task_id,'model_id',model_id,'usage_id',usage_id,'requested_by',requested_by,'idempotency_key',idempotency_key,'status',status,'version',version)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at),
    CHECK ((snapshot->>'finished_at')::timestamptz IS NOT DISTINCT FROM finished_at)
);
CREATE UNIQUE INDEX native_artifact_one_running_review ON native_artifact_reviews(workspace_id,project_id,artifact_id) WHERE status='running';

CREATE TABLE native_artifact_decisions (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    artifact_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    sha256 text NOT NULL,
    review_id uuid,
    repair_task_id uuid,
    decided_by uuid NOT NULL REFERENCES users(id),
    decision text NOT NULL CHECK (decision IN ('approved','changes_requested')),
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    FOREIGN KEY (workspace_id,project_id,artifact_id,revision_id,sha256) REFERENCES native_artifact_revisions(workspace_id,project_id,artifact_id,id,sha256),
    FOREIGN KEY (workspace_id,project_id,artifact_id,revision_id,sha256,review_id) REFERENCES native_artifact_reviews(workspace_id,project_id,artifact_id,revision_id,sha256,id),
    FOREIGN KEY (workspace_id,project_id,repair_task_id) REFERENCES native_tasks(workspace_id,project_id,id),
    CHECK (decision<>'approved' OR repair_task_id IS NULL),
    CHECK (NOT (snapshot->>'automatic')::boolean OR (decision='approved' AND review_id IS NOT NULL)),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'artifact_id',artifact_id,'revision_id',revision_id,'sha256',sha256,'review_id',review_id,'repair_task_id',repair_task_id,'decided_by',decided_by,'decision',decision)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);

CREATE INDEX native_artifact_task_order ON native_artifacts(workspace_id,project_id,task_id,created_at,id);
CREATE INDEX native_artifact_repair_order ON native_artifacts(workspace_id,project_id,pending_repair_task_id,created_at,id) WHERE pending_repair_task_id IS NOT NULL;
CREATE UNIQUE INDEX native_artifact_one_repair_mapping ON native_artifacts(workspace_id,project_id,pending_repair_task_id) WHERE pending_repair_task_id IS NOT NULL;
CREATE INDEX native_artifact_revision_order ON native_artifact_revisions(workspace_id,project_id,artifact_id,sequence,id);
CREATE INDEX native_artifact_check_order ON native_artifact_checks(workspace_id,project_id,revision_id,created_at,id);
CREATE INDEX native_artifact_review_order ON native_artifact_reviews(workspace_id,project_id,revision_id,created_at,id);
CREATE INDEX native_artifact_decision_order ON native_artifact_decisions(workspace_id,project_id,revision_id,created_at,id);

CREATE FUNCTION reject_artifact_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'artifact history is immutable' USING ERRCODE='23514';
END;
$$;
CREATE TRIGGER native_artifact_revisions_immutable BEFORE UPDATE OR DELETE ON native_artifact_revisions FOR EACH ROW EXECUTE FUNCTION reject_artifact_rewrite();
CREATE TRIGGER native_artifact_checks_immutable BEFORE UPDATE OR DELETE ON native_artifact_checks FOR EACH ROW EXECUTE FUNCTION reject_artifact_rewrite();
CREATE TRIGGER native_artifact_decisions_immutable BEFORE UPDATE OR DELETE ON native_artifact_decisions FOR EACH ROW EXECUTE FUNCTION reject_artifact_rewrite();

CREATE FUNCTION protect_artifact_head() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    next_revision native_artifact_revisions%ROWTYPE;
    previous_sequence integer;
BEGIN
    -- Share the adapter's scoped lock so direct SQL cannot create ambiguous task mappings.
    PERFORM pg_advisory_xact_lock(hashtextextended('workspace:'||NEW.workspace_id::text,0));
    IF EXISTS (SELECT 1 FROM native_artifacts a WHERE a.workspace_id=NEW.workspace_id AND a.project_id=NEW.project_id AND a.id<>NEW.id AND
        (a.task_id=NEW.task_id OR a.pending_repair_task_id=NEW.task_id OR
         (NEW.pending_repair_task_id IS NOT NULL AND (a.task_id=NEW.pending_repair_task_id OR a.pending_repair_task_id=NEW.pending_repair_task_id)))) THEN
        RAISE EXCEPTION 'artifact task mapping already belongs to another deliverable' USING ERRCODE='23514';
    END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.current_revision_id IS NOT NULL OR NEW.version<>1 THEN
            RAISE EXCEPTION 'artifact must start without a published head' USING ERRCODE='23514';
        END IF;
    ELSE
        IF NEW.version<>OLD.version+1 OR
           NEW.snapshot-ARRAY['name','current_revision_id','pending_repair_task_id','version','updated_at'] <>
           OLD.snapshot-ARRAY['name','current_revision_id','pending_repair_task_id','version','updated_at'] THEN
            RAISE EXCEPTION 'artifact identity is immutable and updates require a next version' USING ERRCODE='23514';
        END IF;
        IF NEW.current_revision_id IS DISTINCT FROM OLD.current_revision_id THEN
            SELECT * INTO next_revision FROM native_artifact_revisions WHERE id=NEW.current_revision_id AND artifact_id=NEW.id;
            SELECT sequence INTO previous_sequence FROM native_artifact_revisions WHERE id=OLD.current_revision_id;
            IF next_revision.id IS NULL OR next_revision.parent_revision_id IS DISTINCT FROM OLD.current_revision_id
               OR next_revision.sequence<>COALESCE(previous_sequence,0)+1 THEN
                RAISE EXCEPTION 'artifact head must advance in order' USING ERRCODE='23514';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER native_artifact_head_guard BEFORE INSERT OR UPDATE ON native_artifacts FOR EACH ROW EXECUTE FUNCTION protect_artifact_head();

CREATE FUNCTION protect_artifact_revision() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    artifact native_artifacts%ROWTYPE;
    parent_sequence integer;
    run native_execution_runs%ROWTYPE;
BEGIN
    SELECT * INTO artifact FROM native_artifacts WHERE id=NEW.artifact_id FOR UPDATE;
    SELECT sequence INTO parent_sequence FROM native_artifact_revisions WHERE id=NEW.parent_revision_id;
    IF artifact.id IS NULL OR NEW.parent_revision_id IS DISTINCT FROM artifact.current_revision_id
       OR NEW.sequence<>COALESCE(parent_sequence,0)+1
       OR (NEW.task_id<>artifact.task_id AND NEW.task_id IS DISTINCT FROM artifact.pending_repair_task_id) THEN
        RAISE EXCEPTION 'revision must extend current artifact head and authorized task' USING ERRCODE='23514';
    END IF;
    IF NEW.run_id IS NOT NULL THEN
        SELECT * INTO run FROM native_execution_runs WHERE id=NEW.run_id;
        IF run.id IS NULL OR run.task_id<>NEW.task_id OR run.agent_id IS DISTINCT FROM NEW.created_by_agent_id THEN
            RAISE EXCEPTION 'revision run and author must agree' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER native_artifact_revision_guard BEFORE INSERT ON native_artifact_revisions FOR EACH ROW EXECUTE FUNCTION protect_artifact_revision();

CREATE FUNCTION protect_artifact_review() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE producer uuid; original_producer uuid; assigned_maker uuid; original_task uuid; revision_task uuid; review_task native_tasks%ROWTYPE;
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'review history cannot be deleted' USING ERRCODE='23514';
    END IF;
    IF TG_OP='INSERT' THEN
        SELECT created_by_agent_id INTO producer FROM native_artifact_revisions WHERE id=NEW.revision_id;
        SELECT a.created_by_agent_id,t.assignee_agent_id INTO original_producer,assigned_maker
            FROM native_artifacts a JOIN native_tasks t ON t.id=a.task_id WHERE a.id=NEW.artifact_id;
        IF producer=NEW.reviewer_agent_id OR original_producer=NEW.reviewer_agent_id OR assigned_maker=NEW.reviewer_agent_id THEN
            RAISE EXCEPTION 'reviewer must be independent from producer' USING ERRCODE='23514';
        END IF;
        IF NEW.review_task_id IS NOT NULL THEN
            SELECT task_id INTO original_task FROM native_artifacts WHERE id=NEW.artifact_id;
            SELECT task_id INTO revision_task FROM native_artifact_revisions WHERE id=NEW.revision_id;
            SELECT * INTO review_task FROM native_tasks WHERE id=NEW.review_task_id;
            IF review_task.id IS NULL OR review_task.id=original_task OR review_task.id=revision_task
               OR review_task.version<>(NEW.snapshot->>'review_task_version')::integer
               OR review_task.assignee_agent_id IS DISTINCT FROM NEW.reviewer_agent_id THEN
                RAISE EXCEPTION 'review task must be independent, assigned and versioned' USING ERRCODE='23514';
            END IF;
        END IF;
        IF NEW.status<>'running' OR NEW.version<>1 THEN
            RAISE EXCEPTION 'review must start running at version one' USING ERRCODE='23514';
        END IF;
    ELSE
        IF (OLD.status<>'running' AND NOT (OLD.status='unknown' AND NEW.status='cancelled'))
           OR NEW.version<>OLD.version+1 OR
           NEW.snapshot-ARRAY['status','verdict','summary','findings','usage_id','finished_at','version'] <>
           OLD.snapshot-ARRAY['status','verdict','summary','findings','usage_id','finished_at','version'] THEN
            RAISE EXCEPTION 'review inputs and settled outputs are immutable' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER native_artifact_review_guard BEFORE INSERT OR UPDATE OR DELETE ON native_artifact_reviews FOR EACH ROW EXECUTE FUNCTION protect_artifact_review();

CREATE FUNCTION protect_artifact_review_policy() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'review authority cannot be deleted' USING ERRCODE='23514';
    ELSIF TG_OP='INSERT' THEN
        IF NEW.version<>1 THEN
            RAISE EXCEPTION 'review authority starts at version one' USING ERRCODE='23514';
        END IF;
    ELSIF NEW.version<>OLD.version+1 OR NEW.workspace_id<>OLD.workspace_id OR NEW.project_id<>OLD.project_id THEN
        RAISE EXCEPTION 'review authority must retain scope and advance its version' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER native_artifact_review_policy_guard BEFORE INSERT OR UPDATE OR DELETE ON native_review_policies FOR EACH ROW EXECUTE FUNCTION protect_artifact_review_policy();
