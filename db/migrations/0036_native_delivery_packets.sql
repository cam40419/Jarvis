-- Delivery packets preserve immutable exact-file and review/evidence snapshots.
CREATE TABLE native_delivery_packets (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL,
    previous_packet_id uuid,
    sequence integer NOT NULL CHECK (sequence>0),
    manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    manifest_bytes bytea NOT NULL CHECK (octet_length(manifest_bytes)<=1048576),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    UNIQUE (workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id) REFERENCES native_projects(workspace_id,id),
    FOREIGN KEY (workspace_id,project_id,previous_packet_id) REFERENCES native_delivery_packets(workspace_id,project_id,id),
    CHECK ((sequence=1)=(previous_packet_id IS NULL)),
    CHECK (previous_packet_id IS DISTINCT FROM id),
    CHECK (snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'created_by',created_by,'previous_packet_id',previous_packet_id,'sequence',sequence,'manifest_sha256',manifest_sha256)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at),
    CHECK ((convert_from(manifest_bytes,'UTF8')::jsonb=snapshot->'manifest') IS TRUE),
    CHECK (encode(digest(manifest_bytes,'sha256'),'hex')=manifest_sha256)
);
CREATE INDEX native_delivery_packet_scope_order ON native_delivery_packets(workspace_id,project_id,created_at,id);
CREATE UNIQUE INDEX native_delivery_one_successor ON native_delivery_packets(workspace_id,project_id,previous_packet_id) WHERE previous_packet_id IS NOT NULL;
CREATE TRIGGER native_delivery_packets_immutable BEFORE UPDATE OR DELETE ON native_delivery_packets
    FOR EACH ROW EXECUTE FUNCTION reject_artifact_rewrite();

CREATE FUNCTION validate_native_delivery_packet() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    manifest jsonb;
    component jsonb;
    evidence jsonb;
    preview jsonb;
    reference jsonb;
    revision native_artifact_revisions%ROWTYPE;
    artifact native_artifacts%ROWTYPE;
    task native_tasks%ROWTYPE;
    parent_sequence integer;
    project_version integer;
    policy_version integer;
    source_hash text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('workspace:'||NEW.workspace_id::text,0));
    IF NOT EXISTS (SELECT 1 FROM memberships m WHERE m.workspace_id=NEW.workspace_id AND m.user_id=NEW.created_by AND
        (m.role='owner' OR EXISTS (SELECT 1 FROM native_project_members pm WHERE pm.workspace_id=NEW.workspace_id AND pm.project_id=NEW.project_id AND pm.actor_id=NEW.created_by))) THEN
        RAISE EXCEPTION 'delivery creator is outside its project' USING ERRCODE='23514';
    END IF;
    IF NEW.previous_packet_id IS NOT NULL THEN
        SELECT sequence INTO parent_sequence FROM native_delivery_packets WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND id=NEW.previous_packet_id;
        IF parent_sequence IS NULL OR NEW.sequence<>parent_sequence+1 THEN
            RAISE EXCEPTION 'delivery predecessor must identify the prior scoped packet' USING ERRCODE='23514';
        END IF;
    END IF;
    manifest=NEW.snapshot->'manifest';
    IF (jsonb_typeof(manifest)='object' AND manifest->>'schema_version'='1' AND
        jsonb_typeof(manifest->'components')='array' AND jsonb_typeof(manifest->'previews')='array' AND
        jsonb_typeof(manifest->'source_revision_ids')='array' AND jsonb_typeof(manifest->'limitations')='array' AND
        jsonb_typeof(manifest->'declared_limitations')='array' AND
        manifest->>'source_fingerprint' ~ '^[0-9a-f]{64}$') IS NOT TRUE THEN
        RAISE EXCEPTION 'delivery manifest structure is invalid' USING ERRCODE='23514';
    END IF;
    IF jsonb_array_length(manifest->'components') NOT BETWEEN 1 AND 20 OR
       jsonb_array_length(manifest->'previews')>20 OR jsonb_array_length(manifest->'source_revision_ids')>1000 OR
       jsonb_array_length(manifest->'limitations')>64 OR jsonb_array_length(manifest->'declared_limitations')>20 THEN
        RAISE EXCEPTION 'delivery manifest exceeds its collection limits' USING ERRCODE='23514';
    END IF;
    IF (length(manifest->>'title') BETWEEN 1 AND 240) IS NOT TRUE OR
       EXISTS (SELECT 1 FROM jsonb_array_elements((manifest->'limitations')||(manifest->'declared_limitations')) v
               WHERE jsonb_typeof(v)<>'string' OR length(v#>>'{}') NOT BETWEEN 1 AND 1000) THEN
        RAISE EXCEPTION 'delivery title or limitation text is invalid' USING ERRCODE='23514';
    END IF;
    SELECT version INTO project_version FROM native_projects WHERE workspace_id=NEW.workspace_id AND id=NEW.project_id;
    SELECT version INTO policy_version FROM native_review_policies WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id;
    IF project_version IS NULL OR (manifest->>'project_version')::integer IS DISTINCT FROM project_version OR
       (manifest->>'policy_version')::integer IS DISTINCT FROM COALESCE(policy_version,0) THEN
        RAISE EXCEPTION 'delivery project or policy snapshot changed' USING ERRCODE='23514';
    END IF;
    IF (SELECT count(DISTINCT c->>'artifact_id') FROM jsonb_array_elements(manifest->'components') c)<>jsonb_array_length(manifest->'components') OR
       (SELECT count(DISTINCT c->>'revision_id') FROM jsonb_array_elements(manifest->'components') c)<>jsonb_array_length(manifest->'components') OR
       NOT EXISTS (SELECT 1 FROM jsonb_array_elements(manifest->'components') c WHERE c->>'role'='source') THEN
        RAISE EXCEPTION 'delivery components must be distinct and include a source' USING ERRCODE='23514';
    END IF;
    FOR component IN SELECT value FROM jsonb_array_elements(manifest->'components') LOOP
        SELECT * INTO revision FROM native_artifact_revisions WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND id=(component->>'revision_id')::uuid;
        SELECT * INTO artifact FROM native_artifacts WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND id=(component->>'artifact_id')::uuid;
        SELECT * INTO task FROM native_tasks WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND id=artifact.task_id;
        IF revision.id IS NULL OR artifact.id IS NULL OR task.id IS NULL OR revision.artifact_id<>artifact.id OR
           artifact.current_revision_id IS DISTINCT FROM revision.id OR
           revision.sha256 IS DISTINCT FROM component->>'sha256' OR
           revision.snapshot->>'filename' IS DISTINCT FROM component->>'filename' OR
           revision.snapshot->>'media_type' IS DISTINCT FROM component->>'media_type' OR
           revision.size_bytes IS DISTINCT FROM (component->>'size_bytes')::integer OR
           artifact.version IS DISTINCT FROM (component->>'artifact_version')::integer OR
           task.version IS DISTINCT FROM (component->>'task_version')::integer THEN
            RAISE EXCEPTION 'delivery component content, metadata or versions do not match' USING ERRCODE='23514';
        END IF;
        IF (component->>'role' IN ('source','preview','evidence','attachment')) IS NOT TRUE OR
           ((component->>'role'='preview')<>(component->>'derived_from_revision_id' IS NOT NULL)) OR
           (component->>'role'='preview' AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(manifest->'components') c WHERE c->>'role'='source' AND c->>'revision_id'=component->>'derived_from_revision_id')) THEN
            RAISE EXCEPTION 'delivery preview derivation is invalid' USING ERRCODE='23514';
        END IF;
        IF (jsonb_typeof(component->'checks')='array' AND jsonb_typeof(component->'decisions')='array' AND jsonb_typeof(component->'review_ids')='array' AND jsonb_typeof(component->'review_states')='array') IS NOT TRUE THEN
            RAISE EXCEPTION 'delivery evidence lists are invalid' USING ERRCODE='23514';
        END IF;
        IF jsonb_array_length(component->'checks')>100 OR jsonb_array_length(component->'decisions')>100 OR
           jsonb_array_length(component->'review_ids')>100 OR jsonb_array_length(component->'review_states')>100 OR
           (length(component->>'status_at_creation') BETWEEN 1 AND 64) IS NOT TRUE OR
           (SELECT count(DISTINCT c->>'id') FROM jsonb_array_elements(component->'checks') c)<>jsonb_array_length(component->'checks') OR
           (SELECT count(DISTINCT d->>'id') FROM jsonb_array_elements(component->'decisions') d)<>jsonb_array_length(component->'decisions') OR
           (SELECT count(DISTINCT value) FROM jsonb_array_elements(component->'review_ids'))<>jsonb_array_length(component->'review_ids') OR
           (SELECT count(DISTINCT s->>'id') FROM jsonb_array_elements(component->'review_states') s)<>jsonb_array_length(component->'review_states') OR
           jsonb_array_length(component->'review_states')<>jsonb_array_length(component->'review_ids') OR
           EXISTS (SELECT 1 FROM jsonb_array_elements(component->'review_states') s
                   WHERE NOT (component->'review_ids') ? (s->>'id')) THEN
            RAISE EXCEPTION 'delivery evidence identities or collection limits are invalid' USING ERRCODE='23514';
        END IF;
        FOR evidence IN SELECT value FROM jsonb_array_elements(component->'checks') LOOP
            IF NOT EXISTS (SELECT 1 FROM native_artifact_checks c WHERE c.workspace_id=NEW.workspace_id AND c.project_id=NEW.project_id AND c.revision_id=revision.id AND c.sha256=revision.sha256 AND c.id=(evidence->>'id')::uuid AND c.snapshot=evidence) THEN
                RAISE EXCEPTION 'delivery check snapshot does not match immutable evidence' USING ERRCODE='23514';
            END IF;
        END LOOP;
        FOR evidence IN SELECT value FROM jsonb_array_elements(component->'decisions') LOOP
            IF NOT EXISTS (SELECT 1 FROM native_artifact_decisions d WHERE d.workspace_id=NEW.workspace_id AND d.project_id=NEW.project_id AND d.artifact_id=artifact.id AND d.revision_id=revision.id AND d.sha256=revision.sha256 AND d.id=(evidence->>'id')::uuid AND (d.snapshot||jsonb_build_object('authority_snapshot',COALESCE(d.snapshot->'authority_snapshot','{}'::jsonb)))=evidence) THEN
                RAISE EXCEPTION 'delivery decision snapshot does not match immutable evidence' USING ERRCODE='23514';
            END IF;
        END LOOP;
        FOR reference IN SELECT value FROM jsonb_array_elements(component->'review_ids') LOOP
            IF NOT EXISTS (SELECT 1 FROM native_artifact_reviews r WHERE r.workspace_id=NEW.workspace_id AND r.project_id=NEW.project_id AND r.artifact_id=artifact.id AND r.revision_id=revision.id AND r.sha256=revision.sha256 AND r.id=(reference#>>'{}')::uuid) THEN
                RAISE EXCEPTION 'delivery review reference identifies different content' USING ERRCODE='23514';
            END IF;
        END LOOP;
        FOR evidence IN SELECT value FROM jsonb_array_elements(component->'review_states') LOOP
            IF NOT EXISTS (SELECT 1 FROM native_artifact_reviews r WHERE r.workspace_id=NEW.workspace_id AND r.project_id=NEW.project_id AND r.artifact_id=artifact.id AND r.revision_id=revision.id AND r.sha256=revision.sha256 AND r.id=(evidence->>'id')::uuid AND r.version=(evidence->>'version')::integer AND r.status=evidence->>'status' AND (r.snapshot->>'verdict') IS NOT DISTINCT FROM evidence->>'verdict') THEN
                RAISE EXCEPTION 'delivery review state changed before capture' USING ERRCODE='23514';
            END IF;
        END LOOP;
    END LOOP;
    IF (SELECT count(DISTINCT p->>'revision_id') FROM jsonb_array_elements(manifest->'previews') p)<>jsonb_array_length(manifest->'previews') OR
       jsonb_array_length(manifest->'previews')<>(SELECT count(*) FROM jsonb_array_elements(manifest->'components') c WHERE c->>'role'='source') THEN
        RAISE EXCEPTION 'each delivery source requires one generated preview' USING ERRCODE='23514';
    END IF;
    FOR preview IN SELECT value FROM jsonb_array_elements(manifest->'previews') LOOP
        SELECT c->>'sha256' INTO source_hash FROM jsonb_array_elements(manifest->'components') c WHERE c->>'role'='source' AND c->>'revision_id'=preview->>'revision_id';
        IF source_hash IS NULL OR source_hash IS DISTINCT FROM preview->>'sha256' OR
           preview->>'path' IS DISTINCT FROM 'previews/'||(preview->>'revision_id')||'.html' OR
           preview->>'renderer' IS DISTINCT FROM 'simon.safe-text.v1' OR
           (preview->>'content_sha256' ~ '^[0-9a-f]{64}$') IS NOT TRUE OR
           ((preview->>'size_bytes')::integer BETWEEN 0 AND 524288) IS NOT TRUE OR
           jsonb_typeof(preview->'supported') IS DISTINCT FROM 'boolean' OR
           length(COALESCE(preview->>'limitation',''))>1000 OR
           (preview->>'supported'='false' AND COALESCE(preview->>'limitation','')='') THEN
            RAISE EXCEPTION 'delivery generated preview metadata or source binding is invalid' USING ERRCODE='23514';
        END IF;
    END LOOP;
    IF (SELECT count(DISTINCT value) FROM jsonb_array_elements(manifest->'source_revision_ids'))<>jsonb_array_length(manifest->'source_revision_ids') THEN
        RAISE EXCEPTION 'delivery intake source identities must be unique' USING ERRCODE='23514';
    END IF;
    FOR reference IN SELECT value FROM jsonb_array_elements(manifest->'source_revision_ids') LOOP
        IF NOT EXISTS (SELECT 1 FROM native_intake_sources s WHERE s.workspace_id=NEW.workspace_id AND s.project_id=NEW.project_id AND s.id=(reference#>>'{}')::uuid AND s.revoked_at IS NULL) THEN
            RAISE EXCEPTION 'delivery intake source is missing, revoked or outside project' USING ERRCODE='23514';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;
CREATE TRIGGER native_delivery_packet_guard BEFORE INSERT ON native_delivery_packets
    FOR EACH ROW EXECUTE FUNCTION validate_native_delivery_packet();
