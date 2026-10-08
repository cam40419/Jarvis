# Identity: run and test

Use [local operation](local-operations.md) for startup. These instructions do not
assert the state or identity of any existing installation.

## Username and password

Start Docker Desktop. In PowerShell, from the repository root:

```powershell
.\venv\Scripts\python.exe -m pip install -e ".[dev,postgres]"
.\scripts\start-dev.ps1 -Enroll
```

The launcher starts PostgreSQL, runs pending migrations, seeds the development membership,
prints a one-use enrollment token, and starts the API. It changes process environment variables,
not your `.env`. The default database URL matches the Compose default password; for a custom
database use `-DatabaseUrl 'YOUR_CONNECTION_URL'`.

Open **http://localhost:8000/login**. Expand **Set up username and password**, paste the
enrollment token, and choose your username and a password of at least 15 characters.
Then try **Test connection**, **Sign out**, and **Sign in**.

Use `localhost` consistently. `127.0.0.1` is a different origin and is not an alias for the
configured origin. Password sign-in supports HTTPS hosting and loopback development. Passkey
ceremonies remain available through the tested API; the current login UI uses passwords.
For later starts, run `.\scripts\start-dev.ps1` without issuing another enrollment token.
Stop the API with Ctrl+C; accounts and unexpired sessions survive the restart.

## Email verification and password recovery

The site administrator opens **Connections**, selects **Account email delivery**, and chooses
**Resend** or **SMTP**. Enter the verified sender address and the Resend API key, or the SMTP
server, username and password. SMTP uses verified TLS on port 587 (STARTTLS) or 465 (implicit TLS).
Save the connection. Provider credentials are encrypted and saved in the backend; users do not
edit configuration files. Resend requires a sender domain verified with that provider.

While signed in, open **Recovery email** on the login/account page. Enter your email and current
password, request the verification email, then submit its eight-digit code. Only verified email
addresses are eligible for recovery, and each address can belong to only one account.

Under **Forgot your password?**, enter that verified address and request a code. Enter the emailed
code and your new password twice. Codes expire after ten minutes, allow at most five guesses,
and cannot be reused. Requests have a one-minute cooldown and a five-per-hour limit per address
or account. The public response does not reveal whether an account exists. A successful reset
revokes previous sessions, sends a notification, and requires signing in with the new password.
An administrator can use the operator recovery command for accounts that have not yet verified
their email. No real email is delivered until the provider is configured.

## Explicit development login

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
Actor identity and scopes are resolved on the server. Human-only routes reject bearer credentials.

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
Changing `X-Actor-Id`, `X-Workspace-Id`, or `X-Scopes` cannot change your identity. Use
`POST /auth/workspace` to select a workspace you actually belong to; it rotates the session
and returns a new CSRF token. Owner/member roles can read and submit jobs. Guests can
read native projects explicitly shared with them, but cannot edit boards or use chat.

## Local operator commands

In a separate terminal, configure the same database:

```powershell
$env:SIMON_STORAGE_BACKEND = 'postgres'
$env:SIMON_DATABASE_URL = 'postgresql://jarvis:local-development-only@127.0.0.1:5432/jarvis'
.\venv\Scripts\python.exe -m simon.identity_admin --help
```

The commands are `membership`, `remove-membership`, `enroll`, `password-recovery`,
`revoke-sessions`, and `revoke-passkey`. Membership creation/update takes `--actor-id`, `--workspace-id`, and
`--role owner|member|guest`, plus optional display and workspace names. These are privileged
local operations; no unauthenticated code issuance API is exposed. Existing display/workspace
names are preserved when updating a role. Removing a membership revokes all sessions for that
user; other memberships remain available on the next valid login.

To issue another enrollment token for the seeded user:

```powershell
.\venv\Scripts\python.exe -m simon.identity_admin enroll `
    --actor-id 11111111-1111-4111-8111-111111111111 `
    --workspace-id 22222222-2222-4222-8222-222222222222
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
    --actor-id YOUR_ACTOR_ID --workspace-id YOUR_WORKSPACE_ID
```

Recovery codes use the enrollment mechanism, so they also authorize passkey setup. Treat them
as full account recovery secrets and share them only with the account holder. Password sign-in
and reset require HTTPS or a loopback development origin.

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
Browser tests use isolated profiles and generated authenticators. They do not exercise
your physical authenticator or enroll credentials in a real account. See
[database testing](database-testing.md) for the isolated database workflow.

## Private account invitations

The configured site administrator can open the login/account page and use Accounts to create
a private workspace for an invited person. Share its one-use code and the login URL privately.
The recipient chooses Set up username and password. Codes expire after 15 minutes; Renew invitation invalidates
the old code. The web UI cannot issue enrollment for an already registered account.

Disable account revokes sessions and blocks further sign-in without deleting data or passkeys.
Enable account permits a new sign-in; an unregistered account needs a renewed invitation.
The administrator is identified by SIMON_ACCOUNT_ADMIN_ACTOR_ID, separately from workspace
ownership. The production launcher sets it from -ActorId. Existing shared memberships are unchanged.
See the [platform plan](../architecture/autonomous-work-platform-plan.md) for the
remaining per-project model configuration and billing controls. Configured paid model
requests currently use the server's provider connection.
