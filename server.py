import eventlet
 # Simple edit for commit test - Copilot was here
eventlet.monkey_patch()
from eventlet import debug
debug.hub_prevent_multiple_readers(False)

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

from flask import g
import importlib
import tracemalloc, gc
tracemalloc.start()
psutil = None
try:
    spec = importlib.util.find_spec("psutil")
    if spec:
        psutil = importlib.import_module("psutil")
except Exception:
    psutil = None
try:
    _process = psutil.Process(os.getpid()) if psutil else None
except Exception:
    _process = None
# throttle GC: collect only periodically or on large RSS deltas
_resp_counter = 0
_gc_every_n = int(os.environ.get("GC_EVERY_N", "25"))  # collect every N responses
_gc_rss_threshold_mb = float(os.environ.get("GC_RSS_DELTA_MB", "32"))  # or if delta exceeds this



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
app.secret_key = os.environ.get("SECRET_KEY", "dev-fallback-secret")

logout_all_ts = int(os.environ.get("LOGOUT_ALL_TS", "0"))

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
    allowed = {
        (os.environ.get("FRONTEND_URL", "https://machineschedule.netlify.app").strip().rstrip("/")),
        "https://machineschedule.netlify.app",
        "http://localhost:3000",
    }
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
        g._rss_before = _process.memory_info().rss if _process else None
    except Exception:
        g._rss_before = None

# NEW: log RSS delta after each request and trigger opportunistic GC
@app.after_request
def _mem_after(resp):
    try:
        global _resp_counter
        rss_now = _process.memory_info().rss if _process else None
        rss_before = getattr(g, "_rss_before", None)
        if rss_before is not None and rss_now is not None:
            delta_mb = (rss_now - rss_before) / (1024 * 1024)
            app.logger.info(f"[MEM] {request.method} {request.path} Δ={delta_mb:.2f}MB rss={rss_now/1024/1024:.2f}MB")
        # Opportunistic GC: only occasionally or when memory jumps a lot
        _resp_counter = (_resp_counter + 1) % max(_gc_every_n, 1)
        if (
            rss_before is not None and rss_now is not None and (rss_now - rss_before) / (1024 * 1024) >= _gc_rss_threshold_mb
        ) or _resp_counter == 0:
            gc.collect()
    except Exception:
        pass
    return resp


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


@app.route("/api/drive/proxy/<file_id>", methods=["GET"])
@login_required_session
def drive_proxy(file_id):
    """
    Serve a Drive thumbnail (or original image) with strong caching.
    - If 'v' query param is present: use immutable caching and disk cache keyed by (id, size, v).
    - Otherwise: short cache.
    """
    try:
        # --- Query params ---
        size = request.args.get("sz", "w256")               # "w240", "w512" etc (numbers only are used)
        use_thumb = request.args.get("thumb", "1") != "0"   # default True
        debug_mode = request.args.get("debug") == "1"
        v_param = request.args.get("v")                     # version token (md5Checksum or modifiedTime)

        cache_control = "public, max-age=31536000, immutable" if v_param else "public, max-age=86400, s-maxage=86400"
        etag = f'W/"{file_id}-{size}-{v_param or "nov"}"'

        # Serve from disk cache if versioned and present
        if v_param:
            cpath = _thumb_cache_path(file_id, size, v_param)
            if os.path.exists(cpath) and not debug_mode:
                resp = send_file(cpath, mimetype="image/jpeg", as_attachment=False, download_name=os.path.basename(cpath))
                resp.headers["Cache-Control"] = cache_control
                resp.headers["ETag"] = etag
                return resp

        # Short-circuit on client revalidation (when we control ETag)
        inm = request.headers.get("If-None-Match", "")
        if inm and etag in inm and v_param:
            resp = make_response("", 304)
            resp.headers["ETag"] = etag
            resp.headers["Cache-Control"] = cache_control
            return resp

        # ---- Drive request ----
        creds = _load_google_creds()
        if not creds or not creds.token:
            return jsonify({"error": "no_creds"}), 502
        headers = {"Authorization": f"Bearer {creds.token}"}

        # Try to get thumbnail metadata quickly (optional)
        info = None
        thumb = None
        mime = ""
        try:
            meta = requests.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=thumbnailLink,mimeType,name,modifiedTime,md5Checksum",
                headers=headers,
                timeout=4,
            )
            if meta.status_code == 200:
                info = meta.json() or {}
                thumb = info.get("thumbnailLink")
                mime  = (info.get("mimeType") or "").lower()
        except Exception:
            info = None


        # Prefer native Drive thumbnail if available, with retry logic
        if use_thumb and thumb:
            px = re.sub(r"[^0-9]", "", size) or "240"
            if "?" in thumb:
                if re.search(r"[?&](sz|s)=", thumb):
                    thumb = re.sub(r"([?&])(sz|s)=\d+", rf"\1s={px}", thumb)
                else:
                    thumb = f"{thumb}&s={px}"
            else:
                thumb = f"{thumb}?s={px}"

            last_exc = None
            for attempt in range(2):
                try:
                    img = requests.get(thumb, headers=headers, timeout=10)
                    if img.status_code == 200 and img.content:
                        if v_param:
                            try:
                                with open(_thumb_cache_path(file_id, size, v_param), "wb") as f:
                                    f.write(img.content)
                            except Exception:
                                logger.exception("thumb cache write failed")
                        resp = Response(img.content, status=200, mimetype="image/jpeg")
                        resp.headers["Cache-Control"] = cache_control
                        resp.headers["ETag"] = etag
                        return resp
                except Exception as exc:
                    last_exc = exc
                    logger.error(f"Drive thumbnail fetch failed for file_id={file_id} url={thumb} attempt={attempt+1}: {exc}")
            if last_exc:
                logger.error(f"All thumbnail fetch attempts failed for file_id={file_id} url={thumb}: {last_exc}")


        # Fallback: if original is an image, fetch it and cache, with retry logic
        if mime.startswith("image/"):
            file_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
            last_exc = None
            for attempt in range(2):
                try:
                    r = requests.get(file_url, headers=headers, timeout=15, stream=False)
                    if r.status_code == 200 and r.content:
                        if v_param:
                            try:
                                with open(_thumb_cache_path(file_id, size, v_param), "wb") as f:
                                    f.write(r.content)
                            except Exception:
                                logger.exception("thumb cache write failed (full-file)")
                        resp = Response(r.content, status=200, mimetype=mime or "image/jpeg")
                        resp.headers["Cache-Control"] = cache_control
                        resp.headers["ETag"] = etag
                        return resp
                except Exception as exc:
                    last_exc = exc
                    logger.error(f"Drive image fetch failed for file_id={file_id} url={file_url} attempt={attempt+1}: {exc}")
            if last_exc:
                logger.error(f"All image fetch attempts failed for file_id={file_id} url={file_url}: {last_exc}")

        # No thumbnail and not an image
        return jsonify({"error": "no_thumbnail"}), 404

    except Exception as e:
        logger.exception("drive_proxy error")
        return jsonify({"error": str(e)}), 502



@app.route("/api/ping", methods=["GET", "OPTIONS"])
def api_ping():
    try:
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.error(f"Error in /api/ping: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 502

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

def _load_google_creds():
    """
    Load Google OAuth user credentials from:
      1) GOOGLE_TOKEN_JSON env var (preferred, raw token.json contents)
      2) token.json on disk (GOOGLE_TOKEN_PATH)
    Returns an OAuthCredentials object or None.
    """
    env_val = os.environ.get("GOOGLE_TOKEN_JSON", "").strip()
    if env_val:
        try:
            info = json.loads(env_val)
            creds = OAuthCredentials.from_authorized_user_info(info, SCOPES)
            if not creds.valid and creds.refresh_token:
                creds.refresh(GoogleRequest())
            return creds
        except Exception as e:
            print("❌ ENV token could not build OAuthCredentials:", repr(e))
    try:
        if os.path.exists(GOOGLE_TOKEN_PATH):
            with open(GOOGLE_TOKEN_PATH, "r", encoding="utf-8") as f:
                info = json.load(f)
            creds = OAuthCredentials.from_authorized_user_info(info, SCOPES)
            if not creds.valid and creds.refresh_token:
                creds.refresh(GoogleRequest())
            return creds
    except Exception as e:
        print("❌ FILE token could not build OAuthCredentials:", repr(e))
    return None

# --- Protected thread images (behind login) ---
THREAD_IMG_DIR = os.path.join(os.path.dirname(__file__), "static", "thread-images")

@app.route("/thread-images/<int:num>.<ext>", methods=["GET", "OPTIONS"])
@login_required_sessiondef serve_thread_image(num, ext):
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

@app.route('/api/test-copilot', methods=['POST'])
def test_copilot():
    app.logger.info('Copilot test endpoint hit!')
    return jsonify({'status': 'success', 'message': 'Copilot test endpoint hit!'})


@app.route('/api/sheet-update', methods=['POST'])
def sheet_update_webhook():
    app.logger.info('Received Google Sheet update webhook')
    # Emit Socket.IO event to all clients
    from flask_socketio import SocketIO
    # Use the global socketio instance if available
    try:
        socketio.emit('sheet_updated', {'message': 'Sheet data updated'}, broadcast=True)
    except Exception as e:
        app.logger.error(f"SocketIO emit failed: {e}")
    return jsonify({'status': 'success', 'message': 'Webhook received'})