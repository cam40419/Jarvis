-- Immutable local package validation, owner acceptance and continuity metadata.
ALTER TABLE native_storage_charges DROP CONSTRAINT native_storage_charges_object_kind_check;
ALTER TABLE native_storage_charges ADD CHECK(object_kind IN ('source','artifact','delivery','delivery_validation','acceptance','continuity'));

CREATE TABLE native_delivery_profile_assignments (
 id uuid PRIMARY KEY, workspace_id uuid NOT NULL, project_id uuid NOT NULL, packet_id uuid NOT NULL,
 version integer NOT NULL CHECK(version>0),
 manifest_sha256 text NOT NULL CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
 created_by uuid NOT NULL REFERENCES users(id), created_at timestamptz NOT NULL,
 record_bytes bytea NOT NULL CHECK(octet_length(record_bytes)<=1048576), snapshot jsonb NOT NULL,
 UNIQUE(workspace_id,project_id,id), UNIQUE(workspace_id,project_id,packet_id,id), UNIQUE(workspace_id,project_id,packet_id,version), FOREIGN KEY(workspace_id,project_id,packet_id) REFERENCES native_delivery_packets(workspace_id,project_id,id),
 CHECK((convert_from(record_bytes,'UTF8')::jsonb=snapshot) IS TRUE),
 CHECK(snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'packet_id',packet_id,'manifest_sha256',manifest_sha256,'created_by',created_by,'version',version)),
 CHECK((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);
CREATE INDEX native_delivery_profile_assignments_history ON native_delivery_profile_assignments(workspace_id,project_id,packet_id,version);
CREATE TABLE native_delivery_validations (
 id uuid PRIMARY KEY, workspace_id uuid NOT NULL, project_id uuid NOT NULL, packet_id uuid NOT NULL,
 assignment_id uuid NOT NULL, profile_version integer NOT NULL CHECK(profile_version>0), authority_sha256 text NOT NULL CHECK(authority_sha256 ~ '^[0-9a-f]{64}$'), passed boolean NOT NULL,
 manifest_sha256 text NOT NULL CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
 created_by uuid NOT NULL REFERENCES users(id), created_at timestamptz NOT NULL,
 record_bytes bytea NOT NULL CHECK(octet_length(record_bytes)<=1048576), snapshot jsonb NOT NULL,
 UNIQUE(workspace_id,project_id,id), UNIQUE(workspace_id,project_id,packet_id,id), FOREIGN KEY(workspace_id,project_id,packet_id,assignment_id) REFERENCES native_delivery_profile_assignments(workspace_id,project_id,packet_id,id),
 CHECK((convert_from(record_bytes,'UTF8')::jsonb=snapshot) IS TRUE),
 CHECK(snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'packet_id',packet_id,'manifest_sha256',manifest_sha256,'created_by',created_by,'assignment_id',assignment_id,'profile_version',profile_version,'authority_sha256',authority_sha256,'passed',passed)),
 CHECK((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);
CREATE INDEX native_delivery_validations_history ON native_delivery_validations(workspace_id,project_id,packet_id,created_at,id);
CREATE TABLE native_delivery_acceptances (
 id uuid PRIMARY KEY, workspace_id uuid NOT NULL, project_id uuid NOT NULL, packet_id uuid NOT NULL,
 assignment_id uuid NOT NULL, profile_version integer NOT NULL CHECK(profile_version>0), validation_id uuid NOT NULL, authority_sha256 text NOT NULL CHECK(authority_sha256 ~ '^[0-9a-f]{64}$'),
 manifest_sha256 text NOT NULL CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
 created_by uuid NOT NULL REFERENCES users(id), created_at timestamptz NOT NULL,
 record_bytes bytea NOT NULL CHECK(octet_length(record_bytes)<=1048576), snapshot jsonb NOT NULL,
 UNIQUE(workspace_id,project_id,id), FOREIGN KEY(workspace_id,project_id,packet_id,assignment_id) REFERENCES native_delivery_profile_assignments(workspace_id,project_id,packet_id,id), FOREIGN KEY(workspace_id,project_id,packet_id,validation_id) REFERENCES native_delivery_validations(workspace_id,project_id,packet_id,id),
 CHECK((convert_from(record_bytes,'UTF8')::jsonb=snapshot) IS TRUE),
 CHECK(snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'packet_id',packet_id,'manifest_sha256',manifest_sha256,'created_by',created_by,'assignment_id',assignment_id,'profile_version',profile_version,'validation_id',validation_id,'authority_sha256',authority_sha256)),
 CHECK((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);
CREATE INDEX native_delivery_acceptances_history ON native_delivery_acceptances(workspace_id,project_id,packet_id,created_at,id);
CREATE TABLE native_continuity_handoffs (
 id uuid PRIMARY KEY, workspace_id uuid NOT NULL, project_id uuid NOT NULL,
 created_by uuid NOT NULL REFERENCES users(id), recipient_id uuid NOT NULL REFERENCES users(id),
 manifest_sha256 text NOT NULL CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'), created_at timestamptz NOT NULL,
 manifest_bytes bytea NOT NULL CHECK(octet_length(manifest_bytes)<=1048576), snapshot jsonb NOT NULL,
 UNIQUE(workspace_id,project_id,id),
 FOREIGN KEY(workspace_id,project_id) REFERENCES native_projects(workspace_id,id),
 CHECK((convert_from(manifest_bytes,'UTF8')::jsonb=snapshot->'manifest') IS TRUE),
 CHECK(encode(digest(manifest_bytes,'sha256'),'hex')=manifest_sha256),
 CHECK(snapshot @> jsonb_build_object('id',id,'workspace_id',workspace_id,'project_id',project_id,'created_by',created_by,'recipient_id',recipient_id,'manifest_sha256',manifest_sha256)),
 CHECK((snapshot->>'created_at')::timestamptz IS NOT DISTINCT FROM created_at)
);
CREATE INDEX native_continuity_handoffs_history ON native_continuity_handoffs(workspace_id,project_id,created_at,id);
CREATE FUNCTION protect_native_package_record() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE packet native_delivery_packets%ROWTYPE; assignment native_delivery_profile_assignments%ROWTYPE;
 validation native_delivery_validations%ROWTYPE; reference jsonb; role jsonb; manifest jsonb; pv integer;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('workspace:'||NEW.workspace_id::text,0));
 IF NOT EXISTS(SELECT 1 FROM memberships m WHERE m.workspace_id=NEW.workspace_id AND m.user_id=NEW.created_by AND
  (m.role='owner' OR EXISTS(SELECT 1 FROM native_project_members pm WHERE pm.workspace_id=NEW.workspace_id AND pm.project_id=NEW.project_id AND pm.actor_id=NEW.created_by
   AND (TG_TABLE_NAME='native_continuity_handoffs' OR pm.role='owner')))) THEN
  RAISE EXCEPTION 'package creator lacks current project authority' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='native_continuity_handoffs' THEN
  IF NOT EXISTS(SELECT 1 FROM memberships m WHERE m.workspace_id=NEW.workspace_id AND m.user_id=NEW.recipient_id AND
   (m.role='owner' OR EXISTS(SELECT 1 FROM native_project_members pm WHERE pm.workspace_id=NEW.workspace_id AND pm.project_id=NEW.project_id AND pm.actor_id=NEW.recipient_id))) THEN
   RAISE EXCEPTION 'handoff recipient is outside project scope' USING ERRCODE='23514';
  END IF;
  manifest=NEW.snapshot->'manifest';
  SELECT version INTO pv FROM native_projects WHERE workspace_id=NEW.workspace_id AND id=NEW.project_id;
  IF (manifest->>'schema_version'='1' AND (manifest->>'project_version')::integer=pv AND
   jsonb_typeof(manifest->'artifacts')='array' AND jsonb_typeof(manifest->'runs')='array' AND jsonb_typeof(manifest->'source_revision_ids')='array') IS NOT TRUE THEN
   RAISE EXCEPTION 'handoff structure or project version changed' USING ERRCODE='23514';
  END IF;
  IF jsonb_array_length(manifest->'artifacts')>200 OR jsonb_array_length(manifest->'runs')>100 OR jsonb_array_length(manifest->'source_revision_ids')>2000 OR
   (SELECT count(DISTINCT a->>'revision_id') FROM jsonb_array_elements(manifest->'artifacts') a)<>jsonb_array_length(manifest->'artifacts') OR
   (SELECT count(DISTINCT r->>'run_id') FROM jsonb_array_elements(manifest->'runs') r)<>jsonb_array_length(manifest->'runs') OR
   (SELECT count(DISTINCT value) FROM jsonb_array_elements(manifest->'source_revision_ids'))<>jsonb_array_length(manifest->'source_revision_ids') THEN
   RAISE EXCEPTION 'handoff reference bounds or duplicate identities are invalid' USING ERRCODE='23514';
  END IF;
  FOR reference IN SELECT value FROM jsonb_array_elements(manifest->'artifacts') LOOP
   IF NOT EXISTS(SELECT 1 FROM native_artifact_revisions r WHERE r.workspace_id=NEW.workspace_id AND r.project_id=NEW.project_id AND r.artifact_id=(reference->>'artifact_id')::uuid AND r.id=(reference->>'revision_id')::uuid AND r.sha256=reference->>'sha256') THEN
    RAISE EXCEPTION 'handoff artifact scope or bytes changed' USING ERRCODE='23514';
   END IF;
  END LOOP;
  FOR reference IN SELECT value FROM jsonb_array_elements(manifest->'runs') LOOP
   IF NOT EXISTS(SELECT 1 FROM native_execution_runs r WHERE r.workspace_id=NEW.workspace_id AND r.project_id=NEW.project_id AND r.id=(reference->>'run_id')::uuid AND r.version=(reference->>'version')::integer AND
    (r.snapshot->>'step_number')::integer=(reference->>'step_number')::integer AND (r.snapshot->>'applied_step')::integer=(reference->>'applied_step')::integer) THEN
    RAISE EXCEPTION 'handoff run checkpoint is stale or outside scope' USING ERRCODE='23514';
   END IF;
  END LOOP;
  FOR reference IN SELECT value FROM jsonb_array_elements(manifest->'source_revision_ids') LOOP
   IF NOT EXISTS(SELECT 1 FROM native_intake_sources s WHERE s.workspace_id=NEW.workspace_id AND s.project_id=NEW.project_id AND s.id=(reference#>>'{}')::uuid AND s.revoked_at IS NULL) THEN
    RAISE EXCEPTION 'handoff source is outside scope or revoked' USING ERRCODE='23514';
   END IF;
  END LOOP;
 ELSE
  SELECT * INTO packet FROM native_delivery_packets WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND id=NEW.packet_id;
  IF packet.id IS NULL OR packet.manifest_sha256<>NEW.manifest_sha256 THEN
   RAISE EXCEPTION 'package evidence identifies another scoped manifest' USING ERRCODE='23514';
  END IF;
  SELECT * INTO assignment FROM native_delivery_profile_assignments WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND packet_id=NEW.packet_id ORDER BY version DESC LIMIT 1;
  IF TG_TABLE_NAME='native_delivery_profile_assignments' THEN
   IF NEW.version<>COALESCE(assignment.version,0)+1 OR (jsonb_typeof(NEW.snapshot->'roles')='array' AND
    jsonb_array_length(NEW.snapshot->'roles') BETWEEN 1 AND 20 AND native_dependency_selector_valid(NEW.snapshot->'probe'->'selector')) IS NOT TRUE THEN
    RAISE EXCEPTION 'package profile history or role/probe structure is invalid' USING ERRCODE='23514';
   END IF;
   FOR role IN SELECT value FROM jsonb_array_elements(NEW.snapshot->'roles') LOOP
    IF (role->>'role' IN ('json_specification','markdown_rationale','schema_and_check_report')) IS NOT TRUE OR NOT EXISTS(
     SELECT 1 FROM jsonb_array_elements(packet.snapshot->'manifest'->'components') c WHERE c->>'revision_id'=role->>'revision_id') THEN
     RAISE EXCEPTION 'profile role is outside its packet' USING ERRCODE='23514';
    END IF;
   END LOOP;
  ELSE
   IF assignment.id IS NULL OR assignment.id<>NEW.assignment_id OR assignment.version<>NEW.profile_version THEN
    RAISE EXCEPTION 'package validation profile assignment changed' USING ERRCODE='23514';
   END IF;
   IF TG_TABLE_NAME='native_delivery_validations' THEN
    IF (jsonb_typeof(NEW.snapshot->'checks')='array' AND jsonb_array_length(NEW.snapshot->'checks') BETWEEN 1 AND 32 AND
     jsonb_typeof(NEW.snapshot->'evidence')='object' AND jsonb_typeof(NEW.snapshot->'authority')='object') IS NOT TRUE THEN
     RAISE EXCEPTION 'package validation result structure is invalid' USING ERRCODE='23514';
    END IF;
    IF NEW.snapshot->>'profile_sha256' IS DISTINCT FROM assignment.snapshot->>'profile_sha256' OR
     NEW.snapshot->>'validator_key' IS DISTINCT FROM assignment.snapshot->>'validator_key' OR
     NEW.snapshot->>'validator_version' IS DISTINCT FROM assignment.snapshot->>'validator_version' OR
     NEW.passed IS DISTINCT FROM (NOT EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.snapshot->'checks') c WHERE c->>'status' IS DISTINCT FROM 'passed')) OR
     EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.snapshot->'checks') c WHERE (c->>'status' IN ('passed','failed')) IS NOT TRUE) OR
     (SELECT count(DISTINCT c->>'name') FROM jsonb_array_elements(NEW.snapshot->'checks') c)<>jsonb_array_length(NEW.snapshot->'checks') THEN
     RAISE EXCEPTION 'package check results or validator contradict its profile' USING ERRCODE='23514';
    END IF;
   ELSE
    SELECT * INTO validation FROM native_delivery_validations WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND packet_id=NEW.packet_id AND id=NEW.validation_id;
    IF validation.id IS NULL OR NOT validation.passed OR validation.assignment_id<>assignment.id OR validation.authority_sha256<>NEW.authority_sha256 OR
     (NEW.snapshot->>'decision'='approved' AND jsonb_typeof(NEW.snapshot->'owner_authority')='object') IS NOT TRUE THEN
     RAISE EXCEPTION 'package approval requires passed exact validation evidence' USING ERRCODE='23514';
    END IF;
   END IF;
  END IF;
 END IF;
 RETURN NEW;
END;
$$;
CREATE FUNCTION account_native_package_record() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE kind text; content bytea;
BEGIN
 IF TG_TABLE_NAME='native_continuity_handoffs' THEN kind='continuity'; content=NEW.manifest_bytes;
 ELSE content=NEW.record_bytes;
  IF TG_TABLE_NAME='native_delivery_acceptances' THEN kind='acceptance'; ELSE kind='delivery_validation'; END IF;
 END IF;
 PERFORM retain_native_storage_charge(NEW.workspace_id,NEW.project_id,kind,NEW.id,octet_length(content)::bigint,encode(digest(content,'sha256'),'hex'),NEW.created_at);
 RETURN NEW;
END;
$$;
CREATE TRIGGER native_delivery_profile_assignments_guard BEFORE INSERT ON native_delivery_profile_assignments FOR EACH ROW EXECUTE FUNCTION protect_native_package_record();
CREATE TRIGGER native_delivery_profile_assignments_immutable BEFORE UPDATE OR DELETE ON native_delivery_profile_assignments FOR EACH ROW EXECUTE FUNCTION reject_artifact_rewrite();
CREATE TRIGGER native_delivery_profile_assignments_storage AFTER INSERT ON native_delivery_profile_assignments FOR EACH ROW EXECUTE FUNCTION account_native_package_record();
CREATE TRIGGER native_delivery_validations_guard BEFORE INSERT ON native_delivery_validations FOR EACH ROW EXECUTE FUNCTION protect_native_package_record();
CREATE TRIGGER native_delivery_validations_immutable BEFORE UPDATE OR DELETE ON native_delivery_validations FOR EACH ROW EXECUTE FUNCTION reject_artifact_rewrite();
CREATE TRIGGER native_delivery_validations_storage AFTER INSERT ON native_delivery_validations FOR EACH ROW EXECUTE FUNCTION account_native_package_record();
CREATE TRIGGER native_delivery_acceptances_guard BEFORE INSERT ON native_delivery_acceptances FOR EACH ROW EXECUTE FUNCTION protect_native_package_record();
CREATE TRIGGER native_delivery_acceptances_immutable BEFORE UPDATE OR DELETE ON native_delivery_acceptances FOR EACH ROW EXECUTE FUNCTION reject_artifact_rewrite();
CREATE TRIGGER native_delivery_acceptances_storage AFTER INSERT ON native_delivery_acceptances FOR EACH ROW EXECUTE FUNCTION account_native_package_record();
CREATE TRIGGER native_continuity_handoffs_guard BEFORE INSERT ON native_continuity_handoffs FOR EACH ROW EXECUTE FUNCTION protect_native_package_record();
CREATE TRIGGER native_continuity_handoffs_immutable BEFORE UPDATE OR DELETE ON native_continuity_handoffs FOR EACH ROW EXECUTE FUNCTION reject_artifact_rewrite();
CREATE TRIGGER native_continuity_handoffs_storage AFTER INSERT ON native_continuity_handoffs FOR EACH ROW EXECUTE FUNCTION account_native_package_record();

