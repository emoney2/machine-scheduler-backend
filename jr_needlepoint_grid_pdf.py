"""Build vendor stitch-grid PDFs from Studio package document cells.

Layout matches Studio grid / customer-proof bands:
  - Top strip: columns [0, split)
  - Bottom strip: columns [split, width)
  - No leather tabs
  - Faint single-pass light-grey guides (not heavy per-cell strokes)
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Any

logger = logging.getLogger(__name__)

BELT_ROWS = 24
# Soft steel guide — every N stitches only (1-stitch grid becomes the focal point).
GRID_RGB = (95 / 255.0, 105 / 255.0, 120 / 255.0)
GRID_LINE = 0.15
FRAME_LINE = 0.25
GUIDE_EVERY = 5


def _hex_rgb(hex_color: str | None) -> tuple[int, int, int]:
    raw = str(hex_color or "#ffffff").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        return (255, 255, 255)
    try:
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    except ValueError:
        return (255, 255, 255)


def build_vendor_grid_pdf_from_document(document: dict[str, Any]) -> bytes | None:
    """Return PDF bytes for a Studio `document` dict, or None if unusable."""
    try:
        from reportlab.lib.colors import Color
        from reportlab.pdfgen import canvas
    except Exception:
        logger.exception("[Needlepoint] reportlab unavailable for vendor grid PDF")
        return None

    cells = document.get("cells")
    palette = document.get("palette") or []
    if not isinstance(cells, list) or not cells:
        return None
    width = int(document.get("width") or (len(cells[0]) if cells and cells[0] else 0) or 0)
    height = min(BELT_ROWS, len(cells))
    if width < 2 or height < 1:
        return None

    preview = document.get("preview") if isinstance(document.get("preview"), dict) else {}
    try:
        split = int(round(float(preview.get("splitColumn") or width / 2)))
    except (TypeError, ValueError):
        split = width // 2
    split = max(1, min(width - 1, split))

    cell = 5.0
    margin_x = 24.0
    margin_top = 24.0
    margin_bottom = 24.0
    gap = 28.0
    max_cols = max(split, width - split)
    page_w = margin_x * 2 + max_cols * cell
    page_h = margin_top + height * cell + gap + height * cell + margin_bottom

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))

    def draw_band(start_col: int, end_col: int, origin_y_top: float) -> None:
        cols = max(1, end_col - start_col)
        band_w = cols * cell
        band_h = height * cell
        origin_x = margin_x + (max_cols * cell - band_w) / 2
        band_bottom = page_h - origin_y_top - band_h

        # 1) Stitch fills only.
        for y in range(height):
            row = cells[y] if y < len(cells) else None
            if not isinstance(row, list):
                continue
            for x in range(start_col, end_col):
                if x >= len(row):
                    continue
                idx = row[x]
                entry = None
                if isinstance(idx, int) and 0 <= idx < len(palette):
                    entry = palette[idx] if isinstance(palette[idx], dict) else None
                if isinstance(idx, int) and idx >= 0:
                    r, g, b = _hex_rgb((entry or {}).get("hex"))
                else:
                    r, g, b = (255, 255, 255)
                px = origin_x + (x - start_col) * cell
                py = band_bottom + (height - 1 - y) * cell
                c.setFillColor(Color(r / 255.0, g / 255.0, b / 255.0))
                # Slight overlap kills PDF hairline gaps between same-color cells.
                c.rect(px, py, cell + 0.2, cell + 0.2, stroke=0, fill=1)

        # 2) Faint count guides every GUIDE_EVERY stitches (not every cell).
        c.setStrokeColorRGB(*GRID_RGB)
        c.setLineWidth(GRID_LINE)
        for i in range(0, cols + 1, GUIDE_EVERY):
            x = origin_x + i * cell
            c.line(x, band_bottom, x, band_bottom + band_h)
        if cols % GUIDE_EVERY != 0:
            x = origin_x + cols * cell
            c.line(x, band_bottom, x, band_bottom + band_h)
        for j in range(0, height + 1, GUIDE_EVERY):
            y = band_bottom + j * cell
            c.line(origin_x, y, origin_x + band_w, y)
        if height % GUIDE_EVERY != 0:
            y = band_bottom + height * cell
            c.line(origin_x, y, origin_x + band_w, y)

        c.setLineWidth(FRAME_LINE)
        c.rect(origin_x, band_bottom, band_w, band_h, stroke=1, fill=0)

    top_y = margin_top
    bottom_y = top_y + height * cell + gap
    draw_band(0, split, top_y)
    draw_band(split, width, bottom_y)
    c.save()
    return buf.getvalue()


def build_vendor_grid_pdf_from_package(raw: bytes | str) -> bytes | None:
    """Parse a Studio package and build the vendor grid PDF from `document`."""
    import json

    from jr_needlepoint_package import STUDIO_PACKAGE_KIND, parse_studio_package

    document = None
    try:
        text = raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else str(raw)
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get("kind") == STUDIO_PACKAGE_KIND:
            if isinstance(parsed.get("document"), dict):
                document = parsed["document"]
    except Exception:
        document = None

    if document is None:
        pkg = parse_studio_package(raw)
        if pkg and isinstance((pkg.get("rawPackage") or {}).get("document"), dict):
            document = pkg["rawPackage"]["document"]

    if not isinstance(document, dict):
        return None
    return build_vendor_grid_pdf_from_document(document)
