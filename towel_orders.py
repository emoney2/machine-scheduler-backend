"""Towel supplier orders: pending sheet + Tempy Stees email."""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from needlepoint_belts import (
    DESIGN_EXTS,
    SKIP_EXTS,
    attachment_filename,
    deliver_belt_order_email,
    download_drive_bytes,
    find_order_folder_id,
    is_pending_status,
    list_design_files,
)

logger = logging.getLogger(__name__)

SHEET_TAB = "Towel Orders"
STATUS_PENDING = "Pending"
STATUS_ORDERED = "Ordered"
DEFAULT_SUPPLIER_EMAIL = "adam@tempystees.com"
DEFAULT_FROM_EMAIL = "justin.eckard@jrcogolf.com"
TZ = ZoneInfo("America/New_York")

TOWEL_EMAIL_SIGNATURE = (
    "Thanks,\n"
    "Justin Eckard\n"
    "JR & Co.\n"
    "678.294.5350\n"
    "www.jrcogolf.com"
)

SHEET_HEADERS = (
    "Order #",
    "Submitted At",
    "Company",
    "Design",
    "Product",
    "Due Date",
    "Qty",
    "Status",
    "Ordered At",
    "Preview File Id",
    "Notes",
)

_sheet_ready_until = 0.0
_sheet_backoff_until = 0.0
_SHEET_READY_TTL = 600
_SHEET_BACKOFF_SEC = 60


def is_towel_product(product: Any) -> bool:
    return "towel" in str(product or "").strip().lower()


def supplier_email() -> str:
    env = (os.environ.get("TOWEL_SUPPLIER_EMAIL") or "").strip()
    if env:
        return env
    return DEFAULT_SUPPLIER_EMAIL


def from_email() -> str:
    env = (os.environ.get("TOWEL_FROM_EMAIL") or "").strip()
    if env:
        return env
    return DEFAULT_FROM_EMAIL


def sheet_is_ready() -> bool:
    return time.time() < _sheet_ready_until


def _format_mdy(dt: datetime) -> str:
    return f"{dt.month}/{dt.day}/{dt.year}"


def _now_str() -> str:
    now = datetime.now(TZ)
    return f"{_format_mdy(now)} {now.strftime('%H:%M:%S')}"


def _header_index(headers: list[str]) -> dict[str, int]:
    return {str(h).strip(): i for i, h in enumerate(headers or [])}


def _col_letter(n: int) -> str:
    letter = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letter = chr(65 + rem) + letter
    return letter or "A"


def ensure_sheet(sheets_service, spreadsheet_id: str) -> bool:
    """Create the Towel Orders tab with headers if missing."""
    global _sheet_ready_until, _sheet_backoff_until
    if not sheets_service or not spreadsheet_id:
        return False
    now = time.time()
    if now < _sheet_backoff_until:
        return False
    if now < _sheet_ready_until:
        return True
    try:
        meta = (
            sheets_service.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
            .execute()
        )
        titles = {
            (s.get("properties") or {}).get("title") for s in (meta.get("sheets") or [])
        }
        if SHEET_TAB not in titles:
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": SHEET_TAB}}}]},
            ).execute()
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{SHEET_TAB}'!A1",
                valueInputOption="RAW",
                body={"values": [list(SHEET_HEADERS)]},
            ).execute()
            logger.info("Created Google Sheet tab %r", SHEET_TAB)
            _sheet_ready_until = time.time() + _SHEET_READY_TTL
            return True
        existing = (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"'{SHEET_TAB}'!A1:L1")
            .execute()
            .get("values")
            or []
        )
        old_headers = [str(h).strip() for h in (existing[0] if existing else [])]
        if not old_headers:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{SHEET_TAB}'!A1",
                valueInputOption="RAW",
                body={"values": [list(SHEET_HEADERS)]},
            ).execute()
        elif old_headers != list(SHEET_HEADERS):
            _rewrite_headers_preserving_rows(sheets_service, spreadsheet_id, old_headers)
        _sheet_ready_until = time.time() + _SHEET_READY_TTL
        return True
    except Exception as exc:
        _sheet_ready_until = 0.0
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == 429:
            _sheet_backoff_until = time.time() + _SHEET_BACKOFF_SEC
            logger.warning(
                "ensure_sheet(%s) skipped: Sheets read quota exceeded", SHEET_TAB
            )
        else:
            logger.exception("ensure_sheet(%s) failed: %s", SHEET_TAB, exc)
        return False


def _rewrite_headers_preserving_rows(
    sheets_service, spreadsheet_id: str, old_headers: list[str]
) -> None:
    values = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{SHEET_TAB}'!A2:L")
        .execute()
        .get("values")
        or []
    )
    mapped = []
    for row in values:
        padded = list(row) + [""] * max(0, len(old_headers) - len(row))
        data = dict(zip(old_headers, padded))
        mapped.append([data.get(h, "") for h in SHEET_HEADERS])
    sheets_service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_TAB}'!A1:L",
    ).execute()
    body_rows = [list(SHEET_HEADERS)] + mapped
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_TAB}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": body_rows},
    ).execute()
    logger.info("Updated Google Sheet tab %r headers", SHEET_TAB)


def _read_all_rows(
    sheets_service, spreadsheet_id: str
) -> tuple[list[str], list[list[Any]]]:
    if not ensure_sheet(sheets_service, spreadsheet_id):
        return [], []
    values = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{SHEET_TAB}'!A1:L")
        .execute()
        .get("values")
        or []
    )
    if not values:
        return [], []
    headers = [str(h).strip() for h in values[0]]
    return headers, values[1:]


def _row_to_dict(headers: list[str], row: list[Any]) -> dict[str, Any]:
    padded = list(row) + [""] * max(0, len(headers) - len(row))
    data = dict(zip(headers, padded))
    qty_raw = str(data.get("Qty") or "").strip()
    try:
        qty = int(float(qty_raw.replace(",", "") or 0)) if qty_raw else 0
    except (TypeError, ValueError):
        qty = 0
    return {
        "orderNumber": str(data.get("Order #") or "").strip(),
        "submittedAt": str(data.get("Submitted At") or "").strip(),
        "company": str(data.get("Company") or "").strip(),
        "design": str(data.get("Design") or "").strip(),
        "product": str(data.get("Product") or "").strip(),
        "dueDate": str(data.get("Due Date") or "").strip(),
        "qty": qty,
        "status": str(data.get("Status") or STATUS_PENDING).strip() or STATUS_PENDING,
        "orderedAt": str(data.get("Ordered At") or "").strip(),
        "previewFileId": str(data.get("Preview File Id") or "").strip(),
        "notes": str(data.get("Notes") or "").strip(),
    }


def build_sheet_row(
    *,
    order_number: Any,
    company: str = "",
    design: str = "",
    product: str = "",
    due_date: str = "",
    qty: Any = "",
    notes: str = "",
    preview_file_id: str = "",
    submitted_at: str | None = None,
    status: str = STATUS_PENDING,
    ordered_at: str = "",
) -> list[Any]:
    try:
        qty_n = int(float(str(qty or 0).replace(",", "") or 0))
    except (TypeError, ValueError):
        qty_n = 0
    return [
        order_number,
        submitted_at or _now_str(),
        company,
        design,
        product,
        due_date,
        qty_n if qty_n else "",
        status or STATUS_PENDING,
        ordered_at,
        preview_file_id or "",
        notes or "",
    ]


def append_order(sheets_service, spreadsheet_id: str, **kwargs) -> bool:
    if not ensure_sheet(sheets_service, spreadsheet_id):
        return False
    order_number = str(kwargs.get("order_number") or "").strip()
    if not order_number:
        return False
    existing = get_order(sheets_service, spreadsheet_id, order_number)
    if existing:
        update_kwargs = {k: v for k, v in kwargs.items() if k != "order_number"}
        return update_order(
            sheets_service, spreadsheet_id, order_number, **update_kwargs
        )
    row = build_sheet_row(**kwargs)
    sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_TAB}'!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    return True


def get_order(
    sheets_service, spreadsheet_id: str, order_number: Any
) -> dict[str, Any] | None:
    target = str(order_number or "").strip()
    if not target:
        return None
    headers, rows = _read_all_rows(sheets_service, spreadsheet_id)
    for row in rows:
        item = _row_to_dict(headers, row)
        if str(item.get("orderNumber") or "").strip() == target:
            return item
    return None


def update_order(
    sheets_service, spreadsheet_id: str, order_number: Any, **kwargs
) -> bool:
    target = str(order_number or "").strip()
    if not target:
        return False
    if not ensure_sheet(sheets_service, spreadsheet_id):
        return False
    headers, rows = _read_all_rows(sheets_service, spreadsheet_id)
    if not headers:
        return False
    hix = _header_index(headers)
    order_i = hix.get("Order #")
    if order_i is None:
        return False
    for idx, row in enumerate(rows):
        padded = list(row) + [""] * max(0, len(headers) - len(row))
        if str(padded[order_i] or "").strip() != target:
            continue
        existing = _row_to_dict(headers, padded)
        kwargs.setdefault("status", existing.get("status") or STATUS_PENDING)
        kwargs.setdefault("ordered_at", existing.get("orderedAt") or "")
        kwargs.setdefault("submitted_at", existing.get("submittedAt") or _now_str())
        if not kwargs.get("preview_file_id"):
            kwargs["preview_file_id"] = existing.get("previewFileId") or ""
        new_row = build_sheet_row(order_number=target, **kwargs)
        if len(new_row) < len(headers):
            new_row = new_row + [""] * (len(headers) - len(new_row))
        end_col = _col_letter(len(headers))
        sheet_row = idx + 2
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{SHEET_TAB}'!A{sheet_row}:{end_col}{sheet_row}",
            valueInputOption="USER_ENTERED",
            body={"values": [new_row[: len(headers)]]},
        ).execute()
        return True
    return False


def list_pending_orders(sheets_service, spreadsheet_id: str) -> list[dict[str, Any]]:
    headers, rows = _read_all_rows(sheets_service, spreadsheet_id)
    if not headers:
        return []
    out = []
    for row in rows:
        item = _row_to_dict(headers, row)
        if not item["orderNumber"]:
            continue
        if is_pending_status(item.get("status")):
            out.append(item)
    return out


def known_order_numbers(sheets_service, spreadsheet_id: str) -> set[str]:
    headers, rows = _read_all_rows(sheets_service, spreadsheet_id)
    if not headers:
        return set()
    out = set()
    for row in rows:
        item = _row_to_dict(headers, row)
        oid = str(item.get("orderNumber") or "").strip()
        if oid:
            out.add(oid)
    return out


def _extract_drive_id(value: Any) -> str:
    s = str(value or "")
    m = re.search(r"/file/d/([A-Za-z0-9_-]{10,})", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{12,}", s.strip()):
        return s.strip()
    return ""


def sync_open_towels_from_production(
    sheets_service,
    spreadsheet_id: str,
    production_rows: Iterable[dict[str, Any]] | None,
) -> int:
    """
    Ensure open Production Orders towels appear on the Towel Orders pending list.
    Skips already-tracked order numbers (pending or ordered).
    """
    if not production_rows:
        return 0
    if not ensure_sheet(sheets_service, spreadsheet_id):
        return 0
    known = known_order_numbers(sheets_service, spreadsheet_id)
    added = 0
    for row in production_rows:
        if not isinstance(row, dict):
            continue
        product = row.get("Product") or row.get("product") or ""
        if not is_towel_product(product):
            continue
        status = str(row.get("Status") or "").strip().upper()
        stage = str(row.get("Stage") or "").strip().upper()
        if status in ("COMPLETE", "COMPLETED") or stage in ("COMPLETE", "COMPLETED"):
            continue
        order_number = str(row.get("Order #") or row.get("orderNumber") or "").strip()
        if not order_number or order_number in known:
            continue
        preview = _extract_drive_id(
            row.get("Image") or row.get("Preview") or row.get("preview") or ""
        )
        try:
            qty = int(float(str(row.get("Quantity") or 0).replace(",", "") or 0))
        except (TypeError, ValueError):
            qty = 0
        ok = append_order(
            sheets_service,
            spreadsheet_id,
            order_number=order_number,
            company=str(row.get("Company Name") or row.get("Company") or "").strip(),
            design=str(row.get("Design") or "").strip(),
            product=str(product).strip(),
            due_date=str(row.get("Due Date") or "").strip(),
            qty=qty,
            notes="",
            preview_file_id=preview,
            status=STATUS_PENDING,
        )
        if ok:
            known.add(order_number)
            added += 1
    return added


def mark_orders_ordered(
    sheets_service,
    spreadsheet_id: str,
    updates: Iterable[dict[str, Any]],
) -> int:
    by_order = {
        str(u.get("orderNumber") or u.get("order") or "").strip(): u
        for u in (updates or [])
        if str(u.get("orderNumber") or u.get("order") or "").strip()
    }
    if not by_order:
        return 0
    headers, rows = _read_all_rows(sheets_service, spreadsheet_id)
    if not headers:
        return 0
    hix = _header_index(headers)
    status_i = hix.get("Status")
    ordered_i = hix.get("Ordered At")
    if status_i is None:
        return 0
    now = _now_str()
    changed = 0
    for idx, row in enumerate(rows):
        padded = list(row) + [""] * max(0, len(headers) - len(row))
        order_num = str(
            padded[hix.get("Order #", 0)] if hix.get("Order #") is not None else ""
        ).strip()
        if order_num not in by_order:
            continue
        padded[status_i] = STATUS_ORDERED
        if ordered_i is not None:
            padded[ordered_i] = now
        sheet_row = idx + 2
        end_col = _col_letter(len(headers))
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{SHEET_TAB}'!A{sheet_row}:{end_col}{sheet_row}",
            valueInputOption="USER_ENTERED",
            body={"values": [padded[: len(headers)]]},
        ).execute()
        changed += 1
    return changed


def build_email_text(
    orders: list[dict[str, Any]],
    *,
    extra_notes: str = "",
) -> tuple[str, str]:
    orders = list(orders or [])
    parts = ["JR & Co Towel Order"]
    if len(orders) == 1:
        order_num = str(orders[0].get("orderNumber") or orders[0].get("order") or "").strip()
        design = str(orders[0].get("design") or "").strip()
        if order_num:
            parts.append(f"#{order_num}")
        if design:
            parts.append(design)
    else:
        today = datetime.now(TZ)
        parts.append(_format_mdy(today))
        if orders:
            parts.append(f"{len(orders)} towels")
    subject = " - ".join(parts)

    lines = [
        "Hi Adam,",
        "",
        "Could you please make the following towels for us?",
        "",
    ]
    for item in orders:
        order_num = str(item.get("orderNumber") or item.get("order") or "").strip()
        design = str(item.get("design") or "").strip()
        company = str(item.get("company") or "").strip()
        product = str(item.get("product") or "").strip()
        title = f"Order {order_num}" if order_num else "Order"
        extras = [x for x in (company, design) if x]
        if extras:
            title += " — " + " / ".join(extras)
        lines.append(title)
        if product:
            lines.append(f"  Product: {product}")
        qty = item.get("qty")
        try:
            qty_n = int(float(str(qty or 0).replace(",", "") or 0))
        except (TypeError, ValueError):
            qty_n = 0
        if qty_n:
            lines.append(f"  Qty: {qty_n}")
        lines.append("")
    if extra_notes.strip():
        lines.extend(["Notes:", extra_notes.strip(), ""])
    lines.append("Design files are attached, named by order number.")
    lines.append("")
    lines.append(TOWEL_EMAIL_SIGNATURE)
    return subject, "\n".join(lines)


def _is_towel_design_file(file_meta: dict) -> bool:
    name = str(file_meta.get("name") or "")
    mime = str(file_meta.get("mimeType") or "")
    if mime == "application/vnd.google-apps.folder":
        return False
    lower = name.lower()
    ext = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
    if ext in SKIP_EXTS:
        return False
    if mime.startswith("image/") or mime == "application/pdf":
        return True
    return ext in DESIGN_EXTS


def attachment_from_file_id(
    drive, file_id: Any, order_number: Any = ""
) -> dict[str, Any] | None:
    """Download one Drive design file and rename it to the order number."""
    fid = str(file_id or "").strip()
    if not fid:
        return None
    try:
        meta = (
            drive.files()
            .get(
                fileId=fid,
                fields="id,name,mimeType",
                supportsAllDrives=True,
            )
            .execute()
        )
    except Exception:
        logger.exception("[Towels] failed to load Drive metadata for %s", fid)
        meta = {}
    try:
        data = download_drive_bytes(drive, fid)
    except Exception:
        logger.exception("[Towels] failed to download Drive file %s", fid)
        return None
    if not data:
        return None
    name = str(meta.get("name") or "").strip() or "design.jpg"
    mime = str(meta.get("mimeType") or "").strip() or "application/octet-stream"
    filename = attachment_filename(order_number, name, 1, 1)
    if mime.startswith("application/octet-stream"):
        lower = filename.lower()
        if lower.endswith(".png"):
            mime = "image/png"
        elif lower.endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif lower.endswith(".pdf"):
            mime = "application/pdf"
    return {
        "filename": filename,
        "bytes": data,
        "mime": mime,
        "sourceId": fid,
        "sourceName": name,
    }


def collect_renamed_attachments(
    drive, order_number: Any, preview_file_id: Any = ""
) -> list[dict[str, Any]]:
    """
    Attach design images/PDFs for one towel order.

    Prefers previewFileId from the Towel Orders row, then files in the order folder.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    preview = attachment_from_file_id(drive, preview_file_id, order_number)
    if preview:
        out.append(preview)
        seen.add(str(preview.get("sourceId") or ""))

    folder_id = find_order_folder_id(drive, order_number)
    if not folder_id:
        if not out:
            logger.warning("[Towels] order folder not found for %s", order_number)
        return out

    designs = [f for f in list_design_files(drive, folder_id) if _is_towel_design_file(f)]
    designs.sort(
        key=lambda f: (
            0 if str(f.get("mimeType") or "").startswith("image/") else 1,
            str(f.get("name") or "").lower(),
        )
    )
    # If we already have a preview image, keep at most one extra PDF/image from folder.
    for i, meta in enumerate(designs, start=1):
        fid = str(meta.get("id") or "")
        if not fid or fid in seen:
            continue
        try:
            data = download_drive_bytes(drive, fid)
        except Exception:
            logger.exception(
                "[Towels] failed to download %s for order %s",
                fid,
                order_number,
            )
            continue
        if not data:
            continue
        filename = attachment_filename(
            order_number, meta.get("name") or "design.jpg", len(out) + 1, max(1, len(designs))
        )
        mime = str(meta.get("mimeType") or "application/octet-stream")
        if str(meta.get("name") or "").lower().endswith(".pdf"):
            mime = "application/pdf"
        out.append(
            {
                "filename": filename,
                "bytes": data,
                "mime": mime,
                "sourceId": fid,
                "sourceName": meta.get("name"),
            }
        )
        seen.add(fid)
        # One solid design file is enough for the vendor email.
        if len(out) >= 1:
            break
    if not out:
        logger.warning("[Towels] no design file for order %s", order_number)
    return out
