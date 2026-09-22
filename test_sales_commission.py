import unittest

from sales_commission import (
    group_dashboard_orders,
    invoice_product_subtotal_for_commission,
    merge_dashboard_group,
)


def _line(order_id, design, product, qty=6, sales=984.0, commission=118.08):
    return {
        "orderId": order_id,
        "rep": "Dan Janton",
        "company": "Lake Lanier Islands",
        "design": design,
        "product": product,
        "quantity": qty,
        "salesAmount": sales,
        "commission": commission,
        "customerPaid": "Y",
        "repPaid": "N",
        "invoiceQboId": "99",
        "invoiceNum": "141",
        "stage": "COMPLETE",
    }


class SalesCommissionGroupingTests(unittest.TestCase):
    def test_four_designs_on_one_invoice_become_one_payout(self):
        rows = group_dashboard_orders(
            [
                _line("974", "Floating Legacy Blade", "Blade"),
                _line("975", "The Sixes Mallet", "Mallet"),
                _line("976", "Legacy Driver", "Driver Full"),
                _line("977", "Legacy Fairway", "Fairway Full"),
            ]
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["company"], "Lake Lanier Islands")
        self.assertEqual(row["quantity"], 24)
        self.assertEqual(row["salesAmount"], 984.0)
        self.assertEqual(row["commission"], 118.08)
        self.assertEqual(row["invoiceNum"], "141")
        self.assertEqual(row["orderIds"], ["974", "975", "976", "977"])
        self.assertEqual(row["lineCount"], 4)

    def test_uninvoiced_orders_stay_separate(self):
        a = _line("100", "A", "Blade")
        b = _line("101", "B", "Mallet")
        a["invoiceQboId"] = ""
        a["invoiceNum"] = ""
        b["invoiceQboId"] = ""
        b["invoiceNum"] = ""
        rows = group_dashboard_orders([a, b])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["salesAmount"], 984.0)
        self.assertEqual(rows[1]["commission"], 118.08)

    def test_merge_empty_is_empty(self):
        self.assertEqual(merge_dashboard_group([]), {})

    def test_invoice_subtotal_excludes_shipping_and_fees(self):
        inv = {
            "ShipAmt": 45.0,
            "TotalAmt": 1078.43,
            "Line": [
                {
                    "DetailType": "SalesItemLineDetail",
                    "Amount": 984.0,
                    "Description": "Blade covers",
                    "SalesItemLineDetail": {"ItemRef": {"name": "Blade"}},
                },
                {
                    "DetailType": "SalesItemLineDetail",
                    "Amount": 45.0,
                    "Description": "UPS Ground shipping",
                    "SalesItemLineDetail": {"ItemRef": {"name": "Shipping"}},
                },
                {
                    "DetailType": "SalesItemLineDetail",
                    "Amount": 49.43,
                    "Description": "Credit card processing fee",
                    "SalesItemLineDetail": {"ItemRef": {"name": "CC Fee"}},
                },
            ],
        }
        self.assertEqual(invoice_product_subtotal_for_commission(inv), 984.0)


if __name__ == "__main__":
    unittest.main()
