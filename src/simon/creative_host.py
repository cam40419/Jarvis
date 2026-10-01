"""Native application operations and a single-request host pump.

This module uses only the standard library until a vendor adapter is selected, so
it can run inside vendor Python interpreters without installing Simon there.
Call ``pump`` from the host's main thread; never invoke vendor APIs on a timer thread.
"""

import importlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any
from uuid import UUID

EXTENSIONS = {
    "fusion": (".f3d", ".step"),
    "houdini": (".hip",),
    "rhino": (".3dm",),
    "cinema4d": (".c4d",),
    "solidworks": (".sldprt", ".sldasm", ".slddrw", ".step"),
    "photoshop": (".psd",),
    "premiere": (".prproj",),
}


def native(application: str, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Inspect or save the active document through the installed vendor API."""
    path = str(arguments.get("output", ""))
    if application == "fusion":
        core = importlib.import_module("adsk.core")
        fusion = importlib.import_module("adsk.fusion")
        app = core.Application.get()
        design = fusion.Design.cast(app.activeProduct)
        if design is None:
            raise ValueError("An active Fusion design is required")
        if operation == "inspect":
            return {
                "document": app.activeDocument.name,
                "parameters": [
                    {"name": p.name, "expression": p.expression}
                    for p in list(design.userParameters)[:200]
                ],
            }
        if operation == "set_parameter":
            parameter = design.userParameters.itemByName(arguments["name"])
            if parameter is None:
                raise ValueError("Unknown user parameter")
            parameter.expression = arguments["expression"]
            return {"name": parameter.name, "expression": parameter.expression}
        manager = design.exportManager
        options = (
            manager.createSTEPExportOptions(path)
            if path.endswith(".step")
            else manager.createFusionArchiveExportOptions(path)
        )
        if not manager.execute(options):
            raise RuntimeError("Fusion export failed")
    elif application == "houdini":
        hou = importlib.import_module("hou")
        if operation == "inspect":
            return {
                "document": hou.hipFile.path(),
                "nodes": [node.path() for node in hou.node("/obj").children()[:200]],
            }
        if operation == "set_parameter":
            parameter = hou.parm(arguments["name"])
            if parameter is None:
                raise ValueError("Unknown parameter")
            parameter.set(arguments["value"])
            return {"name": parameter.path(), "value": parameter.eval()}
        hou.hipFile.save(file_name=path)
    elif application == "rhino":
        rhino = importlib.import_module("Rhino")
        document = rhino.RhinoDoc.ActiveDoc
        if document is None:
            raise ValueError("An active Rhino document is required")
        if operation == "inspect":
            return {"document": document.Name, "objects": document.Objects.Count}
        options = rhino.FileIO.FileWriteOptions()
        options.SuppressDialogBoxes = True
        options.WriteSelectedObjectsOnly = False
        if not document.WriteFile(path, options):
            raise RuntimeError("Rhino save failed")
    elif application == "cinema4d":
        c4d = importlib.import_module("c4d")
        document = c4d.documents.GetActiveDocument()
        if document is None:
            raise ValueError("An active Cinema 4D document is required")
        if operation == "inspect":
            return {
                "document": document.GetDocumentName(),
                "objects": [obj.GetName() for obj in document.GetObjects()[:200]],
            }
        if not c4d.documents.SaveDocument(
            document, path, c4d.SAVEDOCUMENTFLAGS_0, c4d.FORMAT_C4DEXPORT
        ):
            raise RuntimeError("Cinema 4D save failed")
    elif application == "solidworks":
        client = importlib.import_module("win32com.client")
        com = importlib.import_module("pythoncom")
        app = client.GetActiveObject("SldWorks.Application")
        document = app.ActiveDoc
        if document is None:
            raise ValueError("An active SOLIDWORKS document is required")
        if operation == "inspect":
            return {"document": document.GetTitle(), "type": document.GetType()}
        native_extension = {1: ".sldprt", 2: ".sldasm", 3: ".slddrw"}[document.GetType()]
        if Path(path).suffix != native_extension and (
            Path(path).suffix != ".step" or document.GetType() == 3
        ):
            raise ValueError("Output format does not match the active document")
        errors = client.VARIANT(com.VT_BYREF | com.VT_I4, 0)
        warnings = client.VARIANT(com.VT_BYREF | com.VT_I4, 0)
        document.ClearSelection2(True)
        if not document.Extension.SaveAs(path, 0, 1 | 2, None, errors, warnings):
            raise RuntimeError("SOLIDWORKS save failed")
        return {"output": path, "warnings": warnings.value}
    else:
        raise ValueError("Use the Adobe UXP host for this application")
    return {"output": path}


def validate(request: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    """Bind a request to the runner's session and confine output to its workspace."""
    UUID(request["invocation_id"])
    if (
        request["version"] != 1
        or request["lease_id"] != session["lease_id"]
        or (request["fencing_token"] != session["fencing_token"])
        or request["application"] != session["application"]
    ):
        raise ValueError("Request does not own this application session")
    if not all(
        math.isfinite(value) for value in (request["expires_at"], session["expires_at"])
    ) or (time.time() >= min(request["expires_at"], session["expires_at"])):
        raise ValueError("Session or request expired")
    application = session["application"]
    operation = request["operation"]
    arguments = dict(request["arguments"])
    if operation == "inspect" and not arguments:
        return arguments
    if operation == "save" and set(arguments) == {"output"}:
        # Only a basename is accepted; nested folders, links and overwrite are excluded.
        name = arguments["output"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,110}\.[a-z0-9]+", name):
            raise ValueError("Output must be a filename")
        if Path(name).suffix not in EXTENSIONS[application]:
            raise ValueError("Unsupported output format")
        workspace = Path(session["workspace"]).resolve(strict=True)
        output = workspace / name
        if output.exists() or output.is_symlink():
            raise ValueError("Output already exists")
        arguments["output"] = str(output)
        return arguments
    if operation == "set_parameter":
        if (
            application == "fusion"
            and set(arguments) == {"name", "expression"}
            and all(isinstance(v, str) and 0 < len(v) <= 1000 for v in arguments.values())
        ):
            return arguments
        if (
            application == "houdini"
            and set(arguments) == {"name", "value"}
            and (
                isinstance(arguments["name"], str)
                and arguments["name"].startswith("/obj/")
                and type(arguments["value"]) in (int, float)
                and math.isfinite(arguments["value"])
            )
        ):
            return arguments
    raise ValueError("Unsupported operation or arguments")


def pump(directory: str, application: str) -> bool:
    """Consume one request on the application's main thread; retain durable receipts.

    The runner owns this directory and session.json. It must quiesce the host
    before changing ownership, and must not expose the directory to agent scripts.
    """
    root = Path(directory)
    pending = root / "request.json"
    if not pending.exists():
        return False
    request = json.loads(pending.read_text(encoding="utf-8"))
    identity = str(UUID(request["invocation_id"]))
    receipt = root / (identity + ".claimed")
    with receipt.open("x", encoding="utf-8") as stream:
        stream.write("dispatched")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        session = json.loads((root / "session.json").read_text(encoding="utf-8"))
        if session["application"] != application:
            raise ValueError("Mailbox does not match this host")
        arguments = validate(request, session)
        result = native(session["application"], request["operation"], arguments)
        if request["operation"] == "save" and not Path(arguments["output"]).is_file():
            raise RuntimeError("Application did not produce the requested output file")
        response = {"ok": True, "result": result}
    except Exception:
        response = {"ok": False, "error": "Native operation failed; inspect before retrying"}
    temporary = root / (identity + ".tmp")
    temporary.write_text(json.dumps(response, allow_nan=False), encoding="utf-8")
    pending.unlink()
    temporary.replace(root / (identity + ".result.json"))
    return True
