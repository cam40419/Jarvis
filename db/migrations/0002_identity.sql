CREATE TABLE auth_sessions (
    token_hash char(64) PRIMARY KEY,
    actor_id uuid NOT NULL REFERENCES users(id),
    household_id uuid NOT NULL REFERENCES households(id),
    method text NOT NULL CHECK (method IN ('passkey', 'development')),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > created_at)
);

CREATE INDEX auth_sessions_actor_idx ON auth_sessions(actor_id);

CREATE INDEX auth_sessions_expiry_idx ON auth_sessions(expires_at);

CREATE TABLE auth_enrollments (
    token_hash char(64) PRIMARY KEY,
    actor_id uuid NOT NULL REFERENCES users(id),
    household_id uuid NOT NULL REFERENCES households(id),
    expires_at timestamptz NOT NULL
);

CREATE TABLE auth_challenges (
    token_hash char(64) PRIMARY KEY,
    binding_hash char(64) NOT NULL,
    challenge text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('registration', 'authentication')),
    actor_id uuid REFERENCES users(id),
    household_id uuid REFERENCES households(id),
    enrollment_hash char(64),
    expires_at timestamptz NOT NULL,
    CHECK (kind != 'registration' OR
        (actor_id IS NOT NULL AND household_id IS NOT NULL AND enrollment_hash IS NOT NULL))
);

CREATE INDEX auth_challenges_expiry_idx ON auth_challenges(expires_at);

CREATE TABLE auth_passkeys (
    credential_id text PRIMARY KEY,
    actor_id uuid NOT NULL REFERENCES users(id),
    public_key text NOT NULL,
    sign_count bigint NOT NULL CHECK (sign_count >= 0),
    device_type text NOT NULL CHECK (device_type IN ('single_device', 'multi_device')),
    backed_up boolean NOT NULL
);

CREATE INDEX auth_passkeys_actor_idx ON auth_passkeys(actor_id);
