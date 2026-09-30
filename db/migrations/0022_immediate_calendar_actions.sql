-- Calendar writes persist their claim before the model has finished its answer.
-- Keep a real foreign key to either the completed run or its durable attempt.
ALTER TABLE action_proposals ALTER COLUMN run_id DROP NOT NULL;
ALTER TABLE action_proposals ADD COLUMN attempt_id uuid REFERENCES model_attempts(id);
ALTER TABLE action_proposals ADD CONSTRAINT action_proposals_run_or_attempt
    CHECK (num_nonnulls(run_id, attempt_id) = 1);
CREATE INDEX action_proposals_attempt ON action_proposals(attempt_id);
