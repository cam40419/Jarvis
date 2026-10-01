# Offline PCB checks and exports

The `pcb` transport runs seven fixed KiCad CLI operations inside an authorized
Linux Docker lease. Build the worker, then exercise the real tools against the
included engineering prototype:

```powershell
docker build --file deploy/Dockerfile.pcb-worker --tag simon-pcb:local .
python examples/agents/pcb_smoke.py
```

Provision image `simon-pcb:local`, capabilities `python` and `pcb`, and
`network: "none"`. Grant the desired tool IDs, environment ID, `jobs:write`, and
`max_action: "write"` to the profile; the requesting actor also needs that scope.
Every operation writes a new file. The default factory
`simon.adapters.pcb_tools.pcb_tool_definitions()` returns disabled definitions.
Use the [example declarations](../../examples/agents/pcb-tools.example.json) or
enable the factory only after provisioning the worker.

All tools accept exactly `input` and `output` workspace-relative paths:

| Tool | Input | Output |
| --- | --- | --- |
| `pcb.erc` | `.kicad_sch` | `.json` electrical-rule report |
| `pcb.schematic_pdf` | `.kicad_sch` | `.pdf` schematic |
| `pcb.netlist` | `.kicad_sch` | `.net` XML connectivity |
| `pcb.bom` | `.kicad_sch` | `.csv` references, values, footprints, quantities |
| `pcb.drc` | `.kicad_pcb` | `.json` design-rule and schematic-parity report |
| `pcb.board_svg` | `.kicad_pcb` | `.svg` front copper, silkscreen, outline |
| `pcb.gerbers` | `.kicad_pcb` | `.zip` Gerbers and Excellon drills |

DRC also requires the matching `.kicad_sch` beside the board. Import all project
files and library tables needed for a self-contained review. KiCad 9 standard
symbols/footprints are installed in the image. No arbitrary CLI arguments, shell
commands, plugin execution or network access are exposed by this transport.

To create or edit boards programmatically, separately grant
`workspace.python_execute` and capability `process.execute`. The default Python
command uses `/usr/local/bin/python3`, while Debian's `pcbnew` module belongs to
**`/usr/bin/python3`**. Save a script inside the lease, then invoke it using
`subprocess.run(['/usr/bin/python3', 'edit_board.py'], check=True)` from the
workspace Python tool. Both interpreters run inside the same offline container.
The real smoke exercises this exact bridge by loading and saving an editable
copy of the example board before running the seven fixed KiCad operations.

Inputs and aggregate generated output are capped at 40 MiB; the ZIP has at most
64 entries. Paths reject traversal, metadata directories, symlinks and special
files. Existing outputs are preserved. Commands default to 60 seconds and 64 KiB
of retained process output; container CPU/memory limits remain in force.

The response includes `exit_code`, `stdout`, `stderr`, and `truncated`. Exit zero
means the requested operation completed. ERC/DRC exit **5** means rule violations
were found: the JSON report is still published for inspection. Other nonzero
codes indicate failure. Do not treat a generated report as a clean check. Include
the output paths in the agent's final artifact list to publish durable downloads.

The smoke covers all seven operations through a real isolated worker, rejects an
output collision, checks source hashes, checks that the ZIP contains drills and
releases its lease. `--write-review` refreshes the example's `review/` files.
It makes no model/provider calls and retains its journal under `.local/agents`.

The [four-camera example](../../examples/hardware/pi5-camera-mux/README.md)
demonstrates the CAD workflow. Electrical/design-rule checks do not establish
signal integrity, component suitability, physical operation or driver support;
its release holds and measured evidence are documented with the project.
