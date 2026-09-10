BEGIN;
DROP TABLE IF EXISTS outbox_events, audit_events, confirmations, job_events, jobs, idempotency_records, capabilities, memories, runs, messages, threads, memberships, users, households;
DROP TYPE IF EXISTS job_status;
DROP TYPE IF EXISTS risk_class;
COMMIT;
