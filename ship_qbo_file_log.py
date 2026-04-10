"""
Append-only file logs for UPS labels + QuickBooks during /api/process-shipment.

Logs live under the repo ``logs/`` folder (next to ``backend/``):
  - ship_quickbooks.log  — combined timeline (SHIP / LABEL / QBO / CLIENT)
  - ship_labels.log      — label PDF selection / serve events only
  - quickbooks.log       — invoice Id, DocNumber, browser URL only

Disable with env: SHIP_QBO_FILE_LOG=0
"""
from __future__ import annotations

import json
import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()
_REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = _REPO_ROOT / "logs"
COMBINED = LOG_DIR / "ship_quickbooks.log"
LABELS_ONLY = LOG_DIR / "ship_labels.log"
QBO_ONLY = LOG_DIR / "quickbooks.log"


def _enabled() -> bool:
    return (os.getenv("SHIP_QBO_FILE_LOG") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _write(path: Path, line: str) -> None:
    if not _enabled():
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(line)
                if not line.endswith("\n"):
                    f.write("\n")
    except OSError:
        pass


def log_line(category: str, message: str, **extra) -> None:
    """One human-readable line + optional JSON payload on the next line."""
    cat = (category or "EVENT").strip().upper()[:24]
    row = {"ts": _ts(), "category": cat, "message": message}
    if extra:
        row["data"] = extra
    try:
        payload = json.dumps(row, ensure_ascii=False, default=str)
    except TypeError:
        row["data"] = {k: str(v) for k, v in extra.items()}
        payload = json.dumps(row, ensure_ascii=False)
    _write(COMBINED, payload)


def log_label(message: str, **extra) -> None:
    log_line("LABEL", message, **extra)
    try:
        row = {"ts": _ts(), "message": message, **extra}
        _write(LABELS_ONLY, json.dumps(row, ensure_ascii=False, default=str))
    except TypeError:
        log_line("LABEL", message + " (extra not JSON-serializable)")


def log_qbo(message: str, **extra) -> None:
    log_line("QBO", message, **extra)
    try:
        row = {"ts": _ts(), "message": message, **extra}
        _write(QBO_ONLY, json.dumps(row, ensure_ascii=False, default=str))
    except TypeError:
        log_line("QBO", message + " (extra not JSON-serializable)")


def log_ship(message: str, **extra) -> None:
    log_line("SHIP", message, **extra)


def log_client(message: str, **extra) -> None:
    log_line("CLIENT", message, **extra)


def log_exception(category: str, where: str, exc: BaseException) -> None:
    log_line(
        category,
        f"exception in {where}: {exc!s}",
        traceback=traceback.format_exc(),
    )
