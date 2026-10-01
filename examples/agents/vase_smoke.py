"""Build, inspect and render LILT using the real isolated CAD transport; no provider calls."""

import json
from pathlib import Path
from uuid import uuid4

from simon.adapters.cad_tools import CAPABILITIES, CadToolTransport, cad_tool_definitions
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.execution import EnvironmentDefinition, EnvironmentRequest, ExecutionCommand
from simon.domain.tool_catalog import ToolExecutionContext
from simon.services.execution import EnvironmentManager

DELIVERY_README = """LILT - twisted, fluted decorative vase

The model is 180 mm tall and approximately 98.7 mm at its widest point, with
18 flutes and a 100-degree twist. It has nominal 2.4 mm walls and a 3 mm base.

Files
  lilt-vase.scad: editable OpenSCAD source, with named design parameters.
  lilt-vase.stl: printable triangle mesh; import using millimeters.
  lilt-vase.3mf: the same geometry in a units-aware 3MF container.
  lilt-vase.blend: editable Blender presentation scene and materials.
  lilt-vase.png: real Blender render of the exported geometry.
  geometry-validation.json: independent STL/3MF topology and dimension checks,
    plus 512 sampled wall measurements and centerline base/mouth checks.

Printing
  Place the flat base on the build plate. Use normal solid printing settings;
  this mesh already includes inner and outer walls, so spiral-vase mode is not
  appropriate. Review the sliced layer preview before printing. Printer,
  material, nozzle, and layer settings affect the result. Use a liner for water
  until the physical print has been tested for leaks. The supplied checks
  validate this mesh; no physical print or exhaustive manufacturing test has
  been performed.

Generated and validated locally inside the isolated, offline CAD worker.
"""


def main() -> None:
    state = (Path(".local/agents") / ("vase-smoke-" + uuid4().hex)).resolve()
    manager = EnvironmentManager(
        [EnvironmentDefinition(
            id="cad-smoke", kind="docker", enabled=True, container_image="simon-cad:local",
            capabilities=CAPABILITIES, network="none", cpu_limit=2, memory_mb=4096,
        )], state_path=state / "leases.sqlite3", workspace_root=state / "workspaces",
    )
    request = EnvironmentRequest(
        workspace_id=uuid4(), agent_id="cad-smoke", task_id=uuid4(), attempt_id=uuid4(),
        capabilities=CAPABILITIES, os="linux",
    )
    lease = manager.allocate(request, environment_id="cad-smoke")
    ownership = {"attempt_id": request.attempt_id, "fencing_token": lease.fencing_token}
    context = ToolExecutionContext(
        actor_id=uuid4(), household_id=request.workspace_id, run_id=uuid4(), agent_id="cad-smoke",
        scopes=frozenset({"jobs:read", "jobs:write"}), authorized_action="write",
        allowed_tool_ids=frozenset(item.id for item in cad_tool_definitions()),
        environment_capabilities=CAPABILITIES,
    )
    registry = TransportRegistry()
    registry.register("cad", CadToolTransport(
        manager, lease, actor_id=context.actor_id, run_id=context.run_id, max_timeout_seconds=300,
    ))
    definitions = {tool.id: tool for tool in cad_tool_definitions(enabled=True)}
    reports = {}
    try:
        source = Path(__file__).with_name("spiral_vase.scad").read_text(encoding="utf-8")
        code = "from pathlib import Path; Path('lilt-vase.scad').write_text(" + repr(source) + ")"
        result = manager.execute(lease.id, ExecutionCommand(
            argv=("/usr/local/bin/python3", "-I", "-c", code),
        ), **ownership)
        if result.exit_code:
            raise RuntimeError("Source staging failed")
        operations = (
            ("cad.openscad_export", {"input": "lilt-vase.scad", "output": "lilt-vase.stl"}),
            ("cad.openscad_export", {"input": "lilt-vase.scad", "output": "lilt-vase.3mf"}),
            ("cad.mesh_inspect", {"input": "lilt-vase.stl"}),
            ("cad.mesh_inspect", {"input": "lilt-vase.stl", "vase_checks": True}),
            ("cad.mesh_inspect", {"input": "lilt-vase.3mf"}),
            ("cad.render_mesh", {"input": "lilt-vase.stl", "output": "lilt-vase.png",
                                 "material": "celadon", "resolution": 1200, "samples": 64,
                                 "save_scene": True}),
        )
        for operation, arguments in operations:
            output = registry.execute(definitions[operation], arguments, context).output
            print(operation, arguments["input"], "exit", output["exit_code"], flush=True)
            if output["exit_code"]:
                raise RuntimeError(output["stderr"][-4000:] + output["stdout"][-2000:])
            if operation == "cad.mesh_inspect":
                report = json.loads(output["stdout"])
                reports[arguments["input"]] = report
                print(json.dumps(report), flush=True)
                assert report["watertight"] and report["winding_consistent"]
                assert report["positive_volume"] and report["connected_components"] == 1
                assert abs(report["dimensions_mm"][2] - 180) < 0.001
                if arguments.get("vase_checks"):
                    vase = report["vase"]
                    assert vase["open_mouth"] and vase["closed_base"]
                    assert abs(vase["base_thickness_mm"] - 3) < 0.001
                    assert vase["wall_samples"] == 512 and vase["wall_min_mm"] >= 2.30
                    print(json.dumps(vase), flush=True)
        collision = registry.execute(definitions["cad.openscad_export"], operations[0][1], context)
        assert collision.output["exit_code"] != 0, "Existing mesh must never be overwritten"
    finally:
        manager.release(lease.id, **ownership)
        summary = {"state": str(state), "workspace": str(lease.plan.workspace_path),
                   "lease_id": str(lease.id), "released": True, "geometry": reports}
        (state / "smoke-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print("Deliverables:", lease.plan.workspace_path, flush=True)
    (lease.plan.workspace_path / "geometry-validation.json").write_text(
        json.dumps(reports, indent=2), encoding="utf-8",
    )
    assert "vase" in reports["lilt-vase.stl"]
    (lease.plan.workspace_path / "lilt-vase-readme.txt").write_text(
        DELIVERY_README, encoding="utf-8",
    )


if __name__ == "__main__":
    main()
