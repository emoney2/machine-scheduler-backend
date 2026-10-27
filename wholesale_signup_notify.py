"""Email when a wholesale application is appended to the Directory sheet.

The public form writes the row from Google Apps Script, not from this API.
A single background pass looks for rows submitted on or after the cutoff
that have not been marked Signup Notified, then emails who signed up.
"""

from __future__ import annotations

import html
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import directory_customer as directory_customer_mod

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
# Ignore applications already in the sheet before this notifier existed.
SIGNUP_NOTIFY_FROM = date(2026, 10, 27)
NOTIFY_HEADER = "Signup Notified"
SHEET_RANGE = "Directory!A1:ZZ10000"
MAX_PER_PASS = 15
_SHEETS_EPOCH = datetime(1899, 12, 30)

_started = False
_start_lock = threading.Lock()
_emailed_keys: set[str] = set()

_DATE_FORMATS = (
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%m/%d/%y %H:%M:%S",
    "%m/%d/%y %H:%M",
    "%m/%d/%y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def notify_email() -> str:
    return (
        os.environ.get("WHOLESALE_SIGNUP_NOTIFY_EMAIL")
        or "justin.eckard@jrcogolf.com"
    ).strip()


def notifier_enabled() -> bool:
    flag = (os.environ.get("WHOLESALE_SIGNUP_NOTIFY") or "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def as_text(value) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def parse_submitted(value):
    """Return the calendar date of an Application Submitted cell, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        serial = float(value)
        if serial < 1 or serial > 200000:
            return None
        return (_SHEETS_EPOCH + timedelta(days=serial)).date()
    text = str(value).strip()
    if not text:
        return None
    try:
        serial = float(text)
    except ValueError:
        serial = None
    else:
        if 1 <= serial <= 200000:
            return (_SHEETS_EPOCH + timedelta(days=serial)).date()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def already_notified(value) -> bool:
    if value is True:
        return True
    return as_text(value).lower() in {"yes", "y", "true", "1", "sent"}


def _row_dict(headers, row) -> dict:
    out = {}
    for i, header in enumerate(headers):
        key = str(header or "").strip()
        if not key:
            continue
        out[key] = row[i] if i < len(row) else ""
    return out


def _notify_column(headers):
    """Return (index, headers, needs_header_write)."""
    for i, raw in enumerate(headers):
        if str(raw or "").strip().lower() == NOTIFY_HEADER.lower():
            return i, list(headers), False
    updated = list(headers)
    for i, raw in enumerate(updated):
        if not str(raw or "").strip():
            updated[i] = NOTIFY_HEADER
            return i, updated, True
    updated.append(NOTIFY_HEADER)
    return len(updated) - 1, updated, True


def plan_signups(values, today=None):
    """
    Find Directory rows that still need a signup email.

    Returns (notify_col, header_needs_write, notices) where each notice is
    {sheet_row, row, key}.
    """
    if today is None:
        today = datetime.now(ET).date()
    if not values:
        return None, False, []
    headers = list(values[0] or [])
    notify_col, _headers, header_needs_write = _notify_column(headers)
    latest = today + timedelta(days=1)
    notices = []
    for sheet_row, raw in enumerate(values[1:], start=2):
        row = _row_dict(headers, raw or [])
        submitted = parse_submitted(row.get("Application Submitted"))
        if submitted is None or submitted < SIGNUP_NOTIFY_FROM or submitted > latest:
            continue
        if not header_needs_write and already_notified(row.get(NOTIFY_HEADER)):
            continue
        company = as_text(row.get("Company Name"))
        contact = " ".join(
            part
            for part in (
                as_text(row.get("Contact First Name")),
                as_text(row.get("Contact Last Name")),
            )
            if part
        )
        if not company and not contact:
            continue
        notices.append(
            {
                "sheet_row": sheet_row,
                "row": row,
                "key": f"{sheet_row}|{company.casefold()}|{submitted.isoformat()}",
            }
        )
    return notify_col, header_needs_write, notices


def _address_line(row) -> str:
    street = ", ".join(
        part
        for part in (
            as_text(row.get("Street Address 1")),
            as_text(row.get("Street Address 2")),
            as_text(row.get("Shipping Address 3")),
        )
        if part
    )
    city = as_text(row.get("City"))
    state = as_text(row.get("State"))
    zip_code = as_text(row.get("Zip Code"))
    city_line = " ".join(part for part in (state, zip_code) if part)
    if city and city_line:
        city_line = f"{city}, {city_line}"
    elif city:
        city_line = city
    return ", ".join(part for part in (street, city_line) if part)


def signup_message(row) -> tuple[str, str, str]:
    """Subject, plain text, and HTML for one Directory signup row."""
    company = as_text(row.get("Company Name")) or "(no company name)"
    contact = " ".join(
        part
        for part in (
            as_text(row.get("Contact First Name")),
            as_text(row.get("Contact Last Name")),
        )
        if part
    ) or "(no contact name)"
    email = as_text(row.get("Contact Email Address")) or as_text(row.get("Shipping Email"))
    phone = as_text(row.get("Phone Number")) or as_text(row.get("Shipping Phone"))
    address = _address_line(row)
    payment = as_text(row.get("Payment preference"))
    tax = as_text(row.get("Tax exempt status"))
    certificate = as_text(row.get("Tax Exempt File Link"))
    bill_name = " ".join(
        part
        for part in (
            as_text(row.get("Billing First Name")),
            as_text(row.get("Billing Last Name")),
        )
        if part
    )
    bill_emails = ", ".join(
        part
        for part in (
            as_text(row.get("Billing Email 1")),
            as_text(row.get("Billing Email 2")),
            as_text(row.get("Billing Email 3")),
        )
        if part
    )
    bill_phone = as_text(row.get("Billing Phone"))

    who = contact if company.startswith("(") else f"{contact} at {company}"
    subject = f"New customer signup — {who}"

    lines = [
        f"{who} filled out the wholesale form and was added to the Directory.",
        "",
        f"Company: {company}",
        f"Contact: {contact}",
        f"Email: {email or '—'}",
        f"Phone: {phone or '—'}",
        f"Address: {address or '—'}",
    ]
    if payment:
        lines.append(f"Payment: {payment}")
    if tax:
        lines.append(f"Tax exempt: {tax}")
    if certificate:
        lines.append(f"Certificate: {certificate}")
    if bill_name or bill_emails or bill_phone:
        lines.append(
            "Billing: "
            + ", ".join(part for part in (bill_name, bill_emails, bill_phone) if part)
        )
    plain = "\n".join(lines)

    def row_html(label, value):
        safe = html.escape(value or "—")
        return (
            f"<tr><td style=\"padding:4px 12px 4px 0;color:#444;\">{html.escape(label)}</td>"
            f"<td style=\"padding:4px 0;\">{safe}</td></tr>"
        )

    body_rows = [
        row_html("Company", company),
        row_html("Contact", contact),
        row_html("Email", email),
        row_html("Phone", phone),
        row_html("Address", address),
    ]
    if payment:
        body_rows.append(row_html("Payment", payment))
    if tax:
        body_rows.append(row_html("Tax exempt", tax))
    if certificate:
        body_rows.append(row_html("Certificate", certificate))
    if bill_name or bill_emails or bill_phone:
        body_rows.append(
            row_html(
                "Billing",
                ", ".join(part for part in (bill_name, bill_emails, bill_phone) if part),
            )
        )
    html_body = (
        "<!DOCTYPE html><html><body style=\"font-family:Arial,sans-serif;color:#111;line-height:1.45;\">"
        f"<p><strong>{html.escape(who)}</strong> filled out the wholesale form and was added to the Directory.</p>"
        f"<table style=\"border-collapse:collapse;\">{''.join(body_rows)}</table>"
        "</body></html>"
    )
    return subject, plain, html_body


def _cell_update(notify_col: int, sheet_row: int, value: str) -> dict:
    letter = directory_customer_mod.col_letter(notify_col)
    return {"range": f"Directory!{letter}{sheet_row}", "values": [[value]]}


def send_signup_email(row) -> bool:
    to_email = notify_email()
    from_email = (os.environ.get("DESIGN_CONFIRMATION_FROM_EMAIL") or "info@jrco.us").strip()
    smtp_host = (os.environ.get("SMTP_HOST") or "").strip()
    smtp_port = int(os.environ.get("SMTP_PORT") or "587")
    smtp_user = (os.environ.get("SMTP_USER") or "").strip()
    smtp_password = (os.environ.get("SMTP_PASSWORD") or "").strip()
    if not to_email:
        logger.warning("[WholesaleSignup] no recipient email")
        return False
    if not smtp_host or not smtp_user or not smtp_password:
        logger.warning("[WholesaleSignup] SMTP not configured — signup email not sent")
        return False

    subject, plain, html_body = signup_message(row)
    from email.message import EmailMessage
    import smtplib

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    msg.set_content(plain)
    msg.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(from_email, [to_email], msg.as_string())
    except Exception:
        logger.exception("[WholesaleSignup] failed to email %s", to_email)
        return False
    logger.info("[WholesaleSignup] emailed %s: %s", to_email, subject)
    return True


def updates_for_signups(values, send_fn=None, today=None, emailed_keys=None):
    """
    Send (or simulate) signup emails and return Sheets cell updates.

    Failed sends are left unmarked so the next pass retries them.
    """
    if send_fn is None:
        send_fn = send_signup_email
    if emailed_keys is None:
        emailed_keys = _emailed_keys
    notify_col, header_needs_write, notices = plan_signups(values, today=today)
    updates = []
    if notify_col is None:
        return updates
    if header_needs_write:
        updates.append(_cell_update(notify_col, 1, NOTIFY_HEADER))
    for notice in notices[:MAX_PER_PASS]:
        key = notice["key"]
        if key not in emailed_keys:
            if not send_fn(notice["row"]):
                continue
            emailed_keys.add(key)
        updates.append(_cell_update(notify_col, notice["sheet_row"], "Yes"))
    return updates


def run_once():
    if not notifier_enabled():
        return
    import server

    spreadsheet_id = (os.environ.get("SPREADSHEET_ID") or "").strip()
    if not spreadsheet_id:
        logger.warning("[WholesaleSignup] SPREADSHEET_ID is not set")
        return
    with server.acquire_sheet_lock(timeout=20):
        sheets = server.get_sheets_service()
        resp = (
            sheets.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=SHEET_RANGE,
                valueRenderOption="UNFORMATTED_VALUE",
            )
            .execute()
        )
    values = resp.get("values") or []
    updates = updates_for_signups(values)
    if not updates:
        return
    with server.acquire_sheet_lock(timeout=20):
        sheets = server.get_sheets_service()
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()


def _poll_interval_seconds() -> int:
    try:
        seconds = int(os.environ.get("WHOLESALE_SIGNUP_POLL_SECONDS") or "120")
    except ValueError:
        seconds = 120
    return max(60, seconds)


def _loop():
    try:
        initial = int(os.environ.get("WHOLESALE_SIGNUP_POLL_INITIAL_SECONDS") or "45")
    except ValueError:
        initial = 45
    time.sleep(max(0, initial))
    interval = _poll_interval_seconds()
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("[WholesaleSignup] pass failed")
        time.sleep(interval)


def start_background_poller():
    """Start one daemon poller. Safe to call more than once."""
    global _started
    if not notifier_enabled():
        logger.info("[WholesaleSignup] notifier is off")
        return
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(
        target=_loop, name="wholesale-signup-notify", daemon=True
    ).start()
    logger.info(
        "[WholesaleSignup] watching Directory for new applications; emailing %s",
        notify_email(),
    )
