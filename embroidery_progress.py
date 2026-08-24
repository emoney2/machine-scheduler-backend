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


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            if isinstance(v, dict):
                qty = int(v.get("completedQty") or 0)
                out[oid] = {
                    "completedQty": max(0, qty),
                    "updatedAt": str(v.get("updatedAt") or ""),
                }
            else:
                try:
                    out[oid] = {"completedQty": max(0, int(v)), "updatedAt": ""}
                except (TypeError, ValueError):
                    continue
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


def load_from_sheet(sheets_service, spreadsheet_id: str) -> Optional[Dict[str, dict]]:
    if not sheets_service or not spreadsheet_id:
        return None
    try:
        if not ensure_sheet_tab(sheets_service, spreadsheet_id):
            return None
        resp = (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"'{SHEET_TAB}'!A:C")
            .execute()
        )
        values = resp.get("values") or []
        out: Dict[str, dict] = {}
        for i, row in enumerate(values):
            if not row:
                continue
            oid = _norm_oid(row[0] if len(row) > 0 else "")
            if not oid:
                continue
            if i == 0 and oid.lower() in ("order #", "order#", "order"):
                continue
            try:
                qty = int(float(str(row[1]).strip())) if len(row) > 1 and row[1] not in (None, "") else 0
            except (TypeError, ValueError):
                qty = 0
            updated = str(row[2]).strip() if len(row) > 2 else ""
            out[oid] = {"completedQty": max(0, qty), "updatedAt": updated}
        return out
    except Exception:
        logger.exception("Failed to read embroidery progress from sheet")
        return None


def save_to_sheet(sheets_service, spreadsheet_id: str, rows: Dict[str, dict]) -> bool:
    if not sheets_service or not spreadsheet_id:
        return False
    try:
        if not ensure_sheet_tab(sheets_service, spreadsheet_id):
            return False
        ordered = sorted(rows.items(), key=lambda kv: kv[0])
        values = [["Order #", "Qty Completed", "Updated At"]] + [
            [oid, int(info.get("completedQty") or 0), str(info.get("updatedAt") or "")]
            for oid, info in ordered
        ]
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=f"'{SHEET_TAB}'!A:C",
            body={},
        ).execute()
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{SHEET_TAB}'!A1",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()
        return True
    except Exception:
        logger.exception("Failed to write embroidery progress to sheet")
        return False


def load_all(
    sheets_service: Any = None,
    spreadsheet_id: Optional[str] = None,
) -> Dict[str, dict]:
    with _LOCK:
        sheet_rows = load_from_sheet(sheets_service, spreadsheet_id or "")
        file_rows = _load_from_file()
        if sheet_rows is not None:
            if sheet_rows != file_rows:
                _save_to_file(sheet_rows)
            return sheet_rows
        return file_rows


def get_qty(
    order_id: str,
    sheets_service: Any = None,
    spreadsheet_id: Optional[str] = None,
) -> int:
    oid = _norm_oid(order_id)
    if not oid:
        return 0
    rows = load_all(sheets_service, spreadsheet_id)
    return int((rows.get(oid) or {}).get("completedQty") or 0)


def set_qty(
    order_id: str,
    completed_qty: int,
    sheets_service: Any = None,
    spreadsheet_id: Optional[str] = None,
) -> dict:
    oid = _norm_oid(order_id)
    if not oid:
        raise ValueError("orderId is required")
    qty = max(0, int(completed_qty))
    with _LOCK:
        sheet_rows = load_from_sheet(sheets_service, spreadsheet_id or "")
        rows = dict(sheet_rows) if sheet_rows is not None else _load_from_file()
        rows[oid] = {"completedQty": qty, "updatedAt": _now_iso()}
        _save_to_file(rows)
        save_to_sheet(sheets_service, spreadsheet_id or "", rows)
    return {"orderId": oid, "completedQty": qty, "updatedAt": rows[oid]["updatedAt"]}
