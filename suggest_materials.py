"""
Suggest Material Inventory names + percentages from artwork colors.

Phase 1: sample the image, find dominant non-background colors, nearest-match
to Material Inventory Color hex values. Optional / best-effort — never required.
"""

from __future__ import annotations

import colorsys
import io
import logging
import math
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_hex_color(raw: Any) -> Optional[Tuple[int, int, int]]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Accept rgb(r,g,b)
    m = re.match(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", s, re.I)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _HEX_RE.match(s)
    if not m:
        return None
    h = m.group(1)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rgb_to_lab_approx(r: int, g: int, b: int) -> Tuple[float, float, float]:
    """Cheap RGB→Lab-ish via XYZ for better perceptual matching than raw RGB."""
    def _f(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r_, g_, b_ = _f(r), _f(g), _f(b)
    x = r_ * 0.4124 + g_ * 0.3576 + b_ * 0.1805
    y = r_ * 0.2126 + g_ * 0.7152 + b_ * 0.0722
    z = r_ * 0.0193 + g_ * 0.1192 + b_ * 0.9505
    # D65 white
    x, y, z = x / 0.95047, y / 1.00000, z / 1.08883

    def _lab_f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t) + 16 / 116

    fx, fy, fz = _lab_f(x), _lab_f(y), _lab_f(z)
    L = 116 * fy - 16
    a = 500 * (fx - fy)
    bb = 200 * (fy - fz)
    return (L, a, bb)


def color_distance(c1: Tuple[int, int, int], c2: Tuple[int, int, int]) -> float:
    l1 = _rgb_to_lab_approx(*c1)
    l2 = _rgb_to_lab_approx(*c2)
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(l1, l2)))


def _is_background_pixel(r: int, g: int, b: int, a: int) -> bool:
    if a < 40:
        return True
    # Near-white / paper
    if r >= 245 and g >= 245 and b >= 245:
        return True
    # Very light gray
    if r >= 235 and g >= 235 and b >= 235 and abs(r - g) < 8 and abs(g - b) < 8:
        return True
    return False


def _quantize(r: int, g: int, b: int, step: int = 24) -> Tuple[int, int, int]:
    def q(v: int) -> int:
        return min(255, int(round(v / step) * step))

    return (q(r), q(g), q(b))


def extract_dominant_colors(
    image_bytes: bytes, max_colors: int = 6
) -> List[Dict[str, Any]]:
    """
    Returns [{r,g,b, share, count}, ...] sorted by share desc.
    share is fraction of non-background pixels (0–1).
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("Pillow is required for material suggestions") from e

    im = Image.open(io.BytesIO(image_bytes))
    im = im.convert("RGBA")
    # Shrink for speed
    im.thumbnail((160, 160), Image.Resampling.LANCZOS)
    pixels = list(im.getdata())

    counts: Counter = Counter()
    usable = 0
    for r, g, b, a in pixels:
        if _is_background_pixel(r, g, b, a):
            continue
        usable += 1
        counts[_quantize(r, g, b)] += 1

    if usable < 20:
        return []

    # Merge tiny bins into nearest larger bin
    ranked = counts.most_common()
    significant = [(c, n) for c, n in ranked if n / usable >= 0.015]
    if not significant:
        significant = ranked[:max_colors]

    # Re-center each bin as weighted average of original? Keep quantized center.
    out = []
    for (r, g, b), n in significant[: max_colors * 2]:
        share = n / usable
        out.append({"r": r, "g": g, "b": b, "share": share, "count": n})

    # Drop ultra-low saturation noise if we already have strong colors
    def sat(c):
        h, s, v = colorsys.rgb_to_hsv(c["r"] / 255, c["g"] / 255, c["b"] / 255)
        return s, v

    strong = [c for c in out if sat(c)[0] >= 0.08 or sat(c)[1] <= 0.18]  # keep dark/neutrals
    if len(strong) >= 1:
        out = strong

    out.sort(key=lambda x: x["share"], reverse=True)
    return out[:max_colors]


def match_colors_to_materials(
    dominant: List[Dict[str, Any]],
    inventory: List[Dict[str, Any]],
    max_materials: int = 5,
    max_distance: float = 55.0,
) -> List[Dict[str, Any]]:
    """
    inventory: [{name, color/hex, rgb?}, ...]
    Returns suggestions [{name, percent, confidence, hex, distance, share}]
    """
    catalog = []
    for row in inventory or []:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        rgb = row.get("rgb")
        if not rgb:
            rgb = parse_hex_color(row.get("color") or row.get("hex"))
        if not rgb:
            continue
        catalog.append({"name": name, "rgb": rgb, "hex": "#%02x%02x%02x" % rgb})

    if not catalog or not dominant:
        return []

    # Aggregate share by material name
    by_name: Dict[str, Dict[str, Any]] = {}
    for dom in dominant:
        rgb = (int(dom["r"]), int(dom["g"]), int(dom["b"]))
        best = None
        best_d = 1e9
        for mat in catalog:
            d = color_distance(rgb, mat["rgb"])
            if d < best_d:
                best_d = d
                best = mat
        if best is None or best_d > max_distance:
            continue
        key = best["name"]
        conf = max(0.0, min(1.0, 1.0 - (best_d / max_distance)))
        entry = by_name.get(key)
        if not entry:
            by_name[key] = {
                "name": key,
                "share": float(dom["share"]),
                "confidence": conf,
                "hex": best["hex"],
                "distance": best_d,
            }
        else:
            entry["share"] += float(dom["share"])
            entry["confidence"] = max(entry["confidence"], conf)
            entry["distance"] = min(entry["distance"], best_d)

    ranked = sorted(by_name.values(), key=lambda x: x["share"], reverse=True)[
        : max(1, max_materials)
    ]
    total = sum(x["share"] for x in ranked) or 1.0

    # Percents as integers summing to 100
    raw_pcts = [(x["share"] / total) * 100.0 for x in ranked]
    pcts = [int(round(p)) for p in raw_pcts]
    drift = 100 - sum(pcts)
    if pcts:
        # Fix rounding drift on the largest share
        pcts[0] += drift
        if pcts[0] < 1:
            pcts[0] = 1

    suggestions = []
    for x, pct in zip(ranked, pcts):
        if pct <= 0:
            continue
        suggestions.append(
            {
                "name": x["name"],
                "percent": int(pct),
                "confidence": round(float(x["confidence"]), 2),
                "hex": x["hex"],
                "distance": round(float(x["distance"]), 1),
            }
        )
    # Re-normalize if we dropped zeros
    s = sum(s["percent"] for s in suggestions)
    if suggestions and s != 100:
        suggestions[0]["percent"] += 100 - s
    return suggestions


def load_inventory_colors_from_rows(rows: List[List[Any]]) -> List[Dict[str, Any]]:
    """Parse Material Inventory sheet rows (header + data) into [{name, color, rgb}]."""
    if not rows or len(rows) < 2:
        return []
    headers = [str(h or "").strip() for h in rows[0]]
    headers_l = [h.lower() for h in headers]

    def col(*names: str) -> int:
        for n in names:
            n = n.lower()
            if n in headers_l:
                return headers_l.index(n)
        return -1

    name_i = col("materials", "material", "name")
    color_i = col("color", "colour", "hex")
    if name_i < 0:
        name_i = 0
    if color_i < 0:
        return []

    out = []
    seen = set()
    for r in rows[1:]:
        r = r or []
        if len(r) <= max(name_i, color_i):
            continue
        name = str(r[name_i] or "").strip()
        color = str(r[color_i] or "").strip() if color_i < len(r) else ""
        if not name or not color:
            continue
        key = name.lower()
        if key in seen:
            continue
        rgb = parse_hex_color(color)
        if not rgb:
            continue
        seen.add(key)
        out.append({"name": name, "color": color, "rgb": rgb})
    return out


def suggest_materials_from_image(
    image_bytes: bytes,
    inventory: List[Dict[str, Any]],
    max_materials: int = 5,
) -> Dict[str, Any]:
    dominant = extract_dominant_colors(image_bytes, max_colors=max(6, max_materials + 2))
    suggestions = match_colors_to_materials(
        dominant, inventory, max_materials=max_materials
    )
    avg_conf = (
        sum(s["confidence"] for s in suggestions) / len(suggestions)
        if suggestions
        else 0.0
    )
    return {
        "suggestions": suggestions,
        "dominantColors": [
            {
                "hex": "#%02x%02x%02x" % (c["r"], c["g"], c["b"]),
                "share": round(c["share"], 3),
            }
            for c in dominant
        ],
        "confidence": round(avg_conf, 2),
        "message": (
            "Suggestions only - edit before submit."
            if suggestions
            else "Could not match artwork colors to Material Inventory. Enter materials manually."
        ),
    }
