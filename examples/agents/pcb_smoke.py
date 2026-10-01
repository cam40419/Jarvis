"""Run every isolated KiCad tool against the included camera-mux project, without model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from uuid import uuid4

from simon.adapters.environment_tools import EnvironmentCommandTransport
from simon.adapters.pcb_tools import PCB_CAPABILITIES, PCBToolTransport, pcb_tool_definitions
from simon.adapters.tool_transports import TransportRegistry
from simon.agent_setup import starter_manifest
from simon.config import Settings
from simon.domain.execution import EnvironmentDefinition, EnvironmentRequest
from simon.domain.tool_catalog import ToolExecutionContext
from simon.services.execution import EnvironmentManager

PROJECT = Path(__file__).resolve().parents[1] / "hardware" / "pi5-camera-mux"
NAME = "pi5-camera-mux"
OUTPUTS = {
    "pcb.erc": (".kicad_sch", "erc.json"),
    "pcb.schematic_pdf": (".kicad_sch", "schematic.pdf"),
    "pcb.netlist": (".kicad_sch", "connectivity.net"),
    "pcb.bom": (".kicad_sch", "bom.csv"),
    "pcb.drc": (".kicad_pcb", "drc.json"),
    "pcb.board_svg": (".kicad_pcb", "board.svg"),
    "pcb.gerbers": (".kicad_pcb", "gerbers-review.zip"),
}


def smoke(*, write_review: bool = False) -> None:
    capabilities = PCB_CAPABILITIES | {"process.execute"}
    state = (Path(".local/agents") / ("pcb-smoke-" + uuid4().hex)).resolve()
    manager = EnvironmentManager([EnvironmentDefinition(
        id="pcb", kind="docker", enabled=True, container_image="simon-pcb:local",
        capabilities=capabilities, network="none", memory_mb=1024,
    )], state_path=state / "leases.sqlite3", workspace_root=state / "workspaces")
    request = EnvironmentRequest(workspace_id=uuid4(), agent_id="pcb", task_id=uuid4(),
                                 attempt_id=uuid4(), capabilities=capabilities, os="linux")
    lease = manager.allocate(request, environment_id="pcb")
    context = ToolExecutionContext(
        actor_id=uuid4(), household_id=request.workspace_id, run_id=uuid4(), agent_id="pcb",
        scopes=frozenset({"jobs:write"}), environment_capabilities=capabilities,
        allowed_tool_ids=frozenset(OUTPUTS) | {"workspace.python_execute"},
        authorized_action="write",
    )
    registry = TransportRegistry()
    registry.register("pcb", PCBToolTransport(manager, lease, actor_id=context.actor_id,
                                              run_id=context.run_id))
    registry.register("environment", EnvironmentCommandTransport(
        manager, lease, actor_id=context.actor_id, run_id=context.run_id,
    ))
    definitions = {tool.id: tool for tool in pcb_tool_definitions(enabled=True)}
    hashes = {}
    results = {}
    try:
        for path in PROJECT.iterdir():
            if path.suffix in {".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_sym"} or (
                path.name in {"sym-lib-table", "fp-lib-table"}
            ):
                hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
                shutil.copyfile(path, lease.plan.workspace_path / path.name)
        python_tool = next(tool for tool in starter_manifest(Settings.model_construct()).tools
                           if tool.id == "workspace.python_execute")
        board_script = (
            "import pcbnew; "
            f"board=pcbnew.LoadBoard({(NAME + '.kicad_pcb')!r}); "
            "assert len(board.GetFootprints()) == 38; "
            "pcbnew.SaveBoard('edited-copy.kicad_pcb', board); "
            "print('pcbnew board editing passed')"
        )
        edit = registry.execute(python_tool, {"args": ["-c",
            "import subprocess; subprocess.run(['/usr/bin/python3', '-c', "
            + repr(board_script) + "], check=True)",
        ]}, context)
        if edit.output["exit_code"] or "pcbnew board editing passed" not in edit.output["stdout"]:
            raise RuntimeError(f"Workspace-to-pcbnew invocation failed: {edit.output}")
        if not (lease.plan.workspace_path / "edited-copy.kicad_pcb").is_file():
            raise RuntimeError("pcbnew did not save an editable board")
        print("workspace.python_execute: pcbnew read/write completed")
        for operation, (extension, output) in OUTPUTS.items():
            result = registry.execute(definitions[operation], {
                "input": NAME + extension, "output": "review/" + output,
            }, context)
            code = result.output["exit_code"]
            allowed = {0, 5} if operation in {"pcb.erc", "pcb.drc"} else {0}
            if code not in allowed:
                raise RuntimeError(f"{operation}: {code}; {result.output['stderr']}; "
                                   f"{result.output['stdout']}")
            results[operation] = {"exit_code": code, "output": output}
            print(f"{operation}: completed (exit {code})")
        duplicate = registry.execute(definitions["pcb.bom"], {
            "input": NAME + ".kicad_sch", "output": "review/bom.csv",
        }, context)
        if duplicate.output["exit_code"] == 0:
            raise RuntimeError("Existing KiCad export was overwritten")
    finally:
        manager.release(lease.id, attempt_id=request.attempt_id, fencing_token=lease.fencing_token)
    review = lease.plan.workspace_path / "review"
    with zipfile.ZipFile(review / "gerbers-review.zip") as package:
        if not any(path.endswith(".drl") for path in package.namelist()):
            raise RuntimeError("Gerber package has no drill data")
    for name, digest in hashes.items():
        if hashlib.sha256((PROJECT / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("A source project file changed during the smoke")
    for operation in ("pcb.erc", "pcb.drc"):
        report = json.loads((review / OUTPUTS[operation][1]).read_text())
        if operation == "pcb.drc":
            results[operation]["counts"] = {key: len(report.get(key, [])) for key in (
                "violations", "unconnected_items", "schematic_parity",
            )}
        else:
            results[operation]["counts"] = {"violations": sum(
                len(sheet.get("violations", [])) for sheet in report.get("sheets", [])
            )}
    (review / "tool-smoke.json").write_text(json.dumps({
        "lease_released": True, "source_sha256": hashes, "operations": results,
        "workspace_python_pcbnew_edit": "passed using /usr/bin/python3 subprocess",
        "hardware_validation": "Not performed; see engineering review and board audit",
    }, indent=2), encoding="utf-8")
    if write_review:
        destination = PROJECT / "review"
        destination.mkdir(exist_ok=True)
        for path in review.iterdir():
            shutil.copyfile(path, destination / path.name)
        print(f"Review exports copied to {destination}")
    print(f"Lease released; source unchanged; output collision rejected. Evidence: {state}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-review", action="store_true",
                        help="Replace generated review outputs beside the included source project")
    smoke(write_review=parser.parse_args().write_review)
