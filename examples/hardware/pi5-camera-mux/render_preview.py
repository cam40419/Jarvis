"""Rasterize the real KiCad board SVG for authenticated browser previews.

Requires PyMuPDF (the same isolated development helper as schematic_details.py).
The SVG remains the authoritative vector preview; this does not edit PCB data.
"""

from pathlib import Path

import pymupdf


def main() -> None:
    review = Path(__file__).resolve().parent / "review"
    with pymupdf.open(review / "board.svg") as document:
        vector_pdf = document.convert_to_pdf()
    with pymupdf.open(stream=vector_pdf, filetype="pdf") as vector, pymupdf.open() as preview:
        bounds = vector[0].rect
        page = preview.new_page(width=bounds.width, height=bounds.height)
        page.draw_rect(bounds, color=None, fill=(0.035, 0.055, 0.08))
        page.show_pdf_page(bounds, vector, 0)
        scale = 1800 / bounds.width
        page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(review / "board.png")
    print(review / "board.png")


if __name__ == "__main__":
    main()
