# Connect Google to Simon

Simon can search public web pages immediately when OpenAI is enabled. Gmail and Calendar use your
own Google OAuth connection. Credentials configured in Codex or ChatGPT are not inherited by Simon.

## One-time server setup

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a project. Enable
   **Google Calendar API**, **Gmail API**, **Google Drive API**, **Google Docs API**, and
   **Google Sheets API** under APIs & Services.
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
   **Connect Google**. Sign into the intended account and grant Calendar, Gmail, and/or Drive permission.
   Google redirects back to Simon. The panel shows the account and permissions that were granted.

Google requests `openid`, `email`, `calendar.events.owned`, `gmail.send`, `gmail.readonly`, and
`drive`. Existing connections must use **Reconnect Google** to grant Drive editing access;
the Connections panel lists granted capabilities and flags missing permissions. Enable Google Drive
API in the same Cloud project as the OAuth client. Each capability is available only when its scope
was granted; declining one does not disable the others.

Simon reads and creates events only on the primary calendar. Explicit requests to create/add/schedule
an event execute immediately and return a receipt; an extra confirmation is not required. Calendar
previews are reserved for explicit draft/preview requests. The assistant uses the browser timezone,
defaults an omitted duration to 30 minutes, and asks about ambiguous essential details. Email to
one recipient still uses review. Gmail tools search with Gmail query syntax, return headers/snippets, and
read message bodies without marking messages read or downloading attachments. Drive tools search
file names/content and read Google Docs, Slides, the first sheet of Sheets as CSV, and text files.
Other types, including PDFs, return metadata and a content-support note. Searches paginate, and
content reads have explicit size limits. Project tools can create and edit files inside a project's
linked Drive folder. Simon does not invite attendees, create recurring events, or edit/delete
existing calendar events.
The event scope allows broader operations at Google, but Simon does not expose those operations.

External apps in Testing can receive refresh tokens that expire after seven days for these scopes;
reconnect if Google reports an expired grant. Public distribution may require Google's verification.
See Google's [web OAuth guide](https://developers.google.com/identity/protocols/oauth2/web-server)
and [token expiration guidance](https://developers.google.com/identity/protocols/oauth2#expiration).

## Try it

- **Web:** “Check the Ace website for M4 x 35 mm machine screws. Link the product pages and distinguish
  online listings from verified local stock.” Supply your store or ZIP code for local availability.
- **Calendar:** “What's on my calendar tomorrow?” The browser supplies its timezone automatically.
- **Gmail:** “Show my five most recent unread emails.” Then ask to read a returned message.
- **Drive:** “Find my recent Google Drive documents.” Then ask to summarize a returned Google Doc.
- **Schedule:** “Add a 30-minute dentist appointment next Tuesday at 2 pm.” Simon creates it and
  reports the actual result with a calendar link. Ask for a preview explicitly if desired.
- **Email:** “Email [a real recipient you choose] with subject Test from Simon and body This is a test.”
  Check the recipient and text, then click **Confirm & send email** only when you want it sent.
- **Reservation:** “Find the official reservation page for [restaurant].” Simon can provide a link or
  prepare an email request. Sending a request does **not** confirm a booking. Automatic website form
  submission, payment, and general browser control remain future work.

Text such as “yes, send it” in chat does not execute a pending email card. Use its explicit button.
To change details, request a replacement preview and cancel the old one. Previews expire after 30
minutes. Only the user who requested a preview can see its card or confirm it; household members can
still see shared conversation text. Google results, email bodies and Drive contents used in
conversation go to OpenAI and remain in that conversation's history. OAuth tokens, client secrets,
and encryption keys never enter the
model prompt or public API responses.

Direct calendar creation claims an action durably against the active original model attempt before
calling Google. Repeated identical tool calls reuse the same action and provider event ID. Think
deeper cannot create events. Receipts survive interrupted/failed answers and remain visible in the
conversation. Migration **0022** lets receipts reference either an active attempt or a completed run.

Confirmation claims an explicit preview durably before calling Google outside the database transaction.
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


## Work projects and live Drive files

Work projects automatically get a `Simon - <project name>` folder in the connected account's My Drive.
Use **Link existing folder** or tell Simon which Drive folder to use instead. The folder is bound to
both the Simon account and Google account. Changing Google accounts requires explicitly relinking.
Changing a project's remembered context keeps its existing folder. Removing a project does not delete
its Drive folder or files.

After upgrading, enable Docs and Sheets APIs in the same Google Cloud project, then use
**Connections ? Reconnect Google** and grant Drive editing. No new OAuth client or manually copied
token is needed. The broader `drive` scope is needed to link existing arbitrary folders without a
Google Picker; Simon's project tools enforce the linked-folder boundary. Existing read-only grants
continue to work for the general Drive search/read tools.

Use **Browse files** to see live contents, browse subfolders, preview text, upload files up to 10 MB,
or open files in Drive. Add larger files directly in Drive. The browser refreshes every 30 seconds;
every assistant read fetches current content. The API checks projects every minute and uploads up to
five pending task outputs per project each cycle. Original task artifacts remain available locally;
uploaded files become live Drive files and later edits are never overwritten by resyncing originals.
Relinking uses the new folder for future work and uploads task originals there; existing Drive files
stay in their old folder.

Text and voice share the project tools. For example:

- ?Create a project called Kitchen remodel.?
- ?Use this Drive folder for Kitchen remodel: <folder link>.?
- ?Read the budget notes in Kitchen remodel and change the cabinet allowance to $4,000.?
- ?Create a Google Doc with the next steps in that project.?

Requested file changes execute directly with durable receipts. Text/code and Google Docs support
exact text replacement or append. Google Sheets supports bounded cell reads and literal value writes
(up to 100 rows / 2,000 cells); formula evaluation, formatting, and structural spreadsheet edits are
not exposed. PDFs, images, Office files and other binaries can be stored/opened, but the project tools
do not edit their contents. Text reads and edits are limited to 200,000 characters.

Edits preserve a private pre-edit snapshot in the operation record. Docs uses Google's required
revision check. Other files check current versions and use HTTP ETags when available. Sheets checks
the range again before writing, but Google does not provide an atomic range revision precondition;
simultaneous external edits can still race. Uncertain writes are reported as uncertain and not
repeated automatically. Uploaded binary/text files and folders use durable generated Drive IDs to
recover safely from dropped responses. Native Docs/Sheets creations cannot use these IDs, so an
uncertain native creation must be inspected in Drive before a fresh request.

`SIMON_PROJECT_DRIVE_SYNC_ENABLED=true` enables automatic folder provisioning and task output uploads
(default). Set it false to disable the background sweep; explicit chat/voice/UI operations still work.
Migration `0023_project_drive.sql` adds folder bindings and durable file operation receipts. Project
files use the API process for syncing, so keep Simon's API running alongside the task worker.


## Multiple Google accounts

Connections now supports any number of Google accounts per Simon user. Click **Add another
Google account**, choose the account in Google's account picker, and complete consent. Adding
a different email keeps existing accounts. Reconnecting the same email updates only that
connection. Each account has its own encrypted tokens and granted permissions.

Use **Make default** for requests that do not name an account, or give Simon an exact email:
?Search unread mail in second@example.com.? The `google_accounts_list` tool lists account
emails, IDs, permissions and the default; Calendar/Gmail/Drive tools accept an `account`
argument (email or connection ID, empty for default). Requests for all accounts are executed
separately and results include account identity. Read IDs and page tokens must be used with
the account that returned them. Calendar and email receipts retain their selected account.

Disconnect removes only the selected account from Simon. Removing the default chooses the
first remaining email in alphabetical order. Existing project folders continue using their
bound email regardless of default changes; disconnecting that account pauses its project
access until it is reconnected. Project creation and folder linking accept an account, and
Work asks which account to use when linking a folder with multiple accounts connected.
Local project files are unaffected.

Migration 0024 preserves existing encrypted connections and makes each existing connection
the default for its Simon user. Account selection remains within that user's household and
identity; another Simon user cannot select these accounts by knowing an email or ID.

The existing OAuth client is reused. Each account must grant access in its own browser login;
Simon cannot approve Google's consent screen. If the OAuth app is still in Testing, add each
new account to its test users in Google Cloud. Workspace policies may also restrict consent.
Google documents the `consent select_account` prompts in its
[OAuth web-server guide](https://developers.google.com/identity/protocols/oauth2/web-server).


## Browsing, project locations, unlinking and trash

Simon can browse Drive by name with `drive_list_folder`, including child folders, whole-account
name searches and paginated results. It supports each connected account independently. Work's
**Link existing folder** button opens a folder browser with account selection and name search;
no pasted ID is necessary. **Use My Drive** links the account's root directly. Google's `root`
alias is resolved to its actual folder ID before saving the binding, so containment checks
and subsequent project writes use the same canonical location.

**Unlink Drive** (`project_unlink_drive`) preserves the project, local project directory and
Drive files, clears the folder association and disables automatic Drive provisioning/sync.
This state persists across restarts and project context updates. Linking a folder again resumes
Drive access. Changing a link does not move or delete old files.

`project_drive_trash` and the file browser's **Move to trash** button move selected Drive files
or folders to trash, with durable operation receipts and revision checks. Folders include their
contents. Permanent deletion and emptying trash are not implemented. The active project's folder
or its ancestor must first be unlinked/relinked. My Drive itself is always protected even when
Google reports canTrash=true. Unknown outcomes are not automatically retried. Trashed items can
be restored through Google Drive. Credentials must include full Drive editing permission.

Text and voice use the same tools. Examples: ?Find the Stdout Collective folder in My Drive,?
?Use My Drive itself as this project's main folder,? ?Unlink this project's Drive folder,? and
?Move the old project folder to trash.? Simon should browse to resolve names and only ask for
clarification when matching folders are ambiguous.
