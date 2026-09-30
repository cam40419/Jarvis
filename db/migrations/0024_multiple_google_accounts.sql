ALTER TABLE google_connections ADD COLUMN email text;
UPDATE google_connections SET email = lower(snapshot->>'email'),
    snapshot = snapshot || '{"is_default":true}'::jsonb;
ALTER TABLE google_connections ALTER COLUMN email SET NOT NULL;
ALTER TABLE google_connections DROP CONSTRAINT google_connections_pkey;
ALTER TABLE google_connections ADD PRIMARY KEY (household_id, actor_id, email);
