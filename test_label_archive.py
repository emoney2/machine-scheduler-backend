import unittest
from datetime import datetime, timedelta, timezone

import label_archive as lblarch


def _file(fid, days_ago, now):
    ts = (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"id": fid, "modifiedTime": ts}


class LabelArchiveRetentionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)

    def test_defaults(self):
        self.assertEqual(lblarch.archive_days(""), 30)
        self.assertEqual(lblarch.archive_max(""), 400)
        self.assertEqual(lblarch.archive_days("14"), 14)
        self.assertEqual(lblarch.archive_max("80"), 80)

    def test_trashes_files_older_than_window(self):
        files = [
            _file("new", 2, self.now),
            _file("old", 45, self.now),
        ]
        trash = lblarch.files_to_trash(files, now=self.now, days=30, max_keep=400)
        self.assertEqual(trash, ["old"])

    def test_caps_revolving_count(self):
        files = [
            _file("a", 1, self.now),
            _file("b", 2, self.now),
            _file("c", 3, self.now),
        ]
        trash = lblarch.files_to_trash(files, now=self.now, days=30, max_keep=2)
        self.assertEqual(trash, ["c"])

    def test_keeps_newest_when_both_rules_apply(self):
        files = [
            _file("fresh", 1, self.now),
            _file("mid", 10, self.now),
            _file("stale", 40, self.now),
        ]
        trash = lblarch.files_to_trash(files, now=self.now, days=30, max_keep=1)
        self.assertEqual(set(trash), {"mid", "stale"})


if __name__ == "__main__":
    unittest.main()
