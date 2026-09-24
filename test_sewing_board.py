import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import sewing_board as sb

ET = ZoneInfo("America/New_York")


def job(oid, remaining=10, **extra):
    row = {
        "orderNumber": str(oid),
        "customer": "Acme",
        "product": "Driver",
        "remainingQuantity": remaining,
        "embroideryReady": True,
        "embroideryRemaining": 0,
        "embroideryPercent": 100,
    }
    row.update(extra)
    return str(oid), row


class SewingBoardTests(unittest.TestCase):
    def test_rolling_weekdays_start_today_on_weekday(self):
        now = datetime(2026, 9, 24, 11, 0, tzinfo=ET)  # Thursday
        days = sb.rolling_weekdays(now, 10)
        self.assertEqual(days[0], "2026-09-24")
        self.assertEqual(days[1], "2026-09-25")
        self.assertEqual(days[2], "2026-09-28")
        self.assertEqual(len(days), 10)
        self.assertEqual(days[-1], "2026-10-07")

    def test_weekend_starts_monday(self):
        now = datetime(2026, 9, 26, 9, 0, tzinfo=ET)  # Saturday
        days = sb.rolling_weekdays(now, 5)
        self.assertEqual(days[0], "2026-09-28")

    def test_unfinished_yesterday_moves_to_top_of_today(self):
        jobs = dict([
            job("100"),
            job("200"),
            job("300"),
        ])
        board = {
            "queue": [],
            "days": {
                "2026-09-23": ["100", "200"],
                "2026-09-24": ["300"],
                "2026-09-25": ["400"],
            },
            "lastRolloverDate": "2026-09-23",
            "carryovers": [],
        }
        jobs["400"] = job("400")[1]
        now = datetime(2026, 9, 24, 8, 0, tzinfo=ET)
        next_board, carryovers = sb.rollover_unfinished(board, jobs, now=now)
        self.assertEqual(next_board["days"]["2026-09-24"], ["100", "200", "300"])
        self.assertEqual(next_board["days"]["2026-09-25"], ["400"])
        self.assertNotIn("2026-09-23", next_board["days"])
        self.assertEqual([c["orderNumber"] for c in carryovers], ["100", "200"])
        self.assertEqual(next_board["lastRolloverDate"], "2026-09-24")
        self.assertEqual(next_board["overdue"]["100"], "2026-09-23")
        self.assertEqual(next_board["overdue"]["200"], "2026-09-23")
        self.assertNotIn("300", next_board["overdue"])

    def test_finished_yesterday_does_not_roll(self):
        jobs = dict([job("100", remaining=0), job("200")])
        board = {
            "queue": [],
            "days": {"2026-09-23": ["100", "200"], "2026-09-24": []},
            "lastRolloverDate": "2026-09-23",
            "carryovers": [],
        }
        now = datetime(2026, 9, 24, 8, 0, tzinfo=ET)
        next_board, carryovers = sb.rollover_unfinished(board, jobs, now=now)
        self.assertEqual(next_board["days"]["2026-09-24"], ["200"])
        self.assertEqual([c["orderNumber"] for c in carryovers], ["200"])

    def test_future_days_do_not_move(self):
        jobs = dict([job("100"), job("200")])
        board = {
            "queue": [],
            "days": {"2026-09-24": ["100"], "2026-09-29": ["200"]},
            "lastRolloverDate": "2026-09-24",
            "carryovers": [],
        }
        now = datetime(2026, 9, 24, 8, 0, tzinfo=ET)
        next_board, carryovers = sb.rollover_unfinished(board, jobs, now=now)
        self.assertEqual(next_board["days"]["2026-09-29"], ["200"])
        self.assertEqual(carryovers, [])

    def test_new_jobs_land_in_queue(self):
        jobs = dict([job("100"), job("200")])
        board = {"queue": ["100"], "days": {}, "lastRolloverDate": "2026-09-24", "carryovers": []}
        clean = sb.apply_catalog(board, jobs)
        self.assertEqual(clean["queue"], ["100", "200"])

    def test_queue_sorts_by_ship_date(self):
        jobs = dict([
            job("100", requiredShipDate="2026-10-03"),
            job("200", requiredShipDate="2026-09-26"),
            job("300", requiredShipDate="2026-09-30"),
        ])
        board = {"queue": ["100", "200", "300"], "days": {}, "lastRolloverDate": "2026-09-24", "carryovers": []}
        clean = sb.apply_catalog(board, jobs)
        self.assertEqual(clean["queue"], ["200", "300", "100"])

    def test_closed_jobs_leave_the_board(self):
        jobs = dict([job("200")])
        board = {"queue": ["100"], "days": {"2026-09-24": ["200"]}, "lastRolloverDate": "", "carryovers": []}
        clean = sb.apply_catalog(board, jobs)
        self.assertEqual(clean["queue"], [])
        self.assertEqual(clean["days"]["2026-09-24"], ["200"])

    def test_seed_uses_published_sewing_dates_once(self):
        jobs = dict([job("100"), job("200")])
        schedule = {
            "sewing": [
                {"orderNumber": "100", "date": "2026-09-25"},
                {"orderNumber": "200", "date": "2026-09-28"},
            ]
        }
        seeded = sb.seed_from_schedule(sb.empty_board(), schedule, jobs)
        self.assertEqual(seeded["days"]["2026-09-25"], ["100"])
        self.assertEqual(seeded["days"]["2026-09-28"], ["200"])
        again = sb.seed_from_schedule(seeded, schedule, jobs)
        self.assertEqual(again["days"]["2026-09-25"], ["100"])

    def test_catalog_skips_backs_and_finished_sewing(self):
        schedule = {
            "orders": [
                {"order_number": "1", "product": "Driver Back", "remaining_quantity": 4, "customer": "A"},
                {"order_number": "2", "product": "Driver", "remaining_quantity": 0, "customer": "B"},
                {"order_number": "3", "product": "Driver", "remaining_quantity": 6, "customer": "C", "due_type": "Hard Date"},
                {"order_number": "4", "product": "Fairway", "remaining_quantity": 8, "stage": "SHIPPED", "customer": "D"},
                {"order_number": "5", "product": "HybridBack", "remaining_quantity": 3, "customer": "E"},
                {"order_number": "6", "product": "Driver", "remaining_quantity": 4, "quantity": 4, "shipped": 4, "customer": "F"},
            ]
        }
        jobs = sb.catalog_jobs(schedule, progress={})
        self.assertEqual(list(jobs), ["3"])
        self.assertTrue(jobs["3"]["hardDate"])
        self.assertFalse(jobs["3"]["embroideryReady"])
        self.assertEqual(jobs["3"]["embroideryCompletedQty"], 0)

    def test_sewing_log_sum_drops_finished_jobs(self):
        schedule = {
            "orders": [
                {"order_number": "10", "product": "Driver", "remaining_quantity": 6, "quantity": 6, "customer": "J"},
                {"order_number": "11", "product": "Driver", "remaining_quantity": 4, "quantity": 4, "customer": "K"},
            ]
        }
        finished = sb.parse_sewing_top_by_order([
            ["Timestamp", "Order #", "Name", "Elastic", "Fur", "Flat", "Round", "Top"],
            ["2026-09-24", "10", "Ann", 0, 0, 0, 0, 2],
            ["2026-09-24", "10", "Ann", 0, 0, 0, 0, 4],
            ["2026-09-24", "11", "Ann", 0, 0, 0, 0, 1],
        ], accumulate="sum")
        self.assertEqual(finished["10"], 6)
        self.assertEqual(finished["11"], 1)
        jobs = sb.catalog_jobs(schedule, progress={}, sewing_finished=finished)
        self.assertNotIn("10", jobs)
        self.assertIn("11", jobs)

    def test_catalog_embroidery_ready_only_after_progress(self):
        schedule = {
            "orders": [
                {"order_number": "8", "product": "Driver", "remaining_quantity": 4, "quantity": 4, "customer": "H"},
                {"order_number": "9", "product": "Driver", "remaining_quantity": 4, "quantity": 4, "customer": "I", "embroidery_status": "COMPLETE"},
            ]
        }
        jobs = sb.catalog_jobs(schedule, progress={"8": {"completedQty": 2}})
        self.assertFalse(jobs["8"]["embroideryReady"])
        self.assertEqual(jobs["8"]["embroideryPercent"], 50.0)
        self.assertTrue(jobs["9"]["embroideryReady"])
        self.assertEqual(jobs["9"]["embroideryPercent"], 100)

    def test_catalog_reads_sheet_hard_date_column(self):
        schedule = {
            "orders": [
                {
                    "order_number": "7",
                    "product": "Driver",
                    "remaining_quantity": 4,
                    "customer": "G",
                    "Hard Date/Soft Date": "Hard Date",
                },
            ]
        }
        jobs = sb.catalog_jobs(schedule, progress={})
        self.assertTrue(jobs["7"]["hardDate"])
        self.assertIn("HARD", jobs["7"]["due_type"].upper())

    def test_queue_return_clears_overdue(self):
        board = {
            "queue": ["100"],
            "days": {"2026-09-24": ["200"]},
            "overdue": {"100": "2026-09-23", "200": "2026-09-23"},
            "lastRolloverDate": "2026-09-24",
            "carryovers": [],
        }
        clean = sb.clear_queue_overdue(board)
        self.assertNotIn("100", clean["overdue"])
        self.assertEqual(clean["overdue"]["200"], "2026-09-23")

    def test_reset_puts_every_job_in_queue(self):
        jobs = dict([job("100"), job("200")])
        now = datetime(2026, 9, 24, 8, 0, tzinfo=ET)
        board = sb.board_reset_to_queue(jobs, now=now)
        self.assertEqual(board["queue"], ["100", "200"])
        self.assertEqual(board["days"], {})
        self.assertEqual(board["overdue"], {})
        self.assertEqual(board["resetToken"], sb.RESET_TOKEN)

    def test_live_orders_refresh_dates_and_qty(self):
        schedule = {
            "orders": [
                {
                    "order_number": "3",
                    "product": "Driver",
                    "remaining_quantity": 6,
                    "quantity": 6,
                    "customer": "Old Co",
                    "due_date": "2026-09-01",
                    "required_ship_date": "2026-08-28",
                    "due_type": "Soft Date",
                    "stage": "EMBROIDERY",
                },
            ]
        }
        jobs = sb.catalog_jobs(schedule, progress={})
        live = [{
            "Order #": "3",
            "Company Name": "New Co",
            "Product": "Driver",
            "Quantity": 65,
            "Due Date": "10/05/2026",
            "Shipping Method": "Local Delivery",
            "Ship Date": 46296,
            "Hard Date/Soft Date": "Hard Date",
            "Stage": "SEWING",
        }]
        merged = sb.merge_live_orders(jobs, live, progress={}, drop_missing=True)
        self.assertEqual(merged["3"]["customer"], "New Co")
        self.assertEqual(merged["3"]["quantity"], 65)
        self.assertEqual(merged["3"]["dueDate"], "2026-10-05")
        self.assertEqual(merged["3"]["requiredShipDate"], "2026-10-05")
        self.assertEqual(merged["3"]["shippingMethod"], "Local Delivery")
        self.assertTrue(merged["3"]["hardDate"])
        self.assertTrue(merged["3"]["embroideryReady"])
        self.assertEqual(merged["3"]["stage"], "SEWING")

    def test_live_orders_drop_missing_and_add_new(self):
        schedule = {
            "orders": [
                {"order_number": "3", "product": "Driver", "remaining_quantity": 6, "quantity": 6, "customer": "C"},
            ]
        }
        jobs = sb.catalog_jobs(schedule, progress={})
        live = [{
            "Order #": "9",
            "Company Name": "Fresh",
            "Product": "Fairway",
            "Quantity": 12,
            "Due Date": "2026-10-08",
            "Ship Date": "2026-10-02",
            "Stage": "SEWING",
            "Hard Date/Soft Date": "Soft Date",
        }]
        merged = sb.merge_live_orders(jobs, live, progress={}, drop_missing=True)
        self.assertNotIn("3", merged)
        self.assertIn("9", merged)
        self.assertEqual(merged["9"]["customer"], "Fresh")
        self.assertEqual(merged["9"]["remainingQuantity"], 12)
        self.assertTrue(merged["9"]["embroideryReady"])

    def test_incomplete_live_orders_do_not_drop_catalog(self):
        schedule = {
            "orders": [
                {
                    "order_number": str(i),
                    "product": "Driver",
                    "remaining_quantity": 4,
                    "quantity": 4,
                    "customer": f"C{i}",
                }
                for i in range(10, 22)
            ]
        }
        jobs = sb.catalog_jobs(schedule, progress={})
        live = [{
            "Order #": "10",
            "Company Name": "C10",
            "Product": "Driver",
            "Quantity": 4,
            "Due Date": "2026-10-08",
            "Stage": "SEWING",
        }]
        merged = sb.merge_live_orders(jobs, live, progress={}, drop_missing=True)
        self.assertEqual(len(merged), 12)
        self.assertIn("21", merged)

    def test_missing_reset_token_keeps_day_placements(self):
        import tempfile
        from pathlib import Path
        jobs = dict([job("100"), job("200")])
        now = datetime(2026, 9, 24, 8, 0, tzinfo=ET)
        with tempfile.TemporaryDirectory() as tmp:
            original = sb.BOARD_PATH
            sb.BOARD_PATH = Path(tmp) / "sewing_board.json"
            try:
                board = {
                    "queue": [],
                    "days": {"2026-09-24": ["100"], "2026-09-25": ["200"]},
                    "resetToken": "",
                    "lastRolloverDate": "2026-09-24",
                    "carryovers": [],
                }
                sb.save_board(board)
                snap = sb.snapshot(
                    {"orders": [
                        {"order_number": "100", "product": "Driver", "remaining_quantity": 4, "quantity": 4, "customer": "A"},
                        {"order_number": "200", "product": "Mallet", "remaining_quantity": 4, "quantity": 4, "customer": "B"},
                    ]},
                    now=now,
                    persist=True,
                    progress={},
                    live_orders=None,
                )
                self.assertEqual(snap["board"].get("2026-09-24"), ["100"])
                self.assertEqual(snap["board"].get("2026-09-25"), ["200"])
                saved = sb.load_board()
                self.assertEqual(saved["days"].get("2026-09-24"), ["100"])
                self.assertNotEqual(saved.get("queue"), ["100", "200"])
            finally:
                sb.BOARD_PATH = original

    def test_live_ship_date_uses_method_and_travel_days(self):
        jobs = {}
        local = [{
            "Order #": "10",
            "Company Name": "River Club",
            "Product": "Driver",
            "Quantity": 12,
            "Due Date": "10/05/2026",
            "Shipping Method": "Local Delivery",
            "Ship Date": "09/28/2026",
            "Stage": "SEWING",
        }]
        west = [{
            "Order #": "11",
            "Company Name": "TaylorMade",
            "Product": "Mallet",
            "Quantity": 65,
            "Due Date": "10/05/2026",
            "Shipping Method": "UPS Ground",
            "Shipping City": "Carlsbad",
            "Shipping State": "CA",
            "Ship Date": "09/28/2026",
            "Stage": "SEWING",
        }]
        air = [{
            "Order #": "12",
            "Company Name": "Club",
            "Product": "Fairway",
            "Quantity": 12,
            "Due Date": "10/05/2026",
            "Shipping Method": "Next Day Air",
            "Ship Date": "09/28/2026",
            "Stage": "SEWING",
        }]
        local_job = sb.merge_live_orders(jobs, local, progress={}, drop_missing=True)["10"]
        west_job = sb.merge_live_orders(jobs, west, progress={}, drop_missing=True)["11"]
        air_job = sb.merge_live_orders(jobs, air, progress={}, drop_missing=True)["12"]
        self.assertEqual(local_job["requiredShipDate"], "2026-10-05")
        self.assertEqual(west_job["requiredShipDate"], "2026-09-25")
        self.assertEqual(air_job["requiredShipDate"], "2026-10-01")
        self.assertNotEqual(west_job["requiredShipDate"], "2026-09-28")

    def test_missing_shipping_method_does_not_invent_ground_ship_date(self):
        jobs = {}
        live = [{
            "Order #": "13",
            "Company Name": "River Club",
            "Product": "Driver",
            "Quantity": 12,
            "Due Date": "09/25/2026",
            "Ship Date": "09/21/2026",
            "Stage": "SEWING",
        }]
        merged = sb.merge_live_orders(jobs, live, progress={}, drop_missing=True)["13"]
        self.assertEqual(merged["dueDate"], "2026-09-25")
        self.assertFalse(merged.get("shippingMethod"))
        self.assertNotEqual(merged.get("requiredShipDate"), "2026-09-21")

    def test_seed_skipped_when_reset_token_set(self):
        jobs = dict([job("100")])
        board = sb.empty_board()
        board["resetToken"] = sb.RESET_TOKEN
        schedule = {"sewing": [{"orderNumber": "100", "date": "2026-09-25"}]}
        seeded = sb.seed_from_schedule(board, schedule, jobs)
        self.assertEqual(seeded["days"], {})


if __name__ == "__main__":
    unittest.main()
