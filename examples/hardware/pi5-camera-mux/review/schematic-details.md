# Enlarged schematic review

`schematic-details.pdf` contains the unchanged A1 master followed by nine A4
landscape detail pages. Bookmarks identify the host/root switch, leaf switches,
camera connectors, control/power, decoupling, pull-ups and optional IO1 links.
The source text, pin numbers, net labels and DNP crosses remain vector content;
the document contains no raster images. All 41 component references are covered.

The adjacent `schematic-details.json` records the source SHA-256, crop rectangles,
enlargement factors, reference coverage and checks for partially clipped source
words. The retained first page also passed a pixel comparison against the source
master. All nine detail pages were visually inspected. This changes presentation
only; the project's electrical and fabrication review holds still apply.

To reproduce using PyMuPDF 1.28.2 in a separate Python environment, run from the
repository root:

```powershell
.local/pdf-review-env/Scripts/python.exe examples/hardware/pi5-camera-mux/schematic_details.py `
  --source examples/hardware/pi5-camera-mux/review/schematic.pdf `
  --output examples/hardware/pi5-camera-mux/review/schematic-details.pdf `
  --preview-dir .local/engineering-review/schematic-details
```

The script defaults to the final `review/schematic.pdf` if `--source` is omitted.
Install the optional package in that separate environment with
`python -m pip install PyMuPDF==1.28.2`; the running server does not require it.
The document uses PyMuPDF's documented
[vector-preserving page placement](https://pymupdf.readthedocs.io/en/latest/page.html#Page.show_pdf_page).
