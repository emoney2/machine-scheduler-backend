"""In-memory batch reorder jobs and helpers shared by /api/reorder-batch."""

from __future__ import annotations

import re
import threading
import time
import uuid

PREVIEW_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp")


_LOCK = threading.Lock()
_BATCHES = {}
_MAX_BATCHES = 40
_BATCH_TTL_SEC = 24 * 60 * 60
MAX_BATCH_JOBS = 50


def normalize_order_id(value):
    s = str(value or "").strip()
    if not s:
        return ""
    try:
        return str(int(float(s.replace(",", ""))))
    except ValueError:
        return s


def material_fields_from_row(row):
    materials = []
    percents = []
    src = row if isinstance(row, dict) else {}
    for i in range(1, 6):
        materials.append(str(src.get(f"Material{i}") or "").strip())
        raw = (
            src.get(f"Material{i}%")
            if src.get(f"Material{i}%") not in (None, "")
            else src.get(f"Material {i}%")
            if src.get(f"Material {i}%") not in (None, "")
            else src.get(f"Material{i} Percent")
            if src.get(f"Material{i} Percent") not in (None, "")
            else src.get(f"Material {i} Percent")
        )
        percents.append("" if raw in (None, "") else str(raw).strip())
    return materials, percents


def is_back_product_name(product):
    return "back" in str(product or "").strip().lower()


def product_creates_paired_back_order(product):
    """Match submit_order: only quilted Front products auto-create a back row."""
    p = str(product or "").strip().lower()
    if not p or "back" in p or "full" in p:
        return False
    if "blade" in p or "mallet" in p:
        return False
    if "front" not in p:
        return False
    return "quilted" in p


def paired_front_order_number(order_number):
    try:
        return int(str(order_number).strip()) - 1
    except (TypeError, ValueError):
        return None


def filter_selected_reorder_jobs(jobs, selected_ids):
    """
    Keep selected jobs in the given order.
    Skip a Back job when its paired quilted Front is also selected (that front
    already creates the new back folder/row).
    """
    by_id = {}
    for job in jobs or []:
        oid = normalize_order_id(
            (job or {}).get("Order #") or (job or {}).get("orderId")
        )
        if oid and oid not in by_id:
            by_id[oid] = job

    wanted = []
    seen = set()
    for raw in selected_ids or []:
        oid = normalize_order_id(raw)
        if not oid or oid in seen:
            continue
        seen.add(oid)
        wanted.append(oid)

    selected_set = set(wanted)
    to_process = []
    skipped = []
    for oid in wanted:
        job = by_id.get(oid)
        if not job:
            skipped.append(
                {
                    "sourceOrder": oid,
                    "design": "",
                    "product": "",
                    "status": "skipped",
                    "error": "Order not found for this customer",
                }
            )
            continue
        if is_back_product_name(job.get("Product")):
            front_num = paired_front_order_number(oid)
            front = by_id.get(str(front_num)) if front_num is not None else None
            if (
                front_num is not None
                and str(front_num) in selected_set
                and product_creates_paired_back_order((front or {}).get("Product"))
            ):
                skipped.append(
                    {
                        "sourceOrder": oid,
                        "design": str(job.get("Design") or ""),
                        "product": str(job.get("Product") or ""),
                        "status": "skipped",
                        "error": "Paired back — created with the selected front",
                    }
                )
                continue
        to_process.append(job)
    return to_process, skipped


def combine_notes(original, extra):
    original = str(original or "").strip()
    extra = str(extra or "").strip()
    if original and extra:
        return f"{original}\n{extra}"
    return original or extra


def print_yes_from_row(row):
    raw = str((row or {}).get("Print") or "").strip().upper()
    return raw == "YES"


def sales_rep_from_row(row):
    src = row if isinstance(row, dict) else {}
    for key in ("REP", "Sales Rep", "Referral"):
        val = str(src.get(key) or "").strip()
        if val:
            return val
    return ""


def shipping_method_from_row(row):
    src = row if isinstance(row, dict) else {}
    raw = (
        src.get("Shipping Method")
        or src.get("Shipping Type")
        or src.get("Ship Via")
        or ""
    )
    if "local" in str(raw).casefold():
        return "Local Delivery"
    return "UPS"


def drive_folder_url(folder_id):
    fid = str(folder_id or "").strip()
    if not fid:
        return ""
    return f"https://drive.google.com/drive/folders/{fid}"


def drive_file_view_url(file_id):
    fid = str(file_id or "").strip()
    if not fid:
        return ""
    return f"https://drive.google.com/file/d/{fid}/view"


def is_preview_image_name(name):
    lower = str(name or "").lower()
    return any(lower.endswith(ext) for ext in PREVIEW_IMAGE_EXTS)


def first_drive_file_id_from_image_cell(image_cell):
    """First artwork file id from Image — one URL or a comma-separated list."""
    for part in str(image_cell or "").split(","):
        if "/folders/" in part:
            continue
        match = re.search(r"/file/d/([A-Za-z0-9_-]{10,})", part)
        if match:
            return match.group(1)
        match = re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", part)
        if match:
            return match.group(1)
    return ""


def parse_reorder_job_requests(data, default_due_date=""):
    """
    Accept jobs: [{orderId, quantity, dueDate}] or orderIds plus a shared due date.
    Each request keeps its own quantity/due date when provided.
    """
    default_due = str(default_due_date or "").strip()
    payload = data if isinstance(data, dict) else {}
    raw_jobs = payload.get("jobs")
    requests = []
    seen = set()

    def add(order_id, quantity, due_date):
        oid = normalize_order_id(order_id)
        if not oid or oid in seen:
            return
        seen.add(oid)
        requests.append(
            {
                "orderId": oid,
                "quantity": "" if quantity in (None, "") else str(quantity).strip(),
                "dueDate": str(due_date or default_due or "").strip(),
            }
        )

    if isinstance(raw_jobs, list) and raw_jobs:
        for item in raw_jobs:
            if not isinstance(item, dict):
                continue
            add(
                item.get("orderId") or item.get("order_id") or item.get("Order #"),
                item.get("quantity") if item.get("quantity") not in (None, "") else item.get("Quantity"),
                item.get("dueDate") or item.get("due_date") or "",
            )
    else:
        order_ids = payload.get("orderIds") or payload.get("order_ids") or []
        if isinstance(order_ids, str):
            order_ids = [part.strip() for part in order_ids.split(",") if part.strip()]
        for oid in order_ids or []:
            add(oid, "", default_due)

    return requests


def apply_overrides_to_jobs(jobs, requests):
    by_id = {item["orderId"]: item for item in (requests or []) if item.get("orderId")}
    for job in jobs or []:
        oid = normalize_order_id(job.get("Order #") or job.get("orderId"))
        ov = by_id.get(oid) or {}
        job["_reorder_quantity"] = str(
            ov.get("quantity") or job.get("Quantity") or ""
        ).strip()
        job["_reorder_due_date"] = str(ov.get("dueDate") or "").strip()
    return jobs


def _prune_locked(now=None):
    now = time.time() if now is None else now
    expired = [
        bid
        for bid, batch in _BATCHES.items()
        if now - float(batch.get("updatedAt") or 0) > _BATCH_TTL_SEC
    ]
    for bid in expired:
        _BATCHES.pop(bid, None)
    if len(_BATCHES) > _MAX_BATCHES:
        oldest = sorted(
            _BATCHES.items(), key=lambda kv: float(kv[1].get("updatedAt") or 0)
        )
        for bid, _ in oldest[: len(_BATCHES) - _MAX_BATCHES]:
            _BATCHES.pop(bid, None)


def _item_from_job(job, status="queued", error=""):
    return {
        "sourceOrder": normalize_order_id(
            (job or {}).get("Order #") or (job or {}).get("orderId")
        ),
        "design": str((job or {}).get("Design") or ""),
        "product": str((job or {}).get("Product") or ""),
        "quantity": str(
            (job or {}).get("_reorder_quantity") or (job or {}).get("Quantity") or ""
        ).strip(),
        "dueDate": str((job or {}).get("_reorder_due_date") or "").strip(),
        "status": status,
        "newOrder": None,
        "backOrder": None,
        "error": error or "",
    }


def create_reorder_batch(company, jobs, skipped, due_date, date_type, notes=""):
    now = time.time()
    items = [_item_from_job(job) for job in jobs] + list(skipped or [])
    batch = {
        "id": uuid.uuid4().hex,
        "company": str(company or "").strip(),
        "dueDate": str(due_date or "").strip(),
        "dateType": str(date_type or "Hard Date").strip() or "Hard Date",
        "notes": str(notes or "").strip(),
        "status": "queued",
        "createdAt": now,
        "updatedAt": now,
        "items": items,
    }
    with _LOCK:
        _prune_locked(now)
        _BATCHES[batch["id"]] = batch
    return public_reorder_batch(batch["id"])


def get_reorder_batch(batch_id):
    with _LOCK:
        batch = _BATCHES.get(str(batch_id or "").strip())
        if not batch:
            return None
        return batch


def public_reorder_batch(batch_id):
    batch = get_reorder_batch(batch_id)
    if not batch:
        return None
    items = list(batch.get("items") or [])
    counts = {"queued": 0, "running": 0, "done": 0, "error": 0, "skipped": 0}
    for item in items:
        key = item.get("status") if item.get("status") in counts else "queued"
        counts[key] += 1
    return {
        "batchId": batch["id"],
        "company": batch.get("company") or "",
        "dueDate": batch.get("dueDate") or "",
        "dateType": batch.get("dateType") or "",
        "status": batch.get("status") or "queued",
        "total": len(items),
        "completed": counts["done"],
        "failed": counts["error"],
        "skipped": counts["skipped"],
        "running": counts["running"],
        "queued": counts["queued"],
        "items": items,
    }


def mark_reorder_batch_running(batch_id):
    with _LOCK:
        batch = _BATCHES.get(str(batch_id or "").strip())
        if not batch:
            return
        batch["status"] = "running"
        batch["updatedAt"] = time.time()


def mark_reorder_item(batch_id, source_order, **fields):
    oid = normalize_order_id(source_order)
    with _LOCK:
        batch = _BATCHES.get(str(batch_id or "").strip())
        if not batch:
            return
        for item in batch.get("items") or []:
            if item.get("sourceOrder") == oid:
                item.update(fields)
                break
        batch["updatedAt"] = time.time()


def mark_reorder_batch_finished(batch_id):
    with _LOCK:
        batch = _BATCHES.get(str(batch_id or "").strip())
        if not batch:
            return
        has_error = any(
            (item or {}).get("status") == "error" for item in (batch.get("items") or [])
        )
        has_done = any(
            (item or {}).get("status") == "done" for item in (batch.get("items") or [])
        )
        if has_error and has_done:
            batch["status"] = "partial"
        elif has_error:
            batch["status"] = "error"
        else:
            batch["status"] = "done"
        batch["updatedAt"] = time.time()


def reset_reorder_batches_for_tests():
    with _LOCK:
        _BATCHES.clear()
