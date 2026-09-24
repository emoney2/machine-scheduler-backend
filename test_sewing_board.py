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


if __name__ == "__main__":
    unittest.main()
