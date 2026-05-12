"""
Sales commission ledger + QuickBooks payment sync (no Flask imports — wired from server.py).
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import secrets
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

COMMISSION_LEDGER_TAB = os.environ.get("COMMISSION_LEDGER_TAB", "Commission Ledger")
COMMISSION_RATE_DEFAULT = float(os.environ.get("COMMISSION_RATE", "0.12"))

LEDGER_HEADERS: List[str] = [
    "Invoice QBO Id",
    "Invoice #",
    "Order #s",
    "Rep",
    "Product subtotal",
    "Commission %",
    "Commission $",
    "Customer paid",
    "Invoice paid date",
    "Rep pay due",
    "Rep paid",
    "Rep paid date",
    "Notes",
]

_REP_KEYS = ("REP", "Referral", "Sales Rep", "rep")


def _col_letter(idx0: int) -> str:
    n = idx0 + 1
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def parse_sales_portal_users() -> List[Dict[str, Any]]:
    raw = (os.environ.get("SALES_PORTAL_USERS") or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("SALES_PORTAL_USERS is not valid JSON")
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        u = str(item.get("username") or "").strip()
        p = str(item.get("password") or "")
        role = str(item.get("role") or "rep").strip().lower()
        rep_name = str(item.get("repName") or item.get("rep_name") or "").strip()
        if u and p:
            out.append(
                {
                    "username": u,
                    "password": p,
                    "role": "admin" if role == "admin" else "rep",
                    "repName": rep_name,
                }
            )
    return out


def find_sales_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    u_in = str(username or "").strip()
    p_in = str(password or "")
    if not u_in or not p_in:
        return None
    for rec in parse_sales_portal_users():
        if not secrets.compare_digest(rec["username"], u_in):
            continue
        if secrets.compare_digest(rec["password"], p_in):
            return rec
    return None


def _rep_from_orders(all_order_data: List[dict]) -> Tuple[str, str]:
    names: List[str] = []
    for od in all_order_data or []:
        found = ""
        for k in _REP_KEYS:
            v = str(od.get(k) or "").strip()
            if v:
                found = v
                break
        names.append(found)
    primary = names[0] if names else ""
    uniq = {n for n in names if n}
    note = ""
    if len(uniq) > 1:
        note = (
            "Multiple REPs on consolidated invoice; commission row uses first order's REP: "
            + repr(primary)
            + "."
        )
    return primary, note


def invoice_product_subtotal_for_commission(inv: dict) -> float:
    """
    Sum SalesItemLineDetail amounts, excluding lines that look like shipping / tax / fees.
    Also subtract native ShipAmt on the invoice (shipping not in product sales).
    """
    if not isinstance(inv, dict):
        return 0.0
    lines = inv.get("Line") or []
    if not isinstance(lines, list):
        lines = []
    skip_kw = ("shipping", "freight", "fee", "tax", "surcharge")
    total = 0.0
    for ln in lines:
        if not isinstance(ln, dict):
            continue
        det = str(ln.get("DetailType") or "")
        if det == "TaxLineDetail":
            continue
        if det != "SalesItemLineDetail":
            continue
        try:
            amt = float(ln.get("Amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        sid = ln.get("SalesItemLineDetail") or {}
        item_ref = sid.get("ItemRef") if isinstance(sid, dict) else {}
        name = (
            str((item_ref or {}).get("name") or "").lower()
            if isinstance(item_ref, dict)
            else ""
        )
        desc = str(ln.get("Description") or "").lower()
        blob = f"{name} {desc}"
        if any(k in blob for k in skip_kw):
            continue
        total += amt
    try:
        ship_amt = float(inv.get("ShipAmt") or 0)
    except (TypeError, ValueError):
        ship_amt = 0.0
    total = max(0.0, total - max(0.0, ship_amt))
    return round(total, 2)


def _invoice_balance(inv: dict) -> float:
    try:
        return float(inv.get("Balance") or 0)
    except (TypeError, ValueError):
        return 0.0


def _last_day_of_month(d: date) -> date:
    _, last = calendar.monthrange(d.year, d.month)
    return date(d.year, d.month, last)


def _fmt_mdy_windows_safe(d: date) -> str:
    """M/D/YYYY without %-m (Windows strftime may not support)."""
    return f"{d.month}/{d.day}/{d.year}"


def paid_and_due_dates_safe() -> Tuple[str, str]:
    now = datetime.now()
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        pass
    d = now.date()
    last = _last_day_of_month(d)
    return _fmt_mdy_windows_safe(d), _fmt_mdy_windows_safe(last)


def ensure_ledger_headers(service, spreadsheet_id: str) -> None:
    rng = f"{COMMISSION_LEDGER_TAB}!A1:{_col_letter(len(LEDGER_HEADERS) - 1)}1"
    try:
        res = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=rng)
            .execute()
        )
        row = (res.get("values") or [[]])[0]
        if row and str(row[0] or "").strip() == LEDGER_HEADERS[0]:
            return
    except Exception as e:
        logger.warning("Commission ledger header read: %s", e)
    body = {"values": [LEDGER_HEADERS]}
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=rng,
        valueInputOption="USER_ENTERED",
        body=body,
    ).execute()


def ledger_row_for_pending_invoice(
    inv: dict,
    order_ids: List[str],
    all_order_data: List[dict],
    commission_rate: float,
) -> List[Any]:
    rep, note = _rep_from_orders(all_order_data)
    iid = str(inv.get("Id") or "").strip()
    doc = str(inv.get("DocNumber") or "").strip()
    prod = invoice_product_subtotal_for_commission(inv)
    comm = round(float(prod) * float(commission_rate), 2)
    pct_display = round(float(commission_rate) * 100.0, 2)
    orders_join = ", ".join(str(x).strip() for x in order_ids if str(x).strip())
    return [
        iid,
        doc,
        orders_join,
        rep,
        prod,
        pct_display,
        comm,
        "N",
        "",
        "",
        "N",
        "",
        note,
    ]


def append_ledger_row(service, spreadsheet_id: str, row: List[Any]) -> None:
    ensure_ledger_headers(service, spreadsheet_id)
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{COMMISSION_LEDGER_TAB}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def invoice_id_exists_in_ledger(rows_values: List[List[Any]], invoice_id: str) -> bool:
    iid = str(invoice_id or "").strip()
    if not iid:
        return False
    for r in rows_values[1:] or []:
        if not r:
            continue
        if str(r[0] or "").strip() == iid:
            return True
    return False


def maybe_add_invoice_column_updates(
    updates: List[dict],
    headers_row: List[str],
    sheet_name: str,
    order_id_to_rownum: Dict[str, int],
    qbo_invoice_id: str,
    invoice_doc_number: str,
) -> None:
    """
    If Production Orders has optional columns Invoice QBO Id / Invoice #, queue cell updates.
    """
    hdr = _hdr_index(headers_row)
    idx_qbo = hdr.get("invoice qbo id")
    idx_doc = hdr.get("invoice #")
    if idx_doc is None:
        idx_doc = hdr.get("invoice number")
    if idx_qbo is None and idx_doc is None:
        return
    col_qbo = _col_letter(idx_qbo) if idx_qbo is not None else None
    col_doc = _col_letter(idx_doc) if idx_doc is not None else None
    for oid, rownum in order_id_to_rownum.items():
        if col_qbo:
            updates.append(
                {
                    "range": f"{sheet_name}!{col_qbo}{rownum}",
                    "values": [[str(qbo_invoice_id or "").strip()]],
                }
            )
        if col_doc:
            updates.append(
                {
                    "range": f"{sheet_name}!{col_doc}{rownum}",
                    "values": [[str(invoice_doc_number or "").strip()]],
                }
            )


def _hdr_index(headers: List[str]) -> Dict[str, int]:
    return {str(h or "").strip().lower(): i for i, h in enumerate(headers or [])}


def read_ledger_all(service, spreadsheet_id: str) -> Tuple[List[str], List[List[Any]]]:
    rng = f"{COMMISSION_LEDGER_TAB}!A:{_col_letter(len(LEDGER_HEADERS) - 1)}"
    res = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=rng, majorDimension="ROWS")
        .execute()
    )
    vals = res.get("values") or []
    if not vals:
        return LEDGER_HEADERS, []
    headers = [str(x or "").strip() for x in vals[0]]
    if headers[0] != LEDGER_HEADERS[0]:
        return LEDGER_HEADERS, vals
    return headers, vals


def sync_ledger_with_qbo(
    service,
    spreadsheet_id: str,
    qbo_get_invoice_fn: Callable[..., Tuple[Optional[dict], Optional[str]]],
    headers: dict,
    realm_id: str,
    env_override: Optional[str],
) -> Dict[str, Any]:
    """
    For each ledger row not yet Customer paid, GET invoice; if Balance == 0, fill paid dates.
    """
    _, rows = read_ledger_all(service, spreadsheet_id)
    if len(rows) < 2:
        return {"updated": 0, "message": "no ledger rows"}
    paid_col = 7
    paid_date_col = 8
    rep_due_col = 9
    updates = 0
    paid_today, rep_due = paid_and_due_dates_safe()
    for ri, r in enumerate(rows[1:], start=2):
        r = r or []
        pad = list(r) + [""] * len(LEDGER_HEADERS)
        iid = str(pad[0] or "").strip()
        if not iid:
            continue
        cust_paid = str(pad[paid_col] or "").strip().upper()
        if cust_paid == "Y":
            continue
        inv, err = qbo_get_invoice_fn(headers, realm_id, iid, env_override)
        if not inv:
            logger.warning("commission sync: no invoice %s: %s", iid, err)
            continue
        bal = _invoice_balance(inv)
        if bal > 0.009:
            continue
        try:
            total_amt = float(inv.get("TotalAmt") or 0)
        except (TypeError, ValueError):
            total_amt = 0.0
        if total_amt <= 0:
            continue
        c0 = _col_letter(paid_col)
        c1 = _col_letter(paid_date_col)
        c2 = _col_letter(rep_due_col)
        rng = f"{COMMISSION_LEDGER_TAB}!{c0}{ri}:{c2}{ri}"
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=rng,
            valueInputOption="USER_ENTERED",
            body={"values": [["Y", paid_today, rep_due]]},
        ).execute()
        updates += 1
    return {"updated": updates}


def mark_rep_paid_rows(
    service,
    spreadsheet_id: str,
    invoice_qbo_ids: List[str],
) -> int:
    want = {str(x or "").strip() for x in invoice_qbo_ids if str(x or "").strip()}
    if not want:
        return 0
    _, rows = read_ledger_all(service, spreadsheet_id)
    rep_paid_col = 10
    rep_paid_date_col = 11
    paid_today, _ = paid_and_due_dates_safe()
    changed = 0
    for ri, r in enumerate(rows[1:], start=2):
        r = r or []
        pad = list(r) + [""] * len(LEDGER_HEADERS)
        iid = str(pad[0] or "").strip()
        if iid not in want:
            continue
        cust = str(pad[7] or "").strip().upper()
        if cust != "Y":
            continue
        if str(pad[rep_paid_col] or "").strip().upper() == "Y":
            continue
        c0 = _col_letter(rep_paid_col)
        c1 = _col_letter(rep_paid_date_col)
        rng = f"{COMMISSION_LEDGER_TAB}!{c0}{ri}:{c1}{ri}"
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=rng,
            valueInputOption="USER_ENTERED",
            body={"values": [["Y", paid_today]]},
        ).execute()
        changed += 1
    return changed


def ledger_rows_for_rep(rows: List[List[Any]], rep_name: str) -> List[Dict[str, Any]]:
    out = []
    if len(rows) < 2:
        return out
    headers = LEDGER_HEADERS
    target = str(rep_name or "").strip().lower()
    for r in rows[1:]:
        pad = (r or []) + [""] * len(headers)
        rowd = {headers[i]: pad[i] for i in range(len(headers))}
        rep = str(rowd.get("Rep") or "").strip()
        if rep.lower() == target:
            out.append(rowd)
    return out


def ledger_rows_admin(rows: List[List[Any]]) -> List[Dict[str, Any]]:
    if len(rows) < 2:
        return []
    headers = LEDGER_HEADERS
    out = []
    for r in rows[1:]:
        pad = (r or []) + [""] * len(headers)
        out.append({headers[i]: pad[i] for i in range(len(headers))})
    return out
