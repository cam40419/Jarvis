ALTER TABLE messages ADD COLUMN sequence bigint;
WITH numbered AS (
    SELECT id, row_number() OVER (PARTITION BY thread_id ORDER BY created_at, id) AS n
    FROM messages
)
UPDATE messages SET sequence = numbered.n FROM numbered WHERE messages.id = numbered.id;
ALTER TABLE messages ALTER COLUMN sequence SET NOT NULL;
ALTER TABLE messages ADD CONSTRAINT messages_sequence_positive CHECK (sequence > 0);
ALTER TABLE messages ADD CONSTRAINT messages_thread_sequence_unique UNIQUE (thread_id, sequence);
ALTER TABLE runs ADD COLUMN snapshot jsonb;
CREATE INDEX threads_household_created_idx ON threads(household_id, created_at, id);
CREATE TABLE run_events (
    run_id uuid NOT NULL REFERENCES runs(id),
    sequence integer NOT NULL CHECK (sequence > 0),
    event_type text NOT NULL,
    message_id uuid REFERENCES messages(id),
    PRIMARY KEY (run_id, sequence)
);
CREATE FUNCTION reject_conversation_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'conversation records are append-only';
END;
$$;
CREATE TRIGGER messages_immutable BEFORE UPDATE OR DELETE ON messages
FOR EACH ROW EXECUTE FUNCTION reject_conversation_rewrite();
CREATE TRIGGER run_events_immutable BEFORE UPDATE OR DELETE ON run_events
FOR EACH ROW EXECUTE FUNCTION reject_conversation_rewrite();
CREATE TRIGGER completed_runs_immutable BEFORE UPDATE OR DELETE ON runs
FOR EACH ROW WHEN (OLD.snapshot IS NOT NULL) EXECUTE FUNCTION reject_conversation_rewrite();
