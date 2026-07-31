"""
Packing history: record boxes + pieces used on each shipment, and suggest
box sizes for future mixed (or single-product) shipments.

Primary store: backend/data/packing_history.json
Mirror (best-effort): Google Sheet tab "Packing History"
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_BASE = Path(__file__).resolve().parent
DATA_DIR = _BASE / "data"
HISTORY_PATH = DATA_DIR / "packing_history.json"
MAX_RECORDS = int(os.environ.get("PACKING_HISTORY_MAX", "2000"))
SHEET_TAB = os.environ.get("PACKING_HISTORY_TAB", "Packing History")

# Must stay aligned with frontend/src/Ship.jsx SHIP_BOX_PRESETS
SHIP_BOX_PRESETS: List[Dict[str, Any]] = [
    {"id": "14x5x7", "label": "14×5×7 (5 lbs)", "L": 14, "W": 5, "H": 7, "weight": 5},
    {"id": "10x10x10", "label": "10×10×10 (10 lbs)", "L": 10, "W": 10, "H": 10, "weight": 10},
    {"id": "13x13x13", "label": "13×13×13 (13 lbs)", "L": 13, "W": 13, "H": 13, "weight": 13},
    {"id": "15x15x15", "label": "15×15×15 (15 lbs)", "L": 15, "W": 15, "H": 15, "weight": 15},
    {"id": "20x20x20", "label": "20×20×20 (20 lbs)", "L": 20, "W": 20, "H": 20, "weight": 20},
]

SHEET_HEADERS = [
    "Timestamp",
    "Id",
    "Company",
    "Order #s",
    "Mix signature",
    "Boxes JSON",
    "Pieces JSON",
    "Box contents JSON",
    "Tracking",
]


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def normalize_product_name(name: str, design: str = "") -> str:
    """Strip Front/Full/Back suffixes for stable matching."""
    raw = re.sub(r"[\s\u00a0]+", " ", (name or "").strip())
    if not raw:
        return ""
    if re.search(r"(?i)\bback\b", raw):
        return ""  # embroidery backs are not packed as pieces

    result = (
        raw.replace(" Full", "")
        .replace(" Front", "")
        .replace(" Back", "")
        .strip()
    )
    for suffix in ("Front", "Full", "Back"):
        if len(result) > len(suffix) and result.lower().endswith(suffix.lower()):
            result = result[: -len(suffix)].strip()
            break

    parts = re.split(r"[\s\-_/]+", raw)
    while len(parts) > 1 and parts[-1].lower() in ("front", "full", "back"):
        parts.pop()
    split_result = " ".join(parts).strip()

    for candidate in (result, split_result):
        c = (candidate or "").strip()
        if not c:
            continue
        if c.lower() == "drive" and (
            re.search(r"(?i)driver", raw) or re.search(r"(?i)driver", design or "")
        ):
            return "Driver"
        return c
    return raw


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return max(0, int(float(v)))
    except (TypeError, ValueError):
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def box_key(L: Any, W: Any, H: Any, weight: Any = None) -> str:
    """Stable key for a box size (sorted dims so orientation doesn't matter for matching)."""
    dims = sorted(
        [
            round(_safe_float(L), 2),
            round(_safe_float(W), 2),
            round(_safe_float(H), 2),
        ]
    )
    w = round(_safe_float(weight), 2) if weight is not None else None
    if w and w > 0:
        return f"{dims[0]}x{dims[1]}x{dims[2]}@{w}"
    return f"{dims[0]}x{dims[1]}x{dims[2]}"


def preset_id_for_dims(L: Any, W: Any, H: Any, weight: Any = None) -> Optional[str]:
    dims = sorted([round(_safe_float(L), 2), round(_safe_float(W), 2), round(_safe_float(H), 2)])
    w = round(_safe_float(weight), 2) if weight is not None else None
    for p in SHIP_BOX_PRESETS:
        pd = sorted([float(p["L"]), float(p["W"]), float(p["H"])])
        if pd == dims and (w is None or abs(float(p["weight"]) - w) < 0.01):
            return p["id"]
        if pd == dims and w is None:
            return p["id"]
    # Match dims only (ignore weight)
    for p in SHIP_BOX_PRESETS:
        pd = sorted([float(p["L"]), float(p["W"]), float(p["H"])])
        if pd == dims:
            return p["id"]
    return None


def normalize_pieces(raw_pieces: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(raw_pieces, list):
        return out
    merged: Dict[str, Dict[str, Any]] = {}
    for p in raw_pieces:
        if not isinstance(p, dict):
            continue
        product = str(p.get("product") or p.get("Product") or "").strip()
        design = str(p.get("design") or p.get("Design") or "").strip()
        norm = str(p.get("product_norm") or "").strip() or normalize_product_name(
            product, design
        )
        if not norm:
            continue
        qty = _safe_int(p.get("qty") or p.get("quantity") or p.get("shipQty"), 0)
        if qty <= 0:
            continue
        order_id = str(p.get("order_id") or p.get("orderId") or "").strip()
        key = f"{norm.lower()}|{design.lower()}|{order_id}"
        if key in merged:
            merged[key]["qty"] += qty
        else:
            merged[key] = {
                "order_id": order_id,
                "product": product or norm,
                "product_norm": norm,
                "design": design,
                "qty": qty,
            }
    return list(merged.values())


def normalize_boxes_summary(raw_boxes: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(raw_boxes, list):
        return out
    for b in raw_boxes:
        if not isinstance(b, dict):
            continue
        L = _safe_float(b.get("L") or b.get("length"))
        W = _safe_float(b.get("W") or b.get("width"))
        H = _safe_float(b.get("H") or b.get("height"))
        weight = _safe_float(b.get("weight") or b.get("Weight"), 0)
        qty = _safe_int(b.get("qty") or b.get("quantity") or 1, 1)
        if L <= 0 or W <= 0 or H <= 0 or qty <= 0:
            continue
        label = str(b.get("label") or f"{int(L)}×{int(W)}×{int(H)}").strip()
        pid = preset_id_for_dims(L, W, H, weight) or preset_id_for_dims(L, W, H)
        out.append(
            {
                "label": label,
                "L": L,
                "W": W,
                "H": H,
                "weight": weight if weight > 0 else None,
                "qty": qty,
                "preset_id": pid,
                "box_key": box_key(L, W, H, weight if weight > 0 else None),
            }
        )
    return out


def normalize_box_contents(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for i, b in enumerate(raw):
        if not isinstance(b, dict):
            continue
        L = _safe_float(b.get("L") or b.get("length"))
        W = _safe_float(b.get("W") or b.get("width"))
        H = _safe_float(b.get("H") or b.get("height"))
        weight = _safe_float(b.get("weight") or b.get("Weight"), 0)
        if L <= 0 or W <= 0 or H <= 0:
            continue
        pieces = normalize_pieces(b.get("pieces") or [])
        out.append(
            {
                "box_index": _safe_int(b.get("box_index"), i),
                "label": str(b.get("label") or f"{int(L)}×{int(W)}×{int(H)}").strip(),
                "L": L,
                "W": W,
                "H": H,
                "weight": weight if weight > 0 else None,
                "preset_id": preset_id_for_dims(L, W, H, weight)
                or preset_id_for_dims(L, W, H),
                "box_key": box_key(L, W, H, weight if weight > 0 else None),
                "pieces": pieces,
            }
        )
    return out


def mix_signature(pieces: List[Dict[str, Any]]) -> str:
    """Sorted product_norm:total_qty — design-agnostic packing signature."""
    totals: Counter = Counter()
    for p in pieces:
        norm = str(p.get("product_norm") or "").strip()
        if not norm:
            continue
        totals[norm.lower()] += _safe_int(p.get("qty"), 0)
    parts = [f"{k}:{totals[k]}" for k in sorted(totals.keys()) if totals[k] > 0]
    return "|".join(parts)


def mix_totals(pieces: List[Dict[str, Any]]) -> Dict[str, int]:
    totals: Dict[str, int] = defaultdict(int)
    for p in pieces:
        norm = str(p.get("product_norm") or "").strip().lower()
        if not norm:
            continue
        totals[norm] += _safe_int(p.get("qty"), 0)
    return dict(totals)


def expand_boxes_to_packages(boxes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    packages = []
    for b in boxes:
        n = _safe_int(b.get("qty"), 1)
        for _ in range(n):
            packages.append(
                {
                    "label": b.get("label"),
                    "L": b.get("L"),
                    "W": b.get("W"),
                    "H": b.get("H"),
                    "weight": b.get("weight"),
                    "preset_id": b.get("preset_id"),
                    "box_key": b.get("box_key"),
                }
            )
    return packages


def auto_box_contents_if_single(
    boxes: List[Dict[str, Any]], pieces: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    packages = expand_boxes_to_packages(boxes)
    if len(packages) != 1 or not pieces:
        return []
    p0 = packages[0]
    return [
        {
            "box_index": 0,
            "label": p0.get("label"),
            "L": p0.get("L"),
            "W": p0.get("W"),
            "H": p0.get("H"),
            "weight": p0.get("weight"),
            "preset_id": p0.get("preset_id"),
            "box_key": p0.get("box_key"),
            "pieces": [dict(x) for x in pieces],
        }
    ]


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_history() -> List[Dict[str, Any]]:
    with _LOCK:
        if not HISTORY_PATH.exists():
            return []
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            logger.exception("Failed to read packing history")
            return []


def _save_history_unlocked(records: List[Dict[str, Any]]) -> None:
    _ensure_data_dir()
    trimmed = records[-MAX_RECORDS:]
    tmp = HISTORY_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, indent=2, ensure_ascii=False)
    tmp.replace(HISTORY_PATH)


def build_record(
    *,
    company: str = "",
    order_ids: Optional[List[Any]] = None,
    boxes: Any = None,
    pieces: Any = None,
    box_contents: Any = None,
    tracking_numbers: Optional[List[Any]] = None,
    shipped_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    norm_boxes = normalize_boxes_summary(boxes)
    norm_pieces = normalize_pieces(pieces)
    if not norm_boxes and not norm_pieces:
        return None
    contents = normalize_box_contents(box_contents)
    if not contents:
        contents = auto_box_contents_if_single(norm_boxes, norm_pieces)
    rec = {
        "id": str(uuid.uuid4()),
        "shipped_at": shipped_at or _ts(),
        "company": str(company or "").strip(),
        "order_ids": [str(x).strip() for x in (order_ids or []) if str(x).strip()],
        "boxes": norm_boxes,
        "pieces": norm_pieces,
        "box_contents": contents,
        "mix_signature": mix_signature(norm_pieces),
        "tracking_numbers": [
            str(t).strip() for t in (tracking_numbers or []) if str(t).strip()
        ],
    }
    return rec


def append_record(record: Dict[str, Any]) -> Dict[str, Any]:
    with _LOCK:
        records = []
        if HISTORY_PATH.exists():
            try:
                with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    records = data
            except Exception:
                logger.exception("Failed reading packing history before append")
        records.append(record)
        _save_history_unlocked(records)
    return record


def ensure_sheet_tab(sheets_service, spreadsheet_id: str) -> bool:
    """Create Packing History tab with headers if missing. Returns True if usable."""
    try:
        meta = (
            sheets_service.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
            .execute()
        )
        titles = {
            (s.get("properties") or {}).get("title")
            for s in (meta.get("sheets") or [])
        }
        if SHEET_TAB not in titles:
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {"addSheet": {"properties": {"title": SHEET_TAB}}}
                    ]
                },
            ).execute()
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{SHEET_TAB}'!A1",
                valueInputOption="RAW",
                body={"values": [SHEET_HEADERS]},
            ).execute()
            logger.info("Created Google Sheet tab %r", SHEET_TAB)
            return True
        # Ensure header row exists
        existing = (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"'{SHEET_TAB}'!A1:I1")
            .execute()
            .get("values")
            or []
        )
        if not existing:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{SHEET_TAB}'!A1",
                valueInputOption="RAW",
                body={"values": [SHEET_HEADERS]},
            ).execute()
        return True
    except Exception:
        logger.exception("ensure_sheet_tab(%s) failed", SHEET_TAB)
        return False


def append_record_to_sheet(sheets_service, spreadsheet_id: str, record: Dict[str, Any]) -> None:
    if not ensure_sheet_tab(sheets_service, spreadsheet_id):
        return
    row = [
        record.get("shipped_at") or "",
        record.get("id") or "",
        record.get("company") or "",
        ", ".join(record.get("order_ids") or []),
        record.get("mix_signature") or "",
        json.dumps(record.get("boxes") or [], ensure_ascii=False),
        json.dumps(record.get("pieces") or [], ensure_ascii=False),
        json.dumps(record.get("box_contents") or [], ensure_ascii=False),
        ", ".join(record.get("tracking_numbers") or []),
    ]
    sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_TAB}'!A:I",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def record_shipment_packing(
    *,
    company: str = "",
    order_ids: Optional[List[Any]] = None,
    boxes: Any = None,
    pieces: Any = None,
    box_contents: Any = None,
    tracking_numbers: Optional[List[Any]] = None,
    sheets_service=None,
    spreadsheet_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Persist packing data locally and optionally to Google Sheets. Never raises."""
    try:
        rec = build_record(
            company=company,
            order_ids=order_ids,
            boxes=boxes,
            pieces=pieces,
            box_contents=box_contents,
            tracking_numbers=tracking_numbers,
        )
        if not rec:
            return None
        append_record(rec)
        if sheets_service and spreadsheet_id:
            try:
                append_record_to_sheet(sheets_service, spreadsheet_id, rec)
            except Exception:
                logger.exception("Packing history sheet append failed (local ok)")
        return rec
    except Exception:
        logger.exception("record_shipment_packing failed")
        return None


def _boxes_to_counts(boxes: List[Dict[str, Any]]) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    counts = {p["id"]: 0 for p in SHIP_BOX_PRESETS}
    custom: List[Dict[str, Any]] = []
    for b in boxes:
        pid = b.get("preset_id") or preset_id_for_dims(b.get("L"), b.get("W"), b.get("H"), b.get("weight"))
        qty = _safe_int(b.get("qty"), 1)
        if pid and pid in counts:
            counts[pid] += qty
        else:
            for _ in range(qty):
                custom.append(
                    {
                        "L": b.get("L"),
                        "W": b.get("W"),
                        "H": b.get("H"),
                        "weight": b.get("weight") or 1,
                    }
                )
    return counts, custom


def _similarity(a: Dict[str, int], b: Dict[str, int]) -> float:
    """Jaccard-ish similarity on product keys with qty-aware score in [0,1]."""
    keys = set(a) | set(b)
    if not keys:
        return 0.0
    score = 0.0
    weight = 0.0
    for k in keys:
        va, vb = a.get(k, 0), b.get(k, 0)
        m = max(va, vb)
        if m <= 0:
            continue
        score += min(va, vb) / m
        weight += 1.0
    return score / weight if weight else 0.0


def _scale_boxes(boxes: List[Dict[str, Any]], factor: float) -> List[Dict[str, Any]]:
    if factor <= 0:
        return []
    out = []
    for b in boxes:
        qty = max(1, int(round(_safe_int(b.get("qty"), 1) * factor)))
        nb = dict(b)
        nb["qty"] = qty
        out.append(nb)
    return out


def _median_int(values: List[int]) -> int:
    if not values:
        return 0
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return int(s[mid])
    return max(1, int(round((s[mid - 1] + s[mid]) / 2.0)))


def _preset_by_id(pid: str) -> Optional[Dict[str, Any]]:
    for p in SHIP_BOX_PRESETS:
        if p["id"] == pid:
            return p
    return None


def _preset_vol(p: Dict[str, Any]) -> float:
    return float(p["L"]) * float(p["W"]) * float(p["H"])


def _match_preset_id_for_box_key(bkey: str) -> Optional[str]:
    dim = str(bkey or "").split("@")[0]
    for p in SHIP_BOX_PRESETS:
        pk = box_key(p["L"], p["W"], p["H"], p["weight"]).split("@")[0]
        if pk == dim:
            return p["id"]
    return None


def _learn_capacity(history: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """
    product_norm(lower) -> box_key -> typical pieces per physical box.

    Learns from:
    - explicit per-box contents (if present)
    - single-product shipments using one box size (qty / box count)
    - single-box shipments of one product (all qty in that box)
    """
    samples: Dict[str, Dict[str, List[int]]] = defaultdict(lambda: defaultdict(list))

    def _sample(prod: str, bkey: str, n: int) -> None:
        if not prod or not bkey or n <= 0:
            return
        samples[prod][bkey].append(int(n))

    for rec in history:
        contents = rec.get("box_contents") or []
        for box in contents:
            bkey = box.get("box_key") or box_key(
                box.get("L"), box.get("W"), box.get("H"), box.get("weight")
            )
            for p in box.get("pieces") or []:
                prod = str(p.get("product_norm") or "").strip().lower()
                _sample(prod, bkey, _safe_int(p.get("qty"), 0))

        pieces = rec.get("pieces") or []
        boxes = rec.get("boxes") or []
        totals = mix_totals(pieces)
        packages = expand_boxes_to_packages(boxes)
        if not packages or not totals:
            continue

        # One product type + one box size → pieces-per-box = qty / boxes
        if len(totals) == 1:
            prod = next(iter(totals.keys()))
            keys = {p.get("box_key") for p in packages}
            if len(keys) == 1:
                bkey = next(iter(keys))
                per = max(1, int(round(totals[prod] / float(len(packages)))))
                _sample(prod, bkey, per)

        # One physical box (any mix) with a single product type
        if len(packages) == 1 and len(totals) == 1:
            prod = next(iter(totals.keys()))
            bkey = packages[0].get("box_key") or box_key(
                packages[0].get("L"),
                packages[0].get("W"),
                packages[0].get("H"),
                packages[0].get("weight"),
            )
            _sample(prod, bkey, totals[prod])

    out: Dict[str, Dict[str, int]] = {}
    for prod, by_box in samples.items():
        out[prod] = {}
        for bkey, vals in by_box.items():
            med = _median_int(vals)
            if med > 0:
                out[prod][bkey] = med
    return out


def _volumes_from_capacity(capacity: Dict[str, Dict[str, int]]) -> Dict[str, float]:
    """Infer cubic-inch volume per product from learned pieces-per-box."""
    vols: Dict[str, float] = {}
    for prod, by_box in capacity.items():
        samples: List[float] = []
        for bkey, cap in by_box.items():
            if cap <= 0:
                continue
            pid = _match_preset_id_for_box_key(bkey)
            p = _preset_by_id(pid) if pid else None
            if not p:
                continue
            samples.append(_preset_vol(p) / float(cap))
        if samples:
            samples.sort()
            mid = len(samples) // 2
            if len(samples) % 2:
                vols[prod] = samples[mid]
            else:
                vols[prod] = (samples[mid - 1] + samples[mid]) / 2.0
    return vols


def _merge_volume_maps(*maps: Optional[Dict[str, float]]) -> Dict[str, float]:
    """Later maps win (prefer learned packing volumes over sheet defaults)."""
    out: Dict[str, float] = {}
    for m in maps:
        if not m:
            continue
        for k, v in m.items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv > 0:
                out[str(k).lower()] = fv
    return out


def _boxes_from_used(used: Counter) -> List[Dict[str, Any]]:
    out = []
    for pid, qty in used.items():
        p = _preset_by_id(pid)
        if not p or qty <= 0:
            continue
        out.append(
            {
                "label": p["label"],
                "L": p["L"],
                "W": p["W"],
                "H": p["H"],
                "weight": p["weight"],
                "qty": int(qty),
                "preset_id": pid,
                "box_key": box_key(p["L"], p["W"], p["H"], p["weight"]),
            }
        )
    return out


def _pack_by_capacity(
    totals: Dict[str, int], capacity: Dict[str, Dict[str, int]]
) -> Optional[List[Dict[str, Any]]]:
    """
    Pack mixed products using learned fill fractions.
    Each product uses 1/capacity of a box; fill boxes smallest-first.
    """
    remaining = Counter({k: int(v) for k, v in totals.items() if int(v) > 0})
    if not remaining:
        return None

    # prod -> preset_id -> capacity
    cap_by_preset: Dict[str, Dict[str, int]] = defaultdict(dict)
    for prod, by_box in capacity.items():
        for bkey, cap in by_box.items():
            pid = _match_preset_id_for_box_key(bkey)
            if pid and cap > 0:
                cap_by_preset[prod][pid] = int(cap)

    if any(p not in cap_by_preset for p in remaining):
        return None

    presets_small_first = sorted(SHIP_BOX_PRESETS, key=_preset_vol)
    used: Counter = Counter()
    guard = 0
    while sum(remaining.values()) > 0 and guard < 400:
        guard += 1
        opened = False
        for preset in presets_small_first:
            pid = preset["id"]
            # Need capacity data for at least one remaining product in this box
            if not any(pid in cap_by_preset.get(prod, {}) for prod in remaining):
                continue
            fill_left = 1.0
            box_took = False
            # Prefer denser products first (higher 1/cap)
            prods = sorted(
                [p for p in remaining if pid in cap_by_preset.get(p, {})],
                key=lambda p: 1.0 / float(cap_by_preset[p][pid]),
                reverse=True,
            )
            progressed = True
            while progressed and fill_left > 1e-9:
                progressed = False
                for prod in prods:
                    if remaining[prod] <= 0:
                        continue
                    cap = cap_by_preset[prod][pid]
                    unit = 1.0 / float(cap)
                    if unit <= fill_left + 1e-9:
                        remaining[prod] -= 1
                        if remaining[prod] <= 0:
                            del remaining[prod]
                        fill_left -= unit
                        box_took = True
                        progressed = True
            if box_took:
                used[pid] += 1
                opened = True
                break
        if not opened:
            return None

    return _boxes_from_used(used) or None


def _preferred_preset_order(
    totals: Dict[str, int], capacity: Dict[str, Dict[str, int]]
) -> List[Dict[str, Any]]:
    """
    Prefer box sizes actually used for these products historically.
    Fall back to largest→smallest so we don't spam tiny cartons.
    """
    votes: Counter = Counter()
    for prod in totals:
        for bkey in (capacity.get(prod) or {}):
            pid = _match_preset_id_for_box_key(bkey)
            if pid:
                votes[pid] += 1
    if votes:
        ranked = [pid for pid, _ in votes.most_common()]
        preferred = [_preset_by_id(pid) for pid in ranked if _preset_by_id(pid)]
        rest = [
            p
            for p in sorted(SHIP_BOX_PRESETS, key=_preset_vol, reverse=True)
            if p["id"] not in votes
        ]
        return preferred + rest
    # No history: larger boxes first (fewer cartons for mixed orders)
    return sorted(SHIP_BOX_PRESETS, key=_preset_vol, reverse=True)


def _pack_by_volume(
    totals: Dict[str, int],
    volume_map: Dict[str, float],
    *,
    capacity: Optional[Dict[str, Dict[str, int]]] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Pack by cubic volume into historically preferred / larger boxes."""
    items: List[Tuple[str, float]] = []
    for prod, qty in totals.items():
        vol = volume_map.get(prod.lower())
        if vol is None or vol <= 0:
            return None
        items.extend([(prod, float(vol))] * int(qty))
    if not items:
        return None

    items.sort(key=lambda x: x[1], reverse=True)
    preset_order = _preferred_preset_order(totals, capacity or {})
    largest_vol = max(_preset_vol(p) for p in SHIP_BOX_PRESETS)
    remaining = list(items)
    used: Counter = Counter()
    guard = 0
    while remaining and guard < 400:
        guard += 1
        first = remaining[0]
        if first[1] > largest_vol + 1e-9:
            used[sorted(SHIP_BOX_PRESETS, key=_preset_vol)[-1]["id"]] += 1
            remaining.pop(0)
            continue

        chosen = None
        for p in preset_order:
            if _preset_vol(p) + 1e-9 >= first[1]:
                chosen = p
                break
        chosen = chosen or sorted(SHIP_BOX_PRESETS, key=_preset_vol)[-1]
        space = _preset_vol(chosen)
        i = 0
        while i < len(remaining):
            if remaining[i][1] <= space + 1e-9:
                space -= remaining[i][1]
                remaining.pop(i)
            else:
                i += 1
        used[chosen["id"]] += 1

    return _boxes_from_used(used) or None


def suggest_boxes(
    pieces: Any,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    volume_map: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Suggest preset box counts for the given piece list.

    Primary engine: learned product capacity → inferred volumes → pack math.
    Exact mix replay is only a rare bonus.
    """
    norm_pieces = normalize_pieces(pieces)
    totals = mix_totals(norm_pieces)
    sig = mix_signature(norm_pieces)
    hist = history if history is not None else load_history()
    empty_counts = {p["id"]: 0 for p in SHIP_BOX_PRESETS}

    def _result(boxes, source, confidence, reason):
        counts, custom = _boxes_to_counts(boxes or [])
        return {
            "status": "ok",
            "suggestion": {
                "box_counts": counts,
                "custom_boxes": custom,
                "boxes_summary": boxes or [],
                "confidence": confidence,
                "reason": reason,
                "source": source,
                "mix_signature": sig,
            },
            "history_count": len(hist),
            "pieces": norm_pieces,
        }

    if not totals:
        return {
            "status": "ok",
            "suggestion": {
                "box_counts": empty_counts,
                "custom_boxes": [],
                "boxes_summary": [],
                "confidence": "none",
                "reason": "No packable pieces (backs excluded).",
                "source": "none",
                "mix_signature": "",
            },
            "history_count": len(hist),
            "pieces": [],
        }

    capacity = _learn_capacity(hist)
    learned_vols = _volumes_from_capacity(capacity)
    # Prefer volumes learned from real packs; sheet volumes fill gaps
    merged_vols = _merge_volume_maps(volume_map or {}, learned_vols)

    # 1) Volume math (works for never-seen mixes once products have volumes)
    if merged_vols and all(p in merged_vols for p in totals):
        packed_v = _pack_by_volume(totals, merged_vols, capacity=capacity)
        if packed_v:
            src = "capacity_volume" if learned_vols else "volume"
            conf = "medium" if learned_vols else "low"
            why = (
                "Packed from learned product volumes (how many fit per box historically)."
                if learned_vols
                else "Packed from product volume table (still learning from shipments)."
            )
            return _result(packed_v, src, conf, why)

    # 2) Capacity fill fractions when every product has learned box fit
    packed = _pack_by_capacity(totals, capacity)
    if packed:
        return _result(
            packed,
            "capacity",
            "medium",
            "Packed from learned pieces-per-box for each product.",
        )

    # 3) Rare bonus: exact same mix seen before
    exact = [
        r
        for r in hist
        if r.get("mix_signature") == sig and (r.get("boxes") or [])
    ]
    if exact:
        exact_sorted = sorted(
            exact, key=lambda r: str(r.get("shipped_at") or ""), reverse=True
        )
        recent = exact_sorted[:8]
        vote: Counter = Counter()
        box_meta: Dict[str, Dict[str, Any]] = {}
        for r in recent:
            for b in r.get("boxes") or []:
                bk = b.get("box_key") or box_key(
                    b.get("L"), b.get("W"), b.get("H"), b.get("weight")
                )
                vote[bk] += _safe_int(b.get("qty"), 1)
                box_meta[bk] = b
        n = max(1, len(recent))
        boxes = []
        for bk, total_qty in vote.items():
            avg = max(1, int(round(total_qty / n)))
            meta = box_meta[bk]
            boxes.append(
                {
                    "label": meta.get("label"),
                    "L": meta.get("L"),
                    "W": meta.get("W"),
                    "H": meta.get("H"),
                    "weight": meta.get("weight"),
                    "qty": avg,
                    "preset_id": meta.get("preset_id")
                    or preset_id_for_dims(meta.get("L"), meta.get("W"), meta.get("H")),
                    "box_key": bk,
                }
            )
        return _result(
            boxes,
            "history_exact",
            "high",
            f"Optional match: same mix on {len(exact)} past shipment(s).",
        )

    return _result(
        [],
        "none",
        "none",
        "Not enough packing history yet for these products — pick boxes manually; this ship teaches the next one.",
    )


def load_product_volume_map(fetch_sheet_fn: Callable, spreadsheet_id: str) -> Dict[str, float]:
    """product lower-name -> volume from Table sheet col N (index 13) or col B."""
    out: Dict[str, float] = {}
    try:
        table_data = fetch_sheet_fn(spreadsheet_id, "Table!A1:Z") or []
        for r in table_data[1:]:
            if len(r) < 2:
                continue
            product = (r[0] or "").strip()
            if not product:
                continue
            volume_str = r[13] if len(r) >= 14 else r[1]
            try:
                volume = float(volume_str)
            except (TypeError, ValueError):
                continue
            if volume > 0:
                out[product.lower()] = volume
                norm = normalize_product_name(product)
                if norm:
                    out.setdefault(norm.lower(), volume)
    except Exception:
        logger.exception("load_product_volume_map failed")
    return out
