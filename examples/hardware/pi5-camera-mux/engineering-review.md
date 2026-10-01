# Pi 5 four-camera CSI multiplexer: engineering review

Review date: 2026-09-30. This is a prototype design review, not a fabrication release.
The electrical architecture below has been checked against primary documentation. The
exported schematic netlist passes the independent checks described below. Layout, cable
assemblies, real cameras, and software still require the checks at the end before the board
can be described as working hardware.

## Intended use and assumptions

Four standard Raspberry Pi ribbon-camera modules, each using two CSI-2 data lanes and one
D-PHY clock lane; one camera streams at a time into one Pi 5 CAM/DISP port. The initial
software candidate is four IMX219 Camera Module 2 units. Other sensors require their exact
module schematic and driver configuration to be checked. Four-lane cameras, simultaneous
capture, stereo synchronization, hot-plugging, and arbitrary external sensor clocks are
outside this prototype's scope.

## Connector mapping

Pin numbers below are electrical contact numbers, not the apparent left-to-right order in
a photograph. Raspberry Pi publishes the following mappings and identifies pin 1 relative
to the GPIO header. I2C on the Pi side is already pulled up to 3.3 V; CAM_IO0 is commonly a
camera-enable signal, and CAM_IO1 is module dependent.
[Raspberry Pi camera connector documentation](https://www.raspberrypi.com/documentation/accessories/camera.html#pinout-information)

| Signal | Host 22-pin | Camera 15-pin |
| --- | --- | --- |
| GND | 1, 4, 7, 10, 13, 16, 19 | 1, 4, 7, 10 |
| DATA0_N / DATA0_P | 2 / 3 | 2 / 3 |
| DATA1_N / DATA1_P | 5 / 6 | 5 / 6 |
| CLOCK_N / CLOCK_P | 8 / 9 | 8 / 9 |
| DATA2_N / DATA2_P | 11 / 12, unused | Not present |
| DATA3_N / DATA3_P | 14 / 15, unused | Not present |
| CAM_IO0 | 17 | 11 |
| CAM_IO1 | 18 | 12 |
| SCL / SDA | 20 / 21 | 13 / 14 |
| Supply | 22: HOST_3V3 | 15: separate CAM_3V3 |

Selected host connector: **Amphenol F32Q-1A7H1-11022**, 22 contacts, 0.5 mm pitch,
upper contacts, 2 mm housing, rated 0.5 A per contact. The installed KiCad footprint is
`Connector_FFC-FPC:Amphenol_F32Q-1A7x1-11022_1x22-1MP_P0.5mm_Horizontal`.
[Amphenol product and drawing](https://www.amphenol-cs.com/product/f32q1a7h111022.html)

Selected camera connector: **TE 1-84953-5**, 15 contacts, 1 mm pitch, upper contacts,
0.3 mm FFC, 1 A/contact. Use the matching installed footprint
`Connector_FFC-FPC:TE_1-84953-5_1x15-1MP_P1.0mm_Horizontal`.
The TE product dimensions are 22.92 x 6.54 x 2.56 mm. Do not substitute the bottom-contact
1-84952-5 without rechecking cable orientation.
[TE product specification](https://www.te.com/en/product-1-84953-5.html)

The installed KiCad TE footprint was compared visually with the manufacturer family
catalog's recommended land pattern, pages 10-11. Pads 1 through 15 run from x=-7 to +7 mm,
y=-1.8 mm, with 1 mm spacing and 0.61 x 2 mm pads. The mounting pads are 2.68 x 3.60 mm,
centred at x=+/-9.99, y=1 mm: the drawing's 4.33 and 1.65 mm offsets produce that width and
position. Signal-pad bottoms and mounting-pad tops align at y=-0.8 mm. These dimensions,
the 14 mm contact span, and the unrotated top-view pin-1 position agree with the drawing.
The catalog is revision 05-06; reconfirm against the current purchased part and assembly
process before fabrication. The Amphenol host footprint's exact drawing comparison remains
separate from this verified TE comparison.

In the TE drawing, pin 1 is left in the unrotated top view and the actuator/cable mouth is
toward positive y. Rotated 90 degrees as placed, pin 1 is below pin 15 in the board top view,
and insertion is from positive x toward negative x. Contact-side and pin-1 silkscreen labels
remain preferable to a generic arrow that could be mistaken for the exposed ribbon-contact
face. Do not infer ribbon conductor continuity from contact side alone. With both ends
inserted, verify host pin 1 reaches board pin 1, supply reaches only
supply, and each differential polarity reaches the corresponding contact. Clearly mark pin 1
and cable contact side on the silkscreen and assembly drawing.
[KiCad TE footprint source](https://raw.githubusercontent.com/KiCad/kicad-footprints/master/Connector_FFC-FPC.pretty/TE_1-84953-5_1x15-1MP_P1.0mm_Horizontal.kicad_mod)
[TE FPC family catalog, pages 10-11](https://www.te.com/commerce/DocumentDelivery/DDEController?Action=srchrtrv&DocFormat=pdf&DocLang=English&DocNm=1-1773440-6&DocType=Data+Sheet&PartCntxt=1-84953-5)

## High-speed switch circuit

Use three **TI TS3DV642RUARQ1** switches in a tree: U1 common to the Pi, U1 bank A to
U2 common, U1 bank B to U3 common; U2 banks A/B to cameras A/B and U3 banks A/B to C/D.
Every active path crosses two switches. This topology is an engineering choice, not a
vendor-qualified four-camera reference design.

The RUA package is WQFN-42, 3.5 x 9 mm, 0.5 mm pitch, with grounded exposed pad.
Operate at 3.3 V and decouple each device locally with 100 nF. The datasheet explicitly
includes D-PHY CSI-2 applications. **EN is active high; SEL1 must be high for all channels;
SEL2 selects A=0/B=1.** SEL1=0 passes only the D0 pair and would break this design.
[TI TS3DV642-Q1 datasheet, sections 5, 8.4, 9.1](https://www.ti.com/lit/ds/symlink/ts3dv642-q1.pdf)

| Routed pair | Common + / - | A + / - | B + / - |
| --- | --- | --- | --- |
| Camera DATA0 using D2 | 10 / 11 | 34 / 33 | 25 / 24 |
| Camera DATA1 using D1 | 7 / 8 | 36 / 35 | 27 / 26 |
| Camera CLOCK using D0 | 5 / 6 | 38 / 37 | 29 / 28 |

The routing revision assigns clock to switch D0 and camera DATA0 to switch D2 to keep
connector and switch pad order aligned. All three are equivalent bidirectional channels
with SEL1 high; the clock does not require a special switch channel.

VCC=1, EN=2, SEL1=16, SEL2=17, ground=exposed pad. NC pins 9/30 stay unconnected.
Unused analog channels must not be connected to camera nets. No series AC-coupling capacitors,
3.3 V pull-ups, or ordinary logic buffers belong on these D-PHY lanes.

TS5MP645 is currently marked not recommended for new designs. TMUX646 is an active MIPI
alternative, but its 36-ball nFBGA package makes this prototype's assembly and escape routing
harder. Neither a switch bandwidth headline nor ERC/DRC proves the complete two-switch link.
[TI TS5MP645 status](https://www.ti.com/product/TS5MP645),
[TI TMUX646 package and capabilities](https://www.ti.com/product/TMUX646)

## I2C, control, and power

Use **PCA9544APWR**, TSSOP-20, for four independently selectable camera I2C branches.
Ground address pins 1/2/3 for 0x70; VCC20=HOST_3V3, GND10. Host SDA19/SCL18 connect
to the CSI connector. Branch SDA/SCL pairs are 5/6, 8/9, 12/13, 15/16. Pull unused
interrupt inputs 4/7/11/14 high; output 17 can remain unconnected. Provide downstream
pull-up footprints, initially 4.7 kOhm to CAM_3V3; recalculate with the actual camera's
pull-ups and bus capacitance. Avoid unnecessary additional host pull-ups. Limit I2C to
400 kHz or less. The chip selects one branch, isolating identical sensor addresses.
[TI PCA9544A pinout and interface requirements](https://www.ti.com/lit/ds/symlink/pca9544a.pdf)

Use this GPIO assignment, with 10 kOhm pull-downs on GPIO4, GPIO17, and GPIO18:

| Switch | EN | SEL1 | SEL2 |
| --- | --- | --- | --- |
| U1, root | HOST_3V3 | HOST_3V3 | GPIO17 |
| U2, A/B | GPIO18 | HOST_3V3 | GPIO4 |
| U3, C/D | GPIO17 | HOST_3V3 | GPIO4 |

All camera CAM_IO0 inputs share the host enable so sensors can be discovered through their
I2C branches before a video route is selected. Routing enable only through the video switch
would prevent some probes. This also means the supply must tolerate **all four powered
modules**, even though only one streams. CAM_IO1 must remain explicitly module-dependent;
use a documented optional link rather than claiming universal clock/LED compatibility.

The official Camera Module 2 v2.1 schematic was visually inspected: 15-pin J1 pin 12 is
explicitly unconnected, pin 11 drives the module's regulator enables, and the sensor clock
comes from an onboard 24 MHz oscillator. Therefore R16-R19, the four per-camera IO1 links,
are **DNP by default**. Leave them unpopulated for that module. Populating them for other
camera types requires checking each module's direction, voltage, and clock requirements;
four independent outputs must never be shorted together.
[Raspberry Pi Camera Module 2 schematic](https://datasheets.raspberrypi.com/camera/camera-module-2-schematics.pdf)

Do not join HOST_3V3 and CAM_3V3. Feed camera power from an external regulated 3.3 V supply
through a **TPS22918DBVR** load switch. Proposed connections: VIN1=external 3.3 V, GND2,
ON3=HOST_3V3 with 100 kOhm pull-down, CT4 to a slew-control capacitor, VOUT6=CAM_3V3,
QOD5 connected through 100 Ohm to VOUT. Start with 10 uF input/output decoupling and
measure inrush before finalizing CT. The part supports up to 2 A subject to its thermal
limits; it is not a regulator, fuse, current limiter, or reverse-polarity protector.
The gating and discharge reduce powered-camera/unpowered-Pi exposure but require a measured
power-sequence/backfeed test. Use a current-limited bench supply for bring-up.
[TI TPS22918 datasheet](https://www.ti.com/lit/ds/symlink/tps22918.pdf)

### Power acceptance limits and unresolved sequencing

The external source must be regulated **3.3 V**, with a common ground at J7. A 5 V input
is not supported: the TPS22918 passes its input voltage and does not regulate it. HOST_3V3
supplies the switches and I2C controller; the external source supplies all camera modules.
The host FFC's 0.5 A contact rating, each camera FFC's 1 A rating, and the switch's 2 A rating
are component ceilings, not a validated board current budget. The lowest permissible limit
after copper, via, connector, temperature and module checks governs the finished board.

Required bench results before release:

- With both inputs disconnected, confirm no low-resistance short between HOST_3V3,
  CAM_3V3, external input and ground; verify every ribbon contact against the pin table.
- With the Pi off and only external 3.3 V on, U5 must remain disabled and discharge CAM_3V3.
  Measure current into the disconnected host-power contact and camera/control lines. Any
  sustained powering of the Pi through this board fails acceptance; no allowable Pi
  injection-current budget has been established for this prototype.
- With the Pi on and external supply off, check for camera-rail elevation through I2C or
  CAM_IO0. The load switch alone cannot prevent backfeed through signal pins. This state
  must be measured, including a selected I2C branch, before it can be supported.
- Capture both rails and enable signals during each turn-on/turn-off order and a supply
  interruption. Confirm the switched rail rises cleanly, falls to the unpowered state,
  and does not overshoot the actual camera module's allowed supply range. Steady-state
  readings do not establish safe shutdown sequencing.
- With four connected/enabled modules and one streaming, measure each connector voltage,
  total current, inrush, U5 temperature and camera error rate. Increase a current-limited
  bench source only against the measured budget. Establish the exact module voltage
  limits and a thermally derated board limit before accepting a supply or capture mode.

R4 and the QOD path provide host-off gating and discharge, but do not establish complete
reverse-current isolation. A failed sequencing test requires circuit changes, such as
qualified signal isolation or a revised power controller, followed by another review.

## Software contract

The GPIO circuit above deliberately matches the official four-port overlay's selection
table: A=(GPIO4,18,17)=(0,1,0), B=(1,1,0), C=(0,0,1), D=(1,0,1).
That overlay describes a PCA9544 at 0x70 plus the Linux video-mux graph; GPIO writes alone
are not a camera driver. Its default host port is CAM1, with a `cam0` override.
[Raspberry Pi overlay source](https://raw.githubusercontent.com/raspberrypi/linux/rpi-6.18.y/arch/arm/boot/dts/overlays/camera-mux-4port-overlay.dts)

Pi 5 uses the RP1-CFE/PiSP pipeline rather than the older Unicam/VC4 pipeline. The current
Raspberry Pi PiSP pipeline source explicitly enumerates multiple sensors behind video-mux
devices attached to one CFE, while excluding simultaneous use. This supports the architecture
in principle; it does not establish support in an arbitrary older installed OS image or prove
this new board. Record and test the actual kernel, overlay, libcamera, and rpicam versions.
Legacy `raspistill` instructions and successful Pi 4 tests are not Pi 5 validation.
[Raspberry Pi PiSP pipeline source](https://github.com/raspberrypi/libcamera/blob/main/src/libcamera/pipeline/rpi/pisp/pisp.cpp)

A **candidate to verify on the actual Pi OS image**, for four IMX219 modules, is:

```ini
camera_auto_detect=0
dtoverlay=camera-mux-4port,cam0-imx219,cam1-imx219,cam2-imx219,cam3-imx219
```

Use the parameters for the actual modules; reserve GPIO4/17/18 from other functions.
Check that the installed overlay and Pi 5 kernel support the graph, regulators, and selected
sensors. List cameras with `rpicam-hello --list-cameras`, then select the enumerated camera
through libcamera/rpicam. Stop and release the active capture before selecting another camera.
Do not run a separate GPIO or I2C selection script concurrently with the kernel mux drivers.
[Official overlay parameters](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README),
[Raspberry Pi camera software](https://www.raspberrypi.com/documentation/computers/camera_software.html)

## Independent connectivity review

`python review_connectivity.py` reads the actual KiCad-exported `pi5-camera-mux.net`; it does
not import the circuit generator. The independent tables check 225 conditions covering all
42 CSI signal segments and their exact two-pin endpoints, differential polarity, I2C branches,
address straps, connector grounds, unused channels, supply separation, controls, discharge,
and default DNP links. `connectivity-review.json` records the source SHA-256 and each result.
The current exported netlist passes these checks. Rerun after any schematic change.
The checker was also exercised with temporary negative controls: reversing host DATA0
polarity and removing an IO1 link's DNP flag both correctly produced failed results.

This establishes consistency with the selected electrical pin tables. It does not verify
copper routing, signal integrity, assembled parts, or the physical cable conductor order.
`review_layout.py`, run with KiCad's `pcbnew` Python module, separately verifies each CSI
net forms one front-copper chain between its two expected pads, rejecting stubs, islands,
branches, duplicated segments and crossings within a net. Its length totals are accepted
only with those topology checks passing. It reports board-pad versus schematic-net
mismatches, per-polarity lengths/vias, and three-segment active-path totals; these lengths
exclude chip internals, FFC cables, and via barrels. It also samples the actual filled In1
ground polygons beneath trace centres and edges at intervals no greater than 0.2 mm.
That is a geometric screen; it cannot prove complete return-current continuity, impedance,
loss, or electromagnetic coupling.

### Final routed CAD results

The frozen routed board passes the independent review in
`review/independent-review.json`, with full measurements in `review/layout-review.json`.
The real PCB tool's `review/drc.json` reports zero violations, zero unconnected items, and zero
schematic-parity issues. The independent netlist review passes all 225 checks, and every
board pad matches its exported schematic net.
The real PCB tool smoke test also exports a fresh `review/connectivity.net` that passes
the same 225 checks, and its `review/erc.json` contains zero violations.

All 42 CSI nets form simple pad-to-pad chains: 21 differential segments on front copper,
with no signal vias, branches or disconnected copper. Calculated P/N trace-length
differences are 0.0000 mm at the report's precision. The three-segment active copper paths
range from 109.5515 to 113.6643 mm, excluding switches, connectors and cables. Different
lanes are not equal to one another: the largest data/clock lane length spread is 2.2432 mm.
Those numbers describe CAD geometry, not measured propagation delay or signal margin.

All 24,438 reference-plane samples fall on one filled In1 GND region; In2 carries the camera
power plane. The external camera-power feed is 11.0844 mm of 0.6 mm front-copper track;
the load-switch output and camera connectors have local 0.5 mm feeds into the plane.
The remaining narrow camera-rail traces serve ancillary circuitry in parallel with that
plane. A fabricator dielectric stackup has not been specified: the 0.15 mm CSI trace width
and nominal 0.22 mm trunk gap do **not** establish the intended 100 Ohm impedance.

These results support a routed prototype review. The design has not been fabricated,
assembled, powered, tested with Pi 5 hardware, or qualified for a capture mode.

## Release checks still required

- Keep the independent netlist checks passing and compare the final board against the
  schematic with KiCad. Check the selection truth table under actual boot and shutdown states.
- Confirm exact connector land patterns, mating cables, mounting clearance, and assembly
  orientation from current manufacturer drawings and physical samples.
- The mux exposed pads contain plated ground vias. Specify filled/plugged/tented treatment
  and a compatible solder-paste aperture pattern with the fabricator and assembler;
  uncontrolled solder wicking can compromise those joints. Confirm via drill/annulus and
  copper weights for both ground and camera-power feeds.
- Use a fabricator-confirmed stackup and 100 Ohm differential impedance target. Route each
  pair together over continuous ground, minimize stubs/vias, and document pair skew and total
  path lengths. A generic track width or a clean DRC is not an impedance calculation.
- Simulate or measure insertion loss, return loss, crosstalk, and eye margin across both
  switch stages and the actual cables at the intended lane rates; check low-power D-PHY
  transitions as well as high-speed traffic. Lower the tested mode if margin is inadequate.
- Validate no unselected or unpowered camera backfeeds another rail. Measure startup current,
  loaded camera voltage, regulator temperatures, and discharge behavior.
- On real Pi 5 hardware, verify all four probes, repeated capture switching, CRC/ECC errors,
  cold boots, recovery from a failed camera, and maximum intended capture duration.
- Preserve real ERC/DRC reports and unresolved issues. Label exports as prototype review
  artifacts until these electrical, mechanical, software, and bench checks are complete.
