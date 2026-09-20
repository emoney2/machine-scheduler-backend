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
SHIPPING_DELAY_BUFFER_DAYS = 1


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
    shipping_method: Any = "",
) -> Optional[date]:
    """Ship date = due minus transit, minus a workday UPS-delay buffer for Ground."""
    days = int(_number(transit_days, -1))
    if due and days >= 0:
        extra = 0 if is_local_delivery(shipping_method) or days <= 0 else SHIPPING_DELAY_BUFFER_DAYS
        return subtract_workdays(due, days + extra, holidays)
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
    sewers: List[dict] = field(default_factory=list)
    sewer_absences: Dict[date, List[str]] = field(default_factory=dict)
    overtime_sewing_capacity: float = 50.0
    overtime_dates: set[date] = field(default_factory=set)

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "SchedulerConfig":
        raw = raw or {}
        regular = max(1.0, _number(raw.get("regularSewingCapacity"), 95.0))
        emergency = max(0.0, _number(raw.get("emergencySewingCapacity"), 50.0))
        sewers = assign_sewer_capacities(parse_sewers(raw.get("sewers")), regular, emergency)
        absences: Dict[date, List[str]] = {}
        for key, names in (raw.get("sewerAbsences") or {}).items():
            day = parse_date(key)
            if not day:
                continue
            absences[day] = [_text(n) for n in (names or []) if _text(n)]
        return cls(
            regular_sewing_capacity=regular,
            emergency_sewing_capacity=emergency,
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
            sewers=sewers,
            sewer_absences=absences,
            overtime_sewing_capacity=max(0.0, _number(raw.get("overtimeSewingCapacity"), 50.0)),
            overtime_dates={
                d for d in (parse_date(v) for v in raw.get("overtimeSewingDates", [])) if d
            },
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
            "sewers": list(self.sewers),
            "overtimeSewingCapacity": self.overtime_sewing_capacity,
            "overtimeSewingDates": sorted(iso_day(d) for d in self.overtime_dates),
            "sewerAbsences": {
                iso_day(day): list(names)
                for day, names in sorted(self.sewer_absences.items(), key=lambda item: item[0])
            },
        }

    def sewing_off_days(self) -> set[date]:
        return set(self.holidays) | closed_sewing_dates(self)


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


def is_towel_or_needlepoint(product: Any) -> bool:
    """Towels and needlepoint are not run on the embroidery or sewing calendars."""
    name = re.sub(r"[\s\-_]+", " ", _text(product)).casefold()
    if "towel" in name:
        return True
    return "needlepoint" in name.replace(" ", "")


def is_hard_date(order: dict) -> bool:
    return "HARD" in _text(order.get("due_type") or order.get("dueType")).upper()


_SEWER_NAME_HEADERS = {"name", "sewer", "sewers", "employee", "staff", "sewer name"}
_SEWER_CAPACITY_HEADERS = {"capacity", "pcs", "pieces", "daily capacity"}
_SEWER_IGNORE_NAMES = {"justin", "justin eckard"}
_SEWER_HEADER_WORDS = _SEWER_NAME_HEADERS | {
    "date", "day", "order", "order #", "order#", "qty", "quantity", "total",
    "notes", "product", "design", "stage", "top", "elastic", "fur", "flat",
    "round", "pcs", "pieces", "capacity",
}


def _is_sewer_name(value: Any) -> bool:
    name = _text(value)
    if not name:
        return False
    key = name.casefold()
    if key in _SEWER_IGNORE_NAMES or key.startswith("justin"):
        return False
    if key in _SEWER_HEADER_WORDS:
        return False
    if re.fullmatch(r"[\d./\-]+", name):
        return False
    return bool(re.search(r"[A-Za-z]", name))


def _sewer_name_list_score(names: Sequence[str]) -> int:
    n = len(names)
    if n < 2:
        return n
    if n <= 8:
        return 10 + n
    return max(0, 18 - (n - 8))


def extract_sewer_names(values: Sequence[Sequence[Any]]) -> List[str]:
    """Find the densest name row or column on the Sewing tab."""
    grid = [list(row or []) for row in (values or [])]
    if not grid:
        return []
    best: List[str] = []
    best_score = -1
    for row in grid[:25]:
        names = [_text(cell) for cell in row if _is_sewer_name(cell)]
        score = _sewer_name_list_score(names)
        if score > best_score:
            best, best_score = names, score
    width = max((len(row) for row in grid[:40]), default=0)
    for col in range(min(width, 50)):
        names = []
        for row in grid[:40]:
            if col < len(row) and _is_sewer_name(row[col]):
                names.append(_text(row[col]))
        score = _sewer_name_list_score(names)
        if score > best_score:
            best, best_score = names, score
    return best


def parse_sewers(raw: Any) -> List[dict]:
    """Read sewer names from the Sewing tab. Last remaining name is emergency. Skip Justin."""
    rows: List[dict] = []
    if isinstance(raw, dict):
        raw = raw.get("sewers") or raw.get("values") or []
    if not isinstance(raw, (list, tuple)):
        return []
    if raw and not isinstance(raw[0], dict) and isinstance(raw[0], (list, tuple)):
        for name in extract_sewer_names(raw):
            rows.append({"name": name, "role": "regular", "capacity": 0.0})
        return _dedupe_sewers(rows)
    for item in raw:
        if isinstance(item, dict):
            name = _text(item.get("name") or item.get("Name") or item.get("Sewer"))
            if not _is_sewer_name(name):
                continue
            rows.append({
                "name": name,
                "role": "regular",
                "capacity": _number(item.get("capacity") or item.get("Capacity")),
            })
        else:
            name = _text(item)
            if _is_sewer_name(name):
                rows.append({"name": name, "role": "regular", "capacity": 0.0})
    return _dedupe_sewers(rows)


def _dedupe_sewers(rows: Sequence[dict]) -> List[dict]:
    seen = set()
    out = []
    for row in rows:
        key = _text(row.get("name")).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
    for index, row in enumerate(out):
        row["role"] = "emergency" if len(out) >= 2 and index == len(out) - 1 else "regular"
    return out


def assign_sewer_capacities(
    sewers: Sequence[dict],
    regular_total: float,
    emergency_total: float,
) -> List[dict]:
    regular = [dict(s) for s in sewers if s.get("role") != "emergency"]
    extra = [dict(s) for s in sewers if s.get("role") == "emergency"]
    if regular:
        missing = [s for s in regular if _number(s.get("capacity")) <= 0]
        if missing:
            share = max(0.0, regular_total) / len(regular)
            for row in missing:
                row["capacity"] = share
    for row in extra:
        if _number(row.get("capacity")) <= 0:
            row["capacity"] = max(0.0, emergency_total)
    return regular + extra


def _absence_names(config: "SchedulerConfig", cursor: date) -> set[str]:
    return {_text(name).casefold() for name in (config.sewer_absences.get(cursor) or []) if _text(name)}


def sewers_present(config: "SchedulerConfig", cursor: date) -> List[dict]:
    absent = _absence_names(config, cursor)
    return [row for row in config.sewers if _text(row.get("name")).casefold() not in absent]


def closed_sewing_dates(config: "SchedulerConfig") -> set[date]:
    if not config.sewers:
        return set()
    return {
        day
        for day in config.sewer_absences
        if not sewers_present(config, day)
    }


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
            ship = resolve_required_ship_date(
                due, transit, config.holidays, provided_ship, shipping_method
            )
        else:
            ship = provided_ship
        address = raw.get("_shipping_address") if isinstance(raw.get("_shipping_address"), dict) else {}
        factor = _capacity_factor(raw, config, warnings)
        product = _text(raw.get("Product"))
        back = is_back_product(product)
        outsourced = is_towel_or_needlepoint(product)
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
        if stitches <= 0 and "SEW" not in stage_token and not outsourced:
            warnings.append({
                "type": "missing_stitch_count", "severity": "warning", "orderNumber": oid,
                "message": "Stitch count is missing; embroidery will stay unscheduled until it is available",
            })
        thread_codes = _thread_codes(raw.get("Threads"))
        if not thread_codes and "SEW" not in stage_token and not outsourced:
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
            "embroidery_remaining": 0 if outsourced else max(0, remaining - emb_done),
            "needs_sewing": not back and not outsourced,
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
            "sewing_units": 0.0 if back or outsourced else max(0.0, (max(0, remaining - sewing_done) * factor)),
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


def _day_sewing_cap(cursor: date, config: SchedulerConfig) -> Tuple[float, float, float]:
    """Regular, emergency, and hard daily max (regular + emergency)."""
    if config.sewers:
        present = sewers_present(config, cursor)
        regular = sum(_number(row.get("capacity")) for row in present if row.get("role") != "emergency")
        emergency = sum(_number(row.get("capacity")) for row in present if row.get("role") == "emergency")
    else:
        regular = config.regular_sewing_capacity
        emergency = config.emergency_sewing_capacity
    overtime = config.overtime_sewing_capacity if cursor in config.overtime_dates else 0.0
    return regular, emergency, regular + emergency + overtime


def _sewing_capacity(
    cursor: date,
    config: SchedulerConfig,
    reserved: Dict[date, float],
    day_job_count: Dict[date, int],
    setup_units: float,
    allow_emergency: bool = False,
):
    regular, emergency_max, physical = _day_sewing_cap(cursor, config)
    emergency = (
        emergency_max
        if allow_emergency or cursor in config.approved_emergency_dates
        else 0.0
    )
    total = min(regular + emergency, physical)
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
    sewing_units = max(0.0, _number(order.get("sewing_units")))
    if not entries:
        return
    split = len(entries) > 1
    total_units = sum(max(0.0, _number(row.get("capacityUnits"))) for row in entries)
    if sewing_units > 1e-9 and total_units + 1e-6 < sewing_units:
        pieces = min(pieces, max(0, int(round(total_units / factor))))
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
    off = config.sewing_off_days()

    def take_day(
        cursor: date,
        order: dict,
        group: dict,
        group_ship: date,
        remaining_units: float,
        allow_emergency: bool = False,
        existing_entries: Optional[List[dict]] = None,
        force: bool = False,
        finish_if_fits: bool = False,
    ):
        regular, emergency, total, already, setup, free = _sewing_capacity(
            cursor, config, reserved, day_job_count, setup_units, allow_emergency
        )
        _reg, emergency_max, physical = _day_sewing_cap(cursor, config)
        room = max(0.0, min(total, physical) - already - setup)
        if room <= 1e-9:
            if not force:
                return remaining_units, None
            emergency = max(emergency, emergency_max)
            total = min(regular + emergency, physical)
            room = max(0.0, total - already - setup)
            if room <= 1e-9:
                return remaining_units, None
        used = min(room, remaining_units)
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
            leftover = remaining_units - used
            entry = same_day
        else:
            entry = _sewing_entry(
                order, group, group_ship, cursor, used, setup, regular, emergency, already, total
            )
            reserved[cursor] += setup + used
            day_job_count[cursor] += 1
            leftover = remaining_units - used
            if existing_entries is not None and entry not in existing_entries:
                existing_entries.append(entry)
        if finish_if_fits and leftover > 1e-9 and not allow_emergency:
            _reg, _em, _tot, _al, _st, extra_free = _sewing_capacity(
                cursor, config, reserved, day_job_count, setup_units, True
            )
            if leftover <= extra_free + 1e-9:
                return take_day(
                    cursor, order, group, group_ship, leftover,
                    allow_emergency=True, existing_entries=existing_entries or [entry],
                    force=force,
                )
        return leftover, entry

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
        order_entries: List[dict] = []
        guard = 0
        customer_key = normalize_customer(order.get("customer"))
        ship_in_horizon = bool(group_ship and group_ship >= planning_start)
        last_ok = previous_workday(group_ship, off, include=True) if group_ship else planning_start
        if group_ship and last_ok < planning_start:
            last_ok = planning_start + timedelta(days=400)
        full_units = remaining_units

        def other_customer_owns(place: date) -> bool:
            if remaining_units < 40:
                return False
            counts: Dict[str, float] = defaultdict(float)
            for row in entries:
                if parse_date(row.get("date")) != place:
                    continue
                counts[normalize_customer(row.get("customer"))] += _number(row.get("capacityUnits"))
            if not counts:
                return False
            owner, qty = max(counts.items(), key=lambda item: item[1])
            return bool(owner and owner != customer_key and qty >= 30)

        def clear_order_entries() -> None:
            nonlocal remaining_units
            for row in list(order_entries):
                day = parse_date(row.get("date"))
                if day:
                    reserved[day] = max(
                        0.0,
                        reserved[day] - _number(row.get("capacityUnits")) - _number(row.get("setupUnits")),
                    )
                    day_job_count[day] = max(0, day_job_count[day] - 1)
            order_entries.clear()
            remaining_units = full_units

        def walk_back_from(end_day: date, allow_emergency: bool) -> None:
            nonlocal remaining_units, guard
            started = False
            place = previous_workday(end_day, off, include=True)
            while remaining_units > 1e-9 and guard < 5000:
                guard += 1
                if place < planning_start:
                    break
                remaining_units, entry = take_day(
                    place, order, group, group_ship, remaining_units,
                    allow_emergency=allow_emergency, existing_entries=order_entries,
                )
                if entry:
                    started = True
                    if entry not in order_entries:
                        order_entries.append(entry)
                elif started:
                    break
                place = previous_workday(place, off)

        def take_until_done(start: date, allow_emergency: bool, skip_owned: bool, limit: Optional[date] = None) -> None:
            nonlocal remaining_units, guard
            started = False
            place = start
            stop = limit if limit is not None else last_ok
            while remaining_units > 1e-9 and guard < 5000:
                guard += 1
                if place > stop:
                    break
                if skip_owned and not started and other_customer_owns(place):
                    place = next_workday(place, off)
                    continue
                remaining_units, entry = take_day(
                    place, order, group, group_ship, remaining_units,
                    allow_emergency=allow_emergency, existing_entries=order_entries,
                )
                if entry:
                    started = True
                    if entry not in order_entries:
                        order_entries.append(entry)
                    place = next_workday(place, off)
                elif started:
                    break
                else:
                    place = next_workday(place, off)

        def try_hard_ending(end_day: date, allow_emergency: bool) -> bool:
            clear_order_entries()
            walk_back_from(end_day, allow_emergency)
            return remaining_units <= 1e-9

        if not hard:
            take_until_done(next_workday(planning_start, off, include=True), False, True)
            if remaining_units > 1e-9:
                clear_order_entries()
                take_until_done(next_workday(planning_start, off, include=True), False, False)
            if remaining_units > 1e-9:
                last = max(_sewing_entry_dates(order_entries), default=None)
                take_until_done(next_workday(last or planning_start, off, include=last is None), True, False, planning_start + timedelta(days=400))
            overflow = remaining_units
        else:
            end = previous_workday(group_ship, off, include=True) if group_ship else planning_start
            placed = False
            cursor = end
            for _ in range(40):
                if cursor < planning_start:
                    break
                if try_hard_ending(cursor, False):
                    placed = True
                    break
                cursor = previous_workday(cursor, off)
            if not placed:
                cursor = end
                for _ in range(40):
                    if cursor < planning_start:
                        break
                    if try_hard_ending(cursor, True):
                        placed = True
                        break
                    cursor = previous_workday(cursor, off)
            if not placed:
                clear_order_entries()
                walk_back_from(end, True)
                if remaining_units > 1e-9:
                    last = max(_sewing_entry_dates(order_entries), default=end)
                    take_until_done(
                        next_workday(last, off),
                        True,
                        False,
                        last + timedelta(days=40),
                    )
            overflow = remaining_units
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
        elif hard and remaining_units > 1e-9:
            for entry in order_entries:
                entry["conflict"] = True
            extra_days = int(math.ceil(remaining_units / max(config.overtime_sewing_capacity, 1.0)))
            conflicts.append({
                "type": "overtime_capacity",
                "severity": "blocking",
                "orderNumber": order["order_number"],
                "requiredShipDate": iso_day(group_ship),
                "expectedCompletion": finish.isoformat(),
                "missingSewingUnits": round(remaining_units, 2),
                "overtimeDaysNeeded": extra_days,
                "message": (
                    f"Need about {round(remaining_units)} more pieces of sewing capacity "
                    f"({extra_days} overtime/extra-helper day(s)). Add those dates under "
                    "Scheduling Settings → Overtime / extra-helper dates, then rebuild."
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

    def _place_key(item):
        order, group = item
        ship = group.get("required_ship_date") or order.get("required_ship_date") or date.max
        try:
            oid = int(order["order_number"])
        except (TypeError, ValueError):
            oid = 10**15
        return (
            0 if is_hard_date(order) else 1,
            ship,
            _number(order.get("sewing_units")),
            oid,
        )

    for order, group in sorted(queued, key=_place_key):
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
    pass_args = (
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
    _keep_sewing_together(*pass_args)
    _fill_soft_sewing_gaps(*pass_args)
    _keep_sewing_together(*pass_args)
    _spill_sewing_over_capacity(*pass_args)
    entries.sort(key=lambda e: (e.get("date", ""), e.get("start", ""), e.get("orderNumber", "")))
    return entries, conflicts, embroidery_deadlines


def _sewing_entry_dates(rows: Sequence[dict]) -> List[date]:
    dates = []
    for row in rows:
        day = parse_date(row.get("date"))
        if day:
            dates.append(day)
    return sorted(set(dates))


def _interior_sewing_hole(dates: Sequence[date], off: Iterable[date]) -> Optional[date]:
    occupied = {day for day in dates if day}
    if len(occupied) < 2:
        return None
    cursor = next_workday(min(occupied), off)
    last = max(occupied)
    while cursor < last:
        if cursor not in occupied:
            return cursor
        cursor = next_workday(cursor, off)
    return None


def _customer_sewing_dates(entries: Sequence[dict], customer_key: str, skip: str = "") -> List[date]:
    dates = []
    for row in entries:
        if skip and _text(row.get("orderNumber")) == skip:
            continue
        if normalize_customer(row.get("customer")) != customer_key:
            continue
        day = parse_date(row.get("date"))
        if day:
            dates.append(day)
    return sorted(set(dates))


def _release_sewing_rows(
    rows: Sequence[dict],
    entries: List[dict],
    reserved: Dict[date, float],
    day_job_count: Dict[date, int],
) -> None:
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


def _restore_sewing_rows(
    rows: Sequence[dict],
    entries: List[dict],
    reserved: Dict[date, float],
    day_job_count: Dict[date, int],
) -> None:
    for row in rows:
        day = parse_date(row.get("date"))
        if day:
            reserved[day] += _number(row.get("capacityUnits")) + _number(row.get("setupUnits"))
            day_job_count[day] += 1
        entries.append(row)


def _can_place_contiguous(
    start: date,
    units: float,
    last: Optional[date],
    day_free,
    off: Iterable[date],
) -> bool:
    if units <= 1e-9:
        return True
    remaining = units
    cursor = start
    guard = 0
    while remaining > 1e-9 and guard < 400:
        if last is not None and cursor > last:
            return False
        free = day_free(cursor)
        if free <= 1e-6:
            return False
        remaining -= free
        cursor = next_workday(cursor, off)
        guard += 1
    return remaining <= 1e-9


def _place_sewing_forward(
    take_day,
    order: dict,
    group: dict,
    group_ship: Optional[date],
    start: date,
    units: float,
    off: Iterable[date],
    last: Optional[date] = None,
) -> Tuple[List[dict], float]:
    remaining = units
    new_entries: List[dict] = []
    place = start
    guard = 0
    started = False
    while remaining > 1e-9 and guard < 5000:
        if last is not None and place > last:
            break
        remaining, entry = take_day(
            place, order, group, group_ship, remaining,
            allow_emergency=False, existing_entries=new_entries, finish_if_fits=True,
        )
        if entry:
            if entry not in new_entries:
                new_entries.append(entry)
            started = True
        elif started:
            break
        place = next_workday(place, off)
        guard += 1
    return new_entries, remaining


def _commit_sewing_move(
    new_entries: List[dict],
    remaining: float,
    order: dict,
    group_ship: Optional[date],
    off: Iterable[date],
    entries: List[dict],
    embroidery_deadlines: Dict[str, datetime],
    other_customer_dates: Sequence[date],
    latest_ok: Optional[date] = None,
    ignore_deadline: bool = False,
) -> bool:
    if remaining > 1e-9 or not new_entries:
        return False
    new_entries.sort(key=lambda row: row["start"])
    finish = datetime.fromisoformat(new_entries[-1]["finish"])
    ship_end = _at(group_ship, SEWING_DAY_END) if group_ship else finish
    horizon = ship_end
    if latest_ok is not None:
        try:
            horizon = _at(latest_ok, SEWING_DAY_END)
        except (OverflowError, ValueError, OSError):
            horizon = ship_end
    if is_hard_date(order) and not ignore_deadline and finish > max(ship_end, horizon):
        return False
    combined = _sewing_entry_dates(new_entries) + list(other_customer_dates)
    if _interior_sewing_hole(combined, off):
        return False
    _annotate_day_quantities(new_entries, order)
    if finish > ship_end and not is_hard_date(order):
        for row in new_entries:
            row["late"] = True
            row["conflict"] = True
    entries.extend(new_entries)
    embroidery_deadlines[order["order_number"]] = datetime.fromisoformat(new_entries[0]["start"])
    return True


def _yield_early_scraps_to_adjacent_jobs(
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
    """If a customer skips tomorrow, give today to jobs already running tomorrow."""
    off = config.sewing_off_days()

    def day_free(day: date) -> float:
        _regular, _emergency, _total, _already, _setup, free = _sewing_capacity(
            day, config, reserved, day_job_count, setup_units, False
        )
        return free

    def rows_for(oid: str) -> List[dict]:
        return [row for row in entries if _text(row.get("orderNumber")) == oid]

    def customer_key_of(oid: str, rows: Sequence[dict] = ()) -> str:
        order = order_lookup.get(oid) or {}
        return normalize_customer(order.get("customer") or (rows[0].get("customer") if rows else ""))

    def place_from(
        oid: str,
        start: date,
        last: Optional[date],
        other_dates: Sequence[date],
        latest_ok: Optional[date],
        ignore_deadline: bool = False,
    ) -> bool:
        order = order_lookup.get(oid)
        group = group_lookup.get(oid)
        if not order or not group:
            return False
        group_ship = group.get("required_ship_date") or order.get("required_ship_date")
        units = max(0.0, _number(order.get("sewing_units")))
        bound = start + timedelta(days=180)
        if last is None or last > bound:
            last = bound
        if not _can_place_contiguous(start, units, last, day_free, off):
            return False
        new_entries, remaining = _place_sewing_forward(
            take_day, order, group, group_ship, start, units, off, last
        )
        if _commit_sewing_move(
            new_entries, remaining, order, group_ship, off,
            entries, embroidery_deadlines, other_dates,
            latest_ok=latest_ok, ignore_deadline=ignore_deadline,
        ):
            return True
        _release_sewing_rows(new_entries, entries, reserved, day_job_count)
        return False

    changed = True
    guard = 0
    while changed and guard < 20:
        changed = False
        guard += 1
        dates = [day for day in (parse_date(row.get("date")) for row in entries) if day]
        if not dates:
            return
        cursor = min(dates)
        last_busy = max(dates)
        inner = 0
        while cursor < last_busy and inner < 40:
            nxt = next_workday(cursor, off)
            by_order: Dict[str, List[dict]] = defaultdict(list)
            customer_days: Dict[str, set] = defaultdict(set)
            for row in entries:
                oid = _text(row.get("orderNumber"))
                day = parse_date(row.get("date"))
                if not oid or not day:
                    continue
                by_order[oid].append(row)
                customer_days[customer_key_of(oid, [row])].add(day)
            def units_on(oid: str, day: date) -> float:
                return sum(
                    _number(row.get("capacityUnits"))
                    for row in by_order.get(oid, [])
                    if parse_date(row.get("date")) == day
                )

            distant: List[str] = []
            adjacent: List[str] = []
            for oid, rows in by_order.items():
                if oid in locks_by_order or any(row.get("locked") for row in rows):
                    continue
                here = units_on(oid, cursor)
                there = units_on(oid, nxt)
                if there > 1e-6 and there + 1e-6 >= here:
                    adjacent.append(oid)
                elif here > 1e-6:
                    order = order_lookup.get(oid) or {}
                    group = group_lookup.get(oid) or {}
                    ship = group.get("required_ship_date") or order.get("required_ship_date")
                    key = customer_key_of(oid, rows)
                    owned = customer_days.get(key) or set()
                    customer_last = max(owned) if owned else cursor
                    can_move = (not is_hard_date(order)) or (ship and ship >= nxt) or customer_last >= nxt
                    if can_move:
                        distant.append(oid)
            if not distant or not adjacent:
                cursor = nxt
                continue
            distant_snaps = {oid: list(by_order[oid]) for oid in distant}
            adjacent_snaps = {oid: list(by_order[oid]) for oid in adjacent}
            later_dates = {
                oid: [day for day in _customer_sewing_dates(entries, customer_key_of(oid), oid) if day >= nxt]
                for oid in distant
            }
            latest_ok = max((max(days) for days in later_dates.values() if days), default=nxt)
            for oid in distant:
                _release_sewing_rows(distant_snaps[oid], entries, reserved, day_job_count)
            adjacent_ok = True
            for oid in adjacent:
                old = rows_for(oid)
                dates_here = _sewing_entry_dates(old)
                if not dates_here:
                    continue
                start = min(min(dates_here), cursor)
                finish = max(dates_here)
                other = _customer_sewing_dates(entries, customer_key_of(oid, old), oid)
                _release_sewing_rows(old, entries, reserved, day_job_count)
                if not place_from(oid, start, finish, other, finish):
                    _restore_sewing_rows(old, entries, reserved, day_job_count)
                    adjacent_ok = False
                    break
            distant_ok = adjacent_ok
            if distant_ok:
                for oid in distant:
                    other = later_dates.get(oid) or []
                    if not place_from(oid, nxt, None, other, latest_ok, ignore_deadline=True):
                        distant_ok = False
                        break
            if not distant_ok:
                for oid in list({_text(row.get("orderNumber")) for row in entries}):
                    if oid in distant_snaps or oid in adjacent_snaps:
                        _release_sewing_rows(rows_for(oid), entries, reserved, day_job_count)
                for snap in list(adjacent_snaps.values()) + list(distant_snaps.values()):
                    _restore_sewing_rows(snap, entries, reserved, day_job_count)
            else:
                changed = True
            inner += 1
            cursor = nxt


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
    """Pull soft-date sewing into empty early days, but only on consecutive days."""
    off = config.sewing_off_days()
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

    def day_free(day: date, allow_emergency: bool = False) -> float:
        _regular, _emergency, _total, _already, _setup, free = _sewing_capacity(
            day, config, reserved, day_job_count, setup_units, allow_emergency
        )
        return free

    def owned_by_other(day: date, customer_key: str, units: float) -> bool:
        if units < 40:
            return False
        counts: Dict[str, float] = defaultdict(float)
        for row in entries:
            if parse_date(row.get("date")) != day:
                continue
            counts[normalize_customer(row.get("customer"))] += _number(row.get("capacityUnits"))
        if not counts:
            return False
        owner, qty = max(counts.items(), key=lambda item: item[1])
        return bool(owner and owner != customer_key and qty >= 30)

    soft_ids.sort(key=lambda oid: _number((order_lookup.get(oid) or {}).get("sewing_units")))
    for oid in soft_ids:
        order = order_lookup[oid]
        group = group_lookup[oid]
        group_ship = group.get("required_ship_date") or order.get("required_ship_date")
        units = max(0.0, _number(order.get("sewing_units")))
        old = [row for row in entries if _text(row.get("orderNumber")) == oid]
        customer_key = normalize_customer(order.get("customer") or (old[0].get("customer") if old else ""))
        other_dates = _customer_sewing_dates(entries, customer_key, oid)
        _release_sewing_rows(old, entries, reserved, day_job_count)
        first = next_workday(planning_start, off, include=True)
        last = previous_workday(group_ship, off, include=True) if group_ship else first
        if last < first:
            last = first
        placed = False
        cursor = first
        guard = 0
        while cursor <= last and guard < 400:
            if owned_by_other(cursor, customer_key, units):
                cursor = next_workday(cursor, off)
                guard += 1
                continue
            if day_free(cursor, True) + 1e-9 >= units or _can_place_contiguous(cursor, units, last, day_free, off):
                new_entries, remaining = _place_sewing_forward(
                    take_day, order, group, group_ship, cursor, units, off, last
                )
                if remaining > 1e-9:
                    extra, leftover = _place_sewing_forward(
                        take_day, order, group, group_ship, cursor, remaining, off, last
                    )
                    if extra:
                        for row in extra:
                            if row not in new_entries:
                                new_entries.append(row)
                        remaining = leftover
                if _commit_sewing_move(
                    new_entries, remaining, order, group_ship, off,
                    entries, embroidery_deadlines, other_dates,
                ):
                    placed = True
                    break
                _release_sewing_rows(new_entries, entries, reserved, day_job_count)
            cursor = next_workday(cursor, off)
            guard += 1
        if not placed:
            _restore_sewing_rows(old, entries, reserved, day_job_count)


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
    """If a weekday has leftover room, pull the next day's job up — not a later leftover scrap."""
    off = config.sewing_off_days()

    def day_free(day: date) -> float:
        _regular, _emergency, _total, _already, _setup, free = _sewing_capacity(
            day, config, reserved, day_job_count, setup_units, False
        )
        return free

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
        cursor = next_workday(planning_start, off, include=True)
        if cursor < first_busy:
            cursor = first_busy
        while cursor < last:
            if day_free(cursor) <= 1e-6:
                cursor = next_workday(cursor, off)
                continue
            starts: Dict[str, date] = {}
            for row in entries:
                oid = _text(row.get("orderNumber"))
                day = parse_date(row.get("date"))
                if not oid or not day or row.get("locked") or oid in locks_by_order:
                    continue
                starts[oid] = min(starts[oid], day) if oid in starts else day
            nxt = next_workday(cursor, off)
            already_here = {
                oid
                for oid, start in starts.items()
                if start <= cursor
            }
            later = [
                oid
                for oid, start in starts.items()
                if start == nxt and (not already_here or oid in already_here)
            ]
            if not later:
                cursor = next_workday(cursor, off)
                continue
            later.sort(key=lambda oid: (starts[oid], oid))
            moved = False
            for oid in later:
                order = order_lookup.get(oid)
                group = group_lookup.get(oid)
                if not order or not group or is_hard_date(order):
                    continue
                group_ship = group.get("required_ship_date") or order.get("required_ship_date")
                units = max(0.0, _number(order.get("sewing_units")))
                old = [row for row in entries if _text(row.get("orderNumber")) == oid]
                if not old:
                    continue
                customer_key = normalize_customer(order.get("customer") or old[0].get("customer"))
                if units >= 40:
                    other_on_day = sum(
                        _number(row.get("capacityUnits"))
                        for row in entries
                        if parse_date(row.get("date")) == cursor
                        and normalize_customer(row.get("customer")) != customer_key
                    )
                    if other_on_day >= 1e-9:
                        continue
                other_dates = _customer_sewing_dates(entries, customer_key, oid)
                _release_sewing_rows(old, entries, reserved, day_job_count)
                if not _can_place_contiguous(cursor, units, None, day_free, off):
                    _restore_sewing_rows(old, entries, reserved, day_job_count)
                    continue
                new_entries, remaining = _place_sewing_forward(
                    take_day, order, group, group_ship, cursor, units, off
                )
                if _commit_sewing_move(
                    new_entries, remaining, order, group_ship, off,
                    entries, embroidery_deadlines, other_dates,
                ):
                    moved = True
                    changed = True
                    break
                _release_sewing_rows(new_entries, entries, reserved, day_job_count)
                _restore_sewing_rows(old, entries, reserved, day_job_count)
            if not moved:
                cursor = next_workday(cursor, off)


def _front_load_sewing(
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
    """Move as many pieces as possible from a later day onto leftover earlier capacity."""
    off = config.sewing_off_days()

    def day_free(day: date) -> float:
        _regular, _emergency, _total, _already, _setup, free = _sewing_capacity(
            day, config, reserved, day_job_count, setup_units, False
        )
        return free

    seen: set[str] = set()
    order_ids: List[str] = []
    for row in entries:
        oid = _text(row.get("orderNumber"))
        if not oid or oid in seen or oid in locks_by_order or row.get("locked"):
            continue
        seen.add(oid)
        order_ids.append(oid)

    for oid in order_ids:
        order = order_lookup.get(oid)
        group = group_lookup.get(oid)
        if not order or not group or is_hard_date(order):
            continue
        old = [row for row in entries if _text(row.get("orderNumber")) == oid]
        dates = _sewing_entry_dates(old)
        if len(dates) < 2:
            continue
        group_ship = group.get("required_ship_date") or order.get("required_ship_date")
        units = max(0.0, _number(order.get("sewing_units")))
        customer_key = normalize_customer(order.get("customer") or old[0].get("customer"))
        other_dates = _customer_sewing_dates(entries, customer_key, oid)
        first = min(dates)
        last = max(dates)
        _release_sewing_rows(old, entries, reserved, day_job_count)
        new_entries, remaining = _place_sewing_forward(
            take_day, order, group, group_ship, first, units, off, last
        )
        if _commit_sewing_move(
            new_entries, remaining, order, group_ship, off,
            entries, embroidery_deadlines, other_dates,
        ):
            continue
        _release_sewing_rows(new_entries, entries, reserved, day_job_count)
        _restore_sewing_rows(old, entries, reserved, day_job_count)


def _spill_sewing_over_capacity(
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
    """If a day is over regular+emergency, move the extra pieces to the next workday."""
    off = config.sewing_off_days()
    guard = 0
    changed = True
    while changed and guard < 80:
        changed = False
        guard += 1
        days = sorted({
            day for day in (parse_date(row.get("date")) for row in entries) if day
        })
        for day in days:
            _regular, _emergency, cap = _day_sewing_cap(day, config)
            extra = reserved[day] - cap
            if extra <= 1e-6:
                continue
            rows = [
                row for row in entries
                if parse_date(row.get("date")) == day
                and not row.get("locked")
                and _text(row.get("orderNumber")) not in locks_by_order
            ]
            rows.sort(key=lambda row: (
                1 if is_hard_date(order_lookup.get(_text(row.get("orderNumber"))) or {}) else 0,
                -_number(row.get("capacityUnits")),
            ))
            nxt = next_workday(day, off)
            for row in rows:
                if extra <= 1e-6:
                    break
                oid = _text(row.get("orderNumber"))
                order = order_lookup.get(oid)
                group = group_lookup.get(oid)
                if not order or not group:
                    continue
                take = min(extra, _number(row.get("capacityUnits")))
                if take <= 1e-9:
                    continue
                row["capacityUnits"] = round(_number(row.get("capacityUnits")) - take, 4)
                reserved[day] -= take
                extra -= take
                group_ship = group.get("required_ship_date") or order.get("required_ship_date")
                same_order = [item for item in entries if _text(item.get("orderNumber")) == oid]
                leftover = take
                place = nxt
                inner = 0
                while leftover > 1e-9 and inner < 40:
                    leftover, entry = take_day(
                        place, order, group, group_ship, leftover,
                        allow_emergency=True, existing_entries=same_order,
                    )
                    if entry and entry not in entries:
                        entries.append(entry)
                    place = next_workday(place, off)
                    inner += 1
                if _number(row.get("capacityUnits")) <= 1e-9:
                    reserved[day] = max(0.0, reserved[day] - _number(row.get("setupUnits")))
                    day_job_count[day] = max(0, day_job_count[day] - 1)
                    if row in entries:
                        entries.remove(row)
                _annotate_day_quantities(
                    [item for item in entries if _text(item.get("orderNumber")) == oid],
                    order,
                )
                changed = True


def _keep_sewing_together(
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
    """If a job or customer skips a weekday, move the early pieces onto the later block."""
    off = config.sewing_off_days()

    def day_free(day: date) -> float:
        _regular, _emergency, _total, _already, _setup, free = _sewing_capacity(
            day, config, reserved, day_job_count, setup_units, False
        )
        return free

    def try_start(oid: str, start: date) -> bool:
        order = order_lookup.get(oid)
        group = group_lookup.get(oid)
        if not order or not group:
            return False
        old = [row for row in entries if _text(row.get("orderNumber")) == oid]
        if not old or any(row.get("locked") for row in old) or oid in locks_by_order:
            return False
        group_ship = group.get("required_ship_date") or order.get("required_ship_date")
        units = max(0.0, _number(order.get("sewing_units")))
        customer_key = normalize_customer(order.get("customer") or old[0].get("customer"))
        other_dates = [day for day in _customer_sewing_dates(entries, customer_key, oid) if day >= start]
        _release_sewing_rows(old, entries, reserved, day_job_count)
        last = previous_workday(group_ship, off, include=True) if group_ship else start
        if last < start:
            last = start
        if not _can_place_contiguous(start, units, last, day_free, off):
            _restore_sewing_rows(old, entries, reserved, day_job_count)
            return False
        new_entries, remaining = _place_sewing_forward(
            take_day, order, group, group_ship, start, units, off, last
        )
        customer_last = max(other_dates, default=start)
        if _commit_sewing_move(
            new_entries, remaining, order, group_ship, off,
            entries, embroidery_deadlines, other_dates,
            latest_ok=max(customer_last, last),
        ):
            return True
        _release_sewing_rows(new_entries, entries, reserved, day_job_count)
        _restore_sewing_rows(old, entries, reserved, day_job_count)
        return False

    changed = True
    guard = 0
    while changed and guard < 40:
        changed = False
        guard += 1
        by_order: Dict[str, List[dict]] = defaultdict(list)
        by_customer: Dict[str, List[str]] = defaultdict(list)
        for row in entries:
            oid = _text(row.get("orderNumber"))
            if not oid:
                continue
            by_order[oid].append(row)
        for oid, rows in by_order.items():
            order = order_lookup.get(oid)
            customer_key = normalize_customer(
                (order or {}).get("customer") or rows[0].get("customer")
            )
            if customer_key and oid not in by_customer[customer_key]:
                by_customer[customer_key].append(oid)
            hole = _interior_sewing_hole(_sewing_entry_dates(rows), off)
            if not hole:
                continue
            late_start = next((day for day in _sewing_entry_dates(rows) if day > hole), None)
            if late_start and try_start(oid, late_start):
                changed = True
            elif order and not is_hard_date(order) and try_start(oid, hole):
                changed = True
        if changed:
            continue
        for customer_key, oids in by_customer.items():
            dates = _customer_sewing_dates(entries, customer_key)
            hole = _interior_sewing_hole(dates, off)
            if not hole:
                continue
            late_start = next((day for day in dates if day > hole), None)
            early = []
            for oid in oids:
                rows = by_order.get(oid) or []
                last = max(_sewing_entry_dates(rows), default=None)
                if last and last < hole:
                    early.append(oid)
            for oid in early:
                order = order_lookup.get(oid)
                if late_start and try_start(oid, late_start):
                    changed = True
                elif order and not is_hard_date(order) and try_start(oid, hole):
                    changed = True
            if changed:
                break


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
        if is_towel_or_needlepoint(order.get("product")):
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
