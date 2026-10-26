import unittest
from datetime import date

from sales_breakdown import HOUSE_ACCOUNT, build_sales_breakdown, parse_number


TODAY = date(2026, 10, 26)  # Monday


def _line(sold_on, qty, price, rep, product, order_id, label=None):
    return {
        "date": sold_on,
        "qty": qty,
        "unit_price": price,
        "rep": rep,
        "product": product,
        "product_label": label or product,
        "order_id": order_id,
    }


class ParseNumberTests(unittest.TestCase):
    def test_currency_and_plain_numbers(self):
        self.assertEqual(parse_number("$1,200.50"), 1200.50)
        self.assertEqual(parse_number(6), 6.0)
        self.assertEqual(parse_number("6 pcs"), 6.0)
        self.assertEqual(parse_number(None), 0.0)
        self.assertEqual(parse_number(True), 0.0)


class SalesBreakdownTests(unittest.TestCase):
    def setUp(self):
        self.payload = build_sales_breakdown(
            [
                _line(TODAY, 2, 50, "", "Driver", "100"),
                _line(TODAY, 1, 30, "   ", "Putter", "100"),
                _line(TODAY, 1, 80, " dan ", "Blade", "101"),
                _line(TODAY, 4, 10, "Dan", "Quilted Back", "102"),
                _line(TODAY, 3, 40, "Dan", "Fairway", "103"),
                _line(date(2026, 10, 22), 5, 20, "Ann", "Driver", "104"),
                _line(date(2025, 10, 20), 8, 25, "Ann", "Driver", "90"),
                _line(date(2024, 10, 26), 9, 10, "Ann", "Driver", "1"),
                _line(TODAY, 0, 100, "Ann", "Driver", "105"),
                _line(TODAY, 1.5, 10.10, "House Account", "Hybrid", "106"),
            ],
            TODAY,
        )
        self.by_date = {row["date"]: row for row in self.payload["days"]}

    def test_week_pieces_match_sold_this_week_rules(self):
        # House 2+1, Dan 1+3, House Account hybrid 1.5. Back line and last week left out.
        self.assertEqual(self.payload["weekPieces"], 8.5)
        self.assertEqual(TODAY.weekday(), 0)

    def test_blank_rep_is_house_account_and_names_merge(self):
        today = self.by_date["2026-10-26"]
        self.assertIn(HOUSE_ACCOUNT, today["reps"])
        self.assertNotIn("dan", today["reps"])
        self.assertEqual(today["reps"]["Dan"]["pieces"], 4)
        self.assertEqual(today["reps"]["Dan"]["revenue"], 200)
        self.assertEqual(today["reps"]["Dan"]["orders"], 2)
        # Blank, whitespace, and "House Account" share one bucket. Two orders (100 and 106).
        self.assertEqual(today["reps"][HOUSE_ACCOUNT]["pieces"], 4.5)
        self.assertEqual(today["reps"][HOUSE_ACCOUNT]["revenue"], 145.15)
        self.assertEqual(today["reps"][HOUSE_ACCOUNT]["orders"], 2)

    def test_back_product_and_old_rows_are_left_out(self):
        self.assertGreater(self.payload["stats"]["skippedBack"], 0)
        self.assertNotIn("2024-10-26", self.by_date)
        self.assertIn("2025-10-20", self.by_date)
        self.assertEqual(self.by_date["2025-10-20"]["reps"]["Ann"]["pieces"], 8)

    def test_unique_orders_and_top_products(self):
        today = self.by_date["2026-10-26"]
        # 100, 101, 103, 106. Back order 102 and zero-qty 105 are out.
        self.assertEqual(today["orders"], 4)
        names = [row["name"] for row in self.payload["topProducts"]["day"]]
        self.assertIn("Fairway", names)
        self.assertNotIn("Quilted Back", names)
        fairway = next(row for row in self.payload["topProducts"]["day"] if row["name"] == "Fairway")
        self.assertEqual(fairway["pieces"], 3)
        self.assertEqual(fairway["revenue"], 120)

    def test_year_includes_this_month_and_reps_sort_by_revenue(self):
        year_pieces = sum(row["pieces"] for row in self.payload["days"] if row["date"].startswith("2026"))
        self.assertEqual(year_pieces, 13.5)
        self.assertEqual(self.payload["reps"][0], "Dan")
        self.assertIn(HOUSE_ACCOUNT, self.payload["reps"])
        self.assertIn("Ann", self.payload["reps"])


if __name__ == "__main__":
    unittest.main()
