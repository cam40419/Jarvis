# Isolated CAD and 3D tools

The `cad` transport exports editable OpenSCAD designs, inspects triangle meshes,
and renders them with a fixed Blender studio scene. CAD applications run only in
an offline Linux Docker lease with the `python` and `cad` capabilities. The host
does not execute OpenSCAD, Blender, user Python, or model-supplied shell commands.

Build the worker image and exercise the actual tools:

```powershell
docker build -f deploy/Dockerfile.cad-worker -t simon-cad:local .
venv/Scripts/python.exe examples/agents/vase_smoke.py
```

Use `simon-cad:local` in an enabled Docker environment with `network: "none"`,
2 CPU cores and 4096 MiB of memory. CPU renders benefit from a 300-second worker
timeout. Image construction downloads dependencies; task execution has no network.
The container also has a read-only root, an unprivileged user, no Linux capabilities,
and bounded memory/process count. Keep the lease journal on the same manager host.

Add the required definitions from
[cad-tools.example.json](../../examples/agents/cad-tools.example.json), enable
them in the operator manifest, and grant them to the chosen worker profile.
The reusable example remains disabled until its worker image is provisioned.
Write operations require `jobs:write` and a write-capable profile; inspection
requires `jobs:read`. Registration never expands an account's existing scopes.

| Tool | Arguments and output |
| --- | --- |
| `cad.openscad_export` | `input` relative `.scad` filename, `output` new `.stl` or `.3mf`. STL output is binary. |
| `cad.mesh_inspect` | `input` relative `.stl`/`.3mf`; optional `vase_checks`. Returns dimensions, bounds, topology, volume, area and hash as JSON in stdout. |
| `cad.render_mesh` | `input` `.stl`/`.3mf`, new PNG `output`, optional `material` (`celadon`, `ivory`, `terracotta`, `charcoal`), `resolution` 256–1600, `samples` 8–128 and `save_scene` boolean. The latter also saves a `.blend` next to the PNG. |

Tool results use the same exit-code/stdout/stderr contract as document processing.
Input files and individual outputs are limited to 48 MiB; inspected meshes are
limited to 500,000 triangles. 3MF package expansion is bounded before parsing.
Output destinations must be new files; publication never overwrites an existing
revision. A timeout or execution failure after a write begins is an unknown
outcome requiring inspection, with no automatic retry. Inputs come from the
owned task workspace, for example through `workspace.import_local` or a prior
isolated source-writing step. Return generated paths in the worker's final
`artifacts` list to publish them through authenticated downloads.

OpenSCAD supports the export formats through its documented
[command-line interface](https://files.openscad.org/documentation/manual/Using_OpenSCAD_in_a_command_line_environment.html).
Rendering uses Blender's
[background rendering mode](https://docs.blender.org/manual/en/2.90/advanced/command_line/render.html).
Geometry checks use trimesh's documented
[watertightness, winding and volume properties](https://trimesh.org/trimesh.html).
STL contains no unit declaration; this workflow interprets its coordinates as
millimeters. CAD inspections do not fill holes, reverse normals or repair a mesh
to make a failing design appear valid.

## LILT vase demonstration

[spiral_vase.scad](../../examples/agents/spiral_vase.scad) is the editable design.
It creates an 18-flute vase with 100 degrees of twist, a softly rounded profile,
180 mm height, approximately 99 mm maximum width, nominal 2.4 mm walls and a
3 mm floor. The inner wall follows an inward surface-normal offset, avoiding
the thinner flanks that a constant radial subtraction can create.

The smoke script runs every CAD tool against this source in a real managed
container. It exports STL and 3MF, checks topology and dimensions, verifies the
centerline floor/mouth, samples 512 outer-surface inward-normal rays, and saves
a PNG plus editable Blender scene. It also verifies that an existing mesh cannot
be overwritten. The container is stopped before deliverables are handed back.
Each run prints a unique retained directory under `.local/agents/vase-smoke-*`
and writes `smoke-summary.json` with the workspace location and geometry results.

Wall samples exclude the rim/base transitions. They support this centered vase
design and are not an exhaustive check of arbitrary hollow products. Print LILT
upright using ordinary wall/infill slicing rather than spiral vase mode, since
the model already contains its wall and floor. Dry arrangements are appropriate;
use a liner for water until the chosen material and print have been tested.

To verify delivery through the actual API and browser after the smoke succeeds,
set `SIMON_VASE_SMOKE_WORKSPACE` to its printed workspace path, set
`SIMON_BROWSER_TESTS=1` and optionally `SIMON_BROWSER_CHANNEL=msedge`, then run
`pytest tests/integration/test_cad_delivery_browser.py`. This test uses a separate
memory server and synthetic accounts. It uploads the seven real deliverables,
verifies download hashes, renders the 1200-pixel PNG through the authenticated
preview route, checks desktop/mobile sizing, rejects anonymous and other-account
access, and exercises the Engineering profiles and CAD tool search. It neither
contacts model providers nor touches the active server's sessions or files.
