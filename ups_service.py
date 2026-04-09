# ups_service.py
import base64, io, logging, os, re, shutil, sys, time, uuid, tempfile, json
from datetime import datetime
from typing import List, Dict, Any, Tuple
import requests

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


def _maybe_save_ups_label_debug_raw(tracking: str, ext: str, raw_bytes: bytes) -> None:
    """
    Save undecorated label bytes straight from UPS (before PDF trim / normalization).

    Set UPS_LABEL_DEBUG_SAVE_RAW=1 (or true) to write next to normal temp files, or set
    UPS_LABEL_DEBUG_DIR to an absolute folder to drop files there for easy sharing.

    Output name: ups_<tracking>_api_raw.<ext> (e.g. ups_1Z999..._api_raw.pdf)
    Trimmed file remains ups_<tracking>.<ext> in system temp (served as /labels/...).
    """
    flag = (os.getenv("UPS_LABEL_DEBUG_SAVE_RAW") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    dbg_dir = (os.getenv("UPS_LABEL_DEBUG_DIR") or "").strip()
    if not flag and not dbg_dir:
        return
    out_dir = dbg_dir or tempfile.gettempdir()
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        logging.warning("UPS_LABEL_DEBUG: cannot create directory %s: %s", out_dir, e)
        return
    safe_trk = re.sub(r"[^\w.\-]+", "_", str(tracking or "unknown"))[:120]
    raw_name = f"ups_{safe_trk}_api_raw.{ext}"
    raw_path = os.path.join(out_dir, raw_name)
    try:
        with open(raw_path, "wb") as f:
            f.write(raw_bytes)
        logging.info(
            "UPS label debug: raw API bytes → %s (trimmed for printing: %s/ups_%s.%s)",
            raw_path,
            tempfile.gettempdir(),
            tracking,
            ext,
        )
    except OSError as e:
        logging.warning("UPS_LABEL_DEBUG: failed writing %s: %s", raw_path, e)


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
    r"print\s+this\s+page|how\s+to\s+print|view\s+and\s+print|fold\s+(along|here)|"
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
        return raw_pdf
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import RectangleObject
    except ImportError:
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

    def _prefer_bottom_letter_band(p, instrux_hits: int) -> bool:
        """
        When UPS puts 'how to print' copy above the label on one Letter page, extracted text order
        often lists instructions first; tracking appears only in the lower half.
        """
        if instrux_hits < 2:
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
        return tail_has and not head_has

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

        src_page = reader.pages[best_idx]
        try:
            instrux_hits = len(
                _LABEL_INSTRUX_PATTERNS.findall(src_page.extract_text() or "")
            )
        except Exception:
            instrux_hits = 0

        writer = PdfWriter()
        writer.add_page(src_page)
        page = writer.pages[0]

        # Letter: label may be top band OR bottom band depending on UPS layout.
        if mode in ("auto", "letter_crop"):
            w, h = _dims_at(best_idx)
            if _looks_like_us_letter(w, h) and _prefer_bottom_letter_band(
                src_page, instrux_hits
            ):
                _crop_us_letter_keep_bottom_band(page)
            else:
                _crop_us_letter_label_only(page)

        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception:
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

# ------- Create Shipment (labels) -------
def create_shipment(
    ship_to: Dict[str, str],
    packages: List[Dict[str, Any]],
    service_code: str
) -> Tuple[List[str], List[str], bool]:
    """
    Returns (label_urls, tracking_numbers, saved_to_label_printer_folder).
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
            fname = f"ups_{trk}.{ext}"
            fpath = os.path.join(tempfile.gettempdir(), fname)
            try:
                payload = base64.b64decode(img_clean, validate=True)
            except Exception:
                payload = base64.b64decode(img_clean, validate=False)
            _maybe_save_ups_label_debug_raw(trk, ext, payload)
            if ext == "pdf":
                payload = _normalize_ups_label_pdf_bytes(payload, expected_tracking=trk)
            with open(fpath, "wb") as f:
                f.write(payload)
            if _save_trimmed_label_to_printer_folder(fpath, fname):
                saved_to_printer = True
            label_urls.append(f"/labels/{fname}")
            tracking.append(trk)
    except Exception as e:
        raise RuntimeError(
            f"Could not parse UPS label response: {e!s}; body_snippet={json.dumps(data)[:800]}"
        ) from e

    return label_urls, tracking, saved_to_printer
