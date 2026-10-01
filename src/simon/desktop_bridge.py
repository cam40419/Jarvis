"""Opt-in UI Automation bridge, launched only by a dedicated Windows machine runner.

The runner owns authentication, exclusive desktop ownership, fencing and timeouts.
SIMON_DESKTOP_WINDOW_HANDLE is selected by the operator/runner, never by model input.
No fallback to the foreground window, mouse coordinates, shell or global keyboard exists.
"""

import hashlib
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from simon.domain.models import StrictModel


class BridgeRequest(StrictModel):
    version: Literal[1]
    operation: Literal["inspect", "invoke", "set_text"]
    arguments: dict[str, Any]
    invocation_id: UUID
    lease_id: UUID
    fencing_token: int = Field(ge=1)


class ControlAction(StrictModel):
    control_id: tuple[int, ...] = Field(min_length=1, max_length=16)
    expected_state: str = Field(pattern=r"^[a-f0-9]{64}$")


class SetText(ControlAction):
    text: str = Field(max_length=4000)


def snapshot(window: Any) -> tuple[dict[str, Any], dict[tuple[int, ...], Any]]:
    controls = {}
    rows = []
    size = 0
    for control in [window, *window.descendants(depth=6)][:200]:
        info = control.element_info
        if info.process_id != window.element_info.process_id or info.element.CurrentIsPassword:
            continue
        identity = tuple(info.runtime_id)
        row = {
            "control_id": identity,
            "name": control.window_text()[:160],
            "type": info.control_type,
            "enabled": control.is_enabled(),
            "visible": control.is_visible(),
        }
        size += len(json.dumps(row))
        if size > 24000:
            break
        controls[identity] = control
        rows.append(row)
    data = {
        "window": int(window.handle),
        "process": window.element_info.process_id,
        "controls": rows,
    }
    state = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return {**data, "state": state, "limit": 200}, controls


def execute(request: BridgeRequest, window: Any, workspace: Path) -> dict[str, Any]:
    view, controls = snapshot(window)
    if request.operation == "inspect":
        if request.arguments:
            raise ValueError("Inspect takes no arguments")
        return view
    action = (SetText if request.operation == "set_text" else ControlAction).model_validate(
        request.arguments
    )
    if action.expected_state != view["state"]:
        raise ValueError("Application changed; inspect it again before acting")
    control = controls.get(action.control_id)
    if control is None or not control.is_enabled() or not control.is_visible():
        raise ValueError("Control is unavailable")
    # Persist admission before interacting. A repeated invocation never replays a
    # UI mutation, including when the previous process died before saving its result.
    receipt = workspace / f"desktop-invocation-{request.invocation_id}.jsonl"
    with receipt.open("x", encoding="utf-8") as output:
        json.dump({"invocation_id": str(request.invocation_id), "status": "dispatched"}, output)
        output.flush()
        os.fsync(output.fileno())
        if isinstance(action, SetText):
            control.set_edit_text(action.text)
        else:
            control.invoke()
        output.write("\n")
        json.dump({"invocation_id": str(request.invocation_id), "status": "completed"}, output)
        output.flush()
        os.fsync(output.fileno())
    return {"status": "completed", "invocation_id": str(request.invocation_id)}


def main() -> int:
    if sys.platform != "win32" or os.environ.get("SIMON_DESKTOP_ENABLED") != "1":
        raise ValueError("Desktop bridge requires an explicitly enabled Windows runner")
    request = BridgeRequest.model_validate_json(sys.argv[1])
    # The trusted runner must inject these values from its current owned lease.
    if str(request.lease_id) != os.environ.get("SIMON_DESKTOP_LEASE_ID") or str(
        request.fencing_token
    ) != os.environ.get("SIMON_DESKTOP_FENCING_TOKEN"):
        raise ValueError("Desktop request does not match the runner's owned lease")
    handle = int(os.environ["SIMON_DESKTOP_WINDOW_HANDLE"])
    process_id = int(os.environ["SIMON_DESKTOP_PROCESS_ID"])
    desktop = importlib.import_module("pywinauto").Desktop(backend="uia")
    window = desktop.window(handle=handle).wrapper_object()
    if window.element_info.process_id != process_id:
        raise ValueError("Application window ownership changed")
    print(json.dumps(execute(request, window, Path.cwd())))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # UI/provider exceptions can contain document text. Keep worker logs generic.
        print("Desktop operation failed; inspect the session before retrying", file=sys.stderr)
        sys.exit(2)
