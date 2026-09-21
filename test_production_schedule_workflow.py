import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from production_schedule_service import (
    ProductionScheduleService,
    _parse_embroidery_progress,
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

    def test_same_customer_different_zips_keep_their_own_transit(self):
        service = FakeService(FakeStore())
        planned = [
            {
                "row": {"Company Name": "Acme Golf", "Shipping Method": "UPS"},
                "address": {"zip": "37203", "state": "TN", "city": "Nashville"},
                "service_code": "03",
                "transit": 2,
                "live": 2,
            },
            {
                "row": {"Company Name": "Acme Golf", "Shipping Method": "UPS"},
                "address": {"zip": "80202", "state": "CO", "city": "Denver"},
                "service_code": "03",
                "transit": 4,
                "live": 4,
            },
        ]
        service._unify_destination_transit(planned)
        self.assertEqual(planned[0]["transit"], 2)
        self.assertEqual(planned[1]["transit"], 4)

    def test_job_ship_address_beats_company_directory(self):
        def resolve(row, _by_id, _sheets):
            zipc = str(row.get("Order Ship ZIP") or "").strip()
            if not zipc:
                return {}
            return {
                "addr1": str(row.get("Order Ship Street 1") or ""),
                "city": str(row.get("Order Ship City") or ""),
                "state": str(row.get("Order Ship State") or ""),
                "zip": zipc,
            }

        service = ProductionScheduleService(
            store=FakeStore(),
            fetch_sheet=lambda *a, **k: [],
            orders_range="Production Orders!A1:BZ",
            resolve_order_address=resolve,
            fetch_directory_row=lambda _name: {
                "Street Address 1": "1 HQ Blvd",
                "City": "Denver",
                "State": "CO",
                "Zip Code": "80202",
            },
            normalize_directory_address=lambda _row: {
                "addr1": "1 HQ Blvd",
                "city": "Denver",
                "state": "CO",
                "zip": "80202",
            },
            ups_get_rate=lambda *a, **k: [],
            frontend_url="https://example.test",
        )
        job_addr = service._address(
            {
                "Company Name": "Acme Golf",
                "Order Ship Street 1": "100 Broadway",
                "Order Ship City": "Nashville",
                "Order Ship State": "TN",
                "Order Ship ZIP": "37203",
            },
            {},
            {
                "acme golf": {
                    "Street Address 1": "1 HQ Blvd",
                    "City": "Denver",
                    "State": "CO",
                    "Zip Code": "80202",
                }
            },
        )
        self.assertEqual(job_addr.get("zip"), "37203")
        self.assertEqual(job_addr.get("city"), "Nashville")
        self.assertEqual(service._planning_transit({}, job_addr, "03"), 2)

        company_only = service._address({"Company Name": "Acme Golf"}, {}, None)
        self.assertEqual(company_only.get("zip"), "80202")

        zip_only = service._address(
            {"Company Name": "Acme Golf", "Order Ship ZIP": "37203", "Order Ship State": "TN"},
            {},
            None,
        )
        self.assertEqual(zip_only.get("zip"), "37203")
        self.assertEqual(service._planning_transit({}, zip_only, "03"), 2)

    def test_carlsbad_ground_is_five_days(self):
        def live_three(_ship_to, _pkgs, ask_all_services=False):
            return [{"code": "03", "business_days": 3}]

        service = ProductionScheduleService(
            store=FakeStore(),
            fetch_sheet=lambda *a, **k: [],
            orders_range="Production Orders!A1:BZ",
            resolve_order_address=lambda *_a, **_k: {},
            fetch_directory_row=lambda _name: None,
            normalize_directory_address=lambda _row: {},
            ups_get_rate=live_three,
            frontend_url="https://example.test",
        )
        carlsbad = {"city": "Carlsbad", "state": "CA", "zip": "92008"}
        self.assertEqual(service._planning_transit({}, {"city": "Carlsbad", "state": "CA"}, "03"), 5)
        self.assertEqual(service._planning_transit({}, carlsbad, "03"), 5)
        planned = [
            {
                "row": {"Company Name": "TaylorMade", "Shipping Method": "UPS Ground"},
                "address": carlsbad,
                "service_code": "03",
                "transit": 5,
                "live": 3,
            },
            {
                "row": {"Company Name": "TaylorMade", "Shipping Method": "UPS Ground"},
                "address": {"city": "Carlsbad", "state": "CA", "zip": "92008"},
                "service_code": "03",
                "transit": 5,
                "live": 3,
            },
        ]
        service._unify_destination_transit(planned)
        self.assertEqual(planned[0]["transit"], 5)
        self.assertEqual(planned[1]["transit"], 5)

    def test_embroidery_list_quantity_made_is_used(self):
        parsed = _parse_embroidery_progress([
            ["Order #", "Status", "Quantity Made"],
            ["100", "In Progress", 0],
            ["101", "COMPLETE", 12],
        ])
        self.assertEqual(parsed["100"]["status"], "In Progress")
        self.assertEqual(parsed["100"]["qty"], 0)
        self.assertEqual(parsed["101"]["status"], "COMPLETE")
        self.assertEqual(parsed["101"]["qty"], 12)

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
