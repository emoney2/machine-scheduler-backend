"""Parse JR Needlepoint Studio `.jrneedlepoint.json` order packages.

Studio File → Save embeds:
  order.customerViewPng  — data:image/png;base64,… (shop / employee preview)
  order.gridViewPdf      — data:application/pdf;base64,… (clear stitch grid for customer)
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

STUDIO_PACKAGE_KIND = "jr-needlepoint-studio-package"
DATA_URL_RE = re.compile(r"^data:([^;,]+)?(?:;[^,]*)?,(.+)$", re.DOTALL)


def _safe_stem(name: str) -> str:
    base = (name or "design").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    base = re.sub(r"\.(jrneedlepoint\.)?json$", "", base, flags=re.I)
    base = re.sub(r"[^\w\-]+", "-", base).strip("-") or "design"
    return base[:80]


def _decode_data_url(data_url: str) -> tuple[str, bytes] | None:
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    m = DATA_URL_RE.match(data_url.strip())
    if not m:
        return None
    mime = (m.group(1) or "application/octet-stream").strip().lower()
    try:
        raw = base64.b64decode(m.group(2), validate=False)
    except Exception:
        return None
    if not raw:
        return None
    return mime, raw


def is_studio_package_bytes(raw: bytes | None, filename: str = "") -> bool:
    if not raw:
        return False
    name = (filename or "").lower()
    if name.endswith(".json") or "jrneedlepoint" in name:
        pass
    elif not raw.lstrip().startswith((b"{", b"[")):
        return False
    try:
        text = raw.decode("utf-8-sig")
        parsed = json.loads(text)
    except Exception:
        return False
    return isinstance(parsed, dict) and parsed.get("kind") == STUDIO_PACKAGE_KIND


def parse_studio_package(raw: bytes | str) -> dict[str, Any] | None:
    """Return normalized package fields, or None if not a Studio package."""
    try:
        text = raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else str(raw)
        parsed = json.loads(text)
    except Exception:
        return None
    if not isinstance(parsed, dict) or parsed.get("kind") != STUDIO_PACKAGE_KIND:
        return None
    order = parsed.get("order")
    if not isinstance(order, dict):
        order = {}
    png = _decode_data_url(order.get("customerViewPng") or "")
    pdf = _decode_data_url(order.get("gridViewPdf") or "")
    if not png and not pdf:
        # Package without embedded order artifacts (lean autosave) — not usable for submit preview.
        return None
    document = parsed.get("document") if isinstance(parsed.get("document"), dict) else {}
    preview = document.get("preview") if isinstance(document.get("preview"), dict) else {}
    product_type = str(order.get("productType") or preview.get("productType") or "").strip().lower()
    product = str(order.get("product") or "").strip()
    if product_type not in ("belt", "key-fob"):
        low_product = product.lower()
        low_name = str(order.get("designName") or document.get("name") or "").lower()
        if ("key" in low_product and "fob" in low_product) or "fob" in low_name:
            product_type = "key-fob"
        else:
            product_type = "belt"
    if not product:
        product = (
            "Needle Point Key Fob" if product_type == "key-fob" else "Needle Point Belt"
        )
    return {
        "kind": STUDIO_PACKAGE_KIND,
        "designName": str(order.get("designName") or document.get("name") or "").strip(),
        "customerName": str(order.get("customerName") or "").strip(),
        "productType": product_type,
        "product": product,
        "leather": str(order.get("leather") or "").strip(),
        "buckle": str(order.get("buckle") or "").strip(),
        "materialComboId": str(order.get("materialComboId") or "").strip(),
        "width": order.get("width"),
        "height": order.get("height"),
        "palette": order.get("palette") if isinstance(order.get("palette"), list) else [],
        "customerViewPng": png,  # (mime, bytes) | None
        "gridViewPdf": pdf,  # (mime, bytes) | None
        "rawPackage": parsed,
    }


def expand_studio_package_uploads(
    raw: bytes,
    filename: str,
    *,
    keep_json: bool = True,
) -> list[dict[str, Any]] | None:
    """
    Turn a Studio JSON upload into Drive upload payloads.

    Order matters: PNG first so Sheets IMAGE() / primaryFileId uses the customer view.
    """
    pkg = parse_studio_package(raw)
    if not pkg:
        return None
    stem = _safe_stem(filename)
    if pkg["designName"]:
        stem = _safe_stem(pkg["designName"])
    uploads: list[dict[str, Any]] = []
    if pkg["customerViewPng"]:
        mime, data = pkg["customerViewPng"]
        uploads.append(
            {
                "filename": f"{stem}-customer-view.png",
                "bytes": data,
                "mimetype": mime if mime.startswith("image/") else "image/png",
                "role": "customer_view",
            }
        )
    if pkg["gridViewPdf"]:
        mime, data = pkg["gridViewPdf"]
        uploads.append(
            {
                "filename": f"{stem}-grid.pdf",
                "bytes": data,
                "mimetype": "application/pdf",
                "role": "grid_view",
            }
        )
    if keep_json and uploads:
        uploads.append(
            {
                "filename": f"{stem}.jrneedlepoint.json",
                "bytes": raw if isinstance(raw, (bytes, bytearray)) else raw.encode("utf-8"),
                "mimetype": "application/json",
                "role": "studio_package",
            }
        )
    if not uploads:
        return None
    meta = {
        "designName": pkg["designName"],
        "customerName": pkg["customerName"],
        "leather": pkg["leather"],
        "buckle": pkg["buckle"],
        "materialComboId": pkg["materialComboId"],
        "palette": pkg["palette"],
    }
    for item in uploads:
        item["packageMeta"] = meta
    logger.info(
        "[JR Package] expanded %s → %s",
        filename,
        ", ".join(u["filename"] for u in uploads),
    )
    return uploads


def _attachment_label(att: dict[str, Any]) -> str:
    return str(att.get("sourceName") or att.get("filename") or "").lower()


def prefer_grid_attachments(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Vendor email should get stitch-grid PDFs only (no Drive links, no customer PNG).

    Prefer Studio `-grid.pdf` / embedded grid PDFs; otherwise any PDF; never JSON.
    """
    if not attachments:
        return attachments
    usable = []
    for a in attachments:
        if not a.get("bytes"):
            continue
        # Email filename wins — sourceName may be the Studio .jrneedlepoint.json
        # the PDF was extracted from (must not discard those).
        if a.get("role") == "grid_view":
            usable.append(a)
            continue
        label = str(a.get("filename") or a.get("sourceName") or "").lower()
        if label.endswith(".json") or (
            "jrneedlepoint" in label and not label.endswith(".pdf")
        ):
            continue
        usable.append(a)
    grids = [
        a
        for a in usable
        if a.get("role") == "grid_view"
        or str(a.get("filename") or "").lower().endswith("-grid.pdf")
        or _attachment_label(a).endswith("-grid.pdf")
        or (
            "grid" in _attachment_label(a)
            and (
                _attachment_label(a).endswith(".pdf")
                or "pdf" in str(a.get("mime") or "").lower()
            )
        )
    ]
    if grids:
        return grids
    pdfs = [
        a
        for a in usable
        if _attachment_label(a).endswith(".pdf")
        or "pdf" in str(a.get("mime") or "").lower()
        or (isinstance(a.get("bytes"), (bytes, bytearray)) and a["bytes"][:4] == b"%PDF")
    ]
    if pdfs:
        return pdfs
    # No PDF found — do not fall back to PNG/customer view for the vendor.
    return []
