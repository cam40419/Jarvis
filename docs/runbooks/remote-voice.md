# Simon local-first and remote access

Simon and PostgreSQL run on the home PC. Use **http://localhost:8000/login** on that PC for the
main application. This direct route avoids the Vercel and tunnel hops. The previous website route
forwarded both pages and API requests through those services; it is currently paused, and the
tunnel is stopped.

For future remote access, the website should serve only the browser interface and forward its
authenticated API requests to Simon on this PC. The PC remains responsible for conversations,
models, memory, devices, and data. That split needs explicit remote-origin authentication,
passkeys, cookies, streaming, and voice handling before it is enabled. The existing `/simon`
rewrites and single-origin server configuration do not yet provide that split.
Username/password sign-in is now available for localhost. Before exposing it through a remote
API route, add remote-origin cookie and CSRF handling, route-specific request throttling, a larger
breached-password blocklist, and end-to-end browser tests of authentication and streaming.

The sections below record the previous remote deployment and its setup. They are not instructions
for the currently running local server.

Previously deployed September 16, 2026: the home origin, named Cloudflare tunnel, and sibling
portfolio's Vercel routes. The tunnel is now stopped. The apex redirected to
`www.camrobbins.com`, which was the configured login origin. Phone passkey enrollment and a human
voice test were pending at the time. Production deployment:
`portfolio-nv2104jtc-cam40419-1783s-projects.vercel.app`.

At deployment, the portfolio homepage and About returned 200; Simon login and health returned 200
through the public domain; private device reads returned 401 without a session; development login
was off; Simon responses disabled caching. The targeted Simon API suite passed 11 tests and the portfolio
suite passed four. Route mapping checks preserve portfolio/static paths. The deployment was made
from the local portfolio checkout; its route changes still need to be committed and pushed before
future Git-triggered deployments so they are not lost.

## Try locally first

```powershell
Start-ScheduledTask -TaskName Simon-Local
```

Open `http://localhost:8000/login`, sign in as cam40419 with the existing password, and open chat. Choose **Talk to Simon → Start conversation**, allow the microphone, and say hello. Interrupt while Simon is speaking. Try a current-facts question and inspect **View conversation & approvals** for its task result. **Mute** disables microphone input; **Stop task** cancels pending backend work; **End call** ends the voice session. An already dispatched device change cannot be undone by cancelling it.

Voice uses `SIMON_OPENAI_API_KEY` with access to `gpt-live-1`. Configuration:

```dotenv
SIMON_VOICE_ENABLED=true
SIMON_VOICE_MODEL=gpt-live-1
SIMON_VOICE_NAME=cedar
SIMON_VOICE_MAX_SECONDS=900
```

The live model handles speech and delegates tasks to Simon's existing Auto model routing and tools. Home commands execute immediately under existing device permissions. Email and calendar writes still require the review card. Backend work has its own model usage, in addition to live call duration. At most one call per user can be active, with three starts per minute, a 15-minute default duration, and a browser heartbeat timeout. Usage snapshots are recorded as cumulative seconds. Unconfirmed final usage is explicitly marked.

Keep the page in the foreground and the phone unlocked. Switching tabs/apps or locking the phone ends the call in this version. It is a browser conversation, without a background wake word. Bluetooth/speaker behavior and perceived interruption latency need testing on your phone. Use HTTPS remotely: an HTTP LAN IP cannot request the microphone. Raw audio is not stored by Simon; provider session storage is disabled. Text fragments and delegated task results are persisted. Captions are fragments, not guaranteed complete turns or proof that audio was heard.

## Set up the home origin

1. Install Cloudflare Tunnel (`cloudflared`) and use a Cloudflare-managed DNS zone for the tunnel hostname. If `camrobbins.com` uses another DNS provider, either prepare its migration while preserving all Vercel/MX/TXT records, or use a separate domain already on Cloudflare for the origin hostname. The public portfolio URL remains unchanged. Do not change nameservers blindly.
2. Authenticate and create a named tunnel on the home computer:

   ```powershell
   cloudflared tunnel login
   cloudflared tunnel create simon
   cloudflared tunnel route dns simon simon-origin.camrobbins.com
   ```

3. Copy `deploy/remote/cloudflared.example.yml` into `.local/cloudflared.yml`, filling in the tunnel UUID and credential path. Keep credentials outside Git. The explicit `httpHostHeader: www.camrobbins.com` satisfies Simon's host checks. The tunnel preserves `/simon`. This computer has the verified Cloudflare binary in `.local/bin/cloudflared.exe`; the tunnel launcher also supports a system installation.
4. Stop the development server, keep Docker Desktop running, and start the production process:

   ```powershell
   .\scripts\start-server.ps1 -Check
   .\scripts\start-server.ps1
   ```

   This launcher disables development login, requires HTTPS and PostgreSQL, applies migrations, uses one worker, and binds to loopback. Its default household and actor now identify the `cam40419` account. Override `-HouseholdId`, `-ActorId`, and `-DatabaseUrl` for another installation. It does not seed or replace your identity records.

5. In another terminal:

   ```powershell
   .\scripts\start-tunnel.ps1
   ```

No router port forwarding is required. The home computer, Docker, Simon, and the tunnel must remain running.

## Merge the portfolio route

The sibling portfolio uses legacy `routes`: its `/simon` and `/simon/(.*)` external destinations precede the existing static and Flask routes. For projects using `rewrites`, use `deploy/remote/vercel.simon.example.json` before a catch-all rewrite. Preserve the portfolio's existing configuration. Review the preview deployment, then publish the portfolio change.

Check `https://www.camrobbins.com/simon/health/live`, `/simon/login`, and `/simon/chat`. Login, assets, API requests, Google callbacks, and chat streaming retain the prefix. Responses explicitly disable browser and CDN caching. The portfolio root should still load normally. Check that any existing Vercel headers do not disable the microphone or override Simon's security headers. The portfolio redirects the apex to `www.camrobbins.com`; use that canonical host for the public origin, relying-party ID, tunnel Host header, and Google callback.

## Private sign-in and Google

Use an owner passkey for `www.camrobbins.com`. A localhost passkey belongs to a different relying party and will not authenticate here. Generate a one-use enrollment token locally, then use the enrollment form at `https://www.camrobbins.com/simon/login` within 15 minutes:

```powershell
.\scripts\start-server.ps1 -Enroll
```

Use a synced passkey or a compatible security key to sign in from other supported browsers. No enrollment or admin credential is put into Vercel configuration. The app uses Secure, HttpOnly `__Host-` session cookies and CSRF checks. `/simon` shares the portfolio's browser origin; path routing does not isolate it from other scripts served by that domain. Only trusted portfolio code should share this origin.

If Google is connected, register this exact authorized redirect URI in its Web OAuth client:

`https://www.camrobbins.com/simon/auth/google/callback`

## Keep it running

After the interactive remote test succeeds, create Task Scheduler tasks for the current Windows user at logon: one launches `powershell.exe -NoProfile -WindowStyle Hidden -File "<repo>\scripts\start-server.ps1"`; the other launches the same command with `scripts\start-tunnel.ps1`. Set the working directory, restart on failure, and disable the task's execution time limit. Docker Desktop must start at login, and sleep must be disabled while serving. The launcher returns a nonzero exit code on failure so the task can restart it. For operation before login, move to an always-on host or explicitly configure services with suitable credentials. These tasks have not been registered automatically.

For the initial deployment, both launchers run in hidden background processes. Their logs and
launcher PIDs are in `.local/server.*` and `.local/tunnel.*`. The tunnel uses IPv4 because this
network's IPv6 tunnel connections were unreliable. Keep this computer awake; these background
processes do not survive signing out or rebooting.

Use the PostgreSQL backup and restore procedure in [persistence.md](persistence.md). Database backups include voice transcripts and task history. Store them privately. Run one Simon process; multiple workers require shared voice-session coordination that is not implemented in this phase. On a process crash, the browser disconnects on its next status poll; stale session admission expires at the stored call deadline. Do not automatically reconnect or replay an interrupted command.

Protocol references: [OpenAI GPT-Live](https://developers.openai.com/api/docs/guides/live), [WebRTC](https://developers.openai.com/api/docs/guides/voice-webrtc), [server controls](https://developers.openai.com/api/docs/guides/voice-server-controls), [Vercel external rewrites](https://vercel.com/docs/routing/rewrites), and [Cloudflare tunnel configuration](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/).

## Personal voice settings

Use Personality & voice in chat to choose Jarvis mode, Vesper, your preferred address, and
additional personality guidance. Settings are private to your account within the workspace.
The server voice remains a fallback for accounts without a saved selection. Start a new call
after changing its voice. See [accounts and workshop plan](../accounts-workshop-plan.md).
