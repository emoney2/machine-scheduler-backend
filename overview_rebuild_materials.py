"""
Rebuild Overview columns M (order soon) and N (60+ days) via Google Sheets API.

Ports Tools/OverviewMaterialThread.gs (JRCO_rebuildMaterialsToOrder) so the Overview
"Recalculate lists" button does not depend on an Apps Script web app deployment.

Material model (ledger hole + later-first list split):
  net   = Inventory + On Order
  toBuy = max(0, Reorder - net)          # Reorder 0 → buy back to 0
  available = max(0, Inventory) + On Order
  rawNow/rawLater = job shortfalls (cover now demand first with available)
  Assign toBuy to rawLater FIRST, remainder to Now.
  Example Leaf Green: Inv -87, On Order 74 → toBuy 14 → Now 0, Later 14.

Thread model:
  currentCones = Thread Inventory + max(sheet On Order, Thread Data Ordered cones)
  Ordered cones come from Thread Data rows with IN/OUT=IN and O/R=Ordered
  (feet / 16500). Sheet On Order alone often stays 0 after /threadInventory logs
  because Color is the 4-digit code while inventory labels include color names.
  Zero stock with inbound Ordered cones does NOT re-queue a pod.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

FUTURE_DUE_DAYS = 60
HEADS = 6
START_ROW = 3
# Madeira Polyneon: cones logged to Thread Data as feet (qty × 5500 yd × 3 ft/yd)
FEET_PER_CONE = 5500 * 3
ALLOWED_THREAD_STAGES = frozenset({"ordered", "fur", "cut", "print", "embroidery"})


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


def _as_order_text(v: Any) -> str:
    """Normalize order # from Sheets (1114.0 / 1114 / #1114 -> 1114)."""
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
        fv = float(v)
        if abs(fv - round(fv)) < 1e-9:
            return str(int(round(fv)))
        return str(v).strip()
    s = str(v).strip().replace("#", "").strip()
    if re.fullmatch(r"\d+\.0+", s):
        return s.split(".", 1)[0]
    try:
        n = float(s)
        if math.isfinite(n) and abs(n - round(n)) < 1e-9:
            return str(int(round(n)))
    except ValueError:
        pass
    return s


def _as_num(v: Any) -> float:
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _header_index(headers: List[Any], names: List[str]) -> int:
    """Match headers; prefer earlier names in the names list over later ones."""
    normalized = []
    for h in headers:
        normalized.append(re.sub(r"\s+", " ", _as_text(h).strip().lower()))
    for name in names:
        want = name.lower().strip()
        for i, key in enumerate(normalized):
            if key == want:
                return i
    return -1


def _find_due_column(headers: List[Any]) -> int:
    """Prefer exact 'Due Date', never order/created/timestamp columns."""
    normalized = [
        re.sub(r"\s+", " ", _as_text(h).strip().lower()) for h in headers
    ]
    for i, key in enumerate(normalized):
        if key == "due date":
            return i
    for i, key in enumerate(normalized):
        if key == "due":
            return i
    for i, key in enumerate(normalized):
        if not key:
            continue
        if any(x in key for x in ("order date", "created", "timestamp", "time stamp", "date added")):
            continue
        if "ship" in key and "due" not in key:
            continue
        if "due" in key:
            return i
        if "hard" in key and "soft" in key:
            return i
        if "h/s" in key:
            return i
    return -1


def _find_extra_schedule_column(headers: List[Any], exclude1: int, exclude2: int) -> int:
    for i, h in enumerate(headers):
        if i == exclude1 or i == exclude2:
            continue
        key = re.sub(r"\s+", " ", _as_text(h).strip().lower())
        if not key:
            continue
        if any(x in key for x in ("image", "preview", "stage", "company", "design", "product")):
            continue
        if "quantity" in key or key == "qty":
            continue
        if "due" in key or "h/s" in key or ("hard" in key and "soft" in key):
            return i
    return -1


def _find_qty_column(headers: List[Any]) -> int:
    for i, h in enumerate(headers):
        raw = _as_text(h).strip()
        key = re.sub(r"\s+", " ", raw.lower())
        if key in ("qty", "quantity"):
            return i
        compact = re.sub(r"[\s._:：\-]", "", raw.lower())
        if compact in ("qty", "quantity"):
            return i
    return -1


def _order_key_variants(raw: Any) -> List[str]:
    t = _as_order_text(raw).strip()
    if not t:
        return []
    no_bom = t.lstrip("\ufeff")
    no_hash = no_bom.lstrip("#").strip()
    collapsed = re.sub(r"\s+", "", no_bom)
    collapsed_no_hash = re.sub(r"\s+", "", no_hash)
    out: List[str] = []
    for x in (
        no_bom,
        no_hash,
        collapsed,
        collapsed_no_hash,
        no_bom.lower(),
        no_hash.lower(),
    ):
        if x and x not in out:
            out.append(x)
    return out


def _put_order_map(m: Dict[str, Any], order_raw: Any, value: Any) -> None:
    for k in _order_key_variants(order_raw):
        m[k] = value


def _lookup_order(m: Optional[Dict[str, Any]], order_raw: Any) -> Any:
    if not m:
        return None
    for k in _order_key_variants(order_raw):
        if k in m:
            return m[k]
    return None


def _normalize_material_name(v: Any) -> str:
    return re.sub(r"\s+", " ", _as_text(v).strip().lower())


def _is_terminal_stage(st: str) -> bool:
    return bool(re.search(r"complete|shipped|cancel|delivered|closed", (st or "").lower()))


def _is_open_material_stage(st: str) -> bool:
    s = (st or "").lower().strip()
    return s in {"ordered", "fur", "cut", "print", "embroidery", "sewing", "sew"}


def _parse_date(v: Any) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date) and not isinstance(v, datetime):
        return datetime(v.year, v.month, v.day)
    if isinstance(v, (int, float)) and math.isfinite(v):
        whole = int(math.floor(v))
        # Sheets serial ~1995–2095 only (same guard as Apps Script)
        if whole < 35000 or whole > 65000:
            return None
        # Excel/Sheets epoch 1899-12-30
        epoch = datetime(1899, 12, 30)
        return epoch + timedelta(days=float(v))
    s = _as_text(v).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{5}(\.\d+)?", s):
        try:
            return _parse_date(float(s))
        except ValueError:
            pass
    for fmt in (
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%b-%y",
        "%d-%b-%Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _days_until_job_need(due_val: Any, ship_val: Any, hs_val: Any = None) -> Optional[int]:
    times: List[datetime] = []
    for v in (due_val, ship_val, hs_val):
        d = _parse_date(v)
        if d is not None:
            times.append(d.replace(hour=0, minute=0, second=0, microsecond=0))
    if not times:
        return None
    latest = max(times)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(round((latest - today).total_seconds() / 86400.0))


def _days_until_material_need(due_val: Any, ship_val: Any, hs_val: Any = None) -> Optional[int]:
    """
    Material buy timing follows Due Date (how you plan leather), not Ship.
    Fall back to ship / H/S only when Due is blank/unparseable.
    Overdue Due dates count as "now" (days <= 60).
    """
    due = _parse_date(due_val)
    if due is not None:
        due = due.replace(hour=0, minute=0, second=0, microsecond=0)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return int(round((due - today).total_seconds() / 86400.0))
    return _days_until_job_need(None, ship_val, hs_val)


def _collect_material_job_demand(
    mat_name: str,
    ml: List[List[Any]],
    idx_order: int,
    idx_mat: int,
    idx_inout: int,
    idx_qty: int,
    stg_by_order: Dict[str, Any],
    due_by_order: Dict[str, Any],
    ship_by_order: Dict[str, Any],
    hs_by_order: Optional[Dict[str, Any]],
    idx_panel: int = -1,
) -> Tuple[float, float]:
    """
    Sum Material Log OUT demand for this material, bucketed by Due ≤60d vs later.
    Duplicate OUT rows (same order + material + panel) count once (max qty).
    """
    now_keys: Dict[str, float] = {}
    future_keys: Dict[str, float] = {}

    if idx_order < 0 or idx_mat < 0 or idx_inout < 0 or not ml or len(ml) < 2:
        return 0.0, 0.0

    target = _normalize_material_name(mat_name)
    for row in ml[1:]:
        if idx_mat >= len(row):
            continue
        if _normalize_material_name(row[idx_mat]) != target:
            continue
        io = _as_text(row[idx_inout] if idx_inout < len(row) else "").strip().lower()
        if io != "out":
            continue
        ord_ = _as_order_text(row[idx_order] if idx_order < len(row) else "").strip()
        if not ord_:
            continue
        st = _as_text(_lookup_order(stg_by_order, ord_) or "").lower()
        if _is_terminal_stage(st):
            continue
        if not _is_open_material_stage(st):
            continue
        qty = 0.0
        if idx_qty >= 0 and idx_qty < len(row) and row[idx_qty] not in (None, ""):
            qty = _as_num(row[idx_qty])
        if qty <= 0:
            continue
        if idx_panel >= 0 and idx_panel < len(row):
            panel = _as_text(row[idx_panel]).strip().upper() or "FRONT"
        else:
            panel = "FRONT"
        key = f"{ord_.strip().lower()}|{target}|{panel}"

        due_cell = _lookup_order(due_by_order, ord_)
        ship_cell = _lookup_order(ship_by_order, ord_)
        hs_cell = _lookup_order(hs_by_order, ord_) if hs_by_order else None
        d = _days_until_material_need(due_cell, ship_cell, hs_cell)
        if d is None or d > FUTURE_DUE_DAYS:
            future_keys[key] = max(future_keys.get(key, 0.0), qty)
        else:
            now_keys[key] = max(now_keys.get(key, 0.0), qty)

    return float(sum(now_keys.values())), float(sum(future_keys.values()))


def _allocate_stock_cover_near_first(
    available: float, sum_now: float, sum_future: float
) -> Tuple[int, int, Dict[str, Any]]:
    """Job shortfalls: cover near demand with available first."""
    avail = max(0.0, float(available or 0))
    need_now = max(0, int(math.ceil(sum_now - avail - 1e-9)))
    remain = max(0.0, avail - sum_now)
    need_later = max(0, int(math.ceil(sum_future - remain - 1e-9)))
    meta = {
        "sumNow": float(sum_now),
        "sumFuture": float(sum_future),
        "available": avail,
        "shortNow": need_now,
        "shortFuture": need_later,
    }
    return need_now, need_later, meta


def _material_buy_later_first(
    on_hand: float, on_ord: float, reorder: float, sum_now: float, sum_future: float
) -> Tuple[int, int, Dict[str, Any]]:
    """
    toBuy from ledger hole; assign to later job shortfall first, remainder to now.
    """
    inv = float(on_hand or 0)
    oo = float(on_ord or 0)
    net = inv + oo
    target = max(0.0, float(reorder or 0))
    to_buy = max(0, int(math.ceil(target - net - 1e-9)))
    available = max(0.0, inv) + max(0.0, oo)
    raw_now, raw_later, _ = _allocate_stock_cover_near_first(available, sum_now, sum_future)
    meta = {
        "net": net,
        "toBuy": to_buy,
        "available": available,
        "sumNow": float(sum_now or 0),
        "sumFuture": float(sum_future or 0),
        "rawNow": raw_now,
        "rawLater": raw_later,
    }
    if to_buy <= 0:
        return 0, 0, meta
    if raw_now <= 0 and raw_later <= 0:
        meta["shortNow"] = 0
        meta["shortFuture"] = to_buy
        return 0, to_buy, meta
    need_later = min(to_buy, raw_later)
    need_now = max(0, to_buy - need_later)
    meta["shortNow"] = need_now
    meta["shortFuture"] = need_later
    return need_now, need_later, meta


def _bucket_for_thread(
    usage_map: Dict[str, bool],
    stg_by_order: Dict[str, Any],
    due_by_order: Dict[str, Any],
    ship_by_order: Dict[str, Any],
    hs_by_order: Optional[Dict[str, Any]],
) -> str:
    min_days: Optional[int] = None
    for ord_ in usage_map:
        st = _as_text(_lookup_order(stg_by_order, ord_) or "").lower()
        if _is_terminal_stage(st):
            continue
        d = _days_until_job_need(
            _lookup_order(due_by_order, ord_),
            _lookup_order(ship_by_order, ord_),
            _lookup_order(hs_by_order, ord_) if hs_by_order else None,
        )
        if d is None:
            continue
        min_days = d if min_days is None else min(min_days, d)
    if min_days is None:
        return "future"
    return "future" if min_days > FUTURE_DUE_DAYS else "now"


def _first_four_digits(s: Any) -> Optional[str]:
    m = re.search(r"\b(\d{4})\b", _as_text(s))
    return m.group(1) if m else None


def _thread_inv_columns(headers: List[Any]) -> Tuple[int, int, int]:
    """
    Thread Inventory: Thread Colors / Inventory / On Order (header-flexible).
    Falls back to A/B/C when headers are missing.
    """
    col_thread = _header_index(headers, ["thread colors", "thread color", "color", "colors"])
    col_inv = _header_index(headers, ["inventory..", "inventory"])
    col_oo = _header_index(headers, ["on order..", "on order"])
    if col_thread < 0:
        col_thread = 0
    if col_inv < 0:
        col_inv = 1
    if col_oo < 0:
        col_oo = col_inv + 1 if col_inv >= 0 else 2
    return col_thread, col_inv, col_oo


def _thread_data_columns(headers: List[Any]) -> Dict[str, int]:
    """Thread Data columns by header; fall back to documented A–H layout."""
    return {
        "order": _header_index(headers, ["order number", "order #", "order"]),
        "color": _header_index(headers, ["color", "thread color", "thread colors"]),
        "length": _header_index(headers, ["length (ft)", "length", "length ft", "feet"]),
        "inout": _header_index(headers, ["in/out", "in out", "inout"]),
        "or_": _header_index(headers, ["o/r", "o / r", "ordered/received"]),
    }


def _thread_on_order_cones_from_td(
    td: List[List[Any]], col_color: int, col_len: int, col_inout: int, col_or: int
) -> Dict[str, float]:
    """
    Cones on order from Thread Data rows logged by /threadInventory:
    IN/OUT=IN and O/R=Ordered. Length is feet → cones via FEET_PER_CONE.

    Thread Inventory!On Order sheet formulas often miss these rows (Color is
    just the 4-digit code while inventory labels include the color name), so
    rebuild must not rely solely on column C.
    """
    by_code: Dict[str, float] = {}
    if col_color < 0 or col_inout < 0 or col_or < 0:
        return by_code
    for row in td[1:]:
        inout = _as_text(_cell(row, col_inout)).strip().lower()
        o_r = _as_text(_cell(row, col_or)).strip().lower()
        if inout != "in" or o_r != "ordered":
            continue
        code = _first_four_digits(_cell(row, col_color))
        if not code:
            continue
        feet = _as_num(_cell(row, col_len)) if col_len >= 0 else 0.0
        cones = (feet / FEET_PER_CONE) if feet else 0.0
        if cones <= 0:
            continue
        by_code[code] = by_code.get(code, 0.0) + cones
    return by_code


def _sort_items(items: List[Dict[str, Any]]) -> None:
    items.sort(
        key=lambda a: (
            _as_text(a.get("name")),
            1 if a.get("type") == "Thread" else 0,
        )
    )


def _format_line(item: Dict[str, Any]) -> List[str]:
    if item.get("type") == "Material":
        return [f"{item['name']} {item['qty']} {item['unit']} - {item['vendor']}"]
    if item.get("type") == "Thread":
        clean = re.sub(r"[\r\n]+", " ", _as_text(item.get("label"))).strip()
        return [
            f"THREAD|{item['name']}|{item['qty']}|{item['pct']}|Madeira|{clean}"
        ]
    return [_as_text(item)]


def _cell(row: List[Any], idx: int) -> Any:
    if idx < 0 or idx >= len(row):
        return ""
    return row[idx]


def _fetch_values(svc, spreadsheet_id: str, range_a1: str) -> List[List[Any]]:
    resp = (
        svc.values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueRenderOption="UNFORMATTED_VALUE",
            dateTimeRenderOption="SERIAL_NUMBER",
        )
        .execute()
    )
    return resp.get("values") or []


def rebuild_materials_to_order(sheets_service, spreadsheet_id: str) -> Dict[str, Any]:
    """
    Rebuild Overview!M3:M and Overview!N3:N. Returns stats dict.
    """
    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_ID is not configured")

    svc = sheets_service.spreadsheets()

    po = _fetch_values(svc, spreadsheet_id, "Production Orders")
    mi = _fetch_values(svc, spreadsheet_id, "Material Inventory")
    ti = _fetch_values(svc, spreadsheet_id, "Thread Inventory")
    td = _fetch_values(svc, spreadsheet_id, "Thread Data")
    ml = _fetch_values(svc, spreadsheet_id, "Material Log")

    if not po or not mi or not ti or not td:
        raise RuntimeError("Missing required sheet data (Production Orders / inventories / Thread Data).")

    po_hdr = po[0] if po else []
    idx_po_order = _header_index(po_hdr, ["order #", "order number", "order"])
    # Prefer exact Due Date (not a generic "due" / days-until column with a 0)
    idx_po_due = _header_index(po_hdr, ["due date"])
    if idx_po_due < 0:
        idx_po_due = _header_index(
            po_hdr,
            [
                "due",
                "h/s due",
                "h/s due date",
                "hard/soft due",
                "hard date/soft date",
                "hard date / soft date",
            ],
        )
    if idx_po_due < 0:
        idx_po_due = _find_due_column(po_hdr)
    idx_po_ship = _header_index(po_hdr, ["ship date", "ship"])
    idx_po_stage = _header_index(po_hdr, ["stage"])
    if idx_po_order < 0:
        idx_po_order = 0
    if idx_po_stage < 0:
        idx_po_stage = 8

    idx_po_hs = _header_index(
        po_hdr,
        [
            "hard date/soft date",
            "hard date / soft date",
            "hard/soft due",
            "h/s due date",
            "production due",
            "target ship",
        ],
    )
    if idx_po_hs in (idx_po_due, idx_po_ship):
        idx_po_hs = -1
    if idx_po_hs < 0:
        idx_po_hs = _find_extra_schedule_column(po_hdr, idx_po_due, idx_po_ship)

    stg_by_order: Dict[str, Any] = {}
    due_by_order: Dict[str, Any] = {}
    ship_by_order: Dict[str, Any] = {}
    hs_by_order: Optional[Dict[str, Any]] = {} if idx_po_hs >= 0 else None

    for row in po[1:]:
        order_raw = _as_order_text(_cell(row, idx_po_order)).strip()
        if not order_raw:
            continue
        _put_order_map(stg_by_order, order_raw, _as_text(_cell(row, idx_po_stage)))
        _put_order_map(due_by_order, order_raw, _cell(row, idx_po_due) if idx_po_due >= 0 else "")
        _put_order_map(ship_by_order, order_raw, _cell(row, idx_po_ship) if idx_po_ship >= 0 else "")
        if hs_by_order is not None:
            _put_order_map(hs_by_order, order_raw, _cell(row, idx_po_hs))

    ml_hdr = ml[0] if ml else []
    idx_ml_order = _header_index(ml_hdr, ["order #", "order number", "order"])
    idx_ml_mat = _header_index(ml_hdr, ["material", "materials"])
    idx_ml_inout = _header_index(ml_hdr, ["in/out", "in out"])
    # Prefer QTY (usage) over Quantity (piece count on some logs)
    idx_ml_qty = _header_index(ml_hdr, ["qty"])
    if idx_ml_qty < 0:
        idx_ml_qty = _header_index(ml_hdr, ["quantity"])
    idx_ml_panel = _header_index(ml_hdr, ["panel"])
    if idx_ml_qty < 0:
        idx_ml_qty = _find_qty_column(ml_hdr)

    logger.info(
        "Overview materials columns: PO due=%s (%s) ship=%s | ML order=%s mat=%s inout=%s qty=%s panel=%s",
        idx_po_due,
        _as_text(_cell(po_hdr, idx_po_due)) if idx_po_due >= 0 else "NONE",
        idx_po_ship,
        idx_ml_order,
        idx_ml_mat,
        idx_ml_inout,
        idx_ml_qty,
        idx_ml_panel,
    )

    items_now: List[Dict[str, Any]] = []
    items_future: List[Dict[str, Any]] = []
    dual_bucket_debug: List[Dict[str, Any]] = []

    for row in mi[1:]:
        name = _as_text(_cell(row, 0)).strip()
        if not name:
            continue
        on_hand = _as_num(_cell(row, 1))
        on_ord = _as_num(_cell(row, 2))
        unit = _as_text(_cell(row, 3))
        reorder = _as_num(_cell(row, 5))
        vendor = _as_text(_cell(row, 8)).strip() or "Misc."

        sum_now, sum_future = _collect_material_job_demand(
            name,
            ml,
            idx_ml_order,
            idx_ml_mat,
            idx_ml_inout,
            idx_ml_qty,
            stg_by_order,
            due_by_order,
            ship_by_order,
            hs_by_order,
            idx_ml_panel,
        )
        now_mat, future_mat, meta = _material_buy_later_first(
            on_hand, on_ord, reorder, sum_now, sum_future
        )

        if now_mat <= 0 and future_mat <= 0:
            continue

        if future_mat > 0 and now_mat == 0:
            logger.info(
                "later-only %s: needLater=%s toBuy=%s avail=%.1f ML now=%.1f later=%.1f",
                name,
                future_mat,
                meta.get("toBuy") or 0,
                meta.get("available") or 0,
                meta.get("sumNow") or 0,
                meta.get("sumFuture") or 0,
            )
        if now_mat > 0 and future_mat > 0:
            dual_bucket_debug.append(
                {
                    "name": name,
                    "deficit": meta.get("toBuy") or 0,
                    "now": now_mat,
                    "future": future_mat,
                    **meta,
                }
            )
        if now_mat > 0:
            items_now.append(
                {"vendor": vendor, "name": name, "qty": now_mat, "unit": unit, "type": "Material"}
            )
        if future_mat > 0:
            items_future.append(
                {
                    "vendor": vendor,
                    "name": name,
                    "qty": future_mat,
                    "unit": unit,
                    "type": "Material",
                }
            )

    ti_hdr = ti[0] if ti else []
    col_ti_thread, col_ti_inv, col_ti_oo = _thread_inv_columns(ti_hdr)

    inv_by_code: Dict[str, float] = {}
    sheet_ord_by_code: Dict[str, float] = {}
    for row in ti[1:]:
        code = _first_four_digits(_cell(row, col_ti_thread))
        if not code:
            continue
        inv_by_code[code] = inv_by_code.get(code, 0) + _as_num(_cell(row, col_ti_inv))
        sheet_ord_by_code[code] = sheet_ord_by_code.get(code, 0) + _as_num(
            _cell(row, col_ti_oo)
        )

    td_hdr = td[0] if td else []
    td_cols = _thread_data_columns(td_hdr)
    # Documented layout fallback: B=Order#, C=Color, E=Length, G=IN/OUT, H=O/R
    idx_td_order = td_cols["order"] if td_cols["order"] >= 0 else 1
    idx_td_color = td_cols["color"] if td_cols["color"] >= 0 else 2
    idx_td_len = td_cols["length"] if td_cols["length"] >= 0 else 4
    idx_td_inout = td_cols["inout"] if td_cols["inout"] >= 0 else 6
    idx_td_or = td_cols["or_"] if td_cols["or_"] >= 0 else 7

    td_ord_by_code = _thread_on_order_cones_from_td(
        td, idx_td_color, idx_td_len, idx_td_inout, idx_td_or
    )
    # Prefer the larger of sheet On Order vs Thread Data Ordered (app logs).
    # Sheet formulas often return 0 when Color labels don't exact-match.
    ord_by_code: Dict[str, float] = {}
    for code in set(sheet_ord_by_code) | set(td_ord_by_code):
        ord_by_code[code] = max(
            sheet_ord_by_code.get(code, 0.0), td_ord_by_code.get(code, 0.0)
        )

    logger.info(
        "Thread cols: TI thread=%s inv=%s oo=%s | TD order=%s color=%s len=%s inout=%s or=%s | "
        "tdOrderedCodes=%s",
        col_ti_thread,
        col_ti_inv,
        col_ti_oo,
        idx_td_order,
        idx_td_color,
        idx_td_len,
        idx_td_inout,
        idx_td_or,
        len(td_ord_by_code),
    )

    active_codes: Dict[str, bool] = {}
    usage_by_code: Dict[str, Dict[str, bool]] = {}
    for row in td[1:]:
        td_order = _as_text(_cell(row, idx_td_order)).strip()
        td_color = _as_text(_cell(row, idx_td_color))
        inout = _as_text(_cell(row, idx_td_inout)).lower()
        if not td_order or not td_color or inout != "out":
            continue
        st = _as_text(_lookup_order(stg_by_order, td_order) or "").lower()
        if st not in ALLOWED_THREAD_STAGES:
            continue
        for m in re.finditer(r"(\d{4})", td_color):
            code_found = m.group(1)
            active_codes[code_found] = True
            usage_by_code.setdefault(code_found, {})[td_order] = True

    for code_key in sorted(active_codes.keys(), key=lambda x: (len(x), x)):
        on_hand = inv_by_code.get(code_key, 0)
        on_ord = ord_by_code.get(code_key, 0)
        current_cones = on_hand + on_ord
        remaining_pct = (current_cones / HEADS) * 100
        usage_map = usage_by_code.get(code_key) or {}
        usage_count = len(usage_map)
        pods_to_order = 0
        if current_cones < 0:
            pods_to_order = int(math.ceil((-current_cones) / HEADS))
        elif current_cones == 0 and on_ord <= 0:
            # Truly empty (nothing on hand, nothing inbound) → one pod
            pods_to_order = 1
        elif remaining_pct > 0:
            if usage_count > 30 and remaining_pct < 25:
                pods_to_order = 1
            elif usage_count > 10 and remaining_pct < 10:
                pods_to_order = 1
        if pods_to_order <= 0:
            continue
        pct_rounded = int(round(remaining_pct))
        total_cones = pods_to_order * HEADS
        bucket = _bucket_for_thread(
            usage_map, stg_by_order, due_by_order, ship_by_order, hs_by_order
        )
        future_cones = total_cones if bucket == "future" else 0
        now_cones = max(0, total_cones - future_cones)
        if now_cones > 0:
            items_now.append(
                {
                    "vendor": "Madeira",
                    "name": code_key,
                    "qty": now_cones,
                    "pct": pct_rounded,
                    "label": f"{code_key} (Polyneon) – {now_cones} Cones",
                    "type": "Thread",
                }
            )
        if future_cones > 0:
            items_future.append(
                {
                    "vendor": "Madeira",
                    "name": code_key,
                    "qty": future_cones,
                    "pct": pct_rounded,
                    "label": f"{code_key} (Polyneon) – {future_cones} Cones",
                    "type": "Thread",
                }
            )

    _sort_items(items_now)
    _sort_items(items_future)

    rows_now = [_format_line(i) for i in items_now if i.get("qty") and i["qty"] > 0]
    rows_fut = [_format_line(i) for i in items_future if i.get("qty") and i["qty"] > 0]

    # Clear M and N from row 3 down (cap clear size for API), then write new values.
    clear_end = max(START_ROW + max(len(rows_now), len(rows_fut), 500) + 50, 2000)
    svc.values().batchClear(
        spreadsheetId=spreadsheet_id,
        body={"ranges": [f"Overview!M{START_ROW}:M{clear_end}", f"Overview!N{START_ROW}:N{clear_end}"]},
    ).execute()

    data = []
    if rows_now:
        data.append({"range": f"Overview!M{START_ROW}", "values": rows_now})
    if rows_fut:
        data.append({"range": f"Overview!N{START_ROW}", "values": rows_fut})
    if data:
        svc.values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()

    stats = {
        "mRows": len(rows_now),
        "nRows": len(rows_fut),
        "idxPoDue": idx_po_due,
        "idxPoShip": idx_po_ship,
        "idxPoHs": idx_po_hs,
        "idxMlOrder": idx_ml_order,
        "idxMlMat": idx_ml_mat,
        "idxMlInOut": idx_ml_inout,
        "idxMlQty": idx_ml_qty,
        "dualBucketSamples": dual_bucket_debug[:15],
    }
    logger.info(
        "JRCO to-order (job coverage, stock covers near first): "
        "M rows=%s N rows=%s | dual-bucket materials=%s",
        stats["mRows"],
        stats["nRows"],
        len(dual_bucket_debug),
    )
    for sample in dual_bucket_debug[:8]:
        logger.info(
            "  dual %s: deficit=%s now=%s later=%s | ML demand now=%.1f later=%.1f avail=%.1f shortNow=%s shortLater=%s",
            sample.get("name"),
            sample.get("deficit"),
            sample.get("now"),
            sample.get("future"),
            sample.get("sumNow") or 0,
            sample.get("sumFuture") or 0,
            sample.get("available") or 0,
            sample.get("shortNow"),
            sample.get("shortFuture"),
        )
    return stats
