import unittest

import directory_customer as dc


HEADERS = [
    "Company Name",
    "Contact First Name",
    "Contact Last Name",
    "Contact Email Address",
    "Street Address 1",
    "Street Address 2",
    "City",
    "State",
    "Zip Code",
    "Phone Number",
    "Shipping Email",
    "Shipping Phone",
    "Shipping Attention",
    "Shipping Street Address 1",
    "Billing Same as Shipping",
    "Billing First Name",
    "Billing Last Name",
    "Billing Email 1",
    "Billing Phone",
    "Billing Street Address 1",
    "Billing City",
    "Billing State",
    "Billing Zip",
]


class DirectoryCustomerTests(unittest.TestCase):
    def test_col_letter(self):
        self.assertEqual(dc.col_letter(0), "A")
        self.assertEqual(dc.col_letter(25), "Z")
        self.assertEqual(dc.col_letter(26), "AA")

    def test_find_company_row_is_case_insensitive(self):
        values = [
            HEADERS,
            ["Acme Golf", "Old", "Buyer", "old@acme.com"],
            ["Other Co", "Pat", "Lee", "pat@other.com"],
        ]
        found = dc.find_company_row(values, "  acme golf ")
        self.assertIsNotNone(found)
        row_num, headers, row = found
        self.assertEqual(row_num, 2)
        self.assertEqual(headers[0], "Company Name")
        self.assertEqual(row["Contact First Name"], "Old")

    def test_find_company_row_missing(self):
        values = [HEADERS, ["Acme Golf", "Old", "Buyer"]]
        self.assertIsNone(dc.find_company_row(values, "Nope"))

    def test_form_from_row_prefers_shipping_columns(self):
        row = {
            "Company Name": "Acme Golf",
            "Contact First Name": "Old",
            "Contact Last Name": "Buyer",
            "Contact Email Address": "old@acme.com",
            "Street Address 1": "1 Main",
            "Shipping Street Address 1": "9 Dock",
            "City": "Aiken",
            "Shipping City": "Savannah",
            "Billing Same as Shipping": "Yes",
            "Billing First Name": "Old",
            "Billing Last Name": "Buyer",
        }
        form = dc.form_from_row(row)
        self.assertEqual(form["streetAddress1"], "9 Dock")
        self.assertEqual(form["city"], "Savannah")
        self.assertTrue(form["billingSameAsShipping"])
        self.assertTrue(form["billingSameAsContact"])

    def test_normalize_copies_buyer_into_billing_when_same(self):
        fields = dc.normalize_update_fields(
            {
                "contactFirstName": "Jane",
                "contactLastName": "Doe",
                "contactEmailAddress": "jane@acme.com",
                "phoneNumber": "555-0100",
                "streetAddress1": "9 Dock",
                "city": "Savannah",
                "state": "GA",
                "zipCode": "31401",
                "billingSameAsContact": True,
                "billingSameAsShipping": True,
            }
        )
        self.assertEqual(fields["shippingEmail"], "jane@acme.com")
        self.assertEqual(fields["billingFirstName"], "Jane")
        self.assertEqual(fields["billingLastName"], "Doe")
        self.assertEqual(fields["billingEmail1"], "jane@acme.com")
        self.assertEqual(fields["billingStreetAddress1"], "9 Dock")
        self.assertEqual(fields["billingSameAsShipping"], "Yes")

    def test_same_contact_overwrites_stale_billing_email(self):
        fields = dc.normalize_update_fields(
            {
                "contactFirstName": "Jane",
                "contactLastName": "Doe",
                "contactEmailAddress": "jane@acme.com",
                "billingEmail1": "old@acme.com",
                "billingSameAsContact": True,
                "billingSameAsShipping": False,
            }
        )
        self.assertEqual(fields["billingEmail1"], "jane@acme.com")
        self.assertEqual(fields["billingFirstName"], "Jane")
        self.assertEqual(fields["billingSameAsShipping"], "No")

    def test_header_writes_fill_contact_and_shipping_columns(self):
        fields = dc.normalize_update_fields(
            {
                "contactFirstName": "Jane",
                "contactLastName": "Doe",
                "contactEmailAddress": "jane@acme.com",
                "phoneNumber": "555-0100",
                "shippingAttention": "Receiving",
                "streetAddress1": "9 Dock",
                "billingSameAsShipping": False,
                "billingFirstName": "Accounts",
                "billingLastName": "Payable",
                "billingEmail1": "ap@acme.com",
            }
        )
        writes = dc.header_writes_from_fields(fields, HEADERS)
        self.assertEqual(writes["Contact First Name"], "Jane")
        self.assertEqual(writes["Contact Email Address"], "jane@acme.com")
        self.assertEqual(writes["Shipping Email"], "jane@acme.com")
        self.assertEqual(writes["Phone Number"], "555-0100")
        self.assertEqual(writes["Shipping Phone"], "555-0100")
        self.assertEqual(writes["Shipping Attention"], "Receiving")
        self.assertEqual(writes["Street Address 1"], "9 Dock")
        self.assertEqual(writes["Shipping Street Address 1"], "9 Dock")
        self.assertEqual(writes["Billing First Name"], "Accounts")
        self.assertEqual(writes["Billing Same as Shipping"], "No")
        self.assertNotIn("Company Name", writes)

    def test_sheet_value_ranges_use_column_letters(self):
        writes = {"Contact First Name": "Jane", "Shipping Attention": "Receiving"}
        ranges = dc.sheet_value_ranges("Directory", HEADERS, 4, writes)
        by_range = {item["range"]: item["values"][0][0] for item in ranges}
        self.assertEqual(by_range["Directory!B4"], "Jane")
        self.assertEqual(by_range["Directory!M4"], "Receiving")

    def test_qbo_sparse_update_omits_empty_and_keeps_id(self):
        payload = dc.build_qbo_customer_sparse_update(
            "123",
            "5",
            given_name="Jane",
            family_name="Doe",
            email="jane@acme.com",
            phone="555-0100",
            bill_addr={"Line1": "9 Dock", "City": "Savannah"},
            ship_addr={"Line1": "9 Dock", "City": "Savannah"},
        )
        self.assertEqual(payload["Id"], "123")
        self.assertEqual(payload["SyncToken"], "5")
        self.assertTrue(payload["sparse"])
        self.assertEqual(payload["GivenName"], "Jane")
        self.assertEqual(payload["PrimaryEmailAddr"]["Address"], "jane@acme.com")
        self.assertEqual(payload["BillAddr"]["Line1"], "9 Dock")
        self.assertNotIn("DisplayName", payload)

    def test_supabase_payload_skips_company_name(self):
        payload = dc.supabase_directory_payload(
            {"contactFirstName": "Jane", "phoneNumber": "555"}
        )
        self.assertEqual(payload["Contact First Name"], "Jane")
        self.assertNotIn("Company Name", payload)


if __name__ == "__main__":
    unittest.main()
