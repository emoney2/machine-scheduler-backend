import json
import unittest
from unittest.mock import patch

import packing_history as packhist


def _pieces(qty=1):
    return [{"order_id": "123", "product": "Widget", "qty": qty}]


class BoxSuggestionTests(unittest.TestCase):
    def test_one_small_item_uses_ten_inch_cube_not_largest_box(self):
        result = packhist.suggest_boxes(
            _pieces(), history=[], volume_map={"widget": 400}
        )

        counts = result["suggestion"]["box_counts"]
        self.assertEqual(counts["10x10x10"], 1)
        self.assertEqual(counts["20x20x20"], 0)
        self.assertEqual(counts["9x7x4"], 0)
        self.assertEqual(counts["14x9x3"], 0)

    def test_box_is_sized_for_all_remaining_product_volume(self):
        result = packhist.suggest_boxes(
            _pieces(qty=2), history=[], volume_map={"widget": 600}
        )

        counts = result["suggestion"]["box_counts"]
        self.assertEqual(counts["13x13x13"], 1)
        self.assertEqual(counts["20x20x20"], 0)

    def test_shipped_box_overrides_oversized_product_table_volume(self):
        record = packhist.build_record(
            order_ids=["122"],
            boxes=[{"L": 10, "W": 10, "H": 10, "weight": 10, "qty": 1}],
            pieces=_pieces(),
        )

        result = packhist.suggest_boxes(
            _pieces(), history=[record], volume_map={"widget": 8000}
        )

        counts = result["suggestion"]["box_counts"]
        self.assertEqual(counts["10x10x10"], 1)
        self.assertEqual(counts["20x20x20"], 0)

    def test_durable_sheet_history_is_loaded_and_deduplicated(self):
        boxes = [{"L": 10, "W": 10, "H": 10, "weight": 10, "qty": 1}]
        pieces = _pieces()
        contents = [
            {
                "L": 10,
                "W": 10,
                "H": 10,
                "weight": 10,
                "pieces": pieces,
            }
        ]
        rows = [
            packhist.SHEET_HEADERS,
            [
                "2026-09-03T12:00:00.000Z",
                "packing-1",
                "Customer",
                "123",
                "widget:1",
                json.dumps(boxes),
                json.dumps(pieces),
                json.dumps(contents),
                "1Z123",
            ],
        ]

        loaded = packhist.load_history_from_sheet(lambda *_: rows, "sheet")
        merged = packhist.merge_history(loaded, loaded)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], "packing-1")
        self.assertEqual(merged[0]["box_contents"][0]["preset_id"], "10x10x10")

    def test_tracking_only_record_is_saved_for_shipping_history(self):
        rec = packhist.build_record(
            company="Acme",
            order_ids=["99"],
            tracking_numbers=["1Z999"],
            shipped_at="2026-09-24T12:00:00.000Z",
        )
        self.assertIsNotNone(rec)
        self.assertEqual(rec["tracking_numbers"], ["1Z999"])
        self.assertEqual(rec["boxes"], [])
        rows = packhist.flatten_shipping_rows([rec])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tracking_number"], "1Z999")
        self.assertEqual(rows[0]["company"], "Acme")
        self.assertEqual(rows[0]["source"], "server")

    @patch.object(packhist, "append_record", side_effect=lambda rec: rec)
    def test_import_shipping_entries_skips_known_tracking(self, _append):
        existing = [
            packhist.build_record(
                company="Old",
                tracking_numbers=["1Z111"],
                shipped_at="2026-09-01T12:00:00.000Z",
            )
        ]
        created = packhist.import_shipping_entries(
            [
                {"company": "Old", "trackingNumbers": ["1Z111"]},
                {
                    "company": "New Co",
                    "trackingNumbers": ["1Z222"],
                    "shippedAt": "2026-09-24T15:00:00.000Z",
                    "orderIds": ["55"],
                },
            ],
            existing=existing,
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["tracking_numbers"], ["1Z222"])
        self.assertEqual(created[0]["company"], "New Co")


if __name__ == "__main__":
    unittest.main()
