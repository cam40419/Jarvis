ALTER TABLE home_syncs DROP CONSTRAINT home_syncs_provider_check;
ALTER TABLE home_syncs ADD CONSTRAINT home_syncs_provider_check
    CHECK (provider IN ('lifx', 'tuya', 'shelly'));
ALTER TABLE home_commands ALTER COLUMN thread_id DROP NOT NULL;
ALTER TABLE home_commands ALTER COLUMN run_id DROP NOT NULL;

CREATE TABLE power_samples (
    household_id uuid NOT NULL REFERENCES households(id),
    device_id text NOT NULL,
    captured_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL,
    PRIMARY KEY (household_id, device_id, captured_at)
);
CREATE INDEX power_samples_retention ON power_samples(household_id, captured_at);
