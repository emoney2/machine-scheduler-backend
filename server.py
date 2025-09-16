import eventlet
import os, eventlet
os.environ.setdefault("EVENTLET_NO_GREENDNS", "yes")
eventlet.monkey_patch()
from eventlet import debug
debug.hub_prevent_multiple_readers(False)

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
import asyncio
from flask import current_app
from flask import request, jsonify
from flask import request, make_response, jsonify, Response
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from math import ceil
from playwright.async_api import async_playwright
from functools import wraps
from flask import request, session, jsonify, redirect, url_for, make_response
from datetime import datetime, timedelta
from flask import send_file, Response
from googleapiclient.discovery import build as gbuild
from googleapiclient.http import MediaIoBaseDownload

import socket
from requests.exceptions import ConnectionError as ReqConnError, Timeout as ReqTimeout, RequestException


from flask import g
import psutil, tracemalloc, gc
tracemalloc.start()
_process = psutil.Process(os.getpid())



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
from flask import send_file  # ADD if not present

# ---- Disk cache for thumbnails ----
THUMB_CACHE_DIR = os.environ.get("THUMB_CACHE_DIR", "/opt/render/project/.thumb_cache")
os.makedirs(THUMB_CACHE_DIR, exist_ok=True)

# --- Tiny in-process cache for Drive thumbnails ---
_drive_thumb_cache = {}  # key: f"{file_id}:{modified_time}" -> bytes
_drive_thumb_ttl   = 600  # seconds (10 min)

_matlog_cache = None          # {"by_order": {"123": [items...]}, "ts": float}
_matlog_cache_ttl = 60.0      # seconds



def _thumb_cache_path(file_id: str, size: str, version: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', f"{file_id}_{size}_{version or 'nov'}.jpg")
    return os.path.join(THUMB_CACHE_DIR, safe)


def clamp_iso_to_next_830_et(iso_str: str) -> str:
    """
    If the given ISO time falls between 4:30 PM and 8:30 AM (America/New_York),
    return ISO at the *next day's* 8:30 AM ET. Otherwise return the original.
    """
    try:
        s = (iso_str or "").replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        et = dt.astimezone(ZoneInfo("America/New_York"))

        after_end   = (et.hour > 16) or (et.hour == 16 and et.minute >= 30)
        before_start= (et.hour < 8)  or (et.hour == 8  and et.minute < 30)
        if after_end or before_start:
            # "following time": always move to *next* work morning
            et = (et + timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0)

        out = et.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00","Z")
        return out
    except Exception:
        return iso_str

def _hdr_idx(headers):
    d = {}
    for i, h in enumerate(headers or []):
        k = str(h or "").strip().lower()
        d[k] = i
    return d

def _find_col(headers, name):
    name = (name or "").strip().lower()
    for i, h in enumerate(headers or []):
        if str(h or "").strip().lower() == name:
            return i
    return None

def _get_material_units_lookup():
    """Material Inventory → { MaterialName (exact): Unit (case-sensitive 'Yards'|'Sqft') }."""
    rows = fetch_sheet(SPREADSHEET_ID, "Material Inventory!A1:Z")
    if not rows:
        return {}
    hdr = rows[0]
    hi = _hdr_idx(hdr)  # lower-cased header→index

    m_ix = hi.get("materials")
    u_ix = hi.get("unit")
    out = {}
    for r in rows[1:]:
        mat = (r[m_ix] if m_ix is not None and m_ix < len(r) else "")
        unit = (r[u_ix] if u_ix is not None and u_ix < len(r) else "")
        mat = str(mat).strip()
        unit = str(unit).strip()
        if mat:
            out[mat] = unit  # keep exact case: "Yards" or "Sqft"
    return out

def _get_ppy_lookup():
    """Table sheet → { product.lower(): PPY } matching your Apps Script (column A product, col 6 PPY)."""
    rows = fetch_sheet(SPREADSHEET_ID, "Table!A1:Z")
    if not rows:
        return {}
    hdr = rows[0]
    hi = _hdr_idx(hdr)
    p_col = hi.get("product", 0)
    ppy_col = hi.get("ppy")
    if ppy_col is None:
        # Fallback to the 6th column (Apps Script uses index 5)
        ppy_col = 5 if len(hdr) > 5 else 1

    out = {}
    for r in rows[1:]:
        if not r or p_col >= len(r):
            continue
        prod = str(r[p_col]).strip()
        if not prod:
            continue
        val = r[ppy_col] if ppy_col < len(r) else ""
        try:
            out[prod.lower()] = float(val)
        except Exception:
            pass
    return out

def _compute_usage_units(material_name, unit_str, product, width_in, length_in, qty, ppy_lookup):
    """
    Mirrors your Apps Script rules (36×54 for a yard → 13.5 sqft/yd), using existing 54" width.

    Yards:
      - if Product present: usage = qty / PPY
      - else if W×L:       usage = (W×L in² × qty) / (36×54)

    Sqft:
      - if Product present: usage = (qty / PPY) × 13.5
      - else if W×L:        usage = (W×L in² / 144) × qty
    """
    unit = (unit_str or "").strip()  # upstream enforces case-sensitive "Yards"|"Sqft"
    ppy = ppy_lookup.get((product or "").lower()) if product else None

    area_in2 = None
    if width_in and length_in:
        try:
            area_in2 = float(width_in) * float(length_in)
        except Exception:
            area_in2 = None

    if unit == "Yards":
        if product and ppy and ppy > 0:
            return float(qty) / float(ppy)
        if area_in2:
            return (area_in2 * float(qty)) / (36.0 * 54.0)
        return 0.0

    if unit == "Sqft":
        if product and ppy and ppy > 0:
            return (float(qty) / float(ppy)) * 13.5
        if area_in2:
            return (area_in2 / 144.0) * float(qty)
        return 0.0

    # Unknown unit (blocked upstream)
    return 0.0

def get_vendor_directory():
    """Reads Vendors!A1:E → { vendor_name: {method,email,cc,website} }"""
    svc = get_sheets_service().spreadsheets().values()
    vals = svc.get(
        spreadsheetId=SPREADSHEET_ID,
        range="Vendors!A1:E",
        valueRenderOption="FORMATTED_VALUE"
    ).execute().get("values", [])
    if not vals:
        return {}
    hdr = _hdr_idx(vals[0])
    out = {}
    for r in vals[1:]:
        def gv(key):
            i = hdr.get(key, None)
            return (r[i] if i is not None and i < len(r) else "").strip()
        vname = gv("vendor")
        if not vname:
            continue
        out[vname] = {
            "method": (gv("method") or "email").lower(),
            "email": gv("email"),
            "cc": gv("cc"),
            "website": gv("website"),
        }
    return out

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

THUMB_META_MAX_ENTRIES = 2000
_THUMB_META_INSERTS = 0

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
    meta = requests.get(meta_url, headers=headers, timeout=20)
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

    # NEW: periodic purge to prevent unbounded growth
    global _THUMB_META_INSERTS
    _THUMB_META_INSERTS += 1
    if _THUMB_META_INSERTS % 50 == 0:  # every ~50 inserts
        # 1) drop expired entries
        cutoff = time.time() - THUMB_META_TTL
        for k, v in list(THUMB_META_CACHE.items()):
            if v.get("ts", 0) < cutoff:
                THUMB_META_CACHE.pop(k, None)

        # 2) if still too big, drop oldest entries by timestamp
        if len(THUMB_META_CACHE) > THUMB_META_MAX_ENTRIES:
            oldest = sorted(THUMB_META_CACHE.items(), key=lambda kv: kv[1].get("ts", 0))
            to_drop = len(THUMB_META_CACHE) - THUMB_META_MAX_ENTRIES
            for k, _ in oldest[:to_drop]:
                THUMB_META_CACHE.pop(k, None)

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

# --- Google auth: unify credential loading for Sheets/Drive -------------------
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.auth.transport.requests import Request as GoogleRequest

def get_google_credentials():
    """
    Returns OAuthCredentials using:
      1) GOOGLE_TOKEN_JSON env (your boot writes token.json from env on startup)
      2) token.json file (GOOGLE_TOKEN_PATH)
    Will auto-refresh if expired and refresh_token is present.
    """
    # Prefer the same loader your Drive code uses
    creds = _load_google_creds()
    if not creds:
        raise RuntimeError("No Google OAuth credentials found. Set GOOGLE_TOKEN_JSON or provide token.json")

    # If token is expired but refresh_token exists, google-auth refreshes on first request;
    # we can optionally preemptively refresh:
    try:
        if not creds.valid and getattr(creds, "refresh_token", None):
            creds.refresh(GoogleRequest())
    except Exception:
        # Ignore refresh hiccups here; the API client will retry/raise cleanly.
        pass

    return creds
# -----------------------------------------------------------------------------


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
    # Use an HTTP client with a hard timeout so Eventlet can't hang forever
    import httplib2
    from googleapiclient.discovery import build
    from google_auth_httplib2 import AuthorizedHttp

    creds = get_google_credentials()
    http = httplib2.Http(timeout=10)  # 10s per request
    authed = AuthorizedHttp(creds, http=http)
    return build("sheets", "v4", http=authed, cache_discovery=False)



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
app.secret_key = os.environ.get("SECRET_KEY", "dev-fallback-secret")

logout_all_ts = int(os.environ.get("LOGOUT_ALL_TS", "0"))

# === Tiny TTL cache & ETag helper ============================================
import hashlib, threading
_cache_lock = threading.Lock()
_json_cache = {}  # key -> {'ts': float, 'ttl': int, 'etag': str, 'data': bytes}

def _cache_get(key):
    now = time.time()
    with _cache_lock:
        ent = _json_cache.get(key)
        if ent and (now - ent['ts'] < ent['ttl']):
            return ent
        return None

def _cache_set(key, payload_bytes, ttl):
    etag = 'W/"%s"' % hashlib.sha1(payload_bytes).hexdigest()
    with _cache_lock:
        _json_cache[key] = {'ts': time.time(), 'ttl': ttl, 'etag': etag, 'data': payload_bytes}
    return etag

def _cache_peek(key):
    with _cache_lock:
        return _json_cache.get(key)

def _maybe_304(etag):
    inm = request.headers.get("If-None-Match", "")
    return (etag and inm and etag in inm)

def send_cached_json(key, ttl, payload_obj_builder):
    """
    If cached and fresh, return cached (and 304 on ETag match).
    If cached but stale, return stale immediately and refresh in background.
    Otherwise build payload, cache, and send with ETag.
    """
    ent = _cache_get(key)
    if ent and _maybe_304(ent['etag']):
        resp = make_response("", 304)
        resp.headers["ETag"] = ent['etag']
        resp.headers["Cache-Control"] = f"public, max-age={ttl}, stale-while-revalidate=300"
        return resp

    if ent:
        # Serve hot cache quickly
        resp = Response(ent['data'], mimetype="application/json")
        resp.headers["ETag"] = ent['etag']
        resp.headers["Cache-Control"] = f"public, max-age={ttl}, stale-while-revalidate=300"
        return resp

    # If we have a stale cache entry, serve it immediately and refresh in background
    stale = _cache_peek(key)
    if stale:
        # Kick off a background refresh (Eventlet green thread)
        def _bg_refresh():
            try:
                payload_obj = payload_obj_builder()
                payload_bytes = json.dumps(payload_obj, separators=(",", ":")).encode("utf-8")
                _cache_set(key, payload_bytes, ttl)
            except Exception as e:
                app.logger.warning("Background refresh failed for %s: %s", key, e)
        eventlet.spawn_n(_bg_refresh)

        # Serve stale immediately
        resp = Response(stale['data'], mimetype="application/json")
        resp.headers["ETag"] = stale['etag']
        resp.headers["Cache-Control"] = f"public, max-age=5, stale-while-revalidate=300"
        resp.headers["Warning"] = '110 - "stale response served while refreshing"'
        return resp

    # Build fresh payload
    started = time.time()
    payload_obj = payload_obj_builder()
    payload_bytes = json.dumps(payload_obj, separators=(",", ":")).encode("utf-8")
    etag = _cache_set(key, payload_bytes, ttl)
    resp = Response(payload_bytes, mimetype="application/json")
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = f"public, max-age={ttl}, stale-while-revalidate=300"
    resp.headers["X-Debug-BuildMs"] = str(int((time.time()-started)*1000))
    return resp


def login_required_session(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # 1) OPTIONS are always allowed (CORS preflight)
        if request.method == "OPTIONS":
                    origin = (request.headers.get("Origin") or "").strip().rstrip("/")
                    # Env-driven allow-list + safe defaults
                    allowed_env = os.environ.get("ALLOWED_ORIGINS", "")
                    allowed = {o.strip().rstrip("/") for o in allowed_env.split(",") if o.strip()}
                    allowed.update({
                        (os.environ.get("FRONTEND_URL", "https://machineschedule.netlify.app").strip().rstrip("/")),
                        "https://machineschedule.netlify.app",
                        "http://localhost:3000",
                    })
                    response = make_response("", 204)
                    if origin in allowed:
                        response.headers["Access-Control-Allow-Origin"] = origin
                        response.headers["Access-Control-Allow-Credentials"] = "true"
                        response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
                        response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,PATCH,OPTIONS"
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

MADEIRA_BASE = "https://www.madeirausa.com"

async def madeira_login_and_cart(items):
    """
    items: [{url, qty}]
    Ensures login, then for each item visits page, sets qty if possible, otherwise clicks Add to Cart N times.
    Ends on cart page.
    """
    email = os.environ.get("MADEIRA_EMAIL")
    password = os.environ.get("MADEIRA_PASSWORD")
    if not email or not password:
        raise RuntimeError("Missing MADEIRA_EMAIL/MADEIRA_PASSWORD env vars")

    user_data_dir = os.path.abspath("./.pw_madeira_profile")
    os.makedirs(user_data_dir, exist_ok=True)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=True,  # set to False temporarily if you want to watch it in a local dev box
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = await ctx.new_page()

        async def is_logged_in():
            try:
                await page.goto(MADEIRA_BASE + "/", wait_until="domcontentloaded")
                html = (await page.content()).lower()
                return ("logout" in html) or ("my account" in html) or ("sign out" in html)
            except:
                return False

        async def do_login():
            # Try a few likely login URLs; fall back to clicking a login link
            candidates = [
                "/Account/Login.aspx",
                "/account/login",
                "/login",
                "/login.aspx",
            ]
            for path in candidates:
                try:
                    await page.goto(MADEIRA_BASE + path, wait_until="domcontentloaded")
                    html = (await page.content()).lower()
                    if "password" in html:
                        break
                except:
                    continue

            # If that didn't land on a login page, click a Login link
            if "password" not in (await page.content()).lower():
                try:
                    await page.goto(MADEIRA_BASE + "/", wait_until="domcontentloaded")
                    btn = await page.query_selector('a:has-text("Login"), a:has-text("Sign In"), a[href*="login" i]')
                    if btn:
                        await btn.click()
                        await page.wait_for_load_state("domcontentloaded")
                except:
                    pass

            # Fill in credentials
            email_sel = ['input[type="email"]','input[name*="email" i]','input[id*="email" i]','input[name*="user" i]']
            pass_sel  = ['input[type="password"]','input[name*="pass" i]','input[id*="pass" i]']

            email_el = None
            for s in email_sel:
                email_el = await page.query_selector(s)
                if email_el: break
            pass_el = None
            for s in pass_sel:
                pass_el = await page.query_selector(s)
                if pass_el: break

            if not email_el or not pass_el:
                raise RuntimeError("Could not find login fields on Madeira")

            await email_el.fill(email)
            await pass_el.fill(password)

            # Try click a submit/login button
            submit = await page.query_selector('button:has-text("Log In"), button:has-text("Sign In"), input[type="submit"], button[type="submit"]')
            if submit:
                await submit.click()
            else:
                # fallback: press Enter in password
                await pass_el.press("Enter")

            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except:
                pass

            if not await is_logged_in():
                raise RuntimeError("Madeira login failed")

        if not await is_logged_in():
            await do_login()

        async def add_one(url, qty):
            # Normalize URL
            if not url.startswith("http"):
                url = MADEIRA_BASE + url

            await page.goto(url, wait_until="domcontentloaded")

            # Try to set quantity if an input is present
            qty_set = False
            for s in ['input[name*="qty" i]','input[id*="qty" i]','input[name*="quant" i]','input[id*="quant" i]','input[type="number"]']:
                el = await page.query_selector(s)
                if el:
                    await el.fill(str(qty))
                    try:
                        await el.dispatch_event("change")
                    except:
                        pass
                    qty_set = True
                    break

            # Click "Add to Cart" (fallback: click multiple times if no qty box)
            selectors = [
                'button:has-text("Add to Cart")',
                'input[type="submit"][value*="add" i]',
                'input[type="button"][value*="add" i]',
                'button[id*="add" i]',
                'a:has-text("Add to Cart")',
            ]
            if qty_set:
                # one click should carry the qty value
                for s in selectors:
                    el = await page.query_selector(s)
                    if el:
                        await el.click()
                        break
            else:
                # as a fallback, click N times to achieve qty
                clicked_once = False
                for n in range(int(qty)):
                    found = False
                    for s in selectors:
                        el = await page.query_selector(s)
                        if el:
                            await el.click()
                            found = True
                            clicked_once = True
                            break
                    if not found:
                        break
                    try:
                        await page.wait_for_timeout(400)
                    except:
                        pass
                if not clicked_once:
                    raise RuntimeError("Could not find 'Add to Cart' button on product page")

            # give time for mini-cart / server update
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except:
                pass

        # Add each item
        for it in items:
            await add_one(it.get("url",""), int(it.get("qty") or 1))

        # Load cart page at the end
        cart_url = MADEIRA_BASE + "/shoppingcart.aspx"
        try:
            await page.goto(cart_url, wait_until="domcontentloaded")
        except:
            try:
                await page.goto(MADEIRA_BASE + "/ShoppingCart.aspx", wait_until="domcontentloaded")
                cart_url = MADEIRA_BASE + "/ShoppingCart.aspx"
            except:
                pass

        # Optional: quick sanity check (cart page contains typical strings)
        html = (await page.content()).lower()
        loginish = ("login" in html) and ("password" in html)
        if loginish:
            raise RuntimeError("Ended on login page — account not logged in; check MADEIRA_EMAIL/MADEIRA_PASSWORD")

        # Persist session
        try:
            await ctx.storage_state(path=os.path.join(user_data_dir, "storage_state.json"))
        except:
            pass

        await ctx.close()
        return {"cart_url": cart_url}


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
    # Env-driven allow-list + safe defaults
    allowed_env = os.environ.get("ALLOWED_ORIGINS", "")
    allowed = {o.strip().rstrip("/") for o in allowed_env.split(",") if o.strip()}
    allowed.update({
        (os.environ.get("FRONTEND_URL", "https://machineschedule.netlify.app").strip().rstrip("/")),
        "https://machineschedule.netlify.app",
        "http://localhost:3000",
    })
    if origin in allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,OPTIONS"
    return response  # short-circuit OPTIONS

# NEW: capture RSS before each request
@app.before_request
def _mem_before():
    try:
        g._rss_before = _process.memory_info().rss
    except Exception:
        g._rss_before = None

# NEW: log RSS delta after each request and trigger opportunistic GC
@app.after_request
def _mem_after(resp):
    try:
        rss_now = _process.memory_info().rss
        rss_before = getattr(g, "_rss_before", None)
        if rss_before is not None:
            delta_mb = (rss_now - rss_before) / (1024 * 1024)
            app.logger.info(f"[MEM] {request.method} {request.path} Δ={delta_mb:.2f}MB rss={rss_now/1024/1024:.2f}MB")
        # Opportunistic GC to curb fragmentation during bursts
        gc.collect()
    except Exception:
        pass
    return resp
# --- ADD alongside your other routes ---
@app.route("/api/drive/thumbnail", methods=["GET"])
@login_required_session
def drive_thumbnail():
    file_id = (request.args.get("fileId") or "").strip()
    if not file_id:
        return jsonify({"error": "Missing fileId"}), 400
    try:
        # Use your existing Drive service + OAuth creds
        drive = get_drive_service()
        creds = get_oauth_credentials()  # same creds mechanism you use elsewhere
        if hasattr(creds, "expired") and creds.expired and hasattr(creds, "refresh_token"):
            # Refresh if needed
            creds.refresh(Request())

        # Ask Drive for its lightweight thumbnail URL (much faster than full download)
        meta = drive.files().get(
            fileId=file_id,
            fields="thumbnailLink,hasThumbnail,name,mimeType,modifiedTime,md5Checksum"
        ).execute()

        thumb_url = meta.get("thumbnailLink")
        has_thumb = meta.get("hasThumbnail")
        modified  = meta.get("modifiedTime") or ""
        etag      = meta.get("md5Checksum") or ""

        # Fallback: if no Drive thumbnail, try direct media for image/* files
        if not has_thumb or not thumb_url:
            mime = meta.get("mimeType", "")
            if mime.startswith("image/"):
                session = AuthorizedSession(creds)
                r = session.get(f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media", timeout=8)
                if r.status_code == 200 and r.content:
                    headers = {"Cache-Control": "public, max-age=86400"}
                    if modified:
                        headers["Last-Modified"] = modified
                    if etag:
                        headers["ETag"] = etag
                    return Response(r.content, mimetype=mime, headers=headers)
            # otherwise, no image we can stream → no content
            return Response(status=204)


        # --- In-process cache lookup ---
        cache_key = f"{file_id}:{modified}"
        now = time.time()
        cached = _drive_thumb_cache.get(cache_key)
        if cached and (now - cached["ts"] < _drive_thumb_ttl):
            return Response(
                cached["bytes"],
                mimetype="image/jpeg",
                headers={
                    "Cache-Control": "public, max-age=86400",
                    **({"Last-Modified": modified} if modified else {}),
                    **({"ETag": etag} if etag else {}),
                },
            )

        # Miss → fetch from Drive once
        session = AuthorizedSession(creds)
        r = session.get(thumb_url, timeout=8)
        if r.status_code != 200 or not r.content:
            return Response(status=204)

        # Save to cache
        _drive_thumb_cache[cache_key] = {"bytes": r.content, "ts": now}

        # Respond with strong cache headers for browsers/CDNs
        headers = {"Cache-Control": "public, max-age=86400"}
        if modified:
            headers["Last-Modified"] = modified
        if etag:
            headers["ETag"] = etag

        return Response(r.content, mimetype="image/jpeg", headers=headers)

    except Exception:
        app.logger.exception("drive_thumbnail error")
        # Let the <img> fail silently; UI hides it onError
        return Response(status=204)


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


# --- replace your /api/drive/proxy handler with this ---
from threading import Thread
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def _warm_thumb_async(file_id: str, sz: str, ver: str):
    """Background cache warm with retries; never raises to the request thread."""
    try:
        google_thumb = f"https://drive.google.com/thumbnail?id={file_id}&sz={sz}"
        sess = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        sess.mount("https://", HTTPAdapter(max_retries=retry))
        r = sess.get(google_thumb, headers={"Accept": "image/*", "User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image/"):
            os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
            with open(_thumb_cache_path(file_id, sz, ver or "1"), "wb") as f:
                f.write(r.content)
    except Exception:
        app.logger.exception("thumb warm failed for %s", file_id)

@app.route("/api/drive/proxy/<file_id>")
@login_required_session
def drive_proxy(file_id):
    """
    Disk-cached Drive thumbnail proxy.

    Behavior:
      * If cached: serve from disk (immutable, instant).
      * If not cached: spawn background warm + 302 redirect to Google immediately.
    """
    sz  = (request.args.get("sz") or "w240").strip()
    ver = (request.args.get("v") or "").strip() or "1"

    # 1) Cache hit → serve immediately
    try:
        cpath = _thumb_cache_path(file_id, sz, ver)
        if os.path.exists(cpath):
            resp = send_file(cpath, mimetype="image/jpeg", as_attachment=False, conditional=True)
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return resp
    except Exception:
        app.logger.exception("drive_proxy cache read failed for %s", file_id)

    # 2) Cache miss → warm in background, redirect client so the UI still gets an image now
    try:
        Thread(target=_warm_thumb_async, args=(file_id, sz, ver), daemon=True).start()
    except Exception:
        app.logger.exception("drive_proxy warm spawn failed for %s", file_id)

    google_thumb = f"https://drive.google.com/thumbnail?id={file_id}&sz={sz}"
    return redirect(google_thumb, code=302)


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

def get_or_create_ship_method_ref(name, headers, realm_id, env_override=None):
    """
    Ensure a ShipMethod (e.g., 'UPS') exists in QBO and return {"value": Id, "name": Name}.
    """
    import requests
    base = get_base_qbo_url(env_override)
    query_url = f"{base}/v3/company/{realm_id}/query?minorversion=65"
    create_url = f"{base}/v3/company/{realm_id}/shipmethod?minorversion=65"

    # 1) Try to find it
    escaped = name.replace("'", "''")
    query = f"SELECT * FROM ShipMethod WHERE Name = '{escaped}'"
    r = requests.get(query_url, headers=headers, params={"query": query})
    if r.status_code == 200:
        items = r.json().get("QueryResponse", {}).get("ShipMethod", [])
        if items:
            sm = items[0]
            return {"value": sm["Id"], "name": sm["Name"]}

    # 2) Create if missing
    payload = {"Name": name}
    r2 = requests.post(create_url, headers={**headers, "Content-Type": "application/json"}, json=payload)
    if r2.status_code in (200, 201):
        sm = r2.json().get("ShipMethod", {})
        return {"value": sm["Id"], "name": sm["Name"]}

    # 3) Fallback (name only) — QBO usually needs an Id, but we'll return name if creation fails
    logging.warning("⚠️ Could not create/find ShipMethod '%s' (%s / %s). Falling back to name only.", name, r.status_code, r2.status_code if 'r2' in locals() else None)
    return {"name": name}


def get_next_invoice_number(headers, realm_id, env_override=None, fallback_start=1001):
    """
    Query the latest invoice DocNumber and return +1 (as string).
    If none found or non-numeric, start at fallback_start.
    """
    import requests
    base = get_base_qbo_url(env_override)
    query_url = f"{base}/v3/company/{realm_id}/query?minorversion=65"
    query = "SELECT DocNumber FROM Invoice ORDER BY MetaData.CreateTime DESC MAXRESULTS 1"
    r = requests.get(query_url, headers=headers, params={"query": query})
    if r.status_code == 200:
        resp = r.json().get("QueryResponse", {})
        invs = resp.get("Invoice", [])
        if invs:
            last = (invs[0].get("DocNumber") or "").strip()
            if last.isdigit():
                return str(int(last) + 1)
    return str(fallback_start)


def fetch_customer_email_from_directory(sheet_service, company_name):
    """
    Read the 'Directory' sheet and return the Contact Email Address for the company.
    """
    SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
    if not SPREADSHEET_ID:
        logging.warning("⚠️ Missing SPREADSHEET_ID; cannot read Directory for email.")
        return ""
    resp = sheet_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Directory!A1:Z"
    ).execute()
    rows = resp.get("values", []) or []
    if len(rows) < 2:
        return ""
    headers = rows[0]
    try:
        idx_company = headers.index("Company Name")
        idx_email   = headers.index("Contact Email Address")
    except ValueError:
        return ""
    for row in rows[1:]:
        if len(row) > idx_company and row[idx_company].strip() == (company_name or "").strip():
            return row[idx_email] if len(row) > idx_email else ""
    return ""


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

    # ── Build invoice fields we’re adding ──────────────────────────
    txn_date_str = datetime.now().strftime("%Y-%m-%d")
    # Directory email for this customer
    sheet_service = get_sheets_service()
    bill_email = fetch_customer_email_from_directory(sheet_service, order_data.get("Company Name", "")) or ""
    # Force Ship Via = UPS
    ship_method_ref = get_or_create_ship_method_ref("UPS", headers, realm_id, env_override)
    # Next DocNumber (+1)
    doc_number = get_next_invoice_number(headers, realm_id, env_override)

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
        "TxnDate":       txn_date_str,
        "ShipDate":      txn_date_str,
        "DocNumber":     doc_number,
        "TotalAmt":      float(round(amount, 2)),
        "SalesTermRef":  { "value": "3" },
        "BillEmail":     {"Address": bill_email} if bill_email else None,
        "ShipMethodRef": ship_method_ref
    }
    # Remove any None values QBO might reject (e.g., missing BillEmail)
    invoice_payload = {k: v for k, v in invoice_payload.items() if v is not None}


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
    txn_date_str = datetime.now().strftime("%Y-%m-%d")

    # Directory email for this customer
    sheet_service = get_sheets_service()
    bill_email = fetch_customer_email_from_directory(
        sheet_service,
        first_order.get("Company Name", "")
    ) or ""

    # Force Ship Via = UPS
    ship_method_ref = get_or_create_ship_method_ref("UPS", headers, realm_id, env_override)

    # Next DocNumber (+1 from last)
    doc_number = get_next_invoice_number(headers, realm_id, env_override)

    invoice_payload = {
        "CustomerRef":  customer_ref,
        "Line":         line_items,
        "TxnDate":      txn_date_str,
        "ShipDate":     txn_date_str,
        "DocNumber":    doc_number,
        "TotalAmt":     round(sum(item["Amount"] for item in line_items), 2),
        "SalesTermRef": {"value": "3"},
        "BillEmail":    {"Address": bill_email} if bill_email else None,
        "ShipMethodRef": ship_method_ref,
    }
    # Drop any None values QBO might reject (e.g., missing BillEmail)
    invoice_payload = {k: v for k, v in invoice_payload.items() if v is not None}

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
    max_http_buffer_size=1_000_000,
    logger=False,            # NEW: silence Socket.IO logs
    engineio_logger=False,   # NEW: silence low-level engine logs
)



# ─── Vendor Directory Cache ─────────────────────────────────────────────────
_vendor_dir_cache = None
_vendor_dir_ts    = 0
VENDOR_TTL        = 600  # 10 minutes

def _hdr_index(headers):
    return {str(h or "").strip().lower(): i for i, h in enumerate(headers or [])}

def read_vendor_directory_from_material_inventory():
    """Reads Material Inventory!K1:O as: Vendor | Method | Email | CC | Website"""
    import time as _t
    global _vendor_dir_cache, _vendor_dir_ts
    now = _t.time()
    if _vendor_dir_cache and (now - _vendor_dir_ts) < VENDOR_TTL:
        return _vendor_dir_cache

    svc = get_sheets_service().spreadsheets().values()
    vals = svc.get(
        spreadsheetId=SPREADSHEET_ID,
        range="Material Inventory!K1:O",
        valueRenderOption="FORMATTED_VALUE"
    ).execute().get("values", []) or []

    out = {}
    if vals:
        idx = _hdr_index(vals[0])
        for r in vals[1:]:
            def gv(key):
                i = idx.get(key)
                return (r[i].strip() if i is not None and i < len(r) and r[i] is not None else "")
            vname = gv("vendor").strip()
            if not vname:
                continue
            key = vname.lower()
            out[key] = {
                "vendor":  vname,                          # keep original for display if needed
                "method":  (gv("method") or "").lower(),
                "email":   gv("email"),
                "cc":      gv("cc"),
                "website": gv("website"),
            }

    _vendor_dir_cache = out
    _vendor_dir_ts = now
    return out


@app.route("/api/vendors")
@login_required_session
def get_vendors():
    """Returns [{vendor, method, email, cc, website}, ...] from Material Inventory tab (K:O)."""
    try:
        m = read_vendor_directory_from_material_inventory()
        return jsonify({"vendors": [{"vendor": k, **v} for k, v in m.items()]})
    except Exception:
        app.logger.exception("vendors failed")
        return jsonify({"error":"vendors failed"}), 500


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
FUR_RANGE        = os.environ.get("FUR_RANGE",        "Fur List!A1:Z")
CUT_RANGE    = os.environ.get("CUT_RANGE",    "Cut List!A1:Z")
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
        job_id    = data.get("id")
        iso_start = clamp_iso_to_next_830_et(data.get("startTime"))  # enforce window server-side

        if not job_id or not iso_start:
            return jsonify({"error": "Missing job ID or start time"}), 400

        # Write ISO directly to Production Orders
        update_embroidery_start_time_in_sheet(job_id, iso_start)

        # 🔔 Notify all clients so UIs patch immediately
        try:
            socketio.emit("startTimeUpdated", {"orderId": str(job_id), "startTime": iso_start})
        except Exception:
            app.logger.exception("socket emit failed for startTimeUpdated")

        return jsonify({"status": "success"}), 200
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

# overview + jobs-for-company micro-caches
_overview_cache     = None
_overview_ts        = 0
_jobs_company_cache = {}   # { company_lower: {"ts": <epoch>, "data": {...}} }

# combined: micro-cache + timestamp
_combined_cache = None
_combined_ts    = 0.0

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
# ─── ETag helper (weak ETag based on JSON bytes) ─────────────────────────────
def _json_etag(payload_bytes: bytes) -> str:
    import hashlib as _hashlib
    return 'W/"%s"' % _hashlib.sha1(payload_bytes).hexdigest()
# ─────────────────────────────────────────────────────────────────────────────


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
    """Write ISO start time into the 'Embroidery Start Time' column for the given Order #."""
    try:
        # 1) Read headers & rows
        rows = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE, value_render_option="UNFORMATTED_VALUE")
        if not rows:
            raise RuntimeError("orders range returned no rows")
        headers = [str(h).strip() for h in rows[0]]
        try:
            id_idx    = headers.index("Order #")
            start_idx = headers.index("Embroidery Start Time")
        except ValueError as e:
            raise RuntimeError("Missing 'Order #' or 'Embroidery Start Time' header") from e

        # 2) Locate target row
        row_num = None
        for i, r in enumerate(rows[1:], start=2):
            val = "" if id_idx >= len(r) else r[id_idx]
            if str(val).strip() == str(order_id).strip():
                row_num = i
                break
        if not row_num:
            raise RuntimeError(f"Order # {order_id} not found in range {ORDERS_RANGE}")

        # 3) Compute A1 for the start cell
        def col_to_a1(idx0):
            n = idx0 + 1
            s = ""
            while n:
                n, rem = divmod(n - 1, 26)
                s = chr(65 + rem) + s
            return s

        start_col_a1 = col_to_a1(start_idx)
        target_range = f"Production Orders!{start_col_a1}{row_num}"

        # 4) Write ISO string exactly
        write_sheet(SPREADSHEET_ID, target_range, [[new_start_time]])
        return True
    except Exception as e:
        app.logger.exception("Failed to update Embroidery Start Time")
        raise



def fetch_sheet(spreadsheet_id, rng, value_render=None):
    kwargs = {}
    if value_render:  # e.g., "UNFORMATTED_VALUE"
        kwargs["valueRenderOption"] = value_render

    resp = sheets.values().get(
        spreadsheetId=spreadsheet_id,
        range=rng,
        **kwargs
    ).execute()
    return resp.get("values", [])



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
    next_url = request.args.get("next") or request.form.get("next") or FRONTEND_URL

    if request.method == "POST":
        # We only care about the password. Username can be anything.
        p = (request.form.get("password") or "").strip()
        ADMIN_PW = (os.environ.get("ADMIN_PASSWORD") or "").strip()
        ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

        if not ADMIN_PW:
            error = "Server is missing ADMIN_PASSWORD. Ask the admin to set it."
        elif p == ADMIN_PW:
            session.clear()
            session["user"] = "admin"  # single-user auth
            session["token_at_login"] = ADMIN_TOKEN
            session["last_activity"] = datetime.utcnow().isoformat()
            session["login_time"] = time.time()
            session["login_ts"] = datetime.utcnow().timestamp()

            # If they posted a backend path, still send them to the frontend root
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

import traceback
from googleapiclient.errors import HttpError

def _drive_id_from_link(s: str) -> str:
    s = str(s or "")
    m = re.search(r'[?&]id=([A-Za-z0-9_-]+)', s)
    if m:
        return m.group(1)
    m = re.search(r'/file/d/([A-Za-z0-9_-]+)', s)
    if m:
        return m.group(1)
    return ""



# ── OVERVIEW endpoint available at /overview AND /api/overview ──────────────
@app.route("/overview", methods=["GET"], endpoint="overview_plain")
@app.route("/api/overview", methods=["GET"], endpoint="overview_api")
@login_required_session
def overview_combined():
    """
    Returns: { upcoming: [...], materials: [...], daysWindow: "7" }
    - 15s micro-cache + ETag/304
    - On error: logs and returns last-good or empty payload (never 500s)
    Requires your existing: get_sheets_service(), sheet_lock, SPREADSHEET_ID
    """
    # 15s micro-cache + ETag/304
    global _overview_cache, _overview_ts
    now = time.time()
    if _overview_cache is not None and (now - _overview_ts) < 15:
        payload_bytes = json.dumps(_overview_cache, separators=(",", ":")).encode("utf-8")
        etag = _json_etag(payload_bytes)
        inm = request.headers.get("If-None-Match", "")
        if etag and inm and etag in inm:
            resp = make_response("", 304)
            resp.headers["ETag"] = etag
            resp.headers["Cache-Control"] = "public, max-age=15"
            return resp
        resp = Response(payload_bytes, mimetype="application/json")
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "public, max-age=15"
        return resp

    try:
        # ---- Read ranges from the Overview sheet ----------------------------
        svc = get_sheets_service().spreadsheets().values()

        # 2 quick attempts to ride out transient network hiccups
        resp = None
        for attempt in (1, 2):
            try:
                with sheet_lock:
                    resp = svc.batchGet(
                        spreadsheetId=SPREADSHEET_ID,
                        ranges=["Overview!A3:K", "Overview!M3:M", "Overview!B1", "Overview!Y3:Y"],
                        valueRenderOption="UNFORMATTED_VALUE",
                        fields="valueRanges(values)"
                    ).execute()
                break  # success → leave the loop
            except Exception as e:
                app.logger.warning("Sheets batchGet failed (attempt %d): %s", attempt, e)
                if attempt == 2:
                    raise
                time.sleep(0.6)  # tiny backoff before final try

        vrs = resp.get("valueRanges", []) if resp else []

        # ---------- UPCOMING ----------
        TARGET_HEADERS = [
            "Order #","Preview","Company Name","Design","Quantity",
            "Product","Stage","Due Date","Print","Ship Date","Hard Date/Soft Date"
        ]
        up_vals = (vrs[0].get("values") if len(vrs) > 0 and isinstance(vrs[0].get("values"), list) else []) or []
        # Drop header row if present
        if up_vals:
            first_row = [str(x or "").strip() for x in up_vals[0]]
            if [h.lower() for h in first_row] == [h.lower() for h in TARGET_HEADERS]:
                up_vals = up_vals[1:]

        # Y column values (raw Drive links)
        y_vals = (vrs[3].get("values") if len(vrs) > 3 and isinstance(vrs[3].get("values"), list) else []) or []

        upcoming = []
        for i, r in enumerate(up_vals):
            r = (r or []) + [""] * (len(TARGET_HEADERS) - len(r))
            row = dict(zip(TARGET_HEADERS, r))

            # derive imageUrl from the raw link in column Y for the same row index
            link = ""
            if i < len(y_vals) and y_vals[i]:
                link = y_vals[i][0] if len(y_vals[i]) else ""
            fid = _drive_id_from_link(link)
            if fid:
                backend_root = request.url_root.rstrip("/")
                row["imageUrl"] = f"{backend_root}/api/drive/proxy/{fid}?sz=w160"


            upcoming.append(row)
        # ---------- MATERIALS ----------
        mat_vals = (vrs[1].get("values") if len(vrs) > 1 and isinstance(vrs[1].get("values"), list) else []) or []
        grouped = {}
        for row in mat_vals:
            s = str((row[0] if row else "") or "").strip()
            if not s:
                continue
            # Split: "... Qty Unit - Vendor"
            vendor = "Misc."
            left = s
            if " - " in s:
                left, vendor = s.rsplit(" - ", 1)
                vendor = (vendor or "Misc.").strip() or "Misc."

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
                grouped.setdefault(vendor, []).append({
                    "name": name, "qty": qty, "unit": unit, "type": typ
                })

        materials = [{"vendor": v, "items": items} for v, items in grouped.items()]

        # ---- Read B1 for days window ---------------------------------------
        days_window = "7"
        try:
            b1_vals = (vrs[2].get("values") if len(vrs) > 2 and isinstance(vrs[2].get("values"), list) else []) or []
            if b1_vals and b1_vals[0] and b1_vals[0][0] is not None:
                days_window = str(b1_vals[0][0]).strip() or "7"
        except Exception:
            pass

        # ---- Cache + respond -----------------------------------------------
        resp_data = {"upcoming": upcoming, "materials": materials, "daysWindow": days_window}
        _overview_cache = resp_data
        _overview_ts = now

        payload_bytes = json.dumps(resp_data, separators=(",", ":")).encode("utf-8")
        etag = _json_etag(payload_bytes)
        resp = Response(payload_bytes, mimetype="application/json")
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "public, max-age=15"
        return resp

    except Exception:
        app.logger.exception("overview_combined failed")
        # Serve last-good if we have it
        if _overview_cache is not None:
            payload_bytes = json.dumps(_overview_cache, separators=(",", ":")).encode("utf-8")
            etag = _json_etag(payload_bytes)
            resp = Response(payload_bytes, mimetype="application/json")
            resp.headers["ETag"] = etag
            resp.headers["Cache-Control"] = "public, max-age=15"
            return resp
        # Otherwise serve empty payload (200)
        fallback = {"upcoming": [], "materials": []}
        payload_bytes = json.dumps(fallback, separators=(",", ":")).encode("utf-8")
        etag = _json_etag(payload_bytes)
        resp = Response(payload_bytes, mimetype="application/json")
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "public, max-age=15"
        return resp

# ─────────────────────────────────────────────────────────────────────────────


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
# ─── Fur: mark complete (write Quantity Made in "Fur List") ───────────────────
@app.route("/api/fur/complete", methods=["POST", "OPTIONS"])
@login_required_session
def api_fur_complete():
    try:
        data = request.get_json(silent=True) or {}
        order_id = str(data.get("orderId") or data.get("order_id") or "").strip()
        quantity = data.get("quantity")

        if not order_id:
            return jsonify({"error": "Missing 'orderId'"}), 400
        try:
            qty_int = int(quantity)
        except Exception:
            return jsonify({"error": "Invalid 'quantity'"}), 400

        # Read Fur List to locate the row for this Order #
        svc = get_sheets_service().spreadsheets().values()
        resp = svc.get(
            spreadsheetId=SPREADSHEET_ID,
            range="Fur List!A1:ZZ",
            valueRenderOption="FORMATTED_VALUE"
        ).execute()
        rows = resp.get("values", []) or []
        if not rows:
            return jsonify({"error": "Fur List tab is empty"}), 500

        headers = [str(h or "").strip() for h in rows[0]]
        def idx_of(name):
            try:
                return headers.index(name)
            except ValueError:
                return -1

        order_idx = idx_of("Order #")
        qty_idx   = idx_of("Quantity Made")
        if order_idx < 0 or qty_idx < 0:
            return jsonify({"error": "Missing 'Order #' or 'Quantity Made' header in Fur List"}), 500

        # find row where Order # matches
        row_num = None
        for i, r in enumerate(rows[1:], start=2):
            val = r[order_idx] if order_idx < len(r) else ""
            if str(val).strip() == order_id:
                row_num = i
                break

        if not row_num:
            return jsonify({"error": f"Order # {order_id} not found in Fur List"}), 404

        # small helper: 0-based column index → A1 letter(s)
        def col_to_a1(idx0):
            n = idx0 + 1
            s = ""
            while n:
                n, rem = divmod(n - 1, 26)
                s = chr(65 + rem) + s
            return s

        target_cell = f"Fur List!{col_to_a1(qty_idx)}{row_num}"
        write_sheet(SPREADSHEET_ID, target_cell, [[qty_int]])

        # (Optional) we could emit a socket event here; UI already removes the card.
        return jsonify({"ok": True, "orderId": order_id, "wrote": qty_int})

    except Exception:
        logger.exception("Error in /api/fur/complete")
        return jsonify({"error": "Internal error"}), 500

@app.route("/api/cut/complete", methods=["POST"])
@login_required_session
def cut_complete():
    data = request.get_json(silent=True) or {}
    order_id = str(data.get("orderId", "")).strip()
    quantity = data.get("quantity", 0)

    if not order_id:
        return jsonify(ok=False, error="Missing orderId"), 400

    try:
        # Try to init Sheets client; fail fast with a clear error
        try:
            svc = get_sheets_service().spreadsheets().values()
        except RuntimeError as e:
            app.logger.error("cut_complete: Google credentials missing: %s", e)
            return jsonify(ok=False, error="Server is not connected to Google. Add GOOGLE_TOKEN_JSON or token.json."), 503
        except Exception as e:
            app.logger.exception("cut_complete: unable to init Google Sheets client")
            return jsonify(ok=False, error="Could not initialize Google Sheets client."), 503

        # 1) Load the whole Cut List to find the row by "Order #"
        with sheet_lock:
            find_resp = svc.get(
                spreadsheetId=SPREADSHEET_ID,
                range="Cut List!A1:ZZ",
                valueRenderOption="UNFORMATTED_VALUE",
            ).execute()
        rows = find_resp.get("values", []) or []
        if not rows:
            return jsonify(ok=False, error="Cut List is empty"), 404

        headers = [str(h).strip() for h in rows[0]]
        order_idx = headers.index("Order #") if "Order #" in headers else None
        if order_idx is None:
            return jsonify(ok=False, error="Order # column not found in Cut List"), 400

        row_num = None  # 1-based
        for i in range(1, len(rows)):
            r = rows[i] or []
            val = str(r[order_idx] if order_idx < len(r) else "").strip()
            if val == order_id:
                row_num = i + 1
                break

        if not row_num:
            return jsonify(ok=False, error=f"Order {order_id} not found in Cut List"), 404

        # Get H..R for that row (sources)
        with sheet_lock:
            src_resp = svc.get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"Cut List!H{row_num}:R{row_num}",
                valueRenderOption="UNFORMATTED_VALUE",
            ).execute()
        src_vals = (src_resp.get("values") or [[]])[0]
        # Pad to 11 cells (H..R)
        while len(src_vals) < 11:
            src_vals.append("")

        # Build writes to I..S where source has text
        updates = []
        for idx in range(11):  # 0..10 for H..R
            src_val = str(src_vals[idx]).strip()
            if src_val != "":
                # destination column is next to the right: (H+idx)+1
                dest_col_letter = chr(ord('H') + idx + 1)  # I..S
                updates.append({
                    "range": f"Cut List!{dest_col_letter}{row_num}",
                    "values": [[quantity]],
                })

        if not updates:
            return jsonify(ok=True, wrote=0)  # nothing to write

        # Batch update
        body = {
            "valueInputOption": "USER_ENTERED",
            "data": updates
        }
        with sheet_lock:
            svc.batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body=body
            ).execute()

        return jsonify(ok=True, wrote=len(updates))
    except Exception as e:
        app.logger.exception("cut_complete failed")
        return jsonify(ok=False, error=str(e)), 500

@app.route("/api/cut/completeBatch", methods=["POST"])
@login_required_session
def cut_complete_batch():
    """
    Body: { items: [ {orderId: "<Order #>", quantity: <number>}, ... ] }
    For each item, in the Cut List row:
      Look at H..R (inclusive). For each cell that has text, write 'quantity' into the cell to its right (I..S).
    All rows are handled in one Sheets batchUpdate to avoid timeouts.
    """
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []
    if not isinstance(items, list) or not items:
        return jsonify(ok=False, error="No items to complete"), 400

    # Initialize Sheets client (fail fast if creds missing)
    try:
        vsvc = get_sheets_service().spreadsheets().values()
    except RuntimeError as e:
        app.logger.error("cut_complete_batch: Google credentials missing: %s", e)
        return jsonify(ok=False, error="Server is not connected to Google. Add GOOGLE_TOKEN_JSON or token.json."), 503
    except Exception:
        app.logger.exception("cut_complete_batch: unable to init Google Sheets client")
        return jsonify(ok=False, error="Could not initialize Google Sheets client."), 503

    # Helper: 0-based column index → A1 letter(s)
    def col_letter(idx0: int) -> str:
        n = idx0 + 1
        s = ""
        while n:
            n, rem = divmod(n - 1, 26)
            s = chr(65 + rem) + s
        return s

    try:
        # Load entire Cut List once
        with sheet_lock:
            resp = vsvc.get(
                spreadsheetId=SPREADSHEET_ID,
                range="Cut List!A1:ZZ",
                valueRenderOption="UNFORMATTED_VALUE",
            ).execute()

        rows = resp.get("values", []) or []
        if not rows:
            return jsonify(ok=False, error="Cut List is empty"), 404

        headers = [str(h).strip() for h in rows[0]]
        if "Order #" not in headers:
            return jsonify(ok=False, error="Order # column not found in Cut List"), 400
        order_ix = headers.index("Order #")

        # Map Order # → 1-based row number
        row_index = {}
        for i in range(1, len(rows)):
            r = rows[i] or []
            key = str(r[order_ix] if order_ix < len(r) else "").strip()
            if key:
                row_index[key] = i + 1

        updates = []
        missing = []
        wrote_cells = 0

        for it in items:
            oid = str(it.get("orderId", "")).strip()
            qty = it.get("quantity", 0)
            if not oid:
                continue

            row_num = row_index.get(oid)
            if not row_num:
                missing.append(oid)
                continue

            # Pull the whole row from the cached 'rows'
            row = rows[row_num - 1] or []

            # Ensure row has at least up to column S (index 18)
            while len(row) <= 18:
                row.append("")

            # H..R are indices 7..17 inclusive (0-based). If cell has text, write qty to next col (I..S → 8..18)
            for src_ix in range(7, 18):
                has_text = str(row[src_ix]).strip() != ""
                if has_text:
                    dest_ix = src_ix + 1
                    dest_letter = col_letter(dest_ix)
                    updates.append({
                        "range": f"Cut List!{dest_letter}{row_num}",
                        "values": [[qty]],
                    })
                    wrote_cells += 1

        if not updates:
            return jsonify(ok=True, wrote=0, updated=0, missing=missing)

        body = {"valueInputOption": "USER_ENTERED", "data": updates}
        with sheet_lock:
            vsvc.batchUpdate(spreadsheetId=SPREADSHEET_ID, body=body).execute()

        app.logger.info("cut_complete_batch: updated orders=%d wrote cells=%d missing=%d",
                        len(items) - len(missing), wrote_cells, len(missing))
        return jsonify(ok=True, wrote=wrote_cells, updated=len(items) - len(missing), missing=missing)

    except Exception as e:
        app.logger.exception("cut_complete_batch failed")
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/orders", methods=["GET"])
@login_required_session
def get_orders():
    TTL = 15  # seconds

    # Only fields MaterialLog needs
    KEEP = {"Order #", "Company Name", "Design", "Product", "Stage", "Due Date", "Quantity"}


    def build_payload():
        # Pull unformatted to keep payload lean
        rows = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE, value_render="UNFORMATTED_VALUE") or []
        if not rows:
            return []

        headers = [str(h or "").strip() for h in rows[0]]
        hi = {h: i for i, h in enumerate(headers) if h}

        out = []
        for r in rows[1:]:
            if not r:
                continue
            o = {}
            for k in KEEP:
                i = hi.get(k)
                o[k] = r[i] if (i is not None and i < len(r)) else ""
            out.append(o)
        return out

    return send_cached_json("orders", TTL, build_payload)



@app.route("/api/material-log/original-usage", methods=["GET"])  # ?order=123
@login_required_session
def material_log_original_usage():
    global _matlog_cache
    order = (request.args.get("order") or "").strip()
    if not order:
        return jsonify({"error": "Missing order"}), 400

    now = time.time()
    fresh = (_matlog_cache and (now - _matlog_cache["ts"] < _matlog_cache_ttl))

    if not fresh:
        # Rebuild cache once per TTL
        rows = fetch_sheet(SPREADSHEET_ID, "Material Log!A1:Z") or []
        if not rows:
            _matlog_cache = {"by_order": {}, "ts": now}
        else:
            hdr = rows[0]
            hi = _hdr_idx(hdr)
            units_lookup = _get_material_units_lookup()

            by_order = {}
            for idx, r in enumerate(rows[1:], start=2):
                def gv(key):
                    i = hi.get(key)
                    return r[i] if i is not None and i < len(r) else ""

                order_val = str(gv("order #")).strip()
                if not order_val:
                    continue

                recut_val = str(gv("recut")).strip().lower()
                if recut_val == "recut":
                    continue

                try:
                    qty_pieces = int(float(gv("quantity")))
                except Exception:
                    qty_pieces = 0
                try:
                    qty_units = float(gv("qty"))
                except Exception:
                    qty_units = 0.0

                material_name = str(gv("material"))
                unit = units_lookup.get(material_name, "")

                item = {
                    "id": idx,
                    "date": str(gv("date")),
                    "order": order_val,
                    "companyName": str(gv("company name")),
                    "quantity": qty_pieces,
                    "shape": str(gv("shape")),
                    "material": material_name,
                    "qtyUnits": qty_units,
                    "unit": unit,
                    "inOut": str(gv("in/out")),
                }
                by_order.setdefault(order_val, []).append(item)

            _matlog_cache = {"by_order": by_order, "ts": now}

    # Serve from cache
    items = (_matlog_cache["by_order"].get(order) if _matlog_cache else None) or []
    return jsonify({"items": items}), 200



@app.route("/api/material-log/rd-append", methods=["POST"])
@login_required_session
def material_log_rd_append():
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []
    if not isinstance(items, list) or not items:
        return jsonify({"error": "Missing items"}), 400

    # lookups (Material→Unit, Product→PPY)
    units = _get_material_units_lookup()
    ppy   = _get_ppy_lookup()

    to_append = []
    now_iso = datetime.utcnow().isoformat()

    for it in items:
        mat = (it.get("material") or "").strip()
        prod = (it.get("product") or "").strip()
        w_in = it.get("widthIn")
        l_in = it.get("lengthIn")
        qty  = it.get("quantity")

        # 1) material required
        if not mat:
            return jsonify({"error": "Select the correct material"}), 400

        # 2) whole numbers only
        if not isinstance(qty, (int, float)) or int(qty) != qty or qty <= 0:
            return jsonify({"error": "Quantity must be a whole number"}), 400
        qty = int(qty)

        # 3) one of Product or W×L; if both present, Product wins
        if not prod and not (w_in and l_in):
            return jsonify({"error": "Provide Product or W×L"}), 400
        if prod:
            w_in = None
            l_in = None

        # 4) units must be present & be exactly "Yards" or "Sqft"
        unit_str = units.get(mat)
        if not unit_str:
            return jsonify({"error": f"Material '{mat}' not found in Material Inventory"}), 400
        if unit_str not in ("Yards", "Sqft"):
            return jsonify({"error": f"Unsupported unit '{unit_str}' for material '{mat}'"}), 400

        used = _compute_usage_units(mat, unit_str, prod, w_in, l_in, qty, ppy)
        shape = prod or (f"{int(w_in)}x{int(l_in)}" if (w_in and l_in) else "")

        # Date | Order # | Company Name | Quantity | Shape | Material | QTY | IN/OUT | O/R | Recut
        to_append.append([now_iso, "R&D", "", qty, shape, mat, used, "OUT", "", ""])

    svc = get_sheets_service().spreadsheets().values()
    with sheet_lock:
        svc.append(
            spreadsheetId=SPREADSHEET_ID,
            range="Material Log!A1:Z",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": to_append}
        ).execute()

    return jsonify({"added": len(to_append)}), 200

@app.route("/api/material-log/recut-append", methods=["POST"])
@login_required_session
def material_log_recut_append():
    data = request.get_json(silent=True) or {}
    order_number = str(data.get("orderNumber") or "").strip()
    items        = data.get("items") or []  # each has per-item recutQty (whole number)

    if not order_number:
        return jsonify({"error": "Missing orderNumber"}), 400
    if not items:
        return jsonify({"error": "No items selected"}), 400

    now_iso = datetime.utcnow().isoformat()
    rows = []
    for it in items:
        company = str(it.get("companyName") or "").strip()
        shape   = str(it.get("shape") or "").strip()
        mat     = str(it.get("material") or "").strip()
        orig_q  = it.get("originalQuantity") or 0
        orig_u  = it.get("originalUnits") or 0.0

        # Per-material recut qty (must be whole number > 0)
        rq = it.get("recutQty")
        if not isinstance(rq, (int, float)) or int(rq) != rq or rq <= 0:
            continue
        rq = int(rq)

        try:
            per_piece = float(orig_u) / max(1, int(float(orig_q)))
        except Exception:
            per_piece = 0.0

        used = per_piece * rq
        # Date | Order # | Company Name | Quantity | Shape | Material | QTY | IN/OUT | O/R | Recut
        rows.append([now_iso, order_number, company, rq, shape, mat, used, "OUT", "", "Recut"])

    if not rows:
        return jsonify({"error": "No valid recut quantities provided"}), 400

    svc = get_sheets_service().spreadsheets().values()
    with sheet_lock:
        svc.append(
            spreadsheetId=SPREADSHEET_ID,
            range="Material Log!A1:Z",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows}
        ).execute()

    return jsonify({"added": len(rows)}), 200


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

    # 🔸 15s micro-cache per company
    global _jobs_company_cache
    now = time.time()
    cached = _jobs_company_cache.get(company)
    if cached and (now - cached["ts"] < 15):
        return jsonify(cached["data"])

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

    data = {"jobs": jobs}
    _jobs_company_cache[company] = {"ts": now, "data": data}
    return jsonify(data)



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

# KEEP this handler
@app.route("/api/combined", methods=["GET"], endpoint="api_combined")
@login_required_session
def get_combined():
    """
    Returns: { orders: [...], links: {...} }
    Uses 20s micro-cache and ETag/304.
    """
    TTL = 20

    def build_payload():
        svc = get_sheets_service().spreadsheets().values()
        with sheet_lock:
            resp = svc.batchGet(
                spreadsheetId=SPREADSHEET_ID,
                ranges=[ORDERS_RANGE, FUR_RANGE, CUT_RANGE],  # add Cut
                valueRenderOption="UNFORMATTED_VALUE",
            ).execute()

        vrs = resp.get("valueRanges", [])
        orders_rows = (vrs[0].get("values") if len(vrs) > 0 else []) or []
        fur_rows    = (vrs[1].get("values") if len(vrs) > 1 else []) or []
        cut_rows    = (vrs[2].get("values") if len(vrs) > 2 else []) or []

        def rows_to_dicts(rows):
            if not rows:
                return []
            headers = [str(h).strip() for h in rows[0]]
            out = []
            for r in rows[1:]:
                r = r or []
                if len(r) < len(headers):
                    r += [""] * (len(headers) - len(r))
                out.append(dict(zip(headers, r)))
            return out

        orders_full = rows_to_dicts(orders_rows)
        fur_full    = rows_to_dicts(fur_rows)
        cut_full    = rows_to_dicts(cut_rows)

        # Build lookups by Order #
        fur_map = { str(r.get("Order #", "")).strip(): r
                    for r in fur_full if str(r.get("Order #", "")).strip() }
        cut_map = { str(r.get("Order #", "")).strip(): r
                    for r in cut_full if str(r.get("Order #", "")).strip() }

        # Merge statuses into the Production Orders objects
        for o in orders_full:
            oid = str(o.get("Order #", "")).strip()
            if not oid:
                continue

            fr = fur_map.get(oid)
            if fr and "Status" in fr:
                # keep generic Status (used by Fur tab) and also label it explicitly
                o["Status"] = fr.get("Status", "")
                o["Fur Status"] = fr.get("Status", "")

            cr = cut_map.get(oid)
            if cr and "Status" in cr:
                # used by Cut tab filtering
                o["Cut Status"] = cr.get("Status", "")

        return {"orders": orders_full, "links": _links_store}

    # Allow client to force rebuild right after writes
    if request.args.get("refresh") == "1":
        payload_obj   = build_payload()
        payload_bytes = json.dumps(payload_obj, separators=(",", ":")).encode("utf-8")
        etag          = _json_etag(payload_bytes)
        _cache_set("combined", payload_bytes, TTL)
        resp = Response(payload_bytes, mimetype="application/json")
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = f"public, max-age={TTL}, stale-while-revalidate=300"
        return resp

    return send_cached_json("combined", TTL, build_payload)

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
    """
    Read manual state from the sheet, but PRUNE any order IDs whose Stage is
    'Sewing' or 'Complete' in Production Orders. If pruning occurs, write the
    cleaned lists back to Manual State I2:J2 so the sheet self-heals.
    """
    global _manual_state_cache, _manual_state_ts
    now = time.time()

    global _manual_state_cache, _manual_state_ts
    now = time.time()

    if _manual_state_cache and (now - _manual_state_ts) < 30:
        return jsonify(_manual_state_cache), 200


    # >>> ADDED: 10s fast-path from in-memory cache
    if _manual_state_cache and (now - _manual_state_ts) < 10:
        return jsonify(_manual_state_cache), 200

    try:
        # 1) Read Manual State rows (A2:Z) and parse machine columns (I–Z)
        resp = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=MANUAL_RANGE,  # e.g., "Manual State!I2:J2"
            valueRenderOption="UNFORMATTED_VALUE"
        ).execute()
        rows = resp.get("values", []) or []

        # pad each row to 26 columns
        for i in range(len(rows)):
            while len(rows[i]) < 26:
                rows[i].append("")

        first = rows[0] if rows else [""] * 26
        machines = first[8:26]  # I–Z
        machine_columns = [[s for s in str(col or "").split(",") if s] for col in machines]

        # 2) Build set of ACTIVE (not Sewing/Complete) order IDs from Production Orders
        ord_rows = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE)
        active_ids = set()
        if ord_rows:
            hdr = {str(h).strip().lower(): idx for idx, h in enumerate(ord_rows[0])}
            id_idx = hdr.get("order #", 0)
            stage_idx = hdr.get("stage", None)
            for r in ord_rows[1:]:
                oid = str(r[id_idx] if id_idx < len(r) else "").strip()
                stage = str(r[stage_idx] if stage_idx is not None and stage_idx < len(r) else "").strip().lower()
                # keep only if NOT Sewing or Complete
                if oid and stage not in ("sewing", "complete"):
                    active_ids.add(oid)

        # 3) Prune completed/sewing/unknown IDs from each machine list
        cleaned = [[oid for oid in col if oid in active_ids] for col in machine_columns]

        # 4) If cleaned differs, write fixed strings back to I2:J2
        if cleaned != machine_columns:
            i2 = ",".join(cleaned[0]) if len(cleaned) > 0 else ""
            j2 = ",".join(cleaned[1]) if len(cleaned) > 1 else ""
            sheets.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{MANUAL_RANGE.split('!')[0]}!I2:J2",
                valueInputOption="RAW",
                body={"values": [[i2, j2]]}
            ).execute()
            machine_columns = cleaned

        # 5) Gather placeholders from A–H (unchanged)
        phs = []
        for r in rows:
            if str(r[0]).strip():
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
    """
    Save manual state (machine1/machine2 order + placeholders), but FIRST
    prune any order IDs whose Stage is 'Sewing' or 'Complete' in Production Orders.
    """
    try:
        data = request.get_json(force=True) or {}
        incoming_m1 = [str(x).strip() for x in data.get("machine1", []) if str(x).strip()]
        incoming_m2 = [str(x).strip() for x in data.get("machine2", []) if str(x).strip()]
        placeholders = data.get("placeholders", [])

        # --- Build ACTIVE ID set from Production Orders (exclude Stage == 'sewing' or 'complete') ---
        active_ids = set()
        try:
            ord_rows = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE)
            if ord_rows:
                headers = {str(h).strip().lower(): idx for idx, h in enumerate(ord_rows[0])}
                id_idx = headers.get("order #", 0)
                stage_idx = headers.get("stage", None)
                for r in ord_rows[1:]:
                    oid = str(r[id_idx] if id_idx < len(r) else "").strip()
                    stage = str(r[stage_idx] if stage_idx is not None and stage_idx < len(r) else "").strip().lower()
                    # ✅ Keep only if not Sewing or Complete
                    if oid and stage not in ("sewing", "complete"):
                        active_ids.add(oid)
        except Exception:
            # If orders can't be fetched, fail-open: keep whatever came in
            logger.exception("Could not fetch Production Orders; skipping prune in POST /api/manualState")
            active_ids = set(incoming_m1 + incoming_m2)

        # --- Prune off jobs no longer active ---
        pruned_m1 = [oid for oid in incoming_m1 if oid in active_ids]
        pruned_m2 = [oid for oid in incoming_m2 if oid in active_ids]
        removed_ids = (set(incoming_m1 + incoming_m2) - set(pruned_m1 + pruned_m2))

        # --- Write the pruned lists back to Manual State (I2:J2) ---
        i2 = ",".join(pruned_m1)
        j2 = ",".join(pruned_m2)
        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{MANUAL_RANGE.split('!')[0]}!I2:J2",
            valueInputOption="RAW",
            body={"values": [[i2, j2]]}
        ).execute()

        return jsonify({
            "ok": True,
            "machineColumns": [pruned_m1, pruned_m2],
            "placeholders": placeholders,
            "removed": sorted(list(removed_ids))
        }), 200

    except Exception as e:
        logger.exception("Error in POST /api/manualState")
        return jsonify({"ok": False, "error": str(e)}), 200



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

    # 0) Build Material→Unit and Material→Vendor maps (from Inventory sheet A2:I)
    inv_rows = fetch_sheet(SPREADSHEET_ID, "Material Inventory!A2:I")
    unit_map = {
        r[0]: (r[3] if len(r) > 3 else "")
        for r in inv_rows if r and str(r[0]).strip()
    }
    vendor_map = {
        r[0]: (r[8] if len(r) > 8 else "")
        for r in inv_rows if r and str(r[0]).strip()
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
                    "unit":     unit_map.get(name, ""),
                    "vendor":   vendor_map.get(name, "")
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

@app.route("/api/inventoryOrdered/quantity", methods=["PATCH"])
@login_required_session
def update_inventory_ordered_quantity():
    """
    Update the ordered quantity for an item still in 'Ordered' status.

    JSON:
      { "type": "Material" | "Thread", "row": <1-based row>, "quantity": <str|num> }
    """
    try:
        data = request.get_json(silent=True) or {}
        sheet_type = (data.get("type") or "").strip()
        row = data.get("row")
        qty = data.get("quantity")

        try:
            row = int(row)
        except Exception:
            return jsonify({"error": "Invalid 'row'"}), 400

        if not sheet_type or qty is None:
            return jsonify({"error": "Missing 'type' or 'quantity'"}), 400

        # helper: 0-based index → A1
        def col_to_a1(idx0: int) -> str:
            n = idx0 + 1
            s = ""
            while n:
                n, rem = divmod(n - 1, 26)
                s = chr(65 + rem) + s
            return s

        if sheet_type.lower() == "material":
            # Material Log: quantity is 2 columns left of "O/R" (matches your GET)
            rows = fetch_sheet(SPREADSHEET_ID, "Material Log!A1:Z")
            if not rows:
                return jsonify({"error": "Material Log empty"}), 500

            hdr = rows[0]
            if "O/R" not in hdr:
                return jsonify({"error": "Could not find 'O/R' in Material Log"}), 500
            i_or = hdr.index("O/R")
            idx_qty = i_or - 2
            if idx_qty < 0:
                return jsonify({"error": "Invalid Quantity position"}), 500

            colA1 = col_to_a1(idx_qty)
            sheets.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"Material Log!{colA1}{row}",
                valueInputOption="USER_ENTERED",
                body={"values": [[qty]]}
            ).execute()

        else:
            # Threads: update "Length (ft)" in Thread Data
            rows = fetch_sheet(SPREADSHEET_ID, "Thread Data!A1:Z")
            if not rows:
                return jsonify({"error": "Thread Data empty"}), 500

            hdr = rows[0]
            if "Length (ft)" not in hdr:
                return jsonify({"error": "Could not find 'Length (ft)' in Thread Data"}), 500

            idx_len = hdr.index("Length (ft)")
            colA1 = col_to_a1(idx_len)
            sheets.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"Thread Data!{colA1}{row}",
                valueInputOption="USER_ENTERED",
                body={"values": [[qty]]}
            ).execute()

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print("🔥 update_inventory_ordered_quantity error:", str(e))
        traceback.print_exc()
        return jsonify({"error": "Internal server error"}), 500



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
            "slips":   [slip_url]
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

@app.route("/api/drive/warmThumbnails", methods=["POST"])
@login_required_session
def warm_thumbnails():
    """
    Pre-fetch & cache thumbnails on the server so the frontend loads instantly.
    Body: { "pairs": [ { "id": "<fileId>", "v": "<version>", "sz": "w240" }, ... ] }
    """
    try:
        data = request.get_json(force=True) or {}
        pairs = data.get("pairs") or []

        # De-dup and skip already-cached
        uniq = {}
        for p in pairs:
            fid = str(p.get("id") or "").strip()
            ver = str(p.get("v") or "").strip()
            sz  = str(p.get("sz") or "w240").strip()
            if fid and ver:
                uniq[(fid, ver, sz)] = 1
        work = [{"id": fid, "v": ver, "sz": sz} for (fid, ver, sz) in uniq.keys()]
        work = [p for p in work if not os.path.exists(_thumb_cache_path(p["id"], p["sz"], p["v"]))]

        if not work:
            return jsonify({"ok": True, "warmed": 0}), 200

        # Auth once
        creds = _load_google_creds()
        if not creds or not creds.token:
            return jsonify({"ok": False, "error": "no_creds"}), 502
        headers = {"Authorization": f"Bearer {creds.token}"}

        # Worker fn (fetches Drive thumbnail and writes cache)
        def _warm_one(p):
            try:
                fid, ver, sz = p["id"], p["v"], p["sz"]
                cpath = _thumb_cache_path(fid, sz, ver)
                if os.path.exists(cpath):
                    return True

                meta_url = (
                    f"https://www.googleapis.com/drive/v3/files/{fid}"
                    f"?fields=thumbnailLink,mimeType"
                )
                meta = requests.get(meta_url, headers=headers, timeout=6)
                if meta.status_code != 200:
                    return False
                info = meta.json() or {}
                thumb = info.get("thumbnailLink")
                if not thumb:
                    return False

                px = re.sub(r"[^0-9]", "", sz) or "240"
                if "?" in thumb:
                    if re.search(r"[?&](sz|s)=", thumb):
                        thumb = re.sub(r"([?&])(sz|s)=\d+", rf"\1s={px}", thumb)
                    else:
                        thumb = f"{thumb}&s={px}"
                else:
                    thumb = f"{thumb}?s={px}"

                img = requests.get(thumb, headers=headers, timeout=10)
                if img.status_code == 200 and img.content and img.headers.get("Content-Type","").startswith("image/"):
                    with open(cpath, "wb") as f:
                        f.write(img.content)
                    return True
            except Exception:
                logger.exception("warm one failed")
            return False

        # Fetch in parallel to speed up warm-up
        warmed = 0
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = [ex.submit(_warm_one, p) for p in work]
            for fut in as_completed(futures):
                if fut.result():
                    warmed += 1

        return jsonify({"ok": True, "warmed": warmed}), 200

    except Exception as e:
        logger.exception("warm_thumbnails error")
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/api/drive/metaBatch", methods=["POST"])
@login_required_session
def drive_meta_batch():
    """
    Return a version token per Drive file ID so the frontend can cache-bust only when artwork changes.
    Version = md5Checksum if present, else modifiedTime, else "".
    Request body: {"ids": ["<fileId1>", "<fileId2>", ...]}
    Response:     {"versions": {"<id>": "<ver>", ...}}
    """
    try:
        data = request.get_json(force=True) or {}
        ids = [str(x).strip() for x in (data.get("ids") or []) if str(x).strip()]
        versions = {}
        if not ids:
            return jsonify({"versions": versions}), 200

        svc = get_drive_service()
        for fid in ids:
            try:
                info = svc.files().get(
                    fileId=fid,
                    fields="id, md5Checksum, modifiedTime"
                ).execute()
                ver = info.get("md5Checksum") or info.get("modifiedTime") or ""
                versions[fid] = ver
            except Exception:
                logger.exception(f"drive_meta_batch: failed for {fid}")
                versions[fid] = ""

        return jsonify({"versions": versions}), 200
    except Exception as e:
        logger.exception("drive_meta_batch error")
        return jsonify({"error": str(e), "versions": {}}), 200


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

# --- Protected thread images (behind login) ---
THREAD_IMG_DIR = os.path.join(os.path.dirname(__file__), "static", "thread-images")

@app.route("/thread-images/<int:num>.<ext>", methods=["GET", "OPTIONS"])
@login_required_session
def serve_thread_image(num, ext):
    ext = (ext or "").lower()
    if ext not in ("jpg", "png", "webp"):
        return make_response(("Unsupported extension", 400))
    filename = f"{num}.{ext}"
    full_path = os.path.join(THREAD_IMG_DIR, filename)
    if not os.path.exists(full_path):
        return make_response(("Not found", 404))
    resp = make_response(send_from_directory(THREAD_IMG_DIR, filename, conditional=True))
    resp.headers["Cache-Control"] = "private, max-age=2592000, immutable"
    return resp



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

@app.route("/order/madeira", methods=["POST"])
@login_required_session
def order_madeira():
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []

    # Optional: resolve from Thread Inventory if `threads` is provided (unchanged if you already added)
    # ... keep your existing resolving code here ...

    if not items:
        return jsonify({"error": "No items provided"}), 400

    try:
        result = asyncio.run(madeira_login_and_cart(items))
        return jsonify({"status": "ok", "count": len(items), **(result or {})}), 200
    except Exception as e:
        print("🔥 madeira order error:", str(e))
        return jsonify({"error": "Failed to add to cart", "details": str(e)}), 500

# ── Material Images: robust fuzzy match (both /api/material-image and /material-image) ──
from flask import send_from_directory, redirect
import os, re, unicodedata
from urllib.parse import unquote

if "BASE_DIR" not in globals():
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MATERIAL_BASE = os.path.join(BASE_DIR, "static", "material-images")
# Map common vendor aliases → actual folder names you committed
VENDOR_ALIASES = {
    # Add aliases as needed; left side is what the sheet might say,
    # right side is the folder you created under static/material-images
    "leatherwks": "LeatherWks",
    "leather wks": "LeatherWks",
    "leatherworks": "LeatherWks",
    "carroll leathers": "Carroll Leathers",
    "upholstery supplies": "Upholstery Supplies",
}

def _clean(s: str) -> str:
    """Loose normalizer for filenames & queries: lowercase, strip accents, remove punctuation, collapse spaces."""
    s = unquote(str(s or ""))
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.replace('"', "").replace("'", "")
    s = s.replace("/", "_").replace("\\", "_")  # 1/2" => 1_2
    s = s.replace("&", "and")                   # belts & buckles -> belts and buckles
    s = re.sub(r"[^a-zA-Z0-9._\- ]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _norm_key(s: str) -> str:
    """Aggressive key for fuzzy compare: lowercase and drop spaces/_/-/dots."""
    s = _clean(s).lower()
    return re.sub(r"[ \-_.]+", "", s)

def _pick_vendor_dir(vendor_raw: str) -> str | None:
    """Choose a vendor directory: try exact, alias, and case-insensitive."""
    if not os.path.isdir(MATERIAL_BASE):
        return None
    vendor_clean = _clean(vendor_raw)
    alias = VENDOR_ALIASES.get(vendor_clean.lower())
    candidates = []
    if alias:
        candidates.append(alias)
    candidates.extend([vendor_clean, vendor_clean.lower(), vendor_clean.title(), vendor_clean.replace(" ", "_")])
    # existing folders
    try:
        folders = [d for d in os.listdir(MATERIAL_BASE) if os.path.isdir(os.path.join(MATERIAL_BASE, d))]
    except Exception:
        folders = []
    # exact/alias first
    for c in candidates:
        if c in folders:
            return os.path.join(MATERIAL_BASE, c)
    # fall back to case-insensitive match
    vc = vendor_clean.lower().replace(" ", "")
    for f in folders:
        if f.lower().replace(" ", "") == vc:
            return os.path.join(MATERIAL_BASE, f)
    return None

def _find_best_file(vendor_dir: str, name_raw: str) -> tuple[str, str] | None:
    """
    Fuzzy match a file in vendor_dir to name_raw.
    Tries exact stems with common extensions, then scans directory for best normalized match.
    Returns (dir, filename) or None.
    """
    if not vendor_dir or not os.path.isdir(vendor_dir):
        return None

    name_clean = _clean(name_raw)
    # quick exact tries for common extensions on cleaned stems
    stems = list(dict.fromkeys([name_clean,
                                name_clean.replace(" ", "_"),
                                name_clean.replace(" ", "-"),
                                name_clean.lower(),
                                name_clean.lower().replace(" ", "_"),
                                name_clean.lower().replace(" ", "-")]))
    exts = [".webp", ".jpg", ".jpeg", ".png"]
    for stem in stems:
        for ext in exts:
            fn = stem + ext
            full = os.path.join(vendor_dir, fn)
            if os.path.isfile(full):
                return vendor_dir, fn

    # Fuzzy scan: normalize both requested name and file stems
    target = _norm_key(name_clean)
    try:
        files = [f for f in os.listdir(vendor_dir) if os.path.isfile(os.path.join(vendor_dir, f))]
    except Exception:
        files = []

    best = None
    best_score = -1
    for fn in files:
        stem, ext = os.path.splitext(fn)
        if ext.lower() not in (".webp", ".jpg", ".jpeg", ".png"):
            continue
        fk = _norm_key(stem)
        # scoring: exact => 100, startswith/endswith => 80, contains => 60, partial overlap len
        score = 0
        if fk == target:
            score = 100
        elif fk.startswith(target) or fk.endswith(target):
            score = 80
        elif target in fk or fk in target:
            score = 60
        else:
            # overlap score: length of common subsequence of characters
            common = len(os.path.commonprefix([fk, target]))
            score = common
        if score > best_score:
            best_score = score
            best = fn

    if best and best_score >= 60:  # require decent similarity for safety
        return vendor_dir, best
    return None

@app.route("/api/material-image", methods=["GET"], endpoint="api_material_image")
@app.route("/material-image", methods=["GET"], endpoint="plain_material_image")
@login_required_session
def material_image():
    vendor_raw = request.args.get("vendor", "")
    name_raw   = request.args.get("name", "")
    if not vendor_raw or not name_raw:
        return "", 404

    vendor_dir = _pick_vendor_dir(vendor_raw)
    hit = _find_best_file(vendor_dir, name_raw) if vendor_dir else None
    if not hit:
        # Helpful log once in a while; return clean 404
        app.logger.info("material-image miss vendor=%r name=%r dir=%r", vendor_raw, name_raw, vendor_dir)
        return "", 404

    vdir, filename = hit
    resp = send_from_directory(vdir, filename)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp
# ─────────────────────────────────────────────────────────────────────────────

# ── Set Preview formula for an order (writes to Production Orders sheet) ─────
from flask import request, jsonify

def _col_letter(idx0: int) -> str:
    # 0-based index → A1 letter
    n = idx0 + 1
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s

@app.route("/api/thread/relog", methods=["POST"])
@login_required_session
def thread_relog():
    """
    Body: { orderNumber: str, embroideryQty: int }
    Behavior:
      - Reads 'Thread Data' tab for rows matching this Order Number
      - Finds original order qty from 'Production Orders'
      - For each matching thread row, computes per-piece length and appends a scaled row
    """
    try:
        body = request.get_json(force=True) or {}
        order = str(body.get("orderNumber") or "").strip()
        qty   = int(body.get("embroideryQty") or 0)
        if not order or qty <= 0:
            return jsonify({"error": "orderNumber and positive embroideryQty required"}), 400

        # 1) Load Thread Data
        td_rows = fetch_sheet(SPREADSHEET_ID, "Thread Data!A1:Z", value_render="UNFORMATTED_VALUE") or []
        if not td_rows:
            return jsonify({"error": "Thread Data tab empty"}), 400

        hdr = [str(h or "").strip() for h in td_rows[0]]
        idx = {h.lower(): i for i, h in enumerate(hdr)}

        # Exact columns in your sheet:
        # Date | Order Number | Color | Color Name | Length (ft) | Stitch Count | IN/OUT | O/R
        c_date   = idx.get("date")
        c_order  = idx.get("order number")
        c_color  = idx.get("color")
        c_name   = idx.get("color name")
        c_lenft  = idx.get("length (ft)")
        c_stitch = idx.get("stitch count")
        c_inout  = idx.get("in/out")
        c_or     = idx.get("o/r")

        if c_order is None or c_lenft is None:
            return jsonify({"error": "Thread Data headers missing 'Order Number' or 'Length (ft)'. Check tab."}), 400


        if c_order is None or c_lenft is None:
            return jsonify({"error": "Thread Data headers missing 'Order Number' or 'Length (ft)'. Check tab."}), 400

        # 2) Get original quantity from client body (no Production Orders lookup)
        try:
            orig_qty = float(body.get("originalQuantity"))
        except Exception:
            orig_qty = None

        if not orig_qty or orig_qty <= 0:
            return jsonify({"error": "originalQuantity is required and must be > 0 to re-log thread."}), 400


            po_hdr = [str(h or "").strip() for h in po_rows[0]]
            poi = {h.lower(): i for i, h in enumerate(po_hdr)}

            # Accept multiple aliases
            po_order_col = (poi.get("order #") or
                            poi.get("order number") or
                            poi.get("order"))
            po_qty_col   = (poi.get("quantity") or
                            poi.get("qty") or
                            poi.get("qty pieces"))

            if po_order_col is None or po_qty_col is None:
                return jsonify({"error": "Production Orders missing an order id ('Order #','Order Number','Order') or quantity ('Quantity','Qty','Qty Pieces') header"}), 400

            for r in po_rows[1:]:
                if po_order_col < len(r) and str(r[po_order_col]).strip() == order:
                    try:
                        orig_qty = float(r[po_qty_col]) if po_qty_col < len(r) else None
                    except Exception:
                        orig_qty = None
                    break

            if not orig_qty or orig_qty <= 0:
                return jsonify({"error": f"Original Quantity not found for order {order}"}), 400


        # 3) Gather thread rows for this order
        matches = []
        for r in td_rows[1:]:
            if c_order < len(r) and str(r[c_order]).strip() == order:
                matches.append(r)

        if not matches:
            return jsonify({"error": f"No thread rows found for order {order} in Thread Data"}), 404

        # 4) Build append rows scaled to 'qty'
        #    Per-piece ft = (logged_length_ft / orig_qty); new total = per_piece_ft * qty
        append_values = []
        now_iso = datetime.utcnow().strftime("%m/%d/%Y %H:%M:%S")

        for r in matches:
            try:
                total_ft = float(r[c_lenft]) if c_lenft is not None and c_lenft < len(r) else 0.0
            except Exception:
                total_ft = 0.0

            per_piece = (total_ft / float(orig_qty)) if float(orig_qty) > 0 else 0.0
            new_total = round(per_piece * float(qty), 4)

            # Build a row exactly aligned to your header array
            new_row = [""] * len(hdr)

            if c_date   is not None: new_row[c_date]   = now_iso
            if c_order  is not None: new_row[c_order]  = order
            if c_color  is not None and c_color  < len(r): new_row[c_color]  = r[c_color]
            if c_name   is not None and c_name   < len(r): new_row[c_name]   = r[c_name]
            if c_lenft  is not None: new_row[c_lenft]  = new_total
            if c_stitch is not None and c_stitch < len(r): new_row[c_stitch] = r[c_stitch]
            if c_inout  is not None: new_row[c_inout]  = "OUT"
            if c_or     is not None: new_row[c_or]     = ""

            append_values.append(new_row)


        # 5) Append to Thread Data
        body = {
            "values": append_values
        }
        sheets.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Thread Data!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": append_values}
        ).execute()


        return jsonify({"ok": True, "appended": len(append_values)}), 200

    except Exception as e:
        logger.exception("thread_relog failed")
        return jsonify({"error": f"thread_relog failed: {e}"}), 500


@app.route("/api/orders/<order_number>/preview", methods=["POST"], endpoint="api_set_order_preview")
@login_required_session
def set_order_preview(order_number):
    """
    JSON body: { "previewFormula": "=IFNA(IMAGE(\"https://...\"), \"No preview available\")" }
    Writes the formula into the 'Preview' column for the row whose 'Order #' equals <order_number>.
    """
    try:
        payload = request.get_json(silent=True, force=True) or {}
        formula = str(payload.get("previewFormula", "")).strip()
        if not formula:
            return jsonify(error="previewFormula required"), 400

        # Sheets service
        svc_vals = get_sheets_service().spreadsheets().values()

        # 1) Read header to find columns
        header_resp = svc_vals.get(
            spreadsheetId=SPREADSHEET_ID,
            range="Production Orders!1:1",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        header = (header_resp.get("values") or [[]])[0]
        header_norm = [str(h or "").strip().lower() for h in header]

        def col_idx(name):
            try:
                return header_norm.index(name.lower())
            except ValueError:
                return -1

        order_idx = col_idx("order #")
        preview_idx = col_idx("preview")
        if order_idx < 0:
            return jsonify(error="Header 'Order #' not found in Production Orders"), 400
        if preview_idx < 0:
            return jsonify(error="Header 'Preview' not found in Production Orders"), 400

        # 2) Find row for this order number
        ord_col = _col_letter(order_idx)
        ord_range = f"Production Orders!{ord_col}2:{ord_col}"
        ord_resp = svc_vals.get(
            spreadsheetId=SPREADSHEET_ID,
            range=ord_range,
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        ord_vals = ord_resp.get("values") or []
        ord_str = str(order_number).strip()

        row_offset = None
        for i, r in enumerate(ord_vals):
            val = str((r[0] if r else "")).strip()
            if val == ord_str:
                row_offset = i  # 0-based from row 2
                break

        if row_offset is None:
            return jsonify(error=f"Order # {ord_str} not found"), 404

        row_num = 2 + row_offset
        prev_col = _col_letter(preview_idx)
        target_a1 = f"Production Orders!{prev_col}{row_num}"

        # 3) Write formula (USER_ENTERED so Sheets evaluates IMAGE())
        svc_vals.update(
            spreadsheetId=SPREADSHEET_ID,
            range=target_a1,
            valueInputOption="USER_ENTERED",
            body={"values": [[formula]]},
        ).execute()

        # 4) Nudge any overview cache if present
        try:
            invalidate_overview_cache()  # no-op if you didn't define it
        except Exception:
            pass

        return jsonify(ok=True, cell=target_a1), 200

    except Exception as e:
        app.logger.exception("set_order_preview failed for order=%s", order_number)
        # Don't blow up the client — report a clean error
        return jsonify(error=str(e)), 500
# ─────────────────────────────────────────────────────────────────────────────



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