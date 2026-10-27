import unittest
from datetime import date, datetime, timedelta

import wholesale_signup_notify as notify


TODAY = date(2026, 10, 27)


def _serial(day: date, fraction: float = 0) -> float:
    return (datetime(day.year, day.month, day.day) - notify._SHEETS_EPOCH).days + fraction


HEADERS = [
    "Company Name",
    "Contact First Name",
    "Contact Last Name",
    "Contact Email Address",
    "Street Address 1",
    "City",
    "State",
    "Zip Code",
    "Phone Number",
    "Application Submitted",
    "Payment preference",
    "Tax exempt status",
]


def _sheet(*rows):
    return [HEADERS, *rows]


class WholesaleSignupNotifyTests(unittest.TestCase):
    def test_parse_sheets_serial_and_text_dates(self):
        self.assertEqual(notify.parse_submitted(_serial(TODAY, 0.7)), TODAY)
        self.assertEqual(notify.parse_submitted("10/27/2026 4:16:00"), TODAY)
        self.assertEqual(notify.parse_submitted("2026-10-27"), TODAY)
        self.assertIsNone(notify.parse_submitted(""))
        self.assertIsNone(notify.parse_submitted("not a date"))

    def test_pending_signup_includes_who_signed_up(self):
        values = _sheet(
            [
                "Pine Valley Golf",
                "Jane",
                "Doe",
                "jane@pine.test",
                "1 Fairway",
                "Savannah",
                "GA",
                "31401",
                "555-0100",
                _serial(TODAY, 0.5),
                "Credit card (+3% processing fee)",
                "Certificate uploaded",
            ]
        )
        col, needs_header, notices = notify.plan_signups(values, today=TODAY)
        self.assertTrue(needs_header)
        self.assertEqual(len(notices), 1)
        subject, plain, html_body = notify.signup_message(notices[0]["row"])
        self.assertIn("Jane Doe", subject)
        self.assertIn("Pine Valley Golf", subject)
        self.assertIn("jane@pine.test", plain)
        self.assertIn("555-0100", plain)
        self.assertIn("Savannah", html_body)
        self.assertEqual(col, len(HEADERS))

    def test_skips_old_rows_blank_rows_and_already_notified(self):
        headers = HEADERS + ["Signup Notified"]
        values = [
            headers,
            ["Old Club", "Pat", "Lee", "pat@old.test", "", "", "", "", "", _serial(date(2026, 10, 26)), "", "", ""],
            ["New Club", "Ann", "Kim", "ann@new.test", "", "", "", "", "", _serial(TODAY), "", "", "Yes"],
            ["", "", "", "", "", "", "", "", "", _serial(TODAY), "", "", ""],
            ["Today Club", "Sam", "Ray", "sam@today.test", "", "", "", "", "", _serial(TODAY), "", "", ""],
        ]
        _col, needs_header, notices = notify.plan_signups(values, today=TODAY)
        self.assertFalse(needs_header)
        self.assertEqual([n["row"]["Company Name"] for n in notices], ["Today Club"])

    def test_failed_send_is_not_marked_and_success_is(self):
        values = _sheet(
            ["Acme Golf", "Jane", "Doe", "jane@acme.test", "", "", "", "", "", _serial(TODAY), "", ""]
        )
        sent = []

        def send_fn(row):
            sent.append(row["Company Name"])
            return False

        updates = notify.updates_for_signups(values, send_fn=send_fn, today=TODAY, emailed_keys=set())
        self.assertEqual(sent, ["Acme Golf"])
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["values"], [["Signup Notified"]])

        def send_ok(row):
            return True

        keys = set()
        updates = notify.updates_for_signups(values, send_fn=send_ok, today=TODAY, emailed_keys=keys)
        self.assertEqual(updates[-1]["values"], [["Yes"]])
        self.assertTrue(keys)

        again = notify.updates_for_signups(
            values, send_fn=lambda row: (_ for _ in ()).throw(AssertionError("resent")), today=TODAY, emailed_keys=keys
        )
        self.assertEqual(again[-1]["values"], [["Yes"]])


if __name__ == "__main__":
    unittest.main()
