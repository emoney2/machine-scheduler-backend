import unittest
from datetime import date

from magnet_kanban import (
    INITIAL_INBOUND_EVENT_ID,
    build_status,
    fur_magnet_totals,
    product_pair_multiplier,
)


class MagnetKanbanTests(unittest.TestCase):
    def test_product_multipliers(self):
        self.assertEqual(product_pair_multiplier("Mallet"), 1)
        self.assertEqual(product_pair_multiplier("DF3 Mallet"), 1)
        self.assertEqual(product_pair_multiplier("Blade"), 2)
        self.assertEqual(product_pair_multiplier("Center Shafted Mallet"), 2)
        self.assertEqual(product_pair_multiplier("MackMade Mid Mallet"), 2)
        self.assertEqual(product_pair_multiplier("Driver Full"), 0)

    def test_fur_commitments_deduplicate_rows(self):
        rows = [
            {
                "Order #": "213",
                "Product": "Blade",
                "Quantity": 12,
                "Quantity Made": 0,
            },
            {
                "Order #": "213",
                "Product": "Blade",
                "Quantity": 12,
                "Quantity Made": 0,
            },
            {
                "Order #": "214",
                "Product": "Mallet",
                "Quantity": 10,
                "Quantity Made": 4,
            },
        ]
        result = fur_magnet_totals(rows)
        self.assertEqual(result["madePairs"], 4)
        self.assertEqual(result["committedPairs"], 30)

    def test_complete_fur_row_has_no_commitment(self):
        result = fur_magnet_totals(
            [
                {
                    "Order #": "10",
                    "Product": "Blade",
                    "Quantity": 12,
                    "Quantity Made": "",
                    "Status": "COMPLETE",
                }
            ]
        )
        self.assertEqual(result["madePairs"], 24)
        self.assertEqual(result["committedPairs"], 0)

    def test_baseline_inventory_and_initial_inbound(self):
        production = []
        fur = [
            {
                "Order #": "1",
                "Product": "Mallet",
                "Quantity": 766,
                "Quantity Made": 0,
            }
        ]
        result = build_status(
            production,
            fur,
            [],
            today=date(2026, 9, 18),
            baseline_made_pairs=0,
        )
        self.assertEqual(result["physicalPairs"], 3000)
        self.assertEqual(result["committedPairs"], 766)
        self.assertEqual(result["uncommittedPairs"], 2234)
        self.assertEqual(result["inboundPairs"], 5000)
        self.assertEqual(result["inventoryPositionPairs"], 7234)

    def test_receiving_initial_shipment_moves_inbound_to_physical(self):
        events = [
            {
                "Type": "RECEIVED",
                "Kanban ID": "MAGNETS-NS",
                "Event ID": INITIAL_INBOUND_EVENT_ID,
                "Event Qty": 5000,
                "Timestamp": "2026-10-03T12:00:00Z",
            }
        ]
        result = build_status(
            [],
            [],
            events,
            today=date(2026, 10, 3),
            baseline_made_pairs=0,
        )
        self.assertEqual(result["physicalPairs"], 8000)
        self.assertEqual(result["inboundPairs"], 0)
        self.assertEqual(result["inventoryPositionPairs"], 8000)
        self.assertTrue(result["initialInboundReceived"])

    def test_production_consumption_reduces_physical_not_position(self):
        fur = [
            {
                "Order #": "1",
                "Product": "Blade",
                "Quantity": 10,
                "Quantity Made": 5,
            }
        ]
        result = build_status(
            [],
            fur,
            [],
            today=date(2026, 9, 18),
            baseline_made_pairs=0,
        )
        self.assertEqual(result["physicalPairs"], 2990)
        self.assertEqual(result["committedPairs"], 10)
        self.assertEqual(result["inventoryPositionPairs"], 7980)


if __name__ == "__main__":
    unittest.main()
