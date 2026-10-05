"""Helpers for editing Directory customers (buyer/contact, billing, addresses)."""

from __future__ import annotations

SHIP_ATTN_HEADERS = (
    "Shipping Attention",
    "Shipping ATTN",
    "Ship ATTN",
    "Ship Attn",
    "Ship To Attention",
    "Receiving Attention",
    "Receiving Department",
    "Receiving Dept",
    "ATTN",
    "Attn",
)

# apiKey -> (sheet headers to write, "all" matching columns or "first" only)
FIELD_WRITES = (
    ("contactFirstName", ("Contact First Name",), "all"),
    ("contactLastName", ("Contact Last Name",), "all"),
    ("contactEmailAddress", ("Contact Email Address",), "all"),
    ("shippingEmail", ("Shipping Email",), "all"),
    ("phoneNumber", ("Phone Number",), "all"),
    ("shippingPhone", ("Shipping Phone",), "all"),
    ("shippingAttention", SHIP_ATTN_HEADERS, "first"),
    ("streetAddress1", ("Street Address 1", "Shipping Street Address 1"), "all"),
    ("streetAddress2", ("Street Address 2", "Shipping Street Address 2"), "all"),
    ("streetAddress3", ("Shipping Address 3", "Street Address 3"), "all"),
    ("city", ("City", "Shipping City"), "all"),
    ("state", ("State", "Shipping State"), "all"),
    ("zipCode", ("Zip Code", "Shipping Zip", "Shipping Zip Code"), "all"),
    ("billingFirstName", ("Billing First Name",), "all"),
    ("billingLastName", ("Billing Last Name",), "all"),
    ("billingEmail1", ("Billing Email 1",), "all"),
    ("billingEmail2", ("Billing Email 2",), "all"),
    ("billingEmail3", ("Billing Email 3",), "all"),
    ("billingPhone", ("Billing Phone",), "all"),
    ("billingStreetAddress1", ("Billing Street Address 1",), "all"),
    ("billingStreetAddress2", ("Billing Street Address 2",), "all"),
    ("billingStreetAddress3", ("Billing Street Address 3",), "all"),
    ("billingCity", ("Billing City",), "all"),
    ("billingState", ("Billing State",), "all"),
    ("billingZip", ("Billing Zip", "Billing Zip Code"), "all"),
    ("billingSameAsShipping", ("Billing Same as Shipping",), "all"),
)

API_KEYS = tuple(key for key, _headers, _mode in FIELD_WRITES)

SUPABASE_API_TO_COL = {
    "contactFirstName": "Contact First Name",
    "contactLastName": "Contact Last Name",
    "contactEmailAddress": "Contact Email Address",
    "streetAddress1": "Street Address 1",
    "streetAddress2": "Street Address 2",
    "city": "City",
    "state": "State",
    "zipCode": "Zip Code",
    "phoneNumber": "Phone Number",
}


def col_letter(index_zero_based: int) -> str:
    n = int(index_zero_based) + 1
    if n < 1:
        return "A"
    letters = []
    while n:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(65 + rem))
    return "".join(reversed(letters))


def coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("yes", "y", "true", "1", "on", "same")


def _header_index_map(headers):
    out = {}
    for i, raw in enumerate(headers or []):
        key = str(raw or "").strip().lower()
        if key and key not in out:
            out[key] = i
    return out


def find_company_row(values, company_name):
    """Return (1-based sheet row, headers, row_dict) for a Directory match, or None."""
    if not values or len(values) < 2:
        return None
    headers = list(values[0] or [])
    wanted = " ".join(str(company_name or "").split()).casefold()
    if not wanted:
        return None
    idx_map = _header_index_map(headers)
    company_idx = idx_map.get("company name")
    if company_idx is None:
        return None
    for sheet_row, row in enumerate(values[1:], start=2):
        cell = row[company_idx] if company_idx < len(row) else ""
        name = " ".join(str(cell or "").split())
        if name.casefold() != wanted:
            continue
        row_dict = {}
        for i, header in enumerate(headers):
            key = str(header or "").strip()
            if not key:
                continue
            row_dict[key] = row[i] if i < len(row) else ""
        return sheet_row, headers, row_dict
    return None


def pick_row_value(row, *names, allow_empty=False) -> str:
    if not isinstance(row, dict):
        return ""
    by = {str(k or "").strip().lower(): v for k, v in row.items()}
    for name in names:
        key = str(name or "").strip().lower()
        if key not in by:
            continue
        val = str(by.get(key) or "").strip()
        if val or allow_empty:
            return val
    return ""


def form_from_row(row) -> dict:
    """Map a Directory row into the edit-customer form fields."""
    contact_email = pick_row_value(row, "Contact Email Address")
    phone = pick_row_value(row, "Phone Number")
    billing_same = coerce_bool(pick_row_value(row, "Billing Same as Shipping"))
    contact_first = pick_row_value(row, "Contact First Name")
    contact_last = pick_row_value(row, "Contact Last Name")
    billing_first = pick_row_value(row, "Billing First Name")
    billing_last = pick_row_value(row, "Billing Last Name")
    billing_same_contact = (
        (not billing_first and not billing_last)
        or (
            billing_first.casefold() == contact_first.casefold()
            and billing_last.casefold() == contact_last.casefold()
        )
    )
    return {
        "companyName": pick_row_value(row, "Company Name"),
        "contactFirstName": contact_first,
        "contactLastName": contact_last,
        "contactEmailAddress": contact_email,
        "shippingEmail": pick_row_value(row, "Shipping Email") or contact_email,
        "phoneNumber": phone,
        "shippingPhone": pick_row_value(row, "Shipping Phone") or phone,
        "shippingAttention": pick_row_value(row, *SHIP_ATTN_HEADERS),
        "streetAddress1": pick_row_value(
            row, "Shipping Street Address 1", "Street Address 1"
        ),
        "streetAddress2": pick_row_value(
            row, "Shipping Street Address 2", "Street Address 2"
        ),
        "streetAddress3": pick_row_value(
            row, "Shipping Address 3", "Street Address 3"
        ),
        "city": pick_row_value(row, "Shipping City", "City"),
        "state": pick_row_value(row, "Shipping State", "State"),
        "zipCode": pick_row_value(row, "Shipping Zip", "Shipping Zip Code", "Zip Code"),
        "billingSameAsShipping": billing_same,
        "billingSameAsContact": billing_same_contact,
        "billingFirstName": billing_first or contact_first,
        "billingLastName": billing_last or contact_last,
        "billingEmail1": pick_row_value(row, "Billing Email 1") or contact_email,
        "billingEmail2": pick_row_value(row, "Billing Email 2"),
        "billingEmail3": pick_row_value(row, "Billing Email 3"),
        "billingPhone": pick_row_value(row, "Billing Phone") or phone,
        "billingStreetAddress1": pick_row_value(row, "Billing Street Address 1"),
        "billingStreetAddress2": pick_row_value(row, "Billing Street Address 2"),
        "billingStreetAddress3": pick_row_value(row, "Billing Street Address 3"),
        "billingCity": pick_row_value(row, "Billing City"),
        "billingState": pick_row_value(row, "Billing State"),
        "billingZip": pick_row_value(row, "Billing Zip", "Billing Zip Code"),
    }


def normalize_update_fields(data) -> dict:
    """Copy billing-from-buyer / billing-from-shipping, then return writeable fields."""
    src = data if isinstance(data, dict) else {}
    fields = {}
    for key in API_KEYS:
        if key in ("billingSameAsShipping",):
            continue
        raw = src.get(key)
        fields[key] = "" if raw is None else str(raw).strip()

    same_ship = coerce_bool(src.get("billingSameAsShipping"))
    same_contact = coerce_bool(src.get("billingSameAsContact"))

    if not fields.get("shippingEmail"):
        fields["shippingEmail"] = fields.get("contactEmailAddress") or ""
    if not fields.get("shippingPhone"):
        fields["shippingPhone"] = fields.get("phoneNumber") or ""

    if same_contact:
        fields["billingFirstName"] = fields.get("contactFirstName") or ""
        fields["billingLastName"] = fields.get("contactLastName") or ""
        fields["billingEmail1"] = fields.get("contactEmailAddress") or fields.get("billingEmail1") or ""
        fields["billingPhone"] = fields.get("phoneNumber") or fields.get("billingPhone") or ""

    if same_ship:
        fields["billingSameAsShipping"] = "Yes"
        fields["billingStreetAddress1"] = fields.get("streetAddress1") or ""
        fields["billingStreetAddress2"] = fields.get("streetAddress2") or ""
        fields["billingStreetAddress3"] = fields.get("streetAddress3") or ""
        fields["billingCity"] = fields.get("city") or ""
        fields["billingState"] = fields.get("state") or ""
        fields["billingZip"] = fields.get("zipCode") or ""
        fields["billingPhone"] = fields.get("phoneNumber") or fields.get("billingPhone") or ""
        if not fields.get("billingEmail1"):
            fields["billingEmail1"] = fields.get("contactEmailAddress") or ""
    else:
        fields["billingSameAsShipping"] = "No"
    return fields


def header_writes_from_fields(fields, headers) -> dict:
    """Map normalized fields to actual Directory header names present on the sheet."""
    idx_map = _header_index_map(headers)
    writes = {}
    for api_key, aliases, mode in FIELD_WRITES:
        if api_key not in fields:
            continue
        value = fields[api_key]
        matched = []
        for alias in aliases:
            idx = idx_map.get(str(alias).strip().lower())
            if idx is None:
                continue
            header = str(headers[idx] or "").strip()
            if not header:
                continue
            matched.append(header)
            if mode == "first":
                break
        for header in matched:
            writes[header] = value
    return writes


def sheet_value_ranges(sheet_name, headers, sheet_row, writes) -> list:
    idx_map = _header_index_map(headers)
    data = []
    for header, value in writes.items():
        idx = idx_map.get(str(header or "").strip().lower())
        if idx is None:
            continue
        a1 = f"{sheet_name}!{col_letter(idx)}{int(sheet_row)}"
        data.append({"range": a1, "values": [[value]]})
    return data


def apply_writes_to_row(row_dict, writes) -> dict:
    out = dict(row_dict or {})
    for header, value in (writes or {}).items():
        out[header] = value
    return out


def supabase_directory_payload(fields) -> dict:
    out = {}
    src = fields if isinstance(fields, dict) else {}
    for api_key, col in SUPABASE_API_TO_COL.items():
        if api_key in src:
            out[col] = src.get(api_key) or ""
    return out


def build_qbo_customer_sparse_update(
    customer_id,
    sync_token,
    *,
    given_name="",
    family_name="",
    email="",
    phone="",
    bill_addr=None,
    ship_addr=None,
    active=True,
) -> dict:
    payload = {
        "Id": str(customer_id),
        "SyncToken": str(sync_token),
        "sparse": True,
        "Active": bool(active),
    }
    given = str(given_name or "").strip()[:25]
    family = str(family_name or "").strip()[:25]
    if given:
        payload["GivenName"] = given
    if family:
        payload["FamilyName"] = family
    email_addr = str(email or "").strip()[:100]
    if email_addr:
        payload["PrimaryEmailAddr"] = {"Address": email_addr}
    phone_num = str(phone or "").strip()[:30]
    if phone_num:
        payload["PrimaryPhone"] = {"FreeFormNumber": phone_num}
    if bill_addr:
        payload["BillAddr"] = bill_addr
    if ship_addr:
        payload["ShipAddr"] = ship_addr
    return payload
