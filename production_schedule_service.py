"""Live-data orchestration for the deterministic production scheduler."""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import smtplib
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Callable, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

import requests

from production_scheduler import (
    BUSINESS_TZ,
    LOCAL_DELIVERY_TRANSIT_DAYS,
    SchedulerConfig,
    assign_sewer_capacities,
    build_schedule,
    is_local_delivery,
    normalize_order_number,
    parse_date,
    parse_sewers,
    resolve_required_ship_date,
    transit_days_for_service,
)
from schedule_store import ScheduleSheetStore, friendly_sheets_error

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
GROUND_CODE = "03"
SERVICE_CODES = {
    "GROUND": "03",
    "UPS GROUND": "03",
    "NEXT DAY AIR": "01",
    "NEXT DAY AIR EARLY": "14",
    "NEXT DAY AIR SAVER": "13",
    "2ND DAY AIR": "02",
    "SECOND DAY AIR": "02",
    "3 DAY SELECT": "12",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _rows_to_dicts(values: Sequence[Sequence[Any]]) -> List[dict]:
    if not values:
        return []
    headers = [_text(h) for h in values[0]]
    out = []
    for raw in values[1:]:
        row = list(raw or []) + [""] * max(0, len(headers) - len(raw or []))
        out.append(dict(zip(headers, row)))
    return out


def _active(row: dict) -> bool:
    stage = _text(row.get("Stage")).upper()
    if stage in {"COMPLETE", "COMPLETED", "CANCELED", "CANCELLED", "SHIPPED", "HOLD", "HELD", "ON HOLD"}:
        return False
    qty = max(0.0, _number(row.get("Quantity")))
    shipped = max(0.0, _number(row.get("Shipped")))
    return bool(normalize_order_number(row.get("Order #"))) and qty > shipped


def _parse_thread_usage(thread_rows: Sequence[dict]) -> Dict[str, Dict[str, float]]:
    """OUT length by order/color, converted from feet to cone-equivalents."""
    out: Dict[str, Dict[str, float]] = {}
    for row in thread_rows:
        if _text(row.get("IN/OUT")).upper() != "OUT":
            continue
        oid = normalize_order_number(row.get("Order Number") or row.get("Order #"))
        color_match = re.search(r"\b(\d{4})\b", _text(row.get("Color")))
        if not oid or not color_match:
            continue
        feet = max(0.0, _number(row.get("Length (ft)")))
        color = color_match.group(1)
        out.setdefault(oid, {})[color] = out.setdefault(oid, {}).get(color, 0.0) + feet / 16500.0
    return out


def _thread_data_received_cones(values: Sequence[Any]) -> Dict[str, int]:
    """Mirror the existing thread-inventory endpoint's received-cone calculation."""
    rows = values if values and isinstance(values[0], dict) else _rows_to_dicts(values)
    result: Dict[str, int] = {}
    for row in rows:
        if _text(row.get("IN/OUT")).upper() != "IN":
            continue
        if _text(row.get("O/R")).upper() == "ORDERED":
            continue
        color_match = re.search(r"\b(\d{4})\b", _text(row.get("Color")))
        if not color_match:
            continue
        feet = max(0.0, _number(row.get("Length (ft)")))
        cones = int(round(feet / 16500.0)) if feet else 0
        if cones > 0:
            code = color_match.group(1)
            result[code] = result.get(code, 0) + cones
    return result


def _physical_cones_on_hand(remaining_eq: float, cones_received: int) -> int:
    """Mirror the app's six-head loadable-cone calculation."""
    remaining = max(0.0, _number(remaining_eq))
    if remaining <= 0:
        return 0
    received = max(0, int(_number(cones_received)))
    if received <= 0:
        return int(((remaining + 5.999999) // 6) * 6)
    used = max(0.0, received - remaining)
    emptied = int(used // 6) * 6
    return max(0, received - emptied)


def _parse_sewing_completion(values: Sequence[Sequence[Any]]) -> Dict[str, float]:
    rows = _rows_to_dicts(values)
    result = {}
    for row in rows:
        oid = normalize_order_number(row.get("Order #"))
        if not oid:
            continue
        # Existing app's finished-piece definition is Sewing Summary "Top".
        # Elastic/Fur/Flat/Round are intermediate steps and are never added.
        result[oid] = max(result.get(oid, 0.0), max(0.0, _number(row.get("Top"))))
    return result


def sewing_output_metrics(values: Sequence[Sequence[Any]], now: Optional[datetime] = None) -> dict:
    rows = _rows_to_dicts(values)
    today = (now or datetime.now(ET)).astimezone(ET).date()
    totals: Dict[date, float] = {}
    for row in rows:
        raw = row.get("Timestamp")
        parsed = None
        if isinstance(raw, (int, float)):
            try:
                parsed = date(1899, 12, 30) + timedelta(days=float(raw))
            except Exception:
                parsed = None
        if parsed is None:
            text = _text(raw)
            for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y"):
                try:
                    parsed = datetime.strptime(text, fmt).date()
                    break
                except ValueError:
                    continue
        if not parsed:
            continue
        totals[parsed] = totals.get(parsed, 0.0) + max(0.0, _number(row.get("Top")))

    def metric(days: int) -> dict:
        start = today - timedelta(days=days - 1)
        included = [v for d, v in totals.items() if start <= d <= today]
        workdays = sum(
            1 for offset in range(days)
            if (start + timedelta(days=offset)).weekday() < 5
        )
        total = sum(included)
        return {
            "days": days,
            "finishedPieces": round(total, 2),
            "averagePerWorkday": round(total / workdays, 2) if workdays else None,
            "loggedDays": len(included),
            "enoughData": len(included) >= min(3, workdays),
        }

    return {"7": metric(7), "14": metric(14), "30": metric(30)}


class ProductionScheduleService:
    def __init__(
        self,
        *,
        store: ScheduleSheetStore,
        fetch_sheet: Callable[..., list],
        orders_range: str,
        resolve_order_address: Callable[[dict, Optional[dict], Any], dict],
        fetch_directory_row: Callable[[str], Optional[dict]],
        normalize_directory_address: Callable[[dict], dict],
        ups_get_rate: Callable[..., list],
        frontend_url: str,
    ):
        self.store = store
        self.fetch_sheet = fetch_sheet
        self.orders_range = orders_range
        self.resolve_order_address = resolve_order_address
        self.fetch_directory_row = fetch_directory_row
        self.normalize_directory_address = normalize_directory_address
        self.ups_get_rate = ups_get_rate
        self.frontend_url = frontend_url.rstrip("/")
        self._transit_cache: Dict[str, int] = {}
        self._published_cache: Optional[dict] = None

    def remember_published(self, version: Optional[dict], schedule: Optional[dict]) -> None:
        if version and schedule:
            self._published_cache = {"version": version, "schedule": schedule}

    def cached_published(self) -> Optional[dict]:
        return self._published_cache

    def _settings(self, sewer_values: Optional[Sequence[Sequence[Any]]] = None) -> dict:
        raw = self.store.settings()
        regular = raw.get("regularSewingCapacity", 95)
        emergency = raw.get("emergencySewingCapacity", 50)
        return {
            "regularSewingCapacity": regular,
            "emergencySewingCapacity": emergency,
            "approvedEmergencyDates": raw.get("approvedEmergencyDates", []),
            "holidays": raw.get("holidays", []),
            "productFactors": raw.get("productFactors", {}),
            "frenchSeamFactor": raw.get("frenchSeamFactor"),
            "unusualShapeFactor": raw.get("unusualShapeFactor"),
            "sewingChangeoverMinutes": raw.get("sewingChangeoverMinutes", 5),
            "sewers": self.load_sewer_roster(regular, emergency, sewer_values),
            "sewerAbsences": raw.get("sewerAbsences") or {},
        }

    def load_sewer_roster(
        self,
        regular_total: Any = 95,
        emergency_total: Any = 50,
        values: Optional[Sequence[Sequence[Any]]] = None,
    ) -> List[dict]:
        grid = list(values or [])
        if not grid:
            title = ""
            try:
                title = self.store.find_sheet_title("Sewing")
            except Exception:
                logger.exception("Could not list spreadsheet tabs for sewer names")
            if not title:
                title = "Sewing"
            try:
                grid = (self.store.batch_values([f"'{title}'!A1:CZ40"]) or [[]])[0]
            except Exception:
                logger.exception("Could not read %s sheet for sewer names", title)
                return []
        return assign_sewer_capacities(parse_sewers(grid), _number(regular_total, 95), _number(emergency_total, 50))

    def _inventory(
        self,
        thread_values: Sequence[Any],
        inventory_values: Optional[Sequence[Sequence[Any]]] = None,
    ) -> Dict[str, dict]:
        rows = list(inventory_values or [])
        if len(rows) < 2:
            rows = self.fetch_sheet(self.store.spreadsheet_id, "Thread Inventory!A1:M") or []
        if len(rows) < 2:
            return {}
        received_by_code = _thread_data_received_cones(thread_values)
        headers = [_text(v) for v in rows[0]]
        lower = [h.lower() for h in headers]
        color_i = next((i for i, h in enumerate(lower) if h in {"thread colors", "thread color"}), None)
        inv_i = next((i for i, h in enumerate(lower) if h in {"inventory", "inventory.."}), None)
        order_i = next((i for i, h in enumerate(lower) if h in {"on order", "on order.."}), None)
        result = {}
        for row in rows[1:]:
            if color_i is None or color_i >= len(row):
                continue
            match = re.search(r"\b(\d{4})\b", _text(row[color_i]))
            if not match:
                continue
            code = match.group(1)
            remaining = _number(row[inv_i] if inv_i is not None and inv_i < len(row) else 0)
            # Same six-cone loading rule used by /api/thread-inventory-status.
            loadable = _physical_cones_on_hand(remaining, received_by_code.get(code, 0))
            result[code] = {
                "inventory": remaining,
                "onOrder": _number(row[order_i] if order_i is not None and order_i < len(row) else 0),
                "cones": loadable,
            }
        return result

    def _shipping_method_raw(self, row: dict) -> str:
        return _text(
            row.get("Shipping Type")
            or row.get("Shipping Method")
            or row.get("Shipping Service")
            or row.get("Ship Via")
            or row.get("UPS Service")
        )

    def _is_local_delivery(self, row: dict) -> bool:
        return is_local_delivery(self._shipping_method_raw(row))

    def _service_code(self, row: dict) -> str:
        raw = self._shipping_method_raw(row).upper()
        if not raw or self._is_local_delivery(row):
            return GROUND_CODE
        for label, code in SERVICE_CODES.items():
            if label in raw:
                return code
        return GROUND_CODE

    def _address(self, row: dict, by_id: dict, directory_by_customer: Optional[dict] = None) -> dict:
        specific = self.resolve_order_address(row, by_id, None) or {}
        if self._usable_ship_address(specific):
            return specific
        company = _text(row.get("Company Name"))
        directory = None
        if company and directory_by_customer is not None:
            directory = directory_by_customer.get(company.lower())
        elif company:
            directory = self.fetch_directory_row(company)
        if not directory:
            return {}
        address = self.normalize_directory_address(directory) or {}
        required = ("addr1", "city", "state", "zip")
        return address if all(_text(address.get(k)) for k in required) else {}

    def _usable_ship_address(self, addr: Any) -> bool:
        if not isinstance(addr, dict):
            return False
        zip5 = self._zip5(addr)
        state = _text(addr.get("state"))
        return bool(_text(addr.get("addr1")) or len(zip5) == 5 or len(state) == 2)

    def _zip5(self, address: Optional[dict]) -> str:
        digits = re.sub(r"\D", "", _text((address or {}).get("zip")))
        return digits[:5]

    def _transit_days(self, address: dict, service_code: str) -> Optional[int]:
        if not address.get("zip") and not address.get("addr1"):
            return None
        key = f"{self._zip5(address) or _text(address.get('zip'))}|{service_code}"
        if key in self._transit_cache:
            return self._transit_cache[key]
        ship_to = {**address, "service_code": service_code}
        try:
            rows = self.ups_get_rate(
                ship_to,
                [{"L": 1, "W": 1, "H": 1, "weight": 1}],
                ask_all_services=False,
            )
            match = next((r for r in rows if _text(r.get("code")).zfill(2) == service_code), None)
            raw_days = (match or {}).get("business_days")
            if raw_days in (None, ""):
                return None
            days = int(raw_days)
            if 1 <= days <= 6:
                self._transit_cache[key] = days
                return days
        except Exception:
            logger.exception("UPS transit lookup failed for %s", key)
        return None

    def _planning_transit(self, row: dict, address: dict, service_code: str) -> int:
        if self._is_local_delivery(row):
            return LOCAL_DELIVERY_TRANSIT_DAYS
        live = self._transit_days(address, service_code) if address else None
        if live is not None:
            return live
        return transit_days_for_service(service_code, address.get("zip"), address.get("state"))

    def _unify_destination_transit(self, planned: Sequence[dict]) -> None:
        """Same destination ZIP + same UPS service share one transit time."""
        by_dest: Dict[str, List[dict]] = {}
        for item in planned:
            row = item.get("row") or {}
            if self._is_local_delivery(row):
                continue
            zip5 = self._zip5(item.get("address"))
            service = _text(item.get("service_code")).zfill(2)
            if zip5:
                by_dest.setdefault(f"{zip5}|{service}", []).append(item)

        def apply_shared(group: Sequence[dict]) -> None:
            live_days = [int(item["live"]) for item in group if item.get("live") not in (None, "")]
            if live_days:
                chosen = max(live_days)
            else:
                values = [int(item["transit"]) for item in group if item.get("transit") not in (None, "")]
                if not values:
                    return
                chosen = max(values)
            for item in group:
                item["transit"] = chosen

        for group in by_dest.values():
            if len(group) > 1:
                apply_shared(group)

    def load_inputs(self) -> tuple[List[dict], Dict[str, dict], dict]:
        batched = self.store.batch_values([
            self.orders_range,
            "Directory!A1:ZZ10000",
            "Thread Data!A1:Z",
            "Sewing Summary!A1:Z",
            "Cut List!A1:Z",
            "Fur List!A1:Z",
            "Thread Inventory!A1:M",
        ])
        (
            order_values,
            directory_values,
            thread_values,
            sewing_values,
            cut_values,
            fur_values,
            inventory_values,
        ) = (batched + [[] for _ in range(7)])[:7]
        orders = [r for r in _rows_to_dicts(order_values) if _active(r)]
        by_id = {normalize_order_number(r.get("Order #")): r for r in orders}
        directory_rows = _rows_to_dicts(directory_values)
        directory_by_customer = {
            _text(r.get("Company Name")).lower(): r
            for r in directory_rows
            if _text(r.get("Company Name"))
        }
        thread_rows = _rows_to_dicts(thread_values)
        usage = _parse_thread_usage(thread_rows)
        sewing_done = _parse_sewing_completion(sewing_values)
        cut_rows = {
            normalize_order_number(r.get("Order #")): r
            for r in _rows_to_dicts(cut_values)
        }
        fur_rows = {
            normalize_order_number(r.get("Order #")): r
            for r in _rows_to_dicts(fur_values)
        }
        published = self.store.published_version()
        explicit_groups: Dict[str, str] = {}
        if published:
            try:
                for row in self.store.load_schedule(_text(published.get("Version ID"))).get("sewing", []):
                    # Only honor user/sheet Shipping Group IDs. Inferred
                    # consecutive-order groups must be recomputed so later
                    # deliveries of the same design stay on their own due date.
                    if row.get("shippingGroupSource") == "explicit":
                        explicit_groups[_text(row.get("orderNumber"))] = _text(row.get("shippingGroupId"))
            except Exception:
                logger.exception("Could not load approved shipping groups")

        cfg = SchedulerConfig.from_dict(self._settings())
        planned: List[dict] = []
        for row in orders:
            address = self._address(row, by_id, directory_by_customer)
            service_code = self._service_code(row)
            live = None
            if not self._is_local_delivery(row) and address:
                live = self._transit_days(address, service_code)
            transit = self._planning_transit(row, address, service_code)
            planned.append({
                "row": row,
                "address": address,
                "service_code": service_code,
                "transit": transit,
                "live": live,
            })
        self._unify_destination_transit(planned)
        for item in planned:
            row = item["row"]
            oid = normalize_order_number(row.get("Order #"))
            address = item["address"]
            service_code = item["service_code"]
            transit = item["transit"]
            due = parse_date(row.get("Due Date"))
            # Sheet Ship Date is WORKDAY(due, -5) — a blanket week, not real transit.
            ship_date = resolve_required_ship_date(
                due,
                transit,
                cfg.holidays,
                shipping_method=self._shipping_method_raw(row),
            )
            warnings = []
            cut = cut_rows.get(oid) or {}
            cut_status = _text(cut.get("Status") or row.get("Cut Status")).upper()
            if cut_status and cut_status not in {"COMPLETE", "COMPLETED", "DONE", "READY"}:
                warnings.append(f"Cut components: {cut_status}")
            if _text(row.get("Fur Color")):
                fur_status = _text((fur_rows.get(oid) or {}).get("Status") or row.get("Fur Status")).upper()
                if fur_status and fur_status not in {"COMPLETE", "COMPLETED", "DONE", "READY"}:
                    warnings.append(f"Fur: {fur_status}")
            row["_shipping_address"] = address
            row["_shipping_method"] = (
                "Local Delivery"
                if self._is_local_delivery(row)
                else next(
                    (label.title() for label, code in SERVICE_CODES.items() if code == service_code),
                    "UPS Ground",
                )
            )
            row["_required_ship_date"] = ship_date.isoformat() if ship_date else ""
            row["_transit_business_days"] = transit if transit is not None else ""
            row["_thread_usage_cones"] = usage.get(oid, {})
            row["_sewing_completed_qty"] = sewing_done.get(oid, 0)
            row["_material_warnings"] = warnings
            row["_shipping_group_id"] = explicit_groups.get(oid, "")
        # The production workbook records finished pieces in Sewing Summary.Top.
        # It has no dated Sewing Log, so recent averages remain "Not enough data"
        # until a timestamped finished-output source is introduced.
        return orders, self._inventory(thread_rows, inventory_values), sewing_output_metrics(sewing_values)

    @staticmethod
    def _version_id(fingerprint: str) -> str:
        return f"SCH-{fingerprint[:16].upper()}"

    @staticmethod
    def _baseline_ids(version: Optional[dict]) -> set[str]:
        if not version:
            return set()
        try:
            return set(json.loads(_text(version.get("Baseline Order IDs JSON")) or "[]"))
        except Exception:
            return set()

    def _comparison(self, published: Optional[dict], schedule: dict) -> dict:
        current_ids = {o["order_number"] for o in schedule.get("orders") or []}
        baseline = self._baseline_ids(published)
        new_orders = sorted(current_ids - baseline)
        if not published:
            return {"newOrders": sorted(current_ids), "jobsMoved": [], "machineChanges": []}
        try:
            old = self.store.load_schedule(_text(published.get("Version ID")))
        except Exception:
            return {"newOrders": new_orders, "jobsMoved": [], "machineChanges": []}
        old_sew = {}
        for row in old.get("sewing") or []:
            old_sew.setdefault(_text(row.get("orderNumber")), set()).add(_text(row.get("date")))
        new_sew = {}
        for row in schedule.get("sewing") or []:
            new_sew.setdefault(_text(row.get("orderNumber")), set()).add(_text(row.get("date")))
        moved = []
        for oid in sorted(set(old_sew) | set(new_sew)):
            before, after = sorted(old_sew.get(oid, set())), sorted(new_sew.get(oid, set()))
            if before != after:
                moved.append({"orderNumber": oid, "previousDates": before, "proposedDates": after})
        old_m = {_text(r.get("orderNumber")): _text(r.get("machine")) for r in old.get("embroidery") or []}
        new_m = {_text(r.get("orderNumber")): _text(r.get("machine")) for r in schedule.get("embroidery") or []}
        machine_changes = [
            {"orderNumber": oid, "previousMachine": old_m.get(oid, ""), "proposedMachine": new_m.get(oid, "")}
            for oid in sorted(set(old_m) | set(new_m))
            if old_m.get(oid) != new_m.get(oid)
        ]
        return {"newOrders": new_orders, "jobsMoved": moved, "machineChanges": machine_changes}

    def rebuild(self, reason: str = "manual", *, notify: bool = True) -> dict:
        published = self.store.published_version()
        try:
            orders, inventory, metrics = self.load_inputs()
            schedule = build_schedule(
                orders,
                config=self._settings(),
                thread_inventory=inventory,
                locks=self.store.locks(),
                run_reason=reason,
            )
            version_id = self._version_id(schedule["inputFingerprint"])
            existing = self.store.get_version(version_id)
            if existing:
                loaded = self.store.load_schedule(version_id)
                if _text(existing.get("Status")) == "Published":
                    self.remember_published(existing, loaded)
                return {
                    "ok": True, "deduplicated": True, "version": existing,
                    "schedule": loaded, "comparison": self._comparison(published, schedule),
                    "sewingMetrics": metrics,
                }

            order_ids = [o["order_number"] for o in schedule.get("orders") or []]
            initial_baseline = published is None and not self.store.versions()
            status = "Published" if initial_baseline else "Awaiting Approval"
            version = self.store.write_version(version_id, status, schedule, order_ids)
            if status == "Published":
                self.remember_published(version, {**schedule, "version": version})
            comparison = self._comparison(published, schedule)
            result = {
                "ok": True,
                "initialBaseline": initial_baseline,
                "version": version,
                "schedule": {**schedule, "version": version},
                "comparison": comparison,
                "sewingMetrics": metrics,
            }
            if notify and not initial_baseline:
                try:
                    self.send_approval_email(version_id, result)
                except Exception:
                    logger.exception("Schedule approval email failed; proposal was still saved")
            return result
        except Exception as exc:
            logger.exception("Production schedule rebuild failed; published schedule preserved")
            return {
                "ok": False,
                "error": f"Schedule rebuild failed: {friendly_sheets_error(exc)}",
                "publishedVersion": published,
            }

    def send_approval_email(self, version_id: str, result: dict) -> bool:
        version = self.store.get_version(version_id) or {}
        if _text(version.get("Notification Sent At")):
            return False
        smtp_host = _text(os.environ.get("SMTP_HOST"))
        smtp_user = _text(os.environ.get("SMTP_USER"))
        smtp_password = _text(os.environ.get("SMTP_PASSWORD"))
        to_email = _text(
            os.environ.get("SCHEDULE_ADMIN_EMAIL")
            or os.environ.get("EMBROIDERY_MANAGER_EMAIL")
            or os.environ.get("KANBAN_SCAN_NOTIFY_EMAIL")
        )
        if not all((smtp_host, smtp_user, smtp_password, to_email)):
            logger.info("Schedule approval email skipped: SMTP or SCHEDULE_ADMIN_EMAIL not configured")
            return False
        comparison = result.get("comparison") or {}
        schedule = result.get("schedule") or {}
        summary = schedule.get("summary") or {}
        approval_url = f"{self.frontend_url}/schedule-approvals?version={version_id}"
        subject = (
            f"Schedule approval required — {len(comparison.get('newOrders') or [])} new, "
            f"{summary.get('blockingConflictCount', 0)} conflicts"
        )
        lines = [
            f"Version: {version_id}",
            f"New orders: {', '.join(comparison.get('newOrders') or []) or 'None'}",
            f"Jobs moved: {len(comparison.get('jobsMoved') or [])}",
            f"Machine changes: {len(comparison.get('machineChanges') or [])}",
            f"Blocking conflicts: {summary.get('blockingConflictCount', 0)}",
            f"Warnings: {summary.get('warningCount', 0)}",
            f"Third-sewer dates: {', '.join(summary.get('thirdSewerDates') or []) or 'None'}",
            "",
            f"Review and approve: {approval_url}",
        ]
        plain = "\n".join(lines)
        body = "<br>".join(html.escape(line) for line in lines)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = _text(os.environ.get("DESIGN_CONFIRMATION_FROM_EMAIL") or smtp_user)
        msg["To"] = to_email
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(f"<html><body style='font-family:Arial'>{body}</body></html>", "html"))
        port = int(os.environ.get("SMTP_PORT") or "587")
        with smtplib.SMTP(smtp_host, port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(msg["From"], [to_email], msg.as_string())
        self.store.mark_notified(version_id, datetime.now(ET).isoformat())
        return True

    def approve(self, version_id: str, actor: str, notes: str = "") -> dict:
        proposal = self.store.get_version(version_id)
        if not proposal:
            raise KeyError(version_id)
        if _text(proposal.get("Status")) != "Awaiting Approval":
            raise ValueError("Only an Awaiting Approval version can be published")
        previous = self.store.published_version()
        previous_id = _text((previous or {}).get("Version ID"))
        if previous_id and previous_id != version_id:
            self.store.update_version_status(previous_id, "Superseded", superseded_by=version_id)
        self.store.update_version_status(version_id, "Published")
        approval_id = hashlib.sha256(f"{version_id}|approve".encode()).hexdigest()[:20]
        self.store.append_approval(
            approval_id, version_id, "Approved", actor, notes, previous_id
        )
        return self.store.load_schedule(version_id)

    def reject(self, version_id: str, actor: str, notes: str = "") -> dict:
        proposal = self.store.get_version(version_id)
        if not proposal:
            raise KeyError(version_id)
        if _text(proposal.get("Status")) != "Awaiting Approval":
            raise ValueError("Only an Awaiting Approval version can be rejected")
        self.store.update_version_status(version_id, "Rejected")
        approval_id = hashlib.sha256(f"{version_id}|reject".encode()).hexdigest()[:20]
        self.store.append_approval(
            approval_id, version_id, "Rejected", actor, notes,
            _text((self.store.published_version() or {}).get("Version ID")),
        )
        return self.store.load_schedule(version_id)

    def explain(self, schedule: dict, question: str) -> dict:
        context = {
            "summary": schedule.get("summary") or {},
            "conflicts": schedule.get("conflicts") or [],
            "warnings": schedule.get("warnings") or [],
        }
        key = _text(os.environ.get("OPENAI_API_KEY"))
        if not key:
            return {
                "enabled": False,
                "answer": (
                    f"The deterministic schedule has {context['summary'].get('blockingConflictCount', 0)} "
                    f"blocking conflicts and {context['summary'].get('warningCount', 0)} warnings. "
                    "OpenAI is not configured; all schedule calculations remain available."
                ),
            }
        payload = {
            "model": _text(os.environ.get("OPENAI_SCHEDULE_MODEL") or "gpt-5-mini"),
            "instructions": (
                "Explain only the supplied deterministic schedule. Never invent dates, quantities, "
                "inventory, or actions. Do not claim to publish, purchase, unlock, or contact customers."
            ),
            "input": json.dumps({"question": question, "schedule": context}, default=str),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "schedule_explanation",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                }
            },
        }
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        text = data.get("output_text")
        if not text:
            for item in data.get("output") or []:
                for content in item.get("content") or []:
                    if content.get("type") == "output_text":
                        text = content.get("text")
                        break
        parsed = json.loads(text or "{}")
        answer = _text(parsed.get("answer"))
        if not answer:
            raise ValueError("OpenAI returned an invalid explanation")
        return {"enabled": True, "answer": answer}
