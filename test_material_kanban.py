import unittest
from datetime import date, timedelta

from material_kanban import (
    build_status,
    fur_yards_for_product,
    material_from_name,
    usage_by_material,
    yards_from_rolls,
)


class MaterialKanbanTests(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(material_from_name("Black")["id"], "BLACK-FUR")
        self.assertEqual(material_from_name("light gray")["id"], "LIGHT-GREY-FUR")
        self.assertIsNone(material_from_name("Navy Fur"))

    def test_long_neck_missing_sibling_ppy_does_not_crash(self):
        today = date(2026, 9, 21)
        production = [
            {
                "Order #": "99",
                "Date": today,
                "Company Name": "Club",
                "Product": "Long Neck Blade",
                "Quantity": 2,
                "Fur Color": "Black Fur",
            }
        ]
        result = build_status(
            production,
            [],
            [{"Product": "Long Neck Blade", "PPY": 18}, {"Product": "Blade", "PPY": None}],
            [],
            today=today,
        )
        self.assertTrue(result["ok"])
        raw_table = [
            ["Product", "SKU", "Width", "Length", "Unit", "PPY"],
            ["Long Neck Blade", "", "", "", "Yards", 18],
            ["Blade", "", "", "", "Yards", None],
        ]
        raw_result = build_status(production, [], raw_table, [], today=today)
        self.assertTrue(raw_result["ok"])

    def test_fur_usage_rules(self):
        self.assertAlmostEqual(fur_yards_for_product("Mallet", 20, 20), 1.0)
        self.assertAlmostEqual(fur_yards_for_product("Blade", 40, 20), 2.0)
        self.assertAlmostEqual(fur_yards_for_product("Fairway Full", 20, 20), 2.0)
        self.assertEqual(fur_yards_for_product("Driver Back", 20, 20), 0.0)

    def test_uncut_is_committed_cut_is_consumed(self):
        table = [{"Product": "Mallet", "PPY": 20}]
        production = [
            {
                "Order #": "100",
                "Date": date(2026, 8, 1),
                "Company Name": "Club",
                "Product": "Mallet",
                "Quantity": 20,
                "Fur Color": "Black Fur",
            },
            {
                "Order #": "101",
                "Date": date(2026, 8, 8),
                "Company Name": "Club",
                "Product": "Mallet",
                "Quantity": 40,
                "Fur Color": "Black Fur",
            },
        ]
        cuts = [
            {"Order #": "100", "Quantity": 20, "Quantity Made": 20, "Status": "COMPLETE"},
            {"Order #": "101", "Quantity": 40, "Quantity Made": 0, "Status": "NOT STARTED"},
        ]
        usage = usage_by_material(production, cuts, table)
        self.assertAlmostEqual(usage["BLACK-FUR"]["consumedYards"], 1.0)
        self.assertAlmostEqual(usage["BLACK-FUR"]["committedYards"], 2.0)

    def test_new_order_raises_committed_not_physical(self):
        today = date(2026, 9, 21)
        table = [{"Product": "Mallet", "PPY": 20}]
        production = [
            {
                "Order #": "10",
                "Date": today,
                "Company Name": "Club",
                "Product": "Mallet",
                "Quantity": 20,
                "Fur Color": "Light Grey Fur",
            }
        ]
        result = build_status(production, [], table, [], today=today)
        grey = next(row for row in result["materials"] if row["id"] == "LIGHT-GREY-FUR")
        self.assertAlmostEqual(grey["committedYards"], 1.0)
        self.assertAlmostEqual(grey["physicalYards"], round(yards_from_rolls(6), 1))
        self.assertAlmostEqual(grey["uncommittedYards"], round(yards_from_rolls(6) - 1.0, 1))

    def test_cut_after_count_reduces_physical_and_committed(self):
        today = date(2026, 9, 21)
        table = [{"Product": "Mallet", "PPY": 20}]
        production = [
            {
                "Order #": "10",
                "Date": today - timedelta(days=3),
                "Company Name": "Club",
                "Product": "Mallet",
                "Quantity": 40,
                "Fur Color": "Black Fur",
            }
        ]
        count = [
            {
                "Type": "MATERIAL_COUNT",
                "Kanban ID": "MAT-BLACK-FUR",
                "Event Qty": yards_from_rolls(4),
                "Timestamp": "2026-09-18T12:00:00Z",
                "Notes": '{"rolls":4,"consumedYards":0}',
            }
        ]
        before = build_status(production, [], table, count, today=today)
        after = build_status(
            production,
            [{"Order #": "10", "Quantity": 40, "Status": "COMPLETE"}],
            table,
            count,
            today=today,
        )
        black_before = next(row for row in before["materials"] if row["id"] == "BLACK-FUR")
        black_after = next(row for row in after["materials"] if row["id"] == "BLACK-FUR")
        self.assertAlmostEqual(black_before["committedYards"], 2.0)
        self.assertAlmostEqual(black_after["committedYards"], 0.0)
        self.assertAlmostEqual(
            black_after["physicalYards"],
            round(black_before["physicalYards"] - 2.0, 1),
        )


if __name__ == "__main__":
    unittest.main()
