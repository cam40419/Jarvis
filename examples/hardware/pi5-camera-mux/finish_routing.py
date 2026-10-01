"""Import the outer-layer router session and add explicit inner power/reference planes.

Run in a copied project directory with KiCad's /usr/bin/python3 after generate.py
and the documented offline Freerouting command. This script is prototype CAD
automation, not a substitute for stack-up, thermal, SI or manufacturing review.
"""

from itertools import pairwise
from pathlib import Path

import pcbnew

ROOT = Path(__file__).resolve().parent
NAME = "pi5-camera-mux"


def point(x, y):
    return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))


def add_planes(board):
    """Reserve power and exposed-pad vias before outer-layer autorouting."""
    footprints = {item.GetReference(): item for item in board.GetFootprints()}
    # The external rail carries all camera current before the load switch.
    # Keep it short and wider than the default low-current control routing.
    supply = footprints["J7"].FindPadByNumber("1")
    supply_path = [supply.GetPosition(), point(37, 98), point(39.1, 95.9),
                   point(41, 95.9), point(41.85, 95.05),
                   footprints["U5"].FindPadByNumber("1").GetPosition()]
    for start, end in pairwise(supply_path):
        trace = pcbnew.PCB_TRACK(board)
        trace.SetStart(start)
        trace.SetEnd(end)
        trace.SetWidth(pcbnew.FromMM(0.6))
        trace.SetLayer(pcbnew.F_Cu)
        trace.SetNet(supply.GetNet())
        trace.SetLocked(True)
        board.Add(trace)
    for name, layer in (("/GND", pcbnew.In1_Cu), ("/CAM_3V3", pcbnew.In2_Cu)):
        zone = pcbnew.ZONE(board)
        zone.SetLayer(layer)
        zone.SetNet(board.FindNet(name))
        zone.SetLocalClearance(pcbnew.FromMM(0.2))
        zone.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
        zone.SetMinThickness(pcbnew.FromMM(0.15))
        polygon = zone.Outline()
        polygon.NewOutline()
        for x, y in ((0.5, 0.5), (124.5, 0.5), (124.5, 109.5), (0.5, 109.5)):
            polygon.Append(int(pcbnew.FromMM(x)), int(pcbnew.FromMM(y)))
        board.Add(zone)

    # Short local camera-power connections reach a dedicated inner plane. Their
    # copper geometry and plated vias still need the chosen fabricator's review.
    for reference, pin, dx, dy in (
        ("J2", "15", -2.0, 0), ("J3", "15", -2.0, 0),
        ("J4", "15", -2.0, 0), ("J5", "15", -2.0, 0),
        ("C6", "1", 0, 1.5), ("U5", "6", 2.0, 0),
    ):
        pad = footprints[reference].FindPadByNumber(pin)
        if pad.GetNetname() != "/CAM_3V3":
            raise RuntimeError("Power via reference is not on CAM_3V3")
        start = pad.GetPosition()
        end = start + point(dx, dy)
        trace = pcbnew.PCB_TRACK(board)
        trace.SetStart(start)
        trace.SetEnd(end)
        trace.SetWidth(pcbnew.FromMM(0.5))
        trace.SetLayer(pcbnew.F_Cu)
        trace.SetNet(pad.GetNet())
        trace.SetLocked(True)
        board.Add(trace)
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(end)
        via.SetWidth(pcbnew.F_Cu, pcbnew.FromMM(0.6))
        via.SetDrill(pcbnew.FromMM(0.3))
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetNet(pad.GetNet())
        via.SetLocked(True)
        board.Add(via)
    # The switch exposed ground pads need real plated connections to the inner
    # reference plane. These are intentional thermal vias in the exposed pad;
    # fabrication must specify filling/tenting and an appropriate paste pattern.
    for reference in ("U1", "U2", "U3"):
        pad = footprints[reference].FindPadByNumber("43")
        for delta_y in (-2.5, 0, 2.5):
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pad.GetPosition() + point(0, delta_y))
            via.SetWidth(pcbnew.F_Cu, pcbnew.FromMM(0.6))
            via.SetDrill(pcbnew.FromMM(0.3))
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetNet(pad.GetNet())
            via.SetLocked(True)
            board.Add(via)
def finish():
    board = pcbnew.LoadBoard(str(ROOT / (NAME + ".kicad_pcb")))
    if not pcbnew.ImportSpecctraSES(board, str(ROOT / (NAME + ".ses"))):
        raise RuntimeError("Router session import failed")
    # The router's 41 um junction at J4 changed from 0.15 to 0.20 mm for one
    # segment, leaving 0.1987 mm clearance to clock+. Match its two adjacent
    # ground fanouts' 0.15 mm width; keep the project clearance rule unchanged.
    for track in board.GetTracks():
        if (not isinstance(track, pcbnew.PCB_VIA) and track.GetNetname() == "/GND"
                and pcbnew.ToMM(track.GetLength()) < 0.05
                and 115.4 < pcbnew.ToMM(track.GetStart().x) < 115.6
                and 66.3 < pcbnew.ToMM(track.GetStart().y) < 66.5):
            track.SetWidth(pcbnew.FromMM(0.15))
    labels = [("HOST / TOP CONTACT", 4, 65), ("1", 3.5, 59.25),
              ("GPIO: GND 4 17 18", 4, 96), ("J7: 1=3V3 2=GND", 28, 105)]
    for index, name in enumerate(("A", "B", "C", "D")):
        labels.extend(((f"CAM {name} / TOP", 112, 28 + 26 * index),
                       ("1", 111.5, 22 + 26 * index)))
    for value, x, y in labels:
        text = pcbnew.PCB_TEXT(board)
        text.SetText(value)
        text.SetPosition(point(x, y))
        text.SetTextSize(point(0.8, 0.8))
        text.SetTextThickness(pcbnew.FromMM(0.12))
        text.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_LEFT)
        text.SetLayer(pcbnew.F_SilkS)
        board.Add(text)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(str(ROOT / (NAME + ".kicad_pcb")), board)


if __name__ == "__main__":
    finish()
