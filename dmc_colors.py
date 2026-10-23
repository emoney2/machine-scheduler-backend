"""DMC floss chart + nearest-color suggestions for needlepoint color picking."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from suggest_materials import color_distance, parse_hex_color

logger = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).resolve().parent / "data" / "dmc_floss.json"


@lru_cache(maxsize=1)
def load_dmc_chart() -> list[dict[str, Any]]:
    """Return [{code, name, hex, rgb}, ...] from the bundled chart."""
    try:
        raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("[DMC] failed to load chart from %s", _DATA_PATH)
        return []
    colors = raw.get("colors") if isinstance(raw, dict) else None
    if not isinstance(colors, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in colors:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "").strip()
        name = str(entry.get("name") or "").strip()
        hex_color = str(entry.get("hex") or "").strip()
        rgb = parse_hex_color(hex_color)
        if not code or not rgb:
            continue
        out.append(
            {
                "code": code,
                "name": name,
                "hex": f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}",
                "rgb": rgb,
            }
        )
    return out


def suggest_dmc_for_hex(hex_color: str, *, limit: int = 3) -> list[dict[str, Any]]:
    """Nearest DMC floss colors for a design hex (suggestions only)."""
    target = parse_hex_color(hex_color)
    if not target:
        return []
    chart = load_dmc_chart()
    if not chart:
        return []
    ranked = sorted(chart, key=lambda c: color_distance(target, c["rgb"]))
    out = []
    for entry in ranked[: max(1, int(limit or 3))]:
        dist = color_distance(target, entry["rgb"])
        out.append(
            {
                "code": entry["code"],
                "name": entry["name"],
                "hex": entry["hex"],
                "distance": round(float(dist), 2),
                "label": f"DMC {entry['code']} {entry['name']}".strip(),
            }
        )
    return out
