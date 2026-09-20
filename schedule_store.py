"""Google Sheets persistence for versioned production schedules."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

TAB_HEADERS = {
    "Schedule Versions": [
        "Version ID", "Status", "Created At", "Run Reason", "Input Fingerprint",
        "Baseline Order IDs JSON", "Summary JSON", "Conflict Count", "Warning Count",
        "Notification Sent At", "Superseded By", "Failure Message",
    ],
    "Sewing Schedule": [
        "Version ID", "Record ID", "Order #", "Shipping Group ID", "Date", "Start",
        "Finish", "Capacity Units", "Locked", "Payload JSON",
    ],
    "Embroidery Schedule": [
        "Version ID", "Record ID", "Machine", "Order #", "Start", "Finish",
        "Duration Hours", "Payload JSON",
    ],
    "Schedule Locks": [
        "Lock ID", "Order #", "Date", "Capacity Units", "Created By", "Created At",
        "Active", "Payload JSON",
    ],
    "Schedule Approvals": [
        "Approval ID", "Version ID", "Decision", "Actor", "Decided At", "Notes",
        "Previous Published Version", "Payload JSON",
    ],
    "Scheduling Settings": ["Key", "Value JSON", "Updated At", "Updated By"],
    "Schedule Orders": ["Version ID", "Order #", "Payload JSON"],
    "Schedule Issues": [
        "Version ID", "Record ID", "Severity", "Type", "Order #", "Payload JSON",
    ],
}

VERSION_STATUSES = {"Draft", "Awaiting Approval", "Published", "Rejected", "Superseded"}
SHEET_CELL_LIMIT = 45000
ORDER_PAYLOAD_KEYS = (
    "order_number", "customer", "product", "design", "quantity",
    "remaining_quantity", "embroidery_remaining", "stitch_count",
    "due_date", "in_hand_date", "required_ship_date",
    "shipping_group_id", "stage", "image", "needs_sewing",
)


def _rows_to_dicts(values: List[List[Any]]) -> List[dict]:
    if not values:
        return []
    headers = [str(v or "").strip() for v in values[0]]
    out = []
    for raw in values[1:]:
        row = list(raw or []) + [""] * max(0, len(headers) - len(raw or []))
        out.append(dict(zip(headers, row)))
    return out


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def compact_order(row: dict) -> dict:
    out = {}
    for key in ORDER_PAYLOAD_KEYS:
        out[key] = _jsonable(row.get(key))
    return out


def version_summary_metadata(schedule: dict) -> dict:
    """Keep the Versions tab cell well under Google Sheets' 50k character limit."""
    return {
        "summary": schedule.get("summary") or {},
        "settings": schedule.get("settings") or {},
        "runReason": schedule.get("runReason") or "",
        "timezone": schedule.get("timezone") or "America/New_York",
        "shippingGroups": [
            {
                "id": row.get("id"),
                "source": row.get("source"),
                "requiredShipDate": _jsonable(row.get("requiredShipDate")),
                "orderNumbers": row.get("orderNumbers") or [],
                "customer": row.get("customer") or "",
            }
            for row in (schedule.get("shippingGroups") or [])
        ],
    }


def compact_image(value: Any) -> str:
    text = str(value or "").strip()
    return text[:500]


def safe_job_payload(row: dict) -> dict:
    payload = _jsonable(row)
    if isinstance(payload, dict):
        image = payload.get("image") or payload.get("Image") or payload.get("Preview")
        payload["image"] = compact_image(image)
        payload.pop("Image", None)
        payload.pop("Preview", None)
    return payload if isinstance(payload, dict) else {}


def is_sheets_rate_limit(exc: Exception) -> bool:
    text = str(exc)
    return "429" in text or "RATE_LIMIT" in text or "Quota exceeded" in text


def is_transient_sheets_error(exc: Exception) -> bool:
    text = str(exc)
    if isinstance(exc, AttributeError) and "close" in text:
        return True
    return is_sheets_rate_limit(exc) or any(
        token in text
        for token in (
            "BadStatusLine",
            "00000001",
            "reentrant call",
            "has no attribute 'close'",
            "attribute 'close'",
            "NoneType",
            "Connection reset",
            "Connection aborted",
            "RemoteDisconnected",
            "timed out",
            "Timeout",
            "SSLError",
            "Broken pipe",
            "503",
            "502",
            "500",
        )
    )


def friendly_sheets_error(exc: Exception) -> str:
    if is_sheets_rate_limit(exc) or is_transient_sheets_error(exc):
        return (
            "Google Sheets is temporarily busy. Wait about a minute and refresh. "
            "The published schedule was not changed."
        )
    return str(exc).split("\n", 1)[0][:300]


class ScheduleSheetStore:
    def __init__(self, sheets_service, spreadsheet_id: str):
        self.service = sheets_service
        self.spreadsheet_id = spreadsheet_id
        self._cache: Dict[str, tuple[float, List[List[Any]]]] = {}
        self._cache_ttl = 20.0

    @property
    def values(self):
        return self.service.spreadsheets().values()

    def _execute(self, request):
        last_error = None
        for attempt in range(5):
            try:
                return request.execute()
            except Exception as exc:
                last_error = exc
                if not is_transient_sheets_error(exc) or attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        raise last_error

    def invalidate(self, *titles: str) -> None:
        if not titles:
            self._cache.clear()
            return
        prefixes = {f"'{title}'!" for title in titles} | {f"{title}!" for title in titles}
        for key in list(self._cache):
            if any(key.startswith(prefix) or key == title for prefix in prefixes for title in titles):
                self._cache.pop(key, None)

    def batch_values(self, ranges: Sequence[str]) -> List[List[List[Any]]]:
        if not ranges:
            return []
        response = self._execute(
            self.values.batchGet(
                spreadsheetId=self.spreadsheet_id,
                ranges=list(ranges),
                valueRenderOption="UNFORMATTED_VALUE",
            )
        )
        value_ranges = response.get("valueRanges") or []
        out = []
        for index, range_name in enumerate(ranges):
            values = (value_ranges[index].get("values") if index < len(value_ranges) else None) or []
            self._cache[str(range_name)] = (time.time(), values)
            out.append(values)
        return out

    def _tab_range(self, title: str) -> str:
        return f"'{title}'!A1:ZZ"

    def sheet_titles(self) -> List[str]:
        meta = self._execute(
            self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                fields="sheets.properties.title",
            )
        )
        return [
            str((item.get("properties") or {}).get("title") or "").strip()
            for item in (meta.get("sheets") or [])
            if str((item.get("properties") or {}).get("title") or "").strip()
        ]

    def find_sheet_title(self, *needles: str) -> str:
        titles = self.sheet_titles()
        wanted = [str(n or "").strip().casefold() for n in needles if str(n or "").strip()]
        for needle in wanted:
            for title in titles:
                if title.casefold() == needle:
                    return title
        skip = ("summary", "schedule", "waiting", "log", "priority")
        for needle in wanted:
            for title in titles:
                low = title.casefold()
                if needle in low and not any(part in low for part in skip):
                    return title
        return ""

    def _cached_values(self, range_name: str) -> Optional[List[List[Any]]]:
        hit = self._cache.get(range_name)
        if not hit:
            return None
        stamp, values = hit
        if time.time() - stamp > self._cache_ttl:
            self._cache.pop(range_name, None)
            return None
        return values

    def ensure_schema(self) -> None:
        meta = self._execute(
            self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                fields="sheets.properties.title",
            )
        )
        existing = {
            str((item.get("properties") or {}).get("title") or "")
            for item in (meta.get("sheets") or [])
        }
        requests = [
            {"addSheet": {"properties": {"title": title}}}
            for title in TAB_HEADERS
            if title not in existing
        ]
        if requests:
            self._execute(
                self.service.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"requests": requests},
                )
            )
        header_ranges = [f"'{title}'!1:1" for title in TAB_HEADERS]
        current_headers = self.batch_values(header_ranges)
        updates = []
        for (title, headers), current in zip(TAB_HEADERS.items(), current_headers):
            first = current[0] if current else []
            merged = list(first)
            if not merged:
                merged = list(headers)
            else:
                for index, header in enumerate(headers):
                    if index >= len(merged):
                        merged.append(header)
                    elif not str(merged[index] or "").strip():
                        merged[index] = header
            if merged != first:
                updates.append({"range": f"'{title}'!A1", "values": [merged]})
        if updates:
            self._execute(
                self.values.batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"valueInputOption": "RAW", "data": updates},
                )
            )

    def read_tab(self, title: str) -> List[dict]:
        cached = self._cached_values(self._tab_range(title))
        if cached is not None:
            return _rows_to_dicts(cached)
        try:
            values = (self.batch_values([self._tab_range(title)]) or [[]])[0]
        except Exception:
            if title not in TAB_HEADERS:
                raise
            self.ensure_schema()
            values = (self.batch_values([self._tab_range(title)]) or [[]])[0]
        return _rows_to_dicts(values)

    def _append(self, title: str, rows: List[List[Any]]) -> None:
        if not rows:
            return
        self._execute(
            self.values.append(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{title}'!A:ZZ",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            )
        )
        self.invalidate(title)

    def versions(self) -> List[dict]:
        return self.read_tab("Schedule Versions")

    def get_version(self, version_id: str) -> Optional[dict]:
        return next(
            (row for row in self.versions() if str(row.get("Version ID") or "") == version_id),
            None,
        )

    def latest_by_status(self, *statuses: str) -> Optional[dict]:
        wanted = set(statuses)
        rows = [r for r in self.versions() if str(r.get("Status") or "") in wanted]
        return rows[-1] if rows else None

    def active_proposal(self) -> Optional[dict]:
        return self.latest_by_status("Awaiting Approval", "Draft")

    def published_version(self) -> Optional[dict]:
        return self.latest_by_status("Published")

    def write_version(
        self,
        version_id: str,
        status: str,
        schedule: dict,
        baseline_order_ids: Iterable[str],
        failure_message: str = "",
    ) -> dict:
        if status not in VERSION_STATUSES:
            raise ValueError(f"Unknown schedule status: {status}")
        self.ensure_schema()
        existing = self.get_version(version_id)
        if not existing:
            summary = schedule.get("summary") or {}
            metadata = version_summary_metadata(schedule)
            encoded_meta = _json(metadata)
            if len(encoded_meta) > SHEET_CELL_LIMIT:
                metadata.pop("shippingGroups", None)
                encoded_meta = _json(metadata)
            baseline_json = _json(sorted(set(str(v) for v in baseline_order_ids)))
            if len(baseline_json) > SHEET_CELL_LIMIT:
                baseline_json = _json(sorted(set(str(v) for v in baseline_order_ids))[:400])
            self._append("Schedule Versions", [[
                version_id,
                status,
                schedule.get("createdAt") or datetime.utcnow().isoformat(),
                schedule.get("runReason") or "",
                schedule.get("inputFingerprint") or "",
                baseline_json,
                encoded_meta,
                int(summary.get("blockingConflictCount") or 0),
                int(summary.get("warningCount") or 0),
                "",
                "",
                failure_message,
            ]])

        self.batch_values([
            self._tab_range(title)
            for title in ("Sewing Schedule", "Embroidery Schedule", "Schedule Orders", "Schedule Issues")
        ])
        existing_sewing = {
            str(r.get("Record ID") or "")
            for r in self.read_tab("Sewing Schedule")
            if str(r.get("Version ID") or "") == version_id
        }
        sewing_rows = []
        for index, row in enumerate(schedule.get("sewing") or []):
            rid = f"{version_id}-S-{index:05d}"
            if rid in existing_sewing:
                continue
            sewing_rows.append([
                version_id, rid, row.get("orderNumber", ""), row.get("shippingGroupId", ""),
                row.get("date", ""), row.get("start", ""), row.get("finish", ""),
                row.get("capacityUnits", 0), bool(row.get("locked")), _json(safe_job_payload(row)),
            ])
        self._append("Sewing Schedule", sewing_rows)

        existing_emb = {
            str(r.get("Record ID") or "")
            for r in self.read_tab("Embroidery Schedule")
            if str(r.get("Version ID") or "") == version_id
        }
        emb_rows = []
        for index, row in enumerate(schedule.get("embroidery") or []):
            rid = f"{version_id}-E-{index:05d}"
            if rid in existing_emb:
                continue
            emb_rows.append([
                version_id, rid, row.get("machine", ""), row.get("orderNumber", ""),
                row.get("start", ""), row.get("finish", ""), row.get("durationHours", 0),
                _json(safe_job_payload(row)),
            ])
        self._append("Embroidery Schedule", emb_rows)

        existing_orders = {
            str(r.get("Order #") or "")
            for r in self.read_tab("Schedule Orders")
            if str(r.get("Version ID") or "") == version_id
        }
        order_rows = []
        for row in schedule.get("orders") or []:
            oid = str(row.get("order_number") or "")
            if not oid or oid in existing_orders:
                continue
            order_rows.append([version_id, oid, _json(compact_order(row))])
        self._append("Schedule Orders", order_rows)

        existing_issues = {
            str(r.get("Record ID") or "")
            for r in self.read_tab("Schedule Issues")
            if str(r.get("Version ID") or "") == version_id
        }
        issue_rows = []
        for index, row in enumerate((schedule.get("conflicts") or []) + (schedule.get("warnings") or [])):
            rid = f"{version_id}-I-{index:05d}"
            if rid in existing_issues:
                continue
            issue_rows.append([
                version_id, rid, row.get("severity", ""), row.get("type", ""),
                row.get("orderNumber") or row.get("groupId") or "",
                _json(_jsonable(row)),
            ])
        self._append("Schedule Issues", issue_rows)
        return self.get_version(version_id) or {}

    def _update_version_fields(self, version_id: str, fields: Dict[str, Any]) -> None:
        values = self.values.get(
            spreadsheetId=self.spreadsheet_id,
            range="'Schedule Versions'!A1:Z",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute().get("values") or []
        if not values:
            raise KeyError(version_id)
        headers = [str(v or "").strip() for v in values[0]]
        row_number = None
        for index, row in enumerate(values[1:], start=2):
            if str(row[0] if row else "") == version_id:
                row_number = index
                break
        if not row_number:
            raise KeyError(version_id)
        data = []
        for name, value in fields.items():
            if name not in headers:
                continue
            column = self._column_letter(headers.index(name))
            data.append({"range": f"'Schedule Versions'!{column}{row_number}", "values": [[value]]})
        if data:
            self.values.batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ).execute()

    @staticmethod
    def _column_letter(index: int) -> str:
        number = index + 1
        out = ""
        while number:
            number, rem = divmod(number - 1, 26)
            out = chr(65 + rem) + out
        return out

    def update_version_status(
        self, version_id: str, status: str, *, superseded_by: str = ""
    ) -> None:
        if status not in VERSION_STATUSES:
            raise ValueError(status)
        fields = {"Status": status}
        if superseded_by:
            fields["Superseded By"] = superseded_by
        self._update_version_fields(version_id, fields)

    def mark_notified(self, version_id: str, sent_at: str) -> None:
        self._update_version_fields(version_id, {"Notification Sent At": sent_at})

    def load_schedule(self, version_id: str) -> dict:
        version = self.get_version(version_id)
        if not version:
            raise KeyError(version_id)
        self.batch_values([
            self._tab_range(title)
            for title in ("Sewing Schedule", "Embroidery Schedule", "Schedule Orders", "Schedule Issues")
            if self._cached_values(self._tab_range(title)) is None
        ])

        def payloads(tab: str):
            result = []
            for row in self.read_tab(tab):
                if str(row.get("Version ID") or "") != version_id:
                    continue
                try:
                    result.append(json.loads(str(row.get("Payload JSON") or "{}")))
                except (TypeError, ValueError, json.JSONDecodeError):
                    logger.warning("Invalid %s payload for version %s", tab, version_id)
            return result

        try:
            metadata = json.loads(str(version.get("Summary JSON") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        summary = metadata.get("summary", metadata) if isinstance(metadata, dict) else {}
        orders = payloads("Schedule Orders")
        if not orders and isinstance(metadata, dict):
            orders = metadata.get("orders") or []
        issues = payloads("Schedule Issues")
        conflicts = [row for row in issues if str(row.get("severity") or "").lower() == "blocking"]
        warnings = [row for row in issues if str(row.get("severity") or "").lower() != "blocking"]
        if not issues and isinstance(metadata, dict):
            conflicts = metadata.get("conflicts") or []
            warnings = metadata.get("warnings") or []
        return {
            "version": version,
            "summary": summary,
            "conflicts": conflicts,
            "warnings": warnings,
            "shippingGroups": metadata.get("shippingGroups", []) if isinstance(metadata, dict) else [],
            "orders": orders,
            "settings": metadata.get("settings", {}) if isinstance(metadata, dict) else {},
            "runReason": metadata.get("runReason", "") if isinstance(metadata, dict) else "",
            "timezone": metadata.get("timezone", "America/New_York") if isinstance(metadata, dict) else "America/New_York",
            "sewing": payloads("Sewing Schedule"),
            "embroidery": payloads("Embroidery Schedule"),
        }

    def append_approval(
        self,
        approval_id: str,
        version_id: str,
        decision: str,
        actor: str,
        notes: str,
        previous_published: str,
        payload: Optional[dict] = None,
    ) -> None:
        if any(str(r.get("Approval ID") or "") == approval_id for r in self.read_tab("Schedule Approvals")):
            return
        self._append("Schedule Approvals", [[
            approval_id, version_id, decision, actor, datetime.utcnow().isoformat(),
            notes, previous_published, _json(payload or {}),
        ]])

    def locks(self) -> List[dict]:
        out = []
        for row in self.read_tab("Schedule Locks"):
            if str(row.get("Active") or "").strip().lower() not in {"true", "1", "yes", "y"}:
                continue
            try:
                payload = json.loads(str(row.get("Payload JSON") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            out.append({
                **payload,
                "lockId": str(row.get("Lock ID") or ""),
                "orderNumber": str(row.get("Order #") or ""),
                "date": str(row.get("Date") or ""),
                "capacityUnits": row.get("Capacity Units") or 0,
            })
        return out

    def upsert_lock(self, lock: dict, actor: str) -> None:
        lock_id = str(lock.get("lockId") or "").strip()
        if not lock_id:
            raise ValueError("lockId is required")
        values = self.values.get(
            spreadsheetId=self.spreadsheet_id,
            range="'Schedule Locks'!A1:H",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute().get("values") or []
        row = [
            lock_id, str(lock.get("orderNumber") or ""), str(lock.get("date") or ""),
            float(lock.get("capacityUnits") or 0), actor, datetime.utcnow().isoformat(),
            bool(lock.get("active", True)), _json(lock),
        ]
        row_number = None
        for index, old in enumerate(values[1:], start=2):
            if str(old[0] if old else "") == lock_id:
                row_number = index
                break
        if row_number:
            self.values.update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'Schedule Locks'!A{row_number}:H{row_number}",
                valueInputOption="RAW",
                body={"values": [row]},
            ).execute()
        else:
            self._append("Schedule Locks", [row])

    def settings(self) -> dict:
        out = {}
        for row in self.read_tab("Scheduling Settings"):
            key = str(row.get("Key") or "").strip()
            if not key:
                continue
            try:
                out[key] = json.loads(str(row.get("Value JSON") or "null"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return out

    def save_settings(self, settings: dict, actor: str) -> None:
        current = self.read_tab("Scheduling Settings")
        row_by_key = {
            str(row.get("Key") or ""): index
            for index, row in enumerate(current, start=2)
            if str(row.get("Key") or "")
        }
        updates = []
        appends = []
        for key, value in settings.items():
            row = [key, _json(value), datetime.utcnow().isoformat(), actor]
            if key in row_by_key:
                updates.append({
                    "range": f"'Scheduling Settings'!A{row_by_key[key]}:D{row_by_key[key]}",
                    "values": [row],
                })
            else:
                appends.append(row)
        if updates:
            self.values.batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
        self._append("Scheduling Settings", appends)
