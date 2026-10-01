# Work dashboard and installed tool adapters

Start with the [project workspace](project-teams.md) to define a team and lead, delegate a
request, or configure ongoing work. [External action review](external-actions.md) covers
booking, order, reservation and prerecorded phone-message proposals and provider setup.

Work now includes an agent dashboard alongside existing projects, sessions and
local/Drive files. Build a plan from one or more tasks, choose profiles, assign
dependencies, inspect blockers and model selections, then explicitly start a run.
Independent tasks run concurrently within configured limits. Refresh/reload keeps
saved run state; cancellation prevents subsequent dispatch. Downloads include
final answers and published deliverables.

## Initial server configuration

```powershell
.\venv\Scripts\python.exe -m pip install -e '.[dev,postgres]'
.\venv\Scripts\python.exe -m simon.agent_setup --output .local/agent-platform.json
```

The setup command refuses to overwrite an existing manifest. It copies the
configured chat model ID into a text/tools endpoint, using the existing
`SIMON_OPENAI_API_KEY` reference. It does not copy the credential into the manifest,
call a provider, or change `.env`. Non-OpenAI deployments start with that example
endpoint disabled and must configure a supported local/API endpoint.

Review the generated profiles and set in both API and dispatcher environments:

```dotenv
SIMON_STORAGE_BACKEND=postgres
SIMON_AGENT_MANIFEST_FILE=.local/agent-platform.json
SIMON_AGENT_EXECUTION_ENABLED=true
```

Use an absolute manifest and state path for permanent deployment. Restart both
API and dispatcher after configuration changes; existing plans reject a changed
manifest and must be recreated. The starter includes file research/writing,
connected Google research, repository development, document/media, web preview,
connected storage, creative and engineering teams. Optional provider profiles remain blocked
until their tools and credentials are configured.
Google still needs the account connection described in [Google setup](google.md).

Model tier/context values are conservative configuration declarations to evaluate
with your actual model. Token prices are intentionally unset. Add current provider
rates before using estimated model-budget ceilings; unknown rates reject a ceiling.
Leaving the dashboard's budget blank uses the server policy and does not impose a
monetary cap. Model-step and tool-call limits still apply. Tool/provider charges
are not included in the model estimate.

## Coding and processing workers

```powershell
docker build -f deploy/Dockerfile.git-worker -t simon-coding:local .
docker build -f deploy/Dockerfile.processing-worker -t simon-processing:local .
docker build -f deploy/Dockerfile.browser-worker -t simon-browser:local .
docker build -f deploy/Dockerfile.cad-worker -t simon-cad:local .
docker build -f deploy/Dockerfile.pcb-worker -t simon-pcb:local .
```

Enable the corresponding `coding`/`processing` environment entries in the private
manifest only after the image is installed and its smoke test passes. These
containers have networking disabled and one private working directory. The
developer profile has bounded Python execution, structured local Git operations
and local-file import. Processing supports PDF text, document conversion, OCR,
media inspection, thumbnails, audio extraction and video conversion.

`browser-offline` renders local HTML with scripts and networking disabled.
`browser-web` is separately configured with bridge networking and exact permitted
HTTPS origins. Public-page reads/screenshots validate destinations and redirects;
interactive forms, authentication and arbitrary page scripts are unsupported.

The Engineering team includes **3D designer** and **PCB engineer** profiles. Their
offline `cad` and `pcb` environments must be enabled after the respective real
Docker smoke tests pass. The 3D worker supports OpenSCAD STL/3MF export, mesh checks,
and Blender PNG/scene rendering. The KiCad worker supports ERC, DRC, PDF, netlist,
BOM, board SVG, Gerber and drill exports. Source creation uses the same bounded
workspace Python tool. Checks report actual violations; exports alone do not
establish printability or electrical/hardware correctness.

`workspace.import_local` copies a file from the caller's managed workspace or
authorized project into a uniquely named Docker input file and returns its hash.
It does not expose host folders or alter the original. Agents should use the
returned relative filename in their subsequent tools.

A tool-using worker can finish with:

```json
{"type":"final","output":"Created the deliverables.","artifacts":["report.csv","src/main.py"]}
```

The dispatcher stops the owned container before collecting files. One file is
published directly; multiple files become a ZIP preserving their relative paths
with `simon-deliverables.json` recording hashes and provenance. Downloads are
authorized against the saved run. Collection allows up to 16 files, with a 50 MiB
total artifact limit including ZIP metadata. Paths outside the workspace,
filesystem redirects, repository internals and common credential files are rejected.
File collection from remote machine runners is not implemented.

Dependent tasks receive predecessors' final text and controller-generated artifact
references. Within a project, explicitly granted `project.outputs` and
`project.output_read` tools list saved outputs and read bounded UTF-8 text from successful
tasks. `workspace.import_artifact` copies an authorized output into the current Docker
workspace for binary/large-file processing, returning a relative path and verified hash.
The full prior workspace is not copied; ZIP bundles are imported without extraction.
Review findings must state which files and checks were actually inspected.

Use **Project → Files → Generated outputs → Save to project** to make an editable local
copy. The immutable original remains available, with a receipt linking the copy to its
run and source hash. Existing different files require their current revision before
replacement. The `project.output_save` skill provides the same operation to authorized
agents. **Use in next task** adds the saved file reference to the lead's draft instruction;
the user still submits that task. Previous runs remain accessible after profile changes.

For existing installations, add the canonical tool definitions and chosen grants to the
manifest and restart API/dispatcher together. Existing individually configured project
members and custom profiles retain their saved grants; select the new skills explicitly
where needed. A skill being installed does not grant it to every agent.

## Start the dispatcher

```powershell
.\scripts\start-agent-dispatcher.ps1 -Check
.\scripts\install-agent-task.ps1 -Start
```

`Simon-Agents` is separate from `Simon-Workflow` (assistant tasks/sessions).
The installer uses the current Windows user's logon session and hidden launcher,
with automatic restart after failure. Docker Desktop and the user session must
be available. It does not supply service before Windows sign-in. For another OS,
run `python -m simon.agent_dispatcher` under its service manager with the same
configuration/database/state directory as the API.

See [dispatcher lifecycle](dispatcher-lifecycle.md) for graceful stops, startup
logs and recovery after interrupted execution.

## Integration readiness

The catalog distinguishes disabled, unconfigured, missing-permission and
configured tools. Configuration checks do not prove external service health,
model access, installed binaries or license availability. Execution rechecks
permissions and reports actual failures; unsupported work is never represented
as completed.

Implemented adapters include:

| Integration | Available behavior |
| --- | --- |
| Native files | Managed/project read, edit, move, folders, ZIP operations |
| Native Google | Account selection; Drive/Gmail/Calendar reads; linked project file/Docs/Sheets operations |
| Git | Local status/diff/log/branches/init/branch/switch/add/commit inside Docker |
| Python | Bounded computation, source edits and installed tests inside Docker |
| Documents/media | PDF extraction, conversion, OCR and bounded FFmpeg operations |
| Browser | Static allowlisted HTTPS reads/screenshots; offline HTML previews |
| GitHub | Repository, issue and pull-request reads; create issues and draft PRs |
| WebDAV | Root-scoped listing/read and conditional create/update |
| Dropbox | Root-scoped listing/search/metadata/read and revision-checked upload |
| Box | Folder-scoped listing, metadata and file reads |
| OneDrive/SharePoint | Graph drive/folder-scoped listing, metadata and file reads |
| Image generation | One PNG per request using an explicitly configured OpenAI model |
| Audio transcription | Bounded PCM WAV transcription to a workspace text artifact |
| HTTP JSON | Operator-configured fixed endpoint and validated tool schema |
| MCP | Fixed remote tool calls over Streamable HTTP, pinned protocol 2025-11-25 |

Native Google/file access is scoped to the acting account. Tools are explicitly
granted to profiles; catalog entries do not grant account permissions. Local-only
tasks reject network-connected tools and network-enabled execution environments.

Optional cloud adapters use server-side credentials and explicit workspace/actor
grants. They are disabled in the starter. Google uses each user's existing account
connection; the other storage adapters currently require operator-managed access
tokens, not an in-app OAuth enrollment/refresh flow. Image and transcription calls
incur separate provider charges, which are outside the text-model budget estimate.

Other CAD applications, unrestricted desktop/browser control, additional providers
and distributed runners still require concrete application adapters and provisioning.
The generic disabled templates remain visible as setup targets, not installed
capabilities. Account connections in Codex are not inherited by the Simon server.

See [storage/recovery](storage-recovery.md), [agent execution](agent-execution.md),
[Git tools](agent-git.md), [document/media tools](agent-processing.md),
[browser tools](agent-browser.md), [creative tools](agent-generative.md),
[3D/CAD tools](agent-cad.md), [PCB tools](agent-pcb.md),
[GitHub/WebDAV](optional-github-webdav.md), [cloud storage](optional-cloud-storage.md),
[dispatcher lifecycle](dispatcher-lifecycle.md), and [agent environments](agent-environments.md).
