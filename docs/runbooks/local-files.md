# Account-scoped local files

`LocalFileService` provides generic server-side files through `/v1/local-files` and
the enabled chat local tools. These files are separate from native project knowledge,
artifacts and team storage, which need an explicit project file integration.

The `workspace` root belongs to one actor in one workspace under
`SIMON_LOCAL_FILES_DIR`. Only `SIMON_LOCAL_FILES_ACTOR_ID` (defaulting to the configured
site administrator) may use configured host folder aliases. Other workspace members
do not inherit that host access. A `project:<id>` root is not accepted.

```dotenv
SIMON_LOCAL_FILES_ENABLED=true
SIMON_LOCAL_FILES_DIR=.local/files
SIMON_LOCAL_FILE_ROOTS={}
```

An empty host-root mapping uses the server user's Desktop, Documents and Downloads.
An operator may supply an explicit mapping instead. Restart API and background chat
worker after changing process configuration. Listing roots reports only authorized
locations; file operations accept a root alias plus a relative path, never arbitrary
absolute model-supplied paths. Runtime agent tools accept only the account workspace.

The service supports bounded listing/search/text reads, revision-checked text writes
and edits, folder creation, move, ZIP inspection/extraction/creation, authenticated
download and restricted preview. ZIP extraction creates a new destination and rejects
traversal, links, collisions and excessive sizes before publication. Server checkout,
credential files, private versions, symlinks and junctions are excluded from host access.

Authenticated mutations require CSRF, live permissions and idempotency keys. Reads and
downloads revalidate identity and the resolved location before returning bytes. Keep
unknown write outcomes visible. Downloaded content and archive text are untrusted data.

Managed files and their versions belong in [recovery backups](storage-recovery.md).
Host folders and cloud accounts require separate backup policies. There is no automatic
Drive folder creation, project replication, or project-bound import/export API.
