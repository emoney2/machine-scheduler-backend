import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from production_scheduler import (
    LOCAL_DELIVERY_TRANSIT_DAYS,
    SchedulerConfig,
    build_schedule,
    can_split_embroidery_job,
    detect_shipping_groups,
    embroidery_hours,
    embroidery_runs,
    estimate_ground_transit_days,
    is_back_product,
    is_local_delivery,
    is_towel_or_needlepoint,
    normalize_orders,
    parse_date,
    parse_sewers,
    resolve_required_ship_date,
)
from schedule_store import (
    SHEET_CELL_LIMIT,
    compact_order,
    friendly_sheets_error,
    is_sheets_rate_limit,
    version_summary_metadata,
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

    def test_single_head_runs_one_piece_at_a_time(self):
        self.assertEqual(embroidery_runs(7, 1), 7)
        self.assertAlmostEqual(embroidery_hours(6, 30000, 1), 6.5)

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
        self.assertEqual(machines, {"Machine 2", "Machine 3", "Machine 4"})
        self.assertTrue(all(j["heads"] == 6 for j in result["embroidery"]))
        self.assertTrue(all(isinstance(j["machine"], str) for j in result["embroidery"]))

    def test_single_head_machine_is_available(self):
        rows = [order(i, Quantity=30) for i in (100, 200, 300)] + [order(400, Quantity=1)]
        result = build_schedule(rows, thread_inventory=inventory(30), now=NOW)
        machines = {j["machine"] for j in result["embroidery"]}
        self.assertIn("Single Head Machine", machines)
        single = next(j for j in result["embroidery"] if j["machine"] == "Single Head Machine")
        self.assertEqual(single["orderNumber"], "400")
        self.assertEqual(single["heads"], 1)
        self.assertEqual(single["runs"], 1)

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
        self.assertEqual([r["dayQuantity"] for r in rows], [95, 95])
        self.assertTrue(all(r["split"] for r in rows))
        self.assertEqual(sum(r["dayQuantity"] for r in rows), 190)

    def test_nearby_ground_transit_is_not_a_week(self):
        self.assertEqual(estimate_ground_transit_days("30519", "GA"), 1)
        self.assertEqual(estimate_ground_transit_days("30305", "GA"), 1)
        self.assertEqual(estimate_ground_transit_days("28202", "NC"), 2)
        self.assertEqual(estimate_ground_transit_days("10001", "NY"), 3)
        self.assertEqual(estimate_ground_transit_days("90210", "CA"), 5)
        due = date(2026, 9, 30)
        nearby = resolve_required_ship_date(due, 2)
        week = resolve_required_ship_date(due, 5)
        self.assertEqual(str(nearby), "2026-09-28")
        self.assertEqual(str(week), "2026-09-23")
        self.assertGreater(nearby, week)

    def test_actual_transit_overrides_conservative_sheet_ship_date(self):
        result = build_schedule(
            [order(100, Quantity=6, **{
                "Ship Date": "09/23/2026",
                "Due Date": "09/30/2026",
                "_transit_business_days": 2,
            })],
            thread_inventory=inventory(),
            now=NOW,
        )
        rows = [r for r in result["sewing"] if r["orderNumber"] == "100"]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["requiredShipDate"], "2026-09-28")
        self.assertEqual(rows[0]["transitBusinessDays"], 2)
        self.assertFalse(any(r["late"] for r in rows))

    def test_local_delivery_uses_one_day_buffer(self):
        self.assertTrue(is_local_delivery("Local Delivery"))
        self.assertTrue(is_local_delivery("local"))
        self.assertFalse(is_local_delivery("UPS"))
        self.assertEqual(LOCAL_DELIVERY_TRANSIT_DAYS, 1)
        due = date(2026, 9, 30)
        self.assertEqual(str(resolve_required_ship_date(due, 1)), "2026-09-29")
        result = build_schedule(
            [order(100, Quantity=6, **{
                "Ship Date": "09/23/2026",
                "Due Date": "09/30/2026",
                "Shipping Method": "Local Delivery",
            })],
            thread_inventory=inventory(),
            now=NOW,
        )
        rows = [r for r in result["sewing"] if r["orderNumber"] == "100"]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["shippingMethod"], "Local Delivery")
        self.assertEqual(rows[0]["transitBusinessDays"], 1)
        self.assertEqual(rows[0]["requiredShipDate"], "2026-09-29")
        self.assertFalse(any(r["late"] for r in rows))

    def test_back_products_are_not_sewn(self):
        self.assertTrue(is_back_product("Driver Back"))
        self.assertFalse(is_back_product("Driver Front"))
        result = build_schedule(
            [
                order(100, Product="Driver Front", Quantity=6),
                order(101, Product="Driver Back", Quantity=6),
            ],
            thread_inventory=inventory(),
            now=NOW,
        )
        self.assertTrue(all(r["product"] != "Driver Back" for r in result["sewing"]))
        self.assertTrue(any(r["orderNumber"] == "100" for r in result["sewing"]))
        self.assertTrue(any(r["orderNumber"] == "101" for r in result["embroidery"]))
        back = next(o for o in result["orders"] if o["order_number"] == "101")
        self.assertFalse(back["needs_sewing"])

    def test_towels_and_needlepoint_are_not_scheduled(self):
        self.assertTrue(is_towel_or_needlepoint("Golf Towel"))
        self.assertTrue(is_towel_or_needlepoint("Printed Towels"))
        self.assertTrue(is_towel_or_needlepoint("Needlepoint"))
        self.assertTrue(is_towel_or_needlepoint("Needle Point Wallet"))
        self.assertFalse(is_towel_or_needlepoint("Driver"))
        result = build_schedule(
            [
                order(100, Product="Driver", Quantity=6),
                order(101, Product="Golf Towel", Quantity=12),
                order(102, Product="Needle Point Wallet", Quantity=6),
            ],
            thread_inventory=inventory(),
            now=NOW,
        )
        sew = {r["product"] for r in result["sewing"]}
        emb = {r["product"] for r in result["embroidery"]}
        self.assertIn("Driver", sew)
        self.assertIn("Driver", emb)
        self.assertNotIn("Golf Towel", sew)
        self.assertNotIn("Golf Towel", emb)
        self.assertNotIn("Needle Point Wallet", sew)
        self.assertNotIn("Needle Point Wallet", emb)
        towel = next(o for o in result["orders"] if o["order_number"] == "101")
        needle = next(o for o in result["orders"] if o["order_number"] == "102")
        self.assertFalse(towel["needs_sewing"])
        self.assertFalse(needle["needs_sewing"])
        self.assertEqual(towel["embroidery_remaining"], 0)
        self.assertEqual(needle["embroidery_remaining"], 0)

    def test_optional_third_sewer_capacity(self):
        base = order(
            100,
            Quantity=140,
            **{"Ship Date": "09/21/2026", "Due Date": "09/21/2026", "Stitch Count": 1000},
        )
        result = build_schedule([base], thread_inventory=inventory(), now=NOW)
        rows = [r for r in result["sewing"] if r["orderNumber"] == "100"]
        self.assertFalse(any(c["type"] == "sewing_unscheduled" for c in result["conflicts"]))
        self.assertAlmostEqual(sum(r["capacityUnits"] for r in rows), 140)
        self.assertTrue(any(r.get("emergencyUsed") for r in rows))
        self.assertEqual(result["summary"]["thirdSewerDates"], ["2026-09-21"])
        self.assertFalse(any(r["late"] for r in rows))
        self.assertTrue(any(w["type"] == "emergency_sewing" for w in result["warnings"]))

    def test_named_sewer_out_reduces_that_days_capacity(self):
        self.assertEqual(
            [s["name"] for s in parse_sewers([["Name"], ["Maria"], ["Jose"], ["Justin"], ["Ana"]])],
            ["Maria", "Jose", "Ana"],
        )
        self.assertEqual(
            [s["role"] for s in parse_sewers([["Date", "Maria", "Jose", "Ana", "Justin"]])],
            ["regular", "regular", "emergency"],
        )
        self.assertEqual(
            [s["name"] for s in parse_sewers([
                ["Sewing"],
                [],
                ["", "Luz", "Yessenia", "Karla", "Justin"],
            ])],
            ["Luz", "Yessenia", "Karla"],
        )
        cfg = SchedulerConfig.from_dict({
            "sewers": [
                {"name": "Maria", "role": "regular", "capacity": 47.5},
                {"name": "Jose", "role": "regular", "capacity": 47.5},
                {"name": "Ana", "role": "emergency", "capacity": 50},
            ],
            "sewerAbsences": {"2026-09-21": ["Maria"]},
        })
        result = build_schedule(
            [order(100, Quantity=40, **{
                "Ship Date": "09/21/2026",
                "Due Date": "09/21/2026",
                "Stitch Count": 1000,
            })],
            config=cfg,
            thread_inventory=inventory(),
            now=NOW,
        )
        rows = [r for r in result["sewing"] if r["orderNumber"] == "100"]
        self.assertTrue(rows)
        self.assertAlmostEqual(max(r["regularCapacity"] for r in rows), 47.5)
        self.assertFalse(any(r.get("emergencyUsed") for r in rows))

    def test_emergency_sewer_out_blocks_overtime(self):
        cfg = SchedulerConfig.from_dict({
            "sewers": [
                {"name": "Maria", "role": "regular", "capacity": 47.5},
                {"name": "Jose", "role": "regular", "capacity": 47.5},
                {"name": "Ana", "role": "emergency", "capacity": 50},
            ],
            "sewerAbsences": {"2026-09-21": ["Ana"]},
        })
        result = build_schedule(
            [order(100, Quantity=140, **{
                "Ship Date": "09/21/2026",
                "Due Date": "09/21/2026",
                "Stitch Count": 1000,
            })],
            config=cfg,
            thread_inventory=inventory(),
            now=NOW,
        )
        rows = [r for r in result["sewing"] if r["orderNumber"] == "100"]
        self.assertTrue(rows)
        self.assertFalse(any(r.get("emergencyUsed") for r in rows))
        self.assertEqual(result["summary"]["thirdSewerDates"], [])

    def test_on_time_jobs_do_not_use_emergency_sewing(self):
        result = build_schedule(
            [order(100, Quantity=6)],
            thread_inventory=inventory(),
            now=NOW,
        )
        rows = [r for r in result["sewing"] if r["orderNumber"] == "100"]
        self.assertTrue(rows)
        self.assertFalse(any(r.get("emergencyUsed") for r in rows))
        self.assertEqual(result["summary"]["thirdSewerDates"], [])

    def test_soft_jobs_fill_early_sewing_gaps(self):
        result = build_schedule(
            [
                order(100, Quantity=6, **{
                    "Hard Date/Soft Date": "Hard Date",
                    "Ship Date": "09/28/2026",
                    "Due Date": "09/28/2026",
                    "Stitch Count": 1000,
                }),
                order(200, Quantity=6, **{
                    "Hard Date/Soft Date": "Soft Date",
                    "Ship Date": "09/28/2026",
                    "Due Date": "09/30/2026",
                    "Stitch Count": 1000,
                    "Company Name": "Soft Customer",
                }),
            ],
            thread_inventory=inventory(),
            now=NOW,
        )
        hard = [r for r in result["sewing"] if r["orderNumber"] == "100"]
        soft = [r for r in result["sewing"] if r["orderNumber"] == "200"]
        self.assertTrue(hard and soft)
        self.assertEqual(min(r["date"] for r in soft), "2026-09-21")
        self.assertLessEqual(min(r["date"] for r in hard), "2026-09-28")
        self.assertFalse(any(r["late"] for r in hard))
        self.assertIn("2026-09-21", {r["date"] for r in result["sewing"]})

    def test_later_job_fills_an_empty_weekday(self):
        locks = [
            {"lockId": "L1", "orderNumber": "1", "date": "2026-09-21", "capacityUnits": 95},
            {"lockId": "L2", "orderNumber": "2", "date": "2026-09-22", "capacityUnits": 95},
            {"lockId": "L3", "orderNumber": "3", "date": "2026-09-23", "capacityUnits": 95},
        ]
        result = build_schedule(
            [
                order(1, Quantity=95, **{"Ship Date": "09/21/2026", "Due Date": "09/21/2026", "Stitch Count": 1000}),
                order(2, Quantity=95, **{"Ship Date": "09/22/2026", "Due Date": "09/22/2026", "Stitch Count": 1000, "Company Name": "B"}),
                order(3, Quantity=95, **{"Ship Date": "09/23/2026", "Due Date": "09/23/2026", "Stitch Count": 1000, "Company Name": "C"}),
                order(200, Quantity=100, **{
                    "Hard Date/Soft Date": "Soft Date",
                    "Ship Date": "09/25/2026",
                    "Due Date": "09/28/2026",
                    "Stitch Count": 1000,
                    "Company Name": "Soft Customer",
                }),
            ],
            thread_inventory=inventory(),
            locks=locks,
            now=NOW,
        )
        soft = [r["date"] for r in result["sewing"] if r["orderNumber"] == "200"]
        self.assertIn("2026-09-24", soft)

    def test_soft_job_moves_up_when_the_gap_is_too_small(self):
        lock = {"lockId": "L1", "orderNumber": "100", "date": "2026-09-21", "capacityUnits": 60}
        result = build_schedule(
            [
                order(100, Quantity=60, **{
                    "Ship Date": "09/21/2026",
                    "Due Date": "09/21/2026",
                    "Stitch Count": 1000,
                }),
                order(200, Quantity=100, **{
                    "Hard Date/Soft Date": "Soft Date",
                    "Ship Date": "09/28/2026",
                    "Due Date": "09/30/2026",
                    "Stitch Count": 1000,
                    "Company Name": "Soft Customer",
                }),
            ],
            thread_inventory=inventory(),
            locks=[lock],
            now=NOW,
        )
        soft = sorted(
            [r for r in result["sewing"] if r["orderNumber"] == "200"],
            key=lambda r: r["date"],
        )
        self.assertGreaterEqual(len(soft), 2)
        self.assertEqual(soft[0]["date"], "2026-09-21")
        self.assertLess(soft[0]["dayQuantity"], 100)
        self.assertEqual(sum(r["dayQuantity"] for r in soft), 100)

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

    def test_different_due_dates_are_separate_shipments(self):
        result = build_schedule(
            [
                order(100, **{"Due Date": "10/01/2026", "Ship Date": "09/28/2026"}),
                order(101, **{"Due Date": "11/01/2026", "Ship Date": "10/28/2026"}),
            ],
            thread_inventory=inventory(),
            now=NOW,
        )
        groups = result["shippingGroups"]
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(len(g["orderNumbers"]) == 1 for g in groups))
        sew = {r["orderNumber"]: r for r in result["sewing"]}
        self.assertEqual(sew["100"]["requiredShipDate"], "2026-09-28")
        self.assertEqual(sew["101"]["requiredShipDate"], "2026-10-28")
        self.assertFalse(any(w["type"] == "shipping_group_date_conflict" for w in result["warnings"]))

    def test_explicit_group_splits_when_due_dates_differ(self):
        normalized, _ = normalize_orders(
            [
                order(100, **{
                    "Due Date": "10/01/2026",
                    "Ship Date": "09/28/2026",
                    "_shipping_group_id": "REPEAT-A",
                }),
                order(101, **{
                    "Due Date": "11/01/2026",
                    "Ship Date": "10/28/2026",
                    "_shipping_group_id": "REPEAT-A",
                }),
            ],
            SchedulerConfig(),
        )
        groups, warnings = detect_shipping_groups(normalized)
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(len(g["orders"]) == 1 for g in groups))
        by_order = {
            g["orders"][0]["order_number"]: g["orders"][0]["required_ship_date"]
            for g in groups
        }
        self.assertEqual(str(by_order["100"]), "2026-09-28")
        self.assertEqual(str(by_order["101"]), "2026-10-28")
        self.assertFalse(any(w["type"] == "shipping_group_date_conflict" for w in warnings))

    def test_missing_shipping_address_warns_but_still_schedules(self):
        result = build_schedule(
            [order(100, _shipping_address={})], thread_inventory=inventory(), now=NOW
        )
        self.assertTrue(any(c["type"] == "shipping_address_required" for c in result["warnings"]))
        self.assertTrue(any(r["orderNumber"] == "100" for r in result["sewing"]))
        self.assertTrue(any(r["orderNumber"] == "100" for r in result["embroidery"]))

    def test_past_ship_date_is_still_placed(self):
        result = build_schedule(
            [order(100, Quantity=40, **{
                "Hard Date/Soft Date": "Soft Date",
                "Ship Date": "09/10/2026",
                "Due Date": "09/10/2026",
            })],
            thread_inventory=inventory(),
            now=NOW,
        )
        rows = [r for r in result["sewing"] if r["orderNumber"] == "100"]
        self.assertTrue(rows)
        self.assertTrue(any(r["late"] for r in rows))
        self.assertGreaterEqual(min(r["date"] for r in rows), "2026-09-21")

    def test_hard_date_is_never_late(self):
        result = build_schedule(
            [order(100, Quantity=400, **{
                "Hard Date/Soft Date": "Hard Date",
                "Ship Date": "09/21/2026",
                "Due Date": "09/21/2026",
                "Stitch Count": 1000,
            })],
            thread_inventory=inventory(),
            now=NOW,
        )
        rows = [r for r in result["sewing"] if r["orderNumber"] == "100"]
        self.assertTrue(rows)
        self.assertFalse(any(r["late"] for r in rows))
        self.assertTrue(all(r["date"] <= "2026-09-21" for r in rows))
        self.assertAlmostEqual(sum(r["capacityUnits"] for r in rows), 400)

    def test_past_hard_date_is_not_marked_late(self):
        result = build_schedule(
            [order(100, Quantity=40, **{"Ship Date": "09/10/2026", "Due Date": "09/10/2026"})],
            thread_inventory=inventory(),
            now=NOW,
        )
        rows = [r for r in result["sewing"] if r["orderNumber"] == "100"]
        self.assertTrue(rows)
        self.assertFalse(any(r["late"] for r in rows))
        self.assertTrue(any(c["type"] == "hard_date_missed" for c in result["conflicts"]))

    def test_ordered_without_stitches_stays_on_sewing_only(self):
        result = build_schedule(
            [order(100, Stage="ORDERED", **{"Stitch Count": 0, "Threads": ""})],
            thread_inventory=inventory(),
            now=NOW,
        )
        self.assertTrue(any(r["orderNumber"] == "100" for r in result["sewing"]))
        self.assertFalse(any(r["orderNumber"] == "100" for r in result["embroidery"]))
        self.assertTrue(any(w["type"] == "missing_stitch_count" for w in result["warnings"]))

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


class PersistenceSizeTests(unittest.TestCase):
    def test_sheets_rate_limit_message_is_human(self):
        exc = RuntimeError("HttpError 429 ... Quota exceeded for quota metric 'Read requests'")
        self.assertTrue(is_sheets_rate_limit(exc))
        self.assertIn("busy", friendly_sheets_error(exc).lower())

    def test_parse_date_accepts_sheet_serials_and_serial_strings(self):
        expected = date(1899, 12, 30) + timedelta(days=45980)
        self.assertEqual(parse_date(45980), expected)
        self.assertEqual(parse_date("45980.0"), expected)
        self.assertEqual(str(parse_date("09/30/2026")), "2026-09-30")

    def test_version_summary_stays_under_sheet_cell_limit(self):
        rows = [order(1000 + i, Quantity=12) for i in range(250)]
        result = build_schedule(rows, thread_inventory=inventory(120), now=NOW)
        encoded = __import__("json").dumps(version_summary_metadata(result), default=str)
        self.assertLess(len(encoded), SHEET_CELL_LIMIT)
        sample = compact_order(result["orders"][0])
        self.assertIn("order_number", sample)
        self.assertIn("image", sample)
        self.assertLess(len(__import__("json").dumps(sample, default=str)), 2000)

    def test_sewing_jobs_keep_artwork_link(self):
        result = build_schedule(
            [order(100, Image="https://drive.google.com/file/d/ABCDEFGHIJKLMNOPQRSTUVWXYZ0123/view")],
            thread_inventory=inventory(),
            now=NOW,
        )
        job = next(r for r in result["sewing"] if r["orderNumber"] == "100")
        self.assertIn("drive.google.com", job["image"])
        from schedule_store import safe_job_payload
        self.assertIn("drive.google.com", safe_job_payload(job)["image"])


if __name__ == "__main__":
    unittest.main()
