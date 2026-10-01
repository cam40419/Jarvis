# Remote access to the local server

The server keeps PostgreSQL, managed files and agent workspaces locally. Remote
browsers use the same authenticated Simon pages and APIs, including uploads,
previews, artifact downloads and run progress. Cloud storage is optional.

Use one HTTPS origin for the UI and API first. The older split-frontend proposal
in the voice runbook is historical and is not required for this deployment.

## Private access from your devices

Install Tailscale on the server and client devices, sign them into the intended
network, and configure access to the server for your users/devices. Account login
and the actual hostname are operator setup; this repository cannot supply them.
Use Tailscale Serve (private to the tailnet), not a public Funnel, for this mode.
See [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve).

After obtaining the server's exact HTTPS hostname, preview a configuration plan:

```powershell
.\venv\Scripts\python.exe -m simon.https_setup --provider tailscale `
    --origin https://YOUR-SERVER.YOUR-TAILNET.ts.net
```

The tool requires an exact DNS HTTPS origin and configures production, PostgreSQL,
secure cookies, passkey RP ID and disabled development login together. It retains
existing database/account/Google/agent settings. `--public-path /simon` is optional;
use the same prefix consistently on every hop. Serve forwards the original path.

## Apply either HTTPS plan

Back up database and files first. Let active tasks finish, then use the documented
[maintenance and graceful-stop sequence](dispatcher-lifecycle.md). With API and
workers stopped and `.local/maintenance.request` present, repeat the chosen plan
command with `--apply`. No command here creates public DNS or signs into a provider.
The tool saves the previous `.env`, proxy and plan in `.local/https-backups/`, stages
new files, and restores replaced files if an apply fails. These private backups
contain secrets and need the same protection as `.env`.

```powershell
.\scripts\start-configured-server.ps1 -Check
.\venv\Scripts\python.exe -m simon.https_setup --check
.\scripts\install-https-tasks.ps1
# End maintenance only after checking settings, then restart the existing tasks.
Remove-Item -LiteralPath .local/maintenance.request
.\scripts\resume-local.ps1
# For Tailscale, after install/login/HTTPS enablement, in an administrator terminal:
tailscale serve --bg --https=443 http://127.0.0.1:8000
tailscale serve status
```

Use the HTTPS hostname on both the server and remote clients. `start-local.ps1`
intentionally overrides the origin to localhost, so do not use that launcher for
remote production. `install-https-tasks.ps1` replaces the existing `Simon-Local`
action with the configured launcher and registers the selected Cloudflare tunnel
when appropriate. It refuses to replace a running API task. Do not run the legacy
`install-local-tasks.ps1` afterward; it would restore the localhost launcher.
These Windows tasks run after this user signs in, not before sign-in. The launcher
retains loopback-only listeners and the existing graceful-stop behavior. Recovery
probes loopback with the configured HTTPS hostname and prefix.

Google's OAuth client needs the exact new HTTPS callback URL shown in Connections.
Existing localhost passkeys are bound to localhost; enroll a passkey for the new
hostname using the identity runbook. Existing accounts and password credentials
remain in the database. HTTPS enables browser microphone use, subject to client
permissions and the voice runbook's foreground-tab limitations.

## Public website access

Use an existing named Cloudflare Tunnel and the matching private credentials JSON:

```powershell
.\venv\Scripts\python.exe -m simon.https_setup --provider cloudflare `
    --origin https://simon.example.com --tunnel-id EXISTING-TUNNEL-UUID `
    --credentials-file C:\PRIVATE\EXISTING-TUNNEL-UUID.json
```

The generated ingress accepts only the chosen hostname and optional path prefix,
uses the same Host header toward `127.0.0.1:8000`, and rejects other routes with 404.
After reviewing the plan, apply it during maintenance as above. Configure that exact
hostname's DNS to the named tunnel in the domain account, then start `Simon-Tunnel`.
The launcher validates the saved plan/settings, validates cloudflared ingress and
logs privately to `.local/logs/cloudflared.log`. It never puts tunnel secrets on
the command line. `stop-tunnel.ps1` leaves a stop marker respected by recovery.
Tailscale configurations do not start the old Cloudflare task.
[Cloudflare named-tunnel configuration](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/)

Keep PostgreSQL, Docker, working directories and backend listeners private. Files
remain behind account authorization. The older portfolio rewrite deployment is
historical: its separate origin hostname and rewritten Host header differ from
this single-origin setup. Do not reactivate its old YAML unchanged.

HTTPS responses use HSTS without extending policy to unrelated subdomains. The
server ignores forwarded identity/origin headers and builds redirects from its
configured origin. Request sizes and authentication attempts are bounded in the
application. With a loopback tunnel all clients share the server's source bucket;
add per-client throttling at the authenticated edge for a public deployment. See
the ingress settings in `.env.example`. An edge limit supplements account/passkey
authorization; it cannot replace it.

To roll back, stop the proxy and all writers under maintenance, restore `server.env`
to `.env` and the previous saved proxy/plan files. Remove the new plan if there was
none before. Restore the prior API task action and leave the remote tunnel stopped
before starting the localhost deployment. Retain the backup until the new origin's
login, file access and restart checks pass.

## Acceptance checks

From a phone on mobile data, verify login/logout, an account-private file upload,
image/PDF preview, download, agent run status/cancel, and a completed artifact
download. A second account must not see those private files or runs. Exercise
voice and a Google reconnect on the final origin. Restart the API/dispatcher and
verify that queued work and saved outputs remain visible. Record server shutdown,
network loss and reconnect behavior before calling the remote deployment ready.

Back up both database and files as described in [storage recovery](storage-recovery.md).
