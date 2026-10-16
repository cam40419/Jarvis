-- Existing authority remains inspectable/revocable but cannot execute in a
-- managed installation until explicitly reissued in its current external epoch.
UPDATE model_usage SET snapshot = snapshot || '{"recovery_epoch":null}'::jsonb
WHERE NOT snapshot ? 'recovery_epoch';
UPDATE native_execution_policies SET snapshot = snapshot || '{"recovery_epoch":null}'::jsonb
WHERE NOT snapshot ? 'recovery_epoch';
UPDATE native_execution_runners SET snapshot = snapshot || '{"recovery_epoch":null}'::jsonb
WHERE NOT snapshot ? 'recovery_epoch';
UPDATE native_execution_schedules SET snapshot = snapshot || '{"recovery_epoch":null}'::jsonb
WHERE NOT snapshot ? 'recovery_epoch';
UPDATE native_execution_runs SET snapshot = jsonb_set(
    snapshot, '{bounds,recovery_epoch}', 'null'::jsonb
) WHERE snapshot ? 'bounds' AND NOT (snapshot->'bounds') ? 'recovery_epoch';

CREATE FUNCTION simon_guard_recovery_epoch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.snapshot->'recovery_epoch' IS DISTINCT FROM NEW.snapshot->'recovery_epoch' THEN
        RAISE EXCEPTION 'Recovery authority is immutable; create a new enrollment or operation'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER model_usage_recovery_epoch BEFORE UPDATE ON model_usage
FOR EACH ROW EXECUTE FUNCTION simon_guard_recovery_epoch();
CREATE TRIGGER execution_runner_recovery_epoch BEFORE UPDATE ON native_execution_runners
FOR EACH ROW EXECUTE FUNCTION simon_guard_recovery_epoch();
