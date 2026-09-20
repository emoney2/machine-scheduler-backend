import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from production_schedule_service import (
    ProductionScheduleService,
    _physical_cones_on_hand,
    _thread_data_received_cones,
    sewing_output_metrics,
)

ET = ZoneInfo("America/New_York")


def row(number):
    return {
        "Order #": str(number),
        "Date": "09/01/2026",
        "Company Name": "Customer",
        "Design": "Design",
        "Quantity": 6,
        "Product": "Driver",
        "Stage": "Embroidery",
        "Due Date": "09/30/2026",
        "Ship Date": "09/28/2026",
        "Stitch Count": 30000,
        "Threads": "1800",
        "Hard Date/Soft Date": "Hard Date",
        "_shipping_address": {"addr1": "1 Main", "city": "Buford", "state": "GA", "zip": "30519"},
        "_required_ship_date": "2026-09-28",
        "_thread_usage_cones": {"1800": 1},
    }


class FakeStore:
    spreadsheet_id = "test"

    def __init__(self):
        self.rows = []
        self.schedules = {}
        self.approvals = []

    def settings(self):
        return {}

    def locks(self):
        return []

    def versions(self):
        return list(self.rows)

    def published_version(self):
        rows = [r for r in self.rows if r["Status"] == "Published"]
        return rows[-1] if rows else None

    def active_proposal(self):
        rows = [r for r in self.rows if r["Status"] == "Awaiting Approval"]
        return rows[-1] if rows else None

    def get_version(self, version_id):
        return next((r for r in self.rows if r["Version ID"] == version_id), None)

    def write_version(self, version_id, status, schedule, baseline_order_ids, failure_message=""):
        record = {
            "Version ID": version_id,
            "Status": status,
            "Baseline Order IDs JSON": __import__("json").dumps(list(baseline_order_ids)),
            "Notification Sent At": "",
        }
        self.rows.append(record)
        self.schedules[version_id] = schedule
        return record

    def load_schedule(self, version_id):
        schedule = self.schedules[version_id]
        return {
            "version": self.get_version(version_id),
            "summary": schedule.get("summary", {}),
            "conflicts": schedule.get("conflicts", []),
            "warnings": schedule.get("warnings", []),
            "sewing": schedule.get("sewing", []),
            "embroidery": schedule.get("embroidery", []),
        }

    def update_version_status(self, version_id, status, superseded_by=""):
        row = self.get_version(version_id)
        row["Status"] = status
        row["Superseded By"] = superseded_by

    def append_approval(self, *args, **kwargs):
        self.approvals.append((args, kwargs))


class FakeService(ProductionScheduleService):
    def __init__(self, store):
        self.store = store
        self.frontend_url = "https://example.test"
        self.current_rows = [row(100)]
        self.fail = False
        self.emails = []

    def _settings(self):
        return {}

    def load_inputs(self):
        if self.fail:
            raise RuntimeError("sheet unavailable")
        return self.current_rows, {"1800": {"cones": 18}}, {}

    def send_approval_email(self, version_id, result):
        self.emails.append(version_id)
        return True


class WorkflowTests(unittest.TestCase):
    def test_local_delivery_planning_transit_is_one_day(self):
        service = FakeService(FakeStore())
        self.assertEqual(
            service._planning_transit(
                {"Shipping Method": "Local Delivery"},
                {"zip": "90210", "state": "CA"},
                "03",
            ),
            1,
        )

    def test_same_destination_shares_live_ups_transit(self):
        service = FakeService(FakeStore())
        planned = [
            {
                "row": {"Company Name": "Ocean Reef Club", "Shipping Method": "UPS"},
                "address": {"zip": "33050-1234", "state": "FL"},
                "service_code": "03",
                "transit": 2,
                "live": None,
            },
            {
                "row": {"Company Name": "Ocean Reef Club", "Shipping Method": "UPS"},
                "address": {"zip": "33050", "state": "FL"},
                "service_code": "03",
                "transit": 3,
                "live": 3,
            },
        ]
        service._unify_destination_transit(planned)
        self.assertEqual(planned[0]["transit"], 3)
        self.assertEqual(planned[1]["transit"], 3)

    def test_thread_inventory_reuses_received_cone_math(self):
        values = [
            ["Color", "Length (ft)", "IN/OUT", "O/R"],
            ["1800 Black", 99000, "IN", "Received"],
            ["1800 Black", 99000, "IN", "Ordered"],
        ]
        self.assertEqual(_thread_data_received_cones(values), {"1800": 6})
        self.assertEqual(_physical_cones_on_hand(5.5, 6), 6)
        self.assertEqual(_physical_cones_on_hand(0, 6), 0)

    def test_sewing_summary_without_timestamps_does_not_invent_averages(self):
        metrics = sewing_output_metrics([
            ["Order #", "Elastic", "Fur", "Flat", "Round", "Top"],
            [100, 2, 2, 2, 2, 12],
        ], now=datetime(2026, 9, 19, tzinfo=ET))
        self.assertFalse(metrics["7"]["enoughData"])
        self.assertEqual(metrics["7"]["finishedPieces"], 0)

    def test_initial_deployment_is_published_without_email(self):
        service = FakeService(FakeStore())
        result = service.rebuild("initial")
        self.assertTrue(result["initialBaseline"])
        self.assertEqual(result["version"]["Status"], "Published")
        self.assertEqual(service.emails, [])

    def test_new_order_creates_approval_and_email_then_can_publish(self):
        store = FakeStore()
        service = FakeService(store)
        service.rebuild("initial")
        service.current_rows = [row(100), row(101)]
        result = service.rebuild("new order")
        version_id = result["version"]["Version ID"]
        self.assertEqual(result["version"]["Status"], "Awaiting Approval")
        self.assertEqual(result["comparison"]["newOrders"], ["101"])
        self.assertEqual(service.emails, [version_id])
        service.approve(version_id, "admin")
        self.assertEqual(store.get_version(version_id)["Status"], "Published")
        self.assertEqual(len(store.approvals), 1)

    def test_same_event_is_idempotent(self):
        store = FakeStore()
        service = FakeService(store)
        service.rebuild("initial")
        service.current_rows = [row(100), row(101)]
        first = service.rebuild("changed")
        second = service.rebuild("changed")
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["version"]["Version ID"], second["version"]["Version ID"])
        self.assertEqual(len(service.emails), 1)

    def test_failed_rebuild_preserves_published(self):
        store = FakeStore()
        service = FakeService(store)
        initial = service.rebuild("initial")
        published_id = initial["version"]["Version ID"]
        service.fail = True
        failed = service.rebuild("failure")
        self.assertFalse(failed["ok"])
        self.assertEqual(store.published_version()["Version ID"], published_id)
        self.assertEqual(store.published_version()["Status"], "Published")


if __name__ == "__main__":
    unittest.main()
