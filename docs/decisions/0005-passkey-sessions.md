# ADR 0005: Passkeys and server-side sessions

- Status: accepted for the initial identity implementation
- Date: 2026-09-10

## Decision

Replace the header identity adapter entirely. API callers authenticate with an opaque session
cookie. Actor, household, and scopes are resolved on the server; `X-Actor-Id`, `X-Household-Id`,
and `X-Scopes` have no authority.

Use py_webauthn to generate and verify WebAuthn ceremonies. The relying-party ID must exactly
match the configured public origin's hostname. Discoverable credentials and user verification
are required. Verification checks the challenge, RP ID, exact origin, signature, user handle,
and authenticator counter. Cross-origin/embedded ceremonies are rejected explicitly. Synced
passkeys with a zero counter are supported by the library's counter rules; backup eligibility
cannot change between registration and authentication.

The first passkey and additional/recovery passkeys require a one-use enrollment token issued
by a local operator with database access. It is bound to an existing user and household and
expires after 15 minutes. There is no open registration, email sender, password login, or
public recovery endpoint. The OS/database operator remains a root-of-trust boundary.

Ceremonies expire after five minutes and are bound to a random HttpOnly browser cookie.
The challenge is consumed atomically before cryptographic verification, including failed
attempts. A failed registration leaves its enrollment token available for a fresh ceremony;
successful registration consumes it in the same transaction as credential and session creation.

Session cookies contain 256 bits of random entropy. Only SHA-256 token hashes are stored.
Sessions have an absolute eight-hour lifetime by default (configurable from one to 24 hours).
Login and household switching rotate session and CSRF tokens. Switching cannot extend the
original expiry. Logout deletes the session; local revocation tools remove sessions and
credentials. Membership removal revokes the user's sessions. Memberships and roles are
reloaded on each protected request, rather than copied into long-lived client claims.

Cookies use HttpOnly, SameSite=Strict, Path=/, no Domain, and Secure on HTTPS. HTTPS cookie
names use the `__Host-` prefix. State-changing authenticated requests require both the exact
Origin header and a session-bound CSRF token. The token is derived using HMAC from the session
secret and returned by `/auth/session`; it is never stored in localStorage. Login and ceremony
endpoints require the exact Origin, with enrollment proof or WebAuthn proof for authentication.
No cross-origin credentialed CORS access is enabled. Host validation prevents DNS rebinding
through arbitrary hostnames. Validation errors omit submitted input to avoid reflecting secrets.

The optional development login is disabled by default, requires an explicit random token,
works only with a loopback public origin, and always selects the seeded development user.
Production configuration rejects development login, HTTP origins, and memory storage.

## Initial role mapping

| Role | Scopes |
| --- | --- |
| Owner | `system:read`, `jobs:read`, `jobs:write`, `identity:manage` |
| Member | `system:read`, `jobs:read`, `jobs:write` |
| Guest | `system:read` |

`identity:manage` reserves future authenticated administration. Current membership, enrollment,
and recovery commands require local operator/database access. Unknown roles are rejected by
the schema. No role enables hard or dangerous capability writes.

## Consequences and limits

One PostgreSQL advisory lock serializes initial identity mutations and counter checks. The
reference adapter uses its transaction lock. Audit and state changes share the transaction;
local operator events use a separate reserved actor UUID and an explicit authority field.
The login page is a small testable identity surface, not the planned PWA/chat interface.

Deployment still needs TLS/proxy configuration, rate limits, request-size limits, idle-session
policy, device/session management UI, off-machine backups, and an operational security review.
Configuration checks are not a claim that the whole assistant is production-ready. Physical
device confirmation is a separate action-bound protocol; signing in with a passkey does not
authorize a dangerous action.

## References

- [py_webauthn registration](https://duo-labs.github.io/py_webauthn/registration.html)
- [py_webauthn authentication](https://duo-labs.github.io/py_webauthn/authentication.html)
- [WebAuthn specification](https://www.w3.org/TR/webauthn-3/)
- [OWASP CSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
