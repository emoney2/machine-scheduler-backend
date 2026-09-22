import unittest

from ups_service import (
    _pkg_ship,
    format_ship_to_for_ups_label,
    looks_like_street_address,
)


class LooksLikeStreetTests(unittest.TestCase):
    def test_street_with_number(self):
        self.assertTrue(looks_like_street_address("139 Industrial Park Dr"))

    def test_company_is_not_a_street(self):
        self.assertFalse(looks_like_street_address("Big Cedar Lodge"))

    def test_suite_only_is_not_a_street(self):
        self.assertFalse(looks_like_street_address("Suite B"))

    def test_po_box(self):
        self.assertTrue(looks_like_street_address("PO Box 12"))


class FormatShipToForUpsLabelTests(unittest.TestCase):
    def test_big_cedar_receiving_form(self):
        ship_to = {
            "name": "Big Cedar Lodge",
            "attention_name": "Retail warehouse",
            "phone": "8704168595",
            "addr1": "139 Industrial Park Dr",
            "addr2": "Suite B",
            "city": "Hollister",
            "state": "MO",
            "zip": "65672",
            "country": "US",
        }
        out = format_ship_to_for_ups_label(ship_to, "450021")
        self.assertEqual(out["name"], "Big Cedar Lodge")
        self.assertEqual(out["attention_name"], "ATTN: Retail warehouse")
        self.assertEqual(out["addr1"], "139 Industrial Park Dr, Suite B")
        self.assertEqual(out["addr2"], "PO# 450021")
        self.assertIsNone(out["addr3"])
        self.assertTrue(looks_like_street_address(out["addr1"]))

    def test_contact_name_does_not_replace_company(self):
        out = format_ship_to_for_ups_label(
            {
                "name": "Big Cedar Lodge",
                "attention_name": "Brooke Barron",
                "addr1": "139 Industrial Park Dr",
                "addr2": "Suite B",
                "city": "Hollister",
                "state": "MO",
                "zip": "65672",
            },
            "",
        )
        self.assertEqual(out["name"], "Big Cedar Lodge")
        self.assertEqual(out["attention_name"], "ATTN: Brooke Barron")
        self.assertEqual(out["addr1"], "139 Industrial Park Dr, Suite B")
        self.assertIsNone(out["addr2"])

    def test_blank_po_keeps_real_street_first(self):
        out = format_ship_to_for_ups_label(
            {
                "name": "Acme Golf",
                "attention_name": "Receiving",
                "addr1": "100 Main St",
                "city": "Buford",
                "state": "GA",
                "zip": "30519",
            },
            None,
        )
        self.assertEqual(out["addr1"], "100 Main St")
        self.assertTrue(looks_like_street_address(out["addr1"]))


class PkgShipReferenceTests(unittest.TestCase):
    def test_po_reference_uses_code_po(self):
        pkg = _pkg_ship({"L": 12, "W": 10, "H": 8}, 2, "450021")
        self.assertEqual(pkg["ReferenceNumber"], [{"Code": "PO", "Value": "450021"}])

    def test_blank_po_omits_reference(self):
        pkg = _pkg_ship({"L": 12, "W": 10, "H": 8}, 2, "")
        self.assertNotIn("ReferenceNumber", pkg)


if __name__ == "__main__":
    unittest.main()
