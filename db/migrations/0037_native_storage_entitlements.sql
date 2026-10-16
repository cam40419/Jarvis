-- Storage policies retain immutable version history; object charges bind exact retained bytes.
CREATE TABLE native_storage_entitlements (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    version integer NOT NULL CHECK (version>0),
    plan_key text NOT NULL CHECK (plan_key ~ '^[a-z][a-z0-9_-]{0,63}$'),
    limit_bytes bigint NOT NULL CHECK (limit_bytes BETWEEN 0 AND 1099511627776),
    override_limit_bytes bigint CHECK (override_limit_bytes BETWEEN 0 AND 1099511627776),
    override_expires_at timestamptz,
    reason text NOT NULL CHECK (reason=btrim(reason) AND length(reason) BETWEEN 1 AND 1000),
    issued_by uuid NOT NULL REFERENCES users(id),
    updated_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    PRIMARY KEY (workspace_id,version),
    CHECK ((override_limit_bytes IS NULL)=(override_expires_at IS NULL)),
    CHECK (snapshot @> jsonb_build_object('workspace_id',workspace_id,'version',version,
        'plan_key',plan_key,'limit_bytes',limit_bytes,'override_limit_bytes',override_limit_bytes,
        'reason',reason,'issued_by',issued_by)),
    CHECK ((snapshot->>'updated_at')::timestamptz IS NOT DISTINCT FROM updated_at),
    CHECK ((snapshot->>'override_expires_at')::timestamptz IS NOT DISTINCT FROM override_expires_at),
    CHECK ((snapshot->>'version') ~ '^[0-9]+$' AND (snapshot->>'limit_bytes') ~ '^[0-9]+$'),
    CHECK (override_limit_bytes IS NULL OR (snapshot->>'override_limit_bytes') ~ '^[0-9]+$'),
    CHECK ((snapshot->>'updated_at') ~ '([zZ]|[+-][0-9]{2}:[0-9]{2})$'),
    CHECK (override_expires_at IS NULL OR (snapshot->>'override_expires_at') ~ '([zZ]|[+-][0-9]{2}:[0-9]{2})$'),
    CHECK (snapshot-ARRAY['workspace_id','version','plan_key','limit_bytes','override_limit_bytes',
        'override_expires_at','reason','issued_by','updated_at']='{}'::jsonb)
);

CREATE TABLE native_storage_charges (
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    object_kind text NOT NULL CHECK (object_kind IN ('source','artifact','delivery')),
    object_id uuid NOT NULL,
    bytes bigint NOT NULL CHECK (bytes>=0),
    state text NOT NULL CHECK (state IN ('reserved','retained','released')),
    sha256 text CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    request_fingerprint text CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    PRIMARY KEY (workspace_id,object_kind,object_id),
    FOREIGN KEY (workspace_id,project_id) REFERENCES native_projects(workspace_id,id),
    CHECK (snapshot @> jsonb_build_object('workspace_id',workspace_id,'project_id',project_id,
        'object_kind',object_kind,'object_id',object_id,'bytes',bytes,'state',state,
        'sha256',sha256,'request_fingerprint',request_fingerprint)),
    CHECK ((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at),
    CHECK ((snapshot->>'bytes') ~ '^[0-9]+$'),
    CHECK ((snapshot->>'created_at') ~ '([zZ]|[+-][0-9]{2}:[0-9]{2})$'),
    CHECK (snapshot-ARRAY['workspace_id','project_id','object_kind','object_id','bytes','state',
        'sha256','request_fingerprint','created_at']='{}'::jsonb)
);
CREATE INDEX native_storage_charge_project_order ON native_storage_charges(workspace_id,project_id,created_at,object_kind,object_id);
CREATE INDEX native_storage_charge_active_totals ON native_storage_charges(workspace_id,state) INCLUDE (bytes);

-- Existing retained data is accounted without applying new admission limits retroactively.
INSERT INTO native_storage_charges
    (workspace_id,project_id,object_kind,object_id,bytes,state,sha256,request_fingerprint,created_at,snapshot)
SELECT workspace_id,project_id,kind,id,bytes,'retained',sha256,NULL,created_at,
       jsonb_build_object('workspace_id',workspace_id,'project_id',project_id,'object_kind',kind,
           'object_id',id,'bytes',bytes,'state','retained','sha256',sha256,
           'request_fingerprint',NULL,'created_at',created_at)
FROM (
    SELECT workspace_id,project_id,'source'::text AS kind,id,
           size_bytes::bigint+octet_length(convert_to(COALESCE(snapshot->>'text',''),'UTF8')) AS bytes,
           sha256,created_at FROM native_intake_sources
    UNION ALL
    SELECT workspace_id,project_id,'artifact',id,size_bytes::bigint,sha256,created_at
        FROM native_artifact_revisions
    UNION ALL
    SELECT workspace_id,project_id,'delivery',id,
           octet_length(manifest_bytes)::bigint+COALESCE((SELECT sum((p->>'size_bytes')::bigint)
               FROM jsonb_array_elements(snapshot->'manifest'->'previews') p),0),
           manifest_sha256,created_at FROM native_delivery_packets
) retained;

CREATE FUNCTION protect_native_storage_entitlement() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE current_version integer;
BEGIN
    IF TG_OP<>'INSERT' THEN
        RAISE EXCEPTION 'storage entitlement history is immutable' USING ERRCODE='23514';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('workspace:'||NEW.workspace_id::text,0));
    SELECT COALESCE(max(version),0) INTO current_version FROM native_storage_entitlements WHERE workspace_id=NEW.workspace_id;
    IF NEW.version<>current_version+1 THEN
        RAISE EXCEPTION 'storage entitlement version changed' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER native_storage_entitlement_guard BEFORE INSERT OR UPDATE OR DELETE ON native_storage_entitlements
    FOR EACH ROW EXECUTE FUNCTION protect_native_storage_entitlement();

CREATE FUNCTION protect_native_storage_charge() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE used_bytes numeric;
DECLARE effective_limit bigint;
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'storage accounting history is immutable' USING ERRCODE='23514';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('workspace:'||NEW.workspace_id::text,0));
    IF TG_OP='UPDATE' THEN
        IF ROW(NEW.workspace_id,NEW.project_id,NEW.object_kind,NEW.object_id,NEW.bytes,NEW.sha256,
               NEW.request_fingerprint,NEW.created_at) IS DISTINCT FROM
           ROW(OLD.workspace_id,OLD.project_id,OLD.object_kind,OLD.object_id,OLD.bytes,OLD.sha256,
               OLD.request_fingerprint,OLD.created_at) OR
           (NEW.snapshot-'state') IS DISTINCT FROM (OLD.snapshot-'state') OR
           OLD.state<>'reserved' OR NEW.state NOT IN ('retained','released') THEN
            RAISE EXCEPTION 'storage binding or terminal state is immutable' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.state='released' OR (NEW.state='reserved' AND (NEW.object_kind<>'source' OR NEW.sha256 IS NULL)) THEN
        RAISE EXCEPTION 'new storage charges must bind reserved source or retained content' USING ERRCODE='23514';
    END IF;
    SELECT CASE WHEN override_limit_bytes IS NOT NULL AND override_expires_at>clock_timestamp()
                THEN override_limit_bytes ELSE limit_bytes END
        INTO effective_limit FROM native_storage_entitlements WHERE workspace_id=NEW.workspace_id ORDER BY version DESC LIMIT 1;
    effective_limit=COALESCE(effective_limit,268435456);
    SELECT COALESCE(sum(bytes),0) INTO used_bytes FROM native_storage_charges
        WHERE workspace_id=NEW.workspace_id AND state IN ('reserved','retained');
    IF NEW.bytes>0 AND used_bytes+NEW.bytes>effective_limit THEN
        RAISE EXCEPTION 'storage_quota_exceeded: workspace storage quota exceeded'
            USING ERRCODE='23514',CONSTRAINT='native_storage_quota';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER native_storage_charge_guard BEFORE INSERT OR UPDATE OR DELETE ON native_storage_charges
    FOR EACH ROW EXECUTE FUNCTION protect_native_storage_charge();

CREATE FUNCTION protect_native_storage_source() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'retained source history is immutable' USING ERRCODE='23514';
    END IF;
    IF ROW(NEW.id,NEW.workspace_id,NEW.project_id,NEW.source_key,NEW.revision,NEW.sha256,
           NEW.size_bytes,NEW.created_by,NEW.created_at) IS DISTINCT FROM
       ROW(OLD.id,OLD.workspace_id,OLD.project_id,OLD.source_key,OLD.revision,OLD.sha256,
           OLD.size_bytes,OLD.created_by,OLD.created_at) OR
       (NEW.snapshot-'revoked_at') IS DISTINCT FROM (OLD.snapshot-'revoked_at') OR
       OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL THEN
        RAISE EXCEPTION 'retained source content is immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER native_storage_source_guard BEFORE UPDATE OR DELETE ON native_intake_sources
    FOR EACH ROW EXECUTE FUNCTION protect_native_storage_source();

CREATE FUNCTION retain_native_storage_charge(ws uuid,pid uuid,kind text,oid uuid,amount bigint,hash text,at timestamptz)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE existing native_storage_charges%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('workspace:'||ws::text,0));
    SELECT * INTO existing FROM native_storage_charges WHERE workspace_id=ws AND object_kind=kind AND object_id=oid;
    IF existing.object_id IS NOT NULL THEN
        IF existing.project_id IS DISTINCT FROM pid OR existing.bytes IS DISTINCT FROM amount OR
           existing.sha256 IS DISTINCT FROM hash OR existing.state='released' THEN
            RAISE EXCEPTION 'storage reservation does not match retained content' USING ERRCODE='23514';
        END IF;
        IF existing.state='reserved' THEN
            UPDATE native_storage_charges SET state='retained',snapshot=jsonb_set(snapshot,'{state}','"retained"'::jsonb)
                WHERE workspace_id=ws AND object_kind=kind AND object_id=oid;
        END IF;
        RETURN;
    END IF;
    INSERT INTO native_storage_charges
        (workspace_id,project_id,object_kind,object_id,bytes,state,sha256,request_fingerprint,created_at,snapshot)
    VALUES (ws,pid,kind,oid,amount,'retained',hash,NULL,at,
        jsonb_build_object('workspace_id',ws,'project_id',pid,'object_kind',kind,'object_id',oid,
            'bytes',amount,'state','retained','sha256',hash,'request_fingerprint',NULL,'created_at',at));
END;
$$;

CREATE FUNCTION account_native_storage_object() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE amount bigint;
BEGIN
    IF TG_TABLE_NAME='native_intake_sources' THEN
        amount=NEW.size_bytes::bigint+octet_length(convert_to(COALESCE(NEW.snapshot->>'text',''),'UTF8'));
        PERFORM retain_native_storage_charge(NEW.workspace_id,NEW.project_id,'source',NEW.id,amount,NEW.sha256,NEW.created_at);
    ELSIF TG_TABLE_NAME='native_artifact_revisions' THEN
        PERFORM retain_native_storage_charge(NEW.workspace_id,NEW.project_id,'artifact',NEW.id,NEW.size_bytes::bigint,NEW.sha256,NEW.created_at);
    ELSE
        amount=octet_length(NEW.manifest_bytes)::bigint+COALESCE((SELECT sum((p->>'size_bytes')::bigint)
            FROM jsonb_array_elements(NEW.snapshot->'manifest'->'previews') p),0);
        PERFORM retain_native_storage_charge(NEW.workspace_id,NEW.project_id,'delivery',NEW.id,amount,NEW.manifest_sha256,NEW.created_at);
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER native_intake_source_storage AFTER INSERT ON native_intake_sources FOR EACH ROW EXECUTE FUNCTION account_native_storage_object();
CREATE TRIGGER native_artifact_revision_storage AFTER INSERT ON native_artifact_revisions FOR EACH ROW EXECUTE FUNCTION account_native_storage_object();
CREATE TRIGGER native_delivery_packet_storage AFTER INSERT ON native_delivery_packets FOR EACH ROW EXECUTE FUNCTION account_native_storage_object();
