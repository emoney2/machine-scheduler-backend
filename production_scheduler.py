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
EMBROIDERY_MACHINE_HEADS = {
    "Single Head Machine": 1,
    "Machine 2": 6,
    "Machine 3": 6,
    "Machine 4": 6,
}
EMBROIDERY_MACHINES = tuple(EMBROIDERY_MACHINE_HEADS)
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


DEFAULT_GROUND_TRANSIT_DAYS = 3


def estimate_ground_transit_days(zip_code: Any = "", state: Any = "") -> int:
    """Typical UPS Ground business days from Buford, GA (30519).

    Used when live UPS transit is missing. Nearby Southeast jobs are 1–2 days,
    not a blanket week.
    """
    digits = re.sub(r"\D", "", _text(zip_code))
    prefix = digits[:3]
    lead = prefix[:1]
    st = _text(state).upper()
    if st == "GA" or prefix.startswith("30") or prefix.startswith("31"):
        return 1
    if st in {"SC", "AL", "TN", "FL", "NC"} or lead == "3":
        return 2
    if st in {
        "VA", "WV", "KY", "MS", "LA", "MD", "DC", "DE", "PA", "NJ", "NY",
        "CT", "RI", "MA", "NH", "VT", "ME", "OH", "IN", "MI", "IL", "WI",
        "MO", "AR",
    } or lead in {"1", "2", "4"}:
        return 3
    if lead in {"5", "6", "7"}:
        return 4
    if lead in {"8", "9"}:
        return 5
    return DEFAULT_GROUND_TRANSIT_DAYS


LOCAL_DELIVERY_TRANSIT_DAYS = 1


def is_local_delivery(value: Any) -> bool:
    """True for Local delivery / will-call style shipping (no UPS transit)."""
    return "local" in _text(value).casefold()


def transit_days_for_service(service_code: Any, zip_code: Any = "", state: Any = "") -> int:
    code = _text(service_code).zfill(2)
    if code in {"01", "13", "14"}:
        return 1
    if code == "02":
        return 2
    if code == "12":
        return 3
    return estimate_ground_transit_days(zip_code, state)


def resolve_required_ship_date(
    due: Optional[date],
    transit_days: Any,
    holidays: Iterable[date] = (),
    fallback_ship: Optional[date] = None,
) -> Optional[date]:
    """Ship date = due minus actual transit. Ignore a conservative sheet ship date."""
    days = int(_number(transit_days, -1))
    if due and days >= 0:
        return subtract_workdays(due, days, holidays)
    return fallback_ship or due


def embroidery_heads(machine: Any) -> int:
    name = _text(machine)
    if name in {"Machine 1", "Single Head", "Single Head Machine"}:
        return int(EMBROIDERY_MACHINE_HEADS.get("Single Head Machine", 1))
    return int(EMBROIDERY_MACHINE_HEADS.get(name, EMBROIDERY_HEADS))


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


def is_back_product(product: Any) -> bool:
    """Back panels are embroidered, not sewn as their own sewing job."""
    name = re.sub(r"\s+", " ", _text(product)).strip()
    return bool(re.search(r"(?:^|\s)backs?$", name, flags=re.I))


def is_hard_date(order: dict) -> bool:
    return "HARD" in _text(order.get("due_type") or order.get("dueType")).upper()


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
        transit_raw = raw.get("_transit_business_days")
        transit = int(_number(transit_raw, -1)) if transit_raw not in (None, "") else -1
        shipping_method = _text(
            raw.get("_shipping_method") or raw.get("Shipping Method") or raw.get("Ship Via")
        )
        if is_local_delivery(shipping_method):
            shipping_method = "Local Delivery"
            if transit < 0:
                transit = LOCAL_DELIVERY_TRANSIT_DAYS
        elif not shipping_method:
            shipping_method = "UPS Ground"
        provided_ship = parse_date(raw.get("_required_ship_date") or raw.get("Ship Date"))
        if due and transit >= 0:
            ship = resolve_required_ship_date(due, transit, config.holidays, provided_ship)
        else:
            ship = provided_ship
        address = raw.get("_shipping_address") if isinstance(raw.get("_shipping_address"), dict) else {}
        factor = _capacity_factor(raw, config, warnings)
        product = _text(raw.get("Product"))
        back = is_back_product(product)
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
            "product": product,
            "design": _text(raw.get("Design")),
            "quantity": qty,
            "remaining_quantity": max(0, remaining - sewing_done),
            "embroidery_remaining": max(0, remaining - emb_done),
            "needs_sewing": not back,
            "stitch_count": stitches,
            "thread_colors": thread_codes,
            "thread_usage_cones": {
                _text(k): max(0.0, _number(v))
                for k, v in (raw.get("_thread_usage_cones") or {}).items()
            },
            "due_date": due,
            "in_hand_date": parse_date(raw.get("In-Hand Date")) or due,
            "required_ship_date": ship,
            "transit_business_days": max(0, transit) if transit >= 0 else 0,
            "shipping_method": shipping_method,
            "shipping_address": address,
            "stage": stage,
            "due_type": _text(raw.get("Hard Date/Soft Date")),
            "rush": _flag(raw, "Rush", "Rush Order") or "RUSH" in _text(raw.get("Notes")).upper(),
            "order_date": parse_date(raw.get("Date")),
            "sewing_factor": factor,
            "sewing_units": 0.0 if back else max(0.0, (max(0, remaining - sewing_done) * factor)),
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


def _shipment_due_key(order: dict) -> Optional[date]:
    """Customer delivery date that decides whether jobs may share a shipment."""
    return order.get("due_date") or order.get("in_hand_date") or order.get("required_ship_date")


def _same_shipment_due(left: dict, right: dict) -> bool:
    a, b = _shipment_due_key(left), _shipment_due_key(right)
    return a is not None and a == b


def detect_shipping_groups(orders: Sequence[dict]) -> Tuple[List[dict], List[dict]]:
    """Group same-customer consecutive jobs only when they share a due date.

    Repeat orders of the same design for later deliveries stay separate
    shipments even if the order numbers are consecutive.
    """
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
        _append_groups_split_by_due(groups, members, gid, "explicit")

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
            consecutive = (
                prev.isdigit()
                and current.isdigit()
                and int(current) == int(prev) + 1
            )
            if consecutive and _same_shipment_due(chunk[-1], order):
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


def _append_groups_split_by_due(
    groups: List[dict],
    members: Sequence[dict],
    group_id: str,
    source: str,
) -> None:
    """Keep an explicit group only when every job shares the same due date."""
    if not members:
        return
    buckets: Dict[Optional[date], List[dict]] = defaultdict(list)
    for order in members:
        buckets[_shipment_due_key(order)].append(order)
    if len(buckets) == 1:
        groups.append({"id": group_id, "source": source, "orders": list(members)})
        return
    for chunk in buckets.values():
        customer_key = chunk[0].get("customer_key") or ""
        _append_inferred_group(groups, chunk, customer_key)


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


def _sewing_capacity(
    cursor: date,
    config: SchedulerConfig,
    reserved: Dict[date, float],
    day_job_count: Dict[date, int],
    setup_units: float,
    allow_emergency: bool = False,
):
    regular = config.regular_sewing_capacity
    emergency = (
        config.emergency_sewing_capacity
        if allow_emergency or cursor in config.approved_emergency_dates
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
        "scheduledCapacity": round(already + setup + used, 4),
        "remainingCapacity": round(max(0.0, total - already - setup - used), 4),
        "dueDate": iso_day(order["due_date"]),
        "inHandDate": iso_day(order["in_hand_date"]),
        "requiredShipDate": iso_day(group_ship),
        "transitBusinessDays": order["transit_business_days"],
        "shippingMethod": order["shipping_method"],
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
        "emergencyUsed": bool(emergency > 0 and (already + setup + used) > regular + 1e-9),
        "emergencyCapacity": emergency if emergency > 0 and (already + setup + used) > regular + 1e-9 else 0.0,
        "image": order["image"],
        "dayQuantity": 0,
        "split": False,
        "splitPart": 1,
        "splitParts": 1,
    }


def _annotate_day_quantities(entries: List[dict], order: dict) -> None:
    """Put the pieces for this calendar day on each sewing card."""
    pieces = max(0, int(_number(order.get("remaining_quantity"))))
    factor = max(_number(order.get("sewing_factor"), 1.0), 1e-9)
    if not entries:
        return
    split = len(entries) > 1
    total_units = sum(max(0.0, _number(row.get("capacityUnits"))) for row in entries)
    allocated = 0
    for index, entry in enumerate(entries):
        units = max(0.0, _number(entry.get("capacityUnits")))
        if index == len(entries) - 1:
            qty = max(0, pieces - allocated)
        elif total_units <= 1e-9:
            qty = 0
        else:
            qty = int(round(pieces * units / total_units))
            allocated += qty
        if qty <= 0 and units > 0:
            qty = max(1, int(round(units / factor)))
        entry["dayQuantity"] = qty
        entry["split"] = split
        entry["splitPart"] = index + 1
        entry["splitParts"] = len(entries)


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

    def take_day(
        cursor: date,
        order: dict,
        group: dict,
        group_ship: date,
        remaining_units: float,
        allow_emergency: bool = False,
        existing_entries: Optional[List[dict]] = None,
        force: bool = False,
    ):
        regular, emergency, total, already, setup, free = _sewing_capacity(
            cursor, config, reserved, day_job_count, setup_units, allow_emergency
        )
        if free <= 1e-9:
            if not force:
                return remaining_units, None
            used = remaining_units
            emergency = max(emergency, config.emergency_sewing_capacity)
            total = max(total, already + setup + used)
        else:
            used = min(free, remaining_units)
        same_day = next(
            (row for row in (existing_entries or []) if row.get("date") == iso_day(cursor)),
            None,
        )
        if same_day:
            same_day["capacityUnits"] = round(_number(same_day.get("capacityUnits")) + used, 4)
            used_emergency = emergency > 0 and (already + used) > regular + 1e-9
            same_day["emergencyCapacity"] = emergency if used_emergency else _number(same_day.get("emergencyCapacity"))
            same_day["emergencyUsed"] = bool(same_day.get("emergencyUsed") or used_emergency)
            same_day["scheduledCapacity"] = round(already + used, 4)
            same_day["remainingCapacity"] = round(max(0.0, total - already - used), 4)
            end_fraction = max(0.0, min(1.0, (already + used) / max(total, 1e-9)))
            same_day["finish"] = min(
                _at(cursor, SEWING_DAY_START) + timedelta(minutes=end_fraction * 7.5 * 60.0),
                _at(cursor, SEWING_DAY_END),
            ).isoformat()
            reserved[cursor] += used
            return remaining_units - used, same_day
        entry = _sewing_entry(
            order, group, group_ship, cursor, used, setup, regular, emergency, already, total
        )
        reserved[cursor] += setup + used
        day_job_count[cursor] += 1
        return remaining_units - used, entry

    for group in groups:
        _resolve_group_ship(group, planning_start, config.holidays)

    def place_order(order: dict, group: dict) -> None:
        group_ship = group.get("required_ship_date") or order.get("required_ship_date")
        hard = is_hard_date(order)
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
                    "hardDate": hard,
                    "conflict": False,
                })
            _annotate_day_quantities(fixed, order)
            embroidery_deadlines[order["order_number"]] = datetime.fromisoformat(fixed[0]["start"])
            return
        remaining_units = order["sewing_units"]
        if remaining_units <= 1e-9:
            embroidery_deadlines[order["order_number"]] = _at(group_ship, SEWING_DAY_START)
            return
        cursor = previous_workday(group_ship, config.holidays, include=True)
        order_entries: List[dict] = []
        guard = 0
        while remaining_units > 1e-9 and guard < 5000:
            guard += 1
            if cursor < planning_start:
                break
            remaining_units, entry = take_day(cursor, order, group, group_ship, remaining_units)
            if entry and entry not in order_entries:
                order_entries.append(entry)
            cursor = previous_workday(cursor, config.holidays)
        if remaining_units > 1e-9 and config.emergency_sewing_capacity > 0:
            cursor = previous_workday(group_ship, config.holidays, include=True)
            while remaining_units > 1e-9 and guard < 5000:
                guard += 1
                if cursor < planning_start:
                    break
                remaining_units, entry = take_day(
                    cursor, order, group, group_ship, remaining_units,
                    allow_emergency=True, existing_entries=order_entries,
                )
                if entry and entry not in order_entries:
                    order_entries.append(entry)
                cursor = previous_workday(cursor, config.holidays)
        overflow = remaining_units
        ship_in_horizon = bool(group_ship and group_ship >= planning_start)
        if remaining_units > 1e-9 and hard and ship_in_horizon:
            cursor = previous_workday(group_ship, config.holidays, include=True)
            while remaining_units > 1e-9 and guard < 5000:
                guard += 1
                if cursor < planning_start:
                    break
                remaining_units, entry = take_day(
                    cursor, order, group, group_ship, remaining_units,
                    allow_emergency=True, existing_entries=order_entries, force=True,
                )
                if entry and entry not in order_entries:
                    order_entries.append(entry)
                cursor = previous_workday(cursor, config.holidays)
            overflow = 0.0
        elif remaining_units > 1e-9 and not hard:
            cursor = next_workday(planning_start, config.holidays, include=True)
            while remaining_units > 1e-9 and guard < 5000:
                guard += 1
                remaining_units, entry = take_day(
                    cursor, order, group, group_ship, remaining_units,
                    allow_emergency=True, existing_entries=order_entries,
                )
                if entry and entry not in order_entries:
                    order_entries.append(entry)
                cursor = next_workday(cursor, config.holidays)
        elif remaining_units > 1e-9 and hard:
            cursor = next_workday(planning_start, config.holidays, include=True)
            while remaining_units > 1e-9 and guard < 5000:
                guard += 1
                remaining_units, entry = take_day(
                    cursor, order, group, group_ship, remaining_units,
                    allow_emergency=True, existing_entries=order_entries, force=True,
                )
                if entry and entry not in order_entries:
                    order_entries.append(entry)
                cursor = next_workday(cursor, config.holidays)
        late = (not hard) and overflow > 1e-9
        if not order_entries:
            conflicts.append({
                "type": "sewing_unscheduled",
                "severity": "blocking",
                "orderNumber": order["order_number"],
                "missingSewingUnits": round(remaining_units, 2),
                "message": "Sewing work could not be placed on any workday",
            })
            return
        order_entries.sort(key=lambda e: e["start"])
        _annotate_day_quantities(order_entries, order)
        emergency_dates = sorted({
            row["date"]
            for row in order_entries
            if row.get("emergencyUsed") or _number(row.get("emergencyCapacity")) > 0
        })
        if emergency_dates:
            conflicts.append({
                "type": "emergency_sewing",
                "severity": "warning",
                "orderNumber": order["order_number"],
                "dates": emergency_dates,
                "message": (
                    "Emergency sewing (third sewer +50) added because regular "
                    "capacity would miss the ship date"
                ),
            })
        finish = datetime.fromisoformat(order_entries[-1]["finish"])
        ship_end = _at(group_ship, SEWING_DAY_END) if group_ship else finish
        if hard and not ship_in_horizon:
            for entry in order_entries:
                entry["conflict"] = True
            conflicts.append({
                "type": "hard_date_missed",
                "severity": "blocking",
                "orderNumber": order["order_number"],
                "requiredShipDate": iso_day(group_ship),
                "expectedCompletion": finish.isoformat(),
                "message": (
                    f"Hard date {iso_day(group_ship)} is already past; "
                    "work is placed on the first open days and is not marked late"
                ),
            })
        elif hard and finish > ship_end:
            for entry in order_entries:
                entry["conflict"] = True
            conflicts.append({
                "type": "hard_date_capacity",
                "severity": "blocking",
                "orderNumber": order["order_number"],
                "requiredShipDate": iso_day(group_ship),
                "expectedCompletion": finish.isoformat(),
                "message": "Hard date kept on the calendar; sewing capacity is overloaded to finish on time",
            })
        elif late:
            for entry in order_entries:
                entry["conflict"] = True
                entry["late"] = True
            conflicts.append({
                "type": "sewing_unscheduled",
                "severity": "blocking",
                "orderNumber": order["order_number"],
                "missingSewingUnits": 0,
                "expectedCompletion": finish.isoformat(),
                "requiredShipDate": iso_day(group_ship),
                "thirdSewerWouldHelp": config.emergency_sewing_capacity > 0,
                "thirdSewerDaysNeeded": int(
                    math.ceil(overflow / max(config.emergency_sewing_capacity, 1.0))
                ),
                "message": (
                    f"Sewing is scheduled but finishes {fmt_conflict_day(finish.isoformat())} after "
                    f"required ship date {iso_day(group_ship)}"
                ),
            })
        entries.extend(order_entries)
        embroidery_deadlines[order["order_number"]] = datetime.fromisoformat(order_entries[0]["start"])

    queued = [(order, group) for group in groups for order in group["orders"]]
    for order, group in sorted(queued, key=lambda item: (0 if is_hard_date(item[0]) else 1, _priority(item[0]))):
        place_order(order, group)

    for group in groups:
        group_ship = group.get("required_ship_date")
        hard_numbers = {o["order_number"] for o in group["orders"] if is_hard_date(o)}
        hard_entries = [e for e in entries if e.get("orderNumber") in hard_numbers]
        if not hard_entries or not group_ship:
            continue
        finish = max(datetime.fromisoformat(e["finish"]) for e in hard_entries)
        if finish > _at(group_ship, SEWING_DAY_END):
            conflicts.append({
                "type": "sewing_deadline",
                "severity": "blocking",
                "groupId": group["id"],
                "orderNumbers": sorted(hard_numbers),
                "expectedCompletion": finish.isoformat(),
                "requiredShipDate": iso_day(group_ship),
                "message": "Hard-date shipping group cannot finish sewing by its required ship date",
            })

    order_lookup = {o["order_number"]: o for g in groups for o in g["orders"]}
    group_lookup = {o["order_number"]: g for g in groups for o in g["orders"]}
    _fill_soft_sewing_gaps(
        entries,
        order_lookup,
        group_lookup,
        config,
        planning_start,
        locks_by_order,
        reserved,
        day_job_count,
        setup_units,
        take_day,
        embroidery_deadlines,
    )
    _close_interior_sewing_gaps(
        entries,
        order_lookup,
        group_lookup,
        config,
        planning_start,
        locks_by_order,
        reserved,
        day_job_count,
        setup_units,
        take_day,
        embroidery_deadlines,
    )
    entries.sort(key=lambda e: (e.get("date", ""), e.get("start", ""), e.get("orderNumber", "")))
    return entries, conflicts, embroidery_deadlines


def _fill_soft_sewing_gaps(
    entries: List[dict],
    order_lookup: Dict[str, dict],
    group_lookup: Dict[str, dict],
    config: SchedulerConfig,
    planning_start: date,
    locks_by_order: Dict[str, List[dict]],
    reserved: Dict[date, float],
    day_job_count: Dict[date, int],
    setup_units: float,
    take_day,
    embroidery_deadlines: Dict[str, datetime],
) -> None:
    """Pull soft-date sewing into empty early days. Hard jobs are moved only to close holes."""
    soft_ids: List[str] = []
    seen: set[str] = set()
    for row in entries:
        oid = _text(row.get("orderNumber"))
        order = order_lookup.get(oid)
        if not oid or not order or oid in seen:
            continue
        if oid in locks_by_order or row.get("locked") or row.get("late"):
            continue
        if is_hard_date(order):
            continue
        seen.add(oid)
        soft_ids.append(oid)
    if not soft_ids:
        return

    def day_free(day: date) -> float:
        _regular, _emergency, _total, _already, _setup, free = _sewing_capacity(
            day, config, reserved, day_job_count, setup_units, False
        )
        return free

    def cumulative_free(start: date, finish: date) -> float:
        total = 0.0
        cursor = start
        guard = 0
        while cursor <= finish and guard < 400:
            total += day_free(cursor)
            cursor = next_workday(cursor, config.holidays)
            guard += 1
        return total

    for oid in soft_ids:
        order = order_lookup[oid]
        group = group_lookup[oid]
        group_ship = group.get("required_ship_date") or order.get("required_ship_date")
        units = max(0.0, _number(order.get("sewing_units")))
        old = [row for row in entries if _text(row.get("orderNumber")) == oid]
        for row in old:
            day = parse_date(row.get("date"))
            if day:
                reserved[day] = max(
                    0.0,
                    reserved[day] - _number(row.get("capacityUnits")) - _number(row.get("setupUnits")),
                )
                day_job_count[day] = max(0, day_job_count[day] - 1)
            entries.remove(row)
        first = next_workday(planning_start, config.holidays, include=True)
        last = previous_workday(group_ship, config.holidays, include=True) if group_ship else first
        if last < first:
            last = first
        whole_day = None
        cursor = first
        guard = 0
        while cursor <= last and guard < 400:
            if day_free(cursor) + 1e-9 >= units:
                whole_day = cursor
                break
            cursor = next_workday(cursor, config.holidays)
            guard += 1
        start_day = whole_day
        if start_day is None:
            cursor = first
            guard = 0
            while cursor <= last and guard < 400:
                if day_free(cursor) > 1e-6 and cumulative_free(cursor, last) + 1e-9 >= units:
                    start_day = cursor
                    break
                cursor = next_workday(cursor, config.holidays)
                guard += 1
        if start_day is None:
            start_day = first
        remaining = units
        new_entries: List[dict] = []
        place = start_day
        guard = 0
        while remaining > 1e-9 and guard < 5000:
            remaining, entry = take_day(
                place, order, group, group_ship, remaining, False, new_entries
            )
            if entry and entry not in new_entries:
                new_entries.append(entry)
            place = next_workday(place, config.holidays)
            guard += 1
        if not new_entries:
            continue
        new_entries.sort(key=lambda e: e["start"])
        _annotate_day_quantities(new_entries, order)
        finish = datetime.fromisoformat(new_entries[-1]["finish"])
        ship_end = _at(group_ship, SEWING_DAY_END) if group_ship else finish
        if finish > ship_end:
            for row in new_entries:
                row["late"] = True
                row["conflict"] = True
        entries.extend(new_entries)
        embroidery_deadlines[oid] = datetime.fromisoformat(new_entries[0]["start"])


def _close_interior_sewing_gaps(
    entries: List[dict],
    order_lookup: Dict[str, dict],
    group_lookup: Dict[str, dict],
    config: SchedulerConfig,
    planning_start: date,
    locks_by_order: Dict[str, List[dict]],
    reserved: Dict[date, float],
    day_job_count: Dict[date, int],
    setup_units: float,
    take_day,
    embroidery_deadlines: Dict[str, datetime],
) -> None:
    """If a weekday is open and a later day has work, move that later job up."""

    def day_free(day: date) -> float:
        _regular, _emergency, _total, _already, _setup, free = _sewing_capacity(
            day, config, reserved, day_job_count, setup_units, False
        )
        return free

    def release(rows: List[dict]) -> None:
        for row in rows:
            day = parse_date(row.get("date"))
            if day:
                reserved[day] = max(
                    0.0,
                    reserved[day] - _number(row.get("capacityUnits")) - _number(row.get("setupUnits")),
                )
                day_job_count[day] = max(0, day_job_count[day] - 1)
            if row in entries:
                entries.remove(row)

    changed = True
    guard = 0
    while changed and guard < 80:
        changed = False
        guard += 1
        dates = [parse_date(row.get("date")) for row in entries]
        dates = [day for day in dates if day]
        if not dates:
            return
        first_busy = min(dates)
        last = max(dates)
        cursor = next_workday(first_busy, config.holidays)
        while cursor < last:
            if day_free(cursor) <= 1e-6:
                cursor = next_workday(cursor, config.holidays)
                continue
            starts: Dict[str, date] = {}
            for row in entries:
                oid = _text(row.get("orderNumber"))
                day = parse_date(row.get("date"))
                if not oid or not day or row.get("locked") or oid in locks_by_order:
                    continue
                starts[oid] = min(starts[oid], day) if oid in starts else day
            later = [oid for oid, start in starts.items() if start > cursor]
            if not later:
                cursor = next_workday(cursor, config.holidays)
                continue
            later.sort(key=lambda oid: (starts[oid], oid))
            moved = False
            for oid in later:
                order = order_lookup.get(oid)
                group = group_lookup.get(oid)
                if not order or not group:
                    continue
                group_ship = group.get("required_ship_date") or order.get("required_ship_date")
                old = [row for row in entries if _text(row.get("orderNumber")) == oid]
                if not old:
                    continue
                release(old)
                remaining = max(0.0, _number(order.get("sewing_units")))
                new_entries: List[dict] = []
                place = cursor
                inner = 0
                while remaining > 1e-9 and inner < 5000:
                    remaining, entry = take_day(
                        place,
                        order,
                        group,
                        group_ship,
                        remaining,
                        False,
                        new_entries,
                    )
                    if entry and entry not in new_entries:
                        new_entries.append(entry)
                    place = next_workday(place, config.holidays)
                    inner += 1
                finish = (
                    datetime.fromisoformat(new_entries[-1]["finish"])
                    if new_entries else _at(cursor, SEWING_DAY_END)
                )
                ship_end = _at(group_ship, SEWING_DAY_END) if group_ship else finish
                if not new_entries or (is_hard_date(order) and finish > ship_end):
                    release(new_entries)
                    for row in old:
                        day = parse_date(row.get("date"))
                        if day:
                            reserved[day] += _number(row.get("capacityUnits")) + _number(row.get("setupUnits"))
                            day_job_count[day] += 1
                        entries.append(row)
                    continue
                new_entries.sort(key=lambda row: row["start"])
                _annotate_day_quantities(new_entries, order)
                if finish > ship_end and not is_hard_date(order):
                    for row in new_entries:
                        row["late"] = True
                        row["conflict"] = True
                entries.extend(new_entries)
                embroidery_deadlines[oid] = datetime.fromisoformat(new_entries[0]["start"])
                moved = True
                changed = True
                break
            if not moved:
                cursor = next_workday(cursor, config.holidays)


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
    machine: str,
) -> Tuple[bool, List[dict]]:
    problems: List[dict] = []
    heads = embroidery_heads(machine)
    for color in order["thread_colors"]:
        inv = inventory.get(color) or {}
        available = int(_number(inv.get("cones"), 0))
        simultaneous = [
            p for p in placed
            if color in p.get("threadColors", [])
            and _overlap(start, finish, p["_start"], p["_finish"])
        ]
        required = heads + sum(embroidery_heads(p.get("machine")) for p in simultaneous)
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
        if order["stitch_count"] <= 0:
            continue
        deadline = deadlines.get(order["order_number"])
        if not deadline:
            continue
        candidates: List[tuple] = []
        rejected_thread: List[dict] = []
        for machine in EMBROIDERY_MACHINES:
            heads = embroidery_heads(machine)
            hours = embroidery_hours(order["embroidery_remaining"], order["stitch_count"], heads)
            if hours <= 0:
                continue
            effective_deadline = min(deadline, machine_deadlines[machine])
            start, finish, segments = _split_work_backward(
                effective_deadline, hours, config.holidays
            )
            allowed, problems = _thread_slot_allowed(
                order, start, finish, placed, thread_inventory, machine
            )
            if allowed:
                candidates.append((start, machine, finish, segments, hours, heads))
            else:
                rejected_thread.extend(problems)
        if not candidates:
            machine = max(EMBROIDERY_MACHINES, key=lambda m: machine_deadlines[m])
            heads = embroidery_heads(machine)
            hours = embroidery_hours(order["embroidery_remaining"], order["stitch_count"], heads)
            start, finish, segments = _split_work_backward(
                min(deadline, machine_deadlines[machine]), hours, config.holidays
            )
            conflicts.extend(_dedupe_conflicts(rejected_thread))
            conflict = True
        else:
            start, machine, finish, segments, hours, heads = max(candidates, key=lambda c: c[0])
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
            "heads": heads,
            "orderNumber": order["order_number"],
            "customer": order["customer"],
            "product": order["product"],
            "design": order["design"],
            "quantity": order["embroidery_remaining"],
            "runs": embroidery_runs(order["embroidery_remaining"], heads),
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
            "hardDate": is_hard_date(order),
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
                "message": "Embroidery capacity cannot meet the sewing-ready deadline",
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
    _fill_soft_embroidery_gaps(placed, orders, config, planning_start, thread_inventory)
    for row in placed:
        row.pop("_start", None)
        row.pop("_finish", None)
    placed.sort(key=lambda e: (e["start"], e["machine"], e["orderNumber"]))
    return placed, _dedupe_conflicts(conflicts)


def _fill_soft_embroidery_gaps(
    placed: List[dict],
    orders: Sequence[dict],
    config: SchedulerConfig,
    planning_start: datetime,
    thread_inventory: Dict[str, dict],
) -> None:
    """Move soft embroidery into idle machine time. Hard jobs stay just-in-time."""
    by_oid = {order["order_number"]: order for order in orders}
    soft = [
        job for job in placed
        if not job.get("hardDate")
        and not job.get("threadConflict")
        and not is_hard_date(by_oid.get(job.get("orderNumber"), {}))
    ]
    if not soft:
        return
    soft.sort(key=lambda job: (job.get("sewingReadyDeadline") or "", job.get("start") or "", job.get("orderNumber") or ""))
    for job in soft:
        order = by_oid.get(job.get("orderNumber"))
        if not order:
            continue
        deadline = datetime.fromisoformat(job["sewingReadyDeadline"])
        others = [row for row in placed if row.get("orderNumber") != job.get("orderNumber")]
        best = None
        for machine in EMBROIDERY_MACHINES:
            heads = embroidery_heads(machine)
            hours = embroidery_hours(order["embroidery_remaining"], order["stitch_count"], heads)
            if hours <= 0:
                continue
            starts = [planning_start]
            starts.extend(
                row["_finish"]
                for row in sorted(others, key=lambda r: r["_start"])
                if row.get("machine") == machine
            )
            for start_at in starts:
                start, finish, segments = _split_work_forward(start_at, hours, config.holidays)
                if finish > deadline:
                    continue
                if any(
                    row.get("machine") == machine
                    and _overlap(start, finish, row["_start"], row["_finish"])
                    for row in others
                ):
                    continue
                allowed, _ = _thread_slot_allowed(
                    order, start, finish, others, thread_inventory, machine
                )
                if not allowed:
                    continue
                if best is None or start < best[0]:
                    best = (start, machine, finish, segments, hours, heads)
        if not best or best[0] >= job["_start"]:
            continue
        start, machine, finish, segments, hours, heads = best
        job.update({
            "machine": machine,
            "heads": heads,
            "runs": embroidery_runs(order["embroidery_remaining"], heads),
            "durationHours": round(hours, 4),
            "start": start.isoformat(),
            "finish": finish.isoformat(),
            "segments": [
                {**s, "start": s["start"].isoformat(), "finish": s["finish"].isoformat(), "hours": round(s["hours"], 4)}
                for s in segments
            ],
            "sameDaySewing": finish.date() == deadline.date() and finish <= deadline,
            "_start": start,
            "_finish": finish,
        })


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
    all_issues = warnings + grouping_warnings + sewing_conflicts + embroidery_conflicts
    conflicts = _dedupe_conflicts([w for w in all_issues if w.get("severity") == "blocking"])
    non_blocking = _dedupe_conflicts([w for w in all_issues if w.get("severity") != "blocking"])
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
