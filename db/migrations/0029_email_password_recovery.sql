ALTER TABLE auth_passwords ADD COLUMN email text;
CREATE UNIQUE INDEX auth_passwords_verified_email ON auth_passwords (email) WHERE email IS NOT NULL;

CREATE TABLE auth_email_codes (
    id uuid PRIMARY KEY,
    actor_id uuid NOT NULL REFERENCES users(id),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    email text NOT NULL,
    purpose text NOT NULL CHECK (purpose IN ('verify', 'reset')),
    code_hash text NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
    consumed boolean NOT NULL DEFAULT false
);
CREATE INDEX auth_email_codes_address_time ON auth_email_codes(email, purpose, created_at DESC);
CREATE INDEX auth_email_codes_actor_time ON auth_email_codes(actor_id, purpose, created_at DESC);
CREATE INDEX auth_email_codes_expiry ON auth_email_codes(expires_at);
