"""Read-only PCB geometry audit; run with KiCad's Python (pcbnew) installed.

Prints JSON to stdout. Counts and lengths are evidence, not impedance, SI, or
manufacturing approval. Trace lengths exclude packages, FFCs and via barrels.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pcbnew


def review(source: Path) -> dict:
    board = pcbnew.LoadBoard(str(source))
    expected = {
        (node.attrib["ref"], node.attrib["pin"]): net.attrib["name"]
        for net in ET.parse(source.with_suffix(".net")).findall("./nets/net")
        for node in net.findall("node")
    }
    actual = {
        (footprint.GetReference(), pad.GetNumber()): pad.GetNetname()
        for footprint in board.GetFootprints()
        for pad in footprint.Pads()
        if pad.GetNumber() and pad.GetNumber() != "MP"
    }
    mismatches = [
        {"reference": ref, "pin": pin, "schematic": net, "board": actual.get((ref, pin))}
        for (ref, pin), net in sorted(expected.items())
        if not ref.startswith("#") and actual.get((ref, pin)) != net
    ]
    tracks = list(board.GetTracks())
    pad_positions = {
        (footprint.GetReference(), pad.GetNumber()): pad.GetPosition()
        for footprint in board.GetFootprints()
        for pad in footprint.Pads()
    }

    def chain(name: str) -> dict:
        """Reject stubs, islands, cycles, duplicate copper, and wrong pad endpoints."""
        members = [item for item in tracks if item.GetNetname().lstrip("/") == name]
        segments = [item for item in members if type(item) is pcbnew.PCB_TRACK]

        # One-micrometre quantization accommodates DSN coordinate serialization.
        def coordinate(point):
            return (round(point.x / 1000), round(point.y / 1000))

        edges = [(coordinate(item.GetStart()), coordinate(item.GetEnd())) for item in segments]
        graph = {}
        for first, second in edges:
            graph.setdefault(first, []).append(second)
            graph.setdefault(second, []).append(first)
        remaining = set(graph)
        components = 0
        while remaining:
            components += 1
            pending = [remaining.pop()]
            while pending:
                for neighbor in graph[pending.pop()]:
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        pending.append(neighbor)
        actual_ends = {node for node, links in graph.items() if len(links) == 1}
        wanted = [pin for pin, net in expected.items() if net.lstrip("/") == name]
        expected_ends = {coordinate(pad_positions[pin]) for pin in wanted}

        def orient(a, b, c):
            return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

        def interior(point, first, second):
            return (
                point not in (first, second)
                and orient(first, second, point) == 0
                and min(first[0], second[0]) <= point[0] <= max(first[0], second[0])
                and min(first[1], second[1]) <= point[1] <= max(first[1], second[1])
            )

        crossings = 0
        for index, (a, b) in enumerate(edges):
            for c, d in edges[index + 1 :]:
                if (
                    (
                        orient(a, b, c) * orient(a, b, d) < 0
                        and orient(c, d, a) * orient(c, d, b) < 0
                    )
                    or interior(a, c, d)
                    or interior(b, c, d)
                    or interior(c, a, b)
                    or interior(d, a, b)
                ):
                    crossings += 1
        duplicates = len(edges) - len({tuple(sorted(edge)) for edge in edges})
        branches = sum(len(links) > 2 for links in graph.values())
        front_only = all(item.GetLayer() == pcbnew.F_Cu for item in segments)
        return {
            "single_pad_to_pad_chain": bool(edges)
            and len(wanted) == 2
            and components == 1
            and actual_ends == expected_ends
            and not branches
            and not crossings
            and not duplicates
            and len(segments) == len(members)
            and front_only,
            "expected_pad_endpoints": [f"{ref}.{pin}" for ref, pin in wanted],
            "track_ends_match_pad_centres": actual_ends == expected_ends,
            "connected_components": components,
            "branch_nodes": branches,
            "duplicate_segments": duplicates,
            "interior_crossings_or_overlaps": crossings,
            "unsupported_arcs_or_vias": len(members) - len(segments),
            "all_segments_on_front": front_only,
            "coordinate_tolerance_um": 1,
        }

    def geometry(name: str) -> dict:
        members = [item for item in tracks if item.GetNetname().lstrip("/") == name]
        copper = [item for item in members if not isinstance(item, pcbnew.PCB_VIA)]
        return {
            "trace_length_mm": round(sum(pcbnew.ToMM(item.GetLength()) for item in copper), 4),
            "trace_segments": len(copper),
            "via_count": len(members) - len(copper),
            "layers": sorted({board.GetLayerName(item.GetLayer()) for item in copper}),
            "widths_mm": sorted({round(pcbnew.ToMM(item.GetWidth()), 4) for item in copper}),
        }

    prefixes = ("HOST", "BANK_AB", "BANK_CD", "CAM_A", "CAM_B", "CAM_C", "CAM_D")
    pairs = {}
    chains = {}
    for prefix in prefixes:
        for channel in ("D0", "D1", "CLK"):
            name = f"{prefix}_{channel}"
            for suffix in ("_N", "_P"):
                chains[name + suffix] = chain(name + suffix)
            negative, positive = geometry(name + "_N"), geometry(name + "_P")
            pairs[name] = {
                "negative": negative,
                "positive": positive,
                "absolute_trace_length_difference_mm": round(
                    abs(negative["trace_length_mm"] - positive["trace_length_mm"]), 4
                ),
            }
    paths = {}
    for camera in "ABCD":
        branch = "BANK_AB" if camera in "AB" else "BANK_CD"
        for channel in ("D0", "D1", "CLK"):
            parts = [pairs[f"{prefix}_{channel}"] for prefix in ("HOST", branch, f"CAM_{camera}")]
            totals = {
                polarity: round(sum(part[polarity]["trace_length_mm"] for part in parts), 4)
                for polarity in ("negative", "positive")
            }
            paths[f"CAM_{camera}_{channel}"] = {
                "all_three_segments_are_simple_chains": all(
                    chains[f"{prefix}_{channel}_{suffix}"]["single_pad_to_pad_chain"]
                    for prefix in ("HOST", branch, f"CAM_{camera}")
                    for suffix in ("N", "P")
                ),
                "negative_trace_length_mm": totals["negative"],
                "positive_trace_length_mm": totals["positive"],
                "absolute_trace_length_difference_mm": round(
                    abs(totals["negative"] - totals["positive"]), 4
                ),
                "negative_vias": sum(part["negative"]["via_count"] for part in parts),
                "positive_vias": sum(part["positive"]["via_count"] for part in parts),
            }
    ground_polygons = [
        zone.GetFilledPolysList(pcbnew.In1_Cu)
        for zone in board.Zones()
        if zone.GetNetname().lstrip("/") == "GND"
        and zone.IsOnLayer(pcbnew.In1_Cu)
        and zone.IsFilled()
    ]
    sample_count, missing_count, missing_samples = 0, 0, []
    ground_regions = set()
    for track in tracks:
        if track.GetNetname().lstrip("/") not in chains or type(track) is not pcbnew.PCB_TRACK:
            continue
        start, end = track.GetStart(), track.GetEnd()
        dx, dy = end.x - start.x, end.y - start.y
        length = math.hypot(dx, dy)
        if not length:
            continue
        steps = max(1, math.ceil(pcbnew.ToMM(length) / 0.2))
        for step in range(steps + 1):
            for offset in (-track.GetWidth() / 2, 0, track.GetWidth() / 2):
                sample_count += 1
                point = pcbnew.VECTOR2I(
                    round(start.x + dx * step / steps - dy / length * offset),
                    round(start.y + dy * step / steps + dx / length * offset),
                )
                covered = False
                for zone_index, polygon in enumerate(ground_polygons):
                    if polygon.Contains(point):
                        covered = True
                        for outline_index in range(polygon.OutlineCount()):
                            if polygon.Contains(point, outline_index):
                                ground_regions.add((zone_index, outline_index))
                if not covered:
                    missing_count += 1
                    if len(missing_samples) < 20:
                        missing_samples.append(
                            {
                                "net": track.GetNetname(),
                                "x_mm": round(pcbnew.ToMM(point.x), 4),
                                "y_mm": round(pcbnew.ToMM(point.y), 4),
                            }
                        )
    return {
        "source": source.name,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "scope": "Geometry only; excludes package/cable/via-barrel lengths; no SI approval",
        "copper_layers": board.GetCopperLayerCount(),
        "serialized_stackup_present": "(stackup" in source.read_text(encoding="utf-8"),
        "track_and_via_count": len(tracks),
        "trace_segment_layers": dict(
            Counter(
                board.GetLayerName(item.GetLayer())
                for item in tracks
                if not isinstance(item, pcbnew.PCB_VIA)
            )
        ),
        "schematic_pad_net_mismatches": mismatches,
        "all_csi_nets_have_tracks": all(
            item[polarity]["trace_segments"] > 0
            for item in pairs.values()
            for polarity in ("negative", "positive")
        ),
        "all_csi_nets_are_simple_pad_to_pad_chains": all(
            item["single_pad_to_pad_chain"] for item in chains.values()
        ),
        "csi_chain_topology": chains,
        "ground_reference_screen": {
            "method": "In1 GND fill sampled every <=0.2mm at centre and both trace edges",
            "samples": sample_count,
            "samples_without_ground": missing_count,
            "first_missing_samples": missing_samples,
            "passed_sample_screen": bool(sample_count and ground_polygons) and missing_count == 0,
            "filled_ground_outline_counts": [p.OutlineCount() for p in ground_polygons],
            "ground_regions_under_samples": sorted(ground_regions),
            "single_ground_region_under_all_samples": bool(sample_count)
            and missing_count == 0
            and len(ground_regions) == 1,
            "limit": "Sampling cannot prove all clearances, return-current paths or impedance",
        },
        "zones": [
            {
                "net": zone.GetNetname(),
                "layers": [board.GetLayerName(layer) for layer in zone.GetLayerSet().Seq()],
                "filled": zone.IsFilled(),
            }
            for zone in board.Zones()
        ],
        "csi_segments": pairs,
        "active_csi_paths": paths,
        "power_tracks": {name: geometry(name) for name in ("HOST_3V3", "EXT_3V3", "CAM_3V3")},
    }


if __name__ == "__main__":
    path = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(__file__).with_name("pi5-camera-mux.kicad_pcb")
    )
    print(json.dumps(review(path), indent=2))
