"""Unit tests for Drive image upload normalization (PDF-as-PNG guard)."""

from __future__ import annotations

import unittest
from io import BytesIO

from PIL import Image

import server


def _tiny_png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (2, 2), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


class NormalizeDriveImageUploadTests(unittest.TestCase):
    def test_real_png_passthrough(self):
        png = _tiny_png()
        name, data, mime = server._normalize_drive_image_upload(
            "headcover-flat.png", png, "image/png"
        )
        self.assertEqual(name, "headcover-flat.png")
        self.assertEqual(mime, "image/png")
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_rejects_pdf_bytes_labeled_png(self):
        pdf = b"%PDF-1.4 fake"
        with self.assertRaises(ValueError) as ctx:
            server._normalize_drive_image_upload("headcover-flat.png", pdf, "image/png")
        self.assertIn("PDF bytes", str(ctx.exception))

    def test_rejects_html_labeled_png(self):
        html = b"<!DOCTYPE html><html><body>login</body></html>"
        with self.assertRaises(ValueError) as ctx:
            server._normalize_drive_image_upload("flat.png", html, "image/png")
        self.assertIn("could not coerce", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
