"""Demand forecasting and inventory math for the magnet electronic Kanban."""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta


MAGNET_KANBAN_ID = "MAGNETS-NS"
INITIAL_INBOUND_EVENT_ID = "MAGNETS-INITIAL-2026-10-03"


def product_pair_multiplier(product) -> int:
    """Return N/S pairs consumed by one headcover."""
    name = str(product or "").strip().casefold()
    if "blade" in name or "center shaft" in name or "mid mallet" in name:
        return 2
    if "mallet" in name:
        return 1
    return 0


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
        # Google Sheets / Excel serial date epoch.
        try:
            return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
        except (OverflowError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    iso = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso).date()
    except ValueError:
        pass
    for fmt in (
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def fur_magnet_totals(rows) -> dict:
    """Calculate made and unfinished pair totals, ignoring duplicate Fur List rows."""
    by_order = {}
    for row in rows or []:
        product = row.get("Product")
        multiplier = product_pair_multiplier(product)
        order_id = str(row.get("Order #") or "").strip()
        if not multiplier or not order_id:
            continue
        quantity = max(0.0, _number(row.get("Quantity")))
        status = str(row.get("Status") or "").strip().casefold()
        made = (
            quantity
            if status == "complete"
            else min(quantity, max(0.0, _number(row.get("Quantity Made"))))
        )
        key = (order_id.casefold(), str(product or "").strip().casefold())
        previous = by_order.get(key)
        # Duplicate query rows occur in Fur List. Keep the copy with greatest progress.
        if previous is None or made > previous["made"]:
            by_order[key] = {
                "product": str(product or "").strip(),
                "quantity": quantity,
                "made": made,
                "multiplier": multiplier,
            }

    breakdown = defaultdict(lambda: {"unitsRemaining": 0.0, "pairsCommitted": 0.0})
    made_pairs = 0.0
    committed_pairs = 0.0
    for item in by_order.values():
        remaining = max(0.0, item["quantity"] - item["made"])
        made_pairs += item["made"] * item["multiplier"]
        committed_pairs += remaining * item["multiplier"]
        family = (
            "Blade"
            if "blade" in item["product"].casefold()
            else "Center-shaft mallet"
            if "center shaft" in item["product"].casefold()
            else "Mid mallet"
            if "mid mallet" in item["product"].casefold()
            else "Mallet"
        )
        breakdown[family]["unitsRemaining"] += remaining
        breakdown[family]["pairsCommitted"] += remaining * item["multiplier"]

    return {
        "madePairs": round(made_pairs),
        "committedPairs": round(committed_pairs),
        "breakdown": [
            {
                "family": family,
                "unitsRemaining": round(values["unitsRemaining"]),
                "pairsCommitted": round(values["pairsCommitted"]),
            }
            for family, values in sorted(
                breakdown.items(),
                key=lambda pair: pair[1]["pairsCommitted"],
                reverse=True,
            )
            if values["pairsCommitted"] > 0
        ],
    }


def demand_forecast(production_rows, today: date) -> dict:
    """Forecast pair demand using recent rate, measured growth, and a 99% buffer."""
    weekly = defaultdict(float)
    history_pairs = 0.0
    first_date = None
    current_monday = today - timedelta(days=today.weekday())

    for row in production_rows or []:
        multiplier = product_pair_multiplier(row.get("Product"))
        ordered_on = _date(row.get("Date") or row.get("Order Date"))
        quantity = max(0.0, _number(row.get("Quantity") or row.get("Qty")))
        if not multiplier or not ordered_on or ordered_on > today or quantity <= 0:
            continue
        pairs = quantity * multiplier
        history_pairs += pairs
        first_date = ordered_on if first_date is None else min(first_date, ordered_on)
        monday = ordered_on - timedelta(days=ordered_on.weekday())
        # Exclude the current partial week from rate and variability calculations.
        if monday < current_monday:
            weekly[monday] += pairs

    if first_date is None:
        return {
            "historyPairs": 0,
            "weeklyRate": 0,
            "quarterGrowthPct": 0,
            "weeklyGrowthRate": 0,
            "protectedDemandPairs": 0,
            "safetyStockPairs": 0,
            "reorderPointPairs": 0,
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

    protected_weeks = 13  # 70-day lead time + 21-day delay protection = 91 days.
    future = [
        recent_rate * ((1.0 + weekly_growth) ** week)
        for week in range(1, protected_weeks + 1)
    ]
    protected_demand = sum(future)
    variability_values = values[-26:]
    weekly_sd = statistics.stdev(variability_values) if len(variability_values) > 1 else 0.0
    safety_stock = 2.326 * weekly_sd * math.sqrt(protected_weeks)
    reorder_point = int(math.ceil((protected_demand + safety_stock) / 100.0) * 100)

    return {
        "historyPairs": round(history_pairs),
        "weeklyRate": round(recent_rate, 1),
        "quarterGrowthPct": round(quarter_growth * 100.0, 1),
        "weeklyGrowthRate": weekly_growth,
        "protectedDemandPairs": round(protected_demand),
        "safetyStockPairs": round(safety_stock),
        "reorderPointPairs": reorder_point,
    }


def _event_timestamp(row) -> date | None:
    return _date(row.get("Timestamp"))


def _notes_json(row) -> dict:
    try:
        value = json.loads(str(row.get("Notes") or ""))
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def inventory_state(
    kanban_rows,
    current_made_pairs: int,
    *,
    base_count_pairs: int,
    baseline_made_pairs: int,
    baseline_date: date,
    initial_inbound_pairs: int,
    initial_inbound_due: date,
    today: date,
) -> dict:
    """Derive physical inventory and inbound supply from baseline plus Kanban events."""
    relevant = [
        row
        for row in (kanban_rows or [])
        if str(row.get("Kanban ID") or "").strip().upper() == MAGNET_KANBAN_ID
    ]
    count_north = float(base_count_pairs)
    count_south = float(base_count_pairs)
    count_made_pairs = float(baseline_made_pairs)
    count_date = baseline_date

    for row in relevant:
        if str(row.get("Type") or "").strip().upper() != "MAGNET_COUNT":
            continue
        event_date = _event_timestamp(row)
        if event_date and event_date >= count_date:
            notes = _notes_json(row)
            fallback_count = max(0.0, _number(row.get("Event Qty"), count_north))
            count_north = max(0.0, _number(notes.get("north"), fallback_count))
            count_south = max(0.0, _number(notes.get("south"), fallback_count))
            count_made_pairs = max(
                0.0, _number(notes.get("madePairs"), current_made_pairs)
            )
            count_date = event_date

    received_by_event = defaultdict(float)
    ordered_by_event = defaultdict(float)
    receipts_after_count = 0.0
    initial_received = False
    active_request = None

    for row in relevant:
        row_type = str(row.get("Type") or "").strip().upper()
        event_id = str(row.get("Event ID") or "").strip()
        quantity = max(0.0, _number(row.get("Event Qty")))
        event_date = _event_timestamp(row)
        if row_type == "ORDERED" and event_id:
            ordered_by_event[event_id] = max(ordered_by_event[event_id], quantity)
        elif row_type == "RECEIVED" and event_id:
            received_by_event[event_id] += quantity
            if event_id == INITIAL_INBOUND_EVENT_ID:
                initial_received = True
            if event_date and event_date >= count_date:
                receipts_after_count += quantity
        elif row_type == "REQUEST":
            status = str(row.get("Event Status") or "").strip().casefold()
            if status in ("open", "ordered"):
                active_request = {
                    "eventId": event_id,
                    "status": status,
                    "quantity": round(quantity),
                }

    ordered_inbound = sum(
        max(0.0, quantity - received_by_event.get(event_id, 0.0))
        for event_id, quantity in ordered_by_event.items()
    )
    initial_inbound = 0.0 if initial_received else float(initial_inbound_pairs)
    made_since_count = max(0.0, float(current_made_pairs) - count_made_pairs)
    north_on_hand = max(0.0, count_north + receipts_after_count - made_since_count)
    south_on_hand = max(0.0, count_south + receipts_after_count - made_since_count)
    physical_pairs = min(north_on_hand, south_on_hand)

    return {
        "physicalPairs": round(physical_pairs),
        "northOnHand": round(north_on_hand),
        "southOnHand": round(south_on_hand),
        "initialInboundPairs": round(initial_inbound),
        "orderedInboundPairs": round(ordered_inbound),
        "inboundPairs": round(initial_inbound + ordered_inbound),
        "initialInboundDue": initial_inbound_due.isoformat(),
        "initialInboundReceived": initial_received,
        "countAsOf": count_date.isoformat(),
        "activeRequest": active_request,
    }


def _project_date(start: date, starting_weekly_rate: float, weekly_growth: float, pairs: float):
    if pairs <= 0:
        return start
    if starting_weekly_rate <= 0:
        return None
    used = 0.0
    for week in range(1, 261):
        used += starting_weekly_rate * ((1.0 + weekly_growth) ** week)
        if used >= pairs:
            previous = used - starting_weekly_rate * ((1.0 + weekly_growth) ** week)
            week_demand = max(1.0, used - previous)
            fraction = max(0.0, min(1.0, (pairs - previous) / week_demand))
            return start + timedelta(days=round((week - 1 + fraction) * 7))
    return None


def build_status(
    production_rows,
    fur_rows,
    kanban_rows,
    *,
    today: date,
    base_count_pairs=3000,
    baseline_made_pairs=6229,
    baseline_date=date(2026, 9, 18),
    initial_inbound_pairs=5000,
    initial_inbound_due=date(2026, 10, 3),
    order_quantity_pairs=5000,
) -> dict:
    fur = fur_magnet_totals(fur_rows)
    forecast = demand_forecast(production_rows, today)
    inventory = inventory_state(
        kanban_rows,
        fur["madePairs"],
        base_count_pairs=base_count_pairs,
        baseline_made_pairs=baseline_made_pairs,
        baseline_date=baseline_date,
        initial_inbound_pairs=initial_inbound_pairs,
        initial_inbound_due=initial_inbound_due,
        today=today,
    )
    committed = fur["committedPairs"]
    uncommitted = max(0, inventory["physicalPairs"] - committed)
    inventory_position = max(0, uncommitted + inventory["inboundPairs"])
    reorder_point = forecast["reorderPointPairs"]
    trigger = inventory_position <= reorder_point
    weekly_rate = forecast["weeklyRate"]
    growth = forecast["weeklyGrowthRate"]

    depletion_date = _project_date(today, weekly_rate, growth, uncommitted)
    trigger_date = (
        today
        if trigger
        else _project_date(today, weekly_rate, growth, inventory_position - reorder_point)
    )
    initial_due = _date(inventory["initialInboundDue"])
    inbound_overdue = bool(
        inventory["initialInboundPairs"] > 0 and initial_due and today > initial_due
    )

    level = "order_now" if trigger else "watch" if inbound_overdue else "healthy"
    return {
        "ok": True,
        "asOf": today.isoformat(),
        "level": level,
        "shouldCreateRequest": trigger and not inventory["activeRequest"],
        "physicalPairs": inventory["physicalPairs"],
        "northOnHand": inventory["northOnHand"],
        "southOnHand": inventory["southOnHand"],
        "committedPairs": committed,
        "uncommittedPairs": uncommitted,
        "inboundPairs": inventory["inboundPairs"],
        "inventoryPositionPairs": inventory_position,
        "reorderPointPairs": reorder_point,
        "recommendedOrderPairs": order_quantity_pairs,
        "weeklyDemandPairs": weekly_rate,
        "quarterGrowthPct": forecast["quarterGrowthPct"],
        "protectedDemandPairs": forecast["protectedDemandPairs"],
        "safetyStockPairs": forecast["safetyStockPairs"],
        "projectedPhysicalDepletionDate": depletion_date.isoformat() if depletion_date else None,
        "projectedReorderDate": trigger_date.isoformat() if trigger_date else None,
        "initialInboundDue": inventory["initialInboundDue"],
        "initialInboundPairs": inventory["initialInboundPairs"],
        "initialInboundReceived": inventory["initialInboundReceived"],
        "inboundOverdue": inbound_overdue,
        "activeRequest": inventory["activeRequest"],
        "commitmentBreakdown": fur["breakdown"],
        "historyPairs": forecast["historyPairs"],
        "model": {
            "leadTimeDays": 70,
            "delayBufferDays": 21,
            "serviceLevelPct": 99,
        },
    }
