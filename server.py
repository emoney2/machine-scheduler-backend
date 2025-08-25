# ─── Imports & Logger Setup ─────────────────────────────────────────────────
import os
import json
import logging
import time
import traceback
import re
import requests
import traceback
import gspread
import logging 
import urllib.parse
import secrets
import io
import tempfile
import base64
from flask import current_app
from flask import request, jsonify
from flask import request, make_response, jsonify, Response
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from math import ceil


# ─── Global “logout everyone” timestamp ─────────────────────────────────
logout_all_ts = 0.0
from io import BytesIO
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from ups_service import get_rate
from functools import wraps
from dotenv import load_dotenv
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from eventlet.semaphore import Semaphore
from flask import Flask, jsonify, request, session, redirect, url_for, render_template_string, url_for, send_from_directory, abort
from flask import make_response
from flask_cors import CORS
from flask_cors import CORS, cross_origin
from flask_socketio import SocketIO
from flask import Response
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from httplib2 import Http
from google_auth_httplib2 import AuthorizedHttp

from googleapiclient.http import MediaIoBaseUpload
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from datetime                      import datetime
from requests_oauthlib import OAuth2Session
from urllib.parse import urlencode
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.utils import ImageReader
from uuid import uuid4
from ups_service import get_rate as ups_get_rate, create_shipment as ups_create_shipment
from google.oauth2.credentials import Credentials as OAuthCredentials

# put this near your other helpers
def _iso_to_eastern_display(iso_str: str) -> str:
    """
    Convert ISO8601 (with or without 'Z') to America/New_York display string:
    M/D/YYYY H:MM AM/PM (no leading zeros on month/day/hour).
    """
    try:
        s = iso_str.replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(s)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=ZoneInfo("UTC"))
        dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
        h = dt_et.hour
        ap = "AM" if h < 12 else "PM"
        h12 = (h % 12) or 12
        return f"{dt_et.month}/{dt_et.day}/{dt_et.year} {h12}:{dt_et.minute:02d} {ap}"
    except Exception:
        return iso_str


# Cache the Drive thumbnail URL + version for 10 minutes to avoid refetching metadata every image
THUMB_META_CACHE = {}  # key: (file_id, size) -> {"thumb": str, "etag": str, "ts": float}
THUMB_META_TTL = 600   # seconds

def _get_thumb_meta(file_id: str, size: str, headers: dict):
    """Return {'thumb': url, 'etag': str} using a short-lived cache."""
    now = time.time()
    key = (file_id, size)
    ent = THUMB_META_CACHE.get(key)
    if ent and (now - ent["ts"] < THUMB_META_TTL):
        return {"thumb": ent["thumb"], "etag": ent["etag"]}

    # Fetch metadata once (thumbnailLink + version fields)
    meta_url = (
        f"https://www.googleapis.com/drive/v3/files/{file_id}"
        f"?fields=thumbnailLink,modifiedTime,md5Checksum"
    )
    meta = requests.get(meta_url, headers=headers, timeout=6)
    if meta.status_code != 200:
        return None

    info = meta.json()
    thumb = info.get("thumbnailLink")
    if not thumb:
        return None

    # Convert Drive style (=s220) to requested size (w240 → s240, etc.)
    s_val = "s" + (size[1:] if size.startswith("w") else "256")
    if re.search(r"=s\d+", thumb):
        thumb = re.sub(r"=s\d+", f"={s_val}", thumb)
    else:
        thumb = f"{thumb}={s_val}"

    # Build an ETag based on file version info (no second meta call needed)
    etag = f'W/"{file_id}-t-{size}-{info.get("md5Checksum") or info.get("modifiedTime") or ""}"'

    THUMB_META_CACHE[key] = {"thumb": thumb, "etag": etag, "ts": now}
    return {"thumb": thumb, "etag": etag}


# ── HARD-CODED BOX INVENTORY & FIT-FUNCTION ─────────────────────────────────────
BOX_TYPES = [
    {"size": "10x10x10", "dims": (10, 10, 10)},
    {"size": "15x15x15", "dims": (15, 15, 15)},
    {"size": "20x20x20", "dims": (20, 20, 20)},
]

def can_fit(product_dims, box_dims):
    p = sorted(product_dims)
    b = sorted(box_dims)
    return all(pi <= bi for pi, bi in zip(p, b))

def choose_box_for_item(length, width, height, volume):
    # 1) Filter out boxes that are too small in any orientation
    eligible = [
        box for box in BOX_TYPES
        if can_fit((length, width, height), box["dims"])
    ]
    # 2) Sort by smallest volume
    eligible.sort(key=lambda b: b["dims"][0] * b["dims"][1] * b["dims"][2])
    # 3) Pick the first whose volume ≥ item volume
    for box in eligible:
        box_vol = box["dims"][0] * box["dims"][1] * box["dims"][2]
        if box_vol >= volume:
            return box["size"]
    # Fallback to largest box
    return BOX_TYPES[-1]["size"]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(BASE_DIR, "qbo_token.json")


# ─── Google Drive Token/Scopes Config ──────────────────────────────
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    # keep broader scope if you already had it:
    "https://www.googleapis.com/auth/drive",
]

# Google token file path (your choice)
GOOGLE_TOKEN_PATH = os.path.join(os.getcwd(), "token.json")

def _bootstrap_token_from_env():
    """
    Accept GOOGLE_TOKEN_JSON_B64 (base64 of token.json) or GOOGLE_TOKEN_JSON (raw JSON),
    write token.json, and log whether a refresh_token is present.
    """
    raw = None
    src = None

    b64 = os.environ.get("GOOGLE_TOKEN_JSON_B64", "").strip()
    if b64:
        try:
            raw = base64.b64decode(b64).decode("utf-8")
            src = "GOOGLE_TOKEN_JSON_B64"
        except Exception as e:
            print("⚠️ Failed to decode GOOGLE_TOKEN_JSON_B64:", e)

    if not raw:
        val = os.environ.get("GOOGLE_TOKEN_JSON", "").strip()
        if val:
            raw = val
            src = "GOOGLE_TOKEN_JSON"

    if not raw:
        print("ℹ️ No GOOGLE_TOKEN_JSON(_B64) provided; skipping token bootstrap.")
        return False

    try:
        info = json.loads(raw)
        has_rt = bool(info.get("refresh_token"))
        with open(GOOGLE_TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(raw)
        print(f"✅ token.json written from {src} (refresh_token={has_rt})")
        return True
    except Exception as e:
        print("⚠️ Failed to write token.json from env:", e)
        return False

_bootstrap_token_from_env()



def _ensure_token_json():
    """If GOOGLE_TOKEN_JSON env var is set, write it to token.json on disk."""
    val = os.environ.get("GOOGLE_TOKEN_JSON", "").strip()
    if not val:
        return False
    try:
        # Validate it’s valid JSON
        json.loads(val)
        with open("token.json", "w", encoding="utf-8") as f:
            f.write(val)
        return True
    except Exception:
        return False

START_TIME_COL_INDEX = 27

from google.oauth2.credentials import Credentials as OAuthCredentials

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets"
]

QBO_SCOPE = ["com.intuit.quickbooks.accounting"]
QBO_AUTH_BASE_URL = "https://appcenter.intuit.com/connect/oauth2"

def get_oauth_credentials():
    # Uses the env-aware loader you added earlier, which checks:
    # 1) GOOGLE_TOKEN_JSON env var, then 2) GOOGLE_TOKEN_PATH (token.json)
    creds = _load_google_creds()
    if creds:
        return creds
    raise Exception("🔑 Google Drive credentials not available. Set GOOGLE_TOKEN_JSON or provide token.json.")

def get_sheets_service():
    import time as _t
    global _sheets_service, _service_ts
    if _sheets_service and (_t.time() - _service_ts) < SERVICE_TTL:
        return _sheets_service
    creds = get_oauth_credentials()
    _sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    _service_ts = _t.time()
    return _sheets_service

def get_drive_service():
    import time as _t
    global _drive_service, _service_ts
    if _drive_service and (_t.time() - _service_ts) < SERVICE_TTL:
        return _drive_service
    creds = get_oauth_credentials()
    _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    _service_ts = _t.time()
    return _drive_service


# ─── Load .env & Logger ─────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Front-end URL & Flask Setup ─────────────────────────────────────────────
raw_frontend = os.environ.get("FRONTEND_URL", "https://machineschedule.netlify.app")
FRONTEND_URL = raw_frontend.strip()

# ─── Flask + CORS + SocketIO ────────────────────────────────────────────────────
app = Flask(__name__)
# Optional GZIP compression (safe if package missing)
try:
    from flask_compress import Compress
    Compress(app)
except Exception:
    # gzip not available; continue without it
    pass

CORS(app, resources={r"/api/*": {"origins": FRONTEND_URL}}, supports_credentials=True)
app.secret_key = os.environ.get("FLASK_SECRET", "shhhh")

# Materialize token.json from env once at import time
_bootstrap_ok = _ensure_token_json()
try:
    app.logger.info("BOOTSTRAP token.json from env: %s", "OK" if _bootstrap_ok else "SKIPPED")
except Exception:
    # logger may not be ready in some environments; fall back to print
    print(f"BOOTSTRAP token.json from env: {'OK' if _bootstrap_ok else 'SKIPPED'}")


# ✅ Allow session cookies to be sent cross-site (Netlify → Render)
app.config.update(
    SESSION_COOKIE_SAMESITE="None",  # Required for cross-domain cookies
    SESSION_COOKIE_SECURE=True       # Required when using SAMESITE=None
)

app.config["SESSION_COOKIE_HTTPONLY"] = True
# app.config["SESSION_COOKIE_DOMAIN"] = "machine-scheduler-backend.onrender.com"

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# --- Quick session check ---
def fetch_company_info(headers, realm_id, env_override=None):
    """
    Fetch company info from QuickBooks (sandbox or production).
    """
    base = get_base_qbo_url(env_override)
    url = f"{base}/v3/company/{realm_id}/companyinfo/{realm_id}"
    res = requests.get(url, headers={**headers, "Accept": "application/json"})
    res.raise_for_status()
    info = res.json().get("CompanyInfo", {})
    return {
        "CompanyName": info.get("CompanyName", ""),
        "AddrLine1":   info.get("CompanyAddr", {}).get("Line1", ""),
        "City":        info.get("CompanyAddr", {}).get("City", ""),
        "CountrySubDivisionCode": info.get("CompanyAddr", {}).get("CountrySubDivisionCode", ""),
        "PostalCode":  info.get("CompanyAddr", {}).get("PostalCode", ""),
        "Phone":       info.get("PrimaryPhone", {}).get("FreeFormNumber", ""),
    }

# --- CORS: handle all OPTIONS preflight early ---
@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        origin = (request.headers.get("Origin") or "").strip().rstrip("/")
        allowed = {
            (os.environ.get("FRONTEND_URL", "https://machineschedule.netlify.app").strip().rstrip("/")),
            "https://machineschedule.netlify.app",
            "http://localhost:3000",
        }
        resp = make_response("", 204)
        if origin in allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
            resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,OPTIONS"
        return resp  # short-circuit OPTIONS

@app.after_request
def apply_cors(response):
    origin = (request.headers.get("Origin") or "").strip().rstrip("/")
    allowed = {
        (os.environ.get("FRONTEND_URL", "https://machineschedule.netlify.app").strip().rstrip("/")),
        "https://machineschedule.netlify.app",
        "http://localhost:3000",
    }
    if origin in allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,OPTIONS"
    return response

@app.route("/api/drive/token-status", methods=["GET"])
def token_status():
    env_present = bool(os.environ.get("GOOGLE_TOKEN_JSON", "").strip())
    file_exists = os.path.exists("token.json")
    cwd = os.getcwd()

    creds = _load_google_creds()
    info = {
        "ok": bool(creds and creds.valid),
        "expired": bool(creds and creds.expired),
        "has_refresh_token": bool(creds and getattr(creds, "refresh_token", None)),
        "valid": bool(creds and creds.valid),
        "env_present": env_present,
        "file_exists": file_exists,
        "cwd": cwd,
    }
    # If not ok, include a short reason without leaking secrets
    if not info["ok"]:
        info["reason"] = (
            "no creds" if not creds else
            "expired without refresh_token" if creds and creds.expired and not creds.refresh_token else
            "invalid"
        )
    return jsonify(info)


@app.route("/api/drive/proxy/<file_id>", methods=["GET", "OPTIONS"])
def drive_proxy(file_id):
    # OPTIONS handled by before_request

    # --- Query params ---
    size = request.args.get("sz", "w256")              # e.g., w256 / w512
    use_thumb = request.args.get("thumb", "1") != "0"  # default True
    debug_mode = request.args.get("debug") == "1"      # NEW: debug switch

    # --- Load creds ---
    creds = _load_google_creds()
    if not creds or not creds.token:
        # No creds: return visible placeholder, or JSON if debug
        if debug_mode:
            return jsonify({"ok": False, "where": "no_creds"}), 502

        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="56" height="56" viewBox="0 0 56 56">'
            '<rect width="56" height="56" fill="#e5e7eb"/>'
            '<text x="50%" y="50%" dy=".35em" text-anchor="middle" font-size="8" fill="#9ca3af">no creds</text>'
            '</svg>'
        ).encode("utf-8")
        return Response(svg, status=200, mimetype="image/svg+xml")

    headers = {"Authorization": f"Bearer {creds.token}"}

    try:
        # --- Fast path: Google Drive thumbnail ---
        if use_thumb:
            meta_url = (
                f"https://www.googleapis.com/drive/v3/files/{file_id}"
                f"?fields=thumbnailLink,mimeType,name,modifiedTime,md5Checksum"
            )
            meta = requests.get(meta_url, headers=headers, timeout=6)
            if meta.status_code != 200:
                if debug_mode:
                    return jsonify({"ok": False, "where": "thumb_meta", "status": meta.status_code}), 502
            else:
                info = meta.json()
                thumb = info.get("thumbnailLink")
                if thumb:
                    # force desired size (=wNNN or =hNNN or =sNNN)
                    if size and ("=s" in thumb or "=w" in thumb or "=h" in thumb):
                        base = thumb.split("=s")[0].split("=w")[0].split("=h")[0]
                        thumb = f"{base}={size}"
                    elif size:
                        sep = "&" if "?" in thumb else "?"
                        thumb = f"{thumb}{sep}{size}"

                    # ETag for client cache
                    etag = f'W/"{file_id}-t-{size}-{info.get("md5Checksum") or info.get("modifiedTime") or ""}"'
                    if request.headers.get("If-None-Match") == etag:
                        resp = make_response("", 304)
                        resp.headers["ETag"] = etag
                        resp.headers["Cache-Control"] = "public, max-age=86400, s-maxage=86400"
                        return resp

                    # fetch thumbnail bytes
                    img = requests.get(thumb, headers=headers, timeout=8)
                    if img.status_code == 200 and img.content:
                        ctype = img.headers.get("Content-Type", "image/jpeg")
                        clen  = len(img.content)
                        if ctype.startswith("image/") and clen >= 500:
                            resp = Response(img.content, status=200, mimetype="image/jpeg")
                            resp.headers["Cache-Control"] = "public, max-age=86400, s-maxage=86400"
                            resp.headers["ETag"] = etag
                            return resp
                else:
                    if debug_mode:
                        return jsonify({"ok": False, "where": "no_thumbnailLink"}), 502

        # --- Fallback: full file via alt=media ---
        meta2_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=modifiedTime,md5Checksum,mimeType,name"
        meta2 = requests.get(meta2_url, headers=headers, timeout=6)
        info2 = meta2.json() if meta2.status_code == 200 else {}
        etag_full = f'W/"{file_id}-f-{info2.get("md5Checksum") or info2.get("modifiedTime") or ""}"'
        if request.headers.get("If-None-Match") == etag_full:
            resp = make_response("", 304)
            resp.headers["ETag"] = etag_full
            resp.headers["Cache-Control"] = "public, max-age=86400, s-maxage=86400"
            return resp

        media_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        r = requests.get(media_url, headers=headers, timeout=12, stream=True)
        if r.status_code != 200:
            if debug_mode:
                return jsonify({"ok": False, "where": "alt_media", "status": r.status_code}), 502
            return make_response((f"Drive returned {r.status_code}", r.status_code))

        content_type = r.headers.get("Content-Type", "application/octet-stream")
        if not content_type.startswith("image/"):
            # Non-image -> visible placeholder (not a 1x1)
            svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" width="56" height="56" viewBox="0 0 56 56">'
                '<rect width="56" height="56" fill="#e5e7eb"/>'
                '<text x="50%" y="50%" dy=".35em" text-anchor="middle" font-size="8" fill="#9ca3af">no preview</text>'
                '</svg>'
            ).encode("utf-8")
            resp = Response(svg, status=200, mimetype="image/svg+xml")
        else:
            resp = Response(r.content, status=200, mimetype=content_type)

        resp.headers["Content-Disposition"] = "inline"
        resp.headers["Cache-Control"] = "public, max-age=86400, s-maxage=86400"
        resp.headers["ETag"] = etag_full
        return resp



    except Exception as e:
        app.logger.exception("drive_proxy error")
        if debug_mode:
            return jsonify({"ok": False, "where": "exception", "error": str(e)}), 500
        # visible placeholder instead of 1×1
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="56" height="56" viewBox="0 0 56 56">'
            '<rect width="56" height="56" fill="#e5e7eb"/>'
            '<text x="50%" y="50%" dy=".35em" text-anchor="middle" font-size="8" fill="#9ca3af">error</text>'
            '</svg>'
        ).encode("utf-8")
        return Response(svg, status=200, mimetype="image/svg+xml")


@app.route("/api/ping", methods=["GET", "OPTIONS"])
def api_ping():
    return jsonify({"ok": True}), 200

@app.route("/labels/<path:filename>")
def serve_label(filename):
    # Serve tmp label files (PDF/PNG/ZPL) written by ups_service
    tmp = tempfile.gettempdir()
    safe = os.path.basename(filename)
    full = os.path.join(tmp, safe)
    if not os.path.exists(full):
        return abort(404)
    # Let browser open in a new tab for printing
    return send_from_directory(tmp, safe, as_attachment=False)

def fetch_invoice_pdf_bytes(invoice_id, realm_id, headers, env_override=None):
    """
    Fetch the invoice PDF from QuickBooks (sandbox or production).
    """
    base = get_base_qbo_url(env_override)
    url = f"{base}/v3/company/{realm_id}/invoice/{invoice_id}/pdf"
    response = requests.get(url, headers={**headers, "Accept": "application/pdf"})
    response.raise_for_status()
    return response.content

# ─── Simulated QuickBooks Invoice Generator ─────────────────────────────────────
def build_packing_slip_pdf(order_data_list, boxes, company_info):
    """
    PDF layout:
      ┌──────────────────────────────────────────────────┐
      │ [logo]           │ Ship To: Customer Info      │
      │ Your company     │                              │
      ├──────────────────────────────────────────────────┤
      │                 PACKING SLIP                   │
      ├──────────────────────────────────────────────────┤
      │ Product │ Design │ Qty │
      │  ...    │  ...   │ ... │
      └──────────────────────────────────────────────────┘
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=0.5*inch,
        rightMargin=0.5*inch,
        topMargin=0.5*inch,
        bottomMargin=0.5*inch,
    )
    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    title_style = ParagraphStyle(
        "TitleCenter",
        parent=styles["Title"],
        alignment=1,  # center
        spaceAfter=12
    )

    elems = []

    # ─── Header ─────────────────────────────────────────────
    # Left cell: logo (half size) + your company info
    logo_path = os.path.join(app.root_path, "static", "logo.png")
    try:
        # preserve aspect ratio, max width = 1.0"
        reader = ImageReader(logo_path)
        orig_w, orig_h = reader.getSize()
        max_w = 1.0 * inch
        scale = max_w / orig_w
        logo = Image(logo_path, width=orig_w*scale, height=orig_h*scale)
    except Exception:
        logo = Paragraph("Your Logo", normal)

    your_info = Paragraph(
        f"{company_info['CompanyName']}<br/>"
        f"{company_info['AddrLine1']}<br/>"
        f"{company_info['City']}, {company_info['CountrySubDivisionCode']} {company_info['PostalCode']}<br/>"
        f"Phone: {company_info['Phone']}",
        normal
    )

    left_cell = [logo, Spacer(1, 0.1*inch), your_info]

    # Right cell: customer info from the first order
    cust = order_data_list[0]
    customer_info = Paragraph(
        f"<b>Ship To:</b><br/>{cust.get('Company Name','')}<br/>{cust.get('Address','')}<br/>{cust.get('Phone','')}",
        normal
    )

    # shift customer info further right
    header_table = Table(
        [[left_cell, customer_info]],
        # Swap widths so customer info moves left
        colWidths=[4.0*inch, 2.0*inch]
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("ALIGN",  (1,0), (1,0), "RIGHT"),
        ("LEFTPADDING",  (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
    ]))
    elems.append(header_table)
    elems.append(Spacer(1, 0.25*inch))

    # ─── Title ──────────────────────────────────────────────
    elems.append(Paragraph("PACKING SLIP", title_style))

    # ─── Line Items ────────────────────────────────────────
    data = [["Product", "Design", "Qty"]]
    for od in order_data_list:
        data.append([
            od.get("Product", ""),
            od.get("Design", ""),
            str(od.get("ShippedQty", ""))
        ])

    # widen the table columns
    table = Table(data, colWidths=[3.0*inch, 3.0*inch, 1.0*inch])
    table.setStyle(TableStyle([
        ("GRID",         (0,0), (-1,-1), 0.5, colors.grey),
        ("BACKGROUND",   (0,0), (-1,0),   colors.lightgrey),
        ("ALIGN",        (2,0), (2,0),    "CENTER"),  # center Qty **header** cell
        ("ALIGN",        (2,1), (2,-1),   "CENTER"),  # center Qty column
        ("VALIGN",       (0,0), (-1,-1),  "MIDDLE"),
        ("FONTNAME",     (0,0), (-1,0),   "Helvetica-Bold"),
        ("BOTTOMPADDING",(0,0),  (-1,0),   6),
        ("TOPPADDING",   (0,0),  (-1,0),   6),
    ]))
    elems.append(table)

    # ─── Build PDF ──────────────────────────────────────────
    doc.build(elems)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes

def get_quickbooks_credentials():
    # 1) First, try the live session token in Flask session
    token_data = session.get("qbo_token")
    if token_data and "access_token" in token_data:
        realm_id = token_data.get("realmId")
        token = {
            "access_token":  token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "expires_at":    token_data.get("expires_at", 0)
        }

        # → refresh if expired
        if token["expires_at"] < time.time():
            env_for_oauth = session.get("qboEnv", QBO_ENV)
            client_id, client_secret = get_qbo_oauth_credentials(env_for_oauth)

            oauth = OAuth2Session(
                client_id=client_id,
                token=token,
                auto_refresh_kwargs={
                    "client_id":     client_id,
                    "client_secret": client_secret,
                },
                auto_refresh_url=QBO_TOKEN_URL,
                token_updater=lambda new_tok: None
            )
            new_token = oauth.refresh_token(
                QBO_TOKEN_URL,
                client_id=client_id,
                client_secret=client_secret
            )

            new_token["expires_at"] = time.time() + int(new_token["expires_in"])

            # persist refreshed token to session & disk
            session["qbo_token"] = {
                "access_token":  new_token["access_token"],
                "refresh_token": new_token["refresh_token"],
                "expires_at":    new_token["expires_at"],
                "realmId":       realm_id
            }
            with open(TOKEN_PATH, "w") as f:
                json.dump({**new_token, "realmId": realm_id}, f, indent=2)
            token = new_token

        headers = {
            "Authorization": f"Bearer {token['access_token']}",
            "Accept":        "application/json",
            "Content-Type":  "application/json"
        }
        return headers, realm_id

    # 2) Fallback to disk-persisted token
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH) as f:
            file_token = json.load(f)
        realm_id = file_token.get("realmId")
        token = {
            "access_token":  file_token.get("access_token"),
            "refresh_token": file_token.get("refresh_token"),
            "expires_at":    file_token.get("expires_at", 0)
        }

        if token["access_token"] and realm_id:
            # → refresh if expired
            if token["expires_at"] < time.time():
                # Use env-aware credentials (production vs sandbox)
                env_for_oauth = session.get("qboEnv", os.environ.get("QBO_ENV", "production")).lower()
                try:
                    client_id, client_secret = get_qbo_oauth_credentials(env_for_oauth)
                except NameError:
                    is_prod = env_for_oauth in ("prod", "production", "live")
                    client_id     = os.environ.get("QBO_PROD_CLIENT_ID") if is_prod else os.environ.get("QBO_SANDBOX_CLIENT_ID")
                    client_secret = os.environ.get("QBO_PROD_CLIENT_SECRET") if is_prod else os.environ.get("QBO_SANDBOX_CLIENT_SECRET")
                    # Fallback to generic names if present
                    client_id     = client_id or os.environ.get("QBO_CLIENT_ID")
                    client_secret = client_secret or os.environ.get("QBO_CLIENT_SECRET")

                oauth = OAuth2Session(
                    client_id=client_id,
                    token=token,
                    auto_refresh_kwargs={
                        "client_id":     client_id,
                        "client_secret": client_secret,
                    },
                    auto_refresh_url=QBO_TOKEN_URL,
                    token_updater=lambda new_tok: None
                )
                new_token = oauth.refresh_token(
                    QBO_TOKEN_URL,
                    client_id=client_id,
                    client_secret=client_secret
                )
                new_token["expires_at"] = time.time() + int(new_token.get("expires_in", 3600))

                # persist refreshed token to disk & session
                with open(TOKEN_PATH, "w") as f:
                    json.dump({**new_token, "realmId": realm_id}, f, indent=2)
                session["qbo_token"] = {
                    "access_token":  new_token.get("access_token"),
                    "refresh_token": new_token.get("refresh_token"),
                    "expires_at":    new_token.get("expires_at"),
                    "realmId":       realm_id
                }
                token = new_token

            headers = {
                "Authorization": f"Bearer {token['access_token']}",
                "Accept":        "application/json",
                "Content-Type":  "application/json"
            }
            return headers, realm_id


    # 3) No valid token → restart OAuth
    env_qbo = os.environ.get("QBO_ENV", "production")
    raise RedirectException(f"/qbo/login?env={env_qbo}")


def get_quickbooks_auth_url(redirect_uri, state=""):
    client_id = os.environ["QBO_CLIENT_ID"]
    scope     = "com.intuit.quickbooks.accounting"
    return (
        f"{QBO_AUTH_BASE_URL}?"
        f"client_id={client_id}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&response_type=code"
        f"&scope={scope}"
        f"&state={urllib.parse.quote(state)}"
    )

@app.route("/quickbooks-auth", methods=["GET"])
def quickbooks_auth():
    """
    Initiates the QuickBooks OAuth2 flow by redirecting
    the browser to Intuit’s authorization URL, with a fresh state.
    """
    # 1) Generate a random state and store it in the session
    state = str(uuid4())
    session["qbo_oauth_state"] = state

    # 2) Build redirect URI & auth URL with that state
    redirect_uri = os.environ.get("QBO_REDIRECT_URI")
    auth_url = get_quickbooks_auth_url(redirect_uri, state=state)

    logger.info("🔗 Redirecting to QuickBooks OAuth at %s (state=%s)", auth_url, state)
    return redirect(auth_url)



class RedirectException(Exception):
    def __init__(self, redirect_url):
        self.redirect_url = redirect_url

def refresh_quickbooks_token():
    """
    Refresh the QuickBooks OAuth token using sandbox or production credentials.
    """
    # ── 0) Determine environment from session ─────────────────────
    env_override = session.get("qboEnv", QBO_ENV)

    # ── 1) Pick the correct client ID/secret ─────────────────────
    client_id, client_secret = get_qbo_oauth_credentials(env_override)

    # ── 2) Load existing token from disk ──────────────────────────
    with open(TOKEN_PATH, "r") as f:
        disk_data = json.load(f)

    # ── 3) Rebuild the OAuth2Session with that token ─────────────
    oauth = OAuth2Session(
        client_id=client_id,
        token=disk_data
    )

    # ── 4) Refresh the token using matching client_secret ────────
    new_token = oauth.refresh_token(
        QBO_TOKEN_URL,
        client_secret=client_secret,
        refresh_token=disk_data["refresh_token"]
    )

    # ── 5) Persist refreshed token to disk ───────────────────────
    disk_data.update(new_token)
    with open(TOKEN_PATH, "w") as f:
        json.dump(disk_data, f, indent=2)

    # ── 6) Update session so API calls see the new expiry ───────
    session["qbo_token"] = {
        "access_token":  new_token["access_token"],
        "refresh_token": new_token["refresh_token"],
        "expires_at":    time.time() + int(new_token["expires_in"]),
        "realmId":       disk_data["realmId"]
    }

    print(f"🔁 Refreshed QBO token for env '{env_override}'")

def update_sheet_cell(sheet_id, sheet_name, lookup_col, lookup_value, target_col, new_value):
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{sheet_name}!A1:Z",
    ).execute()
    rows = result.get("values", [])
    headers = rows[0]

    try:
        lookup_index = headers.index(lookup_col)
        target_index = headers.index(target_col)
    except ValueError:
        print(f"❌ Column not found: {lookup_col} or {target_col}")
        return

    for i, row in enumerate(rows[1:], start=2):
        if len(row) > lookup_index and str(row[lookup_index]).strip() == str(lookup_value):
            range_to_update = f"{sheet_name}!{chr(65 + target_index)}{i}"
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=range_to_update,
                valueInputOption="USER_ENTERED",
                body={"values": [[str(new_value)]]}
            ).execute()
            print(f"✅ Updated {target_col} for order {lookup_value} to {new_value}")
            break


def get_or_create_customer_ref(company_name, sheet, quickbooks_headers, realm_id, env_override=None):
    """
    Look up a customer in QuickBooks; create if missing.
    """
    import requests
    import time
    import os

    # ── 1) Try to fetch from QuickBooks ───────────────────────────
    base = get_base_qbo_url(env_override)
    query_url = f"{base}/v3/company/{realm_id}/query?minorversion=65"
    query = f"SELECT * FROM Customer WHERE DisplayName = '{company_name}'"
    response = requests.get(query_url, headers=quickbooks_headers, params={"query": query})

    if response.status_code == 200:
        customers = response.json().get("QueryResponse", {}).get("Customer", [])
        if customers:
            cust = customers[0]
            return {"value": cust["Id"], "name": cust["DisplayName"]}

    # ── 2) Fetch from Google Sheets “Directory” ────────────────────
    SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
    if not SPREADSHEET_ID:
        raise Exception("🚨 Missing SPREADSHEET_ID environment variable")
    resp = sheet.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Directory!A1:Z"
    ).execute()
    rows = resp.get("values", [])
    if not rows or len(rows) < 2:
        directory = []
    else:
        headers = rows[0]
        directory = [dict(zip(headers, row)) for row in rows[1:]]

    match = next(
        (row for row in directory if row.get("Company Name", "").strip() == company_name.strip()),
        None
    )
    if not match:
        raise Exception(f"❌ Customer '{company_name}' not found in Google Sheets Directory")

    # ── 3) Build QuickBooks customer payload ───────────────────────
    payload = {
        "DisplayName":      company_name,
        "CompanyName":      company_name,
        "PrimaryEmailAddr": {"Address": match.get("Contact Email Address", "")},
        "PrimaryPhone":     {"FreeFormNumber": match.get("Phone Number", "")},
        "BillAddr": {
            "Line1":                  match.get("Street Address 1", ""),
            "Line2":                  match.get("Street Address 2", ""),
            "City":                   match.get("City", ""),
            "CountrySubDivisionCode": match.get("State", ""),
            "PostalCode":             match.get("Zip Code", "")
        },
        "GivenName":  match.get("Contact First Name", ""),
        "FamilyName": match.get("Contact Last Name", "")
    }

    # ── 4) Create customer in QuickBooks ──────────────────────────
    create_url = f"{base}/v3/company/{realm_id}/customer"
    res = requests.post(create_url, headers=quickbooks_headers, json=payload)
    if res.status_code in (200, 201):
        data = res.json().get("Customer", {})
        return {"value": data["Id"], "name": data["DisplayName"]}

    # ── 5) Sandbox fallback if “ApplicationAuthorizationFailed” ────
    if res.status_code == 403 and "ApplicationAuthorizationFailed" in res.text:
        print("⚠️ Sandbox blocked creation; retrying with fallback name.")
        payload["DisplayName"] = f"{company_name} Test {int(time.time())}"
        res2 = requests.post(create_url, headers=quickbooks_headers, json=payload)
        if res2.status_code in (200, 201):
            data = res2.json().get("Customer", {})
            return {"value": data["Id"], "name": data["DisplayName"]}
        else:
            raise Exception(f"❌ Retry also failed: {res2.text}")

    # ── Final failure ──────────────────────────────────────────────
    raise Exception(f"❌ Failed to create customer in QuickBooks: {res.text}")

def get_or_create_item_ref(product_name, headers, realm_id, env_override=None):
    base = get_base_qbo_url(env_override)
    query_url = f"{base}/v3/company/{realm_id}/query"
    escaped_name = json.dumps(product_name)  # ensures correct quoting
    query = f"SELECT * FROM Item WHERE Name = {escaped_name}"
    response = requests.get(query_url, headers=headers, params={"query": query})

    items = response.json().get("QueryResponse", {}).get("Item", [])

    if items:
        return {
            "value": items[0]["Id"],
            "name": items[0]["Name"]
        }

    print(f"⚠️ Item '{product_name}' not found. Attempting to create...")

    create_url = f"{base}/v3/company/{realm_id}/item?minorversion=65"
    payload = {
        "Name": product_name,
        "Type": "NonInventory",
        "IncomeAccountRef": {
            "name": "Sales of Product Income",
            "value": "79"  # Use 79 for sandbox
        }
    }

    res = requests.post(create_url, headers=headers, json=payload)
    if res.status_code in [200, 201]:
        item = res.json().get("Item", {})
        return {
            "value": item["Id"],
            "name": item["Name"]
        }

    elif res.status_code == 400 and "Duplicate Name Exists Error" in res.text:
        print(f"🔁 Item '{product_name}' already exists, retrieving existing item...")
        escaped_name = product_name.replace("'", "''")
        query = f"SELECT * FROM Item WHERE Name = '{escaped_name}'"
        print(f"🔍 Fallback item lookup query: {query}")
        lookup_res = requests.get(query_url, headers=headers, params={"query": query})
        print("🧾 Fallback item lookup response:", lookup_res.text)

        existing_items = lookup_res.json().get("QueryResponse", {}).get("Item", [])
        if existing_items:
            return {
                "value": existing_items[0]["Id"],
                "name": existing_items[0]["Name"]
            }
        else:
            raise Exception(f"❌ Item '{product_name}' exists but could not be retrieved.")
    else:
        raise Exception(f"❌ Failed to create item '{product_name}' in QBO: {res.text}")



def create_invoice_in_quickbooks(order_data, shipping_method="UPS Ground", tracking_list=None, base_shipping_cost=0.0, env_override=None):
    print(f"🧾 Creating real QuickBooks invoice for order {order_data['Order #']}")

    token = session.get("qbo_token")
    print("🔑 QBO Token in session:", token)
    print("🏢 Realm ID in session:", token.get("realmId") if token else None)
    if not token or "access_token" not in token or "expires_at" not in token:
      env_qbo = session.get("qboEnv", QBO_ENV)
      next_url = f"/qbo/login?env={env_qbo}"
      raise RedirectException(next_url)


    # Refresh if token is expired
    if time.time() >= token["expires_at"]:
        print("🔁 Access token expired. Refreshing...")
        refresh_quickbooks_token()
        token = session.get("qbo_token")  # re-pull refreshed token

    access_token = token.get("access_token")
    realm_id = token.get("realmId")

    if not access_token or not realm_id:
        raise Exception("❌ Missing QuickBooks access token or realm ID")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    # ── Pick sandbox vs prod for this run ────────────────────
    base = get_base_qbo_url(env_override)

    # Step 1: Get or create customer
    sheet = sh
    customer_ref = get_or_create_customer_ref(order_data.get("Company Name", ""), sheet, headers, realm_id)

    # Step 2: Get item reference from QBO (look up or create if missing)
    product_name = order_data.get("Product", "").strip()
    item_ref = get_or_create_item_ref(product_name, headers, realm_id)

    # Step 3: Build and send invoice
    amount = float(order_data.get("Price", 0)) * int(order_data.get("Quantity", 1))
    num_labels = len(tracking_list or [])
    shipping_total = round(float(base_shipping_cost) * 1.1 + num_labels * 5, 2)

    qty = int(order_data.get("Quantity", 1))
    unit_price = float(order_data.get("Price", 0))
    amount = round(qty * unit_price, 2)

    invoice_payload = {
        "CustomerRef": customer_ref,
        "Line": [
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": float(round(amount, 2)),
                "Description": order_data.get("Design", ""),
                "SalesItemLineDetail": {
                    "ItemRef": { "value": item_ref["value"] },
                    "Qty": float(qty),
                    "UnitPrice": float(round(unit_price, 2))
                }
            }
        ],
        "TxnDate":      datetime.now().strftime("%Y-%m-%d"),
        "TotalAmt":     float(round(amount, 2)),
        "SalesTermRef": { "value": "3" },
    }

    invoice_url = f"{base}/v3/company/{realm_id}/invoice"
    logging.info("📦 Invoice payload about to send to QuickBooks:")
    logging.info(json.dumps(invoice_payload, indent=2))

    invoice_resp = requests.post(invoice_url, headers={**headers, "Content-Type": "application/json"}, json=invoice_payload)

    if invoice_resp.status_code != 200:
        try:
            error_detail = invoice_resp.json()
        except Exception:
            error_detail = invoice_resp.text
        logging.error("❌ Failed to create invoice in QuickBooks")
        logging.error("🔢 Status Code: %s", invoice_resp.status_code)
        logging.error("🧾 Response Content: %s", error_detail)
        logging.error("📦 Invoice Payload:\n%s", json.dumps(invoice_payload, indent=2))
        raise Exception("Failed to create invoice in QuickBooks")

    invoice = invoice_resp.json().get("Invoice")

    if not invoice or "Id" not in invoice:
        raise Exception("❌ QuickBooks invoice creation failed or response invalid")

    print("✅ Invoice created:", invoice)
    # ── Build link for sandbox vs. production UI ────────────
    app_url = (
        "https://app.qbo.intuit.com"
        if (env_override or QBO_ENV) == "production"
        else "https://app.sandbox.qbo.intuit.com"
    )
    return f"{app_url}/app/invoice?txnId={invoice['Id']}"

def create_consolidated_invoice_in_quickbooks(
    order_data_list,
    shipping_method,
    tracking_list,
    base_shipping_cost,
    sheet,
    env_override=None
):
    """
    Builds a single consolidated invoice in QuickBooks (sandbox or production).
    """

    # ── 0) Pick sandbox vs. production base URL ─────────────────────
    base = get_base_qbo_url(env_override)

    # ── 1) Get QBO credentials ──────────────────────────────────────
    headers, realm_id = get_quickbooks_credentials()

    logging.info(
        "📦 Incoming order_data_list:\n%s",
        json.dumps(order_data_list, indent=2)
    )

    # ── 2) Find or create the customer ──────────────────────────────
    first_order = order_data_list[0]
    customer_ref = get_or_create_customer_ref(
        first_order.get("Company Name", ""),
        sheet,
        headers,
        realm_id,
        env_override
    )

    # ── 3) Build line items ─────────────────────────────────────────
    line_items = []
    for order in order_data_list:
        raw_name = order.get("Product", "").strip()

        # Skip any “Back” jobs
        if re.search(r"\s+Back$", raw_name, flags=re.IGNORECASE):
            continue

        product_base = re.sub(
            r"\s+(Front|Full)$", "", raw_name, flags=re.IGNORECASE
        ).strip()
        design_name = order.get("Design", "").strip()

        # Parse shipped quantity (fallback to other fields)
        try:
            raw_qty = (
                order.get("ShippedQty")
                or order.get("Shipped")
                or order.get("Quantity")
            )
            shipped_qty = int(float(raw_qty))
            if shipped_qty <= 0:
                continue
        except Exception:
            continue

        # Parse price
        try:
            price = float(order.get("Price", 0) or 0)
        except Exception:
            price = 0.0

        # Lookup or create item, propagating override
        item_ref = get_or_create_item_ref(
            product_base,
            headers,
            realm_id,
            env_override
        )

        line_items.append({
            "DetailType": "SalesItemLineDetail",
            "Amount":     round(shipped_qty * price, 2),
            "Description": design_name,
            "SalesItemLineDetail": {
                "ItemRef":  {
                    "value": item_ref["value"],
                    "name":  item_ref["name"]
                },
                "Qty":        shipped_qty,
                "UnitPrice":  price
            }
        })

    if not line_items:
        raise Exception("❌ No valid line items to invoice.")

    # ── 4) Build the invoice payload ────────────────────────────────
    invoice_payload = {
        "CustomerRef":  customer_ref,
        "Line":         line_items,
        "TxnDate":      datetime.now().strftime("%Y-%m-%d"),
        "TotalAmt":     round(sum(item["Amount"] for item in line_items), 2),
        "SalesTermRef": {"value": "3"},
        "BillEmail":    {"Address": "sandbox@sample.com"}
    }

    # ── 5) Send to QuickBooks ───────────────────────────────────────
    url = f"{base}/v3/company/{realm_id}/invoice"
    logging.info(
        "📦 Invoice payload:\n%s",
        json.dumps(invoice_payload, indent=2)
    )

    res = requests.post(
        url,
        headers={**headers, "Content-Type": "application/json"},
        json=invoice_payload
    )
    if res.status_code not in (200, 201):
        logging.error("❌ QBO invoice creation failed: %s", res.text)
        raise Exception(f"QuickBooks invoice creation failed: {res.text}")

    invoice = res.json().get("Invoice", {})
    inv_id = invoice.get("Id")

    # ── 6) Build the UI link for sandbox vs. production ────────────
    app_url = (
        "https://app.qbo.intuit.com"
        if (env_override or QBO_ENV) == "production"
        else "https://app.sandbox.qbo.intuit.com"
    )
    return f"{app_url}/app/invoice?txnId={inv_id}"



@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "message": "Backend is running"}), 200

@app.before_request
def _debug_session():
     logger.info("🔑 Session data for %s → %s", request.path, dict(session))
# allow cross-site cookies
app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
)

# only allow our Netlify front-end on /api/* and support cookies
CORS(
    app,
    resources={
        r"/":             {"origins": FRONTEND_URL},
        r"/api/*":        {"origins": FRONTEND_URL},
        r"/api/threads":  {"origins": FRONTEND_URL},
        r"/submit":       {"origins": FRONTEND_URL},
    },
    supports_credentials=True
)


from flask import session  # (if not already imported)

@app.before_request
def _debug_session():
    logger.info("🔑 Session data for %s → %s", request.path, dict(session))



# ─── Session & Auth Helpers ──────────────────────────────────────────────────
app.secret_key = os.environ.get("SECRET_KEY", "dev-fallback-secret")


def login_required_session(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # 1) OPTIONS are always allowed (CORS preflight)
        if request.method == "OPTIONS":
            origin = (request.headers.get("Origin") or "").strip().rstrip("/")
            allowed = {
                (os.environ.get("FRONTEND_URL", "https://machineschedule.netlify.app").strip().rstrip("/")),
                "https://machineschedule.netlify.app",
                "http://localhost:3000",
            }
            response = make_response("", 204)
            if origin in allowed:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Credentials"] = "true"
                response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
                response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,OPTIONS"
            return response

        # 2) Must be logged in at all
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("login", next=request.path))

        # 3) Token‐match check
        ADMIN_TOKEN   = os.environ.get("ADMIN_TOKEN", "")
        token_at_login = session.get("token_at_login", "")
        print("🔐 Comparing tokens — token_at_login =", token_at_login, "vs ADMIN_TOKEN =", ADMIN_TOKEN)
        if token_at_login != ADMIN_TOKEN:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "session invalidated"}), 401
            return redirect(url_for("login", next=request.path))

        # 4) Idle timeout: 3 hours
        last = session.get("last_activity")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
            except:
                session.clear()
                if request.path.startswith("/api/"):
                    return jsonify({"error": "authentication required"}), 401
                return redirect(url_for("login", next=request.path))

            if datetime.utcnow() - last_dt > timedelta(hours=3):
                session.clear()
                if request.path.startswith("/api/"):
                    return jsonify({"error": "session expired"}), 401
                return redirect(url_for("login", next=request.path))
        else:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("login", next=request.path))

        # 5) Forced‐logout check
        #    any session logged in before logout_all_ts is invalidated
        if session.get("login_time", 0) < logout_all_ts:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "forced logout"}), 401
            return redirect(url_for("login", next=request.path))

        # 6) All good—update last_activity and proceed
        session["last_activity"] = datetime.utcnow().isoformat()
        return f(*args, **kwargs)

    return decorated

ALLOWED_WS_ORIGINS = list({
    os.environ.get("FRONTEND_URL", "https://machineschedule.netlify.app").strip().rstrip("/"),
    "https://machineschedule.netlify.app",
    "http://localhost:3000",
})

socketio = SocketIO(
    app,
    cors_allowed_origins=ALLOWED_WS_ORIGINS,
    async_mode="eventlet",
    path="/socket.io",
    ping_interval=25,
    ping_timeout=20,
)

# ─── Google Drive Token/Scopes Config ──────────────────────────────
# ─── Google Drive Token/Scopes Config ──────────────────────────────
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive",
]

# use the same path variable everywhere for Google token
GOOGLE_TOKEN_PATH = os.path.join(os.getcwd(), "token.json")

def _load_google_creds():
    """
    Load Google OAuth user credentials from:
      1) GOOGLE_TOKEN_JSON env var (preferred, raw token.json contents)
      2) token.json on disk (GOOGLE_TOKEN_PATH)
    If the token already contains scopes, do not override them (avoids invalid_scope).
    Returns an OAuthCredentials object or None.
    """
    # 1) ENV first
    env_val = os.environ.get("GOOGLE_TOKEN_JSON", "").strip()
    if env_val:
        try:
            info = json.loads(env_val)
            token_scopes = info.get("scopes")
            # If token already has scopes, don't override (prevents invalid_scope)
            if token_scopes:
                creds = OAuthCredentials.from_authorized_user_info(info)
            else:
                creds = OAuthCredentials.from_authorized_user_info(info, scopes=GOOGLE_SCOPES)
            print("🔎 token (env) scopes:", token_scopes)
            if not creds.valid and creds.refresh_token:
                from google.auth.transport.requests import Request as GoogleRequest
                creds.refresh(GoogleRequest())
            return creds
        except Exception as e:
            print("❌ ENV token could not build OAuthCredentials:", repr(e))
            # fall through to file

    # 2) File next
    try:
        if os.path.exists(GOOGLE_TOKEN_PATH):
            with open(GOOGLE_TOKEN_PATH, "r", encoding="utf-8") as f:
                info = json.load(f)
            token_scopes = info.get("scopes")
            if token_scopes:
                creds = OAuthCredentials.from_authorized_user_info(info)
            else:
                creds = OAuthCredentials.from_authorized_user_info(info, scopes=GOOGLE_SCOPES)
            print("🔎 token (file) scopes:", token_scopes)
            if not creds.valid and creds.refresh_token:
                from google.auth.transport.requests import Request as GoogleRequest
                creds.refresh(GoogleRequest())
            return creds
    except Exception as e:
        print("❌ FILE token could not build OAuthCredentials:", repr(e))

    # 3) Nothing worked
    return None


# --- Drive: make file public (anyone with link → reader) ---------------------
@app.route("/api/drive/makePublic", methods=["POST", "OPTIONS"])
@login_required_session
def drive_make_public():
    if request.method == "OPTIONS":
        # CORS preflight
        resp = make_response("", 204)
        resp.headers["Access-Control-Allow-Origin"] = FRONTEND_URL
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,OPTIONS"
        return resp

    data = request.get_json(silent=True) or {}
    file_id = (data.get("fileId") or "").strip()
    if not file_id:
        return jsonify({"ok": False, "error": "missing fileId"}), 400

    try:
        drive = get_drive_service()
        # Check existing permissions for 'anyone'
        perms = drive.permissions().list(
            fileId=file_id,
            fields="permissions(id,type,role)"
        ).execute()
        already_public = any(
            p.get("type") == "anyone" and p.get("role") in ("reader", "commenter", "writer")
            for p in (perms.get("permissions") or [])
        )

        if not already_public:
            # Make it public (view-only)
            drive.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
                fields="id"
            ).execute()

        return jsonify({"ok": True})
    except Exception as e:
        # Don't blow up the UI on permission hiccups
        return jsonify({"ok": False, "error": str(e)}), 500



# ─── Google Sheets Credentials & Semaphore ───────────────────────────────────
sheet_lock = Semaphore(1)
SPREADSHEET_ID   = os.environ["SPREADSHEET_ID"]
ORDERS_RANGE     = os.environ.get("ORDERS_RANGE",     "Production Orders!A1:AM")
EMBROIDERY_RANGE = os.environ.get("EMBROIDERY_RANGE", "Embroidery List!A1:AM")
MANUAL_RANGE       = os.environ.get("MANUAL_RANGE", "Manual State!A2:H")
MANUAL_CLEAR_RANGE = os.environ.get("MANUAL_RANGE", "Manual State!A2:H")

# ── Legacy QuickBooks vars (still available if used elsewhere) ────
QBO_CLIENT_ID     = os.environ.get("QBO_CLIENT_ID")
QBO_CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET")
QBO_REDIRECT_URI  = (os.environ.get("QBO_REDIRECT_URI") or "").strip()
QBO_AUTH_URL      = "https://appcenter.intuit.com/connect/oauth2"
QBO_TOKEN_URL     = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_SCOPES        = ["com.intuit.quickbooks.accounting"]

# ── QuickBooks environment configuration ────────────────────────
QBO_SANDBOX_BASE_URL = os.environ.get(
    "QBO_SANDBOX_BASE_URL",
    "https://sandbox-quickbooks.api.intuit.com"
)
QBO_PROD_BASE_URL = os.environ.get(
    "QBO_PROD_BASE_URL",
    "https://quickbooks.api.intuit.com"
)
QBO_ENV = os.environ.get("QBO_ENV", "sandbox").lower()  # 'sandbox' or 'production'

def get_base_qbo_url(env_override: str = None) -> str:
    """
    Return the correct QuickBooks API base URL based on env_override or QBO_ENV.
    """
    env = (env_override or QBO_ENV).lower()
    return QBO_PROD_BASE_URL if env == "production" else QBO_SANDBOX_BASE_URL

# ── OAuth client credentials for sandbox vs. production ─────────
QBO_SANDBOX_CLIENT_ID     = os.environ.get("QBO_SANDBOX_CLIENT_ID")
QBO_SANDBOX_CLIENT_SECRET = os.environ.get("QBO_SANDBOX_CLIENT_SECRET")
QBO_PROD_CLIENT_ID        = os.environ.get("QBO_PROD_CLIENT_ID")
QBO_PROD_CLIENT_SECRET    = os.environ.get("QBO_PROD_CLIENT_SECRET")

def get_qbo_oauth_credentials(env_override: str = None):
    """
    Return (client_id, client_secret) for the chosen QuickBooks environment.
    """
    env = (env_override or QBO_ENV).lower()
    if env == "production":
        return QBO_PROD_CLIENT_ID, QBO_PROD_CLIENT_SECRET
    return QBO_SANDBOX_CLIENT_ID, QBO_SANDBOX_CLIENT_SECRET
# ─────────────────────────────────────────────────────────────────

# Unified Google auth: always load from GOOGLE_TOKEN_JSON or token.json via _load_google_creds
# Unified Google auth: always load from GOOGLE_TOKEN_JSON or token.json via _load_google_creds
creds = _load_google_creds()
if not creds:
    raise RuntimeError(
        "No Google credentials. Set GOOGLE_TOKEN_JSON to your token.json contents, "
        "or place a valid token.json on disk."
    )

# Wire clients once, using the creds from _load_google_creds()
sh = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
service = build("sheets", "v4", credentials=creds, cache_discovery=False)
sheets  = service.spreadsheets()



@app.route("/rate", methods=["POST"])
@login_required_session
def rate_all_services():
    """
    Accepts:
      { 
        "to": { "name","phone","addr1","addr2","city","state","zip","country" },
        "packages": [ { "L":10,"W":10,"H":10,"weight":2 }, ... ]
      }
    Returns:
      [ { code, method, rate, currency, delivery }, ... ]
    """
    p = request.get_json(force=True) or {}
    to = p.get("to", {})
    pkgs = p.get("packages", [])
    try:
        options = ups_get_rate(to, pkgs, ask_all_services=True)
        return jsonify(options)  # an array that your Ship.jsx already expects
    except Exception as e:
        return jsonify([
            { "method": "Manual Shipping", "rate": "N/A", "delivery": "TBD" }
        ]), 200


@app.route("/api/updateStartTime", methods=["POST"])
@login_required_session
def update_start_time():
    try:
        data = request.get_json() or {}
        job_id = data.get("id")
        iso_start = data.get("startTime")         # ISO from client

        if not job_id or not iso_start:
            return jsonify({"error": "Missing job ID or start time"}), 400

        # Write ISO directly; sheet will show Z, UI will show ET
        update_embroidery_start_time_in_sheet(job_id, iso_start)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── In-memory caches & settings ────────────────────────────────────────────
# with CACHE_TTL = 0, every GET will hit Sheets directly
CACHE_TTL           = 30

# orders cache + timestamp
_orders_cache       = None
_orders_ts          = 0

# embroidery list cache + timestamp
_emb_cache          = None
_emb_ts             = 0

# manualState cache + timestamp (for placeholders & machine assignments)
_manual_state_cache = None
_manual_state_ts    = 0

# ID-column cache for updateStartTime
_id_cache           = None
_id_ts              = 0

# overview: upcoming jobs cache (keyed by days) + timestamp
_overview_upcoming_cache = {}   # { int(days): {"jobs": [...]} }
_overview_upcoming_ts    = 0.0

# overview: materials-needed cache + timestamp
_materials_needed_cache  = None  # {"vendors": [...]}
_materials_needed_ts     = 0.0

# ─── Google client singletons ───────────────────────────────────────────────
_sheets_service = None
_drive_service  = None
_service_ts     = 0
SERVICE_TTL     = 900  # 15 minutes
# ────────────────────────────────────────────────────────────────────────────

# ---- helpers: clear overview caches ----------------------------------------
def invalidate_materials_needed_cache():
    global _materials_needed_cache, _materials_needed_ts
    _materials_needed_cache = None
    _materials_needed_ts = 0.0

def invalidate_upcoming_cache():
    global _overview_upcoming_cache, _overview_upcoming_ts
    _overview_upcoming_cache = {}   # keyed by days
    _overview_upcoming_ts = 0.0
# ---------------------------------------------------------------------------

# ✅ You must define or update this function to match your actual Google Sheet logic
import traceback

def update_embroidery_start_time_in_sheet(order_id, new_start_time):
    try:
        sheet = sh.worksheet("Production Orders")
        data = sheet.get_all_records()
        col_names = sheet.row_values(1)
        id_col = col_names.index("Order #") + 1
        start_col = col_names.index("Embroidery Start Time") + 1

        for i, row in enumerate(data, start=2):
            if str(row.get("Order #")).strip() == str(order_id).strip():
                sheet.update_cell(i, start_col, new_start_time); invalidate_upcoming_cache()
                print(f"✅ Embroidery Start Time updated for Order #{order_id}")
                return

        print(f"⚠️ Order ID {order_id} not found in Embroidery List")
    except Exception as e:
        print(f"❌ Error updating start time for Order #{order_id}: {e}")




def fetch_sheet(spreadsheet_id, sheet_range, value_render_option="UNFORMATTED_VALUE"):
    svc = get_sheets_service().spreadsheets().values()
    with sheet_lock:
        res = svc.get(
            spreadsheetId=spreadsheet_id,
            range=sheet_range,
            valueRenderOption=value_render_option,
        ).execute()
    return res.get("values", [])


def write_sheet(spreadsheet_id, range_, values):
    service = get_sheets_service()
    body = {
        "range": range_,
        "majorDimension": "ROWS",
        "values": values,
    }
    return service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_,
        valueInputOption="USER_ENTERED",
        body=body
    ).execute()



def get_sheet_password():
    try:
        vals = fetch_sheet(SPREADSHEET_ID, "Manual State!J2:J2")
        return vals[0][0] if vals and vals[0] else ""
    except Exception:
        logger.exception("Failed to fetch sheet password")
        return ""


# ─── Minimal Login Page (HTML) ───────────────────────────────────────────────
_login_page = """
<!DOCTYPE html>
<html>
<head><title>Login</title></head>
<body>
  <h2>Login</h2>
  {% if error %}
    <p style="color: red;">{{ error }}</p>
  {% endif %}
  <form method="POST">
    <input type="text" name="username" placeholder="Username" required><br><br>
    <input type="password" name="password" placeholder="Password" required><br><br>
    <input type="hidden" name="next" value="{{ next }}">
    <button type="submit">Login</button>
  </form>
</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    # always default back to your front-end
    next_url = request.args.get("next") or request.form.get("next") or FRONTEND_URL

    if request.method == "POST":
        u = request.form["username"]
        p = request.form["password"]
        ADMIN_PW    = os.environ.get("ADMIN_PASSWORD", "")
        ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

        print("🔑 ADMIN_PASSWORD env is:", repr(ADMIN_PW))
        if u == "admin":
            session.clear()
            session["user"] = u
            session["token_at_login"] = ADMIN_TOKEN
            session["last_activity"] = datetime.utcnow().isoformat()
            session["login_time"]     = time.time()
            session["login_ts"]      = datetime.utcnow().timestamp()
            # **new**: stamp when they logged in
            session["login_time"] = time.time()

            # if they wanted a backend URL, just send them home
            if next_url.startswith("/"):
                return redirect(FRONTEND_URL)
            return redirect(next_url)
        else:
            error = "Invalid credentials"
    
    return render_template_string(_login_page, error=error, next=next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/api/overview")
@login_required_session
def overview_combined():
    """
    Fast combined overview:
      - Upcoming:  Overview!A3:K  (11 columns pre-filtered/sorted in Sheet)
      - Materials: Overview!M3:M  (single text column: 'Item Qty Unit - Vendor')
    Returns: { upcoming: [...], materials: [...] }
    """
    try:
        svc = get_sheets_service().spreadsheets().values()
        vr = svc.batchGet(
            spreadsheetId=SPREADSHEET_ID,
            ranges=["Overview!A3:K", "Overview!M3:M"],
            valueRenderOption="FORMATTED_VALUE"
        ).execute().get("valueRanges", [])

        # ---- Upcoming rows (already filtered/sorted in Sheet)
        up_vals = (vr[0].get("values") if len(vr) > 0 else []) or []

        TARGET_HEADERS = ["Order #","Preview","Company Name","Design","Quantity","Product","Stage","Due Date","Print","Ship Date","Hard Date/Soft Date"]

        # Drop header row if present
        if up_vals:
            first_row = [str(x or "").strip() for x in up_vals[0]]
            if [h.strip().lower() for h in first_row] == [h.strip().lower() for h in TARGET_HEADERS]:
                up_vals = up_vals[1:]

        headers = TARGET_HEADERS

        def drive_thumb(link, size="w160"):
            s = str(link or "")
            fid = ""
            if "id=" in s:
                fid = s.split("id=")[-1].split("&")[0]
            elif "/file/d/" in s:
                fid = s.split("/file/d/")[-1].split("/")[0]
            return f"https://drive.google.com/thumbnail?id={fid}&sz={size}" if fid else ""

        # Build a lightweight map: Order # -> Image link from Production Orders
        # 1) Read header row to find the "Image" column index
        po_hdr = (get_sheets_service().spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Production Orders!A1:AM1",
            valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [[]]) or [[]])[0]

        img_idx = -1
        for i, h in enumerate(po_hdr):
            if str(h or "").strip().lower() == "image":
                img_idx = i
                break

        order_to_image = {}

        if img_idx >= 0:
            # Convert 0-based index -> column letter
            def col_letter(idx0):
                n = idx0 + 1
                s = ""
                while n:
                    n, r = divmod(n - 1, 26)
                    s = chr(65 + r) + s
                return s

            img_col = col_letter(img_idx)

            # 2) Fetch Order # (A) and Image column only (compact)
            pr = get_sheets_service().spreadsheets().values().batchGet(
                spreadsheetId=SPREADSHEET_ID,
                ranges=[ "Production Orders!A2:A", f"Production Orders!{img_col}2:{img_col}" ],
                valueRenderOption="FORMATTED_VALUE"
            ).execute().get("valueRanges", [])

            orders_col = (pr[0].get("values") if len(pr) > 0 else []) or []
            images_col = (pr[1].get("values") if len(pr) > 1 else []) or []

            max_len = max(len(orders_col), len(images_col))
            for i in range(max_len):
                ord_val = (orders_col[i][0] if i < len(orders_col) and orders_col[i] else "").strip()
                img_val = (images_col[i][0] if i < len(images_col) and images_col[i] else "").strip()
                if ord_val:
                    order_to_image[ord_val] = img_val

        def prefer_image(preview, order_no):
            # Prefer Production Orders → Image; else fall back to Preview-derived thumb
            img = order_to_image.get(str(order_no).strip(), "")
            return drive_thumb(img) or drive_thumb(preview)

        upcoming = []
        for r in up_vals:
            r = r + [""] * (len(headers) - len(r))
            row = dict(zip(headers, r))
            row["image"] = prefer_image(row.get("Preview"), row.get("Order #"))
            upcoming.append(row)

        # ---- Materials lines (single text column)
        mat_vals = (vr[1].get("values") if len(vr) > 1 else []) or []
        lines = [str(a[0]).strip() for a in mat_vals if a and str(a[0]).strip()]
        # Group by vendor from "Item … - Vendor"
        grouped = {}
        for s in lines:
            vendor = "Misc."
            left = s
            if " - " in s:
                left, vendor = s.rsplit(" - ", 1)
                vendor = vendor.strip() or "Misc."
            toks = left.split()
            name, qty, unit = left, 0, ""
            if len(toks) >= 3:
                unit = toks[-1]
                qty_str = toks[-2]
                name = " ".join(toks[:-2]).strip()
                try:
                    qty = int(round(float(qty_str)))
                except Exception:
                    qty = 0
            typ = "Thread" if unit.lower().startswith("cone") else "Material"
            if name:
                grouped.setdefault(vendor, []).append({"name": name, "qty": qty, "unit": unit, "type": typ})

        materials = [{"vendor": v, "items": items} for v, items in grouped.items()]

        # optional 60s cache using your existing globals
        if CACHE_TTL:
            import time as _t
            global _overview_upcoming_cache, _overview_upcoming_ts
            global _materials_needed_cache, _materials_needed_ts
            _overview_upcoming_cache[7] = {"jobs": upcoming}
            _overview_upcoming_ts = _t.time()
            _materials_needed_cache = {"vendors": materials}
            _materials_needed_ts = _t.time()

        return jsonify({"upcoming": upcoming, "materials": materials})

    except Exception as e:
        app.logger.exception("overview_combined failed")
        return jsonify({"error": "overview failed"}), 500


import traceback
from googleapiclient.errors import HttpError

@app.route("/api/overview/upcoming")
@login_required_session
def overview_upcoming():
    debug = request.args.get("debug") == "1"
    try:
        svc = get_sheets_service().spreadsheets().values()
        vals = svc.get(
            spreadsheetId=SPREADSHEET_ID,
            range="Overview!A3:K",
            valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])

        headers = ["Order #","Preview","Company Name","Design","Quantity","Product","Stage","Due Date","Print","Ship Date","Hard Date/Soft Date"]
        def thumb(link):
            s = str(link or "")
            fid = ""
            if "id=" in s: fid = s.split("id=")[-1].split("&")[0]
            elif "file/d/" in s: fid = s.split("file/d/")[-1].split("/")[0]
            return f"https://drive.google.com/thumbnail?id={fid}&sz=w160" if fid else ""

        jobs = []
        for r in vals:
            r = r + [""] * (len(headers) - len(r))
            row = dict(zip(headers, r))
            row["image"] = thumb(row.get("Preview"))
            jobs.append(row)

        # optional cache
        if CACHE_TTL:
            import time as _t
            global _overview_upcoming_cache, _overview_upcoming_ts
            _overview_upcoming_cache[7] = {"jobs": jobs}
            _overview_upcoming_ts = _t.time()
            return jsonify(_overview_upcoming_cache[7])

        if debug:
            return jsonify({"count": len(jobs), "first": jobs[0] if jobs else None})
        return jsonify({"jobs": jobs})

    except Exception as e:
        app.logger.exception("overview_upcoming failed")
        if debug:
            import traceback
            return jsonify({"error": str(e), "trace": traceback.format_exc()}), 200
        return jsonify({"error": "overview_upcoming failed"}), 500


from math import ceil
import re
import traceback
from googleapiclient.errors import HttpError

@app.route("/api/overview/materials-needed")
@login_required_session
def overview_materials_needed():
    debug = request.args.get("debug") == "1"
    try:
        svc = get_sheets_service().spreadsheets().values()
        resp = svc.get(
            spreadsheetId=SPREADSHEET_ID,
            range="Overview!M3:M",
            valueRenderOption="FORMATTED_VALUE"
        ).execute()
        vals = resp.get("values", [])
        lines = [str(r[0]).strip() for r in vals if r and str(r[0]).strip()]

        grouped = {}
        for s in lines:
            # Split "Item Qty Unit - Vendor"
            vendor = "Misc."
            left = s
            if " - " in s:
                left, vendor = s.rsplit(" - ", 1)
                vendor = vendor.strip() or "Misc."

            tokens = left.split()
            # Expect ... <qty> <unit> at the end
            if len(tokens) >= 3:
                unit = tokens[-1]
                qty_str = tokens[-2]
                name = " ".join(tokens[:-2]).strip()
                try:
                    qty = int(round(float(qty_str)))
                except Exception:
                    qty = 0
            else:
                name, qty, unit = left, 0, ""

            if not name:
                continue
            typ = "Thread" if unit.lower().startswith("cone") else "Material"
            grouped.setdefault(vendor, []).append({
                "name": name, "qty": qty, "unit": unit, "type": typ
            })

        vendor_list = [{"vendor": v, "items": items} for v, items in grouped.items()]

        # optional cache
        if CACHE_TTL:
            import time as _t
            global _materials_needed_cache, _materials_needed_ts
            _materials_needed_cache = {"vendors": vendor_list}
            _materials_needed_ts = _t.time()
            return jsonify(_materials_needed_cache)

        if debug:
            return jsonify({"count_vendors": len(vendor_list), "sample": vendor_list[:1]})
        return jsonify({"vendors": vendor_list})

    except Exception as e:
        app.logger.exception("materials-needed failed")
        if debug:
            import traceback
            return jsonify({"error": str(e), "trace": traceback.format_exc()}), 200
        return jsonify({"error": "materials-needed failed"}), 500

# ─── API ENDPOINTS ────────────────────────────────────────────────────────────

@app.route("/api/orders", methods=["GET"])
@login_required_session
def get_orders():
    try:
        rows = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE)
        headers = rows[0] if rows else []
        data    = [dict(zip(headers,r)) for r in rows[1:]] if rows else []
        return jsonify(data), 200
    except Exception:
        logger.exception("Error fetching orders")
        return jsonify([]), 200



@app.route("/api/prepare-shipment", methods=["POST"])
@login_required_session
def prepare_shipment():
    data = request.get_json()
    order_ids = data.get("order_ids", [])
    if not order_ids:
        return jsonify({"error": "Missing order_ids"}), 400

    # Fetch both Production Orders and Table tabs
    prod_data = fetch_sheet(SPREADSHEET_ID, "Production Orders!A1:AM")
    table_data = fetch_sheet(SPREADSHEET_ID, "Table!A1:Z")

    prod_headers = prod_data[0]
    table_headers = table_data[0]

    # ✅ Moved up so it exists before being used
    shipped_quantities = data.get("shipped_quantities", {})

    # Step 1: Find orders that match the given Order #
    prod_rows = []
    all_order_data = []  # ✅ declare this before using it

    for row in prod_data[1:]:
        row_dict = dict(zip(prod_headers, row))
        order_id = str(row_dict.get("Order #")).strip()

        if order_id in order_ids:
            parsed_qty = shipped_quantities.get(order_id, 0)
            headers = list(row_dict.keys())
            order_data = {
                h: (
                    str(parsed_qty) if h == "Shipped" else
                    row_dict.get(h, "")
                )
                for h in headers
            }
            order_data["ShippedQty"] = parsed_qty
            all_order_data.append(order_data)
            prod_rows.append(row_dict)

    # Step 2: Create product→volume map
    table_map = {}
    for r in table_data[1:]:
        if len(r) >= 2:
            product = r[0]
            volume_str = r[13] if len(r) >= 14 else r[1]  # use column N if available
            try:
                volume = float(volume_str)
                table_map[product.strip().lower()] = volume
            except:
                continue

    # Step 3: Check for missing volumes and build job list
    missing_products = []
    jobs = []

    for row in prod_rows:
        order_id = str(row["Order #"])
        product  = row.get("Product","").strip()
        # ── IGNORE Back jobs entirely in prepare-shipment ──
        if re.search(r"\s+Back$", product, flags=re.IGNORECASE):
            continue
        key = product.lower()
        volume = table_map.get(key)

        if volume is None:
            missing_products.append(product)
        else:
            # Use shipQty from frontend if provided, fallback to Quantity column
            raw_ship_qty = shipped_quantities.get(order_id, row.get("Quantity", 0))
            try:
                ship_qty = int(raw_ship_qty)
            except:
                ship_qty = 0

            # parse physical dimensions (in inches) from your sheet
            length = float(row_dict.get("Length", 0)   or 0)
            width  = float(row_dict.get("Width",  0)   or 0)
            height = float(row_dict.get("Height", 0)   or 0)
            jobs.append({
                "order_id":  order_id,
                "product":   product,
                "volume":    volume,
                "dimensions": (length, width, height),
                "ship_qty":  ship_qty
            })

    if missing_products:
        return jsonify({
            "error": "Missing volume data",
            "missing_products": list(set(missing_products))
        }), 400

    # Step 4: Pack items into as few boxes as possible, respecting both vol & dims
    BOX_TYPES = [
        {"size": "Small",  "dims": (10, 10, 10), "vol": 10*10*10},
        {"size": "Medium", "dims": (15, 15, 15), "vol": 15*15*15},
        {"size": "Large",  "dims": (20, 20, 20), "vol": 20*20*20},
    ]

    def can_fit(prod_dims, box_dims):
        p_sorted = sorted(prod_dims)
        b_sorted = sorted(box_dims)
        return all(p <= b for p, b in zip(p_sorted, b_sorted))

    # 4a) Expand each item by its quantity
    items = []
    for job in jobs:
        for _ in range(job["ship_qty"]):
            items.append({
                "order_id": job["order_id"],
                "dims":      job["dimensions"],
                "volume":    job["volume"]
            })

    # 4b) Greedily fill boxes
    boxes = []
    while items:
        # start a new box with the first item
        group     = [items.pop(0)]
        total_vol = group[0]["volume"]
        max_dims  = list(group[0]["dims"])

        # try to pack more items into this same box
        i = 0
        while i < len(items):
            it       = items[i]
            new_dims = (
                max(max_dims[0], it["dims"][0]),
                max(max_dims[1], it["dims"][1]),
                max(max_dims[2], it["dims"][2]),
            )
            # check against the largest box capacity and dims
            largest = BOX_TYPES[-1]
            if (total_vol + it["volume"] <= largest["vol"]
                and can_fit(new_dims, largest["dims"])):
                total_vol += it["volume"]
                max_dims  = list(new_dims)
                group.append(it)
                items.pop(i)
                continue
            i += 1

        # 4c) Choose the smallest box that fits this group
        eligible = [
            b for b in BOX_TYPES
            if can_fit(max_dims, b["dims"]) and b["vol"] >= total_vol
        ]
        eligible.sort(key=lambda b: b["vol"])
        chosen = (eligible[0]["size"] if eligible else BOX_TYPES[-1]["size"])

        boxes.append({
            "size": chosen,
            "jobs": [g["order_id"] for g in group]
        })

    return jsonify({
        "status": "ok",
        "boxes": boxes
    })
@app.route("/api/jobs-for-company")
@login_required_session
def jobs_for_company():
    company = request.args.get("company", "").strip().lower()
    if not company:
        return jsonify({"error": "Missing company parameter"}), 400

    prod_data = fetch_sheet(SPREADSHEET_ID, "Production Orders!A1:AM")
    headers = prod_data[0]
    jobs = []

    for row in prod_data[1:]:
        # ✅ Pad the row so it matches the length of headers
        while len(row) < len(headers):
            row.append("")

        row_dict = dict(zip(headers, row))
        row_company = str(row_dict.get("Company Name", "")).strip().lower()
        stage = str(row_dict.get("Stage", "")).strip().lower()

        if row_company == company and stage != "complete":
            # Parse Google Drive file ID from image link
            image_link = str(row_dict.get("Image", "")).strip()
            file_id = ""
            if "id=" in image_link:
                file_id = image_link.split("id=")[-1].split("&")[0]
            elif "file/d/" in image_link:
                file_id = image_link.split("file/d/")[-1].split("/")[0]

            preview_url = (
                f"https://drive.google.com/thumbnail?id={file_id}"
                if file_id else ""
            )

            # Add required fields to job dict
            row_dict["image"] = preview_url
            row_dict["orderId"] = str(row_dict.get("Order #", "")).strip()
            jobs.append(row_dict)

    return jsonify({ "jobs": jobs })

@app.route("/api/set-volume", methods=["POST"])
def set_volume():
    global SPREADSHEET_ID
    data = request.get_json()
    product = data.get("product")
    length  = data.get("length")
    width   = data.get("width")
    height  = data.get("height")

    if not all([product, length, width, height]):
        return jsonify({"error": "Missing fields"}), 400

    try:
        volume = int(length) * int(width) * int(height)
    except ValueError:
        return jsonify({"error": "Invalid dimensions"}), 400

    sheets = get_sheets_service().spreadsheets()
    table_range = "Table!A2:A"

    # ← now correctly using SPREADSHEET_ID
    result = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=table_range
    ).execute()
    rows = result.get("values", [])
    products = [row[0] for row in rows if row]

    if product in products:
        row_index = products.index(product) + 2
    else:
        row_index = len(products) + 2
        sheets.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Table!A2",
            valueInputOption="RAW",
            body={"values": [[product]]}
        ).execute()

    update_range = f"Table!N{row_index}"
    sheets.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=update_range,
        valueInputOption="RAW",
        body={"values": [[volume]]}
    ).execute()

    return jsonify({"message": "Volume saved", "volume": volume})

@app.route("/api/embroideryList", methods=["GET"])
@login_required_session
def get_embroidery_list():
    try:
        # ─── Spot A: CACHE CHECK ────────────────────────────────────
        global _emb_cache, _emb_ts
        now = time.time()
        if _emb_cache is not None and (now - _emb_ts) < 10:
            return jsonify(_emb_cache), 200

        # ─── Fetch the full sheet ──────────────────────────────────
        rows = fetch_sheet(SPREADSHEET_ID, EMBROIDERY_RANGE)
        if not rows:
            return jsonify([]), 200

        headers = rows[0]
        data = []
        for r in rows[1:]:
            row = dict(zip(headers, r))
            data.append(row)

        # ─── Spot B: CACHE STORE ───────────────────────────────────
        _emb_cache = data
        _emb_ts = now

        return jsonify(data), 200

    except Exception:
        logger.exception("Error fetching embroidery list")
        return jsonify([]), 200

# ─── GET A SINGLE ORDER ───────────────────────────────────────────
@app.route("/api/orders/<order_id>", methods=["GET"])
@login_required_session
def get_order(order_id):
    rows    = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE)
    headers = rows[0]
    for row in rows[1:]:
        if str(row[0]) == str(order_id):
            return jsonify(dict(zip(headers, row))), 200
    return jsonify({"error":"order not found"}), 404


@app.route("/api/orders/<order_id>", methods=["PUT"])
@login_required_session
def update_order(order_id):
    data = request.get_json(silent=True) or {}
    try:
        with sheet_lock:
            sheets.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"Production Orders!H{order_id}",
                valueInputOption="RAW",
                body={"values": [[ data.get("embroidery_start","") ]]}
            ).execute()
        socketio.emit("orderUpdated", {"orderId": order_id})
        return jsonify({"status":"ok"}), 200
    except Exception:
        logger.exception("Error updating order")
        return jsonify({"error":"server error"}), 500

# In-memory links
_links_store = {}

@app.route("/api/links", methods=["GET"])
@login_required_session
def get_links():
    return jsonify(_links_store), 200

@app.route("/api/links", methods=["POST"])
@login_required_session
def save_links():
    global _links_store
    _links_store = request.get_json() or {}
    socketio.emit("linksUpdated", _links_store)
    return jsonify({"status":"ok"}), 200

@socketio.on("placeholdersUpdated")
def handle_placeholders_updated(data):
    socketio.emit("placeholdersUpdated", data, broadcast=True)

# ─── MANUAL STATE ENDPOINTS (multi-row placeholders) ─────────────────────────
MANUAL_RANGE       = "Manual State!A2:Z"
MANUAL_CLEAR_RANGE = "Manual State!A2:Z"

# ─── MANUAL STATE ENDPOINT (GET) ───────────────────────────────────────────────
@app.route("/api/manualState", methods=["GET"])
@login_required_session
def get_manual_state():
    global _manual_state_cache, _manual_state_ts
    now = time.time()
    if _manual_state_cache is not None and (now - _manual_state_ts) < CACHE_TTL:
        return jsonify(_manual_state_cache), 200

    try:
        # fetch A2:Z...
        resp = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=MANUAL_RANGE  # "Manual State!A2:Z"
        ).execute()
        rows = resp.get("values", [])

        # pad each row to 26 cols (A–Z)
        for i in range(len(rows)):
            while len(rows[i]) < 26:
                rows[i].append("")

        # first row: machine lists in I–Z (cols 8–25)
        first = rows[0] if rows else [""] * 26
        machines = first[8:26]
        machine_columns = [[s for s in str(col or "").split(",") if s] for col in machines]

        # rest: placeholders from A–H (cols 0–7)
        phs = []
        for r in rows:
            if str(r[0]).strip():  # only if ID in col A
                phs.append({
                    "id":          r[0],
                    "company":     r[1],
                    "quantity":    r[2],
                    "stitchCount": r[3],
                    "inHand":      r[4],
                    "dueType":     r[5],
                    "fieldG":      r[6],
                    "fieldH":      r[7]
                })

        result = {
            "machineColumns": machine_columns,
            "placeholders":   phs
        }

        _manual_state_cache = result
        _manual_state_ts    = now
        return jsonify(result), 200

    except Exception:
        logger.exception("Error reading manual state")
        if _manual_state_cache:
            return jsonify(_manual_state_cache), 200
        return jsonify({"machineColumns": [], "placeholders": []}), 200


# ─── MANUAL STATE ENDPOINT (POST) ──────────────────────────────────────────────
@app.route("/api/manualState", methods=["POST"])
@login_required_session
def save_manual_state():
    global _manual_state_cache, _manual_state_ts

    try:
        data = request.get_json(silent=True) or {}
        phs  = data.get("placeholders", [])
        m1   = data.get("machine1", [])
        m2   = data.get("machine2", [])

        # 1) Clear A2:Z
        sheets.values().clear(
            spreadsheetId=SPREADSHEET_ID,
            range=MANUAL_CLEAR_RANGE  # "Manual State!A2:Z"
        ).execute()

        # 2) Build new rows
        rows = []

        # --- first row: placeholders[0] in A–H, machines in I & J, rest blank
        first = []
        if phs:
            p0 = phs[0]
            first += [
                p0.get("id",""),
                p0.get("company",""),
                str(p0.get("quantity","")),
                str(p0.get("stitchCount","")),
                p0.get("inHand",""),
                p0.get("dueType",""),
                p0.get("fieldG",""),
                p0.get("fieldH","")
            ]
        else:
            first += [""] * 8
        # columns I & J
        first.append(",".join(m1))
        first.append(",".join(m2))
        # columns K–Z blank
        first += [""] * (26 - len(first))
        rows.append(first)

        # --- subsequent placeholders in A–H, I–Z blank
        for p in phs[1:]:
            row = [
                p.get("id",""),
                p.get("company",""),
                str(p.get("quantity","")),
                str(p.get("stitchCount","")),
                p.get("inHand",""),
                p.get("dueType",""),
                p.get("fieldG",""),
                p.get("fieldH","")
            ]
            row += [""] * 18  # fill cols I–Z
            rows.append(row)

        # 3) Write back A2:Z{end_row}
        num = max(1, len(rows))
        end_row = 2 + num - 1
        sheet_name = MANUAL_CLEAR_RANGE.split("!")[0]
        write_range = f"{sheet_name}!A2:Z{end_row}"
        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=write_range,
            valueInputOption="RAW",
            body={"values": rows}
        ).execute()

        # 4) Update cache & broadcast
        _manual_state_cache = {
            "machineColumns": [m1, m2],  # still expose as list-of-lists
            "placeholders": phs
        }
        _manual_state_ts = time.time()
        socketio.emit("manualStateUpdated", _manual_state_cache)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logger.exception("Error in save_manual_state")
        return jsonify({"status": "error", "message": str(e)}), 500


# ─────────────────────────────────────────────
# helper: grant “anyone with link” reader access
def make_public(file_id, drive_service):
    drive_service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
        fields="id"
    ).execute()

@app.route("/submit", methods=["OPTIONS","POST"])
def submit_order():
    if request.method == "OPTIONS":
        return make_response("", 204)

    try:
        data               = request.form
        prod_files         = request.files.getlist("prodFiles")
        print_files        = request.files.getlist("printFiles")

        # ─── EARLY VALIDATION ────────────────────────────────────────────────
        materials          = data.getlist("materials")
        material_percents  = data.getlist("materialPercents")
        # pad both lists to exactly 5 entries
        materials[:]         = (materials + [""] * 5)[:5]
        material_percents[:] = (material_percents + [""] * 5)[:5]

        # 1) Material1 is mandatory
        if not materials[0].strip():
            return jsonify({"error": "Material1 is required."}), 400

        # 2) If product contains “full”, backMaterial is mandatory
        product_lower = (data.get("product") or "").strip().lower()
        if "full" in product_lower and not data.get("backMaterial","").strip():
            return jsonify({"error": "Back Material is required for “Full” products."}), 400

        # 3) Every populated material must have its percent
        for idx, mat in enumerate(materials):
            if mat.strip():
                pct = material_percents[idx].strip()
                if not pct:
                    return jsonify({
                        "error": f"Percentage for Material{idx+1} (“{mat}”) is required."
                    }), 400
                try:
                    float(pct)
                except ValueError:
                    return jsonify({
                        "error": f"Material{idx+1} percentage (“{pct}”) must be a number."
                    }), 400

        # ─── DETERMINE NEXT ROW & ORDER # ────────────────────────────────────
        col_a      = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Production Orders!A:A"
        ).execute().get("values", [])
        next_row   = len(col_a) + 1
        prev_order = int(col_a[-1][0]) if len(col_a) > 1 else 0
        new_order  = prev_order + 1

        # ─── TEMPLATE FORMULAS ───────────────────────────────────────────────
        def tpl_formula(col_letter, target_row):
            resp = sheets.values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"Production Orders!{col_letter}2",
                valueRenderOption="FORMULA"
            ).execute()
            raw = resp.get("values", [[""]])[0][0] or ""
            return re.sub(r"(\b[A-Z]+)2\b",
                          lambda m: f"{m.group(1)}{target_row}",
                          raw)

        ts           = datetime.now(ZoneInfo("America/New_York")).strftime("%-m/%-d/%Y %H:%M:%S")
        preview      = tpl_formula("C", next_row)
        stage        = tpl_formula("I", next_row)
        ship_date    = tpl_formula("V", next_row)
        stitch_count = tpl_formula("W", next_row)
        schedule_str = tpl_formula("AC", next_row)

        # ─── DRIVE FOLDER HELPERS ────────────────────────────────────────────
        drive = get_drive_service()

        def create_folder(name, parent_id=None):
            query = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            if parent_id:
                query += f" and '{parent_id}' in parents"
            existing = drive.files().list(q=query, fields="files(id)").execute().get("files", [])
            for f in existing:
                drive.files().delete(fileId=f["id"]).execute()
            meta = {"name": str(name), "mimeType": "application/vnd.google-apps.folder"}
            if parent_id:
                meta["parents"] = [parent_id]
            folder = drive.files().create(body=meta, fields="id").execute()
            return folder["id"]

        def make_public(file_id):
            drive.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"}
            ).execute()

        # ─── CREATE ORDER FOLDER & UPLOAD ────────────────────────────────────
        order_folder_id = create_folder(new_order, parent_id="1n6RX0SumEipD5Nb3pUIgO5OtQFfyQXYz")
        make_public(order_folder_id)

        # if reorderFrom, copy .emb files
        if data.get("reorderFrom"):
            copy_emb_files(
                old_order_num = data["reorderFrom"],
                new_order_num = new_order,
                drive_service = drive,
                new_folder_id = order_folder_id
            )

        prod_links = []
        for f in prod_files:
            m  = MediaIoBaseUpload(f.stream, mimetype=f.mimetype)
            up = drive.files().create(
                body={"name": f.filename, "parents": [order_folder_id]},
                media_body=m,
                fields="id,webViewLink"
            ).execute()
            make_public(up["id"])
            prod_links.append(up["webViewLink"])

        print_links = ""
        if print_files:
            pf_id = create_folder("Print Files", parent_id=order_folder_id)
            make_public(pf_id)
            for f in print_files:
                m = MediaIoBaseUpload(f.stream, mimetype=f.mimetype)
                drive.files().create(
                    body={"name": f.filename, "parents": [pf_id]},
                    media_body=m,
                    fields="id"
                ).execute()
            print_links = f"https://drive.google.com/drive/folders/{pf_id}"

        # ─── ASSEMBLE & WRITE ROW A→AK ───────────────────────────────────────
        row = [
            new_order, ts, preview,
            data.get("company"), data.get("designName"), data.get("quantity"),
            "",  # shipped
            data.get("product"), stage, data.get("price"),
            data.get("dueDate"), ("PRINT" if prod_links else "NO"),
            *materials,                  # M–Q
            data.get("backMaterial"), data.get("furColor"),
            data.get("embBacking",""), "",  # top stitch blank
            ship_date, stitch_count,
            data.get("notes"),
            ",".join(prod_links),
            print_links,
            "",  # AA blank
            data.get("dateType"),
            schedule_str,
            "", "", "",                 # AD, AE, AF – pad so percents start at AG
            *material_percents           # AG–AK
        ]

        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Production Orders!A{next_row}:AK{next_row}",
            valueInputOption="USER_ENTERED",
            body={"values": [row]}
        ).execute()

        # ─── COPY AF2 FORMULA DOWN ─────────────────────────────────────────
        resp = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Production Orders!AF2",
            valueRenderOption="FORMULA"
        ).execute()
        raw_f = resp.get("values",[[""]])[0][0] or ""
        new_f = raw_f.replace("2", str(next_row))
        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Production Orders!AF{next_row}",
            valueInputOption="USER_ENTERED",
            body={"values": [[new_f]]}
        ).execute(); invalidate_upcoming_cache()

        return jsonify({"status":"ok","order":new_order}), 200

    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Error in /submit:\n%s", tb)
        return jsonify({"error": str(e), "trace": tb}), 500

@app.route("/api/reorder", methods=["POST"])
@login_required_session
def reorder():
    data = request.get_json(silent=True) or {}
    prev_id = data.get("previousOrder")

    if not prev_id:
        return jsonify({"error": "Missing previous order ID"}), 400

    # This endpoint now just acknowledges reorder intent.
    # Actual reorder is handled via /submit with prefilled data.
    print(f"↪️ Received reorder request for previous order #{prev_id}. Redirecting to /submit flow.")
    return jsonify({"status": "ok", "message": f"Reorder initiated for #{prev_id}"}), 200


@app.route("/api/directory", methods=["GET"])
@login_required_session
def get_directory():
    """
    Returns JSON array of company names from the 'Directory' sheet.
    """
    try:
        # read column A (Company Name) from row 2 down
        rows = fetch_sheet(SPREADSHEET_ID, "Directory!A2:A")
        # flatten and filter out empty cells
        companies = [r[0] for r in rows if r and r[0].strip()]
        return jsonify(companies), 200
    except Exception:
        logger.exception("Error fetching directory")
        return jsonify([]), 200

@app.route("/api/directory", methods=["POST"])
@login_required_session
def add_directory_entry():
    """
    Appends a new row to the Directory sheet.
    Expects JSON with keys:
      companyName,
      contactFirstName,
      contactLastName,
      contactEmailAddress,
      streetAddress1,
      streetAddress2,
      city,
      state,
      zipCode,
      phoneNumber
    """
    try:
        data = request.get_json(force=True)
    except Exception as e:
        print("❌ Failed to parse JSON:", e)
        return jsonify({"error": "Invalid JSON"}), 400

    print("📥 Incoming /api/reorder payload:", data)
    try:
        # build the row in the same order as your sheet columns A→J
        row = [
            data.get("companyName", ""),
            data.get("contactFirstName", ""),
            data.get("contactLastName", ""),
            data.get("contactEmailAddress", ""),
            data.get("streetAddress1", ""),
            data.get("streetAddress2", ""),
            data.get("city", ""),
            data.get("state", ""),
            data.get("zipCode", ""),
            data.get("phoneNumber", ""),
        ]
        sheets.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Directory!A2:J",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        return jsonify({"status": "ok"}), 200
    except Exception:
        logger.exception("Error adding new company")
        return jsonify({"error": "Failed to add company"}), 500



@app.route("/api/fur-colors", methods=["GET"])
@login_required_session
def get_fur_colors():
    try:
        rows = fetch_sheet(SPREADSHEET_ID, "Material Inventory!I2:I")
        colors = [r[0] for r in rows if r and r[0].strip()]
        return jsonify(colors), 200
    except Exception:
        logger.exception("Error fetching fur colors")
        return jsonify([]), 200

# ─── Add /api/threads endpoint with dynamic formulas ────────────────────────
@app.route("/api/threads", methods=["POST"])
@login_required_session
def add_thread():
    try:
        # 1) Parse incoming JSON (single dict or list)
        raw   = request.get_json(silent=True) or []
        items = raw if isinstance(raw, list) else [raw]

        # 2) Find next empty row in Material Inventory column I
        resp     = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Material Inventory!I2:I"
        ).execute().get("values", [])
        next_row = len(resp) + 2

        # 3) Helper to fetch raw formula text
        def tpl(col, src_row):
            return sheets.values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"Material Inventory!{col}{src_row}",
                valueRenderOption="FORMULA"
            ).execute().get("values", [[""]])[0][0] or ""

        # 4) Copy raw formulas from J4, K4 and O2
        rawJ = tpl("J", 4)
        rawK = tpl("K", 4)
        rawO = tpl("O", 2)

        # 5) Rewrite only the row references:
        formulaJ = rawJ.replace("I4", f"I{next_row}")
        formulaK = rawK.replace("I4", f"I{next_row}")
        formulaO = rawO.replace("2", str(next_row))

        # 6) Loop through each item and write its row I→O
        added = 0
        for item in items:
            threadColor = item.get("threadColor", "").strip()
            minInv      = item.get("minInv",      "").strip()
            reorder     = item.get("reorder",     "").strip()
            cost        = item.get("cost",        "").strip()

            # skip any empty entries
            if not threadColor:
                continue

            sheets.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"Material Inventory!I{next_row}:O{next_row}",
                valueInputOption="USER_ENTERED",
                body={"values": [[
                    threadColor,
                    formulaJ,
                    formulaK,
                    minInv,
                    reorder,
                    cost,
                    formulaO
                ]]}
            ).execute()

            added    += 1
            next_row += 1

        return jsonify({"added": added}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/table", methods=["POST"])
@login_required_session
def add_table_entry():
    data = request.get_json(silent=True) or {}

    # 1) Extract your original 11 fields + 3 new ones
    product_name        = data.get("product", "").strip()        # → A
    print_time          = data.get("printTime", "")             # → D
    per_yard            = data.get("perYard", "")               # → F
    foam_half           = data.get("foamHalf", "")              # → G
    foam_38             = data.get("foam38", "")                # → H
    foam_14             = data.get("foam14", "")                # → I
    foam_18             = data.get("foam18", "")                # → J
    n_magnets           = data.get("magnetN", "")               # → K
    s_magnets           = data.get("magnetS", "")               # → L
    elastic_half_length = data.get("elasticHalf", "")           # → M
    volume              = data.get("volume", "")                # → N

    # ← New pouch-specific fields:
    black_grommets      = data.get("blackGrommets", "")         # → O
    paracord_ft         = data.get("paracordFt", "")            # → P
    cord_stoppers       = data.get("cordStoppers", "")          # → Q

    if not product_name:
        return jsonify({"error": "Missing product name"}), 400

    try:
        # 2) Append into Table!A2:Q2 (now 17 cols A–Q)
        sheets.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Table!A2:Q2",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={
                "values": [[
                    product_name,        # A
                    "",                  # B
                    "",                  # C
                    print_time,          # D
                    "",                  # E
                    per_yard,            # F
                    foam_half,           # G
                    foam_38,             # H
                    foam_14,             # I
                    foam_18,             # J
                    n_magnets,           # K
                    s_magnets,           # L
                    elastic_half_length, # M
                    volume,              # N
                    black_grommets,      # O
                    paracord_ft,         # P
                    cord_stoppers        # Q
                ]]
            }
        ).execute()

        return jsonify({"status": "ok", "product": product_name}), 200

    except Exception as e:
        logger.exception("Failed to append new product to Table")
        return jsonify({"error": str(e)}), 500


@app.route("/api/table", methods=["GET"])
@login_required_session
def get_table():
    try:
        rows = fetch_sheet(SPREADSHEET_ID, "Table!A1:Z")
        headers = rows[0]
        data = [dict(zip(headers, r)) for r in rows[1:]]
        return jsonify(data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── MATERIALS ENDPOINTS ────────────────────────────────────────────────

from flask import make_response  # if not already imported

# ─── MATERIALS ENDPOINTS ────────────────────────────────────────────────

@app.route("/api/materials", methods=["OPTIONS"])
def materials_preflight():
    return make_response("", 204)

# 2) GET list for your typeahead
@app.route("/api/materials", methods=["GET"])
@login_required_session
def get_materials():
    try:
        rows = fetch_sheet(SPREADSHEET_ID, "Material Inventory!A2:A")
        names = [r[0] for r in rows if r and r[0].strip()]
        return jsonify(names), 200
    except Exception:
        logger.exception("Error fetching materials")
        return jsonify([]), 200

# 3) POST new material(s) into Material Inventory!A–H
@app.route("/api/materials", methods=["POST"])
@login_required_session
def add_materials():
    raw   = request.get_json(silent=True) or []
    items = raw if isinstance(raw, list) else [raw]

    # fetch the raw formulas from row 2
    def get_formula(col):
        resp = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Material Inventory!{col}2",
            valueRenderOption="FORMULA"
        ).execute()
        return resp.get("values", [[""]])[0][0] or ""

    rawB = get_formula("B")
    rawC = get_formula("C")
    rawH = get_formula("H")

    now      = datetime.now(ZoneInfo("America/New_York"))\
                    .strftime("%-m/%-d/%Y %H:%M:%S")
    inv_rows = []
    
    # find where row 2 starts, so we can compute new rows dynamically
    existing = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Material Inventory!A2:A"
    ).execute().get("values", [])
    next_row = len(existing) + 2  # because A2 is first data row

    for it in items:
        name    = it.get("materialName","").strip()
        unit    = it.get("unit","").strip()
        mininv  = it.get("minInv","").strip()
        reorder = it.get("reorder","").strip()
        cost    = it.get("cost","").strip()

        if not name:
            continue

        # rewrite the row references in each formula
        formulaB = rawB.replace("2", str(next_row))
        formulaC = rawC.replace("2", str(next_row))
        formulaH = rawH.replace("2", str(next_row))

        inv_rows.append([
            name,       # A
            formulaB,   # B: uses rawB but with "2"→next_row
            formulaC,   # C: same
            unit,       # D: user entry
            mininv,     # E
            reorder,    # F
            cost,       # G
            formulaH    # H: uses rawH but with "2"→next_row
        ])

        next_row += 1
    # end for

    # append the rows you’ve built
    if inv_rows:
        sheets.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Material Inventory!A2:H",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": inv_rows}
        ).execute(); invalidate_upcoming_cache()

    return jsonify({"status":"submitted"}), 200


# ─── MATERIAL-LOG Preflight (OPTIONS) ─────────────────────────────────────
@app.route("/api/materialInventory", methods=["OPTIONS"])
def material_inventory_preflight():
    return make_response("", 204)

# ─── MATERIAL-LOG POST ────────────────────────────────────────────────────
@app.route("/api/materialInventory", methods=["POST"])
@login_required_session
def submit_material_inventory():
    """
    Handles adding new materials to Material Inventory (cols A–H),
    copying row 2 formulas dynamically, and logging:
      - Materials -> Material Log
      - Threads   -> Thread Data (by header name; NO writes to Thread Inventory)
    """
    try:
        # 1) Parse incoming payload
        items = request.get_json(silent=True) or []
        items = items if isinstance(items, list) else [items]

        sheet = sheets.values()
        timestamp = datetime.now(ZoneInfo("America/New_York")) \
            .strftime("%-m/%-d/%Y %H:%M:%S")

        material_log_rows = []
        thread_log_rows   = []

        # ========= Material Inventory setup (unchanged behavior) =========
        # 2) Fetch row 2 formulas for Material Inventory (A2:H2)
        mat_formula = sheet.get(
            spreadsheetId=SPREADSHEET_ID,
            range="Material Inventory!A2:H2",
            valueRenderOption="FORMULA"
        ).execute().get("values", [[]])[0]
        mat_inv_tpl  = str(mat_formula[1]) if len(mat_formula) > 1 else ""
        mat_oo_tpl   = str(mat_formula[2]) if len(mat_formula) > 2 else ""
        mat_val_tpl  = str(mat_formula[7]) if len(mat_formula) > 7 else ""

        # 3) Fetch existing rows for Material Inventory
        mat_rows = sheet.get(
            spreadsheetId=SPREADSHEET_ID,
            range="Material Inventory!A1:H1000"
        ).execute().get("values", [])[1:]  # skip header
        existing_mats = {r[0].strip().lower() for r in mat_rows if r and r[0].strip()}

        # Helper: find first blank row (1-based index + header) in a sheet's col A
        def first_blank_row(rows):
            return next((i + 2 for i, r in enumerate(rows)
                         if not r or not (r[0].strip() if len(r) > 0 else "")),
                        len(rows) + 2)

        # ========= Thread Data header-based mapping =========
        # Fetch Thread Data headers so we can place values by name
        td_rows = fetch_sheet(SPREADSHEET_ID, "Thread Data!A1:Z")
        td_headers = td_rows[0] if td_rows else []
        td_idx = {h: i for i, h in enumerate(td_headers)}

        def build_thread_log_row(dt, color, feet, action):
            """Map values into a row aligned to Thread Data headers."""
            row = [""] * len(td_headers)
            if "Date" in td_idx:         row[td_idx["Date"]] = dt
            if "Color" in td_idx:        row[td_idx["Color"]] = color
            if "Length (ft)" in td_idx:  row[td_idx["Length (ft)"]] = feet
            if "IN/OUT" in td_idx:       row[td_idx["IN/OUT"]] = "IN"
            if "O/R" in td_idx:          row[td_idx["O/R"]] = action  # Ordered/Received
            return row

        # 5) Process each item
        for it in items:
            name    = (it.get("materialName") or it.get("value") or "").strip()
            type_   = (it.get("type") or "").strip() or "Material"
            unit    = it.get("unit",    "").strip()
            min_inv = it.get("minInv",  "").strip()
            reorder = it.get("reorder", "").strip()
            cost    = it.get("cost",    "").strip()  # not used for threads per request
            action  = it.get("action",  "").strip()  # "Ordered" / "Received"
            qty_raw = it.get("quantity","")

            # must have name and quantity to do anything
            if not (name and str(qty_raw).strip()):
                continue

            if type_ == "Material":
                # === MATERIAL: add to Material Inventory if new, always log to Material Log ===
                is_new = name.lower() not in existing_mats
                if is_new:
                    target = first_blank_row(mat_rows)

                    # build formulas for the new target row
                    inv_f = re.sub(r"([A-Za-z]+)2", lambda m: f"{m.group(1)}{target}", mat_inv_tpl)
                    oo_f  = re.sub(r"([A-Za-z]+)2", lambda m: f"{m.group(1)}{target}", mat_oo_tpl)
                    val_f = re.sub(r"([A-Za-z]+)2", lambda m: f"{m.group(1)}{target}", mat_val_tpl)

                    row_vals = [[
                        name, inv_f, oo_f, unit,
                        min_inv, reorder, cost, val_f
                    ]]

                    # write the new inventory row
                    sheet.update(
                        spreadsheetId=SPREADSHEET_ID,
                        range=f"Material Inventory!A{target}:H{target}",
                        valueInputOption="USER_INPUT",
                        body={"values": row_vals}
                    ).execute()

                    # keep in-memory sets/rows up to date for subsequent items
                    existing_mats.add(name.lower())
                    while len(mat_rows) < target - 1:
                        mat_rows.append([])
                    mat_rows.append([name])

                # ALWAYS log the material movement (even if not new)
                material_log_rows.append([timestamp, "", "", "", "", name, str(qty_raw).strip(), "IN", action])

            else:
                # === THREAD: DO NOT write to "Thread Inventory"; ONLY log to "Thread Data" ===
                try:
                    cones = int(str(qty_raw).strip())
                    feet  = cones * 5500 * 3  # Quantity × 5500 yards × 3 ft/yd
                except Exception:
                    feet = ""

                thread_log_rows.append(
                    build_thread_log_row(timestamp, name, feet, action)
                )

        # 6) Append to logs
        if material_log_rows:
            sheet.append(
                spreadsheetId=SPREADSHEET_ID,
                range="Material Log!A2:I",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": material_log_rows}
            ).execute()

        if thread_log_rows:
            # Write against a wide range so rows match header width even if columns move
            sheet.append(
                spreadsheetId=SPREADSHEET_ID,
                range="Thread Data!A2:Z",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": thread_log_rows}
            ).execute()

        return jsonify({"status": "submitted"}), 200

    except Exception as e:
        logging.exception("❌ submit_material_inventory failed")
        return jsonify({"error": str(e)}), 500

# server.py (or routes file)
from flask import request, jsonify
from datetime import datetime

@app.route("/api/directory-row", methods=["GET"])
@login_required_session
def directory_row():
    company = (request.args.get("company") or "").strip()
    if not company:
        return jsonify({"error": "Missing company"}), 400

    # Read your 'Directory' tab (A:K or whatever your full header range is)
    sheet = sheets.values()
    resp = sheet.get(
        spreadsheetId=SPREADSHEET_ID,
        range="Directory!A1:K10000",
        valueRenderOption="UNFORMATTED_VALUE"
    ).execute()
    rows = resp.get("values", [])
    if not rows:
        return jsonify({"error": "No Directory data"}), 404

    headers = rows[0]
    # Find row where 'Company Name' matches (case-insensitive, trimmed)
    target = None
    for r in rows[1:]:
        row = { headers[i]: (r[i] if i < len(r) else "") for i in range(len(headers)) }
        name = str(row.get("Company Name", "")).strip()
        if name.lower() == company.lower():
            target = row
            break

    if not target:
        return jsonify({"error": f"Company not found: {company}"}), 404

    return jsonify(target)


@app.route("/api/products", methods=["GET"])
@login_required_session
def get_products():
    """
    Returns JSON array of product names from the 'Table' sheet (column A).
    """
    try:
        # read column A (products) from row 2 down
        rows = fetch_sheet(SPREADSHEET_ID, "Table!A2:A")
        products = [r[0] for r in rows if r and r[0].strip()]
        return jsonify(products), 200
    except Exception:
        logger.exception("Error fetching products")
        return jsonify([]), 200

@app.route("/api/inventory", methods=["GET"])
@login_required_session
def get_inventory():
    # Pull the header row + all data rows from Material Inventory!A1:H
    rows = fetch_sheet(SPREADSHEET_ID, "Material Inventory!A1:H")
    headers = rows[0] if rows else []
    data    = [dict(zip(headers, r)) for r in rows[1:]] if rows else []
    return jsonify({ "headers": headers, "rows": data }), 200

@app.route("/api/threadInventory", methods=["POST"])
@login_required_session
def submit_thread_inventory():
    entries = request.get_json(silent=True) or []
    now = datetime.now(ZoneInfo("America/New_York")).strftime("%-m/%-d/%Y %H:%M:%S")

    # Fetch Thread Data headers so we can place values by name
    td_rows = fetch_sheet(SPREADSHEET_ID, "Thread Data!A1:Z")
    headers = td_rows[0] if td_rows else []
    h_idx = {h: i for i, h in enumerate(headers)}

    def build_row(color, feet, action):
        row = [""] * len(headers)
        if "Date" in h_idx:         row[h_idx["Date"]] = now
        if "Color" in h_idx:        row[h_idx["Color"]] = color
        if "Length (ft)" in h_idx:  row[h_idx["Length (ft)"]] = feet
        if "IN/OUT" in h_idx:       row[h_idx["IN/OUT"]] = "IN"
        if "O/R" in h_idx:          row[h_idx["O/R"]] = action  # Ordered / Received
        return row

    to_log = []
    for e in (entries if isinstance(entries, list) else [entries]):
        color     = (e.get("value") or "").strip()      # NOTE: frontend sends "value"
        action    = (e.get("action") or "").strip()
        qty_cones = (e.get("quantity") or "").strip()
        if not (color and action and qty_cones):
            continue

        try:
            cones = int(qty_cones)
            feet  = cones * 5500 * 3  # Quantity × 5500 yards × 3 ft/yd
        except Exception:
            feet = ""

        to_log.append(build_row(color, feet, action))

    if to_log:
        sheets.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Thread Data!A2:Z",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": to_log}
        ).execute()

    # ✅ invalidate materials-needed cache
    invalidate_materials_needed_cache()

    return jsonify({"added": len(to_log)}), 200



@app.route("/api/inventoryOrdered", methods=["GET"])
@login_required_session
def get_inventory_ordered():
    orders = []

    # 0) Build Material→Unit map from Inventory sheet A2:D
    inv_rows = fetch_sheet(SPREADSHEET_ID, "Material Inventory!A2:D")
    unit_map = {
        r[0]: (r[3] if len(r) > 3 else "")
        for r in inv_rows if r and r[0].strip()
    }

    # 1) Material Log sheet
    mat = fetch_sheet(SPREADSHEET_ID, "Material Log!A1:Z")
    if mat:
        hdr     = mat[0]
        i_dt    = hdr.index("Date")
        i_or    = hdr.index("O/R")
        qty_idx = i_or - 2              # Quantity is one column left of O/R
        i_mat   = hdr.index("Material")

        for idx, row in enumerate(mat[1:], start=2):
            if len(row) > i_or and row[i_or].strip().lower() == "ordered":
                name = row[i_mat] if len(row) > i_mat else ""
                qty  = row[qty_idx] if len(row) > qty_idx else ""
                orders.append({
                    "row":      idx,
                    "date":     row[i_dt] if len(row) > i_dt else "",
                    "type":     "Material",
                    "name":     name,
                    "quantity": qty,
                    "unit":     unit_map.get(name, "")
                })

    # 2) Thread Data sheet (unchanged)
    th = fetch_sheet(SPREADSHEET_ID, "Thread Data!A1:Z")
    if th:
        hdr    = th[0]
        i_or   = hdr.index("O/R")
        i_dt   = hdr.index("Date")
        i_col  = hdr.index("Color")
        i_len  = hdr.index("Length (ft)")

        for idx, row in enumerate(th[1:], start=2):
            if len(row) > i_or and row[i_or].strip().lower() == "ordered":
                qty = row[i_len] if len(row) > i_len else ""
                try:
                    qty = f"{float(qty) / 16500:.2f} cones"
                except:
                    pass
                orders.append({
                    "row":      idx,
                    "date":     row[i_dt] if len(row) > i_dt else "",
                    "type":     "Thread",
                    "name":     row[i_col] if len(row) > i_col else "",
                    "quantity": qty
                })

    return jsonify(orders), 200

@app.route("/api/inventoryOrdered", methods=["PUT"])
@login_required_session
def mark_inventory_received():
    """
    Expects JSON:
      { type: "Material"|"Thread", row: <number> }
    Updates:
      - Column A (Date) to now
      - O/R column to "Received" (I for Material, H for Thread)
    """
    data      = request.get_json(silent=True) or {}
    sheetType = data.get("type")
    row       = data.get("row")

    try:
        row = int(row)
    except:
        return jsonify({"error":"invalid row"}), 400

    # choose sheet & O/R column
    if sheetType == "Material":
        sheet   = "Material Log"
        col_or  = "I"
    else:
        sheet   = "Thread Data"
        col_or  = "H"

    # timestamp in A
    now = datetime.now(ZoneInfo("America/New_York"))\
              .strftime("%-m/%-d/%Y %H:%M:%S")

    # 1) Update the Date cell (col A)
    sheets.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!A{row}",
        valueInputOption="USER_ENTERED",
        body={"values":[[now]]}
    ).execute()

    # 2) Update the O/R cell to "Received"
    sheets.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!{col_or}{row}",
        valueInputOption="USER_ENTERED",
        body={"values":[["Received"]]}
    ).execute()

    return jsonify({"status":"ok"}), 200

@app.route("/api/company-list")
@login_required_session
def company_list():
    directory_data = fetch_sheet(SPREADSHEET_ID, "Directory!A1:Z")
    headers = directory_data[0]

    # Try to find the column with company names
    col_index = None
    for idx, col in enumerate(headers):
        if str(col).strip().lower() in ["company name", "company"]:
            col_index = idx
            break

    if col_index is None:
        return jsonify({"error": "Company name column not found in Directory tab"}), 500

    # Get all non-empty company names and deduplicate
    companies = list({row[col_index].strip() for row in directory_data[1:] if len(row) > col_index and row[col_index].strip()})
    companies.sort()

    return jsonify({"companies": companies})

@app.route("/api/process-shipment", methods=["POST"])
def process_shipment():
    data = request.get_json()
    env_override = data.get("qboEnv")  # either "sandbox" or "production"
    session["qboEnv"] = (env_override or "production")  # ← store desired env
    print("📥 Reorder API received:", data)


    print("📥 Reorder API received:", data)

    # 1) Parse incoming
    order_ids = [str(oid).strip() for oid in data.get("order_ids", [])]
    shipped_quantities = {
        str(k).strip(): v
        for k, v in data.get("shipped_quantities", {}).items()
    }
    boxes = data.get("boxes", [])
    shipping_method = data.get("shipping_method", "")
    service_code    = data.get("service_code")   # e.g., "03", "02", "01", etc.

    print("🔍 Received order_ids:", order_ids)
    print("🔍 Received shipped_quantities:", shipped_quantities)
    print("🔍 Received shipping_method:", shipping_method)

    if not order_ids:
        return jsonify({"error": "Missing order_ids"}), 400

    sheet_id = os.environ["SPREADSHEET_ID"]
    sheet_name = "Production Orders"

    try:
        service = get_sheets_service()
        # 2) Read the full sheet
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{sheet_name}!A1:Z",
        ).execute()
        rows = result.get("values", [])
        headers = rows[0]

        # locate columns
        id_col      = headers.index("Order #")
        shipped_col = headers.index("Shipped")

        updates = []
        all_order_data = []

        # 3) Build update requests & collect data for invoice
        for i, row in enumerate(rows[1:], start=2):
            order_id = str(row[id_col]).strip()
            if order_id in order_ids:
                raw = shipped_quantities.get(order_id, 0)
                try:
                    parsed_qty = int(float(raw))
                except:
                    parsed_qty = 0

                # queue sheet update
                updates.append({
                    "range": f"{sheet_name}!{chr(shipped_col + 65)}{i}",
                    "values": [[str(parsed_qty)]]
                })

                # build order_data dict for invoice
                row_dict = dict(zip(headers, row))
                order_dict = {
                    h: (
                        str(parsed_qty) if h in ("Shipped", "ShippedQty")
                        else row_dict.get(h, "")
                    )
                    for h in headers
                }
                order_dict["ShippedQty"] = parsed_qty
                all_order_data.append(order_dict)

        # 4) Push updates
        if updates:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": updates}
            ).execute()
            print("✅ Shipped quantities written to sheet.")
        else:
            print("⚠️ No updates to push—check order_ids match sheet.")

        # 5) Create invoice in QBO…
        headers, realm_id = get_quickbooks_credentials()
        invoice_url = create_consolidated_invoice_in_quickbooks(
            all_order_data,
            shipping_method,
            tracking_list=[],
            base_shipping_cost=0.0,
            sheet=service,
            env_override=env_override
        )

        # fetch your company’s info
        company_info = fetch_company_info(headers, realm_id, env_override)

        # 6) Generate packing‐slip PDF with real company_info
        pdf_bytes = build_packing_slip_pdf(all_order_data, boxes, company_info)
        filename = f"packing_slip_{int(time.time())}.pdf"
        tmp_path = os.path.join(tempfile.gettempdir(), filename)
        with open(tmp_path, "wb") as f:
            f.write(pdf_bytes)

        # 7) Build a public URL for the front-end
        slip_url = url_for("serve_slip", filename=filename, _external=True)

        # 8) Upload packing slip to Drive for the watcher
        # Use the first order_id from the request (not the loop variable)
        order_ids_str = "-".join(order_ids)
        num_slips     = len(boxes)
        pdf_filename  = f"{order_ids_str}_copies_{len(boxes)}_packing_slip.pdf"
        media            = MediaIoBaseUpload(BytesIO(pdf_bytes), mimetype="application/pdf")
        drive = get_drive_service()
        drive.files().create(
            body={
                "name":    pdf_filename,
                "parents": [os.environ["PACKING_SLIP_PRINT_FOLDER_ID"]]
            },
            media_body=media
        ).execute()

        # now return just the labels & invoice—no more pop-up slips
        return jsonify({
            "labels":  [],
            "invoice": invoice_url,
            "slips":   []
        })


    except RedirectException as e:
        # No valid QuickBooks token → tell client to start OAuth flow
        print("🔁 Redirecting to OAuth:", e.redirect_url)
        return jsonify({"redirect": e.redirect_url}), 200

    except Exception as e:
        # Any other error
        print("❌ Shipment error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.errorhandler(Exception)
def handle_exception(e):
    # Log the full stack for debugging
    logger.exception("Unhandled exception in request:")
    # Return a JSON error and CORS
    resp = jsonify(error=str(e))
    resp.status_code = 500
    resp.headers["Access-Control-Allow-Origin"] = FRONTEND_URL
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    return resp

@app.route("/api/product-specs", methods=["POST"])
@login_required_session
def set_product_specs():
    """
    Accepts JSON:
      {
        "product": "My Product Name",
        "printTime": 12,
        "perYard": 3,
        "foamHalf": 10,
        "foam38": 8,
        "foam14": 6,
        "foam18": 4,
        "magnetN": 5,
        "magnetS": 5,
        "elasticHalf": 100,
        "volume": 2000
      }
    Finds the matching row in the Table sheet by column A, then writes
    each value into its lettered column.
    """
    data = request.get_json() or {}
    product = data.get("product", "").strip()
    if not product:
        return jsonify({"error": "Missing product"}), 400

    # 1) Fetch column A (Products) to find the row
    sheet = get_sheets_service().spreadsheets()
    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Table!A2:Z"
    ).execute()
    values = result.get("values", [])

    row_index = None
    for i, row in enumerate(values, start=2):
        if str(row[0]).strip().lower() == product.lower():
            row_index = i
            break

    if row_index is None:
        return jsonify({"error": f"Product '{product}' not found"}), 404

    # 2) Map each incoming field to its sheet column
    updates = {
        "D": data.get("printTime", ""),       # Print Times (1 Machine)
        "F": data.get("perYard", ""),         # How Many Products Per Yard
        "G": data.get("foamHalf", ""),        # 1/2" Foam
        "H": data.get("foam38", ""),          # 3/8" Foam
        "I": data.get("foam14", ""),          # 1/4" Foam
        "J": data.get("foam18", ""),          # 1/8" Foam
        "K": data.get("magnetN", ""),         # N Magnets
        "L": data.get("magnetS", ""),         # S Magnets
        "M": data.get("elasticHalf", ""),     # 1/2" Elastic
        "N": data.get("volume", ""),          # Volume
    }

    # 3) Write each one cell
    for col, val in updates.items():
        target = f"Table!{col}{row_index}"
        sheet.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Material Inventory!A{target_row}:P{target_row}",  # <- now includes column P
            valueInputOption="USER_ENTERED",
            body={"values": [inventory_row]}
        ).execute()

    return jsonify({"status": "ok"}), 200

@app.route("/api/logout-all", methods=["POST"])
@login_required_session
def logout_all():
    global logout_all_ts
    # bump the timestamp so that any calls to login_required_session will now fail
    logout_all_ts = time.time()
    # push a socket event to all connected clients
    socketio.emit("forceLogout")
    return jsonify({"status": "ok"}), 200

@app.route("/api/rate", methods=["POST"])
@login_required_session
def api_rate():
    """
    Request body:
    {
      "to": { "name","phone","addr1","addr2","city","state","zip","country" },
      "packages": [ { "L":10,"W":10,"H":10,"weight":2 }, ... ]
    }
    Returns list of options: [{code, method, rate, currency, delivery}, ...]
    """
    try:
        payload = request.get_json(force=True)
        to = payload.get("to", {})
        packages = payload.get("packages", [])
        options = ups_get_rate(to, packages, ask_all_services=True)
        return jsonify({"ok": True, "options": options})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/list-folder-files")
def list_folder_files():
    folder_id = request.args.get("folderId")
    if not folder_id:
        return jsonify({"error": "Missing folderId"}), 400

    try:
        drive = get_drive_service()
        results = drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="files(id, name, mimeType)"
        ).execute()
        return jsonify({"files": results.get("files", [])})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/proxy-drive-file")
def proxy_drive_file():
    print("🔥 proxy_drive_file hit")
    file_id = request.args.get("fileId")
    if not file_id:
        print("❌ Missing fileId in request")
        return "Missing fileId", 400

    try:
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
        print(f"🔄 Fetching file from: {url}")
        r = requests.get(url, stream=True)
        r.raise_for_status()

        content_type = r.headers.get("Content-Type", "application/octet-stream")
        print(f"✅ File fetched. Content-Type: {content_type}")
        return Response(r.iter_content(chunk_size=4096), content_type=content_type)
    except Exception as e:
        print("❌ Error during proxying file:")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/drive-file-metadata")
def drive_file_metadata():
    file_id = request.args.get("fileId")
    if not file_id:
        return jsonify({"error": "Missing fileId"}), 400

    try:
        print(f"🔍 Fetching metadata for file ID: {file_id}")
        service = get_drive_service()
        metadata = service.files().get(fileId=file_id, fields="id, name, mimeType").execute()
        print(f"✅ Metadata retrieved: {metadata}")
        return jsonify(metadata)
    except Exception as e:
        app.logger.error(f"❌ Error fetching metadata for file {file_id}: {e}")
        return jsonify({"error": str(e)}), 500
def get_column_index(sheet, header_name):
    headers = sheet.row_values(1)
    for idx, col in enumerate(headers, start=1):
        if col.strip().lower() == header_name.strip().lower():
            return idx
    raise ValueError(f"Column '{header_name}' not found.")

@app.route("/api/resetStartTime", methods=["POST"])
@login_required_session
def reset_start_time():
    try:
        data = request.get_json()
        job_id = str(data.get("id", "")).strip()
        timestamp = data.get("timestamp", "")

        if not job_id or not timestamp:
            return jsonify({"error": "Missing job ID or timestamp"}), 400

        sheet = sh.worksheet("Production Orders")
        header = [h.strip() for h in sheet.row_values(1)]

        try:
            emb_start_col = header.index("Embroidery Start Time") + 1
        except ValueError:
            return jsonify({"error": "Missing 'Embroidery Start Time' column"}), 500

        rows = sheet.get_all_records()
        for i, row in enumerate(rows, start=2):
            if str(row.get("ID", "")).strip() == job_id:
                sheet.update_cell(i, emb_start_col, timestamp)
                print(f"✅ Reset start time for row {i} (Job ID {job_id}) → {timestamp}")
                return jsonify({"status": "ok"}), 200

        return jsonify({"error": "Job not found"}), 404

    except Exception as e:
        print("🔥 Server error:", str(e))
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


def copy_emb_files(old_order_num, new_order_num, drive_service, new_folder_id):
    try:
        # Step 1: Look up the old folder
        query = f"name = '{old_order_num}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        folders = drive_service.files().list(q=query, fields="files(id)").execute().get("files", [])
        if not folders:
            print(f"❌ No folder found for order #{old_order_num}")
            return

        old_folder_id = folders[0]["id"]

        # Step 2: List files inside old folder
        query = f"'{old_folder_id}' in parents and trashed = false"
        files = drive_service.files().list(q=query, fields="files(id, name)").execute().get("files", [])

        for file in files:
            if file["name"].lower().endswith(".emb"):
                print(f"📤 Copying {file['name']} from order {old_order_num} → {new_order_num}")
                drive_service.files().copy(
                    fileId=file["id"],
                    body={"name": f"{new_order_num}.emb", "parents": [new_folder_id]}
                ).execute()
    except Exception as e:
        print("❌ Error copying .emb files:", e)

from flask import request, session, redirect
from requests_oauthlib import OAuth2Session

@app.route("/qbo/login")
def qbo_login():
    print("🚀 Entered /qbo/login route")

    # ── 0) Determine sandbox vs prod from query or session ───────
    env_override = request.args.get("env") or session.get("qboEnv")
    session["qboEnv"] = env_override or QBO_ENV

    # ── 1) Pick the correct OAuth client ID/secret ───────────────
    client_id, client_secret = get_qbo_oauth_credentials(env_override)

    # Log what we’re sending so you can confirm in Render logs
    print(f"QBO env: {session.get('qboEnv', QBO_ENV)} | redirect_uri={QBO_REDIRECT_URI!r}")
    print(f"QBO client id (first 6): {str(client_id)[:6]}…")

    # ── 2) Build the OAuth2Session with the chosen client_id ─────
    qbo = OAuth2Session(
     client_id=client_id,
     redirect_uri=QBO_REDIRECT_URI,
     scope=QBO_SCOPES,
     state=session.get("qbo_oauth_state")
    )


    # ── 3) Generate the authorization URL ─────────────────────────
    auth_url, state = qbo.authorization_url(QBO_AUTH_URL)

    # ── 4) Persist state and redirect the user ────────────────────
    session["qbo_oauth_state"] = state
    print("🔗 QuickBooks redirect URL:", auth_url)
    return redirect(auth_url)


logger = logging.getLogger(__name__)

@app.route("/qbo/callback", methods=["GET"])
def qbo_callback():
    code  = request.args.get("code")
    state = request.args.get("state")
    realm = request.args.get("realmId")

    if state != session.get("qbo_oauth_state"):
        return "⚠️ Invalid state", 400

    # ── 0) Determine sandbox vs production from session ──────────
    env_override = session.get("qboEnv", QBO_ENV)

    # ── 1) Pick the correct OAuth client credentials ─────────────
    client_id, client_secret = get_qbo_oauth_credentials(env_override)

    # ── 2) Rebuild OAuth2Session with that client_id & state ─────
    qbo = OAuth2Session(
        client_id,
        redirect_uri=QBO_REDIRECT_URI,
        state=state
    )

    # ── 3) Exchange code for token using the matching client_secret ─
    token = qbo.fetch_token(
        QBO_TOKEN_URL,
        client_secret=client_secret,
        code=code
    )

    # ── 4) Persist token & realmId to disk for reuse ─────────────
    disk_data = { **token, "realmId": realm }
    with open(TOKEN_PATH, "w") as f:
        json.dump(disk_data, f, indent=2)
    logger.info("✅ Wrote QBO token to disk at %s", TOKEN_PATH)

    # ── 5) Store in session for your API calls ────────────────────
    session["qbo_token"] = {
        "access_token":  token["access_token"],
        "refresh_token": token["refresh_token"],
        "expires_at":    time.time() + int(token["expires_in"]),
        "realmId":       realm
    }
    session["qboEnv"] = env_override
    logger.info("✅ Stored QBO token in session, environment: %s", env_override)

    # ── 6) Redirect back into your Ship UI ────────────────────────
    frontend = FRONTEND_URL.rstrip("/")
    resume_url = f"{frontend}/ship"
    logger.info("🔁 OAuth callback complete — redirecting to Ship page: %s", resume_url)
    return redirect(resume_url)

@app.route("/authorize-quickbooks")
def authorize_quickbooks():
    from requests_oauthlib import OAuth2Session

    qbo = OAuth2Session(
        client_id=QBO_CLIENT_ID,
        redirect_uri=QBO_REDIRECT_URI,
        scope=QBO_SCOPE
    )

    authorization_url, state = qbo.authorization_url(QBO_AUTH_BASE_URL)

    # Save the state in session to protect against CSRF
    session["qbo_oauth_state"] = state

    print("🔗 Redirecting to QuickBooks auth URL:", authorization_url)
    return redirect(authorization_url)

@app.route("/quickbooks/login")
def quickbooks_login_redirect():
    # grab desired post-OAuth path (e.g. /ship)
    next_path = request.args.get("next", "/ship")
    # must match exactly what Intuit expects
    redirect_uri = os.environ["QBO_REDIRECT_URI"]
    # pass that as state so we can come back here
    auth_url = get_quickbooks_auth_url(redirect_uri, state=next_path)
    return redirect(auth_url)

@app.route("/slips/<filename>", methods=["GET"])
def serve_slip(filename):
    # Serve the PDF from the temp directory
    return send_from_directory(tempfile.gettempdir(), filename, as_attachment=False)

@app.route("/api/thread-colors", methods=["GET"])
@login_required_session
def get_thread_colors():
    try:
        sheet = sh.worksheet("Thread Inventory")
        data = sheet.get_all_values()

        if not data or len(data) < 2:
            return jsonify([]), 200

        headers = data[0]
        rows = data[1:]

        # Find the index of the "Thread Colors" column
        try:
            col_idx = headers.index("Thread Colors")
        except ValueError:
            raise Exception("🧵 'Thread Colors' column not found in header row.")

        # Collect all unique numeric thread codes
        thread_colors = set()
        for row in rows:
            if len(row) > col_idx:
                val = str(row[col_idx]).strip()
                if val and val.isdigit():  # Only include values like 1800, 1801, etc.
                    thread_colors.add(val)

        return jsonify(sorted(thread_colors)), 200

    except Exception as e:
        print("❌ Failed to fetch thread colors:", e)
        return jsonify([]), 500


# ─── Socket.IO connect/disconnect ─────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    logger.info(f"Client connected: {request.sid}")

@socketio.on("disconnect")
def on_disconnect():
    logger.info(f"Client disconnected: {request.sid}")

# ─── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # --- Startup banner ---
    print("🚀 JRCO server.py loaded and running...")
    print("📡 Available Flask Routes:")
    for rule in app.url_map.iter_rules():
        print("✅", rule)

    # Prefer explicit port from env (Render sets PORT)
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting on port {port}")

    # Optional one-off Google OAuth token generation
    if os.environ.get("GENERATE_GOOGLE_TOKEN", "false").lower() == "true":
        from google_auth_oauthlib.flow import InstalledAppFlow
        SCOPES = [
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ]
        flow = InstalledAppFlow.from_client_secrets_file("oauth-credentials.json", SCOPES)
        creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
        print("✅ token.json created successfully.")
    else:
        # IMPORTANT: run via SocketIO so websockets work in production.
        # Make sure your SocketIO was created with the path you expect, e.g.:
        # socketio = SocketIO(app, cors_allowed_origins=[FRONTEND_URL], async_mode="eventlet", path="/socket.io")
        socketio.run(
            app,
            host="0.0.0.0",
            port=port,
            debug=False,        # Debug off in production
            use_reloader=False  # Avoid double-start on Render
        )
