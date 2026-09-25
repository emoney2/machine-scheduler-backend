"""Long-lead fur/material forecasting for the Overview electronic Kanban."""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta


METERS_PER_ROLL = 77.5
YARDS_PER_METER = 1.0 / 0.9144
LEAD_TIME_DAYS = 90
DELAY_BUFFER_DAYS = 21
COMMIT_HORIZON_DAYS = LEAD_TIME_DAYS
PROTECTED_WEEKS = 16
LONG_NECK_FACTOR = 1.15
DRIVER_YARD_CALIBRATION = 9.5 / (160.0 / 14.0)
DEFAULT_ORDER_YARDS = 700
PHYSICAL_BASELINE_DATE = date(2026, 9, 18)

TRACKED_MATERIALS = (
    {
        "id": "BLACK-FUR",
        "kanbanId": "MAT-BLACK-FUR",
        "name": "Black Fur",
        "aliases": ("black fur", "black"),
        "rollsOnHand": 4,
        "orderYards": DEFAULT_ORDER_YARDS,
    },
    {
        "id": "LIGHT-GREY-FUR",
        "kanbanId": "MAT-LIGHT-GREY-FUR",
        "name": "Light Grey Fur",
        "aliases": ("light grey fur", "light gray fur", "light grey", "light gray"),
        "rollsOnHand": 6,
        "orderYards": DEFAULT_ORDER_YARDS,
    },
)


def yards_from_rolls(rolls, meters_per_roll=METERS_PER_ROLL) -> float:
    return max(0.0, float(rolls)) * float(meters_per_roll) * YARDS_PER_METER


def rolls_from_yards(yards, meters_per_roll=METERS_PER_ROLL) -> float:
    per_roll = float(meters_per_roll) * YARDS_PER_METER
    if per_roll <= 0:
        return 0.0
    return max(0.0, float(yards)) / per_roll


def _number(value, default=0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and value > 0:
        try:
            return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
        except (OverflowError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _norm(value) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _notes_json(row) -> dict:
    try:
        value = json.loads(str(row.get("Notes") or ""))
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def material_from_name(name):
    key = _norm(name)
    if not key:
        return None
    for item in TRACKED_MATERIALS:
        if key == _norm(item["name"]) or key in item["aliases"]:
            return item
    return None


def _log_header_index(headers, names, start_at=0) -> int:
    normalized = [_norm(header) for header in headers]
    wanted = [_norm(name) for name in names]
    for index, key in enumerate(normalized):
        if index >= start_at and key in wanted:
            return index
    return -1


def _material_log_dicts(log_rows) -> list[dict]:
    if not log_rows:
        return []
    if isinstance(log_rows[0], dict):
        return list(log_rows)
    headers = [str(header or "").strip() for header in (log_rows[0] or [])]
    if not headers:
        headers = ["Date", "Order #", "", "", "", "Material", "QTY", "IN/OUT", "O/R"]
    material_col = _log_header_index(headers, ("material", "materials"))
    if material_col < 0:
        headers = list(headers)
        while len(headers) < 9:
            headers.append("")
        headers[5] = headers[5] or "Material"
        headers[6] = headers[6] or "QTY"
        headers[7] = headers[7] or "IN/OUT"
        headers[8] = headers[8] or "O/R"
        headers[0] = headers[0] or "Date"
        headers[1] = headers[1] or "Order #"
    rows = []
    for raw in log_rows[1:]:
        row = list(raw or []) + [""] * max(0, len(headers) - len(raw or []))
        rows.append(dict(zip(headers, row)))
    return rows


def parse_material_log(log_rows) -> list[dict]:
    """Read Material Log IN/OUT + O/R rows used for vendor inbound."""
    events = []
    for row in _material_log_dicts(log_rows):
        catalog = material_from_name(
            row.get("Material") or row.get("Materials") or row.get("material")
        )
        if not catalog:
            continue
        qty = _number(row.get("QTY") if row.get("QTY") not in (None, "") else None)
        if qty <= 0:
            qty = _number(row.get("Qty") or row.get("Quantity"))
        if qty <= 0:
            continue
        events.append(
            {
                "materialId": catalog["id"],
                "qty": qty,
                "inout": _norm(row.get("IN/OUT") or row.get("In/Out") or row.get("inout")),
                "status": _norm(row.get("O/R") or row.get("O / R") or row.get("Ordered/Received")),
                "when": _date(row.get("Date") or row.get("Timestamp")),
                "orderKey": _norm(row.get("Order #") or row.get("Order") or row.get("Order Number")),
            }
        )
    return events


def vendor_log_quantities(item, log_rows, cutoff: date) -> dict:
    inbound = 0.0
    received_after = 0.0
    for event in parse_material_log(log_rows):
        if event["materialId"] != item["id"] or event["inout"] != "in":
            continue
        if event["orderKey"] == "readjustment":
            continue
        if event["status"] == "ordered":
            inbound += event["qty"]
        elif event["status"] == "received" and event["when"] and event["when"] >= cutoff:
            received_after += event["qty"]
    return {
        "inboundYards": inbound,
        "receivedAfterYards": received_after,
    }


def _header_name(value) -> str:
    return _norm(value).rstrip(" .")


def _inventory_row_dicts(inventory_rows) -> list[dict]:
    if not inventory_rows:
        return []
    if isinstance(inventory_rows[0], dict):
        return list(inventory_rows)
    headers = [str(header or "").strip() for header in (inventory_rows[0] or [])]
    rows = []
    for raw in inventory_rows[1:]:
        row = list(raw or []) + [""] * max(0, len(headers) - len(raw or []))
        rows.append(dict(zip(headers, row)))
    return rows


def inventory_levels_from_sheet(inventory_rows) -> dict:
    """Read Material Inventory on-hand and On Order for tracked furs."""
    levels = {}
    for row in _inventory_row_dicts(inventory_rows):
        name = None
        inventory = None
        on_order = 0.0
        for key, value in row.items():
            header = _header_name(key)
            if header in ("materials", "material") and name is None:
                name = value
            elif header == "inventory" and inventory is None:
                if value not in (None, ""):
                    inventory = _number(value)
            elif header in ("on order", "on-order"):
                if value not in (None, ""):
                    on_order = _number(value)
        catalog = material_from_name(name)
        if not catalog or inventory is None:
            continue
        levels[catalog["id"]] = {
            "inventoryYards": max(0.0, inventory),
            "onOrderYards": max(0.0, on_order),
        }
    return levels


def tracked_kanban_ids():
    return {item["kanbanId"] for item in TRACKED_MATERIALS}


def ppy_lookup_from_values(values) -> dict:
    """Read Table sheet values using the same PPY column fallback as material logging."""
    if not values or len(values) < 2:
        return {}
    headers = [str(header or "").strip() for header in (values[0] or [])]
    lower = [header.casefold() for header in headers]
    product_col = lower.index("product") if "product" in lower else 0
    ppy_col = lower.index("ppy") if "ppy" in lower else (5 if len(headers) > 5 else 1)
    lookup = {}
    for raw in values[1:]:
        row = list(raw or [])
        product = str(row[product_col] if product_col < len(row) else "").strip()
        ppy = _number(row[ppy_col] if ppy_col < len(row) else 0)
        if product and ppy > 0:
            lookup[product.casefold()] = ppy
    return lookup


def ppy_lookup(table_rows) -> dict:
    if table_rows and not isinstance(table_rows[0], dict):
        return ppy_lookup_from_values(table_rows)
    lookup = {}
    for row in table_rows or []:
        product = str(
            row.get("Product")
            or row.get("product")
            or row.get("PRODUCT")
            or ""
        ).strip()
        if not product:
            continue
        ppy = _number(
            row.get("PPY")
            or row.get("Ppy")
            or row.get("ppy")
            or row.get("Pieces Per Yard")
        )
        if ppy > 0:
            lookup[product.casefold()] = ppy
    return lookup


def _resolve_ppy(product: str, lookup: dict) -> float:
    key = (product or "").strip().casefold()
    ppy = _number(lookup.get(key))
    if "long neck" in key:
        sibling = " ".join(product.replace("Long Neck", " ").replace("long neck", " ").split())
        sib = _number(lookup.get(sibling.casefold())) if sibling else 0.0
        if sib <= 0 and "blade" in key:
            sib = _number(lookup.get("blade"))
        if sib > 0:
            ppy = min(ppy, sib) if ppy > 0 else sib
    return ppy


def fur_yards_for_product(product, quantity, ppy) -> float:
    qty = max(0.0, _number(quantity))
    rate = _number(ppy)
    if qty <= 0 or rate <= 0:
        return 0.0
    key = (product or "").strip().casefold()
    if "back" in key:
        return 0.0
    yards = qty / rate
    if "blade" not in key and "mallet" not in key:
        yards *= 2.0
    if "long neck" in key:
        yards *= LONG_NECK_FACTOR
    if "driver" in key:
        yards *= DRIVER_YARD_CALIBRATION
    return yards


def _need_date(row) -> date | None:
    return _date(
        row.get("Ship Date")
        or row.get("Ship")
        or row.get("Due Date")
        or row.get("Due")
    )


def is_deferred_demand(row, today: date) -> bool:
    """Uncut work due after the Turkey lead time does not reserve today's fur."""
    need = _need_date(row)
    if need is None:
        return False
    return need > today + timedelta(days=COMMIT_HORIZON_DAYS)


def _cut_progress(cut_row) -> tuple[str, float, float]:
    quantity = max(0.0, _number(cut_row.get("Quantity") or cut_row.get("Qty")))
    made = min(quantity, max(0.0, _number(cut_row.get("Quantity Made") or cut_row.get("Qty Made"))))
    status = str(cut_row.get("Status") or "").strip().casefold()
    if status == "complete":
        made = quantity
    return status, quantity, made


def _best_cut_row(existing, incoming):
    if existing is None:
        return incoming
    _, _, made_old = _cut_progress(existing)
    _, _, made_new = _cut_progress(incoming)
    return incoming if made_new >= made_old else existing


def usage_by_material(production_rows, cut_rows, table_rows, today=None) -> dict:
    """Return consumed/committed yards for each tracked fur from live orders + Cut List."""
    today = today or date.today()
    lookup = ppy_lookup(table_rows)
    cuts = {}
    for row in cut_rows or []:
        order_id = str(row.get("Order #") or "").strip()
        if not order_id:
            continue
        key = order_id.casefold()
        cuts[key] = _best_cut_row(cuts.get(key), row)

    by_id = {
        item["id"]: {
            "id": item["id"],
            "kanbanId": item["kanbanId"],
            "name": item["name"],
            "consumedYards": 0.0,
            "committedYards": 0.0,
            "deferredYards": 0.0,
            "history": [],
        }
        for item in TRACKED_MATERIALS
    }
    seen = set()

    for row in production_rows or []:
        order_id = str(row.get("Order #") or "").strip()
        product = str(row.get("Product") or "").strip()
        company = str(row.get("Company Name") or "").strip()
        if not order_id or not product or company.casefold() == "test":
            continue
        material = material_from_name(row.get("Fur Color"))
        if not material:
            continue
        ppy = _resolve_ppy(product, lookup)
        yards = _number(fur_yards_for_product(product, row.get("Quantity") or row.get("Qty"), ppy))
        if yards <= 0:
            continue
        dedupe = (order_id.casefold(), product.casefold(), material["id"])
        if dedupe in seen:
            continue
        seen.add(dedupe)

        cut = cuts.get(order_id.casefold())
        status, quantity, made = _cut_progress(cut or {})
        deferred = is_deferred_demand(row, today) and made <= 0 and status != "complete"
        if quantity > 0 and made > 0:
            consumed = yards * (made / quantity)
            committed = max(0.0, yards - consumed)
            deferred_yards = 0.0
        elif status == "complete":
            consumed, committed, deferred_yards = yards, 0.0, 0.0
        elif deferred:
            consumed, committed, deferred_yards = 0.0, 0.0, yards
        else:
            consumed, committed, deferred_yards = 0.0, yards, 0.0

        bucket = by_id[material["id"]]
        bucket["consumedYards"] += consumed
        bucket["committedYards"] += committed
        bucket["deferredYards"] += deferred_yards
        ordered_on = _date(row.get("Date") or row.get("Order Date"))
        if ordered_on and not deferred:
            bucket["history"].append((ordered_on, yards))

    for bucket in by_id.values():
        bucket["consumedYards"] = round(bucket["consumedYards"], 2)
        bucket["committedYards"] = round(bucket["committedYards"], 2)
        bucket["deferredYards"] = round(bucket["deferredYards"], 2)
    return by_id


def demand_forecast(history, today: date) -> dict:
    weekly = defaultdict(float)
    current_monday = today - timedelta(days=today.weekday())
    history_yards = 0.0
    first_date = None
    for ordered_on, yards in history or []:
        yards = _number(yards)
        if not ordered_on or ordered_on > today or yards <= 0:
            continue
        history_yards += yards
        first_date = ordered_on if first_date is None else min(first_date, ordered_on)
        monday = ordered_on - timedelta(days=ordered_on.weekday())
        if monday < current_monday:
            weekly[monday] += yards

    if first_date is None:
        return {
            "historyYards": 0,
            "weeklyRate": 0.0,
            "quarterGrowthPct": 0.0,
            "weeklyGrowthRate": 0.0,
            "protectedDemandYards": 0,
            "safetyStockYards": 0,
            "reorderPointYards": 0,
        }

    first_monday = first_date - timedelta(days=first_date.weekday())
    values = []
    cursor = first_monday
    while cursor < current_monday:
        values.append(weekly.get(cursor, 0.0))
        cursor += timedelta(days=7)

    recent = values[-13:] if values else []
    prior = values[-26:-13] if len(values) > 13 else []
    recent_rate = sum(recent) / max(1, len(recent))
    prior_rate = sum(prior) / max(1, len(prior)) if prior else recent_rate
    quarter_growth = (recent_rate / prior_rate - 1.0) if prior_rate > 0 else 0.0
    if recent_rate > 0 and prior_rate > 0:
        weekly_growth = (recent_rate / prior_rate) ** (1.0 / 13.0) - 1.0
    else:
        weekly_growth = 0.0
    weekly_growth = max(-0.01, min(0.01, weekly_growth))

    future = [
        recent_rate * ((1.0 + weekly_growth) ** week)
        for week in range(1, PROTECTED_WEEKS + 1)
    ]
    protected_demand = sum(future)
    variability = values[-26:]
    weekly_sd = statistics.stdev(variability) if len(variability) > 1 else 0.0
    safety_stock = 2.326 * weekly_sd * math.sqrt(PROTECTED_WEEKS)
    reorder_point = int(math.ceil((protected_demand + safety_stock) / 25.0) * 25)

    return {
        "historyYards": round(history_yards, 1),
        "weeklyRate": round(recent_rate, 2),
        "quarterGrowthPct": round(quarter_growth * 100.0, 1),
        "weeklyGrowthRate": weekly_growth,
        "protectedDemandYards": round(protected_demand),
        "safetyStockYards": round(safety_stock),
        "reorderPointYards": reorder_point,
    }


def _project_date(start: date, weekly_rate: float, weekly_growth: float, yards: float):
    yards = _number(yards)
    weekly_rate = _number(weekly_rate)
    weekly_growth = _number(weekly_growth)
    if yards <= 0:
        return start
    if weekly_rate <= 0:
        return None
    used = 0.0
    for week in range(1, 261):
        increment = weekly_rate * ((1.0 + weekly_growth) ** week)
        used += increment
        if used >= yards:
            previous = used - increment
            fraction = max(0.0, min(1.0, (yards - previous) / max(1.0, increment)))
            return start + timedelta(days=round((week - 1 + fraction) * 7))
    return None


def inventory_state(
    item,
    kanban_rows,
    consumed_yards: float,
    today: date,
    log_rows=None,
    sheet_level=None,
) -> dict:
    relevant = [
        row
        for row in (kanban_rows or [])
        if str(row.get("Kanban ID") or "").strip().upper() == item["kanbanId"]
    ]
    count_yards = yards_from_rolls(item["rollsOnHand"])
    count_consumed = consumed_yards
    count_date = date(1970, 1, 1)
    has_count = False

    for row in relevant:
        if str(row.get("Type") or "").strip().upper() != "MATERIAL_COUNT":
            continue
        event_date = _date(row.get("Timestamp")) or today
        if event_date >= count_date:
            notes = _notes_json(row)
            rolls = _number(notes.get("rolls"), item["rollsOnHand"])
            count_yards = max(
                0.0,
                _number(notes.get("yards"), _number(row.get("Event Qty"), yards_from_rolls(rolls))),
            )
            count_consumed = max(0.0, _number(notes.get("consumedYards"), consumed_yards))
            count_date = event_date
            has_count = True

    if not has_count:
        count_consumed = consumed_yards

    received_by_event = defaultdict(float)
    ordered_by_event = defaultdict(float)
    receipts_after_count = 0.0
    active_request = None

    for row in relevant:
        row_type = str(row.get("Type") or "").strip().upper()
        event_id = str(row.get("Event ID") or "").strip()
        quantity = max(0.0, _number(row.get("Event Qty")))
        event_date = _date(row.get("Timestamp"))
        if row_type == "ORDERED" and event_id:
            ordered_by_event[event_id] = max(ordered_by_event[event_id], quantity)
        elif row_type == "RECEIVED" and event_id:
            received_by_event[event_id] += quantity
            if (not has_count) or (event_date and event_date >= count_date):
                receipts_after_count += quantity
        elif row_type == "REQUEST":
            status = str(row.get("Event Status") or "").strip().casefold()
            if status in ("open", "ordered"):
                active_request = {
                    "eventId": event_id,
                    "status": status,
                    "quantity": round(quantity),
                }

    kanban_inbound = sum(
        max(0.0, quantity - received_by_event.get(event_id, 0.0))
        for event_id, quantity in ordered_by_event.items()
    )
    cutoff = count_date if has_count else PHYSICAL_BASELINE_DATE
    log_state = vendor_log_quantities(item, log_rows, cutoff)
    log_inbound = _number(log_state.get("inboundYards"))
    receipts_after_count += _number(log_state.get("receivedAfterYards"))
    if log_inbound and not active_request:
        active_request = {
            "eventId": "",
            "status": "ordered",
            "quantity": round(log_inbound),
            "source": "material-log",
        }

    from_sheet = isinstance(sheet_level, dict) and "inventoryYards" in sheet_level
    if from_sheet:
        # Material Inventory is the live ledger (OUTs already posted at order submit).
        physical = max(0.0, _number(sheet_level.get("inventoryYards")))
        inbound = max(0.0, _number(sheet_level.get("onOrderYards")))
        if inbound <= 0:
            inbound = log_inbound
        if inbound <= 0:
            inbound = kanban_inbound
        if inbound and not active_request:
            active_request = {
                "eventId": "",
                "status": "ordered",
                "quantity": round(inbound),
                "source": "material-inventory",
            }
        used_since_count = 0.0
        has_count = True
        count_consumed = consumed_yards
    else:
        inbound = kanban_inbound + log_inbound
        used_since_count = max(0.0, consumed_yards - count_consumed) if has_count else 0.0
        physical = max(0.0, count_yards + receipts_after_count - used_since_count)

    places = 2 if from_sheet else 1
    return {
        "physicalYards": round(physical, places),
        "inboundYards": round(inbound, places),
        "hasCount": has_count,
        "fromInventorySheet": from_sheet,
        "countConsumedYards": round(count_consumed, 2),
        "activeRequest": active_request,
    }


def _build_material_status(
    item, usage, kanban_rows, today: date, log_rows=None, sheet_level=None
) -> dict:
    inventory = inventory_state(
        item, kanban_rows, usage["consumedYards"], today, log_rows, sheet_level
    )
    forecast = demand_forecast(usage["history"], today)
    committed = _number(usage.get("committedYards"))
    deferred = _number(usage.get("deferredYards"))
    physical = _number(inventory.get("physicalYards"))
    # Sheet inventory already deducted Material Log OUTs at submit time.
    if inventory.get("fromInventorySheet"):
        uncommitted = physical
    else:
        uncommitted = max(0.0, physical - committed)
    position = max(0.0, uncommitted + _number(inventory.get("inboundYards")))
    reorder_point = _number(forecast.get("reorderPointYards"))
    trigger = position <= reorder_point
    weekly_rate = forecast["weeklyRate"]
    growth = forecast["weeklyGrowthRate"]
    depletion = _project_date(today, weekly_rate, growth, uncommitted)
    trigger_date = (
        today
        if trigger
        else _project_date(today, weekly_rate, growth, position - reorder_point)
    )
    level = "order_now" if trigger else "healthy"
    return {
        "id": item["id"],
        "kanbanId": item["kanbanId"],
        "name": item["name"],
        "level": level,
        "shouldCreateRequest": trigger
        and not inventory["activeRequest"]
        and _number(inventory.get("inboundYards")) <= 0,
        "physicalYards": inventory["physicalYards"],
        "physicalRolls": round(rolls_from_yards(inventory["physicalYards"]), 1),
        "committedYards": round(committed, 1),
        "deferredYards": round(deferred, 1),
        "uncommittedYards": round(
            uncommitted, 2 if inventory.get("fromInventorySheet") else 1
        ),
        "inboundYards": inventory["inboundYards"],
        "inventoryPositionYards": round(
            position, 2 if inventory.get("fromInventorySheet") else 1
        ),
        "reorderPointYards": reorder_point,
        "recommendedOrderYards": item["orderYards"],
        "weeklyDemandYards": weekly_rate,
        "quarterGrowthPct": forecast["quarterGrowthPct"],
        "protectedDemandYards": forecast["protectedDemandYards"],
        "safetyStockYards": forecast["safetyStockYards"],
        "projectedPhysicalDepletionDate": depletion.isoformat() if depletion else None,
        "projectedReorderDate": trigger_date.isoformat() if trigger_date else None,
        "activeRequest": inventory["activeRequest"],
        "consumedYards": usage["consumedYards"],
        "hasCount": inventory["hasCount"],
        "fromInventorySheet": bool(inventory.get("fromInventorySheet")),
        "unit": "yards",
        "metersPerRoll": METERS_PER_ROLL,
    }


def build_status(
    production_rows,
    cut_rows,
    table_rows,
    kanban_rows,
    *,
    today: date,
    log_rows=None,
    inventory_rows=None,
) -> dict:
    usage = usage_by_material(production_rows, cut_rows, table_rows, today=today)
    sheet_levels = inventory_levels_from_sheet(inventory_rows)
    materials = [
        _build_material_status(
            item,
            usage[item["id"]],
            kanban_rows,
            today,
            log_rows,
            sheet_levels.get(item["id"]),
        )
        for item in TRACKED_MATERIALS
    ]
    order_now = [row for row in materials if row["level"] == "order_now"]
    level = "order_now" if order_now else "healthy"
    return {
        "ok": True,
        "asOf": today.isoformat(),
        "level": level,
        "shouldCreateRequests": [row["kanbanId"] for row in materials if row["shouldCreateRequest"]],
        "materials": materials,
        "model": {
            "leadTimeDays": LEAD_TIME_DAYS,
            "delayBufferDays": DELAY_BUFFER_DAYS,
            "commitHorizonDays": COMMIT_HORIZON_DAYS,
            "serviceLevelPct": 99,
            "metersPerRoll": METERS_PER_ROLL,
            "orderYards": DEFAULT_ORDER_YARDS,
        },
    }
