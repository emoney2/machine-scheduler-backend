import unittest

import reorder_batch as rb


class ReorderBatchHelpersTests(unittest.TestCase):
    def setUp(self):
        rb.reset_reorder_batches_for_tests()

    def test_normalize_order_id(self):
        self.assertEqual(rb.normalize_order_id("1842.0"), "1842")
        self.assertEqual(rb.normalize_order_id(" 99 "), "99")
        self.assertEqual(rb.normalize_order_id(""), "")

    def test_material_fields_from_row(self):
        mats, pcts = rb.material_fields_from_row(
            {
                "Material1": "Black",
                "Material2": "Red",
                "Material 2%": "25",
                "Material1%": "75",
            }
        )
        self.assertEqual(mats[0], "Black")
        self.assertEqual(mats[1], "Red")
        self.assertEqual(pcts[0], "75")
        self.assertEqual(pcts[1], "25")
        self.assertEqual(len(mats), 5)
        self.assertEqual(len(pcts), 5)

    def test_filter_skips_paired_quilted_back(self):
        jobs = [
            {"Order #": "100", "Product": "Quilted Driver Front", "Design": "A"},
            {"Order #": "101", "Product": "Quilted Driver Back", "Design": "A"},
            {"Order #": "102", "Product": "Driver Front", "Design": "B"},
            {"Order #": "103", "Product": "Driver Back", "Design": "B"},
        ]
        keep, skipped = rb.filter_selected_reorder_jobs(
            jobs, ["100", "101", "102", "103"]
        )
        kept_ids = [rb.normalize_order_id(j["Order #"]) for j in keep]
        self.assertEqual(kept_ids, ["100", "102", "103"])
        self.assertEqual(skipped[0]["sourceOrder"], "101")
        self.assertEqual(skipped[0]["status"], "skipped")

    def test_drive_folder_url_is_short(self):
        url = rb.drive_folder_url("1k0ifnmHsYqbMfYOlkSGWdG0oEjUSoJro")
        self.assertEqual(
            url, "https://drive.google.com/drive/folders/1k0ifnmHsYqbMfYOlkSGWdG0oEjUSoJro"
        )
        self.assertNotIn(",", url)

    def test_parse_job_requests_keeps_per_job_qty_and_due(self):
        reqs = rb.parse_reorder_job_requests(
            {
                "jobs": [
                    {"orderId": "10", "quantity": "25", "dueDate": "2026-10-01"},
                    {"orderId": "11", "quantity": "8", "dueDate": "2026-11-15"},
                ]
            },
            "2026-12-01",
        )
        self.assertEqual(reqs[0]["quantity"], "25")
        self.assertEqual(reqs[0]["dueDate"], "2026-10-01")
        self.assertEqual(reqs[1]["quantity"], "8")
        self.assertEqual(reqs[1]["dueDate"], "2026-11-15")

    def test_combine_notes(self):
        self.assertEqual(rb.combine_notes("old", "new"), "old\nnew")
        self.assertEqual(rb.combine_notes("old", ""), "old")
        self.assertEqual(rb.combine_notes("", "new"), "new")

    def test_batch_store_lifecycle(self):
        job = {"Order #": "55", "Design": "Logo", "Product": "Driver"}
        public = rb.create_reorder_batch(
            "Acme", [job], [], "2026-10-01", "Hard Date", "rush"
        )
        self.assertEqual(public["status"], "queued")
        self.assertEqual(public["total"], 1)
        bid = public["batchId"]
        rb.mark_reorder_batch_running(bid)
        rb.mark_reorder_item(bid, "55", status="done", newOrder=900)
        rb.mark_reorder_batch_finished(bid)
        done = rb.public_reorder_batch(bid)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["completed"], 1)
        self.assertEqual(done["items"][0]["newOrder"], 900)


if __name__ == "__main__":
    unittest.main()
