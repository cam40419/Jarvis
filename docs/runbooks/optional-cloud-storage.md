# Optional Dropbox, Box, and OneDrive storage

Simon's local files remain the default storage. These optional agent tools access a bounded
part of an operator-managed cloud account. They do not synchronize that account to the local
server or add an interactive OAuth connection to the browser interface.

## Implemented operations

| Provider | Tool IDs | Write support |
| --- | --- | --- |
| Dropbox | `dropbox.list`, `dropbox.search`, `dropbox.metadata`, `dropbox.read`, `dropbox.upload` | Create or update a file up to 256 KiB with revision conflict protection |
| Box | `box.list`, `box.metadata`, `box.read` | Read only |
| OneDrive / SharePoint document libraries | `onedrive.list`, `onedrive.metadata`, `onedrive.read` | Read only |

Dropbox search searches filenames inside the configured root or a requested subfolder.
Box and Microsoft support folder enumeration and direct reads; they do not expose global
search. These adapters do not delete, move, share, invite users, export cloud-native documents,
or upload to Box or Microsoft. SharePoint support means files in an explicitly configured
Microsoft Graph drive, not arbitrary SharePoint pages, lists, or site administration.

## Configure the account and permissions

Each definition grants a service account to **one workspace and an explicit list of actor
IDs**. Obtain these IDs from the authenticated `/auth/session` response. The account is shared
only with those actors; it is not each actor's own OAuth connection. For a separate account
or workspace, use separate definitions with unique tool IDs and separate credentials.

Provision provider access tokens outside Simon and put them in server environment variables.
The defaults are `SIMON_DROPBOX_TOKEN`, `SIMON_BOX_TOKEN`, and `SIMON_ONEDRIVE_TOKEN`.
Tokens must be present in both the API and dispatcher environments. Do not put token values in
manifests, objectives, or prompt variables. Interactive OAuth, token refresh, and account
discovery are not implemented here. An expired or revoked token is reported when a provider
request fails; the catalog's `configured` status checks local configuration and the existence
of the credential, not live account health.

Use provider permissions restricted to the intended content and operations. The adapter's
folder boundary is an additional check; it does not narrow a token at the provider. Dropbox
uses metadata-read and content-read access, plus content-write only for uploads. Box and
Microsoft need read access to the configured folder, its ancestor metadata, and its files.
Provider permissions and admin consent must be configured in the respective provider account.

Generate definitions with the factories below. Add selected definitions to the manifest's
`tools`, their IDs to the agent profile's `tool_ids`, and `jobs:read` to its `tool_scopes`.
Dropbox uploads additionally require `jobs:write` and `max_action: "write"`. The API and
dispatcher must load the same manifest. All factories default to disabled, and all operations
are network tools, so local-only tasks cannot use them.

```python
from uuid import UUID

from simon.adapters.cloud_storage_tools import (
    box_tool_definitions,
    dropbox_tool_definitions,
    onedrive_tool_definitions,
)

workspace = UUID("11111111-1111-4111-8111-111111111111")  # Replace with your workspace.
actor = UUID("22222222-2222-4222-8222-222222222222")      # Replace with the permitted actor.
grant = dict(enabled=True, workspace_id=workspace, actor_ids=[actor])

dropbox = dropbox_tool_definitions(**grant, root_path="/Simon")
box = box_tool_definitions(**grant, root_folder_id="1234567890")
onedrive = onedrive_tool_definitions(
    **grant,
    drive_id="replace-with-drive-id",
    root_item_id="replace-with-folder-item-id",
    download_hosts=["yourtenant.sharepoint.com"],
)

# Only settings and credential environment variable names are serialized.
tool_entries = [tool.model_dump(mode="json") for tool in (*dropbox, *box, *onedrive)]
```

The Microsoft download hostname must match the exact host used by that account's signed
download URLs. The example hostname is illustrative. Configure only exact, provider-owned
hosts ending in `.sharepoint.com` or `.files.1drv.com`; wildcards are rejected. Without a
download host, Microsoft list and metadata operations can be configured but reads remain
unconfigured. Box downloads permit the exact host `dl.boxcloud.com`; other regional hosts
are rejected until explicitly supported in code. API endpoints are fixed to the public
Dropbox, Box, and Microsoft Graph services; sovereign clouds and alternative endpoints are
not supported.

## Folder boundaries and revisions

Dropbox requires a non-root folder path such as `/Simon`. Task arguments are relative to that
folder. Absolute paths, traversal, encoded paths, and `id:` / namespace aliases are rejected.
Responses must identify a path inside the root and the requested folder. Pagination accepts
a page number, then follows a fresh server-issued cursor internally; models cannot supply
cursors from another location. Download metadata comes from the same response as the bytes,
following the [Dropbox download contract](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/files/download).

For a new Dropbox file, explicitly supply `expected_revision: ""`. The adapter uses add mode,
`autorename: false`, and `strict_conflict: true`, preventing replacement or an unexpected
renamed copy. To update, first read the file's `rev` and supply that exact value. These use the
[Dropbox upload contract](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/files/upload)
and [commit conflict options](https://dropbox-sdk-python.readthedocs.io/en/latest/api/files.html#dropbox.files.CommitInfo).

```json
{"path":"reports/summary.md","content":"New report","expected_revision":"","encoding":"text"}
```

Box task arguments use item IDs. Before returning metadata or downloading a file, the adapter
checks the provider's `path_collection` against the configured root folder. Folder listings
also verify each returned child's parent. Downloads request a specific file version, then
recheck ancestry and version before returning content. This follows the
[Box file metadata](https://developer.box.com/reference/get-files-id),
[folder listing](https://developer.box.com/reference/get-folders-id-items/), and
[download](https://developer.box.com/reference/get-files-id-content) contracts.

Microsoft task arguments are paths relative to the configured root item. The adapter walks
the provider's parent references back to that root within the fixed drive. Cross-drive items,
remote shortcuts, cycles, and paths outside the folder tree are rejected. Reads recheck the
parent chain and ETag after downloading, so a detected move or revision change causes a
failure instead of returning the content. This uses Microsoft Graph
[item addressing](https://learn.microsoft.com/en-us/graph/api/driveitem-get?view=graph-rest-1.0),
[parent references](https://learn.microsoft.com/en-us/graph/api/resources/itemreference?view=graph-rest-1.0),
and [download behavior](https://learn.microsoft.com/en-us/graph/api/driveitem-get-content?view=graph-rest-1.0).

Box and Microsoft downloads may redirect to a signed URL. Only the allowed exact HTTPS
download hosts are accepted, and the API bearer is never forwarded to them. Signed URLs
are not included in returned metadata. Provider-supplied listing links are restricted to the
same fixed Graph endpoint and folder. Known access-token values reflected in content or
metadata are redacted; content results indicate when this changes the bytes.

## Limits and recovery

Files are limited to 256 KiB per read or upload, with UTF-8 text or base64 output. Each HTTP
response is bounded to 2 MB. An operation has a 30-second default time budget and at most
32 HTTP requests, including metadata checks. Microsoft ancestry is limited to 20 nodes.
Dropbox and Microsoft accept at most ten pages per request; Box offsets are limited to
10,000. These agent limits do not change the larger browser file upload/download limits.

There are no automatic retries. A Dropbox conflict is a known failure. A network interruption,
server error, or invalid upload receipt may leave the write outcome unknown: inspect the
destination before retrying. Explicit revision checks protect retries from silently
overwriting a newer file, but Simon does not claim that an unverified write failed.

The tests use mocked HTTP contracts for provider requests, folder boundaries, explicit grants,
redirect handling, revisions, race detection, and bounded content. They do not verify that a
particular externally provisioned account or token currently works. No live cloud writes are
part of this test suite.
