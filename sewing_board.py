"""Manual sewing calendar board: queue + weekday placements + unfinished rollover."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from zoneinfo import ZoneInfo

from embroidery_progress import compute_timing, expected_cycle_ms, load_all as load_embroidery_progress
from production_scheduler import (
    is_back_product,
    is_towel_or_needlepoint,
    iso_day,
    parse_date,
)

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_LOCK = Lock()
_BASE = Path(__file__).resolve().parent
DATA_DIR = _BASE / "data"
BOARD_PATH = DATA_DIR / "sewing_board.json"
WEEKDAY_HORIZON = 10
RESET_TOKEN = "scratch-2026-09-24"


def _now_et(now: Optional[datetime] = None) -> datetime:
    stamp = now or datetime.now(_ET)
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=_ET)
    return stamp.astimezone(_ET)


def today_iso(now: Optional[datetime] = None) -> str:
    return iso_day(_now_et(now).date())


def rolling_weekdays(now: Optional[datetime] = None, count: int = WEEKDAY_HORIZON) -> List[str]:
    """Left square is today when it is a weekday; weekends start on the next Monday."""
    cursor = _now_et(now).date()
    while cursor.weekday() > 4:
        cursor += timedelta(days=1)
    days: List[str] = []
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(iso_day(cursor))
        cursor += timedelta(days=1)
    return days


def _norm_oid(value: Any) -> str:
    text = str(value or "").replace("#", "").strip()
    if not text:
        return ""
    try:
        number = float(text)
        if abs(number - round(number)) < 1e-9:
            return str(int(round(number)))
    except (TypeError, ValueError):
        pass
    return text


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(str(value).replace(",", "").strip() or 0)))
    except (TypeError, ValueError):
        return default


def _date_iso(value: Any) -> str:
    parsed = parse_date(value)
    return iso_day(parsed) if parsed else _text(value)[:10]


def empty_board() -> dict:
    return {
        "queue": [],
        "days": {},
        "lastRolloverDate": "",
        "carryovers": [],
        "overdue": {},
        "resetToken": "",
        "updatedAt": "",
    }


def _unique(ids: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in ids:
        oid = _norm_oid(raw)
        if not oid or oid in seen:
            continue
        seen.add(oid)
        out.append(oid)
    return out


def _normalize_board(raw: Any) -> dict:
    data = raw if isinstance(raw, dict) else {}
    days = {}
    for key, value in (data.get("days") or {}).items():
        day = str(key or "")[:10]
        if not day:
            continue
        days[day] = _unique(value if isinstance(value, list) else [])
    carryovers = []
    for row in data.get("carryovers") or []:
        if not isinstance(row, dict):
            continue
        oid = _norm_oid(row.get("orderNumber"))
        if not oid:
            continue
        carryovers.append({
            "orderNumber": oid,
            "fromDate": str(row.get("fromDate") or "")[:10],
            "toDate": str(row.get("toDate") or "")[:10],
            "customer": _text(row.get("customer")),
            "product": _text(row.get("product")),
            "rolledAt": _text(row.get("rolledAt")),
        })
    overdue = {}
    for key, value in (data.get("overdue") or {}).items():
        oid = _norm_oid(key)
        day = str(value or "")[:10]
        if oid and day:
            overdue[oid] = day
    return {
        "queue": _unique(data.get("queue") or []),
        "days": days,
        "lastRolloverDate": str(data.get("lastRolloverDate") or "")[:10],
        "carryovers": carryovers,
        "overdue": overdue,
        "resetToken": _text(data.get("resetToken")),
        "updatedAt": _text(data.get("updatedAt")),
    }


def load_board() -> dict:
    if not BOARD_PATH.exists():
        return empty_board()
    try:
        with BOARD_PATH.open("r", encoding="utf-8") as handle:
            return _normalize_board(json.load(handle))
    except Exception:
        logger.exception("Could not read sewing board")
        return empty_board()


def save_board(board: dict) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    clean = _normalize_board(board)
    clean["updatedAt"] = datetime.now(_ET).isoformat()
    tmp = BOARD_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(clean, handle, indent=2, ensure_ascii=False)
    tmp.replace(BOARD_PATH)
    return clean


CLOSED_STAGES = {"SHIPPED", "COMPLETE", "COMPLETED", "CANCELED", "CANCELLED"}


def is_back_job(product: Any) -> bool:
    if is_back_product(product):
        return True
    name = " ".join(str(product or "").lower().replace("_", " ").replace("-", " ").split())
    return "back" in name


def is_closed_order(order: dict) -> bool:
    stage = _text(order.get("stage") or order.get("Stage") or order.get("status") or order.get("Status")).upper()
    if stage in CLOSED_STAGES:
        return True
    qty = _int(order.get("quantity") if order.get("quantity") is not None else order.get("Quantity"))
    shipped = _int(order.get("shipped") if order.get("shipped") is not None else order.get("Shipped"))
    return qty > 0 and shipped >= qty


def needs_sewing(order: dict) -> bool:
    if order.get("needs_sewing") is False or order.get("needsSewing") is False:
        return False
    product = order.get("product") or order.get("Product")
    if is_back_job(product) or is_towel_or_needlepoint(product):
        return False
    if is_closed_order(order):
        return False
    remaining = _int(
        order.get("remaining_quantity")
        if order.get("remaining_quantity") is not None
        else order.get("remainingQuantity")
    )
    return remaining > 0


def catalog_jobs(
    schedule: Optional[dict],
    progress: Optional[Dict[str, dict]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, dict]:
    """Open sewing jobs from the published schedule, with embroidery % / ETA."""
    stamp = _now_et(now)
    progress = progress if progress is not None else {}
    orders = list((schedule or {}).get("orders") or [])
    sewing_rows = {
        _norm_oid(row.get("orderNumber") or row.get("order_number")): row
        for row in (schedule or {}).get("sewing") or []
        if _norm_oid(row.get("orderNumber") or row.get("order_number"))
    }
    embroidery_rows = {
        _norm_oid(row.get("orderNumber") or row.get("order_number")): row
        for row in (schedule or {}).get("embroidery") or []
        if _norm_oid(row.get("orderNumber") or row.get("order_number"))
    }
    jobs: Dict[str, dict] = {}
    for order in orders:
        oid = _norm_oid(order.get("order_number") or order.get("orderNumber"))
        if not oid or not needs_sewing(order):
            continue
        sew = sewing_rows.get(oid) or {}
        emb = embroidery_rows.get(oid) or {}
        qty = _int(order.get("quantity") if order.get("quantity") is not None else sew.get("quantity"), 1)
        remaining = _int(
            order.get("remaining_quantity")
            if order.get("remaining_quantity") is not None
            else sew.get("remainingQuantity")
        )
        emb_remaining = _int(
            order.get("embroidery_remaining")
            if order.get("embroidery_remaining") is not None
            else order.get("embroideryRemaining")
        )
        stitch = _int(order.get("stitch_count") if order.get("stitch_count") is not None else sew.get("stitchCount"))
        machine = _text(emb.get("machine"))
        heads = 1 if machine in {"Machine 1", "Single Head", "Single Head Machine"} else 6
        prow = progress.get(oid) or {}
        completed = max(0, _int(prow.get("completedQty")))
        if qty > 0:
            completed = min(qty, completed)
        if emb_remaining <= 0 and completed <= 0 and _text(order.get("embroidery_status")).upper() == "COMPLETE":
            completed = qty
            emb_remaining = 0
        if emb_remaining <= 0 and completed > 0:
            emb_remaining = max(0, qty - completed)
        ready = emb_remaining <= 0 or _text(order.get("embroidery_status")).upper() == "COMPLETE"
        if sew.get("embroideryReady") is True and emb_remaining <= 0:
            ready = True
        percent = 100 if ready and qty else (round(100.0 * completed / qty, 1) if qty else 0)
        timing = compute_timing(prow, stitch, heads)
        remaining_ms = expected_cycle_ms(stitch, max(0, emb_remaining or (qty - completed)), heads)
        avg = _int(timing.get("avgCycleMs"))
        if avg > 0 and (emb_remaining or (qty - completed)) > 0:
            runs = max(1, -(-(max(0, emb_remaining or (qty - completed))) // max(1, heads)))
            guessed = avg * runs
            if guessed > 0:
                remaining_ms = guessed
        eta = ""
        if not ready and remaining_ms > 0:
            eta = (stamp + timedelta(milliseconds=remaining_ms)).isoformat()
        jobs[oid] = {
            "orderNumber": oid,
            "customer": _text(order.get("customer") or sew.get("customer")),
            "product": _text(order.get("product") or sew.get("product")),
            "design": _text(order.get("design") or sew.get("design")),
            "quantity": qty,
            "remainingQuantity": remaining,
            "dueDate": _date_iso(order.get("due_date") or sew.get("dueDate")),
            "requiredShipDate": _date_iso(order.get("required_ship_date") or sew.get("requiredShipDate")),
            "transitBusinessDays": _int(
                order.get("transit_business_days")
                if order.get("transit_business_days") is not None
                else sew.get("transitBusinessDays")
            ),
            "shippingMethod": _text(order.get("shipping_method") or sew.get("shippingMethod")),
            "shipCity": _text((order.get("shipping_address") or {}).get("city") or sew.get("shipCity")),
            "shipState": _text((order.get("shipping_address") or {}).get("state") or sew.get("shipState")),
            "hardDate": "HARD" in _text(order.get("due_type") or ("Hard Date" if sew.get("hardDate") else "")).upper(),
            "embroideryReady": ready,
            "embroideryRemaining": max(0, emb_remaining),
            "embroideryCompletedQty": completed,
            "embroideryPercent": percent,
            "embroideryEta": eta,
            "avgCycleMs": avg,
            "stitchCount": stitch,
            "headCount": heads,
            "machine": machine,
            "stage": _text(order.get("stage") or sew.get("stage")),
            "image": _text(order.get("image") or sew.get("image")),
            "imageFileId": _text(sew.get("imageFileId")),
        }
    return jobs


def seed_from_schedule(board: dict, schedule: Optional[dict], jobs: Dict[str, dict]) -> dict:
    """First visit: place published sewing dates, then leftover work goes to the queue."""
    clean = _normalize_board(board)
    if clean.get("resetToken") == RESET_TOKEN or clean["queue"] or any(clean["days"].values()):
        return clean
    days: Dict[str, List[str]] = {}
    placed = set()
    for row in (schedule or {}).get("sewing") or []:
        oid = _norm_oid(row.get("orderNumber") or row.get("order_number"))
        day = str(row.get("date") or row.get("start") or "")[:10]
        if not oid or oid not in jobs or not day or oid in placed:
            continue
        days.setdefault(day, []).append(oid)
        placed.add(oid)
    clean["days"] = days
    return apply_catalog(clean, jobs)


def clear_queue_overdue(board: dict) -> dict:
    """Returning a job to the queue clears its missed-day overdue flag."""
    clean = _normalize_board(board)
    overdue = dict(clean.get("overdue") or {})
    for oid in clean["queue"]:
        overdue.pop(oid, None)
    clean["overdue"] = overdue
    return clean


def stamp_overdue(jobs: Dict[str, dict], overdue: Optional[Dict[str, str]] = None) -> Dict[str, dict]:
    marks = overdue or {}
    for oid, job in jobs.items():
        job["overdue"] = oid in marks
        job["overdueFrom"] = marks.get(oid) or ""
    return jobs


def apply_catalog(board: dict, jobs: Dict[str, dict]) -> dict:
    """Keep placements for open jobs; new work lands in the queue."""
    clean = _normalize_board(board)
    live = set(jobs)
    clean["queue"] = [oid for oid in clean["queue"] if oid in live]
    next_days = {}
    placed = set(clean["queue"])
    for day, ids in clean["days"].items():
        kept = [oid for oid in ids if oid in live]
        next_days[day] = kept
        placed.update(kept)
    clean["days"] = next_days
    for oid in jobs:
        if oid not in placed:
            clean["queue"].append(oid)
    clean["carryovers"] = [row for row in clean["carryovers"] if row["orderNumber"] in live]
    clean["overdue"] = {oid: day for oid, day in (clean.get("overdue") or {}).items() if oid in live}
    return clean


def rollover_unfinished(
    board: dict,
    jobs: Dict[str, dict],
    *,
    now: Optional[datetime] = None,
) -> Tuple[dict, List[dict]]:
    """Move unfinished jobs from past weekdays to the top of today. Nothing else moves."""
    clean = apply_catalog(board, jobs)
    today = rolling_weekdays(now, 1)[0]
    if clean.get("lastRolloverDate") == today:
        return clean, list(clean.get("carryovers") or [])

    past_days = sorted(day for day in clean["days"] if day and day < today)
    rolled: List[str] = []
    carryovers: List[dict] = []
    overdue = dict(clean.get("overdue") or {})
    rolled_at = _now_et(now).isoformat()
    for day in past_days:
        leftovers = []
        for oid in clean["days"].get(day) or []:
            job = jobs.get(oid)
            if not job or job.get("remainingQuantity", 0) <= 0:
                continue
            leftovers.append(oid)
            carryovers.append({
                "orderNumber": oid,
                "fromDate": day,
                "toDate": today,
                "customer": job.get("customer") or "",
                "product": job.get("product") or "",
                "rolledAt": rolled_at,
            })
            overdue[oid] = day
        if leftovers:
            rolled.extend(leftovers)
        clean["days"].pop(day, None)

    if rolled:
        today_jobs = [oid for oid in (clean["days"].get(today) or []) if oid not in set(rolled)]
        clean["days"][today] = _unique(rolled + today_jobs)
        clean["queue"] = [oid for oid in clean["queue"] if oid not in set(rolled)]

    clean["lastRolloverDate"] = today
    clean["carryovers"] = carryovers
    clean["overdue"] = overdue
    return clear_queue_overdue(clean), carryovers


def save_placements(queue: Sequence[Any], days: Dict[str, Sequence[Any]], jobs: Dict[str, dict]) -> dict:
    board = load_board()
    incoming_days = {}
    for key, value in (days or {}).items():
        day = str(key or "")[:10]
        if day:
            incoming_days[day] = _unique(value)
    board["queue"] = _unique(queue)
    board["days"] = incoming_days
    board = apply_catalog(board, jobs)
    return save_board(clear_queue_overdue(board))


def board_reset_to_queue(jobs: Dict[str, dict], now: Optional[datetime] = None) -> dict:
    """In-memory wipe: every open sewing job starts in the queue."""
    board = empty_board()
    board["queue"] = _unique(jobs)
    board["resetToken"] = RESET_TOKEN
    board["lastRolloverDate"] = rolling_weekdays(now, 1)[0]
    return apply_catalog(board, jobs)


def reset_to_queue(jobs: Dict[str, dict], now: Optional[datetime] = None) -> dict:
    """Wipe day placements so every open sewing job starts in the queue."""
    return save_board(board_reset_to_queue(jobs, now))


def snapshot(
    schedule: Optional[dict],
    *,
    now: Optional[datetime] = None,
    persist: bool = True,
    progress: Optional[Dict[str, dict]] = None,
) -> dict:
    with _LOCK:
        jobs = catalog_jobs(schedule, progress if progress is not None else load_embroidery_progress(), now)
        board = load_board()
        if board.get("resetToken") != RESET_TOKEN:
            board = reset_to_queue(jobs, now)
        else:
            board = seed_from_schedule(board, schedule, jobs)
            board, carryovers = rollover_unfinished(board, jobs, now=now)
            if persist:
                board = save_board(board)
        carryovers = list(board.get("carryovers") or [])
        overdue = board.get("overdue") or {}
        stamp_overdue(jobs, overdue)
        days = rolling_weekdays(now)
        return {
            "today": days[0] if days else today_iso(now),
            "days": days,
            "queue": board["queue"],
            "board": board["days"],
            "jobs": jobs,
            "carryovers": carryovers,
            "overdue": overdue,
            "updatedAt": board.get("updatedAt") or "",
            "lastRolloverDate": board.get("lastRolloverDate") or "",
        }
