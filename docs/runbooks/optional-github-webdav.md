# Optional GitHub and WebDAV tools

Simon can use an operator-managed GitHub service account and a WebDAV folder, including
Nextcloud storage. These are real REST/WebDAV adapters. They are disabled until the operator
supplies the account grant, endpoint, credential environment variable, and agent permissions.
They do not connect through plugins installed in the coding application.

## Operations

| Tool                                          | Behavior                                                        |
| --------------------------------------------- | --------------------------------------------------------------- |
| `github.repository`                           | Repository metadata                                             |
| `github.issues`, `github.issue`               | Paginated issues or one issue, with Markdown body               |
| `github.pull_requests`, `github.pull_request` | Paginated pull requests or one pull request                     |
| `github.file_read`                            | UTF-8 repository file at an optional branch or commit reference |
| `github.issue_create`                         | Create an issue in a permitted repository                       |
| `github.pull_request_draft`                   | Create a draft between existing branches in the same repository |
| `webdav.list`                                 | Immediate children of a directory under the configured root     |
| `webdav.read`                                 | File content as UTF-8 text or base64, plus its ETag             |
| `webdav.write`                                | Conditional create or update, using an expected ETag            |

GitHub operations do not push commits, merge pull requests, delete content, or accept arbitrary
HTTP requests. Draft pull requests require existing remote branches. GitHub's issue listing
can contain pull requests; each returned item includes a `kind` field. These contracts follow
the [GitHub issues](https://docs.github.com/en/rest/issues/issues),
[pull requests](https://docs.github.com/en/rest/pulls/pulls), and
[repository contents](https://docs.github.com/en/rest/repos/contents) APIs.

WebDAV does not delete or move files, create directories, or recursively enumerate a server.
Use existing destination directories. The adapter implements the documented
[Nextcloud WebDAV file operations](https://docs.nextcloud.com/server/latest/developer_manual/client_apis/WebDAV/basic.html).

## Configure an explicit account grant

Each tool definition grants one service account to **one workspace and an explicit list of
actor IDs**. Obtain these IDs from the authenticated `/auth/session` response. Empty grants
fail closed. The service account is shared only with those actors; it is not each user's OAuth
connection. For another workspace or service account, use separate tool definitions with
unique IDs and separate credentials.

Credentials are read on the server from environment variable names in `credential_env`.
Never put a token or password into tool settings, objectives, prompt variables, or output files.
GitHub should use a fine-grained token restricted to the listed repositories: metadata,
contents, issues, and pull-request permissions as needed. Enable write permissions only for
profiles that need the two creation operations. For Nextcloud, use a dedicated account and
an app password restricted to the intended data where the server supports that restriction.

Generate definitions using the factories below, then add the selected definitions to the
manifest's `tools`, their IDs to the desired profile's `tool_ids`, and the necessary
`jobs:read` / `jobs:write` scopes to that profile's `tool_scopes`. A writing profile also needs
`max_action: "write"`. The API and dispatcher must load the same manifest and credentials.

```python
from uuid import UUID

from simon.adapters.github_tools import github_tool_definitions
from simon.adapters.webdav_tools import webdav_tool_definitions

workspace = UUID("11111111-1111-4111-8111-111111111111")  # Replace with your workspace.
actor = UUID("22222222-2222-4222-8222-222222222222")      # Replace with the permitted actor.

github = github_tool_definitions(
    enabled=True,
    workspace_id=workspace,
    actor_ids=[actor],
    repositories=["your-organization/your-repository"],
    credential_env="SIMON_GITHUB_TOKEN",
)
webdav = webdav_tool_definitions(
    enabled=True,
    workspace_id=workspace,
    actor_ids=[actor],
    endpoint="https://cloud.example.com/remote.php/dav/files/simon/Simon",
    username="simon",
    credential_env="SIMON_WEBDAV_PASSWORD",
)

# Only configuration and environment variable names are serialized.
tool_entries = [tool.model_dump(mode="json") for tool in (*github, *webdav)]
```

Factories default to disabled. `configured` indicates that configuration fields were supplied;
the catalog's local preflight additionally checks tenant grants and credential availability.
“Configured” does not mean the provider has been contacted or that the remote credentials are
valid. Both adapters declare network use, so a local-only task cannot use them.

The default GitHub API root is `https://api.github.com`, with API version `2026-03-10` explicitly
selected. An operator can supply a fixed GitHub Enterprise REST root with `endpoint` and change
`settings.api_version` to a version supported by that server. The version header follows the
[GitHub API versioning contract](https://docs.github.com/en/rest/about-the-rest-api/api-versions).

## File revisions and limits

Read before updating a WebDAV file. Supply the exact strong, quoted `etag` returned by the read
as `expected_etag`. The adapter sends `If-Match` and reports a conflict when the file has changed.
For a new file, explicitly supply `expected_etag: ""`; the adapter sends `If-None-Match: *`,
which prevents replacing an existing file. Weak ETags, wildcard revision values, revision lists,
and unquoted ETags are rejected. These conditions use the
[HTTP conditional request contract](https://datatracker.ietf.org/doc/html/rfc9110#section-13.1).

```json
{ "path": "reports/summary.md", "content": "New report", "expected_etag": "", "encoding": "text" }
```

Files are limited to 256 KiB per operation. GitHub files must be UTF-8 text. WebDAV supports UTF-8
text or base64 for binary files. A directory listing is shallow and limited to 1,000 entries;
GitHub lists use explicit pages of at most 100 items. Provider responses have a 2 MB hard limit,
with 30-second default request and response deadlines. These agent tools do not change the
larger limits of Simon's browser upload/download interface.

Known credential values reflected by a remote service are redacted from model-visible output.
File results indicate `redacted: true` if that changes the content. Preserve the original at the
source; a redacted result is not a byte-identical archival copy.

## Failures and recovery

Requests do not follow redirects or automatically retry. Paths are relative to the configured
WebDAV root; parent paths, encoded traversal, absolute URLs, and out-of-root listing entries
are rejected. Credentials are sent only to the fixed operator-configured service endpoint.

A refused write or revision conflict has a known failure. A connection loss, an unexpected
redirect after a write, an invalid creation response, or a server error can have an **unknown
outcome**. Inspect the destination before submitting a new write. GitHub issue creation does
not provide a general idempotency key; repeating a creation can produce duplicate issues.

Tests use `httpx.MockTransport` for deterministic API contracts, tenant boundaries, path handling,
credential redaction, conditional requests, and unknown write outcomes. Live account validation
requires an operator-configured endpoint and credential; unit-test success does not verify a
particular cloud account.
