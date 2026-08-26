"""
Embroidery floor progress: pieces completed per order.

Local cache: backend/data/embroidery_progress.json
Durable store:
  Google Sheet tab "Embroidery Progress" — one row per order (current qty)
  Google Sheet tab "Embroidery Run Log" — one row per +N (never overwritten)

Floor tablets read/write via API; server emits socket updates.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_BASE = Path(__file__).resolve().parent
DATA_DIR = _BASE / "data"
PROGRESS_PATH = DATA_DIR / "embroidery_progress.json"
SHEET_TAB = os.environ.get("EMBROIDERY_PROGRESS_TAB", "Embroidery Progress")
LOG_TAB = os.environ.get("EMBROIDERY_RUN_LOG_TAB", "Embroidery Run Log")
LOG_HEADERS = [
    "Posted At",
    "Order #",
    "Machine",
    "+N",
    "Qty After",
    "Actual Min",
    "Expected Min",
    "Ahead Min",
    "Stitch Count",
    "Heads",
    "Posted At ISO",
]
PROGRESS_HEADERS = [
    "Order #",
    "Qty Completed",
    "Updated At",
    "Runs JSON",
    "Manual Start",
    "Manual Start At",
]
_ET = ZoneInfo("America/New_York") if ZoneInfo else None
# Same-process overlay so combined/changes see +N even if this worker's disk is empty.
_MEM: Dict[str, dict] = {}
_LOG_RUNS: Optional[Dict[str, list]] = None
_LOG_RUNS_AT = 0.0
_LOG_RUNS_TTL = 60.0


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_qty_cell(raw: Any) -> int:
    if raw is None or raw is False or raw == "":
        return 0
    try:
        return max(0, int(round(float(raw))))
    except (TypeError, ValueError):
        s = str(raw).strip().replace(",", "")
        m = re.search(r"[-+]?\d*\.?\d+", s)
        if not m:
            return 0
        try:
            return max(0, int(round(float(m.group(0)))))
        except (TypeError, ValueError):
            return 0


def _norm_oid(order_id: Any) -> str:
    s = str(order_id or "").strip()
    if not s:
        return ""
    try:
        f = float(s)
        if abs(f - round(f)) < 1e-9:
            return str(int(round(f)))
        return s
    except (TypeError, ValueError):
        return s


def _parse_iso(val: Any) -> Optional[datetime]:
    s = str(val or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def expected_cycle_ms(stitch_count: Any, pieces: Any, head_count: Any) -> int:
    """One +N cycle: stitches/30k hours times ceil(pieces/heads)."""
    try:
        heads = max(1, int(head_count or 6))
    except (TypeError, ValueError):
        heads = 6
    try:
        left = max(0, int(pieces or 0))
    except (TypeError, ValueError):
        left = 0
    runs = math.ceil(left / heads) if left else 0
    try:
        stitches = float(stitch_count or 0)
    except (TypeError, ValueError):
        stitches = 0
    if stitches <= 0:
        stitches = 30000
    return int(round((stitches / 30000.0) * runs * 3600000))


def _ms_to_min(ms: Any) -> float:
    try:
        n = float(ms or 0)
    except (TypeError, ValueError):
        return 0.0
    if n <= 0:
        return 0.0
    return round(n / 60000.0, 2)


def _min_cell_to_ms(val: Any) -> int:
    try:
        n = float(val)
    except (TypeError, ValueError):
        s = str(val or "").strip().replace(",", "")
        if not s:
            return 0
        try:
            n = float(s)
        except (TypeError, ValueError):
            return 0
    if n <= 0:
        return 0
    return int(round(n * 60000))


def _google_serial_to_dt(n: float) -> Optional[datetime]:
    try:
        serial = float(n)
    except (TypeError, ValueError):
        return None
    if serial < 20000 or serial > 80000:
        return None
    dt = datetime(1899, 12, 30) + timedelta(days=serial)
    if _ET is not None:
        return dt.replace(tzinfo=_ET)
    return dt.replace(tzinfo=timezone.utc)


def _parse_log_posted_at(val: Any) -> str:
    """Posted At from the run log → UTC ISO. Survives formatted text and Sheets serials."""
    if val is None or val == "":
        return ""
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        dt = _google_serial_to_dt(val)
        if dt is not None:
            return dt.astimezone(timezone.utc).isoformat()
    s = str(val).strip()
    if not s:
        return ""
    parsed = _parse_iso(s)
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_ET or timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    try:
        n = float(s)
    except (TypeError, ValueError):
        n = None
    if n is not None:
        dt = _google_serial_to_dt(n)
        if dt is not None:
            return dt.astimezone(timezone.utc).isoformat()
    for fmt in (
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            dt = dt.replace(tzinfo=_ET or timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return ""


def _run_merge_key(r: dict) -> tuple:
    at = str((r or {}).get("at") or "")
    try:
        inc = int((r or {}).get("increment") or 0)
    except (TypeError, ValueError):
        inc = 0
    return (at[:19], inc)


def _merge_run_lists(*lists: Any) -> list:
    seen = set()
    out = []
    combined = []
    for lst in lists:
        if isinstance(lst, list):
            combined.extend(lst)
    combined.sort(key=lambda r: str((r or {}).get("at") or ""))
    for r in combined:
        if not isinstance(r, dict):
            continue
        key = _run_merge_key(r)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return _clean_runs(out)


def _parse_runs_json_cell(raw: Any) -> list:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return _clean_runs(raw)
    s = str(raw).strip()
    if not s or s[0] not in "[ {":
        return []
    try:
        data = json.loads(s)
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get("runs") or []
    return _clean_runs(data)


def _et_sheet_time(iso: str) -> str:
    dt = _parse_iso(iso) or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if _ET is not None:
        dt = dt.astimezone(_ET)
    ampm = dt.strftime("%p")
    hour = dt.hour % 12 or 12
    return f"{dt.month}/{dt.day}/{dt.year} {hour}:{dt.minute:02d}:{dt.second:02d} {ampm}"


def _cycle_ms_between(earlier_iso: str, later_iso: str) -> int:
    t0 = _parse_iso(earlier_iso)
    t1 = _parse_iso(later_iso)
    if not t0 or not t1:
        return 0
    ms = (t1 - t0).total_seconds() * 1000.0
    if 2 * 60 * 1000 <= ms <= 4 * 60 * 60 * 1000:
        return int(round(ms))
    return 0


def _usable_for_average(cycle_ms: int, expected_ms: int) -> bool:
    """Skip taps, overnight gaps, and break-length outliers (e.g. +N after 3 hours)."""
    if cycle_ms < 2 * 60 * 1000:
        return False
    if cycle_ms > 4 * 60 * 60 * 1000:
        return False
    if expected_ms > 0:
        limit = max(int(expected_ms * 2), int(expected_ms) + 15 * 60 * 1000)
        if cycle_ms > limit:
            return False
    elif cycle_ms > 90 * 60 * 1000:
        return False
    return True


def _average_cycle_ms(cycles: list) -> int:
    if not cycles:
        return 0
    if len(cycles) >= 3:
        ordered = sorted(cycles)
        median = ordered[len(ordered) // 2]
        if median > 0:
            cycles = [c for c in cycles if c <= median * 2.5]
    if not cycles:
        return 0
    return int(round(sum(cycles) / len(cycles)))


def _clean_runs(raw: Any) -> list:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            inc = int(item.get("increment") or 0)
        except (TypeError, ValueError):
            continue
        at = str(item.get("at") or "").strip()
        if inc > 0 and at:
            rec = {"at": at, "increment": inc}
            try:
                rec["cycleMs"] = max(0, int(item.get("cycleMs") or 0))
            except (TypeError, ValueError):
                rec["cycleMs"] = 0
            try:
                rec["expectedMs"] = max(0, int(item.get("expectedMs") or 0))
            except (TypeError, ValueError):
                rec["expectedMs"] = 0
            out.append(rec)
    return out[-200:]


def _normalize_row(v: Any) -> Optional[dict]:
    if isinstance(v, dict):
        try:
            qty = int(v.get("completedQty") or 0)
        except (TypeError, ValueError):
            qty = 0
        return {
            "completedQty": max(0, qty),
            "updatedAt": str(v.get("updatedAt") or ""),
            "runs": _clean_runs(v.get("runs")),
            "manualStart": bool(v.get("manualStart")),
            "manualStartAt": str(v.get("manualStartAt") or ""),
        }
    try:
        return {
            "completedQty": max(0, int(v)),
            "updatedAt": "",
            "runs": [],
            "manualStart": False,
            "manualStartAt": "",
        }
    except (TypeError, ValueError):
        return None


def compute_timing(
    row: Optional[dict],
    stitch_count: Any = 0,
    head_count: Any = 6,
) -> dict:
    """Average +N-to-+N cycle time, skipping taps, breaks, and outlier gaps."""
    runs = _clean_runs((row or {}).get("runs"))
    start_at = str((row or {}).get("manualStartAt") or "")
    cycles = []
    recent = []
    for i, r in enumerate(runs):
        prev_at = runs[i - 1].get("at") if i > 0 else start_at
        cycle_ms = int(r.get("cycleMs") or 0) or _cycle_ms_between(prev_at, r.get("at"))
        inc = int(r.get("increment") or 0)
        expected_ms = int(r.get("expectedMs") or 0) or expected_cycle_ms(
            stitch_count, inc, head_count
        )
        if cycle_ms and _usable_for_average(cycle_ms, expected_ms):
            cycles.append(cycle_ms)
        ahead_ms = (expected_ms - cycle_ms) if cycle_ms and expected_ms else 0
        vs_prev_ms = None
        if cycle_ms >= 2 * 60 * 1000:
            if i > 0:
                prev = recent[-1] if recent else None
                prev_cycle = int((prev or {}).get("cycleMs") or 0)
                prev_inc = int((prev or {}).get("increment") or 0)
                if prev_cycle >= 2 * 60 * 1000 and prev_inc > 0 and inc > 0:
                    vs_prev_ms = int(round((prev_cycle / prev_inc) * inc - cycle_ms))
            if vs_prev_ms is None and expected_ms:
                vs_prev_ms = int(expected_ms - cycle_ms)
        recent.append(
            {
                "at": r.get("at"),
                "increment": inc,
                "cycleMs": cycle_ms,
                "expectedMs": expected_ms,
                "aheadMs": ahead_ms,
                "vsPrevMs": vs_prev_ms,
            }
        )
    avg = _average_cycle_ms(cycles)
    last = runs[-1]["at"] if runs else str((row or {}).get("updatedAt") or "")
    typical = expected_cycle_ms(stitch_count, head_count, head_count)
    return {
        "avgCycleMs": avg,
        "lastRunAt": last,
        "runCount": len(runs),
        "recentRuns": recent[-4:],
        "expectedRunMs": typical,
    }


def _load_from_file() -> Dict[str, dict]:
    if not PROGRESS_PATH.exists():
        return {}
    try:
        with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get("orders") if isinstance(data, dict) else data
        if not isinstance(rows, dict):
            return {}
        out = {}
        for k, v in rows.items():
            oid = _norm_oid(k)
            if not oid:
                continue
            row = _normalize_row(v)
            if row:
                out[oid] = row
        return out
    except Exception:
        logger.exception("Failed to read embroidery progress file")
        return {}


def _save_to_file(rows: Dict[str, dict]) -> Dict[str, dict]:
    _ensure_data_dir()
    payload = {"orders": rows}
    tmp = PROGRESS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp.replace(PROGRESS_PATH)
    return rows


def ensure_sheet_tab(sheets_service, spreadsheet_id: str) -> bool:
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
                body={"requests": [{"addSheet": {"properties": {"title": SHEET_TAB}}}]},
            ).execute()
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{SHEET_TAB}'!A1:F1",
                valueInputOption="RAW",
                body={"values": [PROGRESS_HEADERS]},
            ).execute()
            logger.info("Created Google Sheet tab %r", SHEET_TAB)
        if LOG_TAB not in titles:
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": LOG_TAB}}}]},
            ).execute()
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{LOG_TAB}'!A1:K1",
                valueInputOption="RAW",
                body={"values": [LOG_HEADERS]},
            ).execute()
            logger.info("Created Google Sheet tab %r", LOG_TAB)
        return True
    except Exception:
        logger.exception("ensure_sheet_tab(%s) failed", SHEET_TAB)
        return False


def append_run_log(
    sheets_service,
    spreadsheet_id: str,
    *,
    oid: str,
    machine: str,
    increment: int,
    qty_after: int,
    cycle_ms: int,
    expected_ms: int,
    stitch_count: int,
    head_count: int,
    posted_at: str,
) -> bool:
    """Append one +N row. Never updates or deletes existing log rows."""
    if not sheets_service or not spreadsheet_id or not oid or increment <= 0:
        return False
    try:
        if not ensure_sheet_tab(sheets_service, spreadsheet_id):
            return False
        actual_min = _ms_to_min(cycle_ms)
        expected_min = _ms_to_min(expected_ms)
        ahead_min = round(expected_min - actual_min, 2) if actual_min and expected_min else ""
        row = [
            _et_sheet_time(posted_at),
            oid,
            str(machine or ""),
            int(increment),
            int(qty_after),
            actual_min or "",
            expected_min or "",
            ahead_min,
            int(stitch_count or 0),
            int(head_count or 0),
            str(posted_at or ""),
        ]
        sheets_service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"'{LOG_TAB}'!A:K",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        return True
    except Exception:
        logger.exception("Failed to append embroidery run log for %s", oid)
        return False


def parse_run_log_values(values: Any) -> Dict[str, list]:
    """Parse Embroidery Run Log grid into {orderId: [runs...]}. Source of truth after rebuilds."""
    out: Dict[str, list] = {}
    rows = values or []
    if not rows:
        return out
    posted_i, order_i, inc_i, actual_i, expected_i, iso_i = 0, 1, 3, 5, 6, 10
    start = 0
    first = [str(h or "").strip().lower() for h in (rows[0] or [])]
    if first and (
        "posted" in first[0]
        or first[0] in ("posted at", "time", "timestamp")
        or "order" in (first[1] if len(first) > 1 else "")
    ):
        start = 1
        for i, h in enumerate(first):
            if h in ("posted at", "posted", "time", "timestamp"):
                posted_i = i
            elif h in ("order #", "order#", "order"):
                order_i = i
            elif h in ("+n", "n", "increment", "qty posted"):
                inc_i = i
            elif h in ("actual min", "actual", "cycle min"):
                actual_i = i
            elif h in ("expected min", "expected"):
                expected_i = i
            elif "iso" in h and "posted" in h:
                iso_i = i
    for row in rows[start:]:
        if not row:
            continue
        oid = _norm_oid(row[order_i] if order_i < len(row) else "")
        if not oid:
            continue
        try:
            inc = int(round(float(row[inc_i] if inc_i < len(row) else 0)))
        except (TypeError, ValueError):
            inc = 0
        if inc <= 0:
            continue
        iso = ""
        if iso_i < len(row):
            iso = str(row[iso_i] or "").strip()
            if iso and not _parse_iso(iso):
                iso = _parse_log_posted_at(row[iso_i])
        if not iso:
            iso = _parse_log_posted_at(row[posted_i] if posted_i < len(row) else "")
        if not iso:
            continue
        cycle_ms = _min_cell_to_ms(row[actual_i] if actual_i < len(row) else 0)
        expected_ms = _min_cell_to_ms(row[expected_i] if expected_i < len(row) else 0)
        rec = {
            "at": iso,
            "increment": inc,
            "cycleMs": cycle_ms,
            "expectedMs": expected_ms,
        }
        bucket = out.setdefault(oid, [])
        key = _run_merge_key(rec)
        if any(_run_merge_key(x) == key for x in bucket):
            continue
        bucket.append(rec)
    for oid, runs in out.items():
        runs.sort(key=lambda r: str(r.get("at") or ""))
        for i, r in enumerate(runs):
            if int(r.get("cycleMs") or 0) <= 0 and i > 0:
                gap = _cycle_ms_between(runs[i - 1].get("at"), r.get("at"))
                if gap:
                    r["cycleMs"] = gap
        out[oid] = runs[-200:]
    return out


def load_from_run_log(sheets_service, spreadsheet_id: str, force: bool = False) -> Dict[str, list]:
    """Load +N history from the durable run log. Cached briefly; empty after Render rebuilds otherwise."""
    global _LOG_RUNS, _LOG_RUNS_AT
    if not sheets_service or not spreadsheet_id:
        return dict(_LOG_RUNS or {})
    now = time.time()
    if (
        not force
        and _LOG_RUNS is not None
        and now - _LOG_RUNS_AT < _LOG_RUNS_TTL
    ):
        return dict(_LOG_RUNS)
    rng = f"'{LOG_TAB}'!A1:K"
    try:
        resp = (
            sheets_service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=rng,
                valueRenderOption="FORMATTED_VALUE",
            )
            .execute()
        )
        parsed = parse_run_log_values(resp.get("values") or [])
        _LOG_RUNS = parsed
        _LOG_RUNS_AT = now
        return dict(parsed)
    except Exception:
        logger.exception("Failed to read embroidery run log")
        return dict(_LOG_RUNS or {})


def parse_sheet_values(values: Any) -> Dict[str, dict]:
    """Parse Embroidery Progress (or similar) grid into {orderId: row}."""
    out: Dict[str, dict] = {}
    rows = values or []
    if not rows:
        return out
    order_i, qty_i, updated_i = 0, 1, 2
    runs_i, start_i, start_at_i = 3, 4, 5
    start = 0
    first = [str(h or "").strip().lower() for h in (rows[0] or [])]
    headerish = any(
        h in (
            "order #",
            "order#",
            "order",
            "qty completed",
            "quantity made",
            "qty made",
            "updated at",
        )
        for h in first
    )
    if headerish:
        start = 1
        for i, h in enumerate(first):
            if h in ("order #", "order#", "order"):
                order_i = i
            elif h in (
                "qty completed",
                "quantity made",
                "qty made",
                "completed qty",
                "quantity completed",
                "qty done",
                "pieces completed",
                "pieces done",
            ) or ("made" in h and ("qty" in h or "quantity" in h or "piece" in h)) or (
                "completed" in h and ("qty" in h or "quantity" in h or "piece" in h)
            ):
                qty_i = i
            elif "updated" in h:
                updated_i = i
            elif h in ("runs json", "runs", "run json", "run history"):
                runs_i = i
            elif h in ("manual start", "started", "start locked"):
                start_i = i
            elif h in ("manual start at", "start at", "started at"):
                start_at_i = i
    for row in rows[start:]:
        if not row:
            continue
        oid = _norm_oid(row[order_i] if order_i < len(row) else "")
        if not oid:
            continue
        qty = _parse_qty_cell(row[qty_i] if qty_i < len(row) else 0)
        updated = str(row[updated_i]).strip() if updated_i < len(row) else ""
        prev = out.get(oid)
        if prev and int(prev.get("completedQty") or 0) > qty:
            continue
        runs = _parse_runs_json_cell(row[runs_i] if runs_i < len(row) else "")
        manual = False
        if start_i < len(row):
            manual = str(row[start_i] or "").strip().lower() in ("true", "1", "yes", "y")
        manual_at = str(row[start_at_i]).strip() if start_at_i < len(row) else ""
        out[oid] = {
            "completedQty": max(0, qty),
            "updatedAt": updated,
            "runs": runs,
            "manualStart": manual,
            "manualStartAt": manual_at,
        }
    return out


def load_from_sheet(sheets_service, spreadsheet_id: str) -> Optional[Dict[str, dict]]:
    if not sheets_service or not spreadsheet_id:
        return None
    rng = f"'{SHEET_TAB}'!A1:Z"
    try:
        resp = (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=rng)
            .execute()
        )
        return parse_sheet_values(resp.get("values") or [])
    except Exception:
        # Tab may not exist yet — create once, then retry. Do not wipe anything.
        try:
            if not ensure_sheet_tab(sheets_service, spreadsheet_id):
                return None
            resp = (
                sheets_service.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id, range=rng)
                .execute()
            )
            return parse_sheet_values(resp.get("values") or [])
        except Exception:
            logger.exception("Failed to read embroidery progress from sheet")
            return None


def _progress_sheet_cells(
    oid: str,
    qty: int,
    updated_at: str,
    runs: Any = None,
    manual_start: bool = False,
    manual_start_at: str = "",
) -> list:
    payload = _clean_runs(runs)[-50:]
    return [
        oid,
        int(qty),
        str(updated_at or ""),
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "TRUE" if manual_start else "FALSE",
        str(manual_start_at or ""),
    ]


def upsert_sheet_row(
    sheets_service,
    spreadsheet_id: str,
    oid: str,
    qty: int,
    updated_at: str,
    runs: Any = None,
    manual_start: bool = False,
    manual_start_at: str = "",
) -> bool:
    """Write one order's qty + run history. Never clears other rows."""
    if not sheets_service or not spreadsheet_id or not oid:
        return False
    rng = f"'{SHEET_TAB}'!A:F"
    cells = _progress_sheet_cells(
        oid, qty, updated_at, runs, manual_start, manual_start_at
    )
    try:
        try:
            resp = (
                sheets_service.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id, range=rng)
                .execute()
            )
        except Exception:
            if not ensure_sheet_tab(sheets_service, spreadsheet_id):
                return False
            resp = (
                sheets_service.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id, range=rng)
                .execute()
            )
        values = resp.get("values") or []
        row_num = None
        for i, row in enumerate(values):
            if not row:
                continue
            cell = _norm_oid(row[0] if len(row) > 0 else "")
            if i == 0 and str(row[0] or "").strip().lower() in ("order #", "order#", "order"):
                continue
            if cell == oid:
                row_num = i + 1
                break
        body = {"values": [cells]}
        if row_num:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{SHEET_TAB}'!A{row_num}:F{row_num}",
                valueInputOption="RAW",
                body=body,
            ).execute()
        else:
            if not values:
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=spreadsheet_id,
                    range=f"'{SHEET_TAB}'!A1",
                    valueInputOption="RAW",
                    body={"values": [PROGRESS_HEADERS, cells]},
                ).execute()
            else:
                if values and str(values[0][0] or "").strip().lower() in (
                    "order #",
                    "order#",
                    "order",
                ):
                    header_row = list(values[0]) + [""] * max(0, 6 - len(values[0]))
                    header_row[:6] = PROGRESS_HEADERS
                    sheets_service.spreadsheets().values().update(
                        spreadsheetId=spreadsheet_id,
                        range=f"'{SHEET_TAB}'!A1:F1",
                        valueInputOption="RAW",
                        body={"values": [header_row[:6]]},
                    ).execute()
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id,
                    range=rng,
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body=body,
                ).execute()
        return True
    except Exception:
        logger.exception("Failed to upsert embroidery progress for %s", oid)
        return False


def save_to_sheet(sheets_service, spreadsheet_id: str, rows: Dict[str, dict]) -> bool:
    """Upsert rows. Does not clear the tab (empty worker snapshots must not wipe qty)."""
    if not sheets_service or not spreadsheet_id or not rows:
        return False
    ok = True
    for oid, info in rows.items():
        if not upsert_sheet_row(
            sheets_service,
            spreadsheet_id,
            oid,
            int(info.get("completedQty") or 0),
            str(info.get("updatedAt") or ""),
            info.get("runs"),
            bool(info.get("manualStart")),
            str(info.get("manualStartAt") or ""),
        ):
            ok = False
    return ok


def _merge_progress_rows(
    file_rows: Dict[str, dict],
    sheet_rows: Optional[Dict[str, dict]],
    log_runs: Optional[Dict[str, list]] = None,
) -> Dict[str, dict]:
    log_runs = log_runs or {}
    if sheet_rows is None and not log_runs:
        return dict(file_rows)
    sheet_rows = sheet_rows or {}
    out: Dict[str, dict] = {}
    for oid in set(file_rows) | set(sheet_rows) | set(log_runs):
        fr = file_rows.get(oid) or {}
        sr = sheet_rows.get(oid) or {}
        try:
            fq = int(fr.get("completedQty") or 0)
        except (TypeError, ValueError):
            fq = 0
        try:
            sq = int(sr.get("completedQty") or 0)
        except (TypeError, ValueError):
            sq = 0
        ft = _parse_iso(fr.get("updatedAt"))
        st = _parse_iso(sr.get("updatedAt"))
        if st and ft:
            updated = sr.get("updatedAt") if st >= ft else fr.get("updatedAt")
        else:
            updated = sr.get("updatedAt") or fr.get("updatedAt") or ""
        runs = _merge_run_lists(fr.get("runs"), sr.get("runs"), log_runs.get(oid))
        out[oid] = {
            "completedQty": max(0, fq, sq),
            "updatedAt": str(updated or ""),
            "runs": runs,
            "manualStart": bool(
                fr.get("manualStart") or sr.get("manualStart") or runs
            ),
            "manualStartAt": str(
                fr.get("manualStartAt") or sr.get("manualStartAt") or ""
            ),
        }
    return out


def _overlay_mem(merged: Dict[str, dict]) -> Dict[str, dict]:
    if not _MEM:
        return merged
    out = dict(merged)
    for oid, row in _MEM.items():
        prev = out.get(oid) or {}
        try:
            mq = int((row or {}).get("completedQty") or 0)
        except (TypeError, ValueError):
            mq = 0
        try:
            pq = int(prev.get("completedQty") or 0)
        except (TypeError, ValueError):
            pq = 0
        if oid not in out or mq >= pq:
            combined = dict(prev)
            combined.update(row or {})
            combined["completedQty"] = max(mq, pq)
            combined["runs"] = _merge_run_lists(prev.get("runs"), combined.get("runs"))
            out[oid] = combined
    return out


def combine_loaded(
    file_rows: Optional[Dict[str, dict]] = None,
    sheet_rows: Optional[Dict[str, dict]] = None,
    log_runs: Optional[Dict[str, list]] = None,
) -> Dict[str, dict]:
    return _overlay_mem(_merge_progress_rows(file_rows or {}, sheet_rows, log_runs))


def remember_rows(rows: Dict[str, dict]) -> None:
    """Keep reconstructed run history on disk for this Render instance."""
    if not rows:
        return
    with _LOCK:
        file_rows = _load_from_file()
        merged = _merge_progress_rows(file_rows, rows)
        try:
            _save_to_file(merged)
        except Exception:
            logger.exception("Failed to remember embroidery progress rows")
        for oid, row in merged.items():
            if row.get("runs") or int(row.get("completedQty") or 0):
                _MEM[oid] = row


def load_all(
    sheets_service: Any = None,
    spreadsheet_id: Optional[str] = None,
) -> Dict[str, dict]:
    with _LOCK:
        file_rows = _load_from_file()
        sheet_rows = None
        log_runs = None
        if sheets_service and spreadsheet_id:
            sheet_rows = load_from_sheet(sheets_service, spreadsheet_id)
            log_runs = load_from_run_log(sheets_service, spreadsheet_id)
        merged = _overlay_mem(_merge_progress_rows(file_rows, sheet_rows, log_runs))
        if (sheet_rows is not None or log_runs) and merged != file_rows:
            try:
                _save_to_file(merged)
            except Exception:
                logger.exception("Failed to persist merged embroidery progress")
        return merged


def get_row(
    order_id: str,
    sheets_service: Any = None,
    spreadsheet_id: Optional[str] = None,
) -> dict:
    oid = _norm_oid(order_id)
    if not oid:
        return {"completedQty": 0, "updatedAt": "", "runs": []}
    rows = load_all(sheets_service, spreadsheet_id)
    return rows.get(oid) or {"completedQty": 0, "updatedAt": "", "runs": []}


def get_qty(
    order_id: str,
    sheets_service: Any = None,
    spreadsheet_id: Optional[str] = None,
) -> int:
    return int(get_row(order_id, sheets_service, spreadsheet_id).get("completedQty") or 0)


def set_qty(
    order_id: str,
    completed_qty: int,
    sheets_service: Any = None,
    spreadsheet_id: Optional[str] = None,
    increment: Optional[int] = None,
    machine: str = "",
    stitch_count: int = 0,
    head_count: int = 6,
) -> dict:
    oid = _norm_oid(order_id)
    if not oid:
        raise ValueError("orderId is required")
    qty = max(0, int(completed_qty))
    log_payload = None
    with _LOCK:
        file_rows = _load_from_file()
        sheet_rows = load_from_sheet(sheets_service, spreadsheet_id or "")
        log_runs = load_from_run_log(sheets_service, spreadsheet_id or "")
        rows = _merge_progress_rows(file_rows, sheet_rows, log_runs)
        prev = rows.get(oid) or {}
        runs = _clean_runs(prev.get("runs"))
        now = _now_iso()
        try:
            inc = int(increment) if increment is not None else 0
        except (TypeError, ValueError):
            inc = 0
        cycle_ms = 0
        expected_ms = 0
        try:
            heads = max(1, int(head_count or 6))
        except (TypeError, ValueError):
            heads = 6
        try:
            stitches = int(stitch_count or 0)
        except (TypeError, ValueError):
            stitches = 0
        if inc > 0:
            prev_at = ""
            if runs:
                prev_at = str(runs[-1].get("at") or "")
            if not prev_at:
                prev_at = str(prev.get("manualStartAt") or "")
            cycle_ms = _cycle_ms_between(prev_at, now)
            expected_ms = expected_cycle_ms(stitches, inc, heads)
            runs.append(
                {
                    "at": now,
                    "increment": inc,
                    "cycleMs": cycle_ms,
                    "expectedMs": expected_ms,
                }
            )
            runs = runs[-200:]
            log_payload = {
                "oid": oid,
                "machine": str(machine or ""),
                "increment": inc,
                "qty_after": qty,
                "cycle_ms": cycle_ms,
                "expected_ms": expected_ms,
                "stitch_count": stitches,
                "head_count": heads,
                "posted_at": now,
            }
        rows[oid] = {
            "completedQty": qty,
            "updatedAt": now,
            "runs": runs,
            "manualStart": bool(prev.get("manualStart") or runs),
            "manualStartAt": str(prev.get("manualStartAt") or ""),
        }
        _MEM[oid] = rows[oid]
        _save_to_file(rows)
        upsert_sheet_row(
            sheets_service,
            spreadsheet_id or "",
            oid,
            qty,
            now,
            runs,
            bool(rows[oid].get("manualStart")),
            str(rows[oid].get("manualStartAt") or ""),
        )
        timing = compute_timing(rows[oid], stitches, heads)
    if log_payload:
        append_run_log(sheets_service, spreadsheet_id or "", **log_payload)
    return {
        "orderId": oid,
        "completedQty": qty,
        "updatedAt": now,
        "manualStart": bool(rows[oid].get("manualStart")),
        **timing,
    }


def mark_manual_start(order_id: str, started_at: str = "") -> dict:
    """Lock start time to an operator-pressed Start. Survives first +N inference."""
    oid = _norm_oid(order_id)
    if not oid:
        raise ValueError("orderId is required")
    with _LOCK:
        rows = _load_from_file()
        prev = rows.get(oid) or {
            "completedQty": 0,
            "updatedAt": "",
            "runs": [],
            "manualStart": False,
            "manualStartAt": "",
        }
        prev["manualStart"] = True
        prev["manualStartAt"] = str(started_at or _now_iso())
        rows[oid] = prev
        _MEM[oid] = prev
        _save_to_file(rows)
        return dict(prev)
