"""Independently check exported schematic connectivity against engineering-review.md.

Run with ordinary Python; no KiCad import and no dependency on generate.py. The
report covers named-net connectivity, not PCB copper, analog behavior, or cables.
"""

from __future__ import annotations

import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def review(source: Path) -> dict:
    document = ET.fromstring(source.read_bytes())
    nets = {
        net.attrib["name"].lstrip("/"): {
            (node.attrib["ref"], node.attrib["pin"]) for node in net.findall("node")
        }
        for net in document.findall("./nets/net")
    }
    by_pin = {pin: name for name, pins in nets.items() for pin in pins}
    components = {item.attrib["ref"]: item for item in document.findall("./components/comp")}
    checks = []

    def check(name: str, passed: bool) -> None:
        checks.append({"check": name, "passed": bool(passed)})

    def exact(name: str, pins: set[tuple[str, str]]) -> None:
        check(f"{name}: exact endpoints", nets.get(name) == pins)

    def pin(ref: str, number: int, name: str) -> None:
        check(f"{ref}.{number} = {name}", by_pin.get((ref, str(number))) == name)

    def isolated(ref: str, number: int) -> None:
        item = (ref, str(number))
        check(f"{ref}.{number} unconnected", nets.get(by_pin.get(item, "")) == {item})

    # Raspberry Pi connector pin tables; TI TS3DV642-Q1 pin table. Lists use
    # N/P order independently of the generator's P/N implementation.
    suffixes = ("D0_N", "D0_P", "D1_N", "D1_P", "CLK_N", "CLK_P")
    connector_pins = (2, 3, 5, 6, 8, 9)
    # Layout revision uses switch D2 for camera DATA0, D1 for DATA1 and D0
    # for CLOCK. SEL1 is always high, enabling all three equivalent channels.
    common_pins = (11, 10, 8, 7, 6, 5)
    bank_a_pins = (33, 34, 35, 36, 37, 38)
    bank_b_pins = (24, 25, 26, 27, 28, 29)
    segments = [
        ("HOST", "J1", connector_pins, "U1", common_pins),
        ("BANK_AB", "U1", bank_a_pins, "U2", common_pins),
        ("BANK_CD", "U1", bank_b_pins, "U3", common_pins),
        ("CAM_A", "U2", bank_a_pins, "J2", connector_pins),
        ("CAM_B", "U2", bank_b_pins, "J3", connector_pins),
        ("CAM_C", "U3", bank_a_pins, "J4", connector_pins),
        ("CAM_D", "U3", bank_b_pins, "J5", connector_pins),
    ]
    for prefix, left, left_pins, right, right_pins in segments:
        for suffix, first, second in zip(suffixes, left_pins, right_pins, strict=True):
            exact(f"{prefix}_{suffix}", {(left, str(first)), (right, str(second))})

    for number in (1, 4, 7, 10, 13, 16, 19):
        pin("J1", number, "GND")
    for number in (11, 12, 14, 15):
        isolated("J1", number)
    pin("J1", 22, "HOST_3V3")
    pin("J1", 17, "CAM_IO0")
    pin("J1", 18, "HOST_IO1")
    exact("CAM_IO0", {("J1", "17"), *[(f"J{n}", "11") for n in range(2, 6)]})

    for index, camera in enumerate("ABCD"):
        ref = f"J{index + 2}"
        for number in (1, 4, 7, 10):
            pin(ref, number, "GND")
        pin(ref, 15, "CAM_3V3")
        resistor = f"R{index + 16}"
        exact(f"CAM_{camera}_IO1", {(ref, "12"), (resistor, "2")})
        pin(resistor, 1, "HOST_IO1")
        check(
            f"{resistor}: IO1 link DNP",
            components[resistor].find("property[@name='dnp']") is not None,
        )
        sda, scl = ((5, 6), (8, 9), (12, 13), (15, 16))[index]
        exact(f"CAM_{camera}_SDA", {(ref, "14"), ("U4", str(sda)), (f"R{9 + index * 2}", "1")})
        exact(f"CAM_{camera}_SCL", {(ref, "13"), ("U4", str(scl)), (f"R{8 + index * 2}", "1")})
        for pullup in (8 + index * 2, 9 + index * 2):
            pin(f"R{pullup}", 2, "CAM_3V3")
        check(
            f"{ref}: TE upper-contact footprint",
            components[ref].findtext("footprint")
            == "Connector_FFC-FPC:TE_1-84953-5_1x15-1MP_P1.0mm_Horizontal",
        )

    for ref in ("U1", "U2", "U3"):
        pin(ref, 1, "HOST_3V3")
        pin(ref, 16, "HOST_3V3")
        pin(ref, 43, "GND")
        for number in (3, 4, 9, 12, 13, 14, 15, 18, 19, 20, 21, 22, 23, 30, 31, 32, 39, 40, 41, 42):
            isolated(ref, number)
    pin("U1", 2, "HOST_3V3")
    exact("GPIO4_PORT", {("J6", "2"), ("R1", "1"), ("U2", "17"), ("U3", "17")})
    exact("GPIO17_BANK", {("J6", "3"), ("R2", "1"), ("U1", "17"), ("U3", "2")})
    exact("GPIO18_EN_AB", {("J6", "4"), ("R3", "1"), ("U2", "2")})
    for ref in ("R1", "R2", "R3", "R4"):
        pin(ref, 2, "GND")
    pin("J6", 1, "GND")

    for number in (1, 2, 3, 10):
        pin("U4", number, "GND")
    for number in (4, 7, 11, 14, 20):
        pin("U4", number, "HOST_3V3")
    isolated("U4", 17)
    exact("HOST_SCL", {("J1", "20"), ("U4", "18"), ("R6", "1")})
    exact("HOST_SDA", {("J1", "21"), ("U4", "19"), ("R7", "1")})
    for ref in ("R6", "R7"):
        check(
            f"{ref}: extra host pullup DNP",
            components[ref].find("property[@name='dnp']") is not None,
        )

    # TPS22918 pin table, including discharge path and three separate rails.
    for number, name in {
        1: "EXT_3V3",
        2: "GND",
        3: "HOST_3V3",
        4: "SLEW_CT",
        5: "DISCHARGE",
        6: "CAM_3V3",
    }.items():
        pin("U5", number, name)
    exact("EXT_3V3", {("J7", "1"), ("U5", "1"), ("C5", "1")})
    exact("DISCHARGE", {("U5", "5"), ("R5", "1")})
    exact("SLEW_CT", {("U5", "4"), ("C7", "1")})
    pin("R5", 2, "CAM_3V3")
    pin("J7", 2, "GND")
    pin("R4", 1, "HOST_3V3")
    for index in range(1, 8):
        pin(f"C{index}", 2, "GND")
    for index in range(1, 5):
        pin(f"C{index}", 1, "HOST_3V3")
    pin("C6", 1, "CAM_3V3")

    return {
        "source": source.name,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "scope": "Independent schematic-netlist connectivity only; no fabrication or SI approval",
        "passed": all(item["passed"] for item in checks),
        "check_count": len(checks),
        "checks": checks,
    }


if __name__ == "__main__":
    directory = Path(__file__).resolve().parent
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else directory / "pi5-camera-mux.net"
    result = review(source)
    (directory / "connectivity-review.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in result.items() if key != "checks"}, indent=2))
    for item in result["checks"]:
        if not item["passed"]:
            print("FAIL: " + item["check"])
    sys.exit(0 if result["passed"] else 1)
