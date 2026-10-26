"""
Sales pieces and revenue by rep for the overview breakdown.

Pure aggregation: no Flask or Google Sheets imports. The route in server.py
reads Production Orders and passes line dicts here.

A blank rep is House Account. Products whose name contains "back" are left
out, matching Sold This Week. Revenue is unit price times quantity.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

HOUSE_ACCOUNT = "House Account"
HOUSE_KEY = "house account"
DAILY_PIECE_GOAL = 75
WEEKLY_PIECE_GOAL = 375


def parse_number(raw: Any) -> float:
    """Sheet number, serial, or currency-ish text to a finite float."""
    if raw is None or isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value != value or value in (float("inf"), float("-inf")):
            return 0.0
        return value
    text = str(raw).strip().replace(",", "").replace("$", "").replace(" ", "")
    if not text:
        return 0.0
    try:
        value = float(text)
    except ValueError:
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            return 0.0
        try:
            value = float(match.group(0))
        except ValueError:
            return 0.0
    if value != value or value in (float("inf"), float("-inf")):
        return 0.0
    return value


def _coerce_date(val: Any) -> Optional[date]:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        text = val.strip()
        if len(text) >= 10:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
    return None


def _is_back_product(name: Any) -> bool:
    return "back" in str(name or "").lower()


def _rep_parts(raw: Any):
    label = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not label or label.casefold() == HOUSE_KEY:
        return HOUSE_KEY, HOUSE_ACCOUNT
    return label.casefold(), label


def _round2(value: float) -> float:
    return round(float(value), 2)


def _top_products(bucket: Dict[str, Dict[str, float]], limit: int = 8) -> List[dict]:
    rows = [
        {
            "name": name,
            "pieces": _round2(vals["pieces"]),
            "revenue": _round2(vals["revenue"]),
        }
        for name, vals in bucket.items()
        if vals["pieces"] or vals["revenue"]
    ]
    rows.sort(key=lambda row: (-row["revenue"], -row["pieces"], row["name"].lower()))
    return rows[:limit]


def build_sales_breakdown(lines: Iterable[dict], today: date) -> dict:
    """
    Aggregate order lines into daily rep buckets for this year and last year.

    Each line: date, qty, unit_price, rep, product, product_label, order_id.
    """
    if isinstance(today, datetime):
        today = today.date()

    window_start = date(today.year - 1, 1, 1)
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    year_start = date(today.year, 1, 1)

    # day -> rep key -> pieces/revenue/orders
    day_reps: Dict[str, Dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"pieces": 0.0, "revenue": 0.0, "orders": set()})
    )
    day_orders: Dict[str, set] = defaultdict(set)
    spellings: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    products = {
        key: defaultdict(lambda: {"pieces": 0.0, "revenue": 0.0})
        for key in ("day", "week", "month", "year")
    }
    stats = {
        "seen": 0,
        "counted": 0,
        "skippedBack": 0,
        "skippedNoDate": 0,
        "skippedOutside": 0,
        "skippedZeroQty": 0,
    }

    for line in lines or []:
        stats["seen"] += 1
        if not isinstance(line, dict):
            continue
        if _is_back_product(line.get("product")):
            stats["skippedBack"] += 1
            continue
        sold_on = _coerce_date(line.get("date"))
        if sold_on is None:
            stats["skippedNoDate"] += 1
            continue
        if sold_on < window_start or sold_on > today:
            stats["skippedOutside"] += 1
            continue
        qty = parse_number(line.get("qty"))
        if qty == 0:
            stats["skippedZeroQty"] += 1
            continue
        unit_price = parse_number(line.get("unit_price"))
        revenue = round(qty * unit_price, 2)
        rep_key, rep_label = _rep_parts(line.get("rep"))
        order_id = str(line.get("order_id") or "").strip() or f"line-{stats['seen']}"
        day_key = sold_on.isoformat()

        bucket = day_reps[day_key][rep_key]
        bucket["pieces"] += qty
        bucket["revenue"] += revenue
        bucket["orders"].add(order_id)
        day_orders[day_key].add(order_id)
        spellings[rep_key][rep_label] += abs(qty)

        label = str(line.get("product_label") or line.get("product") or "").strip() or "Untitled"
        if len(label) > 120:
            label = label[:117] + "..."
        for period in _periods_for(sold_on, today, week_start, month_start, year_start):
            products[period][label]["pieces"] += qty
            products[period][label]["revenue"] += revenue
        stats["counted"] += 1

    display_of = {}
    for rep_key, spells in spellings.items():
        if rep_key == HOUSE_KEY:
            display_of[rep_key] = HOUSE_ACCOUNT
            continue
        best_label = max(
            spells.items(),
            key=lambda item: (
                item[1],
                1 if any(ch.islower() for ch in item[0]) and any(ch.isupper() for ch in item[0]) else 0,
                len(item[0]),
                item[0],
            ),
        )[0]
        display_of[rep_key] = best_label

    days_out = []
    week_pieces = 0.0
    for day_key in sorted(day_reps.keys()):
        sold_on = date.fromisoformat(day_key)
        merged: Dict[str, dict] = {}
        pieces = 0.0
        revenue = 0.0
        for rep_key, agg in day_reps[day_key].items():
            name = display_of.get(rep_key, HOUSE_ACCOUNT)
            slot = merged.setdefault(name, {"pieces": 0.0, "revenue": 0.0, "orders": set()})
            slot["pieces"] += agg["pieces"]
            slot["revenue"] += agg["revenue"]
            slot["orders"].update(agg["orders"])
            pieces += agg["pieces"]
            revenue += agg["revenue"]
        if week_start <= sold_on <= today:
            week_pieces += pieces
        rep_payload = {}
        for name, agg in merged.items():
            if not agg["pieces"] and not agg["revenue"] and not agg["orders"]:
                continue
            rep_payload[name] = {
                "pieces": _round2(agg["pieces"]),
                "revenue": _round2(agg["revenue"]),
                "orders": len(agg["orders"]),
            }
        if not rep_payload:
            continue
        days_out.append(
            {
                "date": day_key,
                "pieces": _round2(pieces),
                "revenue": _round2(revenue),
                "orders": len(day_orders.get(day_key) or ()),
                "reps": rep_payload,
            }
        )

    ytd_revenue: Dict[str, float] = defaultdict(float)
    prior_revenue: Dict[str, float] = defaultdict(float)
    for day in days_out:
        year = int(day["date"][:4])
        target = ytd_revenue if year == today.year else prior_revenue
        for name, vals in day["reps"].items():
            target[name] += float(vals["revenue"] or 0)

    names = set(ytd_revenue) | set(prior_revenue)
    reps = sorted(names, key=lambda name: (-ytd_revenue.get(name, 0.0), -prior_revenue.get(name, 0.0), name.lower()))

    return {
        "asOf": today.isoformat(),
        "timezone": "America/New_York",
        "houseLabel": HOUSE_ACCOUNT,
        "dailyGoal": DAILY_PIECE_GOAL,
        "weeklyGoal": WEEKLY_PIECE_GOAL,
        "source": "Production Orders",
        "weekPieces": _round2(week_pieces),
        "ranges": {
            "day": {"start": today.isoformat(), "end": today.isoformat()},
            "week": {"start": week_start.isoformat(), "end": today.isoformat()},
            "month": {"start": month_start.isoformat(), "end": today.isoformat()},
            "year": {"start": year_start.isoformat(), "end": today.isoformat()},
            "windowStart": window_start.isoformat(),
        },
        "reps": reps,
        "days": days_out,
        "topProducts": {key: _top_products(products[key]) for key in ("day", "week", "month", "year")},
        "stats": stats,
        "note": (
            "Pieces are Production Orders quantity. A product whose name contains "
            '"back" is left out, same as Sold This Week. Revenue is price times quantity. '
            "A blank sales rep is House Account."
        ),
    }


def _periods_for(sold_on: date, today: date, week_start: date, month_start: date, year_start: date):
    found = []
    if sold_on == today:
        found.append("day")
    if week_start <= sold_on <= today:
        found.append("week")
    if month_start <= sold_on <= today:
        found.append("month")
    if year_start <= sold_on <= today:
        found.append("year")
    return found
