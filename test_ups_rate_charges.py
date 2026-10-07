import unittest

from ups_service import (
    _money_and_currency_from_rated,
    _rated_charge_amounts,
    _row_from_rated,
)


def _pascal_rated(list_amt="159.15", negotiated_amt="66.56"):
    return {
        "Service": {"Code": "13"},
        "TotalCharges": {"CurrencyCode": "USD", "MonetaryValue": list_amt},
        "NegotiatedRateCharges": {
            "TotalCharge": {"CurrencyCode": "USD", "MonetaryValue": negotiated_amt}
        },
    }


def _camel_rated(list_amt="159.15", negotiated_amt="66.56"):
    return {
        "service": {"Code": "13"},
        "totalCharges": {"currencyCode": "USD", "monetaryValue": list_amt},
        "negotiatedRateCharges": {
            "totalCharge": {"currencyCode": "USD", "monetaryValue": negotiated_amt}
        },
    }


class RatedChargeParsingTests(unittest.TestCase):
    def test_prefers_pascal_negotiated_over_list(self):
        listed, negotiated, curr = _rated_charge_amounts(_pascal_rated())
        self.assertEqual(listed, 159.15)
        self.assertEqual(negotiated, 66.56)
        self.assertEqual(curr, "USD")
        money, _ = _money_and_currency_from_rated(_pascal_rated())
        self.assertEqual(float(money), 66.56)

    def test_prefers_camel_negotiated_over_list(self):
        """REST v2409 often uses monetaryValue; old parser only read MonetaryValue."""
        listed, negotiated, curr = _rated_charge_amounts(_camel_rated())
        self.assertEqual(listed, 159.15)
        self.assertEqual(negotiated, 66.56)
        self.assertEqual(curr, "USD")
        money, _ = _money_and_currency_from_rated(_camel_rated())
        self.assertEqual(float(money), 66.56)

    def test_list_only_when_no_negotiated_container(self):
        rated = {"Service": {"Code": "03"}, "TotalCharges": {"MonetaryValue": "37.44"}}
        listed, negotiated, _ = _rated_charge_amounts(rated)
        self.assertEqual(listed, 37.44)
        self.assertIsNone(negotiated)
        money, _ = _money_and_currency_from_rated(rated)
        self.assertEqual(float(money), 37.44)

    def test_row_exposes_list_rate_and_source(self):
        row = _row_from_rated(_camel_rated())
        self.assertIsNotNone(row)
        self.assertEqual(row["rate"], 66.56)
        self.assertEqual(row["list_rate"], 159.15)
        self.assertEqual(row["rate_source"], "negotiated")
        self.assertEqual(row["code"], "13")
        self.assertEqual(row["method"], "Next Day Air Saver")

    def test_row_includes_ups_billing_weight(self):
        rated = _camel_rated()
        rated["billingWeight"] = {"unitOfMeasurement": {"code": "LBS"}, "weight": "8.0"}
        row = _row_from_rated(rated)
        self.assertEqual(row["billed_weight"], 8.0)

    def test_row_marks_list_when_negotiated_missing(self):
        row = _row_from_rated(
            {"Service": {"Code": "03"}, "TotalCharges": {"MonetaryValue": "37.44"}}
        )
        self.assertEqual(row["rate"], 37.44)
        self.assertEqual(row["list_rate"], 37.44)
        self.assertEqual(row["rate_source"], "list")


if __name__ == "__main__":
    unittest.main()
