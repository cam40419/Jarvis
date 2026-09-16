# Connect Google to Simon

Simon can search public web pages immediately when OpenAI is enabled. Gmail and Calendar use your
own Google OAuth connection. Credentials configured in Codex or ChatGPT are not inherited by Simon.

## One-time server setup

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a project. Enable
   **Google Calendar API** and **Gmail API** under APIs & Services.
2. Configure the OAuth consent screen in **Google Auth Platform**. For a personal test app, select
   External, leave it in Testing, and add your own Google account as a test user under Audience.
3. Create an OAuth client with application type **Web application**. Add the exact authorized redirect
   URI shown in Simon's Connections panel. The normal development URL is:
   `http://localhost:8000/auth/google/callback`. A deployed HTTPS origin needs its own exact redirect.
4. Download the client JSON to a private location outside the repository. Import it locally:

   ```powershell
   .\venv\Scripts\python.exe -m simon.google_setup --client-file "C:\path\to\client_secret.json"
   ```

   This preserves other `.env` settings and writes `SIMON_GOOGLE_CLIENT_ID`,
   `SIMON_GOOGLE_CLIENT_SECRET`, and a generated `SIMON_GOOGLE_TOKEN_KEY`. It prints no secrets.
   Re-running with an existing valid token key preserves it. Keep this file private and backed up;
   losing the key means you must reconnect Google, even if the database backup is intact.
5. Restart Simon using `scripts/start-dev.ps1`. It applies migration **0007**, which adds encrypted
   connection storage, short-lived OAuth state, and durable action previews.
6. Open **Connections**, acknowledge that chats are shared with your household, then click
   **Connect Google**. Sign into the intended account and grant Calendar and/or Gmail permission.
   Google redirects back to Simon. The panel shows the account and permissions that were granted.

Google requests `openid`, `email`, `calendar.events.owned`, and `gmail.send`. Simon reads and creates
events only on the primary calendar and sends plain-text email to one recipient. It does not read
your inbox, add attachments, invite attendees, create recurring events, or edit/delete existing events.
The event scope allows broader operations at Google, but Simon does not expose those operations.

External apps in Testing can receive refresh tokens that expire after seven days for these scopes;
reconnect if Google reports an expired grant. Public distribution may require Google's verification.
See Google's [web OAuth guide](https://developers.google.com/identity/protocols/oauth2/web-server)
and [token expiration guidance](https://developers.google.com/identity/protocols/oauth2#expiration).

## Try it

- **Web:** “Check the Ace website for M4 x 35 mm machine screws. Link the product pages and distinguish
  online listings from verified local stock.” Supply your store or ZIP code for local availability.
- **Calendar:** “What's on my calendar tomorrow?” The browser supplies its timezone automatically.
- **Schedule:** “Add a 30-minute dentist appointment next Tuesday at 2 pm.” Review the date, UTC
  offset, title, and location, then click **Confirm & create event**.
- **Email:** “Email [a real recipient you choose] with subject Test from Simon and body This is a test.”
  Check the recipient and text, then click **Confirm & send email** only when you want it sent.
- **Reservation:** “Find the official reservation page for [restaurant].” Simon can provide a link or
  prepare an email request. Sending a request does **not** confirm a booking. Automatic website form
  submission, payment, and general browser control remain future work.

Text such as “yes, send it” in chat does not execute a pending card. Use the card's explicit button.
To change details, request a replacement preview and cancel the old one. Previews expire after 30
minutes. Only the user who requested a preview can see its card or confirm it; household members can
still see the conversation text. Calendar results and drafts used in conversation go to OpenAI and
remain in household chat history. OAuth tokens, client secrets, and encryption keys never enter the
model prompt or public API responses.

Confirmation claims the preview durably before calling Google outside the database transaction.
Repeating the same confirmation returns its status without dispatching again. An unknown network
outcome is not automatically retried; check Google before requesting a new action. A server crash can
leave a card showing “Action started”; check Google before making another request. Event IDs are
derived from the preview ID; email delivery still has no provider-level exactly-once guarantee.

Disconnect deletes Simon's stored credentials. To revoke the OAuth grant at Google too, remove Simon
from [Google account connections](https://myaccount.google.com/connections). Existing history and
receipts remain. Reconnecting creates a new binding and invalidates older pending previews.

## Verification

The automated suite simulates Google's OAuth/token, calendar, and Gmail endpoints. It checks browser
binding, single-use state, encrypted storage, scope restrictions, confirmation, expiry, cancellation,
concurrent duplicate protection, and unknown outcomes. It sends **no real emails or calendar events**.
Live web search has a separate opt-in synthetic test:

```powershell
$env:SIMON_LIVE_MODEL_TESTS='1'
.\venv\Scripts\python.exe -m pytest tests/integration/test_live_web.py -q
```

That test uses your OpenAI API billing. Web search itself can incur tool charges in addition to model
tokens; Simon records usage and tool calls but does not yet enforce a spending budget.
See the [OpenAI web tool guide](https://developers.openai.com/api/docs/guides/tools-web-search).

Verified September 14, 2026: 264 offline cases passed across the regression run and final focused
browser recheck; regression coverage was 96.18%. Two separate live checks passed for public web
citations and a synthetic email-preview function loop, including encrypted reasoning continuation.
The latter prepared only an in-memory preview; it did not call Google or send a message.

Ruff, mypy (40 source files), wheel build, and a clean wheel installation with dependency and HTTP
smoke checks passed. The pre-existing development venv still contains an unrelated `mysql` package
with a missing `mysqlclient` dependency; Simon uses PostgreSQL, and its clean install has no broken
requirements. Migration 0007 is applied locally. Backup/restore verified all 26 tables against
`.local/backups/simon_20260914T173639Z_2deb48f6.dump` and its JSON report. Desktop/mobile screenshots
are in `.local/screenshots/simon-action-preview-{desktop,mobile}.png`.
