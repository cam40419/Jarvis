-- Existing identities and home membership remain intact. New invitations create
-- their own workspace using the existing household isolation boundary.
CREATE TABLE managed_accounts (
    actor_id uuid PRIMARY KEY REFERENCES users(id),
    household_id uuid NOT NULL UNIQUE REFERENCES households(id),
    invited_by uuid NOT NULL REFERENCES users(id),
    snapshot jsonb NOT NULL
);
