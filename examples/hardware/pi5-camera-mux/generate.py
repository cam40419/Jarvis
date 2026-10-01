"""Regenerate the review prototype with KiCad 9's installed footprint library.

Run with /usr/bin/python3 inside simon-pcb:local, in this copied project folder.
Schematic pin assignments are explicit below. This generator does not certify
signal integrity, power sequencing, cable orientation, or driver compatibility.
"""

from __future__ import annotations

import itertools
import json
import math
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pcbnew

ROOT = Path(__file__).resolve().parent
NAME = "pi5-camera-mux"
LIBRARY = "CameraMux"
PROJECT_UUID = str(uuid5(NAMESPACE_URL, NAME))
COMPONENTS = []
NETS = set()


def uid(value):
    return str(uuid5(NAMESPACE_URL, NAME + ":" + value))


def quoted(value):
    return json.dumps(str(value), ensure_ascii=False)


def component(reference, value, footprint, pins, schematic, board, *, dnp=False):
    symbol = reference if reference.startswith("U") else value.replace("-", "_").replace(".", "_")
    symbol = "".join(char if char.isalnum() or char == "_" else "_" for char in symbol)
    item = dict(
        reference=reference,
        value=value,
        footprint=footprint,
        pins=pins,
        schematic=schematic,
        board=board,
        symbol=symbol,
        dnp=dnp,
        uuid=uid(reference),
    )
    COMPONENTS.append(item)
    NETS.update(net for _name, net, _kind in pins.values() if net)
    return item


def passive(reference, value, first, second, schematic, board, *, dnp=False):
    category = (
        "Capacitor_SMD:C_0603_1608Metric"
        if reference.startswith("C")
        else ("Resistor_SMD:R_0603_1608Metric")
    )
    return component(
        reference,
        value,
        category,
        {"1": ("1", first, "passive"), "2": ("2", second, "passive")},
        schematic,
        board,
        dnp=dnp,
    )


def mux(reference, common, bank_a, bank_b, enable, select, schematic, board):
    names = {
        1: "VCC",
        2: "EN",
        3: "SCL",
        4: "SDA",
        5: "D0+",
        6: "D0-",
        7: "D1+",
        8: "D1-",
        9: "NC",
        10: "D2+",
        11: "D2-",
        12: "D3+",
        13: "D3-",
        14: "HPD",
        15: "CEC",
        16: "SEL1",
        17: "SEL2",
        18: "CEC_A",
        19: "HPD_A",
        20: "CEC_B",
        21: "HPD_B",
        22: "D3-B",
        23: "D3+B",
        24: "D2-B",
        25: "D2+B",
        26: "D1-B",
        27: "D1+B",
        28: "D0-B",
        29: "D0+B",
        30: "NC",
        31: "D3-A",
        32: "D3+A",
        33: "D2-A",
        34: "D2+A",
        35: "D1-A",
        36: "D1+A",
        37: "D0-A",
        38: "D0+A",
        39: "SDA_B",
        40: "SCL_B",
        41: "SDA_A",
        42: "SCL_A",
        43: "EP_GND",
    }
    connections = {1: "HOST_3V3", 2: enable, 16: "HOST_3V3", 17: select, 43: "GND"}
    for prefix, numbers in (
        (common, (5, 6, 7, 8, 10, 11)),
        (bank_a, (38, 37, 36, 35, 34, 33)),
        (bank_b, (29, 28, 27, 26, 25, 24)),
    ):
        for number, suffix in zip(
            numbers,
            ("CLK_P", "CLK_N", "D1_P", "D1_N", "D0_P", "D0_N"),
            strict=True,
        ):
            connections[number] = prefix + "_" + suffix
    pins = {
        str(number): (
            name,
            connections.get(number),
            "power_in" if number in {1, 43} else "input" if number in {2, 16, 17} else "passive",
        )
        for number, name in names.items()
    }
    return component(
        reference,
        "TS3DV642RUARQ1",
        "Package_DFN_QFN:WQFN-42-1EP_3.5x9mm_P0.5mm_EP2.05x7.55mm",
        pins,
        schematic,
        board,
    )


def design():
    host = {
        1: "GND",
        2: "HOST_D0_N",
        3: "HOST_D0_P",
        4: "GND",
        5: "HOST_D1_N",
        6: "HOST_D1_P",
        7: "GND",
        8: "HOST_CLK_N",
        9: "HOST_CLK_P",
        10: "GND",
        11: None,
        12: None,
        13: "GND",
        14: None,
        15: None,
        16: "GND",
        17: "CAM_IO0",
        18: "HOST_IO1",
        19: "GND",
        20: "HOST_SCL",
        21: "HOST_SDA",
        22: "HOST_3V3",
    }
    component(
        "J1",
        "F32Q-1A7H1-11022_HOST",
        "Connector_FFC-FPC:Amphenol_F32Q-1A7x1-11022_1x22-1MP_P0.5mm_Horizontal",
        {str(n): (net or "UNUSED_4LANE", net, "passive") for n, net in host.items()},
        (45.72, 96.52),
        (9, 54, 90),
    )
    for index, prefix in enumerate(("CAM_A", "CAM_B", "CAM_C", "CAM_D")):
        nets = {
            1: "GND",
            2: prefix + "_D0_N",
            3: prefix + "_D0_P",
            4: "GND",
            5: prefix + "_D1_N",
            6: prefix + "_D1_P",
            7: "GND",
            8: prefix + "_CLK_N",
            9: prefix + "_CLK_P",
            10: "GND",
            11: "CAM_IO0",
            12: prefix + "_IO1",
            13: prefix + "_SCL",
            14: prefix + "_SDA",
            15: "CAM_3V3",
        }
        component(
            f"J{index + 2}",
            "TE_1-84953-5_" + prefix,
            "Connector_FFC-FPC:TE_1-84953-5_1x15-1MP_P1.0mm_Horizontal",
            {str(n): (net, net, "passive") for n, net in nets.items()},
            (414.02, 48.26 + index * 60.96),
            (116, 15 + index * 26, 90),
        )
    mux("U1", "HOST", "BANK_AB", "BANK_CD", "HOST_3V3", "GPIO17_BANK", (165.1, 96.52), (39, 54, 0))
    mux(
        "U2", "BANK_AB", "CAM_A", "CAM_B", "GPIO18_EN_AB", "GPIO4_PORT", (292.1, 58.42), (82, 28, 0)
    )
    mux(
        "U3", "BANK_CD", "CAM_C", "CAM_D", "GPIO17_BANK", "GPIO4_PORT", (292.1, 180.34), (82, 80, 0)
    )
    pca = {
        1: ("A0", "GND", "input"),
        2: ("A1", "GND", "input"),
        3: ("A2", "GND", "input"),
        4: ("INT0", "HOST_3V3", "input"),
        5: ("SD0", "CAM_A_SDA", "bidirectional"),
        6: ("SC0", "CAM_A_SCL", "bidirectional"),
        7: ("INT1", "HOST_3V3", "input"),
        8: ("SD1", "CAM_B_SDA", "bidirectional"),
        9: ("SC1", "CAM_B_SCL", "bidirectional"),
        10: ("GND", "GND", "power_in"),
        11: ("INT2", "HOST_3V3", "input"),
        12: ("SD2", "CAM_C_SDA", "bidirectional"),
        13: ("SC2", "CAM_C_SCL", "bidirectional"),
        14: ("INT3", "HOST_3V3", "input"),
        15: ("SD3", "CAM_D_SDA", "bidirectional"),
        16: ("SC3", "CAM_D_SCL", "bidirectional"),
        17: ("INT", None, "open_collector"),
        18: ("SCL", "HOST_SCL", "input"),
        19: ("SDA", "HOST_SDA", "bidirectional"),
        20: ("VCC", "HOST_3V3", "power_in"),
    }
    component(
        "U4",
        "PCA9544APWR_ADDR_0x70",
        "Package_SO:Texas_PW0020A_TSSOP-20_4.4x6.5mm_P0.65mm",
        {str(n): pin for n, pin in pca.items()},
        (556.26, 96.52),
        (19, 82, 0),
    )
    switch = {
        "1": ("VIN", "EXT_3V3", "power_in"),
        "2": ("GND", "GND", "power_in"),
        "3": ("ON", "HOST_3V3", "input"),
        "4": ("CT", "SLEW_CT", "passive"),
        "5": ("QOD", "DISCHARGE", "passive"),
        "6": ("VOUT", "CAM_3V3", "power_out"),
    }
    component(
        "U5", "TPS22918DBVR", "Package_TO_SOT_SMD:SOT-23-6", switch, (556.26, 243.84), (44, 96, 0)
    )
    component(
        "J6",
        "GPIO_GND_4_17_18",
        "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
        {
            str(n): (net, net, "passive")
            for n, net in enumerate(("GND", "GPIO4_PORT", "GPIO17_BANK", "GPIO18_EN_AB"), 1)
        },
        (45.72, 251.46),
        (8, 83, 0),
    )
    component(
        "J7",
        "REGULATED_3V3_INPUT",
        "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        {"1": ("EXT_3V3", "EXT_3V3", "passive"), "2": ("GND", "GND", "passive")},
        (650.24, 243.84),
        (37, 102, 90),
    )
    caps = [
        ("C1", "100nF", "HOST_3V3", "GND", (36, 47, 0)),
        ("C2", "100nF", "HOST_3V3", "GND", (79, 21, 0)),
        ("C3", "100nF", "HOST_3V3", "GND", (79, 73, 0)),
        ("C4", "100nF", "HOST_3V3", "GND", (22.7, 76.8, 0)),
        ("C5", "10uF_10V_X5R", "EXT_3V3", "GND", (39, 95, 90)),
        ("C6", "10uF_10V_X5R", "CAM_3V3", "GND", (49, 95, 90)),
        ("C7", "1nF", "SLEW_CT", "GND", (44, 91, 0)),
    ]
    for index, (ref, value, first, second, board) in enumerate(caps):
        item = passive(ref, value, first, second, (50.8 + index * 76.2, 340.36), board)
        if ref in {"C5", "C6"}:
            item["footprint"] = "Capacitor_SMD:C_0805_2012Metric"
    resistors = [
        ("R1", "10k", "GPIO4_PORT", "GND", (11, 100, 0), False),
        ("R2", "10k", "GPIO17_BANK", "GND", (18, 100, 0), False),
        ("R3", "10k", "GPIO18_EN_AB", "GND", (25, 100, 0), False),
        ("R4", "100k", "HOST_3V3", "GND", (40, 88, 0), False),
        ("R5", "100R", "DISCHARGE", "CAM_3V3", (48, 100, 0), False),
        ("R6", "4.7k_DNP", "HOST_SCL", "HOST_3V3", (12, 68, 0), True),
        ("R7", "4.7k_DNP", "HOST_SDA", "HOST_3V3", (20, 68, 0), True),
    ]
    for index, prefix in enumerate(("CAM_A", "CAM_B", "CAM_C", "CAM_D")):
        for channel, signal in enumerate(("SCL", "SDA")):
            number = 8 + index * 2 + channel
            resistors.append(
                (
                    f"R{number}",
                    "4.7k",
                    prefix + "_" + signal,
                    "CAM_3V3",
                    (106 + channel * 5, 6 + index * 26, 90),
                    False,
                )
            )
    for index, prefix in enumerate(("CAM_A", "CAM_B", "CAM_C", "CAM_D")):
        resistors.append(
            (
                f"R{16 + index}",
                "0R_DNP_IO1",
                "HOST_IO1",
                prefix + "_IO1",
                (110, 11 + index * 26, 90),
                True,
            )
        )
    for index, (ref, value, first, second, board, dnp) in enumerate(resistors):
        passive(
            ref,
            value,
            first,
            second,
            (50.8 + (index % 7) * 76.2, 391.16 + (index // 7) * 50.8),
            board,
            dnp=dnp,
        )
    for index, net in enumerate(("HOST_3V3", "EXT_3V3", "GND")):
        component(
            f"#FLG0{index + 1}",
            "PWR_FLAG",
            "",
            {"1": ("pwr", net, "power_out")},
            (650.24 + index * 55.88, 340.36),
            None,
        )


def pin_layout(item):
    pins = list(item["pins"])
    if item["reference"].startswith("U"):
        left = [
            pin
            for pin in pins
            if (int(pin) <= (17 if len(pins) > 40 else (len(pins) + 1) // 2) or pin == "43")
        ]
        right = [pin for pin in pins if pin not in left]
    else:
        left, right = [], pins
    layout = {}
    for side, numbers in ((-1, left), (1, right)):
        for index, number in enumerate(numbers):
            layout[number] = (
                side * 17.78,
                round((len(numbers) - 1) * 1.27 - index * 2.54, 3),
                0 if side < 0 else 180,
            )
    return layout


def library_symbol(item, name):
    layout = pin_layout(item)
    height = max(5.08, max(abs(y) for _x, y, _a in layout.values()) + 2.54)
    prefix = item["reference"].rstrip("0123456789")
    elements = [
        f"(symbol {quoted(name)} (pin_names (offset 0.762)) (in_bom yes) (on_board yes)",
        f'(property "Reference" {quoted(prefix)} (at 0 {height + 5.08} 0) '
        "(effects (font (size 1.27 1.27))))",
        f'(property "Value" {quoted(item["value"])} (at 0 {height + 2.54} 0) '
        "(effects (font (size 1.27 1.27))))",
        f"(symbol {quoted(item['symbol'] + '_0_1')} "
        f"(rectangle (start -10.16 {height}) (end 10.16 {-height}) "
        "(stroke (width 0.254) (type default)) (fill (type background))))",
        f"(symbol {quoted(item['symbol'] + '_1_1')}",
    ]
    for number, (pin_name, _net, kind) in item["pins"].items():
        x, y, angle = layout[number]
        elements.append(
            f"(pin {kind} line (at {x} {y} {angle}) (length 7.62) "
            f"(name {quoted(pin_name)} (effects (font (size 0.762 0.762)))) "
            f"(number {quoted(number)} (effects (font (size 0.762 0.762)))))"
        )
    return "\n".join(elements) + "))"


def schematic():
    libraries = {}
    for item in COMPONENTS:
        libraries.setdefault(item["symbol"], item)
    contents = [
        '(kicad_sch (version 20231120) (generator "eeschema")',
        f'(uuid {PROJECT_UUID}) (paper "A1")',
        '(title_block (title "Pi 5 / four-camera CSI selector - engineering prototype") '
        '(date "2026-09-30") (rev "0.1") (company "Simon hardware example") '
        '(comment 1 "Two CSI data lanes; ONE selected camera; separate 3.3V camera supply") '
        '(comment 2 "Not fabrication released: validate routing, SI, power and software"))',
        "(lib_symbols",
        *[library_symbol(item, LIBRARY + ":" + name) for name, item in libraries.items()],
        ")",
    ]
    for item in COMPONENTS:
        x, y = item["schematic"]
        layout = pin_layout(item)
        height = max(5.08, max(abs(py) for _px, py, _a in layout.values()) + 2.54)
        contents.extend(
            [
                f"(symbol (lib_id {quoted(LIBRARY + ':' + item['symbol'])}) (at {x} {y} 0) "
                f"(unit 1) (in_bom {'no' if item['reference'].startswith('#') else 'yes'}) "
                f"(on_board {'no' if item['board'] is None else 'yes'}) "
                f"(dnp {'yes' if item['dnp'] else 'no'}) (uuid {item['uuid']})",
                f'(property "Reference" {quoted(item["reference"])} (at {x} {y - height - 5.08} 0) '
                "(effects (font (size 1.27 1.27))))",
                f'(property "Value" {quoted(item["value"])} (at {x} {y - height - 2.54} 0) '
                "(effects (font (size 1.016 1.016))))",
                f'(property "Footprint" {quoted(item["footprint"])} (at {x} {y} 0) '
                "(effects (font (size 1.27 1.27)) hide))",
                *[
                    f"(pin {quoted(number)} (uuid {uid(item['reference'] + 'pin' + number)}))"
                    for number in item["pins"]
                ],
                f'(instances (project {quoted(NAME)} (path "/{PROJECT_UUID}" '
                f"(reference {quoted(item['reference'])}) (unit 1)))))",
            ]
        )
        for number, (_name, net, _kind) in item["pins"].items():
            px, py, _angle = layout[number]
            ax, ay = round(x + px, 3), round(y - py, 3)
            if net is None:
                contents.append(
                    f"(no_connect (at {ax} {ay}) (uuid {uid(item['reference'] + 'nc' + number)}))"
                )
                continue
            bx = round(ax + (5.08 if px > 0 else -5.08), 3)
            contents.append(
                f"(wire (pts (xy {ax} {ay}) (xy {bx} {ay})) "
                "(stroke (width 0) (type default)) "
                f"(uuid {uid(item['reference'] + 'wire' + number)}))"
            )
            justify = "left" if px > 0 else "right"
            contents.append(
                f"(label {quoted(net)} (at {bx} {ay} 0) "
                "(effects (font (size 0.9 0.9)) "
                f"(justify {justify} bottom)) "
                f"(uuid {uid(item['reference'] + 'label' + number)}))"
            )
    notes = [
        (20.32, 20.32, "SIGNAL TREE | Switch channels D0=CSI clock, D1=CSI DATA1, D2=CSI DATA0"),
        (505.46, 20.32, "CONTROL / PCA9544A address 0x70; individual downstream I2C buses"),
        (505.46, 190.5, "POWER / regulated external 3.3V, current-limited supply; common ground"),
        (
            20.32,
            302.26,
            "DECOUPLING AND CONTROL | 10k GPIO pulls disable both leaf switches at reset",
        ),
        (20.32, 551.18, "A: GPIO4/18/17 = 0/1/0; B = 1/1/0; C = 0/0/1; D = 1/0/1"),
        (
            20.32,
            561.34,
            "Camera IO0 is common for probing. IO1 links R16-R19 are DNP "
            "pending camera-specific review.",
        ),
        (
            20.32,
            571.5,
            "R6/R7 DNP: host pull-ups normally present. "
            "Check parallel module pull-ups before fitting R8-R15.",
        ),
    ]
    for index, (x, y, text) in enumerate(notes):
        contents.append(
            f"(text {quoted(text)} (at {x} {y} 0) "
            "(effects (font (size 1.524 1.524)) (justify left)) "
            f"(uuid {uid('note' + str(index))}))"
        )
    contents.append('(sheet_instances (path "/" (page "1"))))')
    (ROOT / (NAME + ".kicad_sch")).write_text("\n".join(contents), encoding="utf-8")
    symbols = '(kicad_symbol_lib (version 20231120) (generator "kicad_symbol_editor")\n'
    symbols += "\n".join(library_symbol(item, name) for name, item in libraries.items()) + ")"
    (ROOT / (LIBRARY + ".kicad_sym")).write_text(symbols, encoding="utf-8")
    (ROOT / "sym-lib-table").write_text(
        f'(sym_lib_table (version 7) (lib (name "{LIBRARY}") (type "KiCad") '
        f'(uri "${{KIPRJMOD}}/{LIBRARY}.kicad_sym") (options "") '
        '(descr "Project pin-verified symbols")))',
        encoding="utf-8",
    )


def make_board():
    board = pcbnew.BOARD()
    board.SetFileName(str(ROOT / (NAME + ".kicad_pcb")))
    board.SetCopperLayerCount(4)
    connectivity = {}
    for net in ET.parse(ROOT / (NAME + ".net")).findall("./nets/net"):
        for node in net.findall("node"):
            connectivity[(node.attrib["ref"], node.attrib["pin"])] = net.attrib["name"]
    nets = {}
    for index, name in enumerate(sorted(set(connectivity.values())), 1):
        net = pcbnew.NETINFO_ITEM(board, name, index)
        board.Add(net)
        nets[name] = net
    footprints = {}
    for item in COMPONENTS:
        if item["board"] is None:
            continue
        library, name = item["footprint"].split(":")
        footprint = pcbnew.FootprintLoad("/usr/share/kicad/footprints/" + library + ".pretty", name)
        if footprint is None:
            raise RuntimeError("Missing footprint: " + item["footprint"])
        footprint.SetReference(item["reference"])
        footprint.SetValue(item["value"])
        footprint.SetFPID(pcbnew.LIB_ID(library, name))
        footprint.SetPath(pcbnew.KIID_PATH("/" + PROJECT_UUID + "/" + item["uuid"]))
        footprint.SetDNP(item["dnp"])
        board.Add(footprint)
        footprints[item["reference"]] = footprint
        x, y, angle = item["board"]
        footprint.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
        footprint.SetOrientationDegrees(angle)
        footprint.Reference().SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.9), pcbnew.FromMM(0.9)))
        footprint.Reference().SetTextThickness(pcbnew.FromMM(0.15))
        for number in item["pins"]:
            pad = footprint.FindPadByNumber(number)
            if pad is None:
                raise RuntimeError(item["reference"] + " footprint lacks pin " + number)
            assigned = connectivity.get((item["reference"], number))
            if assigned is not None:
                pad.SetNet(nets[assigned])
    for first, second in (
        ((0, 0), (125, 0)),
        ((125, 0), (125, 110)),
        ((125, 110), (0, 110)),
        ((0, 110), (0, 0)),
    ):
        edge = pcbnew.PCB_SHAPE()
        edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
        edge.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(first[0]), pcbnew.FromMM(first[1])))
        edge.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(second[0]), pcbnew.FromMM(second[1])))
        edge.SetLayer(pcbnew.Edge_Cuts)
        edge.SetWidth(pcbnew.FromMM(0.05))
        board.Add(edge)
    for value, x, y in (
        ("PI5 / 4x CSI CAMERA SELECTOR", 4, 4),
        ("REV0.1 - ENGINEERING PROTOTYPE", 4, 7),
        ("EXT 3V3 ONLY / GND COMMON", 27, 107),
    ):
        text = pcbnew.PCB_TEXT(board)
        text.SetText(value)
        text.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
        text.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(1), pcbnew.FromMM(1)))
        text.SetTextThickness(pcbnew.FromMM(0.15))
        text.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_LEFT)
        text.SetLayer(pcbnew.F_SilkS)
        board.Add(text)
    paired_routes(board, footprints)
    from finish_routing import add_planes

    add_planes(board)
    pcbnew.SaveBoard(str(ROOT / (NAME + ".kicad_pcb")), board)
    board = pcbnew.LoadBoard(str(ROOT / (NAME + ".kicad_pcb")))
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(str(ROOT / (NAME + ".kicad_pcb")), board)
    # KiCad performs the final plane fill. Export fixed vias/tracks but omit
    # plane polygons from the outer-layer router, whose plane handling cannot
    # reliably connect this project's SMD islands to non-routable inner layers.
    for zone in list(board.Zones()):
        board.Remove(zone)
    pcbnew.ExportSpecctraDSN(board, str(ROOT / (NAME + ".dsn")))
    # Explicit library table keeps footprint provenance checkable in the worker
    # and in a normal KiCad installation with KICAD9_FOOTPRINT_DIR defined.
    libraries = sorted(
        {item["footprint"].split(":")[0] for item in COMPONENTS if item["footprint"]}
    )
    (ROOT / "fp-lib-table").write_text(
        "(fp_lib_table (version 7)\n"
        + "\n".join(
            f'(lib (name "{name}") (type "KiCad") '
            f'(uri "${{KICAD9_FOOTPRINT_DIR}}/{name}.pretty") (options "") (descr ""))'
            for name in libraries
        )
        + ")",
        encoding="utf-8",
    )


def paired_routes(board, footprints):
    """Keep each CSI pair together on front copper, with no signal vias.

    This geometric prototype uses 0.15 mm copper and 0.22 mm pair gap through
    its straight trunk. Connector/package fanouts taper from their pad spacing.
    The stack-up still requires a fabricator's impedance calculation and SI review.
    """
    common = ((5, 6), (7, 8), (10, 11))
    bank_a = ((38, 37), (36, 35), (34, 33))
    bank_b = ((29, 28), (27, 26), (25, 24))
    camera = ((9, 8), (6, 5), (3, 2))
    links = [
        ("J1", camera, "U1", common),
        ("U1", bank_a, "U2", common),
        ("U1", bank_b, "U3", common),
        ("U2", bank_a, "J2", camera),
        ("U2", bank_b, "J3", camera),
        ("U3", bank_a, "J4", camera),
        ("U3", bank_b, "J5", camera),
    ]

    def point(pad):
        return (pcbnew.ToMM(pad.GetPosition().x), pcbnew.ToMM(pad.GetPosition().y))

    for source_ref, source_pins, target_ref, target_pins in links:
        for pair_source, pair_target in zip(source_pins, target_pins, strict=True):
            sources = [footprints[source_ref].FindPadByNumber(str(pin)) for pin in pair_source]
            targets = [footprints[target_ref].FindPadByNumber(str(pin)) for pin in pair_target]
            start, end = [point(pad) for pad in sources], [point(pad) for pad in targets]
            sx, sy = start[0][0], sum(p[1] for p in start) / 2
            tx, ty = end[0][0], sum(p[1] for p in end) / 2
            dx, dy = tx - sx - 5, ty - sy
            length = math.hypot(dx, dy)
            ux, uy = dx / length, dy / length
            if sx >= tx:
                raise RuntimeError("Paired layout requires left-to-right pad ordering")
            for index in range(2):
                offset = -0.185 if index == 0 else 0.185
                # Intersect the two offset lines at each bend (miter), instead
                # of narrowing pair clearance while joining shifted endpoints.
                shift = -offset * uy / (1 + ux)
                path = [
                    start[index],
                    (sx + 0.6, start[index][1]),
                    (sx + 1.4, sy + offset),
                    (sx + 2.5 + shift, sy + offset),
                    (tx - 2.5 + shift, ty + offset),
                    (tx - 1.4, ty + offset),
                    (tx - 0.6, end[index][1]),
                    end[index],
                ]
                for first, second in itertools.pairwise(path):
                    if math.dist(first, second) < 0.000001:
                        continue
                    track = pcbnew.PCB_TRACK(board)
                    track.SetStart(
                        pcbnew.VECTOR2I(pcbnew.FromMM(first[0]), pcbnew.FromMM(first[1]))
                    )
                    track.SetEnd(
                        pcbnew.VECTOR2I(pcbnew.FromMM(second[0]), pcbnew.FromMM(second[1]))
                    )
                    track.SetWidth(pcbnew.FromMM(0.15))
                    track.SetLayer(pcbnew.F_Cu)
                    track.SetNet(sources[index].GetNet())
                    track.SetLocked(True)
                    board.Add(track)


if __name__ == "__main__":
    design()
    schematic()
    subprocess.run(
        [
            "/usr/bin/kicad-cli",
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadxml",
            "--output",
            str(ROOT / (NAME + ".net")),
            str(ROOT / (NAME + ".kicad_sch")),
        ],
        check=True,
    )
    make_board()
    (ROOT / (NAME + ".kicad_pro")).write_text(
        json.dumps(
            {
                "meta": {"filename": NAME + ".kicad_pro", "version": 1},
                "board": {
                    "design_settings": {
                        "rules": {
                            "min_clearance": 0.15,
                            "min_track_width": 0.15,
                            "min_copper_edge_clearance": 0.3,
                        }
                    }
                },
                "erc": {"erc_exclusions": []},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Generated {len(COMPONENTS)} symbols and {len(NETS)} named nets")
