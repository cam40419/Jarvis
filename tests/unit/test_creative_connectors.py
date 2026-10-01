"""Contract tests exercise native SDK calls without attaching to installed applications."""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from simon import creative_bridge, creative_host
from simon.adapters.application_tools import (
    application_configuration_reason,
    creative_tool_definitions,
)


@pytest.fixture
def mailbox(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    root = tmp_path / "mailbox"
    root.mkdir()
    session = {
        "lease_id": str(uuid4()),
        "fencing_token": 2,
        "application": "houdini",
        "workspace": str(workspace),
        "expires_at": time.time() + 100,
    }
    (root / "session.json").write_text(json.dumps(session))
    request = {
        "version": 1,
        "lease_id": session["lease_id"],
        "fencing_token": 2,
        "invocation_id": str(uuid4()),
        "application": "houdini",
        "operation": "inspect",
        "arguments": {},
        "expires_at": time.time() + 60,
    }
    return root, session, request


@pytest.mark.parametrize("application", creative_host.EXTENSIONS)
def test_tools_are_disabled_and_constrained(application):
    tools = creative_tool_definitions(python_executable="python", application=application)
    assert len(tools) >= 2
    for tool in tools:
        assert application_configuration_reason(tool) is None
        assert not tool.enabled
        assert f"application.{application}" in tool.environment_capabilities
        assert tool.settings["argv_prefix"][-1] == application
        assert tool.input_schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("fencing_token", 1),
        ("lease_id", str(uuid4())),
        ("application", "rhino"),
        ("expires_at", 0),
        ("expires_at", float("nan")),
        ("operation", "eval"),
        ("arguments", {"code": "arbitrary"}),
    ],
)
def test_host_rejects_unowned_or_invalid_requests(mailbox, field, value):
    _, session, request = mailbox
    request[field] = value
    with pytest.raises(ValueError):
        creative_host.validate(request, session)


@pytest.mark.parametrize("name", ["../x.hip", "C:\\x.hip", "x.py", "sub/x.hip", "x:ads.hip"])
def test_host_rejects_output_escape(mailbox, name):
    _, session, request = mailbox
    request.update(operation="save", arguments={"output": name})
    with pytest.raises(ValueError):
        creative_host.validate(request, session)


def test_host_rejects_overwrite(mailbox):
    _, session, request = mailbox
    (Path(session["workspace"]) / "existing.hip").touch()
    request.update(operation="save", arguments={"output": "existing.hip"})
    with pytest.raises(ValueError, match="already exists"):
        creative_host.validate(request, session)


def test_bridge_to_native_host_and_replay(mailbox, monkeypatch):
    root, _, request = mailbox
    call = MagicMock(return_value={"document": "scene.hip"})
    monkeypatch.setattr(creative_host, "native", call)
    monkeypatch.setattr(
        creative_bridge.time, "sleep", lambda _: creative_host.pump(str(root), "houdini")
    )
    result = creative_bridge.dispatch(json.dumps(request), root, "houdini")
    assert result == {"ok": True, "result": {"document": "scene.hip"}}
    call.assert_called_once_with("houdini", "inspect", {})
    assert not (root / "busy").exists()
    with pytest.raises(ValueError, match="already dispatched"):
        creative_bridge.dispatch(json.dumps(request), root, "houdini")
    assert call.call_count == 1


def test_unknown_result_retains_lock(mailbox):
    root, _, request = mailbox
    with pytest.raises(TimeoutError):
        creative_bridge.dispatch(json.dumps(request), root, "houdini", timeout=0.01)
    assert (root / "busy").exists()
    with pytest.raises(FileExistsError):
        creative_bridge.dispatch(json.dumps(request), root, "houdini")


def test_wrong_application_never_queues(mailbox):
    root, _, request = mailbox
    with pytest.raises(ValueError, match="another application"):
        creative_bridge.dispatch(json.dumps(request), root, "fusion")
    assert not (root / "request.json").exists()


def test_host_failure_is_receipted_without_provider_details(mailbox, monkeypatch):
    root, _, request = mailbox
    monkeypatch.setattr(creative_host, "native", MagicMock(side_effect=RuntimeError("secret")))
    (root / "request.json").write_text(json.dumps(request))
    assert creative_host.pump(str(root), "houdini")
    result = (root / (request["invocation_id"] + ".result.json")).read_text()
    assert '"ok": false' in result and "secret" not in result
    (root / "request.json").write_text(json.dumps(request))
    with pytest.raises(FileExistsError):
        creative_host.pump(str(root), "houdini")


@pytest.mark.parametrize(
    "application,extension",
    [
        ("fusion", ".f3d"),
        ("fusion", ".step"),
        ("houdini", ".hip"),
        ("rhino", ".3dm"),
        ("cinema4d", ".c4d"),
        ("solidworks", ".sldprt"),
    ],
)
def test_native_save_calls_vendor_api(application, extension, monkeypatch, tmp_path):
    sdk = MagicMock()
    monkeypatch.setattr(creative_host.importlib, "import_module", lambda _: sdk)
    path = str(tmp_path / ("output" + extension))
    sdk.GetActiveObject.return_value.ActiveDoc.GetType.return_value = 1
    result = creative_host.native(application, "save", {"output": path})
    assert result["output"] == path
    if application == "fusion":
        manager = sdk.Design.cast.return_value.exportManager
        method = (
            manager.createSTEPExportOptions
            if extension == ".step"
            else manager.createFusionArchiveExportOptions
        )
        method.assert_called_once_with(path)
        manager.execute.assert_called_once()
    elif application == "houdini":
        sdk.hipFile.save.assert_called_once_with(file_name=path)
    elif application == "rhino":
        sdk.RhinoDoc.ActiveDoc.WriteFile.assert_called_once_with(
            path, sdk.FileIO.FileWriteOptions.return_value
        )
    elif application == "cinema4d":
        sdk.documents.SaveDocument.assert_called_once_with(
            sdk.documents.GetActiveDocument.return_value,
            path,
            sdk.SAVEDOCUMENTFLAGS_0,
            sdk.FORMAT_C4DEXPORT,
        )
    else:
        sdk.GetActiveObject.assert_called_once_with("SldWorks.Application")
        sdk.GetActiveObject.return_value.ActiveDoc.Extension.SaveAs.assert_called_once()


def test_fusion_parameter_edit(monkeypatch):
    parameter = SimpleNamespace(name="width", expression="10 mm")
    sdk = MagicMock()
    sdk.Design.cast.return_value.userParameters.itemByName.return_value = parameter
    monkeypatch.setattr(creative_host.importlib, "import_module", lambda _: sdk)
    assert creative_host.native(
        "fusion", "set_parameter", {"name": "width", "expression": "20 mm"}
    ) == {"name": "width", "expression": "20 mm"}


def test_houdini_parameter_edit(monkeypatch):
    sdk = MagicMock()
    monkeypatch.setattr(creative_host.importlib, "import_module", lambda _: sdk)
    creative_host.native("houdini", "set_parameter", {"name": "/obj/box/scale", "value": 2.0})
    sdk.parm.assert_called_once_with("/obj/box/scale")
    sdk.parm.return_value.set.assert_called_once_with(2.0)
