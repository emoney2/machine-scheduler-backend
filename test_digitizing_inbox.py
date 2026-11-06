"""Unit tests for Classic Digitizers Gmail → Drive importer matching logic."""
from __future__ import annotations

import unittest

import digitizing_inbox as inbox


EXAMPLE_SUBJECT = "PO 1670 Jigglypuff Fairway"
EXAMPLE_FILES = [
    "PO 1670 Jigglypuff Fairway.EMB",
    "PO 1670 Jigglypuff Fairway.DST",
    "PO 1670 Jigglypuff Fairway.pdf",
    "PO 1670 Jigglypuff Fairway.PNG",
]
VENDOR_FROM = "William Hill <classicdigitizers@gmail.com>"


class ParsePoTests(unittest.TestCase):
    def test_example_subject(self):
        po = inbox.parse_po_from_subject(EXAMPLE_SUBJECT)
        self.assertTrue(po["ok"])
        self.assertEqual(po["orderNumber"], "1670")

    def test_reply_forward_prefixes_and_spacing(self):
        cases = [
            "Re: PO 1670 Jigglypuff Fairway",
            "FW: po#1670 design",
            "Fwd: PO-1670 files",
            "Re: Re: PO  1670  Jigglypuff",
            "[EXTERNAL] PO:1670 ready",
        ]
        for subject in cases:
            with self.subTest(subject=subject):
                po = inbox.parse_po_from_subject(subject)
                self.assertTrue(po["ok"], subject)
                self.assertEqual(po["orderNumber"], "1670", subject)

    def test_missing_po_needs_review(self):
        po = inbox.parse_po_from_subject("Your logo files are attached")
        self.assertFalse(po["ok"])
        self.assertEqual(po["reason"], "missing_po")

    def test_ambiguous_po_numbers(self):
        po = inbox.parse_po_from_subject("PO 1670 vs PO 1671 comparison")
        self.assertFalse(po["ok"])
        self.assertEqual(po["reason"], "ambiguous_po")
        self.assertEqual(po["candidates"], ["1670", "1671"])


class SenderAndAttachmentTests(unittest.TestCase):
    def test_exact_vendor_email_not_display_name(self):
        self.assertTrue(inbox.is_vendor_sender(VENDOR_FROM))
        self.assertFalse(
            inbox.is_vendor_sender("Classic Digitizers <other@example.com>")
        )
        self.assertFalse(inbox.is_vendor_sender("justin.eckard@jrcogolf.com"))

    def test_required_emb_or_dst_case_insensitive(self):
        self.assertTrue(inbox.message_has_required_design_file(EXAMPLE_FILES))
        self.assertTrue(
            inbox.message_has_required_design_file(["design.Dst", "preview.png"])
        )
        self.assertFalse(
            inbox.message_has_required_design_file(
                ["PO 1670 Jigglypuff Fairway.pdf", "PO 1670 Jigglypuff Fairway.PNG"]
            )
        )

    def test_skips_signature_images_keeps_design_files(self):
        self.assertFalse(inbox.should_keep_attachment("image001.png"))
        self.assertFalse(inbox.should_keep_attachment("logo.jpg"))
        self.assertTrue(inbox.should_keep_attachment(EXAMPLE_FILES[0], "application/octet-stream"))
        self.assertTrue(inbox.should_keep_attachment(EXAMPLE_FILES[2], "application/pdf"))
        self.assertTrue(inbox.should_keep_attachment(EXAMPLE_FILES[3], "image/png"))

    def test_filename_po_conflict(self):
        conflict = inbox.attachment_po_conflict(
            ["PO 9999 wrong.EMB", "PO 1670 ok.DST"], "1670"
        )
        self.assertIsNotNone(conflict)
        self.assertIsNone(inbox.attachment_po_conflict(EXAMPLE_FILES, "1670"))


class GateAndRevisionTests(unittest.TestCase):
    def test_example_email_qualifies(self):
        gate = inbox.evaluate_message_metadata(
            from_header=VENDOR_FROM,
            subject=EXAMPLE_SUBJECT,
            attachment_filenames=EXAMPLE_FILES,
        )
        self.assertTrue(gate["qualify"])
        self.assertEqual(gate["orderNumber"], "1670")

    def test_separate_thread_subject_still_matches(self):
        # New email, not a reply in the original request thread.
        gate = inbox.evaluate_message_metadata(
            from_header=VENDOR_FROM,
            subject="PO 1670 Jigglypuff Fairway",
            attachment_filenames=["PO 1670 Jigglypuff Fairway.emb"],
        )
        self.assertTrue(gate["qualify"])

    def test_wrong_sender_skipped(self):
        gate = inbox.evaluate_message_metadata(
            from_header="Other <someone@example.com>",
            subject=EXAMPLE_SUBJECT,
            attachment_filenames=EXAMPLE_FILES,
        )
        self.assertTrue(gate["skip"])
        self.assertEqual(gate["reason"], "wrong_sender")

    def test_pdf_only_skipped(self):
        gate = inbox.evaluate_message_metadata(
            from_header=VENDOR_FROM,
            subject=EXAMPLE_SUBJECT,
            attachment_filenames=["PO 1670 Jigglypuff Fairway.pdf"],
        )
        self.assertTrue(gate["skip"])
        self.assertEqual(gate["reason"], "no_emb_or_dst")

    def test_ambiguous_subject_needs_review(self):
        gate = inbox.evaluate_message_metadata(
            from_header=VENDOR_FROM,
            subject="PO 1670 / PO 1680 files",
            attachment_filenames=EXAMPLE_FILES,
        )
        self.assertTrue(gate.get("needsReview"))
        self.assertEqual(gate["reason"], "ambiguous_po")

    def test_revision_filename(self):
        existing = {"PO 1670 Jigglypuff Fairway.EMB"}
        rev = inbox.revision_filename(existing, "PO 1670 Jigglypuff Fairway.EMB")
        self.assertEqual(rev, "PO 1670 Jigglypuff Fairway (rev 2).EMB")
        existing.add(rev)
        rev3 = inbox.revision_filename(existing, "PO 1670 Jigglypuff Fairway.EMB")
        self.assertEqual(rev3, "PO 1670 Jigglypuff Fairway (rev 3).EMB")

    def test_sha256_stable(self):
        self.assertEqual(
            inbox.sha256_bytes(b"hello"),
            inbox.sha256_bytes(b"hello"),
        )
        self.assertNotEqual(inbox.sha256_bytes(b"hello"), inbox.sha256_bytes(b"world"))


class DryRunFixtureTests(unittest.TestCase):
    """PO 1670 dry-run shape without touching Google."""

    def test_proposed_files_list(self):
        gate = inbox.evaluate_message_metadata(
            from_header=VENDOR_FROM,
            subject=EXAMPLE_SUBJECT,
            attachment_filenames=EXAMPLE_FILES,
        )
        self.assertTrue(gate["qualify"])
        proposed = {
            "orderNumber": gate["orderNumber"],
            "filenames": EXAMPLE_FILES,
            "folderId": "DRIVE_FOLDER_ID_FOR_1670",
        }
        self.assertEqual(proposed["orderNumber"], "1670")
        self.assertEqual(len(proposed["filenames"]), 4)
        self.assertTrue(any(n.lower().endswith(".emb") for n in proposed["filenames"]))
        self.assertTrue(any(n.lower().endswith(".dst") for n in proposed["filenames"]))


class OrderLockTests(unittest.TestCase):
    def test_first_import_locks_order_against_later_mail(self):
        state = inbox._default_state()
        self.assertFalse(inbox.order_is_locked(state, "1670"))
        inbox.lock_order(state, "1670", message_id="msg-first")
        self.assertTrue(inbox.order_is_locked(state, "1670"))
        # A later wrong PO reuse of 1670 must see the lock.
        self.assertTrue(inbox.order_is_locked(state, "1670.0"))

    def test_imported_message_rebuilds_lock(self):
        state = inbox._default_state()
        state["processedMessages"]["msg-1"] = {
            "status": "imported",
            "orderNumber": "1670",
            "updatedAt": "2026-03-01T12:00:00+00:00",
        }
        rebuilt = inbox._rebuild_locked_orders(state)
        self.assertIn("1670", rebuilt["lockedOrders"])
        self.assertTrue(inbox.order_is_locked(rebuilt, "1670"))

    def test_default_poll_interval_is_not_aggressive(self):
        self.assertGreaterEqual(inbox.poll_interval_seconds(), 300)


if __name__ == "__main__":
    unittest.main()
