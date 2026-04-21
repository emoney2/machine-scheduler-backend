# ups_service.py
import base64, io, logging, os, re, shutil, sys, time, uuid, tempfile, json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple
from zoneinfo import ZoneInfo
import requests

from ship_qbo_file_log import log_label

# Note: read ID/secret inside get_access_token() so they pick up .env after load_dotenv() in server.py.
SHIPPER_NUMBER = os.getenv("UPS_ACCOUNT_NUMBER", "")
NEGOTIATED = (os.getenv("UPS_NEGOTIATED_RATES", "true").lower() == "true")

# Default folder synced by Google Drive for desktop (Windows): G:\My Drive\Label Printer
_DEFAULT_UPS_LABEL_OUTPUT_DIR = r"G:\My Drive\Label Printer"

# Ship-from defaults (override any field with SHIP_FROM_* in .env)
_a2 = (os.getenv("SHIP_FROM_ADDRESS2", "Suite 300") or "").strip()
FROM = {
    "name":   os.getenv("SHIP_FROM_NAME", "JR & Co."),
    "phone":  os.getenv("SHIP_FROM_PHONE", "0000000000"),
    "addr1":  os.getenv("SHIP_FROM_ADDRESS1", "1384 Buford Business Blvd"),
    "addr2":  _a2 or None,
    "city":   os.getenv("SHIP_FROM_CITY", "Buford"),
    "state":  os.getenv("SHIP_FROM_STATE", "GA"),
    "zip":    os.getenv("SHIP_FROM_ZIP", "30518"),
    "country":os.getenv("SHIP_FROM_COUNTRY", "US"),
}

UNITS = (os.getenv("UPS_UNITS", "IN_LBS").upper())
DIM_UNIT = "IN" if UNITS == "IN_LBS" else "CM"
WT_UNIT  = "LBS" if UNITS == "IN_LBS" else "KGS"

PICKUP_TYPE = os.getenv("UPS_PICKUP_TYPE", "DailyPickup")
LABEL_FORMAT = os.getenv("UPS_LABEL_FORMAT", "PDF").upper()
LABEL_SIZE = os.getenv("UPS_LABEL_SIZE", "4x6")

# "Rate" = price only (Ground often omits TimeInTransit). "Ratetimeintransit" = rates + transit (needed for Ground ETA).
_RATING_REQ_OPT = (os.getenv("UPS_RATING_REQUEST_OPTION") or "Ratetimeintransit").strip() or "Rate"


def _pickup_date_ymd() -> str:
    """YYYYMMDD for Rating API; required for transit with Ratetimeintransit."""
    return datetime.now().strftime("%Y%m%d")


def _ups_base_url() -> str:
    """Production: onlinetools.ups.com. Sandbox: wwwcie.ups.com. Set UPS_ENV=production for live."""
    env = (os.getenv("UPS_ENV") or "sandbox").lower()
    return (
        "https://onlinetools.ups.com"
        if env == "production"
        else "https://wwwcie.ups.com"
    )


def _ups_ship_endpoint() -> str:
    """
    Create-shipment URL. UPS returns 404 for removed paths like /api/shipments/v1/shipments.
    Current REST shape: POST /api/shipments/{version}/ship (see UPS Shipping.yaml).
    Override with UPS_SHIP_API_VERSION (default v2409).
    """
    ver = (os.getenv("UPS_SHIP_API_VERSION") or "v2409").strip()
    if ver and not ver.startswith("v"):
        ver = f"v{ver}"
    return f"{_ups_base_url()}/api/shipments/{ver}/ship"


def _ups_error_snippet(resp: requests.Response) -> str:
    t = (resp.text or "").strip()
    if t:
        return t[:800] + ("…" if len(t) > 800 else "")
    return (resp.reason or "").strip() or "empty response body"


def _qv_format_http_error_response(resp: requests.Response) -> str:
    """
    Quantum View often returns 400 with JSON:
    {"response":{"errors":[{"code":"330052","message":"..."}]}}
    """
    try:
        j = resp.json()
    except Exception:
        return _ups_error_snippet(resp)
    err_list = None
    if isinstance(j, dict):
        r = j.get("response")
        if isinstance(r, dict):
            err_list = r.get("errors")
        if err_list is None:
            err_list = j.get("errors")
    if not isinstance(err_list, list) or not err_list:
        return _ups_error_snippet(resp)
    lines: List[str] = []
    codes_seen: List[str] = []
    for e in err_list:
        if not isinstance(e, dict):
            continue
        code = str(e.get("code") or "").strip()
        msg = str(e.get("message") or "").strip()
        if code:
            codes_seen.append(code)
        if code and msg:
            lines.append(f"[{code}] {msg}")
        elif msg:
            lines.append(msg)
    if not lines:
        return _ups_error_snippet(resp)
    out = " ".join(lines)
    if "330052" in codes_seen:
        out += (
            " — What to do: UPS is blocking Quantum View **file downloads** until the right subscription "
            "statuses are active. On **ups.com** (logged in as the shipping admin), open **Quantum View** "
            "management / subscription services and enable **Quantum View Data** (outbound) for the "
            "**company** and for **your user** (UPS maps those to CompanyQVD / UserQVD). The message also "
            "mentions Inbound; if you only ship outbound, inbound can stay off, but outbound QVD must be "
            "active (and any required Quantum View product must be on your UPS invoice). If you do not see "
            "those options, contact **UPS account support** and ask them to activate Quantum View Data for "
            "your shipper account."
        )
    return out


def _trans_id_header() -> str:
    """UPS transId header max length 32 (UUID string is 36 with hyphens)."""
    return uuid.uuid4().hex


# Known service codes you likely want visible; we'll loop to “shop” rates
UPS_SERVICES = [
    ("01", "Next Day Air"),
    ("14", "Next Day Air Early"),
    ("13", "Next Day Air Saver"),
    ("02", "2nd Day Air"),
    ("12", "3 Day Select"),
    ("03", "Ground"),
]

# ------- OAuth token cache -------
_token_cache: Dict[str, Any] = {"access_token": None, "exp": 0}

def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()

def get_access_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and _token_cache["exp"] - 60 > now:
        return _token_cache["access_token"]

    client_id = os.getenv("UPS_CLIENT_ID", "").strip()
    client_secret = os.getenv("UPS_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "UPS OAuth: set UPS_CLIENT_ID and UPS_CLIENT_SECRET in the environment or backend .env"
        )

    url = f"{_ups_base_url()}/security/v1/oauth/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": "Basic " + _b64(f"{client_id}:{client_secret}"),
    }
    data = "grant_type=client_credentials"
    r = requests.post(url, headers=headers, data=data, timeout=20)
    r.raise_for_status()
    js = r.json()
    _token_cache["access_token"] = js["access_token"]
    _token_cache["exp"] = now + int(js.get("expires_in", 3300))
    return _token_cache["access_token"]

# ------- Helpers to build UPS address/package JSON -------
def _addr(
    name: str,
    phone: str,
    a1: str,
    city: str,
    state: str,
    postal: str,
    country: str,
    a2: str | None = None,
    attention_name: str | None = None,
) -> Dict[str, Any]:
    """
    UPS ShipTo/ShipFrom: Name = company (or person if no company); AttentionName = contact person.
    Omitting AttentionName can make the label show odd lines (e.g. phone prominence).
    """
    out = {
        "Name": name[:35] if name else "Recipient",
        "Phone": {"Number": phone or "0000000000"},
        "Address": {
            "AddressLine": [a1] if not a2 else [a1, a2],
            "City": city, "StateProvinceCode": state,
            "PostalCode": postal, "CountryCode": country
        },
    }
    attn = (attention_name or "").strip()
    if attn:
        out["AttentionName"] = attn[:35]
    return out

def _shipper() -> Dict[str, Any]:
    return {
        "Name": FROM["name"][:35],
        "ShipperNumber": SHIPPER_NUMBER,
        "Phone": {"Number": FROM["phone"]},
        "Address": {
            "AddressLine": [FROM["addr1"]] if not FROM["addr2"] else [FROM["addr1"], FROM["addr2"]],
            "City": FROM["city"], "StateProvinceCode": FROM["state"],
            "PostalCode": FROM["zip"], "CountryCode": FROM["country"]
        }
    }

def _ship_from() -> Dict[str, Any]:
    return _addr(FROM["name"], FROM["phone"], FROM["addr1"], FROM["city"], FROM["state"], FROM["zip"], FROM["country"], FROM["addr2"])

def _pkg(dim: Dict[str, Any], weight_lbs: float|int) -> Dict[str, Any]:
    """Package node for Rating API (expects PackagingType)."""
    L, W, H = str(dim["L"]), str(dim["W"]), str(dim["H"])
    WGT = f"{float(weight_lbs):.2f}"
    return {
        "PackagingType": {"Code": "02"},  # Customer Supplied Package
        "Dimensions": {
            "UnitOfMeasurement": {"Code": DIM_UNIT},
            "Length": L, "Width": W, "Height": H
        },
        "PackageWeight": {
            "UnitOfMeasurement": {"Code": WT_UNIT},
            "Weight": WGT
        }
    }


def _pkg_ship(dim: Dict[str, Any], weight_lbs: float|int) -> Dict[str, Any]:
    """
    Package node for Ship API v2409+ (Shipping.yaml Shipment_Package).
    Requires `Packaging` with `Code`, not `PackagingType` — otherwise 120600.
    """
    L, W, H = str(dim["L"]), str(dim["W"]), str(dim["H"])
    WGT = f"{float(weight_lbs):.2f}"
    return {
        "Packaging": {"Code": "02"},
        "Dimensions": {
            "UnitOfMeasurement": {"Code": DIM_UNIT},
            "Length": L, "Width": W, "Height": H
        },
        "PackageWeight": {
            "UnitOfMeasurement": {"Code": WT_UNIT},
            "Weight": WGT
        }
    }

def _label_stock_size_for_request() -> Dict[str, str]:
    """
    UPS ShipmentRequest LabelSpecification requires LabelStockSize (see Shipping.yaml).
    Do not send null — a missing stock size can yield Success + tracking but no ShippingLabel.
    For 4×6 thermal stock: Width=4, Height=6 (YAML: width valid 4; height 6 or 8).
    """
    raw = (LABEL_SIZE or "4x6").strip().lower()
    raw = re.sub(r"\s+", "", raw)
    if raw in ("4x8", "4x8in"):
        return {"Height": "8", "Width": "4"}
    return {"Height": "6", "Width": "4"}


def _label_spec() -> Dict[str, Any]:
    # PDF recommended for your print flow
    img_code = "PDF" if LABEL_FORMAT == "PDF" else ("PNG" if LABEL_FORMAT == "PNG" else "ZPL")
    return {
        "LabelImageFormat": {"Code": img_code},
        "HTTPUserAgent": "JRCO-App",
        "LabelStockSize": _label_stock_size_for_request(),
    }


def _label_printer_output_dir() -> str:
    """Google Drive sync folder for the label printer watcher (override with UPS_LABEL_OUTPUT_DIR)."""
    return (os.getenv("UPS_LABEL_OUTPUT_DIR") or _DEFAULT_UPS_LABEL_OUTPUT_DIR).strip()


def _label_output_path_usable_on_this_host(out_dir: str) -> bool:
    """
    On Render/Linux, a default like G:\\My Drive\\Label Printer is not a real mount.
    os.makedirs/copy2 can still "succeed" under a bogus relative path, which hides labels
    from users and disables browser fallback. Skip copy unless the path makes sense here.
    Set UPS_LABEL_OUTPUT_DIR to a POSIX absolute path if the cloud host has that mount.
    """
    if not out_dir or not str(out_dir).strip():
        return False
    s = str(out_dir).strip()
    if sys.platform == "win32":
        return True
    if re.match(r"^[A-Za-z]:[/\\]", s) or s.startswith("\\\\"):
        logging.info(
            "UPS label folder skipped on non-Windows host (Windows-only path): %s",
            s,
        )
        return False
    if s.startswith("/"):
        return True
    logging.info(
        "UPS label folder skipped on non-Windows host (set UPS_LABEL_OUTPUT_DIR to an absolute POSIX path): %s",
        s,
    )
    return False


def _save_ups_label_api_raw_copy(tracking: str, ext: str, raw_bytes: bytes) -> None:
    """
    Always save undecorated label bytes from UPS (before trim) for troubleshooting.

    File: ups_<safe_tracking>_api_raw.<ext> in the system temp directory (served as /labels/...).
    Set UPS_LABEL_DEBUG_DIR to also copy the raw file to that folder.
    """
    safe_trk = re.sub(r"[^\w.\-]+", "_", str(tracking or "unknown"))[:120]
    raw_name = f"ups_{safe_trk}_api_raw.{ext}"
    tmp_dir = tempfile.gettempdir()
    raw_path = os.path.join(tmp_dir, raw_name)
    try:
        with open(raw_path, "wb") as f:
            f.write(raw_bytes)
        log_label(
            "label_api_raw_saved",
            tracking=str(tracking or ""),
            path_basename=raw_name,
            byte_len=len(raw_bytes or b""),
        )
    except OSError as e:
        logging.warning("UPS label raw copy: failed writing %s: %s", raw_path, e)
        return
    dbg_dir = (os.getenv("UPS_LABEL_DEBUG_DIR") or "").strip()
    if not dbg_dir:
        return
    try:
        os.makedirs(dbg_dir, exist_ok=True)
        shutil.copy2(raw_path, os.path.join(dbg_dir, raw_name))
        logging.info("UPS label raw copy: also wrote %s", os.path.join(dbg_dir, raw_name))
    except OSError as e:
        logging.warning("UPS_LABEL_DEBUG_DIR copy failed: %s", e)


def _repo_label_debug_dir() -> str:
    """`<repo>/debug/shipping_labels` — repo root is parent of `backend/`."""
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "debug", "shipping_labels")
    )


def _ups_label_repo_debug_copies_enabled() -> bool:
    raw = os.getenv("UPS_LABEL_REPO_DEBUG_COPIES")
    if raw is None or not str(raw).strip():
        return True
    v = str(raw).strip().lower()
    return v not in ("0", "false", "no", "off")


def _save_ups_label_repo_pre_post_crop(
    tracking: str, ext: str, pre_bytes: bytes, post_bytes: bytes
) -> None:
    """
    Write two files next to the codebase for before/after crop comparison:
      debug/shipping_labels/<ts>_<tracking>_PRE_CROP.pdf
      debug/shipping_labels/<ts>_<tracking>_POST_CROP.pdf

    Only used for PDF (crop is PDF-specific). Disable with UPS_LABEL_REPO_DEBUG_COPIES=0
    (recommended on cloud hosts).
    """
    if ext.lower() != "pdf" or not pre_bytes:
        return
    if not _ups_label_repo_debug_copies_enabled():
        return
    out_dir = _repo_label_debug_dir()
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        logging.warning("UPS repo label debug: mkdir %s: %s", out_dir, e)
        return
    safe_trk = re.sub(r"[^\w.\-]+", "_", str(tracking or "unknown"))[:80]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    pre_name = f"{ts}_{safe_trk}_PRE_CROP.pdf"
    post_name = f"{ts}_{safe_trk}_POST_CROP.pdf"
    pre_path = os.path.join(out_dir, pre_name)
    post_path = os.path.join(out_dir, post_name)
    try:
        with open(pre_path, "wb") as f:
            f.write(pre_bytes)
        with open(post_path, "wb") as f:
            f.write(post_bytes or b"")
        logging.info(
            "UPS label repo debug: wrote pre/post crop — %s | %s",
            pre_path,
            post_path,
        )
        # Same filenames under system temp (always writable; e.g. cloud hosts with no repo mount).
        try:
            alt_dir = os.path.join(tempfile.gettempdir(), "ups_label_compare")
            os.makedirs(alt_dir, exist_ok=True)
            apre = os.path.join(alt_dir, pre_name)
            apost = os.path.join(alt_dir, post_name)
            with open(apre, "wb") as f:
                f.write(pre_bytes)
            with open(apost, "wb") as f:
                f.write(post_bytes or b"")
            logging.info("UPS label compare (temp): %s | %s", apre, apost)
        except OSError as ex2:
            logging.warning("UPS label temp compare copy failed: %s", ex2)
        log_label(
            "label_repo_debug_pre_post_saved",
            tracking=str(tracking or ""),
            pre_basename=pre_name,
            post_basename=post_name,
            pre_bytes=len(pre_bytes),
            post_bytes=len(post_bytes or b""),
        )
    except OSError as e:
        logging.warning("UPS repo label debug: write failed: %s", e)


def _save_trimmed_label_to_printer_folder(fpath: str, fname: str) -> bool:
    """
    After the PDF is trimmed, copy it into UPS_LABEL_OUTPUT_DIR (default G:\\My Drive\\Label Printer).
    On Linux cloud hosts the default Windows path is skipped so the API can return open_label_windows=true.
    """
    out_dir = _label_printer_output_dir()
    if not out_dir:
        return False
    if not _label_output_path_usable_on_this_host(out_dir):
        return False
    try:
        os.makedirs(out_dir, exist_ok=True)
        dest = os.path.join(out_dir, fname)
        shutil.copy2(fpath, dest)
        logging.info("UPS label copied to label output folder: %s", dest)
        return True
    except OSError as e:
        logging.warning(
            "Failed to save UPS label to Label Printer folder %s: %s",
            out_dir,
            e,
        )
        return False


# Phrases UPS puts on "how to print" / instruction pages (not on the thermal label itself).
_LABEL_INSTRUX_PATTERNS = re.compile(
    r"print\s+this\s+page|how\s+to\s+print|view\s+and\s+print|view\s+instructions|"
    r"fold\s+(along|here)|"
    r"shipping\s+label\s+instructions|packing\s+list(\s+included)?|"
    r"adobe\s+acrobat|adobe\s+reader|download\s+the\s+label|"
    r"laser\s+printer|inkjet|cut\s+along|do\s+not\s+scale|actual\s+size|"
    r"^\s*instructions\s*$",
    re.I | re.MULTILINE,
)


def _normalize_ups_label_pdf_bytes(raw_pdf: bytes, expected_tracking: str | None = None) -> bytes:
    """
    UPS often returns (a) a multi-page PDF with instructions after (or before) the label, or
    (b) one US Letter page with the scannable label on top and fold/instructions below.

    We output a single page with label only (no instruction sheets):
      - Score each page: prefer 4×6-ish dimensions, tracking # in text, avoid instruction copy.
      - If that page is US Letter, crop to the top band (actual label); default crop is
        aggressive so "view/print" blocks below the barcode are removed.

    Set UPS_LABEL_PDF_TRIM_MODE=off to disable trimming (not recommended for web printing).
    UPS_LABEL_LETTER_TOP_FRACTION (default 0.40) = fraction of page height kept from the top.
    """
    mode = (os.getenv("UPS_LABEL_PDF_TRIM_MODE") or "auto").strip().lower()
    if mode == "off" or not raw_pdf:
        log_label(
            "pdf_normalize_skipped",
            reason="trim_off_or_empty" if raw_pdf else "empty_pdf",
            ups_label_pdf_trim_mode=mode,
            expected_tracking=expected_tracking,
            raw_bytes=len(raw_pdf or b""),
        )
        return raw_pdf
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import RectangleObject
    except ImportError:
        log_label(
            "pdf_normalize_skipped",
            reason="pypdf_missing",
            expected_tracking=expected_tracking,
        )
        return raw_pdf

    def _page_wh(page):
        mb = page.mediabox
        left, bottom, right, top = (
            float(mb.left),
            float(mb.bottom),
            float(mb.right),
            float(mb.top),
        )
        w, h = right - left, top - bottom
        return w, h, left, bottom, right, top

    def _looks_like_thermal_4x6(w: float, h: float) -> bool:
        """UPS thermal 4×6 ≈ 288×432 pt at 72 dpi; allow scaled PDFs."""
        if w <= 0 or h <= 0:
            return False
        r = h / w
        # portrait 6:4
        if 1.2 <= r <= 1.75 and w <= 420 and h <= 700:
            return True
        return False

    def _looks_like_us_letter(w: float, h: float) -> bool:
        return h >= 700 and 580 <= w <= 640 and h / max(w, 1) > 1.15

    def _crop_us_letter_label_only(page):
        """If page is tall US Letter with label in upper block, remove instructions below."""
        w, h, left, bottom, right, top = _page_wh(page)
        if not _looks_like_us_letter(w, h):
            return
        # Default a bit tighter than before so folded / print-help text is excluded.
        frac = float(os.getenv("UPS_LABEL_LETTER_TOP_FRACTION") or "0.40")
        frac = min(max(frac, 0.22), 0.75)
        new_bottom = bottom + h * (1.0 - frac)
        rect = RectangleObject([left, new_bottom, right, top])
        page.mediabox = rect
        page.cropbox = rect

    def _crop_us_letter_keep_bottom_band(page):
        """US Letter where the scannable label is in the lower portion; strip instruction header."""
        w, h, left, bottom, right, top = _page_wh(page)
        if not _looks_like_us_letter(w, h):
            return
        frac = float(os.getenv("UPS_LABEL_LETTER_BOTTOM_FRACTION") or "0.48")
        frac = min(max(frac, 0.22), 0.78)
        new_top = bottom + h * frac
        rect = RectangleObject([left, bottom, right, new_top])
        page.mediabox = rect
        page.cropbox = rect

    def _letter_label_likely_lower_half(p, trk: str) -> bool:
        """
        When UPS puts instructions at the top of one US Letter page and the label below,
        tracking text often appears only in the lower half of extracted lines.
        """
        wanted = re.sub(r"[^A-Za-z0-9]", "", str(trk or "")).upper()
        if len(wanted) < 10:
            return False
        try:
            lines = [ln for ln in (p.extract_text() or "").splitlines() if ln.strip()]
        except Exception:
            return False
        if len(lines) < 8:
            return False
        mid = max(1, len(lines) // 2)
        upper = "\n".join(lines[:mid])
        lower = "\n".join(lines[mid:])
        nu = re.sub(r"[^A-Za-z0-9]", "", upper).upper()
        nl = re.sub(r"[^A-Za-z0-9]", "", lower).upper()
        return wanted in nl and wanted not in nu

    def _prefer_bottom_letter_band(p, instrux_hits: int) -> bool:
        """
        When UPS puts 'how to print' copy above the label on one Letter page, extracted text order
        often lists instructions first; tracking appears only in the lower half.
        """
        if instrux_hits < 1:
            return False
        wanted = re.sub(r"[^A-Za-z0-9]", "", str(expected_tracking or "")).upper()
        if not wanted or len(wanted) < 8:
            return False
        try:
            txt = p.extract_text() or ""
        except Exception:
            return False
        if len(txt) < 100:
            return False
        mid = len(txt) // 2
        nrm = lambda s: re.sub(r"[^A-Za-z0-9]", "", s).upper()
        tail_has = wanted in nrm(txt[mid:])
        head_has = wanted in nrm(txt[:mid])
        # At least one instruction phrase plus tracking only in the lower half → label is below.
        return bool(tail_has and not head_has)

    def _ups_letter_instructions_dominate_top(p) -> bool:
        """
        Letter PDFs often stack UPS help copy in the upper ~40% and the scannable label below.
        If instruction phrases cluster in the top lines only, a top-band crop prints help text only.
        """
        try:
            txt = p.extract_text() or ""
        except Exception:
            return False
        lines = [ln for ln in txt.splitlines() if ln.strip()]
        if len(lines) < 5:
            return False
        top_line_cut = max(2, int(len(lines) * 0.42) + 1)
        top = "\n".join(lines[:top_line_cut])
        rest = "\n".join(lines[top_line_cut:])
        hits_top = len(_LABEL_INSTRUX_PATTERNS.findall(top))
        hits_rest = len(_LABEL_INSTRUX_PATTERNS.findall(rest))
        # Allow one stray phrase on the label (e.g. "do not scale") in the lower band.
        if hits_top >= 2 and hits_rest <= 1:
            return True
        if hits_top >= 1 and not rest.strip():
            return False
        if hits_top >= 1 and hits_rest == 0 and len(top) >= 100:
            return True
        return False

    def _tracking_first_line_in_lower_portion(p, trk: str) -> bool:
        """True when the tracking # first appears well below the instruction header block."""
        wanted = re.sub(r"[^A-Za-z0-9]", "", str(trk or "")).upper()
        if len(wanted) < 10:
            return False
        try:
            lines = [ln for ln in (p.extract_text() or "").splitlines() if ln.strip()]
        except Exception:
            return False
        if len(lines) < 5:
            return False
        nrm_ln = lambda s: re.sub(r"[^A-Za-z0-9]", "", s).upper()
        for i, ln in enumerate(lines):
            if wanted in nrm_ln(ln):
                return i >= max(2, int(len(lines) * 0.25))
        return False

    def _page_label_score(idx: int, p) -> float:
        w, h, *_ = _page_wh(p)
        score = 0.0
        try:
            txt = p.extract_text() or ""
        except Exception:
            txt = ""
        low = txt.lower()
        wanted = re.sub(r"[^A-Za-z0-9]", "", str(expected_tracking or "")).upper()
        if wanted:
            normalized = re.sub(r"[^A-Za-z0-9]", "", txt).upper()
            if wanted in normalized:
                score += 120.0
        # Dedicated thermal page (no letter-sized instruction sheet)
        if _looks_like_thermal_4x6(w, h):
            score += 80.0
        # Slightly prefer narrower pages (label) over full letter when tracking missing
        if w > 0 and w < 400:
            score += 25.0
        # Penalize obvious instruction-only pages (stronger so Letter instruction sheets lose to labels)
        hits = len(_LABEL_INSTRUX_PATTERNS.findall(txt))
        if hits:
            score -= min(55.0 + 35.0 * hits, 200.0)
        # Letter-sized page with lots of prose, no tracking → likely instructions
        if _looks_like_us_letter(w, h) and len(txt) > 400 and wanted and wanted not in re.sub(
            r"[^A-Za-z0-9]", "", txt
        ).upper():
            score -= 60.0
        # Prefer earlier pages when UPS puts label first (common)
        score -= idx * 3.0
        return score

    try:
        reader = PdfReader(io.BytesIO(raw_pdf))
        n = len(reader.pages)
        if n == 0:
            log_label(
                "pdf_normalize_no_pages",
                expected_tracking=expected_tracking,
                raw_bytes=len(raw_pdf),
            )
            return raw_pdf

        def _dims_at(i: int):
            w, h, *_ = _page_wh(reader.pages[i])
            return w, h

        thermal_pages = [i for i in range(n) if _looks_like_thermal_4x6(*_dims_at(i))]
        if thermal_pages:
            # Prefer real 4×6 label pages over separate instruction PDF pages.
            best_idx = max(
                thermal_pages, key=lambda i: _page_label_score(i, reader.pages[i])
            )
        elif n > 1:
            scores = [_page_label_score(i, reader.pages[i]) for i in range(n)]
            best_idx = max(range(n), key=lambda i: scores[i])
        else:
            best_idx = 0

        per_page = []
        try:
            for i in range(n):
                w_i, h_i = _dims_at(i)
                sc_i = _page_label_score(i, reader.pages[i])
                prev = ""
                try:
                    prev = (reader.pages[i].extract_text() or "").replace("\n", " ")
                    prev = prev.strip()[:220]
                except Exception:
                    prev = ""
                per_page.append(
                    {
                        "page": i,
                        "w_pt": round(w_i, 1),
                        "h_pt": round(h_i, 1),
                        "score": round(sc_i, 2),
                        "text_preview": prev,
                    }
                )
            log_label(
                "pdf_page_pick",
                expected_tracking=expected_tracking,
                page_count=n,
                best_page_index=best_idx,
                thermal_page_indexes=thermal_pages,
                ups_label_pdf_trim_mode=mode,
                per_page=per_page,
            )
        except Exception as ex:
            log_label(
                "pdf_page_pick_log_failed",
                expected_tracking=expected_tracking,
                error=str(ex),
            )

        src_page = reader.pages[best_idx]
        try:
            instrux_hits = len(
                _LABEL_INSTRUX_PATTERNS.findall(src_page.extract_text() or "")
            )
        except Exception:
            instrux_hits = 0

        writer = PdfWriter()
        letter_crop = None
        score_top = None
        score_bot = None
        w_let, h_let = _dims_at(best_idx)
        # US Letter: build BOTH top- and bottom-band crops, score extracted text (tracking + digits
        # vs instruction phrases), and keep the winner. Heuristics alone often kept the wrong band.
        if mode in ("auto", "letter_crop") and _looks_like_us_letter(w_let, h_let):

            def _clone_letter_page():
                bio = io.BytesIO()
                tw = PdfWriter()
                tw.add_page(reader.pages[best_idx])
                tw.write(bio)
                bio.seek(0)
                return PdfReader(bio).pages[0]

            def _one_page_pdf_bytes(crop_apply, pg):
                cw = PdfWriter()
                cw.add_page(pg)
                p0 = cw.pages[0]
                crop_apply(p0)
                ob = io.BytesIO()
                cw.write(ob)
                return ob.getvalue()

            def _score_letter_bytes(pdf_b: bytes, trk):
                try:
                    rr = PdfReader(io.BytesIO(pdf_b))
                    if not rr.pages:
                        return -1e9
                    t = rr.pages[0].extract_text() or ""
                except Exception:
                    return -1e9
                wanted = re.sub(r"[^A-Za-z0-9]", "", str(trk or "")).upper()
                norm = re.sub(r"[^A-Za-z0-9]", "", t).upper()
                sc = 0.0
                if wanted and wanted in norm:
                    sc += 500.0
                sc += min(260.0, sum(ch.isdigit() for ch in t) * 4.0)
                sc -= 45.0 * len(_LABEL_INSTRUX_PATTERNS.findall(t))
                return sc

            try:
                p_top = _clone_letter_page()
                p_bot = _clone_letter_page()
                b_top = _one_page_pdf_bytes(_crop_us_letter_label_only, p_top)
                b_bot = _one_page_pdf_bytes(_crop_us_letter_keep_bottom_band, p_bot)
                score_top = _score_letter_bytes(b_top, expected_tracking)
                score_bot = _score_letter_bytes(b_bot, expected_tracking)
                st, sb = score_top, score_bot
                pick_bottom = False
                if sb > st + 8.0:
                    pick_bottom = True
                elif st > sb + 8.0:
                    pick_bottom = False
                else:
                    pick_bottom = bool(_ups_letter_instructions_dominate_top(src_page))
                    if not pick_bottom and _prefer_bottom_letter_band(
                        src_page, instrux_hits
                    ):
                        pick_bottom = True
                    if not pick_bottom and expected_tracking:
                        if _letter_label_likely_lower_half(
                            src_page, expected_tracking
                        ):
                            pick_bottom = True
                        elif _tracking_first_line_in_lower_portion(
                            src_page, expected_tracking
                        ):
                            pick_bottom = True
                letter_crop = "bottom_band_scored" if pick_bottom else "top_band_scored"
                chosen = b_bot if pick_bottom else b_top
                writer.add_page(PdfReader(io.BytesIO(chosen)).pages[0])
            except Exception as ex_letter:
                log_label(
                    "pdf_letter_dual_crop_failed",
                    expected_tracking=expected_tracking,
                    error=str(ex_letter),
                )
                writer = PdfWriter()
                writer.add_page(reader.pages[best_idx])
                p0 = writer.pages[0]
                _crop_us_letter_label_only(p0)
                letter_crop = "top_band_fallback_error"
        else:
            writer.add_page(reader.pages[best_idx])

        out = io.BytesIO()
        writer.write(out)
        final_b = out.getvalue()
        try:
            log_label(
                "pdf_normalize_done",
                expected_tracking=expected_tracking,
                best_page_index=best_idx,
                instruction_pattern_hits_on_chosen_page=instrux_hits,
                letter_crop=letter_crop,
                letter_score_top=round(score_top, 2)
                if score_top is not None
                else None,
                letter_score_bottom=round(score_bot, 2)
                if score_bot is not None
                else None,
                output_bytes=len(final_b),
            )
        except Exception:
            pass
        return final_b
    except Exception as ex:
        log_label(
            "pdf_normalize_exception",
            expected_tracking=expected_tracking,
            error=str(ex),
        )
        return raw_pdf

def _first_rated_shipment(data: Dict[str, Any]):
    """UPS returns RatedShipment as either one object or a list of objects."""
    rr = data.get("RateResponse") or {}
    rs = rr.get("RatedShipment")
    if rs is None:
        return None
    if isinstance(rs, list):
        return rs[0] if rs else None
    if isinstance(rs, dict):
        return rs
    return None


def _transit_and_schedule_from_rated(rated: Dict[str, Any]) -> Tuple[Any, Any]:
    """
    Business days and calendar ETA from RatedShipment.

    Air services often put days at TimeInTransit.DaysInTransit or GuaranteedDelivery.
    Ground typically nests under TimeInTransit.ServiceSummary.EstimatedArrival.
    """
    if not rated:
        return None, None
    gd = rated.get("GuaranteedDelivery") or {}
    tit = rated.get("TimeInTransit") or {}
    eta = gd.get("BusinessDaysInTransit") or tit.get("DaysInTransit")
    sched = gd.get("ScheduledDeliveryDate") or tit.get("Date")

    ss = tit.get("ServiceSummary")
    summaries = ss if isinstance(ss, list) else ([ss] if isinstance(ss, dict) else [])
    for ssum in summaries:
        if not isinstance(ssum, dict):
            continue
        ea = ssum.get("EstimatedArrival") or {}
        if not isinstance(ea, dict):
            continue
        if eta in (None, ""):
            eta = (
                ea.get("BusinessDaysInTransit")
                or ea.get("TotalTransitDays")
                or ea.get("businessDaysInTransit")
                or ea.get("totalTransitDays")
            )
        arr = ea.get("Arrival") or ea.get("arrival") or {}
        if isinstance(arr, dict) and sched in (None, ""):
            sched = arr.get("Date") or arr.get("date")
        if sched in (None, ""):
            sched = ea.get("Date") or ea.get("date")

    if (eta in (None, "") or sched in (None, "")) and isinstance(
        rated.get("RatedPackage"), list
    ):
        for rp in rated["RatedPackage"]:
            if not isinstance(rp, dict):
                continue
            tit2 = rp.get("TimeInTransit") or {}
            if eta in (None, ""):
                eta = tit2.get("DaysInTransit") or tit2.get("BusinessDaysInTransit")
            if sched in (None, ""):
                sched = tit2.get("Date")
            if eta not in (None, "") and sched not in (None, ""):
                break

    if eta == "":
        eta = None
    if sched == "":
        sched = None
    return eta, sched


def _money_and_currency_from_rated(rated: Dict[str, Any]) -> Tuple[Any, str]:
    """Pull total from RatedShipment.

    When NegotiatedRatesIndicator was sent, UPS returns both TotalCharges (retail/list)
    and NegotiatedRateCharges (account pricing). Prefer negotiated so the app matches
    UPS.com logged-in / account quotes; otherwise we would show list price only.
    """
    if not rated:
        return None, "USD"
    nrc = rated.get("NegotiatedRateCharges")
    if isinstance(nrc, dict):
        inner = nrc.get("TotalCharge") or nrc.get("TotalCharges")
        if isinstance(inner, dict) and inner.get("MonetaryValue") not in (None, ""):
            return inner.get("MonetaryValue"), (inner.get("CurrencyCode") or "USD")
        if nrc.get("MonetaryValue") not in (None, ""):
            return nrc.get("MonetaryValue"), (nrc.get("CurrencyCode") or "USD")
    tc = rated.get("TotalCharges")
    if isinstance(tc, dict) and tc.get("MonetaryValue") not in (None, ""):
        return tc.get("MonetaryValue"), (tc.get("CurrencyCode") or "USD")
    return None, "USD"


def _package_weight_lb(p: Dict[str, Any]) -> float:
    w = p.get("weight")
    if w is None:
        w = p.get("Weight")
    try:
        return float(w) if w is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


# ------- Rating -------
def get_rate(
    ship_to: Dict[str, str],
    packages: List[Dict[str, Any]],
    ask_all_services: bool = True
) -> List[Dict[str, Any]]:
    """
    ship_to: { name, phone, addr1, addr2, city, state, zip, country, attention_name? }
    packages: [{ L,W,H, weight }, ...]
    returns: [{code, method, rate, currency, delivery}, ...]
    """
    token = get_access_token()
    url = f"{_ups_base_url()}/api/rating/v1/{_RATING_REQ_OPT}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "transId": str(uuid.uuid4()),
        "transactionSrc": "JRCO",
    }

    # Build base shipment body
    dti: Dict[str, Any] = {"PackageBillType": "03"}  # DAP (shipper pays)
    if "timeintransit" in _RATING_REQ_OPT.lower():
        dti["Pickup"] = {"Date": _pickup_date_ymd()}

    base_shipment = {
        "Shipper": _shipper(),
        "ShipTo": _addr(
            ship_to["name"], ship_to.get("phone",""),
            ship_to["addr1"], ship_to["city"], ship_to["state"], ship_to["zip"], ship_to.get("country","US"),
            ship_to.get("addr2") or None,
            ship_to.get("attention_name") or None,
        ),
        "ShipFrom": _ship_from(),
        "PaymentDetails": {
            "ShipmentCharge": [{
                "Type": "01",
                "BillShipper": {"AccountNumber": SHIPPER_NUMBER}
            }]
        },
        "Package": [_pkg(p, _package_weight_lb(p)) for p in packages],
        "DeliveryTimeInformation": dti,
    }

    results: List[Dict[str, Any]] = []

    def _loop_services(use_negotiated: bool) -> None:
        for code, name in UPS_SERVICES:
            body = {
                "RateRequest": {
                    "Shipment": {
                        **base_shipment,
                        "Service": {"Code": code}
                    },
                    "Request": {"SubVersion": "1707"}
                }
            }
            if use_negotiated:
                body["RateRequest"]["Shipment"]["ShipmentRatingOptions"] = {
                    "NegotiatedRatesIndicator": "Y"
                }

            resp = requests.post(url, headers=headers, json=body, timeout=25)
            if resp.status_code >= 400:
                continue
            try:
                data = resp.json()
            except Exception:
                continue
            rated = _first_rated_shipment(data)
            if not rated:
                continue
            money, curr = _money_and_currency_from_rated(rated)
            try:
                money_f = float(money) if money not in (None, "") else None
            except (TypeError, ValueError):
                money_f = None
            if money_f is None:
                continue
            eta, sched = _transit_and_schedule_from_rated(rated)

            row: Dict[str, Any] = {
                "code": code,
                "method": name,
                "rate": money_f,
                "currency": curr or "USD",
                "delivery": f"{eta} business days" if eta is not None else None,
            }
            try:
                if eta is not None:
                    row["business_days"] = int(eta)
            except (TypeError, ValueError):
                pass
            if sched:
                row["scheduled_delivery_date"] = str(sched)
            results.append(row)

    if ask_all_services:
        _loop_services(NEGOTIATED)
        # Negotiated-only failures often return 200 with no usable charge, or skip all services.
        if NEGOTIATED and not results:
            _loop_services(False)
    else:
        # Single service expected in ship_to["service_code"]
        code = ship_to.get("service_code", "03")
        name = next((n for c, n in UPS_SERVICES if c == code), code)
        body = {
            "RateRequest": {
                "Shipment": {
                    **base_shipment,
                    "Service": {"Code": code}
                },
                "Request": {"SubVersion": "1707"}
            }
        }
        if NEGOTIATED:
            body["RateRequest"]["Shipment"]["ShipmentRatingOptions"] = {"NegotiatedRatesIndicator": "Y"}

        resp = requests.post(url, headers=headers, json=body, timeout=25)
        resp.raise_for_status()
        data = resp.json()
        rated = _first_rated_shipment(data)
        if not rated:
            raise RuntimeError(f"UPS Rate: missing RatedShipment in {json.dumps(data)[:600]}")
        money, curr = _money_and_currency_from_rated(rated)
        money_f = float(money) if money not in (None, "") else None
        eta, sched = _transit_and_schedule_from_rated(rated)
        row2: Dict[str, Any] = {
            "code": code,
            "method": name,
            "rate": money_f,
            "currency": curr or "USD",
            "delivery": f"{eta} business days" if eta is not None else None,
        }
        try:
            if eta is not None:
                row2["business_days"] = int(eta)
        except (TypeError, ValueError):
            pass
        if sched:
            row2["scheduled_delivery_date"] = str(sched)
        results.append(row2)

    # Sort by price ascending, put None at end
    results.sort(key=lambda x: (x["rate"] is None, x["rate"] if x["rate"] is not None else 1e9))
    return results


def _money_blob_to_float(obj: Any) -> float | None:
    """UPS money nodes: {\"MonetaryValue\": \"12.34\"} or bare string/number."""
    if obj is None:
        return None
    if isinstance(obj, bool):
        return None
    if isinstance(obj, (int, float)):
        try:
            v = float(obj)
            return v if v >= 0 else None
        except (TypeError, ValueError):
            return None
    if isinstance(obj, str):
        try:
            v = float(obj.strip())
            return v if v >= 0 else None
        except (TypeError, ValueError):
            return None
    if isinstance(obj, dict):
        for k in ("MonetaryValue", "monetaryValue", "value"):
            if k in obj and obj.get(k) not in (None, ""):
                return _money_blob_to_float(obj.get(k))
    return None


def _charges_block_total_usd(blk: Any) -> float | None:
    if not isinstance(blk, dict):
        return None
    for key in (
        "GrandTotalOfAllCharge",
        "GrandTotalOfAllCharges",
        "TotalCharges",
        "totalCharges",
        "TotalCharge",
        "totalCharge",
    ):
        m = _money_blob_to_float(blk.get(key))
        if m is not None and m > 0:
            return m
    return None


def extract_ups_ship_billed_amount_usd(data: Dict[str, Any]) -> float | None:
    """
    Billed shipping from UPS Ship API JSON (v2409; tolerates camelCase).
    Prefer negotiated/account charges. Used for QuickBooks ShipAmt when the client
    omits or zeros ups_purchased_rate.
    """
    if not isinstance(data, dict):
        return None
    sr = data.get("ShipmentResponse") or data.get("shipmentResponse")
    if not isinstance(sr, dict):
        return None
    res = sr.get("ShipmentResults") or sr.get("shipmentResults")
    if not isinstance(res, dict):
        return None

    for blk_key in (
        "NegotiatedRateCharges",
        "negotiatedRateCharges",
        "NegotiatedCharges",
        "negotiatedCharges",
    ):
        v = _charges_block_total_usd(res.get(blk_key))
        if v is not None and v > 0:
            return v
    for blk_key in ("ShipmentCharges", "shipmentCharges"):
        v = _charges_block_total_usd(res.get(blk_key))
        if v is not None and v > 0:
            return v

    pr = res.get("PackageResults") or res.get("packageResults")
    if isinstance(pr, list):
        pkgs = pr
    elif isinstance(pr, dict):
        pkgs = [pr]
    else:
        pkgs = []
    pkg_total = 0.0
    n_pkg = 0
    for p in pkgs:
        if not isinstance(p, dict):
            continue
        got: float | None = None
        for inner_key in (
            "NegotiatedCharges",
            "negotiatedCharges",
            "NegotiatedRateCharges",
            "negotiatedRateCharges",
            "ShipmentCharges",
            "shipmentCharges",
        ):
            inner = p.get(inner_key)
            if isinstance(inner, dict):
                got = _charges_block_total_usd(inner)
                if got is not None and got > 0:
                    break
        if got is None:
            got = _charges_block_total_usd(p)
        if got is None:
            got = _money_blob_to_float(
                p.get("TotalCharges") or p.get("totalCharges")
            )
        if got is not None and got > 0:
            pkg_total += got
            n_pkg += 1
    if n_pkg > 0 and pkg_total > 0:
        return pkg_total
    return None


# ------- Create Shipment (labels) -------
def create_shipment(
    ship_to: Dict[str, str],
    packages: List[Dict[str, Any]],
    service_code: str
) -> Tuple[List[str], List[str], bool, float | None]:
    """
    Returns (label_urls, tracking_numbers, saved_to_label_printer_folder, billed_shipping_usd_or_none).

    billed_shipping_usd_or_none is parsed from the Ship API response (authoritative when present).
    Trimmed PDF/PNG/ZPL is written to temp for /labels/… and copied to UPS_LABEL_OUTPUT_DIR when that path exists.
    """
    token = get_access_token()
    url = _ups_ship_endpoint()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "transId": _trans_id_header(),
        "transactionSrc": "JRCO",
    }

    shipment = {
        "ShipmentRequest": {
            "Request": {"SubVersion": "1707"},
            "Shipment": {
                "Description": "JRCO shipment",
                "Shipper": _shipper(),
                "ShipFrom": _ship_from(),
                "ShipTo": _addr(
                    ship_to["name"], ship_to.get("phone",""),
                    ship_to["addr1"], ship_to["city"], ship_to["state"], ship_to["zip"], ship_to.get("country","US"),
                    ship_to.get("addr2") or None,
                    ship_to.get("attention_name") or None,
                ),
                "Service": {"Code": service_code},
                "PaymentInformation": {
                    "ShipmentCharge": [{
                        "Type": "01",
                        "BillShipper": {"AccountNumber": SHIPPER_NUMBER}
                    }]
                },
                "Package": [_pkg_ship(p, p.get("weight", 1.0)) for p in packages],
                "ShipmentServiceOptions": {}
            },
            "LabelSpecification": _label_spec()
        }
    }

    if NEGOTIATED:
        shipment["ShipmentRequest"]["Shipment"]["ShipmentRatingOptions"] = {"NegotiatedRatesIndicator": "Y"}

    resp = requests.post(url, headers=headers, json=shipment, timeout=35)
    try:
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(
            f"UPS Ship error {resp.status_code}: {_ups_error_snippet(resp)}"
        ) from e

    data = resp.json()
    billed_shipping_usd = extract_ups_ship_billed_amount_usd(data)
    try:
        log_label(
            "ups_ship_response_billed_amount",
            billed_shipping_usd=billed_shipping_usd,
            service_code=service_code,
        )
    except Exception:
        pass
    # Collect labels & tracking
    label_urls: List[str] = []
    tracking: List[str] = []
    saved_to_printer = False

    def _graphic_from_package(pkg: Dict[str, Any]) -> str | None:
        sl = pkg.get("ShippingLabel")
        if not isinstance(sl, dict):
            return None
        img = sl.get("GraphicImage")
        if isinstance(img, str) and img.strip():
            return img
        parts = sl.get("GraphicImagePart")
        if isinstance(parts, list) and parts:
            joined = "".join(p for p in parts if isinstance(p, str))
            return joined if joined.strip() else None
        return None

    try:
        results = data["ShipmentResponse"]["ShipmentResults"]["PackageResults"]
        if isinstance(results, dict):
            results = [results]
        for pkg in results:
            trk = pkg["TrackingNumber"]
            img = _graphic_from_package(pkg)
            if not img:
                sl = pkg.get("ShippingLabel")
                raise KeyError(
                    "ShippingLabel.GraphicImage missing on package "
                    f"(have ShippingLabel keys: {list(sl.keys()) if isinstance(sl, dict) else sl!r})"
                )
            # UPS sometimes embeds CRLF in base64 streams; strict b64decode rejects them.
            img_clean = re.sub(r"\s+", "", img.strip())
            ext = "pdf" if LABEL_FORMAT == "PDF" else ("png" if LABEL_FORMAT == "PNG" else "zpl")
            safe_trk = re.sub(r"[^\w.\-]+", "_", str(trk or "unknown"))[:120]
            fname = f"ups_{safe_trk}.{ext}"
            fpath = os.path.join(tempfile.gettempdir(), fname)
            try:
                payload = base64.b64decode(img_clean, validate=True)
            except Exception:
                payload = base64.b64decode(img_clean, validate=False)
            _save_ups_label_api_raw_copy(trk, ext, payload)
            if ext == "pdf":
                pre_crop_pdf = payload
                payload = _normalize_ups_label_pdf_bytes(
                    payload, expected_tracking=trk
                )
                _save_ups_label_repo_pre_post_crop(trk, ext, pre_crop_pdf, payload)
            with open(fpath, "wb") as f:
                f.write(payload)
            log_label(
                "label_file_written",
                tracking=trk,
                ext=ext,
                path_basename=fname,
                byte_len=len(payload),
            )
            if _save_trimmed_label_to_printer_folder(fpath, fname):
                saved_to_printer = True
            label_urls.append(f"/labels/{fname}")
            tracking.append(trk)
    except Exception as e:
        raise RuntimeError(
            f"Could not parse UPS label response: {e!s}; body_snippet={json.dumps(data)[:800]}"
        ) from e

    return label_urls, tracking, saved_to_printer, billed_shipping_usd


# ------- Quantum View (account shipment history) -------
def _ups_quantum_view_endpoint() -> str:
    ver = (os.getenv("UPS_QUANTUM_VIEW_API_VERSION") or "v3").strip()
    if ver and not ver.startswith("v"):
        ver = f"v{ver}"
    return f"{_ups_base_url()}/api/quantumview/{ver}/events"


def _qv_as_list(v: Any) -> List[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _qv_walk_collect_manifest_dicts(node: Any, out: List[Dict[str, Any]]) -> None:
    if isinstance(node, dict):
        if "Manifest" in node:
            for m in _qv_as_list(node.get("Manifest")):
                if isinstance(m, dict):
                    out.append(m)
        for v in node.values():
            _qv_walk_collect_manifest_dicts(v, out)
    elif isinstance(node, list):
        for item in node:
            _qv_walk_collect_manifest_dicts(item, out)


def _qv_response_root(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return (
        data.get("QuantumViewResponse")
        or data.get("quantumViewResponse")
        or data
    )


def _qv_response_errors(root: Dict[str, Any]) -> List[str]:
    resp = root.get("Response")
    if not isinstance(resp, dict):
        return []
    errs = _qv_as_list(resp.get("Error"))
    messages = []
    for e in errs:
        if not isinstance(e, dict):
            continue
        desc = (e.get("ErrorDescription") or "").strip()
        code = (e.get("ErrorCode") or "").strip()
        if desc or code:
            messages.append(f"{code}: {desc}".strip(": "))
    return messages


def _qv_is_response_success(root: Dict[str, Any]) -> bool:
    resp = root.get("Response")
    if not isinstance(resp, dict):
        return True
    code = str(resp.get("ResponseStatusCode", "")).strip()
    return code in ("", "1")


def _manifest_rows_from_quantum_view(manifests: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for manifest in manifests:
        ship_to = manifest.get("ShipTo") if isinstance(manifest.get("ShipTo"), dict) else {}
        company = (
            (ship_to.get("CompanyName") or "").strip()
            or (ship_to.get("AttentionName") or "").strip()
            or (ship_to.get("ReceivingAddressName") or "").strip()
        )
        pickup = (manifest.get("PickupDate") or "").strip()
        ship_date = pickup[:8] if len(pickup) >= 8 else ""
        for pkg in _qv_as_list(manifest.get("Package")):
            if not isinstance(pkg, dict):
                continue
            trk = (pkg.get("TrackingNumber") or "").strip()
            if not trk:
                continue
            rows.append(
                {
                    "tracking_number": trk,
                    "company": company or "—",
                    "ship_date": ship_date,
                }
            )
    return rows


def _dedupe_tracking_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    best: Dict[str, Dict[str, str]] = {}
    for r in rows:
        t = r.get("tracking_number") or ""
        if not t:
            continue
        prev = best.get(t)
        if not prev or (r.get("ship_date") or "") > (prev.get("ship_date") or ""):
            best[t] = r
    out = list(best.values())
    out.sort(key=lambda x: x.get("ship_date") or "", reverse=True)
    return out


def _qv_fetch_manifests_for_subscription(
    subscription_name: str,
    begin_s: str,
    end_s: str,
    token: str,
    url: str,
) -> Tuple[List[Dict[str, Any]], str | None, int]:
    """
    One subscription name: paginate by Bookmark until done.
    Returns (manifest_dicts, error_message_or_none, total_rounds).
    """
    all_manifests: List[Dict[str, Any]] = []
    bookmark: str | None = None
    rounds = 0
    max_rounds = 15
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "transId": _trans_id_header(),
        "transactionSrc": "JRCO",
    }

    while rounds < max_rounds:
        rounds += 1
        body: Dict[str, Any] = {
            "QuantumViewRequest": {
                "Request": {
                    "TransactionReference": {"CustomerContext": "jrco-ship-history"},
                    "RequestAction": "QVEvents",
                },
                "SubscriptionRequest": [
                    {
                        "Name": subscription_name,
                        "DateTimeRange": {
                            "BeginDateTime": begin_s,
                            "EndDateTime": end_s,
                        },
                    }
                ],
            }
        }
        if bookmark:
            body["QuantumViewRequest"]["Bookmark"] = bookmark

        resp = requests.post(url, headers=headers, json=body, timeout=45)
        try:
            data = resp.json()
        except Exception:
            data = {}

        if not resp.ok:
            detail = _qv_format_http_error_response(resp)
            return (
                all_manifests,
                f"HTTP {resp.status_code} for subscription {subscription_name!r}: {detail}",
                rounds,
            )

        root = _qv_response_root(data)
        if not _qv_is_response_success(root):
            err_txt = (
                "; ".join(_qv_response_errors(root))
                or "Quantum View request failed"
            )
            return (
                all_manifests,
                f"{subscription_name!r}: {err_txt}",
                rounds,
            )

        chunk: List[Dict[str, Any]] = []
        _qv_walk_collect_manifest_dicts(root, chunk)
        all_manifests.extend(chunk)

        next_bm = root.get("Bookmark") or root.get("bookmark")
        if (
            isinstance(next_bm, str)
            and next_bm.strip()
            and next_bm.strip() != (bookmark or "").strip()
        ):
            bookmark = next_bm.strip()
            continue
        break

    return all_manifests, None, rounds


def quantum_view_fetch_shipment_rows(days: int = 7) -> Dict[str, Any]:
    """
    Pull recent outbound shipment rows from UPS Quantum View (requires UPS-side subscription).

    Env:
      UPS_QUANTUM_VIEW_SUBSCRIPTION_NAME — optional. Comma-separated subscription names to query.
        If unset, defaults to OutboundXML (UPS docs example: outbound + XML feed).
        Your UPS Quantum View setup may use the same name, or a custom name from Manage Subscriptions.
      UPS_QUANTUM_VIEW_TIMEZONE — IANA zone for date window (default America/New_York).
      UPS_QUANTUM_VIEW_API_VERSION — default v3.

    Returns:
      dict with rows, error, message, subscription_names_tried, used_default_subscription_name, etc.
    """
    raw_names = (os.getenv("UPS_QUANTUM_VIEW_SUBSCRIPTION_NAME") or "").strip()
    used_default = not bool(raw_names)
    if not raw_names:
        raw_names = "OutboundXML"
    subscription_names = [x.strip() for x in raw_names.split(",") if x.strip()]

    try:
        d = max(1, min(int(days or 7), 7))
    except (TypeError, ValueError):
        d = 7

    tz_name = (os.getenv("UPS_QUANTUM_VIEW_TIMEZONE") or "America/New_York").strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/New_York")

    end = datetime.now(tz)
    begin = end - timedelta(days=d)
    begin_s = begin.strftime("%Y%m%d%H%M%S")
    end_s = end.strftime("%Y%m%d%H%M%S")

    token = get_access_token()
    url = _ups_quantum_view_endpoint()

    all_manifests: List[Dict[str, Any]] = []
    per_name_errors: List[str] = []
    total_rounds = 0

    for sub in subscription_names:
        manifests, err, rounds = _qv_fetch_manifests_for_subscription(
            sub, begin_s, end_s, token, url
        )
        total_rounds += rounds
        if err:
            per_name_errors.append(err)
            continue
        all_manifests.extend(manifests)

    raw_rows = _manifest_rows_from_quantum_view(all_manifests)
    rows = _dedupe_tracking_rows(raw_rows)

    hint_default = None
    if used_default:
        hint_default = (
            "No UPS_QUANTUM_VIEW_SUBSCRIPTION_NAME was set; used the UPS documentation default "
            "OutboundXML. If the list is empty or you see errors, set the variable to the exact "
            "subscription name from your UPS Quantum View subscription (UPS.com → Shipping → "
            "Quantum View Manage Subscriptions), or try comma-separated names, e.g. "
            "OutboundXML,YourCustomName."
        )

    if rows:
        return {
            "configured": True,
            "rows": rows,
            "message": hint_default,
            "error": None,
            "bookmark_rounds": total_rounds,
            "subscription_names_tried": subscription_names,
            "used_default_subscription_name": used_default,
        }

    if per_name_errors:
        joined = " ".join(per_name_errors)
        return {
            "configured": True,
            "rows": [],
            "message": hint_default,
            "error": joined,
            "bookmark_rounds": total_rounds,
            "subscription_names_tried": subscription_names,
            "used_default_subscription_name": used_default,
        }

    return {
        "configured": True,
        "rows": [],
        "message": hint_default
        or (
            "Quantum View returned no manifest rows in this date range for "
            f"{subscription_names!r}. Confirm outbound shipments exist in the last {d} day(s) "
            "and that the subscription name matches UPS (set UPS_QUANTUM_VIEW_SUBSCRIPTION_NAME if needed)."
        ),
        "error": None,
        "bookmark_rounds": total_rounds,
        "subscription_names_tried": subscription_names,
        "used_default_subscription_name": used_default,
    }
