ALTER TABLE auth_sessions DROP CONSTRAINT auth_sessions_method_check;
ALTER TABLE auth_sessions ADD CONSTRAINT auth_sessions_method_check
    CHECK (method IN ('passkey', 'development', 'password'));

CREATE TABLE auth_passwords (
    actor_id uuid PRIMARY KEY REFERENCES users(id),
    username text NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9][a-z0-9._-]{2,31}$'),
    password_hash text NOT NULL,
    failed_attempts integer NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    locked_until timestamptz
);
