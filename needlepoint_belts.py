"""Needlepoint belt orders: size chart, Sheets tab, and supplier email."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import smtplib
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
from typing import Any, Iterable
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SHEET_TAB = "Needlepoint Belts"
BELT_SIZES = tuple(str(n) for n in range(28, 55))
STATUS_PENDING = "Pending"
STATUS_ORDERED = "Ordered"
ORDERS_PARENT_FOLDER_ID = "1n6RX0SumEipD5Nb3pUIgO5OtQFfyQXYz"
DEFAULT_GMAIL_ACCOUNT = "justin.eckard@jrcogolf.com"
TZ = ZoneInfo("America/New_York")

META_HEADERS = (
    "Order #",
    "Submitted At",
    "Company",
    "Design",
    "Product",
    "Due Date",
    "Qty",
)
TRAIL_HEADERS = (
    "Size Summary",
    "Thread Colors",
    "Status",
    "Ordered At",
    "Preview File Id",
    "Notes",
)
SHEET_HEADERS = META_HEADERS + BELT_SIZES + TRAIL_HEADERS

DESIGN_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".pdf",
    ".svg",
    ".ai",
}
SKIP_EXTS = {
    ".emb",
    ".dst",
    ".exp",
    ".pes",
    ".xxx",
    ".ofm",
    ".dxf",
    ".zip",
    ".cdr",
}


def is_needlepoint_product(product: Any) -> bool:
    name = str(product or "").strip().lower().replace(" ", "")
    return "needlepoint" in name


def parse_size_quantities(raw: Any) -> dict[str, int]:
    """Accept a dict, JSON string, or form-style mapping of size -> qty."""
    payload = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {}
            for part in re.split(r"[;,]", text):
                m = re.match(r"\s*(\d+)\s*[:=x×]\s*(\d+)\s*$", part, re.I)
                if m:
                    payload[m.group(1)] = m.group(2)
    if not isinstance(payload, dict):
        return {}
    out = {}
    for size in BELT_SIZES:
        val = payload.get(size, payload.get(int(size) if size.isdigit() else size, 0))
        try:
            qty = int(float(str(val).strip().replace(",", "") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty > 0:
            out[size] = qty
    return out


def total_quantity(sizes: dict[str, int] | None) -> int:
    return sum(int(q or 0) for q in (sizes or {}).values())


def size_summary(sizes: dict[str, int] | None) -> str:
    parts = []
    for size in BELT_SIZES:
        qty = int((sizes or {}).get(size) or 0)
        if qty > 0:
            parts.append(f"{size}×{qty}")
    return ", ".join(parts)


def _format_mdy(dt: datetime) -> str:
    return f"{dt.month}/{dt.day}/{dt.year}"


def _now_str() -> str:
    now = datetime.now(TZ)
    return f"{_format_mdy(now)} {now.strftime('%H:%M:%S')}"


def _norm_status(value: Any) -> str:
    return str(value or "").strip().lower()


def is_pending_status(value: Any) -> bool:
    status = _norm_status(value)
    return status in ("", "pending")


def supplier_email() -> str:
    return (os.environ.get("NEEDLEPOINT_SUPPLIER_EMAIL") or "").strip()


def _header_index(headers: list[str]) -> dict[str, int]:
    return {str(h).strip(): i for i, h in enumerate(headers or [])}


def ensure_sheet(sheets_service, spreadsheet_id: str) -> bool:
    """Create the Needlepoint Belts tab with headers if missing."""
    if not sheets_service or not spreadsheet_id:
        return False
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
            return True
        existing = (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"'{SHEET_TAB}'!A1:CZ1")
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
        return True
    except Exception:
        logger.exception("ensure_sheet(%s) failed", SHEET_TAB)
        return False


def _rewrite_headers_preserving_rows(sheets_service, spreadsheet_id: str, old_headers: list[str]) -> None:
    """Map existing rows onto the current header layout (adds odd sizes 29–53 and 54)."""
    values = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{SHEET_TAB}'!A2:CZ")
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
        range=f"'{SHEET_TAB}'!A1:CZ",
    ).execute()
    body_rows = [list(SHEET_HEADERS)] + mapped
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_TAB}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": body_rows},
    ).execute()
    logger.info("Updated Google Sheet tab %r headers to include sizes 28-54", SHEET_TAB)


def _read_all_rows(sheets_service, spreadsheet_id: str) -> tuple[list[str], list[list[Any]]]:
    if not ensure_sheet(sheets_service, spreadsheet_id):
        return [], []
    values = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{SHEET_TAB}'!A1:CZ")
        .execute()
        .get("values")
        or []
    )
    if not values:
        return list(SHEET_HEADERS), []
    headers = [str(h).strip() for h in values[0]]
    rows = values[1:]
    return headers, rows


def _row_to_dict(headers: list[str], row: list[Any]) -> dict[str, Any]:
    padded = list(row) + [""] * max(0, len(headers) - len(row))
    data = dict(zip(headers, padded))
    sizes = {}
    for size in BELT_SIZES:
        try:
            qty = int(float(str(data.get(size) or 0).replace(",", "") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty > 0:
            sizes[size] = qty
    preview = str(data.get("Preview File Id") or "").strip()
    return {
        "orderNumber": str(data.get("Order #") or "").strip(),
        "submittedAt": str(data.get("Submitted At") or "").strip(),
        "company": str(data.get("Company") or "").strip(),
        "design": str(data.get("Design") or "").strip(),
        "product": str(data.get("Product") or "").strip(),
        "dueDate": str(data.get("Due Date") or "").strip(),
        "qty": total_quantity(sizes) or str(data.get("Qty") or "").strip(),
        "sizes": sizes,
        "sizeSummary": str(data.get("Size Summary") or "").strip() or size_summary(sizes),
        "threadColors": str(data.get("Thread Colors") or "").strip(),
        "status": str(data.get("Status") or STATUS_PENDING).strip() or STATUS_PENDING,
        "orderedAt": str(data.get("Ordered At") or "").strip(),
        "previewFileId": preview,
        "notes": str(data.get("Notes") or "").strip(),
    }


def build_sheet_row(
    *,
    order_number: Any,
    company: str = "",
    design: str = "",
    product: str = "",
    due_date: str = "",
    sizes: dict[str, int] | None = None,
    notes: str = "",
    preview_file_id: str = "",
    submitted_at: str | None = None,
    thread_colors: str = "",
    status: str = STATUS_PENDING,
    ordered_at: str = "",
) -> list[Any]:
    sizes = parse_size_quantities(sizes or {})
    row = [
        order_number,
        submitted_at or _now_str(),
        company,
        design,
        product,
        due_date,
        total_quantity(sizes),
    ]
    for size in BELT_SIZES:
        qty = sizes.get(size)
        row.append(qty if qty else "")
    row.extend(
        [
            size_summary(sizes),
            thread_colors,
            status or STATUS_PENDING,
            ordered_at,
            preview_file_id or "",
            notes or "",
        ]
    )
    return row


def append_order(sheets_service, spreadsheet_id: str, **kwargs) -> bool:
    if not ensure_sheet(sheets_service, spreadsheet_id):
        return False
    row = build_sheet_row(**kwargs)
    sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_TAB}'!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    return True


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


def get_order(sheets_service, spreadsheet_id: str, order_number: Any) -> dict[str, Any] | None:
    target = str(order_number or "").strip()
    if not target:
        return None
    headers, rows = _read_all_rows(sheets_service, spreadsheet_id)
    for row in rows:
        item = _row_to_dict(headers, row)
        if str(item.get("orderNumber") or "").strip() == target:
            return item
    return None


def mark_orders_ordered(
    sheets_service,
    spreadsheet_id: str,
    updates: Iterable[dict[str, Any]],
) -> int:
    """updates: [{orderNumber, threadColors}]"""
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
    thread_i = hix.get("Thread Colors")
    if status_i is None:
        return 0
    now = _now_str()
    changed = 0
    for idx, row in enumerate(rows):
        padded = list(row) + [""] * max(0, len(headers) - len(row))
        order_num = str(padded[hix.get("Order #", 0)] if hix.get("Order #") is not None else "").strip()
        upd = by_order.get(order_num)
        if not upd:
            continue
        if thread_i is not None:
            colors = str(upd.get("threadColors") or upd.get("thread_colors") or "").strip()
            if colors:
                padded[thread_i] = colors
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


def _col_letter(n: int) -> str:
    """1-based column count -> last column letter (A, B, ... AZ)."""
    letter = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letter = chr(65 + rem) + letter
    return letter or "A"


def build_email_text(
    orders: list[dict[str, Any]],
    *,
    extra_notes: str = "",
) -> tuple[str, str]:
    today = datetime.now(TZ)
    date_label = _format_mdy(today)
    subject = f"JR & Co. Needlepoint Belt Order — {date_label}"
    lines = [
        "Hello,",
        "",
        "Please make the following needlepoint belts:",
        "",
    ]
    for item in orders or []:
        order_num = str(item.get("orderNumber") or item.get("order") or "").strip()
        design = str(item.get("design") or "").strip()
        company = str(item.get("company") or "").strip()
        sizes = item.get("sizes") if isinstance(item.get("sizes"), dict) else parse_size_quantities(item.get("sizes"))
        summary = str(item.get("sizeSummary") or "").strip() or size_summary(sizes)
        colors = str(item.get("threadColors") or item.get("thread_colors") or "").strip()
        title = f"Order {order_num}"
        extras = [x for x in (company, design) if x]
        if extras:
            title += " — " + " / ".join(extras)
        lines.append(title)
        if summary:
            lines.append(f"  Sizes: {summary}")
        qty = item.get("qty") or total_quantity(sizes)
        if qty:
            lines.append(f"  Total qty: {qty}")
        if colors:
            lines.append(f"  Thread colors: {colors}")
        lines.append("")
    if extra_notes.strip():
        lines.extend(["Notes:", extra_notes.strip(), ""])
    lines.extend(
        [
            "Design files are attached, named by order number.",
            "",
            "Thank you,",
            "Justin Eckard",
            "JR & Co.",
            "678.294.5350",
        ]
    )
    return subject, "\n".join(lines)


def attachment_filename(order_number: Any, original_name: str, index: int = 1, total: int = 1) -> str:
    order = str(order_number or "").strip() or "design"
    ext = ""
    name = str(original_name or "")
    if "." in name:
        ext = "." + name.rsplit(".", 1)[-1].lower()
    if ext not in DESIGN_EXTS and ext != ".pdf":
        ext = ext if ext and len(ext) <= 5 else ".jpg"
    if total <= 1 and index <= 1:
        return f"{order}{ext}"
    return f"{order}-{index}{ext}"


def _drive_escape(name: str) -> str:
    return str(name or "").replace("\\", "\\\\").replace("'", "\\'")


def find_order_folder_id(drive, order_number: Any, parent_id: str | None = None) -> str | None:
    parent = parent_id or os.environ.get("ORDERS_PARENT_FOLDER_ID") or ORDERS_PARENT_FOLDER_ID
    safe = _drive_escape(str(order_number).strip())
    if not safe:
        return None
    query = (
        f"name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false and '{parent}' in parents"
    )
    files = (
        drive.files()
        .list(q=query, fields="files(id, name)", pageSize=5, supportsAllDrives=True, includeItemsFromAllDrives=True)
        .execute()
        .get("files")
        or []
    )
    return files[0]["id"] if files else None


def _is_design_file(file_meta: dict) -> bool:
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


def list_design_files(drive, folder_id: str) -> list[dict[str, str]]:
    if not folder_id:
        return []
    files = []
    page_token = None
    while True:
        res = (
            drive.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType)",
                pageSize=100,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files.extend(res.get("files") or [])
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    designs = [f for f in files if _is_design_file(f)]
    designs.sort(key=lambda f: str(f.get("name") or "").lower())
    return designs


def download_drive_bytes(drive, file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    request_drive = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = BytesIO()
    downloader = MediaIoBaseDownload(buf, request_drive)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def collect_renamed_attachments(drive, order_number: Any) -> list[dict[str, Any]]:
    folder_id = find_order_folder_id(drive, order_number)
    if not folder_id:
        logger.warning("[Needlepoint] order folder not found for %s", order_number)
        return []
    designs = list_design_files(drive, folder_id)
    if not designs:
        logger.warning("[Needlepoint] no design files in folder for %s", order_number)
        return []
    out = []
    total = len(designs)
    for i, meta in enumerate(designs, start=1):
        try:
            data = download_drive_bytes(drive, meta["id"])
        except Exception:
            logger.exception("[Needlepoint] failed to download %s for order %s", meta.get("id"), order_number)
            continue
        filename = attachment_filename(order_number, meta.get("name") or "", i, total)
        mime = meta.get("mimeType") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        out.append(
            {
                "filename": filename,
                "bytes": data,
                "mime": mime,
                "sourceId": meta.get("id"),
                "sourceName": meta.get("name"),
            }
        )
    return out


def _mime_message(to_email: str, subject: str, body: str, attachments: list[dict[str, Any]], from_email: str = "") -> MIMEMultipart:
    msg = MIMEMultipart()
    if from_email:
        msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    for att in attachments or []:
        data = att.get("bytes") or b""
        filename = att.get("filename") or "design.jpg"
        mime = str(att.get("mime") or "application/octet-stream")
        main, _, sub = mime.partition("/")
        part = MIMEBase(main or "application", sub or "octet-stream")
        part.set_payload(data)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)
    return msg


def create_gmail_draft(
    creds,
    *,
    to_email: str,
    subject: str,
    body: str,
    attachments: list[dict[str, Any]],
    from_email: str = "",
) -> dict[str, Any] | None:
    from googleapiclient.discovery import build

    msg = _mime_message(to_email, subject, body, attachments, from_email=from_email)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    created = (
        gmail.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": raw}})
        .execute()
    )
    draft_id = created.get("id") or ""
    message_id = (created.get("message") or {}).get("id") or ""
    account = (os.environ.get("NEEDLEPOINT_GMAIL_ACCOUNT") or DEFAULT_GMAIL_ACCOUNT).strip()
    params = {"authuser": account}
    compose = message_id or draft_id
    draft_url = (
        f"https://mail.google.com/mail/u/?{urlencode(params)}#drafts?compose={quote(str(compose))}"
        if compose
        else ""
    )
    return {"draftId": draft_id, "messageId": message_id, "draftUrl": draft_url}


def send_smtp_email(
    *,
    to_email: str,
    subject: str,
    body: str,
    attachments: list[dict[str, Any]],
) -> bool:
    from_email = (
        os.environ.get("DESIGN_CONFIRMATION_FROM_EMAIL") or os.environ.get("SMTP_USER") or "info@jrco.us"
    ).strip()
    smtp_host = (os.environ.get("SMTP_HOST") or "").strip()
    smtp_port = int(os.environ.get("SMTP_PORT") or "587")
    smtp_user = (os.environ.get("SMTP_USER") or "").strip()
    smtp_password = (os.environ.get("SMTP_PASSWORD") or "").strip()
    if not to_email or not smtp_host or not smtp_user or not smtp_password:
        logger.warning("[Needlepoint] SMTP not configured; email not sent")
        return False
    msg = _mime_message(to_email, subject, body, attachments, from_email=from_email)
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(from_email, [to_email], msg.as_string())
    logger.info("[Needlepoint] SMTP belt order sent to %s", to_email)
    return True


def gmail_compose_url(to_email: str, subject: str, body: str) -> str:
    account = (os.environ.get("NEEDLEPOINT_GMAIL_ACCOUNT") or DEFAULT_GMAIL_ACCOUNT).strip()
    p = {
        "view": "cm",
        "fs": "1",
        "to": to_email or "",
        "su": subject or "",
        "body": body or "",
        "authuser": account,
    }
    return f"https://mail.google.com/mail/u/?{urlencode(p)}"


def deliver_belt_order_email(
    *,
    creds,
    to_email: str,
    subject: str,
    body: str,
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create a Gmail draft with attachments; fall back to SMTP, then compose URL."""
    result = {
        "ok": False,
        "method": "",
        "draftUrl": "",
        "composeUrl": gmail_compose_url(to_email, subject, body),
        "sent": False,
        "error": "",
        "attachmentCount": len(attachments or []),
        "attachmentNames": [a.get("filename") for a in (attachments or [])],
    }
    try:
        draft = create_gmail_draft(
            creds,
            to_email=to_email,
            subject=subject,
            body=body,
            attachments=attachments,
        )
        if draft and (draft.get("draftUrl") or draft.get("draftId")):
            result.update(
                {
                    "ok": True,
                    "method": "gmail_draft",
                    "draftUrl": draft.get("draftUrl") or "",
                    "draftId": draft.get("draftId") or "",
                }
            )
            return result
    except Exception as exc:
        logger.warning("[Needlepoint] Gmail draft failed: %s", exc)
        result["error"] = str(exc)

    try:
        if send_smtp_email(
            to_email=to_email,
            subject=subject,
            body=body,
            attachments=attachments,
        ):
            result.update({"ok": True, "method": "smtp", "sent": True, "error": ""})
            return result
    except Exception as exc:
        logger.warning("[Needlepoint] SMTP send failed: %s", exc)
        result["error"] = str(exc)

    extra_lines = []
    for att in attachments or []:
        fid = att.get("sourceId")
        fname = att.get("filename")
        if fid and fname:
            extra_lines.append(f"{fname}: https://drive.google.com/file/d/{fid}/view")
    fallback_body = body
    if extra_lines:
        fallback_body = body + "\n\nDesign files:\n" + "\n".join(extra_lines)
    result["composeUrl"] = gmail_compose_url(to_email, subject, fallback_body)
    result["method"] = "compose_url"
    result["ok"] = True
    return result
