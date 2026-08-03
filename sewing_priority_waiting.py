"""
Shared Sewing Priority "Waiting" tile flags.

Local cache: backend/data/sewing_priority_waiting.json
Durable store: Google Sheet tab "Sewing Waiting" (column A = key)

All logged-in clients read/toggle via API; server emits socket updates.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, List, Optional, Set

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_BASE = Path(__file__).resolve().parent
DATA_DIR = _BASE / "data"
WAITING_PATH = DATA_DIR / "sewing_priority_waiting.json"
SHEET_TAB = os.environ.get("SEWING_WAITING_TAB", "Sewing Waiting")


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_keys_from_file() -> Set[str]:
    if not WAITING_PATH.exists():
        return set()
    try:
        with open(WAITING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            keys = data.get("keys") or []
        elif isinstance(data, list):
            keys = data
        else:
            keys = []
        return {str(k).strip() for k in keys if str(k).strip()}
    except Exception:
        logger.exception("Failed to read sewing priority waiting keys file")
        return set()


def _save_keys_to_file(keys: Set[str]) -> List[str]:
    _ensure_data_dir()
    ordered = sorted(keys)
    payload = {"keys": ordered}
    tmp = WAITING_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp.replace(WAITING_PATH)
    return ordered


def ensure_sheet_tab(sheets_service, spreadsheet_id: str) -> bool:
    """Create Sewing Waiting tab with header if missing."""
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
                body={"values": [["Key"]]},
            ).execute()
            logger.info("Created Google Sheet tab %r", SHEET_TAB)
        return True
    except Exception:
        logger.exception("ensure_sheet_tab(%s) failed", SHEET_TAB)
        return False


def load_keys_from_sheet(sheets_service, spreadsheet_id: str) -> Optional[Set[str]]:
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
            cell = str(row[0] or "").strip()
            if not cell:
                continue
            if i == 0 and cell.lower() == "key":
                continue
            keys.add(cell)
        return keys
    except Exception:
        logger.exception("Failed to read sewing waiting keys from sheet")
        return None


def save_keys_to_sheet(sheets_service, spreadsheet_id: str, keys: Set[str]) -> bool:
    if not sheets_service or not spreadsheet_id:
        return False
    try:
        if not ensure_sheet_tab(sheets_service, spreadsheet_id):
            return False
        ordered = sorted(keys)
        values = [["Key"]] + [[k] for k in ordered]
        # Clear then write so removed keys disappear.
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=f"'{SHEET_TAB}'!A:A",
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
        logger.exception("Failed to write sewing waiting keys to sheet")
        return False


def load_keys(
    sheets_service: Any = None,
    spreadsheet_id: Optional[str] = None,
) -> List[str]:
    """Prefer durable sheet keys; fall back to local file cache."""
    with _LOCK:
        sheet_keys = load_keys_from_sheet(sheets_service, spreadsheet_id or "")
        file_keys = _load_keys_from_file()
        if sheet_keys is not None:
            # Sheet is source of truth; keep file cache aligned.
            if sheet_keys != file_keys:
                _save_keys_to_file(sheet_keys)
            return sorted(sheet_keys)
        return sorted(file_keys)


def toggle_key(
    key: str,
    sheets_service: Any = None,
    spreadsheet_id: Optional[str] = None,
) -> dict:
    """
    Toggle a waiting key. Returns { keys, key, waiting }.
    `waiting` is True if the key is now marked waiting.
    """
    k = str(key or "").strip()
    if not k:
        raise ValueError("key is required")
    with _LOCK:
        sheet_keys = load_keys_from_sheet(sheets_service, spreadsheet_id or "")
        keys = set(sheet_keys) if sheet_keys is not None else _load_keys_from_file()
        if k in keys:
            keys.remove(k)
            waiting = False
        else:
            keys.add(k)
            waiting = True
        ordered = _save_keys_to_file(keys)
        save_keys_to_sheet(sheets_service, spreadsheet_id or "", keys)
    return {"keys": ordered, "key": k, "waiting": waiting}
