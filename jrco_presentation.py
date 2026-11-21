"""JR Local Presentation PDF handoff (schemaVersion 1).

Presentation PDFs embed ``jrco-manifest.json`` as a file attachment. Pages are
customer proofs only; immutable flat PNGs live on Google Drive (or ``local:<sha>``
placeholders when desktop Drive upload was not configured).

A design may also name a separate print PDF (``printPdfDriveFileId``). An empty
id means that design has no print file. ``local:`` means it was never uploaded.
``printView`` is already baked into that PDF and is not used to rebuild it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from io import BytesIO
from typing import Any, Callable

logger = logging.getLogger(__name__)

MANIFEST_NAME = "jrco-manifest.json"
SCHEMA_VERSION = 1
LOCAL_ID_PREFIX = "local:"
LOCAL_PRINT_ERROR = (
    "This page’s print PDF was never uploaded (local:). "
    "Re-export after Drive upload. No print file was attached."
)
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


class JrcoPresentationError(ValueError):
    """User-facing / API error for invalid or unresolvable presentation PDFs."""


def _safe_stem(name: str) -> str:
    base = (name or "design").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    base = re.sub(r"\.pdf$", "", base, flags=re.I)
    base = re.sub(r"[^\w\-]+", "-", base).strip("-") or "design"
    return base[:80]


def is_pdf_filename(filename: str = "") -> bool:
    name = (filename or "").lower()
    return name.endswith(".pdf")


def looks_like_pdf(raw: bytes | None) -> bool:
    return bool(raw) and raw[:5] == b"%PDF-"


def extract_embedded_file(pdf_bytes: bytes, name: str) -> bytes | None:
    """Return the first embedded attachment with the given name, or None."""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise JrcoPresentationError(
            "pypdf is required to read JR presentation PDFs"
        ) from e
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        attachments = reader.attachments or {}
        contents = attachments.get(name)
        if not contents:
            # Case-insensitive fallback
            for key, vals in attachments.items():
                if str(key).lower() == name.lower() and vals:
                    return vals[0]
            return None
        return contents[0]
    except JrcoPresentationError:
        raise
    except Exception as e:
        raise JrcoPresentationError(f"Could not read PDF attachments: {e}") from e


def read_jrco_manifest(pdf_bytes: bytes) -> dict[str, Any]:
    """Parse and validate ``jrco-manifest.json`` from a presentation PDF."""
    if not looks_like_pdf(pdf_bytes):
        raise JrcoPresentationError("Not a PDF file")
    raw = extract_embedded_file(pdf_bytes, MANIFEST_NAME)
    if raw is None:
        raise JrcoPresentationError(
            "Not a JR presentation PDF (missing jrco-manifest.json attachment)"
        )
    try:
        data = json.loads(raw)
    except Exception as e:
        raise JrcoPresentationError(f"Invalid jrco-manifest.json: {e}") from e
    if not isinstance(data, dict):
        raise JrcoPresentationError("jrco-manifest.json must be a JSON object")
    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise JrcoPresentationError(
            f"Unsupported schemaVersion: {data.get('schemaVersion')}"
        )
    designs = data.get("designs")
    if not isinstance(designs, list) or not designs:
        raise JrcoPresentationError("jrco-manifest.json designs[] must be non-empty")
    for i, d in enumerate(designs):
        if not isinstance(d, dict):
            raise JrcoPresentationError(f"designs[{i}] must be an object")
        for key in ("page", "designId", "flatPngDriveFileId", "flatPngSha256"):
            if d.get(key) in (None, ""):
                raise JrcoPresentationError(f"designs[{i}] missing required field: {key}")
    return data


def is_jrco_presentation_pdf(raw: bytes | None, filename: str = "") -> bool:
    if not raw or not looks_like_pdf(raw):
        return False
    if filename and not is_pdf_filename(filename):
        return False
    try:
        read_jrco_manifest(raw)
        return True
    except JrcoPresentationError:
        return False


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def design_flat_filename(design: dict[str, Any], pdf_filename: str = "") -> str:
    """Build a stable local filename for a resolved flat PNG."""
    artwork = str(design.get("artworkVersion") or "").strip()
    product = str(design.get("productType") or "design").strip() or "design"
    design_id = str(design.get("designId") or "")[:8]
    stem = _safe_stem(f"{product}-{design_id}" if design_id else product)
    if artwork:
        return f"{stem}__{artwork}.png"
    pdf_stem = _safe_stem(pdf_filename) if pdf_filename else "presentation"
    page = design.get("page") or 1
    return f"{pdf_stem}-p{page}-flat.png"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def design_order_fields(design: dict[str, Any]) -> dict[str, Any]:
    """
    Swatch / fur fields the desktop exporter stores on each manifest design.

    Photoshop material swatches land as ``materialCategory`` + ``materialColorName``
    (and optional ``materials[]`` when more than one swatch was applied).
    Fur is ``furColorName``. Older PDFs only have the singular fields.
    """
    swatches: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add_swatch(category: str, color_name: str, name: str = "") -> None:
        category = _text(category)
        color_name = _text(color_name)
        name = _text(name)
        if not category and not color_name and not name:
            return
        key = (category.casefold(), color_name.casefold(), name.casefold())
        if key in seen:
            return
        seen.add(key)
        swatches.append(
            {"category": category, "colorName": color_name, "name": name}
        )

    raw_list = design.get("materials")
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, str):
                add_swatch("", "", item)
            elif isinstance(item, dict):
                add_swatch(
                    item.get("category") or item.get("materialCategory") or "",
                    item.get("colorName") or item.get("materialColorName") or "",
                    item.get("name") or "",
                )
    if not swatches:
        add_swatch(
            design.get("materialCategory") or "",
            design.get("materialColorName") or "",
            "",
        )

    fur_rgb = design.get("furColorRgb")
    if not (
        isinstance(fur_rgb, (list, tuple))
        and len(fur_rgb) >= 3
    ):
        fur_rgb = None
    else:
        try:
            fur_rgb = [int(fur_rgb[0]), int(fur_rgb[1]), int(fur_rgb[2])]
        except (TypeError, ValueError):
            fur_rgb = None

    design_name = _text(
        design.get("designName") or design.get("sourceName") or design.get("label")
    )
    return {
        "furColorName": _text(design.get("furColorName")),
        "furColorHex": _text(design.get("furColorHex")),
        "furColorRgb": fur_rgb,
        "materialCategory": _text(design.get("materialCategory")),
        "materialColorName": _text(design.get("materialColorName")),
        "leatherColorName": _text(design.get("leatherColorName")),
        "designName": design_name,
        "materials": swatches[:5],
    }


def classify_flat_png_id(file_id: str) -> str:
    """Return ``local``, ``drive``, or ``empty``."""
    fid = (file_id or "").strip()
    if not fid:
        return "empty"
    if fid.lower().startswith(LOCAL_ID_PREFIX):
        return "local"
    return "drive"


def order_print_pdf_filename(
    order_number: str, product_name: str, copy_index: int = 1
) -> str:
    """``{order} - Print File - {Product}.pdf``, with ``-2`` / ``-3`` before .pdf."""
    order = _ILLEGAL_FILENAME_CHARS.sub("", str(order_number or "")).strip() or "order"
    product = _ILLEGAL_FILENAME_CHARS.sub("", str(product_name or ""))
    product = re.sub(r"\s+", " ", product).strip() or "Product"
    stem = f"{order} - Print File - {product}"
    if copy_index >= 2:
        stem = f"{stem}-{copy_index}"
    return f"{stem}.pdf"


def next_print_pdf_filename(
    existing_names, order_number: str, product_name: str
) -> str:
    """Pick a Print Files name that does not overwrite an existing file."""
    taken = {str(name) for name in (existing_names or []) if str(name)}
    name = order_print_pdf_filename(order_number, product_name, 1)
    if name not in taken:
        return name
    copy_index = 2
    while True:
        candidate = order_print_pdf_filename(order_number, product_name, copy_index)
        if candidate not in taken:
            return candidate
        copy_index += 1


def resolve_print_pdf(
    file_id: str,
    expected_sha256: str,
    download_bytes: Callable[[str], bytes],
) -> bytes:
    """Download a manifest print PDF and require a SHA-256 match.

    Empty ids are not a print file. ``local:`` is an error — do not guess.
    A hash mismatch raises and must not be saved onto the order.
    """
    fid = (file_id or "").strip()
    kind = classify_flat_png_id(fid)
    if kind == "empty":
        raise JrcoPresentationError("This design has no print file.")
    if kind == "local":
        raise JrcoPresentationError(LOCAL_PRINT_ERROR)
    expected = (expected_sha256 or "").strip().lower()
    if not expected:
        raise JrcoPresentationError(
            "Print PDF is missing printPdfSha256. It was not saved."
        )
    try:
        raw = download_bytes(fid)
    except Exception as e:
        raise JrcoPresentationError(
            f"Failed to download print PDF (Drive id {fid}): {e}"
        ) from e
    if not raw:
        raise JrcoPresentationError(
            f"Empty download for print PDF (Drive id {fid})"
        )
    actual = _sha256_hex(raw).lower()
    if actual != expected:
        raise JrcoPresentationError(
            f"SHA-256 mismatch for print PDF: expected {expected}, got {actual}"
        )
    if not looks_like_pdf(raw):
        raise JrcoPresentationError(
            "Drive file for this print PDF is not a PDF. It was not saved."
        )
    return raw


def render_page_tiles(
    pdf_bytes: bytes,
    *,
    max_width: int = 280,
    jpeg_quality: int = 72,
) -> list[dict[str, Any]]:
    """
    Rasterize each PDF page to a small JPEG data-URL for the order-form tile picker.

    Uses PyMuPDF when available. Returns [] if rendering is unavailable.
    """
    if not pdf_bytes:
        return []
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("PyMuPDF not installed — page tiles unavailable")
        return []

    tiles: list[dict[str, Any]] = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        logger.warning("Could not open PDF for page tiles: %s", e)
        return []
    try:
        for i in range(len(doc)):
            page = doc.load_page(i)
            width = float(page.rect.width) or 1.0
            zoom = max(0.15, min(2.0, float(max_width) / width))
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            try:
                jpg = pix.tobytes("jpeg")
            except Exception:
                # Older PyMuPDF: convert via Pillow
                from PIL import Image

                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
                jpg = buf.getvalue()
            if not jpg:
                continue
            # Re-encode once for consistent quality / size
            try:
                from PIL import Image

                im = Image.open(BytesIO(jpg))
                if im.mode != "RGB":
                    im = im.convert("RGB")
                out = BytesIO()
                im.save(out, format="JPEG", quality=jpeg_quality, optimize=True)
                jpg = out.getvalue()
            except Exception:
                pass
            import base64

            b64 = base64.b64encode(jpg).decode("ascii")
            tiles.append(
                {
                    "page": i + 1,
                    "thumbDataUrl": f"data:image/jpeg;base64,{b64}",
                }
            )
    finally:
        doc.close()
    return tiles


def render_page_png(
    pdf_bytes: bytes,
    page_1based: int,
    *,
    max_edge: int = 2400,
) -> bytes:
    """
    Rasterize one PDF page (1-based) to a PNG suitable as a production preview.

    Used when a presentation PDF has ``local:`` flat ids (Drive not configured
    at export time) so the order form can still pick a page.
    """
    if not pdf_bytes:
        raise JrcoPresentationError("Empty PDF")
    try:
        page_n = int(page_1based)
    except (TypeError, ValueError) as e:
        raise JrcoPresentationError("Page number must be an integer") from e
    if page_n < 1:
        raise JrcoPresentationError("Page number must be >= 1")
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise JrcoPresentationError(
            "PyMuPDF is required to rasterize presentation pages"
        ) from e
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise JrcoPresentationError(f"Could not open PDF: {e}") from e
    try:
        if page_n > len(doc):
            raise JrcoPresentationError(
                f"Page {page_n} is out of range (PDF has {len(doc)} page(s))"
            )
        page = doc.load_page(page_n - 1)
        rect = page.rect
        long_edge = max(float(rect.width) or 1.0, float(rect.height) or 1.0)
        target = max(400, min(int(max_edge or 2400), 4000))
        zoom = max(0.5, min(4.0, float(target) / long_edge))
        # PNG pHYs must match the zoom. Leaving it at 72 or 96 ppi turns a
        # 2400px edge into ~33" or ~25" instead of the PDF page size.
        dpi = max(36, int(round(72.0 * zoom)))
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        try:
            png = pix.tobytes("png")
        except Exception:
            from PIL import Image

            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            buf = BytesIO()
            img.save(buf, format="PNG", optimize=True, dpi=(dpi, dpi))
            png = buf.getvalue()
        if not png:
            raise JrcoPresentationError(f"Failed to rasterize page {page_n}")
        return png
    finally:
        doc.close()


def inspect_presentation(
    pdf_bytes: bytes,
    filename: str = "",
    *,
    include_page_tiles: bool = False,
    tile_max_width: int = 280,
) -> dict[str, Any]:
    """
    Extract manifest and classify each design (no Drive download).

    Returns a JSON-serializable summary for the order UI.
    When ``include_page_tiles`` is True, also rasterizes PDF pages for the picker.
    """
    manifest = read_jrco_manifest(pdf_bytes)
    designs_out: list[dict[str, Any]] = []
    blocking_errors: list[str] = []
    for d in manifest["designs"]:
        fid = str(d.get("flatPngDriveFileId") or "").strip()
        kind = classify_flat_png_id(fid)
        print_id = str(d.get("printPdfDriveFileId") or "").strip()
        print_kind = classify_flat_png_id(print_id)
        err = None
        if kind == "local":
            err = (
                "This PDF was produced without Drive upload — re-export after "
                "Drive is configured (paths.presentationExportsFolderId), "
                "or attach the flat PNG manually."
            )
            blocking_errors.append(
                f"Page {d.get('page')}: {err}"
            )
        elif kind == "empty":
            err = "Missing flatPngDriveFileId"
            blocking_errors.append(f"Page {d.get('page')}: {err}")
        row = {
            "page": d.get("page"),
            "designId": d.get("designId"),
            "productType": d.get("productType") or "",
            "artworkVersion": d.get("artworkVersion") or "",
            "flatPngDriveFileId": fid,
            "flatPngSha256": d.get("flatPngSha256") or "",
            "sourcePsdDriveFileId": d.get("sourcePsdDriveFileId") or "",
            "idKind": kind,
            "suggestedFilename": design_flat_filename(d, filename),
            "error": err,
            "printPdfDriveFileId": print_id,
            "printPdfSha256": str(d.get("printPdfSha256") or "").strip(),
            "printView": str(d.get("printView") or "").strip(),
            "printIdKind": print_kind,
            "printError": LOCAL_PRINT_ERROR if print_kind == "local" else None,
        }
        row.update(design_order_fields(d))
        designs_out.append(row)
    page_tiles: list[dict[str, Any]] = []
    if include_page_tiles:
        try:
            page_tiles = render_page_tiles(pdf_bytes, max_width=tile_max_width)
        except Exception as e:
            logger.warning("page tile render failed: %s", e)
            page_tiles = []
    # Attach matching tile onto each design for convenient UI binding
    tiles_by_page = {
        int(t["page"]): t.get("thumbDataUrl") or ""
        for t in page_tiles
        if t.get("page") is not None
    }
    for d in designs_out:
        try:
            page_n = int(d.get("page") or 0)
        except (TypeError, ValueError):
            page_n = 0
        d["pageThumbDataUrl"] = tiles_by_page.get(page_n) or ""
    return {
        "ok": not blocking_errors,
        "schemaVersion": manifest.get("schemaVersion"),
        "presentationId": manifest.get("presentationId"),
        "designs": designs_out,
        "pageTiles": page_tiles,
        "blockingErrors": blocking_errors,
        "filename": filename or "",
    }


def _normalize_design_id_filter(
    design_ids: list[str] | set[str] | tuple[str, ...] | None,
) -> set[str] | None:
    """Return a set of designIds to keep, or None to keep all."""
    if design_ids is None:
        return None
    out = {str(x).strip() for x in design_ids if str(x).strip()}
    return out or None


def resolve_flat_pngs(
    pdf_bytes: bytes,
    filename: str,
    download_bytes: Callable[[str], bytes],
    *,
    design_ids: list[str] | set[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """
    Download and verify each design's flat PNG.

    ``download_bytes(file_id)`` must return raw file bytes from Drive.
    When ``design_ids`` is set, only those designs are resolved (order UI pick).
    Raises ``JrcoPresentationError`` on local: ids or integrity failure.
    """
    inspect = inspect_presentation(pdf_bytes, filename)
    wanted = _normalize_design_id_filter(design_ids)
    designs = list(inspect["designs"])
    if wanted is not None:
        designs = [d for d in designs if str(d.get("designId") or "") in wanted]
        if not designs:
            raise JrcoPresentationError(
                "No matching design(s) in presentation PDF for the selection"
            )
        # Only selected designs must be Drive-resolvable
        bad = [d for d in designs if d.get("idKind") != "drive"]
        if bad:
            msgs = [
                d.get("error")
                or f"Page {d.get('page')}: cannot resolve flat PNG"
                for d in bad
            ]
            raise JrcoPresentationError("; ".join(msgs))
    elif not inspect["ok"]:
        raise JrcoPresentationError(
            "; ".join(inspect["blockingErrors"])
            or "Presentation PDF cannot be resolved"
        )
    resolved: list[dict[str, Any]] = []
    for d in designs:
        fid = d["flatPngDriveFileId"]
        try:
            png_bytes = download_bytes(fid)
        except Exception as e:
            raise JrcoPresentationError(
                f"Failed to download flat PNG for page {d.get('page')} "
                f"(Drive id {fid}): {e}"
            ) from e
        if not png_bytes:
            raise JrcoPresentationError(
                f"Empty download for flat PNG page {d.get('page')} (Drive id {fid})"
            )
        actual = _sha256_hex(png_bytes)
        expected = str(d.get("flatPngSha256") or "").strip().lower()
        if expected and actual.lower() != expected:
            raise JrcoPresentationError(
                f"SHA-256 mismatch for page {d.get('page')}: "
                f"expected {expected}, got {actual}"
            )
        resolved.append(
            {
                "page": d.get("page"),
                "designId": d.get("designId"),
                "productType": d.get("productType") or "",
                "artworkVersion": d.get("artworkVersion") or "",
                "flatPngDriveFileId": fid,
                "flatPngSha256": actual,
                "filename": d["suggestedFilename"],
                "bytes": png_bytes,
                "mimetype": "image/png",
                "role": "flat_production",
            }
        )
    return resolved


def expand_presentation_uploads(
    raw: bytes,
    filename: str,
    download_bytes: Callable[[str], bytes],
    *,
    keep_proof_pdf: bool = True,
    design_ids: list[str] | set[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]] | None:
    """
    Turn a JR presentation PDF into Drive upload payloads (flat PNGs + optional proof PDF).

    Returns None if the file is not a JR presentation PDF (so callers can fall through).
    Raises ``JrcoPresentationError`` if it is a presentation but cannot be resolved.
    """
    if not looks_like_pdf(raw):
        return None
    if not is_pdf_filename(filename) and not looks_like_pdf(raw):
        return None
    # Detect without raising for ordinary PDFs
    try:
        if extract_embedded_file(raw, MANIFEST_NAME) is None:
            return None
    except JrcoPresentationError:
        return None

    flats = resolve_flat_pngs(
        raw, filename, download_bytes, design_ids=design_ids
    )
    uploads: list[dict[str, Any]] = []
    for item in flats:
        uploads.append(
            {
                "filename": item["filename"],
                "bytes": item["bytes"],
                "mimetype": "image/png",
                "role": "flat_production",
                "designId": item.get("designId"),
                "page": item.get("page"),
                "flatPngDriveFileId": item.get("flatPngDriveFileId"),
                "flatPngSha256": item.get("flatPngSha256"),
                "productType": item.get("productType"),
                "artworkVersion": item.get("artworkVersion"),
            }
        )
    if keep_proof_pdf:
        stem = _safe_stem(filename)
        uploads.append(
            {
                "filename": f"{stem}-jr-presentation-proof.pdf",
                "bytes": raw,
                "mimetype": "application/pdf",
                "role": "presentation_proof",
            }
        )
    logger.info(
        "[JR Presentation] expanded %s → %s",
        filename,
        ", ".join(u["filename"] for u in uploads),
    )
    return uploads
