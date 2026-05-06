"""
Shopify Admin REST API helpers (authenticated staff flows).

Requires env:
  SHOPIFY_STORE_DOMAIN      e.g. jrcogolf.myshopify.com
  SHOPIFY_ADMIN_ACCESS_TOKEN  Admin API access token (custom app)
  SHOPIFY_API_VERSION       optional, default 2024-10
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

SHOPIFY_STORE_DOMAIN = (os.environ.get("SHOPIFY_STORE_DOMAIN") or "").strip()
SHOPIFY_ADMIN_ACCESS_TOKEN = (os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or "").strip()
SHOPIFY_API_VERSION = (os.environ.get("SHOPIFY_API_VERSION") or "2024-10").strip()


def shopify_configured() -> bool:
    return bool(SHOPIFY_STORE_DOMAIN and SHOPIFY_ADMIN_ACCESS_TOKEN)


def normalize_shop_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    if not d:
        return ""
    if ".myshopify.com" not in d:
        d = f"{d}.myshopify.com"
    return d


def admin_api_base() -> str:
    shop = normalize_shop_domain(SHOPIFY_STORE_DOMAIN)
    if not shop:
        raise RuntimeError("SHOPIFY_STORE_DOMAIN is not set")
    return f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}"


def admin_order_editor_url(order_numeric_id: str) -> str:
    """Legacy admin URL — opens the order; staff uses Print → Packing slip."""
    shop = normalize_shop_domain(SHOPIFY_STORE_DOMAIN)
    oid = str(order_numeric_id or "").strip()
    if not shop or not oid:
        return ""
    return f"https://{shop}/admin/orders/{oid}"

def admin_order_print_bulk_hint_url() -> str:
    """Orders index (bulk packing slips live under … actions). Optional fallback."""
    shop = normalize_shop_domain(SHOPIFY_STORE_DOMAIN)
    if not shop:
        return ""
    return f"https://{shop}/admin/orders"


def _headers() -> dict[str, str]:
    return {
        "X-Shopify-Access-Token": SHOPIFY_ADMIN_ACCESS_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def admin_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    timeout: int = 60,
) -> tuple[int, Any]:
    """
    path: relative to admin/api/VERSION/ — e.g. orders/123.json
    Returns (status_code, parsed_json_or_text).
    """
    if not shopify_configured():
        raise RuntimeError(
            "Shopify Admin API is not configured (SHOPIFY_STORE_DOMAIN / SHOPIFY_ADMIN_ACCESS_TOKEN)"
        )
    base = admin_api_base().rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    r = requests.request(
        method.upper(),
        url,
        headers=_headers(),
        params=params,
        json=json_body,
        timeout=timeout,
    )
    try:
        data = r.json()
    except Exception:
        data = {"_raw": (r.text or "")[:2000]}
    return r.status_code, data


def fetch_order(order_numeric_id: str) -> tuple[int, Any]:
    oid = str(order_numeric_id or "").strip()
    if not oid.isdigit():
        return 400, {"error": "shopify_order_id must be a numeric Shopify order id"}
    fields = ",".join(
        [
            "id",
            "name",
            "email",
            "financial_status",
            "fulfillment_status",
            "shipping_lines",
            "shipping_address",
            "line_items",
            "note",
            "created_at",
            "cancelled_at",
        ]
    )
    return admin_request(
        "GET",
        f"orders/{oid}.json",
        params={"fields": fields},
    )


def create_fulfillment_with_tracking(
    order_numeric_id: str,
    tracking_numbers: list[str],
    *,
    carrier_name: str = "UPS",
    notify_customer: bool = True,
) -> tuple[int, Any]:
    """
    Fulfill all open/in_progress fulfillment orders for this Shopify order,
    using the Fulfillment Orders API.
    """
    oid = str(order_numeric_id or "").strip()
    if not oid.isdigit():
        return 400, {"error": "shopify_order_id must be a numeric Shopify order id"}

    tracks = [str(t).strip() for t in (tracking_numbers or []) if str(t).strip()]
    primary_tracking = tracks[0] if tracks else ""

    st, fo_payload = admin_request(
        "GET", f"orders/{oid}/fulfillment_orders.json"
    )
    if st >= 400:
        return st, fo_payload

    fulfillment_orders = (fo_payload or {}).get("fulfillment_orders") or []
    line_items_by_fulfillment_order: list[dict[str, Any]] = []

    for fo in fulfillment_orders:
        status = str(fo.get("status") or "").lower()
        if status in ("closed", "cancelled"):
            continue
        fo_id = fo.get("id")
        if fo_id is None:
            continue
        folis = fo.get("line_items") or []
        items_out: list[dict[str, Any]] = []
        for li in folis:
            li_id = li.get("id")
            if li_id is None:
                continue
            qty = int(
                li.get("fulfillable_quantity")
                or li.get("remaining_quantity")
                or li.get("quantity")
                or 0
            )
            if qty <= 0:
                continue
            items_out.append({"id": li_id, "quantity": qty})
        if items_out:
            line_items_by_fulfillment_order.append(
                {
                    "fulfillment_order_id": fo_id,
                    "fulfillment_order_line_items": items_out,
                }
            )

    if not line_items_by_fulfillment_order:
        return 422, {
            "error": "No fulfillable line items (order may already be fulfilled)",
            "detail": fo_payload,
        }

    fulfillment_body: dict[str, Any] = {
        "fulfillment": {
            "notify_customer": bool(notify_customer),
            "line_items_by_fulfillment_order": line_items_by_fulfillment_order,
        }
    }
    if primary_tracking:
        fulfillment_body["fulfillment"]["tracking_info"] = {
            "company": (carrier_name or "UPS")[:50],
            "number": primary_tracking[:100],
        }

    st2, created = admin_request(
        "POST", "fulfillments.json", json_body=fulfillment_body
    )
    return st2, created
