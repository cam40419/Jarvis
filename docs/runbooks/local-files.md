# Local files and ZIP archives

Simon can browse, search, read, create, edit and rename/move local files, create folders,
inspect/extract ZIP archives, and create ZIPs. Text chat and voice share these tools. Requested
operations execute directly and return results without a confirmation card.

Local means the computer running Simon's server. Opening Simon on a phone does not expose
the phone's filesystem. Use **Work → Local files → Upload** to supply files from another device.
Each Work project also has a **Local files** button for its account-specific local folder.

## Folder access

Every account has a separate workspace under:

```
.local/files/<household-id>/<actor-id>/
```

Project folders use `projects/<project-id>/` inside that workspace. Assistant tools can address
them as root `project:<project-id>` after resolving the project through `project_list`.

Only the configured local-file owner can access host folders. By default that is
`SIMON_ACCOUNT_ADMIN_ACTOR_ID`, and the available host folders are Desktop, Documents and
Downloads under the server process user's home directory. Other accounts get their own
workspaces; household membership does not grant access to the owner's host folders.

Configuration:

```dotenv
SIMON_LOCAL_FILES_ENABLED=true
SIMON_LOCAL_FILES_DIR=.local/files
# Optional: override which account can use host folders.
# SIMON_LOCAL_FILES_ACTOR_ID=<actor UUID>
# Optional: replace the default host folder aliases. Use forward slashes on Windows.
# SIMON_LOCAL_FILE_ROOTS={"downloads":"C:/Users/cam40/Downloads","projects":"D:/Projects"}
```

An empty root mapping uses the defaults. Restart both API and worker after changing configuration.
Tools always use a listed root and a relative path; arbitrary absolute paths are not accepted.
Simon server files, common credential files, private-key files, internal backup folders,
symlinks and Windows junctions are excluded. Existing host files are not moved or scanned until
requested. Folder searches match names and stop after 100 matches, 20,000 entries or five seconds.

## Examples

- “Find Stdout Collective.zip in Downloads and tell me what is inside.”
- “Unzip it into a new Stdout Collective folder in my workspace, then read the README.”
- “Change the project description in those notes and save it.”
- “Rename notes.md to project-notes.md.”
- “Zip that project folder so I can download it.”
- “Download the ZIP from my project's Drive folder, unpack it locally, and review its text files.”
- “Upload this local report to the project's Drive folder.”

The browser supports local folder navigation, file/ZIP uploads, text previews with pagination,
downloads, new folders, ZIP inspection/extraction, and handing a file to chat for editing.

## Drive and local files

`local_file_import_drive` downloads a binary/text/ZIP from an authorized linked project folder.
Use native project tools for Google Docs and Sheets. `local_file_export_drive` uploads a local
file as a new Drive file. Transfers currently use the Drive adapter's 10 MB limit.

Local folders and Drive folders are separate stores. Local edits and extracted files are not
automatically mirrored to Drive. Existing automatic task-output uploads remain unchanged.

## Limits and operation behavior

- Local uploads/downloads and ZIP inputs: 50 MB. Text reads: 2 MB UTF-8, returned in pages.
- ZIP extraction: up to 2,000 entries, 100 MB total expanded, 50 MB per file, with a compression
  ratio limit. Encrypted archives, special entries, duplicate/case-colliding paths and unsafe
  paths are rejected. Formats such as RAR and 7z are not implemented.
- Extraction validates every entry, writes into a temporary staging folder, then publishes
  into a new destination. It never merges into or overwrites an existing destination. Cancelled
  or malformed extraction cleans up staging and leaves the destination unpublished.
- Editing requires the last read content hash. Exact replacement must match once. Simon keeps
  private pre-edit bytes under `.internal/versions/<previous-hash>` for administrative recovery.
  External processes can still race the final filesystem replacement; avoid simultaneous edits.
- Moves currently support files on the same filesystem. Existing destinations are never
  overwritten. Folder renaming, cross-volume moves, deletion and shell execution are not exposed.
- Successful tool/UI requests have durable idempotency receipts. A process crash between filesystem
  publication and receipt commit can leave a completed file without its receipt; a retry fails
  conservatively on the existing path or changed revision. Inspect the result before a new request.
- Binary files can be uploaded, downloaded, moved and archived. Local PDF/image/Office content
  parsing and editing are not included; native Google Docs/Sheets keep their existing tools.
- File content is untrusted data. Extracting an archive does not run any of its programs.

Validation covers memory/PostgreSQL service behavior, access boundaries, stale revisions,
duplicate requests, unsafe ZIPs, cancellation, Drive transfers, HTTP/CSRF, an actual browser
upload/extract/read flow, and an opt-in live model test using only temporary synthetic files.
