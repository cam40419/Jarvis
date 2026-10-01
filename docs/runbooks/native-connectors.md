# Native creative connectors

Simon includes native adapters for Fusion, Houdini, Rhino, Cinema 4D, SOLIDWORKS,
Photoshop, and Premiere. They operate on the document already open in a dedicated
application session. They are opt-in and have contract tests; no licensed host
has been used for end-to-end acceptance yet.

| Application    | Implemented operations                                                      | Host entrypoint            |
| -------------- | --------------------------------------------------------------------------- | -------------------------- |
| Fusion         | Inspect user parameters, set a parameter expression, export F3D or STEP     | Python add-in              |
| Houdini        | Inspect top-level object nodes, set a numeric `/obj/` parameter, save HIP   | Main-thread event callback |
| Rhino 8        | Inspect document/object count, write 3DM                                    | Python 3 idle callback     |
| Cinema 4D      | Inspect top-level objects, save C4D                                         | Python dialog timer        |
| SOLIDWORKS     | Inspect document/type, save native copy or export STEP for parts/assemblies | Windows COM worker         |
| Photoshop      | Inspect document/layers, save PSD copy                                      | UXP panel                  |
| Premiere 25.6+ | Inspect project, save PRPROJ                                                | UXP panel                  |

Saving in some hosts, including Premiere and Houdini, changes the active document's
path. Work in disposable task sessions. CAD exports may require particular document
types or licenses. The connectors do not create arbitrary geometry, edit timelines,
render video, open untrusted files, or execute model-supplied scripts. InDesign,
Media Encoder, and other Adobe products still need their own adapters.

## Provision the runner

Use the [machine lease protocol](agent-environments.md) with an exclusive interactive
account or VM, the installed licensed application, and a task workspace. Simon still
does not ship the machine runner server. A cloud API key alone does not provide a
desktop application installation or license.

Load the disabled [native tool manifest](../../examples/agents/creative-tools.example.json),
set the worker Python path and runner origin, and enable only the applications you
have provisioned. Grant each tool explicitly to the agent. Each tool requires
`application.control`, its `application.<name>` capability, and `jobs:write`.
The runner routes each fixed application argument to that application's mailbox
via `SIMON_CREATIVE_MAILBOX`. Do not accept a mailbox path from model arguments.

Create a private local mailbox outside the agent workspace. Restrict it to the
trusted runner and host identities, excluding other sessions and agent-executed
code. Do not put it on a network share. The runner writes `session.json` atomically
from its ownership journal, with this structure:

```json
{
  "application": "fusion",
  "lease_id": "00000000-0000-0000-0000-000000000001",
  "fencing_token": 1,
  "workspace": "C:/Simon/workspaces/task-123",
  "expires_at": 1790900000
}
```

Use the real lease UUID, positive fencing token, canonical absolute workspace path,
and future Unix expiry. Refresh the expiry while the owned lease is healthy. Start
the bridge command with that workspace as its working directory. The runner must
stop and quiesce the host before changing lease ownership: checking a fencing token
cannot interrupt an SDK operation already in progress. Allow only one invocation
at a time across all applications sharing that desktop/workspace.

The command bridge publishes a request and waits up to 45 seconds. A host validates
the current session before invoking its native API. Requests expire, output names
are restricted to basenames and supported extensions, and existing files are refused.
The workspace must remain exclusive for the entire operation, including saving.
Long saves may outlive the command timeout and require reconciliation.

## Install a host

Copy [creative_host.py](../../src/simon/creative_host.py) next to each Python host
entrypoint. Add that directory to the vendor interpreter's import path if its script
editor does not do so. This module uses the standard library and needs Python 3.10+
in the application. Keep the code outside agent-writable directories.

- **Fusion:** copy the files in [fusion](../../examples/connectors/fusion) into an
  add-in directory named `SimonBridge`, with `creative_host.py` beside them. Set
  `SIMON_CREATIVE_MAILBOX` before starting Fusion. Register and run it through
  Scripts and Add-Ins. The timer only posts a custom event; API calls run on Fusion's
  main thread. Stop the add-in before releasing the session.
- **Houdini, Rhino, Cinema 4D:** set `SIMON_CREATIVE_MAILBOX` and
  `SIMON_CREATIVE_APPLICATION` (`houdini`, `rhino`, or `cinema4d`) before launch.
  Run [host.py](../../examples/connectors/python/host.py) once from the application's
  Python script editor. Use Python 3 in Rhino. Keep Cinema 4D's bridge dialog open.
  Stop/restart the application to remove callbacks between leases; do not register
  duplicate callbacks. Houdini's entrypoint requires the GUI, not hython.
- **SOLIDWORKS:** install `python -m pip install -e ".[solidworks]"` in the worker
  environment. Start exactly one SOLIDWORKS instance in the dedicated account,
  set the same two variables with application `solidworks`, and launch `host.py`
  using that Python. It attaches with `GetActiveObject`; it does not launch an
  application or select among multiple instances. Stop the worker and application
  at lease release.
- **Photoshop/Premiere:** load [adobe/manifest.json](../../examples/connectors/adobe/manifest.json)
  with Adobe UXP Developer Tool, show the Simon Bridge panel, and click **Connect
  folders**. Select the mailbox first, then the exact task workspace. Keep the panel
  running. Disconnect before changing session ownership. The panel requests folder
  access, uses native save APIs, and requires no network permission. Photoshop 25+
  and Premiere 25.6+ are declared minimums, pending installed-host acceptance.

## Recovery and acceptance

The mailbox's exclusive `busy` file serializes command dispatch. An invocation's
`.claimed` receipt is created before SDK access; the same invocation ID cannot be
replayed. Success produces a `.result.json` receipt. Failures and timeouts retain
`busy`, so another command fails closed. Never clear it just because time elapsed.
Stop/quiesce the application, inspect its document and output files, reconcile the
lease, then let the runner archive the mailbox and provision a fresh session.
Keep receipts for the run's audit retention period. Neither mailbox nor native files
are automatically published as Simon artifacts yet.

Before enabling unattended use, test each installed version on a disposable document:
inspect, supported parameter edit, save/export and reopen, overwrite rejection,
stale lease rejection, duplicate invocation, timeout during saving, missing license,
and host restart. Confirm an unexpected modal dialog cannot affect another session.
Record the exact application/version/license and reopen results; passing simulated
tests does not certify file fidelity.

Local regression commands:

```powershell
python -m pytest tests/unit/test_creative_connectors.py tests/unit/test_application_tools.py
node --test tests/connectors/adobe.test.cjs
```

Native API references: [Fusion export manager](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/fusion_ExportManager_execute.htm),
[Houdini HIP files](https://www.sidefx.com/docs/houdini/hom/hou/hipFile.html),
[Rhino document writing](https://developer.rhino3d.com/api/RhinoCommon/html/M_Rhino_RhinoDoc_WriteFile.htm),
[Cinema 4D Python SDK](https://developers.maxon.net/docs/py/),
[SOLIDWORKS SaveAs](https://help.solidworks.com/2019/english/api/sldworksapi/SOLIDWORKS.Interop.sldworks~SOLIDWORKS.Interop.sldworks.IModelDocExtension~SaveAs.html),
[Photoshop documents](https://developer.adobe.com/photoshop/uxp/ps_reference/classes/document/),
and [Premiere projects](https://developer.adobe.com/premiere-pro/uxp/ppro-reference/classes/project).
