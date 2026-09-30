# Identity: run and test

For the active `cam40419` localhost installation, sign in with the existing password and follow
[local operations](local-operations.md). The development launch instructions below are for a
separate test session; the running local service has development-token login disabled.

## First passkey

Start Docker Desktop. In PowerShell, from the repository root:

```powershell
.\venv\Scripts\python.exe -m pip install -e ".[dev,postgres]"
.\scripts\start-dev.ps1 -Enroll
```

The launcher starts PostgreSQL, runs pending migrations, seeds the development membership,
prints a one-use enrollment token, and starts the API. It changes process environment variables,
not your `.env`. The default database URL matches the Compose default password; for a custom
database use `-DatabaseUrl 'YOUR_CONNECTION_URL'`.

Open **http://localhost:8000/login**. Expand **Set up a passkey**, paste the enrollment token,
and choose **Create a passkey**. Complete your browser/device verification prompt. Then try
**Test connection**, **Sign out**, and **Sign in with a passkey**.

Use `localhost` consistently. `127.0.0.1` is a different origin and is not an alias for the
configured relying party. A passkey needs a supported browser and authenticator (for example
Windows Hello or a security key). This implementation requests discoverable credentials and
user verification. For later starts, run `.\scripts\start-dev.ps1` without issuing another
enrollment token. Stop the API with Ctrl+C; persistent passkeys and unexpired sessions survive
the restart.

## Development login without a passkey

```powershell
.\scripts\start-dev.ps1 -DevelopmentLogin
```

Open the same login page, expand **Local development login**, and enter the development token
printed in the terminal. This flow uses the same sessions, CSRF checks, and server-side scopes.
It is available only when explicitly enabled and is refused in production. Restarting without
the flag disables both development login and previously issued development sessions.

For a database-free smoke test use `.\scripts\start-dev.ps1 -Memory`. It enables the temporary
development login; all state disappears on restart. The normal launcher uses PostgreSQL.

## API testing

The login page includes an authenticated echo test. For other requests, `/docs` provides the
API schema. Sign in on the same origin first. Get `/auth/session` to obtain `csrf_token`, and
supply it as `X-CSRF-Token` on protected POST requests. Browser POSTs supply Origin automatically.
Neither the actor nor scopes can be supplied through headers anymore.

For PowerShell automation, start with `-DevelopmentLogin` and run in a second terminal:

```powershell
$token = Read-Host 'Development token from the server terminal'
$login = Invoke-RestMethod -Uri 'http://localhost:8000/auth/dev-login' `
    -Method Post -SessionVariable simonSession -ContentType 'application/json' `
    -Headers @{ Origin = 'http://localhost:8000' } `
    -Body (@{ token = $token } | ConvertTo-Json)
$token = $null
$headers = @{ Origin = 'http://localhost:8000'; 'X-CSRF-Token' = $login.csrf_token }
$body = @{ capability = 'system.echo'; arguments = @{ message = 'Authenticated hello' }; `
    idempotency_key = [guid]::NewGuid().ToString() } | ConvertTo-Json
Invoke-RestMethod -Uri 'http://localhost:8000/v1/capabilities/invoke' `
    -Method Post -WebSession $simonSession -Headers $headers `
    -ContentType 'application/json' -Body $body
Invoke-RestMethod -Uri 'http://localhost:8000/auth/logout' `
    -Method Post -WebSession $simonSession -Headers $headers
```

Expected failures: missing session is 401; wrong/missing Origin or CSRF token is 403;
invalid request fields are 422 without reflected input. A revoked or expired session is 401.
Changing `X-Actor-Id`, `X-Household-Id`, or `X-Scopes` cannot change your identity. Use
`POST /auth/household` to select a household you actually belong to; it rotates the session
and returns a new CSRF token. Owner/member roles can read and submit jobs; guests can only echo.

## Local operator commands

In a separate terminal, configure the same database:

```powershell
$env:SIMON_STORAGE_BACKEND = 'postgres'
$env:SIMON_DATABASE_URL = 'postgresql://jarvis:local-development-only@127.0.0.1:5432/jarvis'
.\venv\Scripts\python.exe -m simon.identity_admin --help
```

The commands are `membership`, `remove-membership`, `enroll`, `password-recovery`,
`revoke-sessions`, and `revoke-passkey`. Membership creation/update takes `--actor-id`, `--household-id`, and
`--role owner|member|guest`, plus optional display and household names. These are privileged
local operations; no unauthenticated code issuance API is exposed. Existing display/household
names are preserved when updating a role. Removing a membership revokes all sessions for that
user; other memberships remain available on the next valid passkey login.

To issue another enrollment token for the seeded user:

```powershell
.\venv\Scripts\python.exe -m simon.identity_admin enroll `
    --actor-id 11111111-1111-4111-8111-111111111111 `
    --household-id 22222222-2222-4222-8222-222222222222
```

The enrollment token expires in 15 minutes and can successfully enroll only one credential.
Keep the token in the local terminal and enrollment form; it is not placed in a URL or logged.
Revoke a lost credential with `revoke-passkey --credential-id ID`; this also revokes that user's
sessions. Local database access is the recovery authority. Session revocation alone leaves
passkeys usable for a new login.

## Password recovery

On localhost, a signed-in user can change their username and password under **Username and
password**. A password session requires the current password; a passkey session can set a new
password without it.

For a lost password on an invited account, the site administrator can select **Issue password
recovery code** in Accounts and share the code privately. The account holder opens **Forgot your
password?** on the login page, enters the code, their existing username, and a new password of
at least 15 characters. The code lasts 15 minutes and works once. Completing the reset revokes
the account's other sessions and signs in the account holder.

For the site administrator's own account, a local operator with database access can issue a code:

```powershell
.\venv\Scripts\python.exe -m simon.identity_admin password-recovery `
    --actor-id YOUR_ACTOR_ID --household-id YOUR_HOUSEHOLD_ID
```

Recovery codes use the enrollment mechanism, so they also authorize passkey setup. Treat them
as full account recovery secrets and share them only with the account holder. Password sign-in
and reset remain available only on localhost.

## Verification

The normal suite tests actual WebAuthn cryptographic verification with generated test keys,
against both storage adapters. It covers incorrect origins/challenges/signatures/user handles,
missing user verification, stale counters, expired/replayed challenges, browser binding,
concurrent replay, CSRF, token hashing, session rotation/expiry/revocation, membership changes,
and administrative commands. No real personal passkey is used by these tests.

```powershell
$env:SIMON_TEST_DATABASE_URL = 'postgresql://jarvis:local-development-only@127.0.0.1:5432/jarvis_test'
.\venv\Scripts\python.exe -m pytest --cov=simon --cov-report=term-missing
```

Create `simon_test` once if needed, as described in the persistence runbook. For the complete
browser ceremony test (isolated profile and a virtual authenticator):

```powershell
.\venv\Scripts\python.exe -m pip install -e ".[dev,postgres,browser]"
.\venv\Scripts\python.exe -m playwright install chromium
$env:SIMON_BROWSER_TESTS = '1'
.\venv\Scripts\python.exe -m pytest -q tests/integration/test_browser_identity.py
```

To use an installed Microsoft Edge instead of downloading Chromium, set
`$env:SIMON_BROWSER_CHANNEL = 'msedge'` and skip the browser installation command.
The automated browser test verified enrollment, passkey login, authenticated echo, reload,
and logout locally with headless Edge. Testing your physical authenticator requires the
interactive prompt described above.

See [ADR 0005](../decisions/0005-passkey-sessions.md) for security decisions and remaining
operational work. Threads/runs and the chat interface are the next implementation milestone.

Latest local verification: 108 tests passed, including the Edge browser test, with 96.93%
branch-inclusive coverage. Ruff and strict mypy passed. The identity migration applied to the
existing development database, and a subsequent backup/restore compared all 19 public tables.

## Private account invitations

The configured site administrator can open the login/account page and use Accounts to create
a private workspace for an invited person. Share its one-use code and the login URL privately.
The recipient chooses Set up a passkey. Codes expire after 15 minutes; Renew invitation invalidates
the old code. The web UI cannot issue enrollment for an already registered account.

Disable account revokes sessions and blocks further sign-in without deleting data or passkeys.
Enable account permits a new sign-in; an unregistered account needs a renewed invitation.
The administrator is identified by SIMON_ACCOUNT_ADMIN_ACTOR_ID, separately from workspace
ownership. The production launcher sets it from -ActorId. Existing shared memberships are unchanged.
See [account boundaries and workshop plan](../accounts-workshop-plan.md) for remaining sharing
and per-account billing work. Invited accounts currently use the server's OpenAI billing.
