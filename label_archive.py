"""
Revolving UPS label backups in Google Drive.

Print queue (Label Printer) can be emptied by the shop watcher.
Archive (Label Archive) keeps ups_<tracking>.pdf for UPS_LABEL_ARCHIVE_DAYS
(default 30) and at most UPS_LABEL_ARCHIVE_MAX files (default 400).
Oldest / stale files are trashed after each archive write or reprint.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


DEFAULT_DAYS = 30
DEFAULT_MAX = 400
DEFAULT_FOLDER_NAME = "Label Archive"


def archive_days(raw: Optional[str] = None) -> int:
    text = raw if raw is not None else os.environ.get("UPS_LABEL_ARCHIVE_DAYS")
    try:
        days = int(str(text if text is not None else DEFAULT_DAYS).strip() or DEFAULT_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    return max(1, min(days, 365))


def archive_max(raw: Optional[str] = None) -> int:
    text = raw if raw is not None else os.environ.get("UPS_LABEL_ARCHIVE_MAX")
    try:
        n = int(str(text if text is not None else DEFAULT_MAX).strip() or DEFAULT_MAX)
    except (TypeError, ValueError):
        n = DEFAULT_MAX
    return max(20, min(n, 5000))


def archive_folder_name() -> str:
    name = (os.environ.get("UPS_LABEL_ARCHIVE_FOLDER_NAME") or DEFAULT_FOLDER_NAME).strip()
    return name or DEFAULT_FOLDER_NAME


def parse_drive_time(raw: Any) -> datetime:
    s = str(raw or "").strip()
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def files_to_trash(
    files: List[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    days: Optional[int] = None,
    max_keep: Optional[int] = None,
) -> List[str]:
    """
    Keep the newest files that are still inside the retention window.
    Trash anything older than `days` or past `max_keep`.
    """
    keep_days = archive_days() if days is None else max(1, int(days))
    cap = archive_max() if max_keep is None else max(1, int(max_keep))
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    cutoff = now_dt - timedelta(days=keep_days)

    parsed = []
    for row in files or []:
        fid = str((row or {}).get("id") or "").strip()
        if not fid:
            continue
        dt = parse_drive_time(row.get("modifiedTime") or row.get("createdTime"))
        parsed.append((dt, fid))
    parsed.sort(key=lambda item: item[0], reverse=True)

    trash: List[str] = []
    kept = 0
    for dt, fid in parsed:
        if dt < cutoff or kept >= cap:
            trash.append(fid)
            continue
        kept += 1
    return trash
