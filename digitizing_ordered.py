"""
Shared "Digitize Ordered" flags for the Digitizing tab.

Local cache: backend/data/digitizing_ordered.json
Durable store: Google Sheet tab "Digitizing Ordered"

All logged-in clients read via API; mark happens when the vendor email sends successfully.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_BASE = Path(__file__).resolve().parent
DATA_DIR = _BASE / "data"
ORDERED_PATH = DATA_DIR / "digitizing_ordered.json"
SHEET_TAB = os.environ.get("DIGITIZING_ORDERED_TAB", "Digitizing Ordered")
SHEET_HEADERS = ["Order #", "Ordered At"]


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


def _load_from_file() -> Set[str]:
    if not ORDERED_PATH.exists():
        return set()
    try:
        with open(ORDERED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            keys = data.get("orderNumbers") or data.get("keys") or []
        elif isinstance(data, list):
            keys = data
        else:
            keys = []
        return {_norm_oid(k) for k in keys if _norm_oid(k)}
    except Exception:
        logger.exception("Failed to read digitizing ordered file")
        return set()


def _save_to_file(keys: Set[str]) -> List[str]:
    _ensure_data_dir()
    ordered = sorted(keys)
    payload = {"orderNumbers": ordered}
    tmp = ORDERED_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp.replace(ORDERED_PATH)
    return ordered


def ensure_sheet_tab(sheets_service, spreadsheet_id: str) -> bool:
    """Create Digitizing Ordered tab with headers if missing."""
    if not sheets_service or not spreadsheet_id:
        return False
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
                body={"values": [list(SHEET_HEADERS)]},
            ).execute()
            logger.info("Created Google Sheet tab %r", SHEET_TAB)
        return True
    except Exception:
        logger.exception("ensure_sheet_tab(%s) failed", SHEET_TAB)
        return False


def load_from_sheet(sheets_service, spreadsheet_id: str) -> Optional[Set[str]]:
    if not sheets_service or not spreadsheet_id:
        return None
    try:
        if not ensure_sheet_tab(sheets_service, spreadsheet_id):
            return None
        resp = (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"'{SHEET_TAB}'!A:A")
            .execute()
        )
        values = resp.get("values") or []
        keys: Set[str] = set()
        for i, row in enumerate(values):
            if not row:
                continue
            cell = _norm_oid(row[0])
            if not cell:
                continue
            if i == 0 and cell.lower() in ("order #", "order#", "key", "order"):
                continue
            keys.add(cell)
        return keys
    except Exception:
        logger.exception("Failed to read digitizing ordered from sheet")
        return None


def _append_to_sheet(
    sheets_service,
    spreadsheet_id: str,
    order_numbers: Iterable[str],
    *,
    ordered_at: Optional[str] = None,
) -> bool:
    rows = []
    stamp = ordered_at or _now_iso()
    for oid in order_numbers:
        n = _norm_oid(oid)
        if n:
            rows.append([n, stamp])
    if not rows:
        return True
    try:
        if not ensure_sheet_tab(sheets_service, spreadsheet_id):
            return False
        sheets_service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"'{SHEET_TAB}'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
        return True
    except Exception:
        logger.exception("Failed to append digitizing ordered rows")
        return False


def list_ordered(
    sheets_service: Any = None,
    spreadsheet_id: Optional[str] = None,
) -> List[str]:
    """Prefer durable sheet; fall back to local file cache."""
    with _LOCK:
        sheet_keys = load_from_sheet(sheets_service, spreadsheet_id or "")
        file_keys = _load_from_file()
        if sheet_keys is not None:
            if sheet_keys != file_keys:
                _save_to_file(sheet_keys)
            return sorted(sheet_keys)
        return sorted(file_keys)


def mark_ordered(
    order_numbers: Iterable[Any],
    sheets_service: Any = None,
    spreadsheet_id: Optional[str] = None,
) -> List[str]:
    """
    Mark one or more order numbers as digitize-ordered.
    Returns the full sorted list of ordered ids after update.
    """
    incoming = {_norm_oid(x) for x in (order_numbers or []) if _norm_oid(x)}
    if not incoming:
        return list_ordered(sheets_service, spreadsheet_id)

    with _LOCK:
        sheet_keys = load_from_sheet(sheets_service, spreadsheet_id or "")
        keys = set(sheet_keys) if sheet_keys is not None else _load_from_file()
        new_ids = sorted(incoming - keys)
        if new_ids:
            keys.update(new_ids)
            ordered = _save_to_file(keys)
            if sheets_service and spreadsheet_id:
                _append_to_sheet(sheets_service, spreadsheet_id, new_ids)
            return ordered
        return sorted(keys)
