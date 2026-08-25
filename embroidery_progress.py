"""
Embroidery floor progress: pieces completed per order.

Local cache: backend/data/embroidery_progress.json
Durable store: Google Sheet tab "Embroidery Progress"
  A: Order #  B: Qty Completed  C: Updated At

Floor tablets read/write via API; server emits socket updates.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_BASE = Path(__file__).resolve().parent
DATA_DIR = _BASE / "data"
PROGRESS_PATH = DATA_DIR / "embroidery_progress.json"
SHEET_TAB = os.environ.get("EMBROIDERY_PROGRESS_TAB", "Embroidery Progress")
# Same-process overlay so combined/changes see +N even if this worker's disk is empty.
_MEM: Dict[str, dict] = {}


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
            out.append({"at": at, "increment": inc})
    return out[-24:]


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


def compute_timing(row: Optional[dict]) -> dict:
    """Average +N-to-+N cycle time, skipping tiny taps and long breaks."""
    runs = _clean_runs((row or {}).get("runs"))
    cycles = []
    for i in range(1, len(runs)):
        t0 = _parse_iso(runs[i - 1].get("at"))
        t1 = _parse_iso(runs[i].get("at"))
        if not t0 or not t1:
            continue
        ms = (t1 - t0).total_seconds() * 1000.0
        # Ignore double-taps and lunch/overnight gaps
        if 2 * 60 * 1000 <= ms <= 4 * 60 * 60 * 1000:
            cycles.append(ms)
    avg = int(round(sum(cycles) / len(cycles))) if cycles else 0
    last = runs[-1]["at"] if runs else str((row or {}).get("updatedAt") or "")
    return {
        "avgCycleMs": avg,
        "lastRunAt": last,
        "runCount": len(runs),
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
                range=f"'{SHEET_TAB}'!A1:C1",
                valueInputOption="RAW",
                body={"values": [["Order #", "Qty Completed", "Updated At"]]},
            ).execute()
            logger.info("Created Google Sheet tab %r", SHEET_TAB)
        return True
    except Exception:
        logger.exception("ensure_sheet_tab(%s) failed", SHEET_TAB)
        return False


def parse_sheet_values(values: Any) -> Dict[str, dict]:
    """Parse Embroidery Progress (or similar) grid into {orderId: row}."""
    out: Dict[str, dict] = {}
    rows = values or []
    if not rows:
        return out
    order_i, qty_i, updated_i = 0, 1, 2
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
        out[oid] = {"completedQty": max(0, qty), "updatedAt": updated, "runs": []}
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


def upsert_sheet_row(
    sheets_service,
    spreadsheet_id: str,
    oid: str,
    qty: int,
    updated_at: str,
) -> bool:
    """Write one order's qty. Never clears other rows."""
    if not sheets_service or not spreadsheet_id or not oid:
        return False
    rng = f"'{SHEET_TAB}'!A:C"
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
        body = {"values": [[oid, int(qty), str(updated_at or "")]]}
        if row_num:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{SHEET_TAB}'!A{row_num}:C{row_num}",
                valueInputOption="RAW",
                body=body,
            ).execute()
        else:
            if not values:
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=spreadsheet_id,
                    range=f"'{SHEET_TAB}'!A1",
                    valueInputOption="RAW",
                    body={
                        "values": [
                            ["Order #", "Qty Completed", "Updated At"],
                            [oid, int(qty), str(updated_at or "")],
                        ]
                    },
                ).execute()
            else:
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
        ):
            ok = False
    return ok


def _merge_progress_rows(file_rows: Dict[str, dict], sheet_rows: Optional[Dict[str, dict]]) -> Dict[str, dict]:
    if sheet_rows is None:
        return dict(file_rows)
    out: Dict[str, dict] = {}
    for oid in set(file_rows) | set(sheet_rows):
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
        out[oid] = {
            "completedQty": max(0, fq, sq),
            "updatedAt": str(updated or ""),
            "runs": _clean_runs(fr.get("runs") or sr.get("runs")),
            "manualStart": bool(fr.get("manualStart") or sr.get("manualStart")),
            "manualStartAt": str(fr.get("manualStartAt") or sr.get("manualStartAt") or ""),
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
            combined["runs"] = _clean_runs(combined.get("runs") or prev.get("runs"))
            out[oid] = combined
    return out


def combine_loaded(
    file_rows: Optional[Dict[str, dict]] = None,
    sheet_rows: Optional[Dict[str, dict]] = None,
) -> Dict[str, dict]:
    return _overlay_mem(_merge_progress_rows(file_rows or {}, sheet_rows))


def load_all(
    sheets_service: Any = None,
    spreadsheet_id: Optional[str] = None,
) -> Dict[str, dict]:
    with _LOCK:
        file_rows = _load_from_file()
        sheet_rows = None
        if sheets_service and spreadsheet_id:
            sheet_rows = load_from_sheet(sheets_service, spreadsheet_id)
        merged = _overlay_mem(_merge_progress_rows(file_rows, sheet_rows))
        if sheet_rows is not None and merged != file_rows:
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
) -> dict:
    oid = _norm_oid(order_id)
    if not oid:
        raise ValueError("orderId is required")
    qty = max(0, int(completed_qty))
    with _LOCK:
        file_rows = _load_from_file()
        sheet_rows = load_from_sheet(sheets_service, spreadsheet_id or "")
        rows = _merge_progress_rows(file_rows, sheet_rows)
        prev = rows.get(oid) or {}
        runs = _clean_runs(prev.get("runs"))
        now = _now_iso()
        try:
            inc = int(increment) if increment is not None else 0
        except (TypeError, ValueError):
            inc = 0
        if inc > 0:
            runs.append({"at": now, "increment": inc})
            runs = runs[-24:]
        rows[oid] = {
            "completedQty": qty,
            "updatedAt": now,
            "runs": runs,
            "manualStart": bool(prev.get("manualStart")),
            "manualStartAt": str(prev.get("manualStartAt") or ""),
        }
        _MEM[oid] = rows[oid]
        _save_to_file(rows)
        upsert_sheet_row(sheets_service, spreadsheet_id or "", oid, qty, now)
        timing = compute_timing(rows[oid])
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
