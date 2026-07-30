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


def _learn_capacity(history: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """
    product_norm(lower) -> box_key -> observed max pieces in one physical box.
    Also derives from homogeneous single-box-type shipments (qty / box_count).
    """
    cap: Dict[str, Dict[str, int]] = defaultdict(dict)

    def _bump(prod: str, bkey: str, n: int) -> None:
        if not prod or not bkey or n <= 0:
            return
        prev = cap[prod].get(bkey, 0)
        if n > prev:
            cap[prod][bkey] = n

    for rec in history:
        contents = rec.get("box_contents") or []
        for box in contents:
            bkey = box.get("box_key") or box_key(
                box.get("L"), box.get("W"), box.get("H"), box.get("weight")
            )
            for p in box.get("pieces") or []:
                prod = str(p.get("product_norm") or "").strip().lower()
                _bump(prod, bkey, _safe_int(p.get("qty"), 0))

        pieces = rec.get("pieces") or []
        boxes = rec.get("boxes") or []
        totals = mix_totals(pieces)
        packages = expand_boxes_to_packages(boxes)
        if len(totals) == 1 and packages:
            prod = next(iter(totals.keys()))
            # Only trust when all packages same size
            keys = {p.get("box_key") for p in packages}
            if len(keys) == 1:
                bkey = next(iter(keys))
                per = max(1, int(round(totals[prod] / len(packages))))
                _bump(prod, bkey, per)
    return {k: dict(v) for k, v in cap.items()}


def _preset_by_id(pid: str) -> Optional[Dict[str, Any]]:
    for p in SHIP_BOX_PRESETS:
        if p["id"] == pid:
            return p
    return None


def _pack_by_capacity(
    totals: Dict[str, int], capacity: Dict[str, Dict[str, int]]
) -> Optional[List[Dict[str, Any]]]:
    """Greedy: pack largest remaining product into best-known box repeatedly."""
    remaining = dict(totals)
    if not remaining:
        return None
    # Prefer larger known capacities / larger boxes
    preset_keys = {
        p["id"]: box_key(p["L"], p["W"], p["H"], p["weight"]) for p in SHIP_BOX_PRESETS
    }
    used: Counter = Counter()
    guard = 0
    while any(v > 0 for v in remaining.values()) and guard < 200:
        guard += 1
        # pick product with most remaining
        prod = max(remaining.keys(), key=lambda k: remaining[k])
        if remaining[prod] <= 0:
            remaining.pop(prod, None)
            continue
        caps = capacity.get(prod) or {}
        # Map box_key -> preset id when possible
        best_pid = None
        best_cap = 0
        for pid, bkey in preset_keys.items():
            c = caps.get(bkey, 0)
            if c > best_cap:
                best_cap = c
                best_pid = pid
        if not best_pid or best_cap <= 0:
            # try any learned key matching a preset by dims only
            for bkey, c in caps.items():
                for pid, pk in preset_keys.items():
                    # compare without weight
                    if bkey.split("@")[0] == pk.split("@")[0] and c > best_cap:
                        best_cap = c
                        best_pid = pid
        if not best_pid or best_cap <= 0:
            return None
        take = min(remaining[prod], best_cap)
        # Also try to fill same box with other products if we know they fit — skip for simplicity
        remaining[prod] -= take
        if remaining[prod] <= 0:
            remaining.pop(prod, None)
        used[best_pid] += 1
        # If other products remain and this box has spare capacity for them, leave for next iteration
    if any(v > 0 for v in remaining.values()):
        return None
    out = []
    for pid, qty in used.items():
        p = _preset_by_id(pid)
        if not p:
            continue
        out.append(
            {
                "label": p["label"],
                "L": p["L"],
                "W": p["W"],
                "H": p["H"],
                "weight": p["weight"],
                "qty": qty,
                "preset_id": pid,
                "box_key": box_key(p["L"], p["W"], p["H"], p["weight"]),
            }
        )
    return out or None


def _pack_by_volume(
    totals: Dict[str, int],
    volume_map: Dict[str, float],
) -> Optional[List[Dict[str, Any]]]:
    """Fallback using product volumes + preset box volumes (largest-first greedy fill)."""
    items: List[float] = []
    missing = []
    for prod, qty in totals.items():
        vol = volume_map.get(prod.lower())
        if vol is None or vol <= 0:
            missing.append(prod)
            continue
        items.extend([vol] * qty)
    if missing or not items:
        return None
    presets = sorted(
        SHIP_BOX_PRESETS,
        key=lambda p: p["L"] * p["W"] * p["H"],
    )
    largest_vol = presets[-1]["L"] * presets[-1]["W"] * presets[-1]["H"]
    remaining = list(items)
    used: Counter = Counter()
    guard = 0
    while remaining and guard < 200:
        guard += 1
        group = [remaining.pop(0)]
        total = group[0]
        i = 0
        while i < len(remaining):
            if total + remaining[i] <= largest_vol:
                total += remaining[i]
                group.append(remaining.pop(i))
            else:
                i += 1
        chosen = None
        for p in presets:
            if p["L"] * p["W"] * p["H"] >= total:
                chosen = p
                break
        chosen = chosen or presets[-1]
        used[chosen["id"]] += 1
    out = []
    for pid, qty in used.items():
        p = _preset_by_id(pid)
        if p:
            out.append(
                {
                    "label": p["label"],
                    "L": p["L"],
                    "W": p["W"],
                    "H": p["H"],
                    "weight": p["weight"],
                    "qty": qty,
                    "preset_id": pid,
                    "box_key": box_key(p["L"], p["W"], p["H"], p["weight"]),
                }
            )
    return out or None


def suggest_boxes(
    pieces: Any,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    volume_map: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Suggest preset box counts for the given piece list.
    Returns { status, suggestion, history_count }.
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

    # 1) Exact mix signature matches
    exact = [
        r
        for r in hist
        if r.get("mix_signature") == sig and (r.get("boxes") or [])
    ]
    if exact:
        # Prefer most recent
        exact_sorted = sorted(
            exact, key=lambda r: str(r.get("shipped_at") or ""), reverse=True
        )
        # Majority vote on box_key counts among last few
        recent = exact_sorted[:8]
        vote: Counter = Counter()
        box_meta: Dict[str, Dict[str, Any]] = {}
        for r in recent:
            for b in r.get("boxes") or []:
                bk = b.get("box_key") or box_key(b.get("L"), b.get("W"), b.get("H"), b.get("weight"))
                vote[bk] += _safe_int(b.get("qty"), 1)
                box_meta[bk] = b
        # Average qty per box_key across recent
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
            f"Matched {len(exact)} past shipment(s) with the same product mix.",
        )

    # 2) Similar mixes (same products, scalable qty)
    best = None
    best_score = 0.0
    for r in hist:
        if not (r.get("boxes") or []):
            continue
        rt = mix_totals(r.get("pieces") or [])
        if set(rt.keys()) != set(totals.keys()):
            # still allow subset similarity
            if not set(totals.keys()) & set(rt.keys()):
                continue
        score = _similarity(totals, rt)
        if score > best_score:
            best_score = score
            best = r
    if best and best_score >= 0.75:
        rt = mix_totals(best.get("pieces") or [])
        # Scale by total piece ratio
        sum_a = sum(totals.values()) or 1
        sum_b = sum(rt.values()) or 1
        factor = sum_a / sum_b
        scaled = _scale_boxes(best.get("boxes") or [], factor)
        return _result(
            scaled,
            "history_similar",
            "medium" if best_score >= 0.9 else "low",
            f"Scaled a similar past shipment (similarity {best_score:.0%}).",
        )

    # 3) Learned per-product capacity
    capacity = _learn_capacity(hist)
    packed = _pack_by_capacity(totals, capacity)
    if packed:
        return _result(
            packed,
            "capacity",
            "medium",
            "Estimated from how many of each product fit in boxes on past shipments.",
        )

    # 4) Volume fallback from Table sheet map
    if volume_map:
        packed_v = _pack_by_volume(totals, volume_map)
        if packed_v:
            return _result(
                packed_v,
                "volume",
                "low",
                "Estimated from product volumes (no strong packing history yet).",
            )

    return _result(
        [],
        "none",
        "none",
        "Not enough packing history yet — pick boxes manually; this shipment will teach the next one.",
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
