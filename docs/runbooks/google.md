# Google account connections

Google access uses each Simon user's OAuth connection. Credentials from another
application are not inherited. Native project teams do not yet execute Google tools;
the current connection is used by the independent chat and voice services.

## Setup

1. Create a Google Cloud project and enable the Calendar, Gmail, Drive, Docs and
   Sheets APIs required by the tools you intend to use.
2. Configure the OAuth consent screen and add intended users to the test-user list
   when using a test application.
3. Create a Web application OAuth client with the exact redirect URI displayed by
   Simon. Development normally uses `http://localhost:8000/auth/google/callback`.
4. As the site administrator, open **Connections**, select **Google application
   setup**, and save the client ID and secret. The backend encrypts the secret.
5. Acknowledge the connection's data-sharing explanation, then choose **Connect
   Google** and grant the desired capabilities in Google's consent screen.

The account panel reports granted permissions. Reconnect a connection to change its
grant. Keep the database and its matching credential encryption key in a private
[recovery bundle](storage-recovery.md). The `simon.google_setup` command is also
available for server-side setup; inspect `--help` before use.

## Current tools

- Calendar reads and creates events on the selected account's primary calendar.
  Explicit requests to schedule an event execute directly with a durable receipt.
  Preview requests produce a review card. Attendee invitations, recurrence and
  editing or deleting existing events are not exposed.
- Gmail search returns headers and snippets; message reads do not mark mail read.
  Sending an email requires the requesting user to confirm its exact preview card.
- Drive search, folder browsing and bounded document reads are available. Supported
  text includes Google documents and exported content; unsupported formats return
  metadata. These tools do not provision native project folders or synchronize
  board outputs. The native file integration remains future work.

Each Simon user can connect multiple Google accounts. Select a default or give an
account email or connection ID to a tool. Results identify their account; IDs and
page tokens must be used with the account that returned them. Changing the default
does not change an existing action's selected account. Another Simon user cannot
select these connections by knowing their IDs.

## Review and uncertain outcomes

Only the user who requested an email preview can confirm it. Text such as "send it"
does not execute the card. Request a replacement preview when details change and
cancel the old preview. Confirmation durably claims an action before provider I/O;
repeating confirmation returns its status without dispatching again.

Timeouts and interruptions can leave an unknown provider outcome. Inspect Google
before requesting a new action. Email delivery has no provider-level exactly-once
guarantee. Disconnecting removes Simon's stored credentials and invalidates pending
previews; it does not delete conversation history or revoke the grant at Google.

Google results used in an answer enter the configured model's context and can
remain in conversation history. OAuth tokens and application secrets are excluded
from prompts and public responses. Tests use synthetic OAuth and provider responses;
they do not send real messages or calendar events. Live model tests are separately
opted in and may incur provider charges. See [development](../development.md).
