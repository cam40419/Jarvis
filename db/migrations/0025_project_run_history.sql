-- Include previously stored project plans/runs without rewriting historical jobs.
CREATE INDEX jobs_project_plan_lookup ON jobs (
    household_id, created_by, (input->'plan'->>'project_id'), (id::text)
) WHERE kind = 'platform.plan';

CREATE INDEX jobs_project_run_lookup ON jobs (
    household_id, created_by, (input->>'plan_id'), created_at DESC, id DESC
) WHERE kind = 'platform.run';
