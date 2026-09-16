BEGIN;
CREATE TABLE home_devices (
    household_id uuid NOT NULL REFERENCES households(id),
    id text NOT NULL,
    snapshot jsonb NOT NULL,
    PRIMARY KEY (household_id, id)
);
CREATE TABLE home_syncs (
    household_id uuid NOT NULL REFERENCES households(id),
    provider text NOT NULL CHECK (provider IN ('lifx', 'tuya')),
    snapshot jsonb NOT NULL,
    PRIMARY KEY (household_id, provider)
);
COMMIT;
