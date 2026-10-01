# Raspberry Pi 5 four-camera selector: engineering prototype

This review project connects **four IMX219 Camera Module 2 cameras, with one CSI
camera selected at a time**, to a Raspberry Pi 5 camera connector. It implements
three two-to-one TS3DV642-Q1 switches, a PCA9544A I2C selector at address `0x70`,
and a separately supplied camera rail. It is a CAD prototype awaiting physical
electrical, signal-integrity, cable and Raspberry Pi software validation.

**Use an external regulated, current-limited 3.3 V supply at J7.** Never connect
5 V to that header. The Pi connector powers the switch/control rail; it does not
supply the four camera modules. A TPS22918 gates the external camera rail from
the Pi's 3.3 V presence. It is not a regulator, current limiter or reverse-current
protector. A 1.5 A current limit is an initial bench setup, not a measured demand
or guaranteed operating budget. All four modules may be powered for enumeration.

## Review files

- Open `pi5-camera-mux.kicad_pro` in KiCad 9; the corresponding schematic and PCB
  are editable source. `CameraMux.kicad_sym` and library tables accompany them.
- `review/schematic.pdf` and `review/board.png` provide browser previews.
- `review/schematic-details.pdf` retains the master and adds nine enlarged A4
  section pages for reading and printing; its companion JSON records provenance.
- `review/bom.csv`, `review/connectivity.net`, and `review/gerbers-review.zip`
  provide the BOM, actual exported netlist, and Gerber/Excellon review package.
- `review/erc.json`, `review/drc.json`, and `review/tool-smoke.json` contain the
  actual KiCad tool results. A nonzero violation count remains a release hold.
- `connectivity-review.json` and `review/layout-review.json` record independent
  pin, board/netlist, trace-length, layer, via and plane observations.
- [engineering-review.md](engineering-review.md) records manufacturer references,
  component pin tables, power/driver analysis and the outstanding release checks.

The Gerber ZIP is for engineering review. Do not order or assemble this revision
until the documented release holds have been resolved by an electronics reviewer.

Validation recorded on 2026-10-01 UTC with KiCad 9.0.2:

- All seven isolated PCB operations completed successfully. ERC: **0 violations**.
  DRC: **0 violations, 0 missing connections, 0 schematic-parity issues**.
- The workspace Python tool successfully invoked Debian's `pcbnew` interpreter
  to load and save an editable board; source hashes remained unchanged and the
  container lease was released. An attempted output overwrite was rejected.
- Independent review passed **225 netlist checks**, found no board-pad/netlist
  mismatches, and confirmed all 42 CSI nets are simple, unbranched front-layer
  paths. All 21 measured pairs have **0.0000 mm P/N difference** and zero vias.
- All **24,438 ground-reference samples** beneath CSI traces fall within one
  filled In1 ground region. This is sampled geometry, not electromagnetic analysis.
- Selected board copper paths measure **109.5515–113.6643 mm**, excluding switch
  packages and cables. Lane-to-clock length differences and total insertion loss
  remain subjects for signal-integrity qualification and physical tests.

The reviewed board SHA-256 is
`dcc29d94eef476e0034d6c41a2a14266b6fae18dcd7bfd40f2587fa7990fcb64`.
No physical board, camera capture, impedance measurement or fabrication was performed.

## Connections and assembly

| Connector | Purpose | Key details |
| --- | --- | --- |
| J1 | Pi 5 host | Amphenol F32Q-1A7H1-11022, 22 positions, 0.5 mm, top contacts |
| J2/J3/J4/J5 | Cameras A/B/C/D | TE 1-84953-5, 15 positions, 1 mm, top contacts |
| J6 | Pi GPIO control | Pins 1–4: GND, GPIO4, GPIO17, GPIO18 |
| J7 | External camera power | Pin 1 regulated 3.3 V; pin 2 common ground |

Camera connector pin 11 shares the host camera-enable IO0. Pin 12 IO1 connects
through separate **DNP** zero-ohm links R16–R19; leave all four links unfitted for
Camera Module 2, whose official schematic marks pin 12 unconnected. R6/R7 host
I2C pull-ups are also DNP. Check the combined module/board pull-up resistance
before fitting the downstream pull-ups R8–R15.

Read board pin-1 markings and manufacturer contact drawings, and verify every
cable's conductor continuity before applying power. A pitch adapter cable alone
does not establish contact orientation. The TE signal/mounting-pad drawing review
is recorded in the engineering review; actual mating cable clearances, connector
samples and host cable orientation still require assembly validation.
On the board top view, pin 1 is below the last/supply pin on all five FFC
connectors. J1's host cable may extend over the board: confirm insertion direction,
actuator access and physical cable clearance with the actual connector samples.

The GPIO4/18/17 selection states are A=`0/1/0`, B=`1/1/0`, C=`0/0/1`,
D=`1/0/1`. GPIO pull-downs disable both leaf switches at reset. Do not switch
cameras during capture. Use a reviewed device-tree/libcamera configuration to
coordinate video selection and I2C selection; the candidate official overlay in
the engineering review has not been tested on this physical board.

## Layout intent and limits

The board is 125 × 110 mm with four copper layers: F.Cu carries the paired CSI
paths, In1.Cu supplies their ground reference, In2.Cu distributes camera power,
and B.Cu carries control/auxiliary routes. The CSI paths use 0.15 mm copper and
0.22 mm pair gap in their trunks, with short symmetric pad fanouts and no signal
vias. These dimensions are geometric choices, **not a verified 100-ohm stack-up**.

The independent layout report measures each of 21 differential pairs and the
three board segments in each selectable path. Package internals and FFC cables
are excluded. Two switch stages, path length, connector discontinuities and
the actual fabricated stack-up require signal-integrity review and bench tests.
Ground plane fill is checked geometrically; this does not replace return-current
and via-antipad review. Plane necks, supply drop, inrush, thermal behavior and
backfeeding under every power sequence require measurement.

## Reproducing the CAD workflow

Use a **copy** of this directory. Generation replaces the source board with the
deterministic placement and locked paired routes; the final low-speed routing
comes from the separately imported router session. Ground/power vias are fixed
before routing; KiCad supplies the final internal-plane fill after import. The
router receives those fixed conductors without plane polygons, avoiding its
incomplete SMD-to-plane handling on disabled inner routing layers.

```powershell
docker build -f deploy/Dockerfile.pcb-worker -t simon-pcb:local .
docker build -f deploy/Dockerfile.pcb-router -t simon-pcb-router:local .
$pcbProject = (Resolve-Path 'path/to/project-copy').Path
docker run --rm --network none --mount "type=bind,source=$pcbProject,target=/workspace" --entrypoint /usr/bin/python3 simon-pcb:local /workspace/generate.py
docker run --rm --hostname localhost --network none --cpus 2 --memory 2g --mount "type=bind,source=$pcbProject,target=/workspace" --entrypoint /opt/java/openjdk/bin/java simon-pcb-router:local -jar /opt/freerouting.jar -de /workspace/pi5-camera-mux.dsn -do /workspace/pi5-camera-mux.ses --gui.enabled=false -mp 20 -mt 0 --router.layers.routable=true,false,false,true -da --api_server.enabled=false --user_data_path=/tmp/freerouting
docker run --rm --network none --mount "type=bind,source=$pcbProject,target=/workspace" --entrypoint /usr/bin/python3 simon-pcb:local /workspace/finish_routing.py
```

The optional router image pins Freerouting 2.4.1 by SHA-256 and uses Java 25.
Routing runs offline, preserves fixed CSI geometry, and may require manual
completion/review after tool or library changes. Inspect actual DRC rather than
assuming an autorouter's completion message establishes a correct board.

From the repository root, refresh real isolated-tool exports with
`python examples/agents/pcb_smoke.py --write-review`. Refresh the independent
connectivity audit with `python examples/hardware/pi5-camera-mux/review_connectivity.py`.
Run `review_layout.py` using KiCad's `/usr/bin/python3` and save its JSON output.
`render_preview.py` rasterizes the actual SVG using PyMuPDF; `schematic_details.py`
uses the same development dependency to create vector-preserving enlarged pages.
These scripts use no model/provider calls. See the
[PCB tool runbook](../../../docs/runbooks/agent-pcb.md) for authorization and limits.
