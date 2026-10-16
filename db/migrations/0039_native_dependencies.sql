-- Typed input history and exact per-output dependencies remain immutable evidence.
CREATE TABLE native_facts (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    key text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_.-]{0,95}$'),
    kind text NOT NULL CHECK (kind IN ('fact','requirement')),
    version integer NOT NULL CHECK (version>0),
    current_revision_id uuid,
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    UNIQUE (workspace_id,project_id,key),
    FOREIGN KEY (workspace_id,project_id) REFERENCES native_projects(workspace_id,id),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'key',key,'kind',kind,'version',version,'current_revision_id',current_revision_id,'created_by',created_by)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at),
    CHECK ((snapshot->>'updated_at')::timestamptz IS NOT DISTINCT FROM updated_at)
);
CREATE INDEX native_fact_scope_order ON native_facts(workspace_id,project_id,created_at,id);

CREATE TABLE native_fact_revisions (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    fact_id uuid NOT NULL,
    version integer NOT NULL CHECK (version>0),
    parent_revision_id uuid,
    value_type text NOT NULL CHECK (value_type IN ('string','number','boolean','json')),
    value_bytes bytea NOT NULL CHECK (octet_length(value_bytes)<=8192),
    unit text NOT NULL CHECK (length(unit)<=64),
    semantic_sha256 text NOT NULL CHECK (semantic_sha256 ~ '^[0-9a-f]{64}$'),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    UNIQUE (workspace_id,project_id,fact_id,id),
    UNIQUE (workspace_id,project_id,fact_id,version),
    FOREIGN KEY (workspace_id,project_id,fact_id) REFERENCES native_facts(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,fact_id,parent_revision_id) REFERENCES native_fact_revisions(workspace_id,project_id,fact_id,id),
    CHECK ((version=1)=(parent_revision_id IS NULL)),
    CHECK (parent_revision_id IS DISTINCT FROM id),
    CHECK ((convert_from(value_bytes,'UTF8')::jsonb=snapshot->'value') IS TRUE),
    CHECK (value_type='json' OR (jsonb_typeof(snapshot->'value')=value_type) IS TRUE),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'fact_id',fact_id,'version',version,'parent_revision_id',parent_revision_id,'value_type',value_type,'unit',unit,'semantic_sha256',semantic_sha256,'sha256',sha256,'created_by',created_by)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);
ALTER TABLE native_facts ADD FOREIGN KEY (workspace_id,project_id,id,current_revision_id)
    REFERENCES native_fact_revisions(workspace_id,project_id,fact_id,id);

CREATE TABLE native_dependency_declarations (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    artifact_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    version integer NOT NULL CHECK (version>0),
    coverage text NOT NULL CHECK (coverage IN ('complete','partial','unknown')),
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    UNIQUE (workspace_id,project_id,revision_id,version),
    FOREIGN KEY (workspace_id,project_id,artifact_id) REFERENCES native_artifacts(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,revision_id) REFERENCES native_artifact_revisions(workspace_id,project_id,id),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'artifact_id',artifact_id,'revision_id',revision_id,'sha256',sha256,'version',version,'coverage',coverage,'created_by',created_by)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at),
    CHECK ((jsonb_typeof(snapshot->'notes')='string' AND length(snapshot->>'notes')<=2000) IS TRUE)
);
CREATE INDEX native_dependency_current ON native_dependency_declarations(workspace_id,project_id,revision_id,version DESC);

CREATE TABLE native_impact_assessments (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    fact_id uuid NOT NULL,
    from_revision_id uuid NOT NULL,
    to_revision_id uuid NOT NULL,
    graph_fingerprint text NOT NULL CHECK (graph_fingerprint ~ '^[0-9a-f]{64}$'),
    source_fingerprint text NOT NULL CHECK (source_fingerprint ~ '^[0-9a-f]{64}$'),
    outputs_bytes bytea NOT NULL CHECK (octet_length(outputs_bytes)<=262144),
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,fact_id,from_revision_id) REFERENCES native_fact_revisions(workspace_id,project_id,fact_id,id),
    FOREIGN KEY (workspace_id,project_id,fact_id,to_revision_id) REFERENCES native_fact_revisions(workspace_id,project_id,fact_id,id),
    CHECK (from_revision_id<>to_revision_id),
    CHECK ((jsonb_typeof(snapshot->'outputs')='array' AND convert_from(outputs_bytes,'UTF8')::jsonb=snapshot->'outputs') IS TRUE),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'fact_id',fact_id,'from_revision_id',from_revision_id,'to_revision_id',to_revision_id,'graph_fingerprint',graph_fingerprint,'source_fingerprint',source_fingerprint,'created_by',created_by)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);
CREATE INDEX native_impact_scope_order ON native_impact_assessments(workspace_id,project_id,created_at,id);

CREATE TABLE native_repair_proposals (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    assessment_id uuid NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    plan_bytes bytea NOT NULL CHECK (octet_length(plan_bytes)<=262144),
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,assessment_id) REFERENCES native_impact_assessments(workspace_id,project_id,id),
    CHECK ((jsonb_typeof(snapshot->'plan')='object' AND convert_from(plan_bytes,'UTF8')::jsonb=snapshot->'plan') IS TRUE),
    CHECK (encode(digest(plan_bytes,'sha256'),'hex')=sha256),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'assessment_id',assessment_id,'sha256',sha256,'created_by',created_by)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);
CREATE INDEX native_repair_proposal_scope_order ON native_repair_proposals(workspace_id,project_id,created_at,id);

CREATE FUNCTION native_dependency_selector_valid(selector jsonb) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT (jsonb_typeof(selector)='object' AND jsonb_typeof(selector->'path')='string' AND length(selector->>'path')<=512 AND
        ((selector->>'kind'='whole_file' AND selector->>'path'='') OR
         (selector->>'kind'='json_pointer' AND (selector->>'path'='' OR left(selector->>'path',1)='/') AND selector->>'path' !~ '~([^01]|$)'))) IS TRUE
$$;

CREATE FUNCTION native_fact_sources_valid(ws uuid,pid uuid,sources jsonb) RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE binding jsonb;
BEGIN
    IF jsonb_typeof(sources) IS DISTINCT FROM 'array' THEN RETURN false; END IF;
    IF jsonb_array_length(sources)>16 OR
       (SELECT count(DISTINCT (s->>'source_revision_id',s->'selector')) FROM jsonb_array_elements(sources) s)<>jsonb_array_length(sources) THEN RETURN false; END IF;
    FOR binding IN SELECT value FROM jsonb_array_elements(sources) LOOP
        IF NOT native_dependency_selector_valid(binding->'selector') OR NOT EXISTS (
            SELECT 1 FROM native_intake_sources s WHERE s.workspace_id=ws AND s.project_id=pid
            AND s.id=(binding->>'source_revision_id')::uuid AND s.sha256=binding->>'sha256'
            AND s.revoked_at IS NULL) THEN RETURN false; END IF;
    END LOOP;
    RETURN true;
END;
$$;

CREATE FUNCTION native_dependency_outputs_valid(ws uuid,pid uuid,outputs jsonb) RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE output jsonb; revision native_artifact_revisions%ROWTYPE; artifact native_artifacts%ROWTYPE;
BEGIN
    IF jsonb_typeof(outputs) IS DISTINCT FROM 'array' THEN RETURN false; END IF;
    FOR output IN SELECT value FROM jsonb_array_elements(outputs) LOOP
        IF jsonb_typeof(output)<>'object' THEN RETURN false; END IF;
        IF output ? 'artifact_id' THEN
            SELECT * INTO artifact FROM native_artifacts WHERE workspace_id=ws AND project_id=pid AND id=(output->>'artifact_id')::uuid;
            IF artifact.id IS NULL THEN RETURN false; END IF;
            IF output->>'revision_id' IS NOT NULL THEN
                SELECT * INTO revision FROM native_artifact_revisions WHERE workspace_id=ws AND project_id=pid AND id=(output->>'revision_id')::uuid;
                IF revision.id IS NULL OR revision.artifact_id<>artifact.id OR
                   (COALESCE(output->>'sha256',output->>'before_sha256') IS NOT NULL AND
                    revision.sha256 IS DISTINCT FROM COALESCE(output->>'sha256',output->>'before_sha256')) THEN RETURN false; END IF;
            END IF;
            IF output->>'task_id' IS NOT NULL AND (output->>'task_id')::uuid IS DISTINCT FROM artifact.task_id
               AND (output->>'task_id')::uuid IS DISTINCT FROM artifact.pending_repair_task_id THEN RETURN false; END IF;
            IF output->>'declaration_id' IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM native_dependency_declarations d WHERE d.workspace_id=ws AND d.project_id=pid
                AND d.id=(output->>'declaration_id')::uuid AND d.artifact_id=artifact.id
                AND d.revision_id=(output->>'revision_id')::uuid
                AND d.version=(output->>'declaration_version')::integer) THEN RETURN false; END IF;
        END IF;
    END LOOP;
    RETURN true;
END;
$$;

CREATE FUNCTION protect_native_dependency_record() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE fact native_facts%ROWTYPE; revision native_fact_revisions%ROWTYPE; parent_version integer;
        declaration_version integer; binding jsonb; assessment native_impact_assessments%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('workspace:'||NEW.workspace_id::text,0));
    IF TG_OP='INSERT' AND NOT EXISTS (
        SELECT 1 FROM memberships m WHERE m.workspace_id=NEW.workspace_id AND m.user_id=NEW.created_by AND
        (m.role='owner' OR EXISTS (SELECT 1 FROM native_project_members pm WHERE pm.workspace_id=NEW.workspace_id AND pm.project_id=NEW.project_id AND pm.actor_id=NEW.created_by))) THEN
        RAISE EXCEPTION 'dependency author is outside its project' USING ERRCODE='23514';
    END IF;
    IF TG_TABLE_NAME='native_facts' THEN
        IF TG_OP='INSERT' THEN
            IF NEW.version<>1 OR NEW.current_revision_id IS NOT NULL THEN
                RAISE EXCEPTION 'initial fact head must be empty at version one' USING ERRCODE='23514';
            END IF;
        ELSE
            IF NEW.version<>OLD.version+1 OR NEW.snapshot-ARRAY['version','current_revision_id','updated_at']<>
               OLD.snapshot-ARRAY['version','current_revision_id','updated_at'] THEN
                RAISE EXCEPTION 'fact identity is immutable and updates require a next version' USING ERRCODE='23514';
            END IF;
            SELECT * INTO revision FROM native_fact_revisions WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND fact_id=NEW.id AND id=NEW.current_revision_id;
            IF revision.id IS NULL OR revision.version<>OLD.version OR revision.parent_revision_id IS DISTINCT FROM OLD.current_revision_id OR
               NOT native_fact_sources_valid(NEW.workspace_id,NEW.project_id,revision.snapshot->'sources') THEN
                RAISE EXCEPTION 'fact head must advance to the exact next revision with active evidence' USING ERRCODE='23514';
            END IF;
        END IF;
    ELSIF TG_TABLE_NAME='native_fact_revisions' THEN
        SELECT * INTO fact FROM native_facts WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND id=NEW.fact_id FOR UPDATE;
        SELECT version INTO parent_version FROM native_fact_revisions WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND fact_id=NEW.fact_id AND id=NEW.parent_revision_id;
        IF fact.id IS NULL OR fact.current_revision_id IS DISTINCT FROM NEW.parent_revision_id OR NEW.version<>COALESCE(parent_version,0)+1 OR
           NOT native_fact_sources_valid(NEW.workspace_id,NEW.project_id,NEW.snapshot->'sources') THEN
            RAISE EXCEPTION 'fact revision ancestry or evidence is invalid' USING ERRCODE='23514';
        END IF;
    ELSIF TG_TABLE_NAME='native_dependency_declarations' THEN
        SELECT max(version) INTO declaration_version FROM native_dependency_declarations WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND revision_id=NEW.revision_id;
        IF NEW.version<>COALESCE(declaration_version,0)+1 OR NOT EXISTS (
            SELECT 1 FROM native_artifact_revisions r WHERE r.workspace_id=NEW.workspace_id AND r.project_id=NEW.project_id AND r.id=NEW.revision_id AND r.artifact_id=NEW.artifact_id AND r.sha256=NEW.sha256) THEN
            RAISE EXCEPTION 'dependency declaration must extend its exact output history' USING ERRCODE='23514';
        END IF;
        IF jsonb_typeof(NEW.snapshot->'dependencies') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'dependency bindings must be an array' USING ERRCODE='23514';
        END IF;
        IF jsonb_array_length(NEW.snapshot->'dependencies')>32 OR
           (SELECT count(DISTINCT value) FROM jsonb_array_elements(NEW.snapshot->'dependencies'))<>jsonb_array_length(NEW.snapshot->'dependencies') THEN
            RAISE EXCEPTION 'dependency bindings exceed limits or repeat' USING ERRCODE='23514';
        END IF;
        FOR binding IN SELECT value FROM jsonb_array_elements(NEW.snapshot->'dependencies') LOOP
            IF NOT native_dependency_selector_valid(binding->'input_selector') OR NOT native_dependency_selector_valid(binding->'output_selector') OR
               (binding->>'input_kind' IN ('fact','artifact')) IS NOT TRUE OR
               (binding->>'input_kind'='fact' AND NOT EXISTS (SELECT 1 FROM native_fact_revisions r WHERE r.workspace_id=NEW.workspace_id AND r.project_id=NEW.project_id AND r.id=(binding->>'revision_id')::uuid AND r.fact_id=(binding->>'input_id')::uuid AND r.sha256=binding->>'sha256')) OR
               (binding->>'input_kind'='artifact' AND NOT EXISTS (SELECT 1 FROM native_artifact_revisions r WHERE r.workspace_id=NEW.workspace_id AND r.project_id=NEW.project_id AND r.id=(binding->>'revision_id')::uuid AND r.artifact_id=(binding->>'input_id')::uuid AND r.sha256=binding->>'sha256')) THEN
                RAISE EXCEPTION 'dependency selector or exact input scope is invalid' USING ERRCODE='23514';
            END IF;
        END LOOP;
    ELSIF TG_TABLE_NAME='native_impact_assessments' THEN
        SELECT version INTO parent_version FROM native_fact_revisions WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND fact_id=NEW.fact_id AND id=NEW.from_revision_id;
        SELECT * INTO revision FROM native_fact_revisions WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND fact_id=NEW.fact_id AND id=NEW.to_revision_id;
        IF parent_version IS NULL OR revision.id IS NULL OR parent_version>=revision.version OR
           NOT native_dependency_outputs_valid(NEW.workspace_id,NEW.project_id,NEW.snapshot->'outputs') THEN
            RAISE EXCEPTION 'impact ancestry or output scope is invalid' USING ERRCODE='23514';
        END IF;
    ELSIF TG_TABLE_NAME='native_repair_proposals' THEN
        SELECT * INTO assessment FROM native_impact_assessments WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND id=NEW.assessment_id;
        IF assessment.id IS NULL OR
           (NEW.snapshot->'plan' ? 'assessment_id' AND (NEW.snapshot->'plan'->>'assessment_id')::uuid<>assessment.id) OR
           (NEW.snapshot->'plan' ? 'graph_fingerprint' AND NEW.snapshot->'plan'->>'graph_fingerprint' IS DISTINCT FROM assessment.graph_fingerprint) OR
           (NEW.snapshot->'plan' ? 'source_fingerprint' AND NEW.snapshot->'plan'->>'source_fingerprint' IS DISTINCT FROM assessment.source_fingerprint) OR
           NOT native_dependency_outputs_valid(NEW.workspace_id,NEW.project_id,COALESCE(NEW.snapshot->'plan'->'outputs','[]'::jsonb)) THEN
            RAISE EXCEPTION 'repair plan assessment or output scope is invalid' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER native_fact_head_guard BEFORE INSERT OR UPDATE ON native_facts FOR EACH ROW EXECUTE FUNCTION protect_native_dependency_record();
CREATE TRIGGER native_fact_delete_guard BEFORE DELETE ON native_facts FOR EACH ROW EXECUTE FUNCTION reject_artifact_rewrite();
CREATE TRIGGER native_fact_revision_guard BEFORE INSERT ON native_fact_revisions FOR EACH ROW EXECUTE FUNCTION protect_native_dependency_record();
CREATE TRIGGER native_dependency_declaration_guard BEFORE INSERT ON native_dependency_declarations FOR EACH ROW EXECUTE FUNCTION protect_native_dependency_record();
CREATE TRIGGER native_impact_guard BEFORE INSERT ON native_impact_assessments FOR EACH ROW EXECUTE FUNCTION protect_native_dependency_record();
CREATE TRIGGER native_repair_proposal_guard BEFORE INSERT ON native_repair_proposals FOR EACH ROW EXECUTE FUNCTION protect_native_dependency_record();
CREATE TRIGGER native_fact_revisions_immutable BEFORE UPDATE OR DELETE ON native_fact_revisions FOR EACH ROW EXECUTE FUNCTION reject_artifact_rewrite();
CREATE TRIGGER native_dependency_declarations_immutable BEFORE UPDATE OR DELETE ON native_dependency_declarations FOR EACH ROW EXECUTE FUNCTION reject_artifact_rewrite();
CREATE TRIGGER native_impact_assessments_immutable BEFORE UPDATE OR DELETE ON native_impact_assessments FOR EACH ROW EXECUTE FUNCTION reject_artifact_rewrite();
CREATE TRIGGER native_repair_proposals_immutable BEFORE UPDATE OR DELETE ON native_repair_proposals FOR EACH ROW EXECUTE FUNCTION reject_artifact_rewrite();
