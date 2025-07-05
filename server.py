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

# ─── Global “logout everyone” timestamp ─────────────────────────────────
logout_all_ts = 0.0
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from ups_service import get_rate
from functools import wraps
from dotenv import load_dotenv

from eventlet.semaphore import Semaphore
from flask import Flask, jsonify, request, session, redirect, url_for, render_template_string
from flask import make_response
from flask_cors import CORS
from flask_cors import CORS, cross_origin
from flask_socketio import SocketIO
from flask import Response
from google.oauth2 import service_account
from googleapiclient.discovery import build
from httplib2 import Http
from google_auth_httplib2 import AuthorizedHttp

from googleapiclient.http         import MediaIoBaseUpload
from datetime                      import datetime
from requests_oauthlib import OAuth2Session
from urllib.parse import urlencode

START_TIME_COL_INDEX = 27

from google.oauth2.credentials import Credentials as OAuthCredentials

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets"
]

QBO_SCOPE = ["com.intuit.quickbooks.accounting"]
QBO_AUTH_BASE_URL = "https://appcenter.intuit.com/connect/oauth2"

def get_oauth_credentials():
    if os.path.exists("token.json"):
        creds = OAuthCredentials.from_authorized_user_file("token.json", SCOPES)
        return creds
    else:
        raise Exception("🔑 token.json not found. Please authenticate via OAuth2.")

def get_sheets_service():
    return build("sheets", "v4", credentials=get_oauth_credentials())

def get_drive_service():
    return build("drive", "v3", credentials=get_oauth_credentials())

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
CORS(app, resources={r"/api/*": {"origins": FRONTEND_URL}}, supports_credentials=True)

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ─── Simulated QuickBooks Invoice Generator ─────────────────────────────────────
def refresh_quickbooks_token():
    token = session.get("qbo_token")
    if not token or "refresh_token" not in token:
        raise Exception("No refresh token available in session")

    client_id = os.getenv("QBO_CLIENT_ID")
    client_secret = os.getenv("QBO_CLIENT_SECRET")
    refresh_token = token["refresh_token"]

    auth = (client_id, client_secret)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }

    resp = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        auth=auth,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data=data
    )

    if resp.status_code != 200:
        raise Exception(f"❌ Failed to refresh token: {resp.text}")

    new_token_data = resp.json()
    token.update({
        "access_token": new_token_data["access_token"],
        "expires_at": time.time() + int(new_token_data["expires_in"]),
        "refresh_token": new_token_data.get("refresh_token", refresh_token),  # fallback to old one if not refreshed
    })
    session["qbo_token"] = token
    print("🔁 Refreshed QuickBooks token")



def create_invoice_in_quickbooks(order_data, shipping_method="UPS Ground", tracking_list=None, base_shipping_cost=0.0):
    print(f"🧾 Creating real QuickBooks invoice for order {order_data['Order #']}")

    token = session.get("qbo_token")
    if not token or "access_token" not in token or "expires_at" not in token:
        raise Exception("❌ No QuickBooks token in session")

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

    # Step 1: Get Customer by name
    customer_name = order_data.get("Company Name", "").strip().lower()
    query_url = f"https://sandbox-quickbooks.api.intuit.com/v3/company/{realm_id}/query?query=select * from Customer"
    resp = requests.get(query_url, headers=headers)
    if resp.status_code != 200:
        raise Exception(f"❌ Failed to query customers: {resp.text}")

    customers = resp.json().get("QueryResponse", {}).get("Customer", [])
    customer_id = None

    print("🔍 All customers in QBO:")
    for c in customers:
        display_name = c.get("DisplayName", "").strip()
        print(f"   - '{display_name}' (ID: {c.get('Id')})")
        if display_name.lower() == customer_name:
            customer_id = c["Id"]
            break

    if not customer_id:
        raise Exception(f"❌ Customer '{order_data.get('Company Name')}' not found in QBO sandbox.")
    print(f"✅ Found customer '{order_data.get('Company Name')}' with ID {customer_id}")


    # Step 2: Get Item by name
    item_name = order_data.get("Product", "").strip()
    item_query_url = f"https://sandbox-quickbooks.api.intuit.com/v3/company/{realm_id}/query?query=select * from Item where Name = '{item_name}'"
    item_resp = requests.get(item_query_url, headers=headers)
    item_data = item_resp.json()

    if not item_data.get("QueryResponse", {}).get("Item"):
        raise Exception(f"❌ Item '{item_name}' not found in QBO sandbox.")
    item_id = item_data["QueryResponse"]["Item"][0]["Id"]
    print(f"✅ Found item '{item_name}' with ID {item_id}")

    # Step 3: Build and send invoice
    amount = float(order_data.get("Price", 0)) * int(order_data.get("Quantity", 1))
    num_labels = len(tracking_list or [])
    shipping_total = round(float(base_shipping_cost) * 1.1 + num_labels * 5, 2)

    invoice_payload = {
        "CustomerRef": { "value": customer_id },
        "Line": [
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": amount,
                "SalesItemLineDetail": {
                    "ItemRef": { "value": item_id }
                }
            }
        ],
        "SalesTermRef": {
            "value": "3",  # Replace with real "Net 30" ID if different
            "name": "Net 30"
        },
        "ShippingAmt": shipping_total,
        "PrivateNote": "\n".join(tracking_list or [])
    }

    invoice_url = f"https://sandbox-quickbooks.api.intuit.com/v3/company/{realm_id}/invoice"
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
    print("✅ Invoice created:", invoice)
    return f"https://app.sandbox.qbo.intuit.com/app/invoice?txnId={invoice['Id']}"


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



# After-request, echo back the real Origin so withCredentials can work
@app.after_request
def apply_cors(response):
    # 1) Grab whatever Origin header the browser sent
    origin = request.headers.get("Origin", "").strip()

    # 2) If it matches exactly our FRONTEND_URL, expose CORS headers
    if origin == FRONTEND_URL:
        response.headers["Access-Control-Allow-Origin"]      = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"]     = "Content-Type,Authorization"
        response.headers["Access-Control-Allow-Methods"]     = "GET,POST,PUT,OPTIONS"
    return response


# ─── Session & Auth Helpers ──────────────────────────────────────────────────
app.secret_key = os.environ.get("SECRET_KEY", "dev-fallback-secret")


def login_required_session(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # 1) OPTIONS are always allowed (CORS preflight)
        if request.method == "OPTIONS":
            response = make_response("", 204)
            response.headers["Access-Control-Allow-Origin"]      = FRONTEND_URL
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"]     = "Content-Type,Authorization"
            response.headers["Access-Control-Allow-Methods"]     = "GET,POST,PUT,OPTIONS"
            return response

        # 2) Must be logged in at all
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("login", next=request.path))

        # 3) Token‐match check
        ADMIN_TOKEN   = os.environ.get("ADMIN_TOKEN", "")
        token_at_login = session.get("token_at_login", "")
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

# Socket.IO (same origin)
socketio = SocketIO(
    app,
    cors_allowed_origins=[FRONTEND_URL],
    async_mode="eventlet"
)



# ─── Google Sheets Credentials & Semaphore ───────────────────────────────────
sheet_lock = Semaphore(1)
SPREADSHEET_ID   = os.environ["SPREADSHEET_ID"]
ORDERS_RANGE     = os.environ.get("ORDERS_RANGE",     "Production Orders!A1:AM")
EMBROIDERY_RANGE = os.environ.get("EMBROIDERY_RANGE", "Embroidery List!A1:AM")
MANUAL_RANGE       = os.environ.get("MANUAL_RANGE", "Manual State!A2:H")
MANUAL_CLEAR_RANGE = os.environ.get("MANUAL_RANGE", "Manual State!A2:H")

QBO_CLIENT_ID     = os.environ.get("QBO_CLIENT_ID")
QBO_CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET")
QBO_REDIRECT_URI  = os.environ.get("QBO_REDIRECT_URI")
QBO_BASE_URL      = os.environ.get("QBO_BASE_URL")
QBO_AUTH_URL      = "https://appcenter.intuit.com/connect/oauth2"
QBO_TOKEN_URL     = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_SCOPES        = ["com.intuit.quickbooks.accounting"]

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
import pickle

SCOPES = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]

creds = None
if os.path.exists("token.pickle"):
    with open("token.pickle", "rb") as token:
        creds = pickle.load(token)

if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        import json
        from google.oauth2.credentials import Credentials

        oauth_client_id     = os.environ["OAUTH_CLIENT_ID"]
        oauth_client_secret = os.environ["OAUTH_CLIENT_SECRET"]
        oauth_redirect_uri  = os.environ["OAUTH_REDIRECT_URI"]

        client_config = {
            "installed": {
                "client_id":     oauth_client_id,
                "client_secret": oauth_client_secret,
                "redirect_uris": [oauth_redirect_uri],
                "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                "token_uri":     "https://oauth2.googleapis.com/token"
            }
        }

        token_json_str = os.environ["TOKEN_JSON"]
        token_data = json.loads(token_json_str)

        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    with open("token.pickle", "wb") as token:
        pickle.dump(creds, token)

sh = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
sheet_cache = sh.worksheet("Embroidery List")


_http = Http(timeout=10)
authed_http = AuthorizedHttp(creds, http=_http)
service = build("sheets", "v4", credentials=creds, cache_discovery=False)
sheets  = service.spreadsheets()

@app.route('/api/updateStartTime', methods=["POST"])
@login_required_session
def update_start_time():
    try:
        data = request.get_json()
        job_id = data.get("id")
        start_time = data.get("startTime")

        if not job_id or not start_time:
            return jsonify({"error": "Missing job ID or start time"}), 400

        print(f"🧵 Updating embroidery start time for job {job_id} → {start_time}")
        update_embroidery_start_time_in_sheet(job_id, start_time)
        return jsonify({"status": "success"})  # ✅ This line was missing

    except Exception as e:
        print("🔥 Error in update_start_time:", e)
        return jsonify({"error": str(e)}), 500

# ✅ You must define or update this function to match your actual Google Sheet logic
import traceback

def update_embroidery_start_time_in_sheet(order_id, new_start_time):
    try:
        sheet = sh.worksheet("Embroidery List")
        data = sheet.get_all_records()
        col_names = sheet.row_values(1)
        id_col = col_names.index("Order #") + 1
        start_col = col_names.index("Embroidery Start Time") + 1

        for i, row in enumerate(data, start=2):
            if str(row.get("Order #")).strip() == str(order_id).strip():
                sheet.update_cell(i, start_col, new_start_time)
                print(f"✅ Embroidery Start Time updated for Order #{order_id}")
                return

        print(f"⚠️ Order ID {order_id} not found in Embroidery List")
    except Exception as e:
        print(f"❌ Error updating start time for Order #{order_id}: {e}")
# ─── In-memory caches & settings ────────────────────────────────────────────
# with CACHE_TTL = 0, every GET will hit Sheets directly
CACHE_TTL           = 10

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


def fetch_sheet(spreadsheet_id, sheet_range):
    with sheet_lock:
        res = sheets.values().get(
            spreadsheetId=spreadsheet_id,
            range=sheet_range
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

        if u == "admin" and p == ADMIN_PW:
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

    invoices = []

    # Fetch both Production Orders and Table tabs
    prod_data = fetch_sheet(SPREADSHEET_ID, "Production Orders!A1:AM")
    table_data = fetch_sheet(SPREADSHEET_ID, "Table!A1:Z")

    prod_headers = prod_data[0]
    table_headers = table_data[0]

    # Step 1: Find orders that match the given Order #
    prod_rows = []
    for row in prod_data[1:]:
        row_dict = dict(zip(prod_headers, row))
        if str(row_dict.get("Order #")) in order_ids:
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
        product = row.get("Product", "").strip()
        key = product.lower()
        volume = table_map.get(key)

        if volume is None:
            missing_products.append(product)
        else:
            jobs.append({
                "order_id": order_id,
                "product": product,
                "volume": volume
            })

    if missing_products:
        return jsonify({
            "error": "Missing volume data",
            "missing_products": list(set(missing_products))
        }), 400

    # Step 4: Pack jobs into boxes using greedy volume fit
    box_sizes = {
        "Small": 1000,
        "Medium": 2197,
        "Large": 8000,
    }

    boxes = []
    remaining = jobs.copy()

    while remaining:
        used_volume = 0
        box_jobs = []

        for job in remaining[:]:
            if used_volume + job["volume"] <= box_sizes["Large"]:
                used_volume += job["volume"]
                box_jobs.append(job)
                remaining.remove(job)

        for size, cap in box_sizes.items():
            if used_volume <= cap:
                boxes.append({
                    "size": size,
                    "jobs": [j["order_id"] for j in box_jobs]
                })
                break

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
        machine_columns = [[s for s in col.split(",") if s] for col in machines]

        # rest: placeholders from A–H (cols 0–7)
        phs = []
        for r in rows:
            if r[0].strip():  # only if ID in col A
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
        data       = request.form
        print("📥 Incoming files:", request.files)
        prod_files = request.files.getlist("prodFiles")
        print("🧾 prod_files length:", len(prod_files))
        print_files= request.files.getlist("printFiles")

        # find next empty row in col A
        col_a = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Production Orders!A:A"
        ).execute().get("values", [])
        next_row   = len(col_a) + 1
        prev_order = int(col_a[-1][0]) if len(col_a)>1 else 0
        new_order  = prev_order + 1

        # helper: copy the formula from row 2 of <cell> and rewrite “2” → new row
        def tpl_formula(col_letter, next_row):
            # grab the raw formula from row 2
            resp = sheets.values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"Production Orders!{col_letter}2",
                valueRenderOption="FORMULA"
            ).execute()
            raw = resp.get("values", [[""]])[0][0] or ""
            # rewrite *any* reference ending in “2” to the new row
            # e.g. C2→C3, Y2→Y3, AC2→AC3, etc.
            formula = re.sub(r"(\b[A-Z]+)2\b", lambda m: f"{m.group(1)}{next_row}", raw)
            return formula

        # timestamp + template cells from row 2
        ts = datetime.now(ZoneInfo("America/New_York")).strftime("%-m/%-d/%Y %H:%M:%S")
        def tpl(cell):
            return sheets.values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"Production Orders!{cell}2"
            ).execute().get("values",[[""]])[0][0]

        preview      = tpl_formula("C",  next_row)
        stage        = tpl_formula("I",  next_row)
        ship_date    = tpl_formula("V",  next_row)
        stitch_count = tpl_formula("W", next_row)
        schedule_str = tpl_formula("AC", next_row)

        # helper: make a Drive folder (optionally under a parent) and return its ID
        def create_folder(name, parent_id=None):
            print(f"🔍 Looking for existing folders named '{name}' under parent: {parent_id or 'root'}")
            query = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            if parent_id:
                query += f" and '{parent_id}' in parents"

            try:
                matches = drive.files().list(q=query, fields="files(id, name)").execute().get("files", [])
                for match in matches:
                    print(f"🗑️ Deleting existing folder: {match['name']} (ID: {match['id']})")
                    drive.files().delete(fileId=match["id"]).execute()
            except Exception as e:
                print(f"❌ Failed to check/delete existing folders: {e}")

            meta = {
                "name": str(name),
                "mimeType": "application/vnd.google-apps.folder",
            }
            if parent_id:
                meta["parents"] = [parent_id]

            print(f"📁 Creating new folder: {name}")
            folder = drive.files().create(body=meta, fields="id").execute()
            print(f"✅ Folder created with ID: {folder['id']}")
            return folder["id"]


        # create Drive folder for this order
        drive = get_drive_service()

        # 1) Make the root folder for this order
        order_folder_id = create_folder(new_order, parent_id="1n6RX0SumEipD5Nb3pUIgO5OtQFfyQXYz")
        make_public(order_folder_id, drive)

        # 🧵 If this is a reorder, copy any .emb files from the original job folder
        reorder_from = data.get("reorderFrom")
        if reorder_from:
            copy_emb_files(
                old_order_num=reorder_from,
                new_order_num=new_order,
                drive_service=drive,
                new_folder_id=order_folder_id  # the ID we just created
            )

        # (we’ll link to it later as order_folder_link)
        order_folder_link = f"https://drive.google.com/drive/folders/{order_folder_id}"

        # upload production files
        prod_links = []
        for f in prod_files:
            m = MediaIoBaseUpload(f.stream, mimetype=f.mimetype)
            up = drive.files().create(
                body={"name": f.filename, "parents": [order_folder_id]},
                media_body=m,
                fields="id, webViewLink"
            ).execute()
            make_public(up["id"], drive)
            prod_links.append(up["webViewLink"])

        # upload print files (if present) — link to the “Print Files” folder
        print_links = ""
        if print_files:
            # 1) create the “Print Files” folder under the job folder, and get its link
            print_folder_id = create_folder("Print Files", parent_id=order_folder_id)
            make_public(print_folder_id, drive)
            pf_link = f"https://drive.google.com/drive/folders/{print_folder_id}"

            # 2) upload each print file into that folder (we don't need their individual links)
            for f in print_files:
                m = MediaIoBaseUpload(f.stream, mimetype=f.mimetype)
                drive.files().create(
                    body={"name": f.filename, "parents": [print_folder_id]},
                    media_body=m,
                    fields="id"
                ).execute()

            # 3) write only the folder’s public link into the sheet
            print_links = pf_link


        # assemble row A→AC
        row = [
          new_order, ts, preview,
          data.get("company"), data.get("designName"), data.get("quantity"),
          "",  # shipped
          data.get("product"), stage, data.get("price"),
          data.get("dueDate"), ("PRINT" if print_files else "NO"),
          *data.getlist("materials"),  # M–Q
          data.get("backMaterial"), data.get("furColor"),
          data.get("embBacking",""), "",  # top stitch blank
          ship_date, stitch_count,
          data.get("notes"),
          ",".join(prod_links),
          print_links,
          "",
          data.get("dateType"),
          schedule_str
        ]

        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Production Orders!A{next_row}:AC{next_row}",
            valueInputOption="USER_ENTERED",
            body={"values":[row]}
        ).execute()
 
        # ─── COPY AF2 FORMULA DOWN TO AF<next_row> ─────────────────
        resp = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Production Orders!AF2",
            valueRenderOption="FORMULA"
        ).execute()
        raw_formula = resp.get("values", [[""]])[0][0] or ""
        new_formula = raw_formula.replace("2", str(next_row))
        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Production Orders!AF{next_row}",
            valueInputOption="USER_ENTERED",
            body={"values": [[new_formula]]}
        ).execute()

        return jsonify({"status":"ok","order":new_order}), 200

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error("Error in /submit:\n%s", tb)
        # return the exception message and stack to the client (for now)
        return jsonify({
            "error": str(e),
            "trace": tb
        }), 500

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
        ).execute()

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
    Only appends entries to Material Log!A2:H
    Expects JSON array (or single object) with:
      materialName, action (Ordered/Received), quantity, notes?
    """
    raw   = request.get_json(silent=True) or []
    items = raw if isinstance(raw, list) else [raw]

    now      = datetime.now(ZoneInfo("America/New_York"))\
                    .strftime("%-m/%-d/%Y %H:%M:%S")
    log_rows = []

    for it in items:
        name   = it.get("materialName","").strip()
        action = it.get("action","").strip()
        qty    = it.get("quantity","").strip()
        notes  = it.get("notes","").strip()

        if not (name and action and qty):
            continue

        # build the log row: columns A–H of Material Log
        log_rows.append([
            now,    # A: Timestamp
            "",     # B
            "",     # C
            "",     # D
            "",     # E
            name,   # F: Material name
            qty,    # G: Quantity
            "IN",   # H: fixed “IN”
            action  # I: O/R (“Ordered” or “Received”)
        ])

    if log_rows:
        sheets.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Material Log!A2:H",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": log_rows}
        ).execute()

    return jsonify({"status":"submitted"}), 200


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
    now     = datetime.now(ZoneInfo("America/New_York")).strftime("%-m/%-d/%Y %H:%M:%S")
    to_log  = []

    for e in entries:
        # ← change here: read e["value"] not e["color"]
        color     = e.get("value",    "").strip()
        action    = e.get("action",   "").strip()
        qty_cones = e.get("quantity", "").strip()
        if not (color and action and qty_cones):
            continue

        # compute feet: cones × 5500 (yards) × 3 (feet per yard)
        try:
            cones = int(qty_cones)
            yards = cones * 5500
            feet  = yards * 3
        except:
            feet = 0

        to_log.append([
            now,     # A: timestamp
            "",      # B
            color,   # C: Thread Color
            "",      # D
            feet,    # E: feet
            "",      # F: empty
            "IN",    # G: fixed “IN”
            action   # H: action (Ordered/Received)
        ])

    if to_log:
        sheets.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Thread Data!A2:H",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": to_log}
        ).execute()

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
    print("📥 Reorder API received:", data)
    order_ids = [str(oid).strip() for oid in data.get("order_ids", [])]
    shipped_quantities = {str(k).strip(): v for k, v in data.get("shipped_quantities", {}).items()}

    print("→ order_ids received:", order_ids)
    print("→ shipped_quantities received:", shipped_quantities)

    if not order_ids:
        return jsonify({"error": "Missing order_ids"}), 400

    sheet_id = os.environ["SPREADSHEET_ID"]
    sheet_name = "Production Orders"

    try:
        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{sheet_name}!A1:Z",
        ).execute()
        rows = result.get("values", [])
        headers = rows[0]

        id_col = headers.index("Order #")
        shipped_col = headers.index("Shipped")

        updates = []
        invoices = []

        for i, row in enumerate(rows[1:], start=2):  # start=2 for 1-based index
            row_order_id = str(row[id_col]).strip() if id_col < len(row) else ""
            print(f"→ Row {i}: Order ID in sheet = '{row_order_id}'")

            if row_order_id in order_ids:
                qty = shipped_quantities.get(row_order_id)
                print(f"✅ Match! Writing {qty} to row {i}")
                if qty is not None:
                    updates.append({
                        "range": f"{sheet_name}!{chr(shipped_col + 65)}{i}",
                        "values": [[str(qty)]]
                    })

                # Build order data dict and create invoice
                order_data = {h: row[headers.index(h)] if headers.index(h) < len(row) else "" for h in headers}
                invoice_url = create_invoice_in_quickbooks(order_data)
                invoices.append(invoice_url)

        if not updates:
            print("⚠️ No updates prepared — check for ID mismatches.")

        if updates:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": updates}
            ).execute()
            print("✅ Successfully wrote quantities to sheet.")

        return jsonify({
            "labels": ["https://example.com/label1.pdf"],
            "invoice": invoices[0] if invoices else "",
            "slips": ["https://example.com/slip1.pdf"]
        })

    except Exception as e:
        print("❌ Shipment error:", str(e))
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
            range=target,
            valueInputOption="RAW",
            body={"values": [[ val ]]}
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
def rate_shipment():
    """
    Expects JSON like:
    {
      "shipper": { … },
      "recipient": { … },
      "packages": [ {Weight, Dimensions?}, … ]
    }
    """
    payload = request.get_json()
    if not payload:
        return jsonify({"error": "Missing payload"}), 400

    try:
        rates = get_rate(
          shipper    = payload["shipper"],
          recipient  = payload["recipient"],
          packages   = payload["packages"]
        )
        return jsonify({ "rates": rates }), 200
    except KeyError as ke:
        return jsonify({ "error": f"Missing field: {ke}" }), 400
    except Exception as e:
        logger.exception("UPS rating error")
        return jsonify({ "error": str(e) }), 500

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

@app.route("/qbo/login")
def qbo_login():
    from requests_oauthlib import OAuth2Session

    print("🚀 Entered /qbo/login route")

    qbo = OAuth2Session(
        client_id=QBO_CLIENT_ID,
        redirect_uri=QBO_REDIRECT_URI,
        scope=QBO_SCOPES,  # ✅ Keep it here
        state=session.get("qbo_oauth_state")
    )

    # ✅ Do not include `scope` again here
    auth_url, state = qbo.authorization_url(QBO_AUTH_URL)

    session["qbo_oauth_state"] = state
    print("🔗 QuickBooks redirect URL:", auth_url)
    return redirect(auth_url)


@app.route("/qbo/callback")
def qbo_callback():
    from requests_oauthlib import OAuth2Session

    try:
        print("🔁 Received QBO callback with URL:", request.url)

        qbo = OAuth2Session(
            client_id=QBO_CLIENT_ID,
            redirect_uri=QBO_REDIRECT_URI,
            state=session.get("qbo_oauth_state")
        )

        token = qbo.fetch_token(
            QBO_TOKEN_URL,
            client_secret=QBO_CLIENT_SECRET,
            authorization_response=request.url,
        )

        realm_id = request.args.get("realmId")
        token["realmId"] = realm_id  # 👈 Add this line
        session["qbo_token"] = token
        print("✅ QBO token + realmId stored in session:", token)

        return "✅ QuickBooks authorized successfully!"
    except Exception as e:
        print("❌ Error in /qbo/callback:")
        import traceback
        traceback.print_exc()
        return f"❌ Callback error: {str(e)}", 500

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




# ─── Socket.IO connect/disconnect ─────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    logger.info(f"Client connected: {request.sid}")

@socketio.on("disconnect")
def on_disconnect():
    logger.info(f"Client disconnected: {request.sid}")

# ─── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🚀 JRCO server.py loaded and running...")
    print("📡 Available Flask Routes:")
    for rule in app.url_map.iter_rules():
        print("✅", rule)

    logger.info(f"Starting on port {os.environ.get('PORT', 10000)}")

    # If you're using token creation for Google auth
    if os.environ.get("GENERATE_GOOGLE_TOKEN") == "true":
        from google_auth_oauthlib.flow import InstalledAppFlow
        SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets']
        flow = InstalledAppFlow.from_client_secrets_file("oauth-credentials.json", SCOPES)
        creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
        print("✅ token.json created successfully.")
    else:
        port = int(os.environ.get("PORT", 10000))
        socketio.run(app, host="0.0.0.0", port=port, debug=True, use_reloader=False)

