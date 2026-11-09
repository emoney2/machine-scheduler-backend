"""Unit tests for JR presentation PDF → flat PNG manifest handoff."""

from __future__ import annotations

import hashlib
import json
import unittest
from io import BytesIO

from pypdf import PdfWriter

from jrco_presentation import (
    JrcoPresentationError,
    expand_presentation_uploads,
    inspect_presentation,
    is_jrco_presentation_pdf,
    read_jrco_manifest,
    resolve_flat_pngs,
)


def _make_presentation_pdf(manifest: dict) -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.add_attachment("jrco-manifest.json", json.dumps(manifest).encode("utf-8"))
    buf = BytesIO()
    w.write(buf)
    return buf.getvalue()


def _valid_manifest(**overrides):
    design = {
        "page": 1,
        "designId": "11111111-2222-3333-4444-555555555555",
        "productType": "driver",
        "artworkVersion": "deadbeef",
        "flatPngDriveFileId": "1AbCdEfGhIjKlMnOpQrStUvWxYz",
        "flatPngSha256": "a" * 64,
        "sourcePsdDriveFileId": "",
    }
    design.update(overrides.pop("design", {}))
    data = {
        "schemaVersion": 1,
        "presentationId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "designs": [design],
    }
    data.update(overrides)
    return data


class JrcoPresentationTests(unittest.TestCase):
    def test_read_manifest_ok(self):
        pdf = _make_presentation_pdf(_valid_manifest())
        data = read_jrco_manifest(pdf)
        self.assertEqual(data["schemaVersion"], 1)
        self.assertEqual(len(data["designs"]), 1)
        self.assertTrue(is_jrco_presentation_pdf(pdf, "proof.pdf"))

    def test_missing_manifest(self):
        w = PdfWriter()
        w.add_blank_page(width=100, height=100)
        buf = BytesIO()
        w.write(buf)
        with self.assertRaises(JrcoPresentationError):
            read_jrco_manifest(buf.getvalue())
        self.assertFalse(is_jrco_presentation_pdf(buf.getvalue(), "x.pdf"))

    def test_local_id_blocks_inspect(self):
        m = _valid_manifest(design={"flatPngDriveFileId": "local:" + "ab" * 32})
        pdf = _make_presentation_pdf(m)
        info = inspect_presentation(pdf, "demo.pdf")
        self.assertFalse(info["ok"])
        self.assertTrue(info["blockingErrors"])
        self.assertIn("without Drive upload", info["designs"][0]["error"])

    def test_resolve_verifies_sha(self):
        png = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes"
        digest = hashlib.sha256(png).hexdigest()
        m = _valid_manifest(
            design={
                "flatPngDriveFileId": "driveFile1",
                "flatPngSha256": digest,
            }
        )
        pdf = _make_presentation_pdf(m)
        store = {"driveFile1": png}

        resolved = resolve_flat_pngs(pdf, "demo.pdf", store.__getitem__)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["bytes"], png)
        self.assertTrue(resolved[0]["filename"].endswith(".png"))

        bad = _valid_manifest(
            design={
                "flatPngDriveFileId": "driveFile1",
                "flatPngSha256": "0" * 64,
            }
        )
        with self.assertRaises(JrcoPresentationError) as ctx:
            resolve_flat_pngs(
                _make_presentation_pdf(bad), "demo.pdf", store.__getitem__
            )
        self.assertIn("SHA-256", str(ctx.exception))

    def test_expand_uploads(self):
        png = b"\x89PNG\r\n\x1a\nflat"
        digest = hashlib.sha256(png).hexdigest()
        m = _valid_manifest(
            design={
                "flatPngDriveFileId": "fid",
                "flatPngSha256": digest,
                "productType": "blade",
                "artworkVersion": "v1",
            }
        )
        pdf = _make_presentation_pdf(m)
        uploads = expand_presentation_uploads(
            pdf, "My Proof.pdf", lambda _fid: png, keep_proof_pdf=True
        )
        self.assertIsNotNone(uploads)
        names = [u["filename"] for u in uploads]
        self.assertTrue(any(n.endswith(".png") for n in names))
        self.assertTrue(any(n.endswith("-jr-presentation-proof.pdf") for n in names))
        prod_only = expand_presentation_uploads(
            pdf, "My Proof.pdf", lambda _fid: png, keep_proof_pdf=False
        )
        prod_names = [u["filename"] for u in (prod_only or [])]
        self.assertTrue(any(n.endswith(".png") for n in prod_names))
        self.assertFalse(
            any(n.endswith("-jr-presentation-proof.pdf") for n in prod_names)
        )
        self.assertEqual(
            expand_presentation_uploads(b"not-pdf", "x.pdf", lambda f: b""),
            None,
        )

    def test_resolve_filters_selected_design(self):
        png_a = b"\x89PNG\r\n\x1a\nA"
        png_b = b"\x89PNG\r\n\x1a\nB"
        dig_a = hashlib.sha256(png_a).hexdigest()
        dig_b = hashlib.sha256(png_b).hexdigest()
        m = {
            "schemaVersion": 1,
            "presentationId": "p1",
            "designs": [
                {
                    "page": 1,
                    "designId": "design-a",
                    "productType": "driver",
                    "artworkVersion": "v1",
                    "flatPngDriveFileId": "fid-a",
                    "flatPngSha256": dig_a,
                    "sourcePsdDriveFileId": "",
                },
                {
                    "page": 2,
                    "designId": "design-b",
                    "productType": "fairway",
                    "artworkVersion": "v2",
                    "flatPngDriveFileId": "fid-b",
                    "flatPngSha256": dig_b,
                    "sourcePsdDriveFileId": "",
                },
            ],
        }
        pdf = _make_presentation_pdf(m)
        store = {"fid-a": png_a, "fid-b": png_b}
        resolved = resolve_flat_pngs(
            pdf, "demo.pdf", store.__getitem__, design_ids=["design-b"]
        )
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["designId"], "design-b")
        self.assertEqual(resolved[0]["bytes"], png_b)


if __name__ == "__main__":
    unittest.main()
