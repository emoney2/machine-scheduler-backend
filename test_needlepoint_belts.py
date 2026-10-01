import json
import unittest
from unittest.mock import MagicMock, patch

import needlepoint_belts as npb


class NeedlepointHelpersTests(unittest.TestCase):
    def test_detects_needlepoint_product_names(self):
        self.assertTrue(npb.is_needlepoint_product("Needlepoint"))
        self.assertTrue(npb.is_needlepoint_product("Needle Point Wallet"))
        self.assertTrue(npb.is_needlepoint_product("NEEDLEPOINT BELT"))
        self.assertFalse(npb.is_needlepoint_product("Driver"))
        self.assertFalse(npb.is_needlepoint_product("Golf Towel"))

    def test_parse_size_quantities_from_dict_and_json(self):
        self.assertEqual(npb.parse_size_quantities({"34": "2", "36": 1, "99": 4, "29": 1}), {"29": 1, "34": 2, "36": 1})
        self.assertEqual(
            npb.parse_size_quantities(json.dumps({"32": "1", "40": "3"})),
            {"32": 1, "40": 3},
        )
        self.assertEqual(npb.parse_size_quantities("34x2, 36=1"), {"34": 2, "36": 1})
        self.assertEqual(npb.parse_size_quantities(""), {})
        self.assertEqual(npb.total_quantity({"34": 2, "36": 1}), 3)
        self.assertEqual(npb.size_summary({"34": 2, "36": 1}), "34×2, 36×1")

    def test_belt_sizes_include_odds_from_28_to_54(self):
        self.assertEqual(npb.BELT_SIZES[0], "28")
        self.assertEqual(npb.BELT_SIZES[-1], "54")
        self.assertEqual(list(npb.BELT_SIZES), [str(n) for n in range(28, 55)])
        self.assertIn("29", npb.BELT_SIZES)
        self.assertIn("53", npb.BELT_SIZES)

    def test_pending_status_treats_blank_as_pending(self):
        self.assertTrue(npb.is_pending_status(""))
        self.assertTrue(npb.is_pending_status("Pending"))
        self.assertFalse(npb.is_pending_status("Ordered"))

    def test_sheet_row_layout_matches_headers(self):
        row = npb.build_sheet_row(
            order_number=4401,
            company="Augusta",
            design="Crest",
            product="Needlepoint",
            due_date="2026-10-15",
            sizes={"34": 2, "38": 1},
            notes="rush",
            preview_file_id="abc123",
            submitted_at="10/1/2026 13:00:00",
        )
        self.assertEqual(len(row), len(npb.SHEET_HEADERS))
        self.assertEqual(row[0], 4401)
        self.assertEqual(row[6], 3)
        self.assertEqual(row[npb.SHEET_HEADERS.index("34")], 2)
        self.assertEqual(row[npb.SHEET_HEADERS.index("38")], 1)
        self.assertEqual(row[npb.SHEET_HEADERS.index("Size Summary")], "34×2, 38×1")
        self.assertEqual(row[npb.SHEET_HEADERS.index("Status")], "Pending")

    def test_email_body_includes_sizes_and_thread_colors(self):
        subject, body = npb.build_email_text(
            [
                {
                    "orderNumber": "4401",
                    "company": "Augusta",
                    "design": "Crest",
                    "sizes": {"34": 2, "38": 1},
                    "threadColors": "1801 Navy, 1637 Red",
                }
            ]
        )
        self.assertIn("Needlepoint Belt Order", subject)
        self.assertIn("Order 4401", body)
        self.assertIn("34×2, 38×1", body)
        self.assertIn("1801 Navy, 1637 Red", body)
        self.assertIn("named by order number", body)

    def test_attachment_filename_uses_order_number(self):
        self.assertEqual(npb.attachment_filename(4401, "logo.PNG"), "4401.png")
        self.assertEqual(npb.attachment_filename(4401, "a.jpg", 1, 2), "4401-1.jpg")
        self.assertEqual(npb.attachment_filename(4401, "b.jpg", 2, 2), "4401-2.jpg")

    def test_list_pending_skips_ordered_rows(self):
        values_svc = MagicMock()
        values_svc.get.return_value.execute.return_value = {
            "values": [
                list(npb.SHEET_HEADERS),
                npb.build_sheet_row(order_number="1", sizes={"34": 1}, status="Pending"),
                npb.build_sheet_row(order_number="2", sizes={"36": 1}, status="Ordered"),
                npb.build_sheet_row(order_number="3", sizes={"38": 2}, status=""),
            ]
        }
        sheets = MagicMock()
        sheets.spreadsheets.return_value.values.return_value = values_svc
        with patch.object(npb, "ensure_sheet", return_value=True):
            pending = npb.list_pending_orders(sheets, "sheet-id")
        nums = [p["orderNumber"] for p in pending]
        self.assertEqual(nums, ["1", "3"])


if __name__ == "__main__":
    unittest.main()
