"""Keep the master schematic and add vector-preserving, labeled A4 detail pages.

Run with Python plus PyMuPDF 1.28.2. No electrical source or original PDF is changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent
INK = (0.08, 0.15, 0.23)
MUTED = (0.28, 0.35, 0.42)
WIDTH, HEIGHT = 842, 595


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "review/schematic.pdf")
    parser.add_argument("--output", type=Path, default=ROOT / "review/schematic-details.pdf")
    parser.add_argument("--preview-dir", type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        raise ValueError("Keep the exported master separate from the detail document")
    original = args.source.read_bytes()
    with pymupdf.open(stream=original, filetype="pdf") as source, pymupdf.open() as result:
        if len(source) != 1 or not source[0].rect.contains(pymupdf.Rect(0, 0, 2380, 1680)):
            raise ValueError("Expected the single A1 sheet from this camera-mux project")
        result.insert_pdf(source)
        page_source = source[0]
        words = page_source.get_text("words")
        report = {"source_sha256": hashlib.sha256(original).hexdigest(), "panels": []}
        contents = [[1, "Master schematic - unchanged A1 export", 1]]

        def page(title: str, subtitle: str):
            output = result.new_page(width=WIDTH, height=HEIGHT)
            output.insert_text((30, 30), title, fontsize=18, fontname="hebo", color=INK)
            output.insert_text((30, 48), subtitle, fontsize=9, color=MUTED)
            output.draw_line((30, 58), (812, 58), color=(0.7, 0.76, 0.81), width=0.6)
            output.insert_text(
                (30, 574), "Engineering prototype | Enlarged master-sheet regions; "
                "electrical content unchanged | Review holds remain", fontsize=8, color=MUTED,
            )
            output.insert_text((782, 574), str(len(result)), fontsize=9, color=INK)
            contents.append([1, title, len(result)])
            return output

        def panel(output, title, clip, destination):
            clip, destination = pymupdf.Rect(clip), pymupdf.Rect(destination)
            output.insert_text((destination.x0, destination.y0 + 11), title,
                               fontsize=11, fontname="hebo", color=INK)
            content = pymupdf.Rect(destination.x0, destination.y0 + 20,
                                   destination.x1, destination.y1)
            # show_pdf_page imports source text and paths; it does not rasterize.
            output.show_pdf_page(content, source, 0, clip=clip, keep_proportion=True)
            partial = [word[4] for word in words if clip.intersects(pymupdf.Rect(word[:4]))
                       and not clip.contains(pymupdf.Rect(word[:4]))]
            if partial:
                raise ValueError(f"Crop cuts source labels in {title}: {partial}")
            report["panels"].append({
                "page": len(result), "title": title, "source_clip_points": list(clip),
                "scale": min(content.width / clip.width, content.height / clip.height),
                "partial_source_words": partial,
            })

        output = page("Signal path / host and root switch",
                      "J1 host connector and U1 bank selector")
        panel(output, "J1 / Raspberry Pi 5 host", (85, 164, 241, 366), (30, 78, 398, 545))
        panel(output, "U1 / root switch", (375, 150, 577, 376), (434, 78, 812, 545))

        output = page("Signal path / leaf switches",
                      "Bank A/B and bank C/D; original net labels retained")
        panel(output, "U2 / cameras A and B", (715, 44, 945, 276), (30, 78, 406, 545))
        panel(output, "U3 / cameras C and D", (715, 390, 945, 622), (436, 78, 812, 545))

        for title, refs, offset in (("A and B", ("J2", "J3"), 0),
                                    ("C and D", ("J4", "J5"), 345.6)):
            output = page("Camera connectors / " + title,
                          "15-position camera connectors; original pin numbering and IO labels")
            panel(output, refs[0], (1115, 50 + offset, 1280, 204 + offset),
                  (30, 78, 406, 545))
            panel(output, refs[1], (1115, 224 + offset, 1280, 378 + offset),
                  (436, 78, 812, 545))

        output = page("Control and camera power",
                      "Selector, control header, load switch and external input")
        panel(output, "U4 / PCA9544A I2C selector", (1460, 208, 1690, 331),
              (30, 78, 406, 338))
        panel(output, "U5 / TPS22918 load switch", (1460, 651, 1690, 723),
              (436, 78, 812, 338))
        panel(output, "J6 / GPIO control", (85, 672, 238, 741), (30, 361, 406, 545))
        panel(output, "J7 / regulated 3.3 V input", (1800, 651, 1940, 723),
              (436, 361, 812, 545))

        def component_box(reference):
            matches = [word for word in words if word[4] == reference]
            if len(matches) != 1:
                raise ValueError(f"Expected exactly one source reference {reference}")
            reference_box = pymupdf.Rect(matches[0][:4])
            center = (reference_box.x0 + reference_box.x1) / 2
            return (center - 55, reference_box.y0 - 4, center + 115, reference_box.y0 + 57)

        sections = (
            ("Decoupling and supply markers", [*(f"C{i}" for i in range(1, 8)),
                                                "#FLG01", "#FLG02", "#FLG03"]),
            ("Control bias and host pull-ups", [f"R{i}" for i in range(1, 8)]),
            ("Camera I2C pull-ups", [f"R{i}" for i in range(8, 16)]),
            ("Optional camera IO1 links", [f"R{i}" for i in range(16, 20)]),
        )
        for title, references in sections:
            output = page(title, "Individual source regions; DNP crosses and values are preserved")
            for index, reference in enumerate(references):
                column, row = index % 3, index // 3
                x, y = 30 + column * 268, 75 + row * 117
                panel(output, reference, component_box(reference), (x, y, x + 246, y + 110))
            if title == "Optional camera IO1 links":
                panel(output, "Original design notes", (45, 1548, 410, 1634),
                      (30, 363, 812, 543))

        references = {word[4]: pymupdf.Rect(word[:4]) for word in words
                      if re.fullmatch(r"(?:[CURJ][0-9]+|#FLG[0-9]+)", word[4])}
        covered = sorted(name for name, rectangle in references.items() if any(
            pymupdf.Rect(entry["source_clip_points"]).contains(rectangle)
            for entry in report["panels"]
        ))
        if set(covered) != set(references):
            raise ValueError("Detail pages omitted one or more component references")
        preview_matrix = pymupdf.Matrix(0.5, 0.5)
        unchanged = (result[0].get_pixmap(matrix=preview_matrix).samples
                     == page_source.get_pixmap(matrix=preview_matrix).samples)
        if not unchanged:
            raise ValueError("The retained master page changed visually")
        report.update({"component_references_covered": covered,
                       "master_raster_comparison_equal": unchanged})

        result.set_toc(contents)
        result.set_metadata({
            "title": "Pi 5 four-camera selector - master and enlarged schematic details",
            "subject": "Vector review pages; source master and electrical content unchanged",
            "creator": "Simon schematic_details.py / PyMuPDF",
        })
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result.save(args.output, garbage=4, deflate=True)
        if args.preview_dir:
            args.preview_dir.mkdir(parents=True, exist_ok=True)
            for index, output in enumerate(result):
                if index:
                    output.get_pixmap(matrix=pymupdf.Matrix(1.7, 1.7)).save(
                        args.preview_dir / f"schematic-detail-{index + 1:02d}.png",
                    )
        report.update({"pages": len(result), "text_preserved": bool(result[1].get_text()),
                       "raster_images": sum(len(output.get_images()) for output in result)})
        args.output.with_suffix(".json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.source.read_bytes() != original:
        raise RuntimeError("Source master unexpectedly changed")
    print(f"Created {report['pages']} pages: {args.output}")


if __name__ == "__main__":
    main()
