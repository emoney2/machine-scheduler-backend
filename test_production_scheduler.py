import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from production_scheduler import (
    SchedulerConfig,
    build_schedule,
    can_split_embroidery_job,
    detect_shipping_groups,
    embroidery_hours,
    embroidery_runs,
    normalize_orders,
)

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 21, 8, 30, tzinfo=ET)  # Monday


def order(number, **changes):
    row = {
        "Order #": str(number),
        "Date": "09/01/2026",
        "Company Name": "Exact Customer",
        "Design": f"Design {number}",
        "Quantity": 6,
        "Shipped": 0,
        "Product": "Driver",
        "Stage": "Embroidery",
        "Due Date": "09/30/2026",
        "Ship Date": "09/28/2026",
        "Stitch Count": 30000,
        "Threads": "1800",
        "Hard Date/Soft Date": "Hard Date",
        "_shipping_address": {
            "addr1": "1 Main St", "city": "Buford", "state": "GA", "zip": "30519"
        },
        "_thread_usage_cones": {"1800": 1},
    }
    row.update(changes)
    return row


def inventory(cones=18):
    return {"1800": {"cones": cones, "inventory": cones, "onOrder": 0}}


class EmbroideryMathTests(unittest.TestCase):
    def test_divisible_by_six(self):
        self.assertEqual(embroidery_runs(12), 2)
        self.assertAlmostEqual(embroidery_hours(12, 30000), 2.5)

    def test_remainder_requires_full_run(self):
        self.assertEqual(embroidery_runs(7), 2)
        self.assertAlmostEqual(embroidery_hours(7, 30000), 2.5)

    def test_changeover_is_thirty_minutes(self):
        self.assertAlmostEqual(embroidery_hours(6, 30000), 1.5)

    def test_split_permitted_and_prohibited(self):
        allowed, _ = can_split_embroidery_job({"1800": 7}, {"1800": 12})
        self.assertTrue(allowed)
        allowed, _ = can_split_embroidery_job({"1800": 6}, {"1800": 12})
        self.assertFalse(allowed)
        allowed, reason = can_split_embroidery_job({}, {"1800": 12})
        self.assertFalse(allowed)
        self.assertIn("missing", reason.lower())


class ScheduleTests(unittest.TestCase):
    def test_three_machine_capacity_and_jobs_stay_on_one_machine(self):
        rows = [order(i, Quantity=30) for i in (100, 200, 300)]
        result = build_schedule(rows, thread_inventory=inventory(), now=NOW)
        machines = {j["machine"] for j in result["embroidery"]}
        self.assertEqual(machines, {"Machine 1", "Machine 2", "Machine 3"})
        self.assertTrue(all(isinstance(j["machine"], str) for j in result["embroidery"]))

    def test_thread_cone_conflict_prevents_silent_overlap(self):
        rows = [order(100, Quantity=30), order(200, Quantity=30)]
        result = build_schedule(rows, thread_inventory=inventory(6), now=NOW)
        jobs = result["embroidery"]
        self.assertEqual(len(jobs), 2)
        a, b = sorted(jobs, key=lambda j: j["start"])
        self.assertLessEqual(a["finish"], b["start"])

    def test_sewing_spans_days_at_95(self):
        result = build_schedule(
            [order(100, Quantity=190, **{"Stitch Count": 1000})],
            thread_inventory=inventory(),
            now=NOW,
        )
        rows = [r for r in result["sewing"] if r["orderNumber"] == "100"]
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(sum(r["capacityUnits"] for r in rows), 190)
        self.assertTrue(all(r["regularCapacity"] == 95 for r in rows))

    def test_optional_third_sewer_capacity(self):
        base = order(
            100,
            Quantity=140,
            **{"Ship Date": "09/21/2026", "Due Date": "09/21/2026", "Stitch Count": 1000},
        )
        without = build_schedule([base], thread_inventory=inventory(), now=NOW)
        self.assertTrue(any(c["type"] == "sewing_unscheduled" for c in without["conflicts"]))
        cfg = SchedulerConfig.from_dict({"approvedEmergencyDates": ["2026-09-21"]})
        with_extra = build_schedule([base], config=cfg, thread_inventory=inventory(), now=NOW)
        self.assertFalse(any(c["type"] == "sewing_unscheduled" for c in with_extra["conflicts"]))
        self.assertEqual(with_extra["summary"]["thirdSewerDates"], ["2026-09-21"])

    def test_consecutive_exact_customer_orders_group(self):
        normalized, _ = normalize_orders([order(100), order(101)], SchedulerConfig())
        groups, _ = detect_shipping_groups(normalized)
        auto = next(g for g in groups if g["source"] == "inferred")
        self.assertEqual([o["order_number"] for o in auto["orders"]], ["100", "101"])

    def test_similar_customer_names_do_not_group(self):
        normalized, _ = normalize_orders(
            [order(100), order(101, **{"Company Name": "Exact Customer LLC"})],
            SchedulerConfig(),
        )
        groups, _ = detect_shipping_groups(normalized)
        self.assertFalse(any(g["source"] == "inferred" for g in groups))

    def test_group_uses_earliest_ship_date(self):
        result = build_schedule(
            [order(100, **{"Ship Date": "09/28/2026"}), order(101, **{"Ship Date": "09/25/2026"})],
            thread_inventory=inventory(),
            now=NOW,
        )
        self.assertEqual(result["shippingGroups"][0]["requiredShipDate"], "2026-09-25")
        self.assertTrue(any(w["type"] == "shipping_group_date_conflict" for w in result["warnings"]))

    def test_missing_shipping_address_blocks(self):
        result = build_schedule(
            [order(100, _shipping_address={})], thread_inventory=inventory(), now=NOW
        )
        self.assertTrue(any(c["type"] == "shipping_address_required" for c in result["conflicts"]))

    def test_weekends_and_holidays_are_skipped(self):
        result = build_schedule(
            [order(100, Quantity=190, **{"Ship Date": "09/28/2026", "Stitch Count": 1000})],
            config={"holidays": ["2026-09-25"]},
            thread_inventory=inventory(),
            now=NOW,
        )
        days = {r["date"] for r in result["sewing"]}
        self.assertNotIn("2026-09-25", days)
        self.assertTrue(all(datetime.fromisoformat(d).weekday() < 5 for d in days))

    def test_same_day_embroidery_to_sewing_handoff(self):
        result = build_schedule(
            [order(100, Quantity=6, **{"Ship Date": "09/22/2026", "Stitch Count": 1000})],
            thread_inventory=inventory(),
            now=NOW,
        )
        emb = result["embroidery"][0]
        sew = next(r for r in result["sewing"] if r["orderNumber"] == "100")
        self.assertLessEqual(emb["finish"], sew["start"])
        self.assertEqual(emb["sameDaySewing"], emb["finish"][:10] == sew["start"][:10])

    def test_locked_sewing_job_is_preserved_and_not_duplicated(self):
        lock = {"lockId": "L1", "orderNumber": "100", "date": "2026-09-24", "capacityUnits": 6}
        result = build_schedule(
            [order(100)], thread_inventory=inventory(), locks=[lock], now=NOW
        )
        rows = [r for r in result["sewing"] if r["orderNumber"] == "100"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["locked"])
        self.assertEqual(rows[0]["date"], "2026-09-24")

    def test_completion_recalculation_reduces_remaining(self):
        first = build_schedule([order(100, Quantity=100)], thread_inventory=inventory(), now=NOW)
        second = build_schedule(
            [order(100, Quantity=100, _sewing_completed_qty=60)],
            thread_inventory=inventory(),
            now=NOW,
        )
        self.assertGreater(
            sum(r["capacityUnits"] for r in first["sewing"]),
            sum(r["capacityUnits"] for r in second["sewing"]),
        )


if __name__ == "__main__":
    unittest.main()
