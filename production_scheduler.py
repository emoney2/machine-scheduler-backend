"""Deterministic backward production scheduler for JR & Co.

The engine has no Flask, Google Sheets, UPS, email, or OpenAI dependencies.
Adapters enrich real order rows before calling :func:`build_schedule`; this
keeps all production math deterministic and unit-testable.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("America/New_York")
EMBROIDERY_HEADS = 6
EMBROIDERY_MACHINES = ("Machine 1", "Machine 2", "Machine 3")
EMBROIDERY_SPEED = 30_000.0
EMBROIDERY_SETUP_HOURS = 0.5
SEWING_DAY_START = time(8, 30)
SEWING_DAY_END = time(16, 0)  # 7.5 productive hours
EMBROIDERY_DAY_START = time(8, 30)
EMBROIDERY_DAY_END = time(16, 30)

TERMINAL_STAGES = {"COMPLETE", "COMPLETED", "CANCELED", "CANCELLED", "SHIPPED"}
HELD_STAGES = {"HOLD", "HELD", "ON HOLD"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float = 0.0) -> float:
    if value is None or value is False or value == "":
        return default
    try:
        n = float(str(value).replace(",", "").strip())
        return n if math.isfinite(n) else default
    except (TypeError, ValueError):
        return default


def normalize_order_number(value: Any) -> str:
    raw = _text(value).lstrip("#")
    try:
        n = float(raw)
        if abs(n - round(n)) < 1e-9:
            return str(int(round(n)))
    except (TypeError, ValueError):
        pass
    return raw


def normalize_customer(value: Any) -> str:
    """Exact normalized customer key; deliberately avoids fuzzy matching."""
    return re.sub(r"[^a-z0-9]+", " ", _text(value).lower()).strip()


def parse_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(BUSINESS_TZ).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return date(1899, 12, 30) + timedelta(days=float(value))
        except (TypeError, ValueError, OverflowError):
            return None
    raw = _text(value).split("T", 1)[0].split(" ", 1)[0].replace(",", "")
    if raw and raw[0].isdigit() and "/" not in raw and raw.count("-") == 0:
        try:
            return date(1899, 12, 30) + timedelta(days=float(raw))
        except (TypeError, ValueError, OverflowError):
            return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def iso_day(value: Optional[date]) -> str:
    return value.isoformat() if value else ""


def workday(value: date, holidays: Iterable[date]) -> bool:
    return value.weekday() < 5 and value not in set(holidays)


def previous_workday(value: date, holidays: Iterable[date], include: bool = False) -> date:
    cursor = value if include else value - timedelta(days=1)
    blocked = set(holidays)
    while not workday(cursor, blocked):
        cursor -= timedelta(days=1)
    return cursor


def next_workday(value: date, holidays: Iterable[date], include: bool = False) -> date:
    cursor = value if include else value + timedelta(days=1)
    blocked = set(holidays)
    while not workday(cursor, blocked):
        cursor += timedelta(days=1)
    return cursor


def subtract_workdays(value: date, days: int, holidays: Iterable[date]) -> date:
    cursor = value
    for _ in range(max(0, int(days))):
        cursor = previous_workday(cursor, holidays)
    return cursor


def embroidery_runs(quantity: Any, heads: int = EMBROIDERY_HEADS) -> int:
    qty = max(0, int(math.ceil(_number(quantity))))
    return int(math.ceil(qty / max(1, heads))) if qty else 0


def embroidery_hours(
    quantity: Any,
    stitch_count: Any,
    heads: int = EMBROIDERY_HEADS,
    include_setup: bool = True,
) -> float:
    runs = embroidery_runs(quantity, heads)
    stitches = _number(stitch_count)
    if runs <= 0 or stitches <= 0:
        return 0.0
    return (runs * stitches / EMBROIDERY_SPEED) + (
        EMBROIDERY_SETUP_HOURS if include_setup else 0.0
    )


def can_split_embroidery_job(
    thread_usage_cones: Dict[str, float],
    available_cones: Dict[str, int],
) -> Tuple[bool, str]:
    """Return whether a split avoids buying cones solely for the split.

    Every required color must already consume more than one cone per head
    (>6 cone-equivalents total) and at least 12 loadable cones must be on hand.
    Missing usage data is review-only, never silently allowed.
    """
    if not thread_usage_cones:
        return False, "Thread consumption is missing; administrator review required"
    for color, usage in thread_usage_cones.items():
        if _number(usage) <= EMBROIDERY_HEADS:
            return False, f"{color} does not require more than one cone per head"
        if int(_number(available_cones.get(color))) < EMBROIDERY_HEADS * 2:
            return False, f"{color} does not have 12 loadable cones available"
    return True, "Existing consumption and inventory support a split without an extra purchase"


@dataclass
class SchedulerConfig:
    regular_sewing_capacity: float = 95.0
    emergency_sewing_capacity: float = 50.0
    approved_emergency_dates: set[date] = field(default_factory=set)
    holidays: set[date] = field(default_factory=set)
    product_factors: Dict[str, float] = field(default_factory=dict)
    french_seam_factor: Optional[float] = None
    unusual_shape_factor: Optional[float] = None
    sewing_changeover_minutes: float = 5.0
    timezone: str = "America/New_York"

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "SchedulerConfig":
        raw = raw or {}
        return cls(
            regular_sewing_capacity=max(1.0, _number(raw.get("regularSewingCapacity"), 95.0)),
            emergency_sewing_capacity=max(0.0, _number(raw.get("emergencySewingCapacity"), 50.0)),
            approved_emergency_dates={
                d for d in (parse_date(v) for v in raw.get("approvedEmergencyDates", [])) if d
            },
            holidays={d for d in (parse_date(v) for v in raw.get("holidays", [])) if d},
            product_factors={
                _text(k).lower(): max(0.01, _number(v, 1.0))
                for k, v in (raw.get("productFactors") or {}).items()
                if _text(k)
            },
            french_seam_factor=(
                max(0.01, _number(raw.get("frenchSeamFactor"), 1.0))
                if raw.get("frenchSeamFactor") not in (None, "")
                else None
            ),
            unusual_shape_factor=(
                max(0.01, _number(raw.get("unusualShapeFactor"), 1.0))
                if raw.get("unusualShapeFactor") not in (None, "")
                else None
            ),
            sewing_changeover_minutes=max(0.0, _number(raw.get("sewingChangeoverMinutes"), 5.0)),
        )

    def as_dict(self) -> dict:
        return {
            "regularSewingCapacity": self.regular_sewing_capacity,
            "emergencySewingCapacity": self.emergency_sewing_capacity,
            "approvedEmergencyDates": sorted(iso_day(d) for d in self.approved_emergency_dates),
            "holidays": sorted(iso_day(d) for d in self.holidays),
            "productFactors": dict(self.product_factors),
            "frenchSeamFactor": self.french_seam_factor,
            "unusualShapeFactor": self.unusual_shape_factor,
            "sewingChangeoverMinutes": self.sewing_changeover_minutes,
            "timezone": self.timezone,
        }


def _flag(row: dict, *names: str) -> bool:
    for name in names:
        raw = row.get(name)
        if raw is True:
            return True
        if _text(raw).lower() in {"1", "true", "yes", "y", "x", "checked"}:
            return True
    return False


def _thread_codes(value: Any) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for hit in re.findall(r"\b(\d{4})\b", _text(value)):
        if hit not in seen:
            seen.add(hit)
            out.append(hit)
    return out


def _stage_priority(stage: str) -> int:
    token = stage.upper()
    if "SEW" in token:
        return 0
    if "EMBROID" in token:
        return 1
    return 2


def _priority(order: dict) -> tuple:
    hard = 0 if "HARD" in order["due_type"].upper() else 1
    rush = 0 if order["rush"] else 1
    ship = order["required_ship_date"] or date.max
    created = order["order_date"] or date.max
    try:
        oid = int(order["order_number"])
    except (TypeError, ValueError):
        oid = 10**15
    return (hard, rush, ship, _stage_priority(order["stage"]), created, oid)


def _capacity_factor(row: dict, config: SchedulerConfig, warnings: List[dict]) -> float:
    product = _text(row.get("Product"))
    factor = config.product_factors.get(product.lower(), 1.0)
    french = _flag(row, "French Seam", "French Seams", "French-seam")
    unusual = _flag(row, "Unusual Shape", "Custom Shape", "Unusual/Custom Shape")
    oid = normalize_order_number(row.get("Order #"))
    if french:
        if config.french_seam_factor is None:
            warnings.append({
                "type": "capacity_factor_missing",
                "severity": "warning",
                "orderNumber": oid,
                "message": "French-seam factor is not configured; standard factor used",
            })
        else:
            factor *= config.french_seam_factor
    if unusual:
        if config.unusual_shape_factor is None:
            warnings.append({
                "type": "capacity_factor_missing",
                "severity": "warning",
                "orderNumber": oid,
                "message": "Unusual-shape factor is not configured; standard factor used",
            })
        else:
            factor *= config.unusual_shape_factor
    return factor


def normalize_orders(rows: Sequence[dict], config: SchedulerConfig) -> Tuple[List[dict], List[dict]]:
    orders: List[dict] = []
    warnings: List[dict] = []
    for raw in rows:
        oid = normalize_order_number(raw.get("Order #"))
        if not oid:
            continue
        stage = _text(raw.get("Stage"))
        stage_token = stage.upper()
        if stage_token in TERMINAL_STAGES or stage_token in HELD_STAGES:
            continue
        qty = max(0, int(math.ceil(_number(raw.get("Quantity")))))
        shipped = max(0, int(math.floor(_number(raw.get("Shipped")))))
        remaining = max(0, qty - shipped)
        if remaining <= 0:
            continue
        stitches = max(0, int(round(_number(raw.get("Stitch Count")))))
        due = parse_date(raw.get("Due Date"))
        ship = parse_date(raw.get("_required_ship_date") or raw.get("Ship Date"))
        address = raw.get("_shipping_address") if isinstance(raw.get("_shipping_address"), dict) else {}
        factor = _capacity_factor(raw, config, warnings)
        emb_done = max(0, int(_number(raw.get("Embroidery Completed Qty"))))
        sewing_done = max(0, int(_number(raw.get("_sewing_completed_qty"))))
        if not due:
            warnings.append({
                "type": "missing_due_date", "severity": "warning", "orderNumber": oid,
                "message": "Due date / required in-hand date is missing; sewing will still be placed",
            })
        if not ship:
            warnings.append({
                "type": "missing_ship_date", "severity": "warning", "orderNumber": oid,
                "message": "Required ship date could not be calculated; due date or the next workday was used",
            })
        if not address.get("addr1"):
            warnings.append({
                "type": "shipping_address_required", "severity": "warning", "orderNumber": oid,
                "message": "Shipping address required",
            })
        if stitches <= 0 and "SEW" not in stage_token:
            warnings.append({
                "type": "missing_stitch_count", "severity": "warning", "orderNumber": oid,
                "message": "Stitch count is missing; embroidery will stay unscheduled until it is available",
            })
        thread_codes = _thread_codes(raw.get("Threads"))
        if not thread_codes and "SEW" not in stage_token:
            warnings.append({
                "type": "missing_thread_data", "severity": "warning", "orderNumber": oid,
                "message": "Thread colors are missing; embroidery will stay unscheduled until they are available",
            })
        material_warnings = list(raw.get("_material_warnings") or [])
        orders.append({
            "order_number": oid,
            "customer": _text(raw.get("Company Name")),
            "customer_key": normalize_customer(raw.get("Company Name")),
            "product": _text(raw.get("Product")),
            "design": _text(raw.get("Design")),
            "quantity": qty,
            "remaining_quantity": max(0, remaining - sewing_done),
            "embroidery_remaining": max(0, remaining - emb_done),
            "stitch_count": stitches,
            "thread_colors": thread_codes,
            "thread_usage_cones": {
                _text(k): max(0.0, _number(v))
                for k, v in (raw.get("_thread_usage_cones") or {}).items()
            },
            "due_date": due,
            "in_hand_date": parse_date(raw.get("In-Hand Date")) or due,
            "required_ship_date": ship,
            "transit_business_days": int(_number(raw.get("_transit_business_days"), 0)),
            "shipping_method": _text(raw.get("_shipping_method") or raw.get("Shipping Method") or "UPS Ground"),
            "shipping_address": address,
            "stage": stage,
            "due_type": _text(raw.get("Hard Date/Soft Date")),
            "rush": _flag(raw, "Rush", "Rush Order") or "RUSH" in _text(raw.get("Notes")).upper(),
            "order_date": parse_date(raw.get("Date")),
            "sewing_factor": factor,
            "sewing_units": max(0.0, (max(0, remaining - sewing_done) * factor)),
            "french_seam": _flag(raw, "French Seam", "French Seams", "French-seam"),
            "unusual_shape": _flag(raw, "Unusual Shape", "Custom Shape", "Unusual/Custom Shape"),
            "materials_ready": not material_warnings,
            "material_warnings": material_warnings,
            "readiness_override": bool(raw.get("_readiness_override")),
            "explicit_group_id": _text(raw.get("_shipping_group_id") or raw.get("Shipping Group")),
            "image": _text(raw.get("Image") or raw.get("Preview")),
        })
    orders.sort(key=_priority)
    return orders, warnings


def detect_shipping_groups(orders: Sequence[dict]) -> Tuple[List[dict], List[dict]]:
    """Group exact normalized customers with consecutive integer order numbers."""
    warnings: List[dict] = []
    explicit: Dict[str, List[dict]] = defaultdict(list)
    ungrouped: List[dict] = []
    for order in orders:
        if order["explicit_group_id"]:
            explicit[order["explicit_group_id"]].append(order)
        else:
            ungrouped.append(order)
    groups: List[dict] = []
    for gid, members in explicit.items():
        groups.append({"id": gid, "source": "explicit", "orders": members})

    by_customer: Dict[str, List[dict]] = defaultdict(list)
    for order in ungrouped:
        by_customer[order["customer_key"]].append(order)
    for customer_key, members in by_customer.items():
        members.sort(key=lambda o: int(o["order_number"]) if o["order_number"].isdigit() else 10**15)
        chunk: List[dict] = []
        for order in members:
            if not chunk:
                chunk = [order]
                continue
            prev, current = chunk[-1]["order_number"], order["order_number"]
            if prev.isdigit() and current.isdigit() and int(current) == int(prev) + 1:
                chunk.append(order)
            else:
                _append_inferred_group(groups, chunk, customer_key)
                chunk = [order]
        _append_inferred_group(groups, chunk, customer_key)

    for group in groups:
        dates = [o["required_ship_date"] for o in group["orders"] if o["required_ship_date"]]
        group["required_ship_date"] = min(dates) if dates else None
        if len(set(dates)) > 1:
            warnings.append({
                "type": "shipping_group_date_conflict",
                "severity": "warning",
                "groupId": group["id"],
                "orderNumbers": [o["order_number"] for o in group["orders"]],
                "message": "Grouped orders had different ship dates; earliest date was used",
            })
        for order in group["orders"]:
            order["shipping_group_id"] = group["id"]
            order["shipping_group_source"] = group["source"]
            order["required_ship_date"] = group["required_ship_date"]
    groups.sort(key=lambda g: (_priority(g["orders"][0]), g["id"]))
    return groups, warnings


def _append_inferred_group(groups: List[dict], chunk: List[dict], customer_key: str) -> None:
    if not chunk:
        return
    if len(chunk) > 1 and customer_key:
        digest = hashlib.sha1(
            f"{customer_key}|{'|'.join(o['order_number'] for o in chunk)}".encode("utf-8")
        ).hexdigest()[:10]
        groups.append({"id": f"AUTO-{digest}", "source": "inferred", "orders": list(chunk)})
    else:
        order = chunk[0]
        groups.append({"id": f"ORDER-{order['order_number']}", "source": "single", "orders": [order]})


def _at(day: date, value: time) -> datetime:
    return datetime.combine(day, value, BUSINESS_TZ)


def _setup_units(config: SchedulerConfig) -> float:
    # Planned capacity is 95 pieces across 15 productive labor-hours.
    pieces_per_minute = config.regular_sewing_capacity / (2 * 7.5 * 60.0)
    return config.sewing_changeover_minutes * pieces_per_minute


def _reserve_locked_sewing(
    locks: Sequence[dict], config: SchedulerConfig
) -> Tuple[Dict[date, float], List[dict]]:
    reserved: Dict[date, float] = defaultdict(float)
    normalized: List[dict] = []
    for lock in locks or []:
        day = parse_date(lock.get("date") or lock.get("plannedDate"))
        if not day:
            continue
        units = max(0.0, _number(lock.get("capacityUnits") or lock.get("units")))
        reserved[day] += units
        start = _at(day, SEWING_DAY_START)
        finish = min(
            _at(day, SEWING_DAY_END),
            start + timedelta(
                minutes=(units / max(config.regular_sewing_capacity, 1.0)) * 7.5 * 60.0
            ),
        )
        normalized.append({
            "orderNumber": normalize_order_number(lock.get("orderNumber")),
            "date": iso_day(day),
            "start": start.isoformat(),
            "finish": finish.isoformat(),
            "capacityUnits": round(units, 4),
            "locked": True,
            "lockId": _text(lock.get("lockId") or lock.get("id")),
        })
    return reserved, normalized


def _sewing_capacity(cursor: date, config: SchedulerConfig, reserved: Dict[date, float], day_job_count: Dict[date, int], setup_units: float):
    regular = config.regular_sewing_capacity
    emergency = (
        config.emergency_sewing_capacity
        if cursor in config.approved_emergency_dates
        else 0.0
    )
    total = regular + emergency
    already = reserved[cursor]
    setup = setup_units if day_job_count[cursor] > 0 else 0.0
    free = max(0.0, total - already - setup)
    return regular, emergency, total, already, setup, free


def _sewing_entry(
    order: dict,
    group: dict,
    group_ship: date,
    cursor: date,
    used: float,
    setup: float,
    regular: float,
    emergency: float,
    already: float,
    total: float,
) -> dict:
    end_fraction = max(0.0, min(1.0, (already + setup + used) / max(total, 1e-9)))
    start_fraction = max(0.0, min(1.0, (already + setup) / max(total, 1e-9)))
    productive_minutes = 7.5 * 60.0
    start_dt = _at(cursor, SEWING_DAY_START) + timedelta(minutes=start_fraction * productive_minutes)
    end_dt = _at(cursor, SEWING_DAY_START) + timedelta(minutes=end_fraction * productive_minutes)
    return {
        "orderNumber": order["order_number"],
        "customer": order["customer"],
        "product": order["product"],
        "design": order["design"],
        "shippingGroupId": group["id"],
        "shippingGroupSource": group["source"],
        "date": iso_day(cursor),
        "start": start_dt.isoformat(),
        "finish": min(end_dt, _at(cursor, SEWING_DAY_END)).isoformat(),
        "quantity": order["quantity"],
        "remainingQuantity": order["remaining_quantity"],
        "capacityUnits": round(used, 4),
        "setupUnits": round(setup, 4),
        "regularCapacity": regular,
        "emergencyCapacity": emergency,
        "scheduledCapacity": round(already + setup + used, 4),
        "remainingCapacity": round(max(0.0, total - already - setup - used), 4),
        "dueDate": iso_day(order["due_date"]),
        "inHandDate": iso_day(order["in_hand_date"]),
        "requiredShipDate": iso_day(group_ship),
        "embroideryReady": False,
        "materialsReady": order["materials_ready"],
        "materialsWarnings": order["material_warnings"],
        "readinessOverride": order["readiness_override"],
        "frenchSeam": order["french_seam"],
        "unusualShape": order["unusual_shape"],
        "rush": order["rush"],
        "hardDate": "HARD" in order["due_type"].upper(),
        "locked": False,
        "conflict": False,
        "late": False,
        "image": order["image"],
    }


def _resolve_group_ship(group: dict, planning_start: date, holidays: set[date]) -> date:
    group_ship = group.get("required_ship_date")
    if group_ship:
        return group_ship
    dues = [order["due_date"] for order in group["orders"] if order.get("due_date")]
    if dues:
        group_ship = min(dues)
    else:
        group_ship = next_workday(planning_start, holidays, include=True)
    group["required_ship_date"] = group_ship
    for order in group["orders"]:
        order["required_ship_date"] = group_ship
    return group_ship


def _schedule_sewing(
    groups: Sequence[dict],
    config: SchedulerConfig,
    locks: Sequence[dict],
    planning_start: date,
) -> Tuple[List[dict], List[dict], Dict[str, datetime]]:
    conflicts: List[dict] = []
    entries: List[dict] = []
    reserved, lock_entries = _reserve_locked_sewing(locks, config)
    entries.extend(lock_entries)
    locks_by_order: Dict[str, List[dict]] = defaultdict(list)
    for entry in lock_entries:
        if entry.get("orderNumber"):
            locks_by_order[entry["orderNumber"]].append(entry)
    day_job_count: Dict[date, int] = defaultdict(int)
    for lock in lock_entries:
        day = parse_date(lock["date"])
        if day:
            day_job_count[day] += 1
    embroidery_deadlines: Dict[str, datetime] = {}
    setup_units = _setup_units(config)

    def take_day(cursor: date, order: dict, group: dict, group_ship: date, remaining_units: float):
        regular, emergency, total, already, setup, free = _sewing_capacity(
            cursor, config, reserved, day_job_count, setup_units
        )
        if free <= 1e-9:
            return remaining_units, None
        used = min(free, remaining_units)
        entry = _sewing_entry(
            order, group, group_ship, cursor, used, setup, regular, emergency, already, total
        )
        reserved[cursor] += setup + used
        day_job_count[cursor] += 1
        return remaining_units - used, entry

    for group in groups:
        group_ship = _resolve_group_ship(group, planning_start, config.holidays)
        group_entries: List[dict] = []
        for order in sorted(group["orders"], key=_priority):
            fixed = locks_by_order.get(order["order_number"]) or []
            if fixed:
                fixed.sort(key=lambda e: e["start"])
                for entry in fixed:
                    entry.update({
                        "customer": order["customer"],
                        "product": order["product"],
                        "design": order["design"],
                        "shippingGroupId": group["id"],
                        "requiredShipDate": iso_day(group_ship),
                        "quantity": order["quantity"],
                        "remainingQuantity": order["remaining_quantity"],
                        "materialsReady": order["materials_ready"],
                        "materialsWarnings": order["material_warnings"],
                        "readinessOverride": order["readiness_override"],
                        "frenchSeam": order["french_seam"],
                        "unusualShape": order["unusual_shape"],
                        "rush": order["rush"],
                        "hardDate": "HARD" in order["due_type"].upper(),
                        "conflict": False,
                    })
                group_entries.extend(fixed)
                embroidery_deadlines[order["order_number"]] = datetime.fromisoformat(fixed[0]["start"])
                continue
            remaining_units = order["sewing_units"]
            if remaining_units <= 1e-9:
                embroidery_deadlines[order["order_number"]] = _at(group_ship, SEWING_DAY_START)
                continue
            cursor = previous_workday(group_ship, config.holidays, include=True)
            order_entries: List[dict] = []
            guard = 0
            while remaining_units > 1e-9 and guard < 5000:
                guard += 1
                if cursor < planning_start:
                    break
                remaining_units, entry = take_day(cursor, order, group, group_ship, remaining_units)
                if entry:
                    order_entries.append(entry)
                    cursor = previous_workday(cursor, config.holidays)
                else:
                    cursor = previous_workday(cursor, config.holidays)
            overflow = remaining_units
            late = overflow > 1e-9
            if remaining_units > 1e-9:
                cursor = next_workday(planning_start, config.holidays, include=True)
                while remaining_units > 1e-9 and guard < 5000:
                    guard += 1
                    remaining_units, entry = take_day(cursor, order, group, group_ship, remaining_units)
                    if entry:
                        order_entries.append(entry)
                    cursor = next_workday(cursor, config.holidays)
            if not order_entries:
                conflicts.append({
                    "type": "sewing_unscheduled",
                    "severity": "blocking",
                    "orderNumber": order["order_number"],
                    "missingSewingUnits": round(remaining_units, 2),
                    "message": "Sewing work could not be placed on any workday",
                })
                continue
            order_entries.sort(key=lambda e: e["start"])
            if late:
                finish = order_entries[-1]["finish"]
                emergency_days_needed = int(
                    math.ceil(overflow / max(config.emergency_sewing_capacity, 1.0))
                )
                for entry in order_entries:
                    entry["conflict"] = True
                    entry["late"] = True
                conflicts.append({
                    "type": "sewing_unscheduled",
                    "severity": "blocking",
                    "orderNumber": order["order_number"],
                    "missingSewingUnits": 0,
                    "expectedCompletion": finish,
                    "requiredShipDate": iso_day(group_ship),
                    "thirdSewerWouldHelp": config.emergency_sewing_capacity > 0,
                    "thirdSewerDaysNeeded": emergency_days_needed,
                    "message": (
                        f"Sewing is scheduled but finishes {fmt_conflict_day(finish)} after "
                        f"required ship date {iso_day(group_ship)}"
                    ),
                })
            entries.extend(order_entries)
            group_entries.extend(order_entries)
            embroidery_deadlines[order["order_number"]] = datetime.fromisoformat(order_entries[0]["start"])

        if group_entries:
            finish = max(datetime.fromisoformat(e["finish"]) for e in group_entries)
            ship_end = _at(group_ship, SEWING_DAY_END)
            if finish > ship_end:
                conflicts.append({
                    "type": "sewing_deadline",
                    "severity": "blocking",
                    "groupId": group["id"],
                    "orderNumbers": [o["order_number"] for o in group["orders"]],
                    "expectedCompletion": finish.isoformat(),
                    "requiredShipDate": iso_day(group_ship),
                    "message": "Shipping group cannot finish sewing by its required ship date",
                })

    entries.sort(key=lambda e: (e.get("date", ""), e.get("start", ""), e.get("orderNumber", "")))
    return entries, conflicts, embroidery_deadlines


def fmt_conflict_day(value: str) -> str:
    return str(value)[:10]


def _split_work_backward(
    deadline: datetime, hours: float, holidays: set[date]
) -> Tuple[datetime, datetime, List[dict]]:
    finish = min(deadline, _at(deadline.date(), EMBROIDERY_DAY_END))
    if not workday(finish.date(), holidays) or finish <= _at(finish.date(), EMBROIDERY_DAY_START):
        day = previous_workday(finish.date(), holidays)
        finish = _at(day, EMBROIDERY_DAY_END)
    cursor = finish
    remaining = max(0.0, hours)
    segments: List[dict] = []
    guard = 0
    while remaining > 1e-9 and guard < 5000:
        guard += 1
        day_start = _at(cursor.date(), EMBROIDERY_DAY_START)
        available = max(0.0, (cursor - day_start).total_seconds() / 3600.0)
        if available <= 1e-9:
            cursor = _at(previous_workday(cursor.date(), holidays), EMBROIDERY_DAY_END)
            continue
        used = min(available, remaining)
        start = cursor - timedelta(hours=used)
        segments.append({"date": iso_day(cursor.date()), "start": start, "finish": cursor, "hours": used})
        cursor = start
        remaining -= used
        if remaining > 1e-9:
            cursor = _at(previous_workday(cursor.date(), holidays), EMBROIDERY_DAY_END)
    segments.reverse()
    return cursor, finish, segments


def _split_work_forward(
    start_at: datetime, hours: float, holidays: set[date]
) -> Tuple[datetime, datetime, List[dict]]:
    cursor = start_at
    if not workday(cursor.date(), holidays) or cursor >= _at(cursor.date(), EMBROIDERY_DAY_END):
        cursor = _at(next_workday(cursor.date(), holidays), EMBROIDERY_DAY_START)
    elif cursor < _at(cursor.date(), EMBROIDERY_DAY_START):
        cursor = _at(cursor.date(), EMBROIDERY_DAY_START)
    remaining = max(0.0, hours)
    segments: List[dict] = []
    start = cursor
    guard = 0
    while remaining > 1e-9 and guard < 5000:
        guard += 1
        day_end = _at(cursor.date(), EMBROIDERY_DAY_END)
        available = max(0.0, (day_end - cursor).total_seconds() / 3600.0)
        if available <= 1e-9:
            cursor = _at(next_workday(cursor.date(), holidays), EMBROIDERY_DAY_START)
            continue
        used = min(available, remaining)
        finish = cursor + timedelta(hours=used)
        segments.append({"date": iso_day(cursor.date()), "start": cursor, "finish": finish, "hours": used})
        remaining -= used
        cursor = finish
        if remaining > 1e-9:
            cursor = _at(next_workday(cursor.date(), holidays), EMBROIDERY_DAY_START)
    finish = segments[-1]["finish"] if segments else start
    return start, finish, segments


def _overlap(a_start: datetime, a_finish: datetime, b_start: datetime, b_finish: datetime) -> bool:
    return a_start < b_finish and b_start < a_finish


def _thread_slot_allowed(
    order: dict,
    start: datetime,
    finish: datetime,
    placed: Sequence[dict],
    inventory: Dict[str, dict],
) -> Tuple[bool, List[dict]]:
    problems: List[dict] = []
    for color in order["thread_colors"]:
        inv = inventory.get(color) or {}
        available = int(_number(inv.get("cones"), 0))
        simultaneous = [
            p for p in placed
            if color in p.get("threadColors", [])
            and _overlap(start, finish, p["_start"], p["_finish"])
        ]
        required = EMBROIDERY_HEADS * (1 + len({p["machine"] for p in simultaneous}))
        if required > available:
            problems.append({
                "type": "thread_cone_conflict",
                "severity": "blocking",
                "color": color,
                "availableCones": available,
                "requiredCones": required,
                "affectedMachines": sorted({p["machine"] for p in simultaneous}),
                "affectedJobs": sorted({p["orderNumber"] for p in simultaneous} | {order["order_number"]}),
                "message": f"Thread {color}: {required} cones required, {available} available",
            })
    return not problems, problems


def _schedule_embroidery(
    orders: Sequence[dict],
    config: SchedulerConfig,
    deadlines: Dict[str, datetime],
    thread_inventory: Dict[str, dict],
    planning_start: datetime,
) -> Tuple[List[dict], List[dict]]:
    placed: List[dict] = []
    conflicts: List[dict] = []
    machine_deadlines: Dict[str, datetime] = {
        m: datetime.max.replace(tzinfo=BUSINESS_TZ) for m in EMBROIDERY_MACHINES
    }
    for order in sorted(orders, key=_priority):
        if order["embroidery_remaining"] <= 0 or "SEW" in order["stage"].upper():
            continue
        deadline = deadlines.get(order["order_number"])
        if not deadline:
            continue
        hours = embroidery_hours(order["embroidery_remaining"], order["stitch_count"])
        if hours <= 0:
            continue
        candidates: List[tuple] = []
        rejected_thread: List[dict] = []
        for machine in EMBROIDERY_MACHINES:
            effective_deadline = min(deadline, machine_deadlines[machine])
            start, finish, segments = _split_work_backward(
                effective_deadline, hours, config.holidays
            )
            allowed, problems = _thread_slot_allowed(
                order, start, finish, placed, thread_inventory
            )
            if allowed:
                candidates.append((start, machine, finish, segments))
            else:
                rejected_thread.extend(problems)
        if not candidates:
            # Keep work visible on the least-late machine and surface exact conflict.
            machine = max(EMBROIDERY_MACHINES, key=lambda m: machine_deadlines[m])
            start, finish, segments = _split_work_backward(
                min(deadline, machine_deadlines[machine]), hours, config.holidays
            )
            conflicts.extend(_dedupe_conflicts(rejected_thread))
            conflict = True
        else:
            start, machine, finish, segments = max(candidates, key=lambda c: c[0])
            conflict = False
        if start < planning_start:
            start, finish, segments = _split_work_forward(planning_start, hours, config.holidays)
            conflict = True
        machine_deadlines[machine] = start
        split_allowed, split_reason = can_split_embroidery_job(
            order["thread_usage_cones"],
            {c: int(_number((thread_inventory.get(c) or {}).get("cones"))) for c in order["thread_colors"]},
        )
        entry = {
            "machine": machine,
            "orderNumber": order["order_number"],
            "customer": order["customer"],
            "product": order["product"],
            "design": order["design"],
            "quantity": order["embroidery_remaining"],
            "runs": embroidery_runs(order["embroidery_remaining"]),
            "stitchCount": order["stitch_count"],
            "durationHours": round(hours, 4),
            "start": start.isoformat(),
            "finish": finish.isoformat(),
            "segments": [
                {**s, "start": s["start"].isoformat(), "finish": s["finish"].isoformat(), "hours": round(s["hours"], 4)}
                for s in segments
            ],
            "threadColors": order["thread_colors"],
            "threadUsageCones": order["thread_usage_cones"],
            "threadConflict": conflict,
            "sewingReadyDeadline": deadline.isoformat(),
            "sameDaySewing": finish.date() == deadline.date() and finish <= deadline,
            "splitPermitted": split_allowed,
            "splitReason": split_reason,
            "image": order["image"],
            "_start": start,
            "_finish": finish,
        }
        placed.append(entry)
        if start < planning_start:
            conflicts.append({
                "type": "embroidery_capacity",
                "severity": "blocking",
                "orderNumber": order["order_number"],
                "missingEmbroideryHours": round(
                    min(hours, (planning_start - start).total_seconds() / 3600.0), 2
                ),
                "requiredStart": start.isoformat(),
                "earliestAvailable": planning_start.isoformat(),
                "message": "Three-machine embroidery capacity cannot meet the sewing-ready deadline",
            })
        if finish > deadline:
            conflicts.append({
                "type": "embroidery_deadline",
                "severity": "blocking",
                "orderNumber": order["order_number"],
                "missingEmbroideryHours": round((finish - deadline).total_seconds() / 3600.0, 2),
                "expectedCompletion": finish.isoformat(),
                "requiredCompletion": deadline.isoformat(),
                "message": "Embroidery cannot finish before sewing must begin",
            })
    for row in placed:
        row.pop("_start", None)
        row.pop("_finish", None)
    placed.sort(key=lambda e: (e["start"], e["machine"], e["orderNumber"]))
    return placed, _dedupe_conflicts(conflicts)


def _dedupe_conflicts(conflicts: Sequence[dict]) -> List[dict]:
    out: List[dict] = []
    seen: set[str] = set()
    for conflict in conflicts:
        key = json.dumps(conflict, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(conflict)
    return out


def _mark_readiness(sewing: List[dict], embroidery: Sequence[dict]) -> None:
    finish_by_order = {
        row["orderNumber"]: datetime.fromisoformat(row["finish"]) for row in embroidery
    }
    for row in sewing:
        if row.get("locked") and not row.get("orderNumber"):
            continue
        start = datetime.fromisoformat(row["start"]) if row.get("start") else None
        finish = finish_by_order.get(row.get("orderNumber"))
        row["embroideryReady"] = finish is None or (start is not None and finish <= start)
        if not row["embroideryReady"]:
            row["conflict"] = True


def build_schedule(
    order_rows: Sequence[dict],
    *,
    config: Optional[SchedulerConfig | dict] = None,
    thread_inventory: Optional[Dict[str, dict]] = None,
    locks: Optional[Sequence[dict]] = None,
    run_reason: str = "manual",
    now: Optional[datetime] = None,
) -> dict:
    cfg = config if isinstance(config, SchedulerConfig) else SchedulerConfig.from_dict(config)
    created = now or datetime.now(BUSINESS_TZ)
    if created.tzinfo is None:
        created = created.replace(tzinfo=BUSINESS_TZ)
    planning_start_day = created.astimezone(BUSINESS_TZ).date()
    planning_start_dt = max(created.astimezone(BUSINESS_TZ), _at(planning_start_day, EMBROIDERY_DAY_START))
    orders, warnings = normalize_orders(order_rows, cfg)
    groups, grouping_warnings = detect_shipping_groups(orders)
    sewing, sewing_conflicts, deadlines = _schedule_sewing(
        groups, cfg, locks or [], planning_start_day
    )
    embroidery, embroidery_conflicts = _schedule_embroidery(
        orders, cfg, deadlines, thread_inventory or {}, planning_start_dt
    )
    _mark_readiness(sewing, embroidery)
    conflicts = _dedupe_conflicts(
        [w for w in warnings + grouping_warnings if w.get("severity") == "blocking"]
        + sewing_conflicts
        + embroidery_conflicts
    )
    non_blocking = [w for w in warnings + grouping_warnings if w.get("severity") != "blocking"]
    input_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "orders": order_rows,
                "config": cfg.as_dict(),
                "inventory": thread_inventory or {},
                "locks": locks or [],
            },
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "createdAt": created.isoformat(),
        "timezone": cfg.timezone,
        "runReason": run_reason,
        "inputFingerprint": input_fingerprint,
        "orderCount": len(orders),
        "orders": orders,
        "shippingGroups": [
            {
                "id": g["id"],
                "source": g["source"],
                "requiredShipDate": iso_day(g["required_ship_date"]),
                "orderNumbers": [o["order_number"] for o in g["orders"]],
                "customer": g["orders"][0]["customer"] if g["orders"] else "",
            }
            for g in groups
        ],
        "sewing": sewing,
        "embroidery": embroidery,
        "conflicts": conflicts,
        "warnings": non_blocking,
        "settings": cfg.as_dict(),
        "summary": {
            "blockingConflictCount": len(conflicts),
            "warningCount": len(non_blocking),
            "sewingEntryCount": len(sewing),
            "embroideryJobCount": len(embroidery),
            "thirdSewerDates": sorted(
                {
                    row["date"]
                    for row in sewing
                    if _number(row.get("emergencyCapacity")) > 0
                }
            ),
        },
    }
