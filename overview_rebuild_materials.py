"""
Rebuild Overview columns M (order soon) and N (60+ days) via Google Sheets API.

Ports Tools/OverviewMaterialThread.gs (JRCO_rebuildMaterialsToOrder) so the Overview
"Recalculate lists" button does not depend on an Apps Script web app deployment.
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
ALLOWED_THREAD_STAGES = frozenset({"ordered", "fur", "cut", "print", "embroidery"})


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


def _as_num(v: Any) -> float:
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _header_index(headers: List[Any], names: List[str]) -> int:
    want = {n.lower() for n in names}
    for i, h in enumerate(headers):
        key = re.sub(r"\s+", " ", _as_text(h).strip().lower())
        if key in want:
            return i
    return -1


def _find_due_column(headers: List[Any]) -> int:
    for i, h in enumerate(headers):
        key = re.sub(r"\s+", " ", _as_text(h).strip().lower())
        if not key:
            continue
        if "ship" in key and "due" not in key:
            continue
        if key == "due" or "due" in key:
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
    t = _as_text(raw).strip()
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


def _future_qty_material(
    mat_name: str,
    total: float,
    ml: List[List[Any]],
    idx_order: int,
    idx_mat: int,
    idx_inout: int,
    idx_qty: int,
    stg_by_order: Dict[str, Any],
    due_by_order: Dict[str, Any],
    ship_by_order: Dict[str, Any],
    hs_by_order: Optional[Dict[str, Any]],
) -> int:
    d_total = max(0, int(math.ceil(total - 1e-9)))
    if idx_order < 0 or idx_mat < 0 or idx_inout < 0 or not ml or len(ml) < 2:
        return 0

    target = _normalize_material_name(mat_name)
    sum_now = 0.0
    sum_future = 0.0

    for row in ml[1:]:
        if idx_mat >= len(row):
            continue
        if _normalize_material_name(row[idx_mat]) != target:
            continue
        io = _as_text(row[idx_inout] if idx_inout < len(row) else "").strip().lower()
        if io != "out":
            continue
        ord_ = _as_text(row[idx_order] if idx_order < len(row) else "").strip()
        if not ord_:
            continue
        st = _as_text(_lookup_order(stg_by_order, ord_) or "").lower()
        if _is_terminal_stage(st):
            continue
        qty = 0.0
        if idx_qty >= 0 and idx_qty < len(row) and row[idx_qty] not in (None, ""):
            qty = _as_num(row[idx_qty])
        if qty <= 0:
            continue
        due_cell = _lookup_order(due_by_order, ord_)
        ship_cell = _lookup_order(ship_by_order, ord_)
        hs_cell = _lookup_order(hs_by_order, ord_) if hs_by_order else None
        d = _days_until_job_need(due_cell, ship_cell, hs_cell)
        if d is None:
            sum_future += qty
        elif d > FUTURE_DUE_DAYS:
            sum_future += qty
        else:
            sum_now += qty

    job_total = sum_now + sum_future
    if job_total <= 0 or sum_future <= 0:
        return 0
    if sum_now <= 0:
        return d_total
    future_qty = int(math.floor((d_total * sum_future) / job_total))
    return max(0, min(d_total, future_qty))


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
    idx_po_due = _header_index(
        po_hdr,
        [
            "due date",
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
        order_raw = _as_text(_cell(row, idx_po_order)).strip()
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
    idx_ml_qty = _header_index(ml_hdr, ["qty", "quantity"])
    if idx_ml_qty < 0:
        idx_ml_qty = _find_qty_column(ml_hdr)

    items_now: List[Dict[str, Any]] = []
    items_future: List[Dict[str, Any]] = []

    for row in mi[1:]:
        name = _as_text(_cell(row, 0)).strip()
        if not name:
            continue
        on_hand = _as_num(_cell(row, 1))
        on_ord = _as_num(_cell(row, 2))
        unit = _as_text(_cell(row, 3))
        min_inv = _as_num(_cell(row, 4))
        reorder = _as_num(_cell(row, 5))
        vendor = _as_text(_cell(row, 8)).strip() or "Misc."
        total = on_hand + on_ord
        deficit = max(0, int(math.ceil(reorder - total)))
        need = (min_inv == 0 and on_hand < 0) or (min_inv > 0 and total < min_inv and deficit > 0)
        if not (need and deficit > 0):
            continue
        future_mat = _future_qty_material(
            name,
            deficit,
            ml,
            idx_ml_order,
            idx_ml_mat,
            idx_ml_inout,
            idx_ml_qty,
            stg_by_order,
            due_by_order,
            ship_by_order,
            hs_by_order,
        )
        now_mat = max(0, deficit - future_mat)
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

    inv_by_code: Dict[str, float] = {}
    ord_by_code: Dict[str, float] = {}
    for row in ti[1:]:
        code = _first_four_digits(_cell(row, 0))
        if not code:
            continue
        inv_by_code[code] = inv_by_code.get(code, 0) + _as_num(_cell(row, 1))
        ord_by_code[code] = ord_by_code.get(code, 0) + _as_num(_cell(row, 2))

    active_codes: Dict[str, bool] = {}
    usage_by_code: Dict[str, Dict[str, bool]] = {}
    for row in td[1:]:
        td_order = _as_text(_cell(row, 1)).strip()
        td_color = _as_text(_cell(row, 2))
        inout = _as_text(_cell(row, 6)).lower()
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
        current_cones = inv_by_code.get(code_key, 0) + ord_by_code.get(code_key, 0)
        remaining_pct = (current_cones / HEADS) * 100
        usage_map = usage_by_code.get(code_key) or {}
        usage_count = len(usage_map)
        pods_to_order = 0
        if current_cones < 0:
            pods_to_order = int(math.ceil((-current_cones) / HEADS))
        else:
            if remaining_pct <= 0:
                pods_to_order = 1
            elif usage_count > 30 and remaining_pct < 25:
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
    }
    logger.info(
        "JRCO to-order (Sheets API): M rows=%s N rows=%s | PO due=%s ship=%s hs=%s | ML order=%s mat=%s in/out=%s qty=%s",
        stats["mRows"],
        stats["nRows"],
        idx_po_due,
        idx_po_ship,
        idx_po_hs,
        idx_ml_order,
        idx_ml_mat,
        idx_ml_inout,
        idx_ml_qty,
    )
    return stats
