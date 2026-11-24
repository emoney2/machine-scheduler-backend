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
    design_order_fields,
    inspect_presentation,
    is_jrco_presentation_pdf,
    next_print_pdf_filename,
    order_print_pdf_filename,
    read_jrco_manifest,
    render_page_png,
    resolve_flat_pngs,
    resolve_print_pdf,
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

    def test_inspect_passes_photoshop_swatch_fields(self):
        m = _valid_manifest(
            design={
                "furColorName": "Brown Fur",
                "furColorHex": "#382424",
                "furColorRgb": [56, 36, 36],
                "materialCategory": "Ostrich Vinyl",
                "materialColorName": "Coffee",
                "leatherColorName": "",
                "productType": "mallet",
            }
        )
        info = inspect_presentation(
            _make_presentation_pdf(m),
            "Atlanta Athletic Club 2026-11-11.pdf",
        )
        d = info["designs"][0]
        self.assertEqual(d["furColorName"], "Brown Fur")
        self.assertEqual(d["furColorHex"], "#382424")
        self.assertEqual(d["furColorRgb"], [56, 36, 36])
        self.assertEqual(d["materialCategory"], "Ostrich Vinyl")
        self.assertEqual(d["materialColorName"], "Coffee")
        self.assertEqual(d["leatherColorName"], "")
        self.assertEqual(
            d["materials"],
            [{"category": "Ostrich Vinyl", "colorName": "Coffee", "name": ""}],
        )
        self.assertEqual(d["designName"], "")

    def test_order_fields_prefer_materials_list(self):
        fields = design_order_fields(
            {
                "materialCategory": "Ignored",
                "materialColorName": "Ignored",
                "furColorName": "Navy Blue Fur",
                "designName": "patternlogos",
                "materials": [
                    {"category": "Cheyenne", "colorName": "Long Rifle"},
                    {"category": "Wow", "colorName": "Black"},
                    "Black Fur",
                ],
            }
        )
        self.assertEqual(fields["designName"], "patternlogos")
        self.assertEqual(fields["furColorName"], "Navy Blue Fur")
        self.assertEqual(
            [m["colorName"] or m["name"] for m in fields["materials"]],
            ["Long Rifle", "Black", "Black Fur"],
        )

    def test_render_page_keeps_pdf_page_inches(self):
        import fitz
        from PIL import Image

        width_in = 16.615
        doc = fitz.open()
        doc.new_page(width=width_in * 72, height=10 * 72)
        png = render_page_png(doc.tobytes(), 1, max_edge=2400)
        im = Image.open(BytesIO(png))
        dpi = im.info.get("dpi")
        self.assertIsNotNone(dpi)
        self.assertAlmostEqual(im.size[0] / float(dpi[0]), width_in, places=2)
        # 2400px at 72 ppi is the 33.33" mis-size this must not produce.
        self.assertLess(im.size[0] / float(dpi[0]), 20)

    def test_inspect_print_pdf_fields(self):
        m = _valid_manifest(
            design={
                "printPdfDriveFileId": "print123",
                "printPdfSha256": "b" * 64,
                "printView": "front only",
                "designName": "Logo",
            }
        )
        info = inspect_presentation(_make_presentation_pdf(m), "x.pdf")
        design = info["designs"][0]
        self.assertTrue(info["ok"])
        self.assertEqual(design["printIdKind"], "drive")
        self.assertEqual(design["printPdfDriveFileId"], "print123")
        self.assertEqual(design["printPdfSha256"], "b" * 64)
        self.assertEqual(design["printView"], "front only")
        self.assertIsNone(design["printError"])
        self.assertEqual(design["designName"], "Logo")

    def test_empty_print_id_is_not_an_error(self):
        info = inspect_presentation(_make_presentation_pdf(_valid_manifest()), "x.pdf")
        design = info["designs"][0]
        self.assertTrue(info["ok"])
        self.assertEqual(design["printIdKind"], "empty")
        self.assertEqual(design["printPdfDriveFileId"], "")
        self.assertIsNone(design["printError"])

    def test_local_print_id_does_not_block_the_flat(self):
        m = _valid_manifest(design={"printPdfDriveFileId": "local:" + "ab" * 32})
        info = inspect_presentation(_make_presentation_pdf(m), "x.pdf")
        self.assertTrue(info["ok"])
        self.assertEqual(info["designs"][0]["printIdKind"], "local")
        self.assertIn("never uploaded", info["designs"][0]["printError"])

    def test_print_filename_uses_manifest_product_type(self):
        self.assertEqual(
            order_print_pdf_filename("1640", "blade"),
            "1640 - Print File - BladeBlackOutline.pdf",
        )
        self.assertEqual(
            order_print_pdf_filename("1640", "center_shaft_mallet"),
            "1640 - Print File - CenterShaftMalletTBlackOutline.pdf",
        )
        existing = {"1640 - Print File - FairwayBlackOutline.pdf"}
        self.assertEqual(
            next_print_pdf_filename(existing, "1640", "fairway"),
            "1640 - Print File - FairwayBlackOutline-2.pdf",
        )
        with self.assertRaises(JrcoPresentationError):
            order_print_pdf_filename("1640", "driver")
        with self.assertRaises(JrcoPresentationError):
            order_print_pdf_filename("1640", "Driver Front")

    def test_resolve_print_pdf_verifies_sha_and_rejects_local(self):
        pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
        digest = hashlib.sha256(pdf).hexdigest()
        self.assertEqual(
            resolve_print_pdf("print-id", digest, lambda _fid: pdf),
            pdf,
        )
        with self.assertRaises(JrcoPresentationError) as bad_hash:
            resolve_print_pdf("print-id", "0" * 64, lambda _fid: pdf)
        self.assertIn("SHA-256", str(bad_hash.exception))
        with self.assertRaises(JrcoPresentationError) as local:
            resolve_print_pdf("local:abc", digest, lambda _fid: pdf)
        self.assertIn("never uploaded", str(local.exception))
        with self.assertRaises(JrcoPresentationError):
            resolve_print_pdf("", digest, lambda _fid: pdf)


if __name__ == "__main__":
    unittest.main()
