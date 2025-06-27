# ─── Imports & Logger Setup ─────────────────────────────────────────────────
import os
import json
import logging
import time
import traceback
import re

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

from google.oauth2 import service_account
from googleapiclient.discovery import build
from httplib2 import Http
from google_auth_httplib2 import AuthorizedHttp

from googleapiclient.http         import MediaIoBaseUpload
from datetime                      import datetime

START_TIME_COL_INDEX = 27

def get_sheets_service():
    scopes = ['https://www.googleapis.com/auth/spreadsheets']
    credentials = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
        scopes=scopes
    )
    return build("sheets", "v4", credentials=credentials)

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

creds_json = os.environ.get("GOOGLE_CREDENTIALS")
if creds_json:
    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
else:
    creds = service_account.Credentials.from_service_account_file(
    "credentials.json",
    scopes=[
      "https://www.googleapis.com/auth/spreadsheets",
      "https://www.googleapis.com/auth/drive"
    ]
)

_http = Http(timeout=10)
authed_http = AuthorizedHttp(creds, http=_http)
service = build("sheets", "v4", credentials=creds, cache_discovery=False)
sheets  = service.spreadsheets()

@app.route('/api/updateStartTime', methods=["POST"])
def update_start_time():
    data = request.get_json()
    row_id     = data.get("id")
    start_time = data.get("startTime")

    if not row_id or not start_time:
        return jsonify({"error": "Missing ID or start time"}), 400

    try:
        success = update_embroidery_start_time_in_sheet(row_id, start_time)
        if not success:
            print(f"⚠️ No matching row for job_id={row_id} — skipping update")
        # Always respond 200 so the client won’t see an error
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print("❌ Server exception in /updateStartTime:", e)
        # Still return 200 so the UI won’t break; check logs for details
        return jsonify({"status": "ok"}), 200


# ✅ You must define or update this function to match your actual Google Sheet logic
import traceback

def update_embroidery_start_time_in_sheet(job_id, start_time):
    service = get_sheets_service()
    sheet   = service.spreadsheets()

    # 1) Read the IDs
    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Embroidery List!A2:A"
    ).execute()
    values = result.get("values", [])
    print(
        "🧐 Sheet IDs (A2:A):",
        [ (r[0] if len(r) > 0 else None) for r in values ][:20],
        "…"
    )

    # 2) Find & write
    for idx, row in enumerate(values, start=2):
        # ← Skip empty rows to avoid IndexError
        if not row or len(row) == 0:
            continue

        # Safe to access row[0] now
        cell = str(row[0]).strip()
        if cell == str(job_id).strip():
            target = f"Embroidery List!AA{idx}"
            try:
                sheet.values().update(
                    spreadsheetId=SPREADSHEET_ID,
                    range=target,
                    valueInputOption="RAW",
                    body={"values": [[start_time]]}
                ).execute()
                print(f"✅ Updated embroidery_start for job {job_id} at {target}")
                return True
            except Exception as e:
                print("⚠️ Error writing startTime to sheet:", e)
                return False

    # 3) No match → explicit False
    print(f"❌ Job ID {job_id} not found in Embroidery List column A")
    return False


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
        return jsonify({"error": "No order IDs provided"}), 400

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

            jobs.append({
                "orderId": str(row_dict.get("Order #", "")).strip(),
                "date": row_dict.get("Date", "").strip(),
                "company": row_dict.get("Company Name", "").strip(),
                "design": row_dict.get("Design", "").strip(),
                "quantity": row_dict.get("Quantity", "").strip(),
                "product": row_dict.get("Product", "").strip(),
                "stage": row_dict.get("Stage", "").strip(),
                "price": row_dict.get("Price", "").strip(),
                "due": row_dict.get("Due Date", "").strip(),
                "image": preview_url
            })

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
        data    = [dict(zip(headers, r)) for r in rows[1:]]
        for row in data:
            row["startTime"] = row.get("Start Time", "")

        # ─── Spot B: CACHE STORE ───────────────────────────────────
        _emb_cache = data
        _emb_ts    = now

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
@app.route("/submit", methods=["OPTIONS","POST"])
def submit_order():
    if request.method == "OPTIONS":
        return make_response("", 204)

    try:
        data       = request.form
        prod_files = request.files.getlist("prodFiles")
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
            meta = {
                "name": str(name),
                "mimeType": "application/vnd.google-apps.folder",
            }
            if parent_id:
                meta["parents"] = [parent_id]
            folder = drive.files().create(body=meta, fields="id").execute()
            return folder["id"]

        # create Drive folder for this order
        drive = build("drive","v3",credentials=creds)
        def make_public(file_id):
            drive.permissions().create(
                fileId=file_id,
                body={"type":"anyone","role":"reader"}
            ).execute()

        # 1) Make the root folder for this order
        order_folder_id   = create_folder(new_order)
        make_public(order_folder_id)

        # (we’ll link to it later as order_folder_link)
        order_folder_link = f"https://drive.google.com/drive/folders/{order_folder_id}"


        # helper: grant “anyone with link” reader access
        def make_public(file_id):
            drive.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"}
            ).execute()

        # upload production files
        prod_links = []
        for f in prod_files:
            m = MediaIoBaseUpload(f.stream, mimetype=f.mimetype)
            up = drive.files().create(
                body={"name": f.filename, "parents": [order_folder_id]},
                media_body=m,
                fields="id, webViewLink"
            ).execute()
            make_public(up["id"])
            prod_links.append(up["webViewLink"])

        # upload print files (if present) — link to the “Print Files” folder
        print_links = ""
        if print_files:
            # 1) create the “Print Files” folder under the job folder, and get its link
            print_folder_id = create_folder("Print Files", parent_id=order_folder_id)
            make_public(print_folder_id)
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
    """
    Expects JSON:
      {
        "previousOrder": <number>,
        "newDueDate": "YYYY-MM-DD",
        "newDateType": "Hard Date"|"Soft Date",
        "notes": "<overrideable notes>"
      }
    """
    data = request.get_json(silent=True) or {}

    print("📥 Incoming /api/reorder payload:", data)

    prev_id    = data.get("previousOrder")
    new_due    = data.get("newDueDate")
    new_type   = data.get("newDateType")
    new_notes  = data.get("notes", "")

    if not all([prev_id, new_due, new_type]):
        print("🚨 Missing required field(s):", {
            "previousOrder": prev_id,
            "newDueDate": new_due,
            "newDateType": new_type
        })
        return jsonify({"error": "Missing fields"}), 400

    try:
        # 1) Read the sheet row for prev_id
        rows = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE)
        headers = rows[0]
        match = None
        print(f"🔍 Searching for order #{prev_id} in spreadsheet...")
        for r in rows[1:]:
            print(f"🔍 Comparing: sheet value {r[0]} vs requested {prev_id}")
            if str(r[0]).strip().lstrip("#") == str(prev_id).strip().lstrip("#"):
                match = dict(zip(headers, r))
                break

        if not match:
            print(f"❌ Order #{prev_id} not found.")
            return jsonify({"error": f"Order {prev_id} not found"}), 404

        print("✅ Found matching order row:", match)

        # 2) Determine new Order #
        colA = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Production Orders!A:A"
        ).execute().get("values", [])
        last = int(colA[-1][0] or 0)
        new_id = last + 1

        # 3) Create new Drive folder and copy prod/print files + .emb
        drive = build("drive", "v3", credentials=creds)

        def create_folder(name, parent=None):
            meta = {
                "name": str(name),
                "mimeType": "application/vnd.google-apps.folder"
            }
            if parent:
                meta["parents"] = [parent]
            return drive.files().create(body=meta, fields="id").execute()["id"]

        def copy_item(file_id, folder_id, new_name=None):
            body = {"parents": [folder_id]}
            if new_name:
                body["name"] = new_name
            return drive.files().copy(fileId=file_id, body=body, fields="id").execute()

        prev_link = match.get("Image", "")
        if not prev_link:
            print("❌ Missing order_folder_link in match.")
            return jsonify({"error": "Missing previous folder link"}), 400

        import re
        match_drive_id = re.search(r"/d/([a-zA-Z0-9_-]+)", prev_link)
        prev_folder = match_drive_id.group(1) if match_drive_id else ""
        new_folder = create_folder(new_id)

        if not prev_folder:
            return jsonify({"error": "Could not extract folder ID from link"}), 400

        def make_public(fid):
            drive.permissions().create(
                fileId=fid,
                body={"type": "anyone", "role": "reader"}
            ).execute()

        make_public(new_folder)

        query = f"'{prev_folder}' in parents"
        prod_files = match.get("production_files", "")
        print_files = match.get("print_files", "")

        for f in drive.files().list(q=query, fields="files(id,name,mimeType)").execute()["files"]:
            name, fid = f["name"], f["id"]
            if (
                name.endswith(".emb")
                or name in prod_files.split(",")
                or name in print_files.split(",")
            ):
                new_name = f"{new_id}.emb" if name.endswith(".emb") else None
                copied = copy_item(fid, new_folder, new_name)
                make_public(copied["id"])

        # 4) Build the new row for the sheet
        ts = datetime.now(ZoneInfo("America/New_York")).strftime("%-m/%-d/%Y %H:%M:%S")
        row = [
            new_id,                         # A - Order #
            ts,                             # B - Date
            match.get("Preview", ""),       # C - Preview
            match.get("Company Name", ""),  # D
            match.get("Design", ""),        # E
            match.get("Quantity", ""),      # F
            "",                             # G - Shipped (leave blank for reorder)
            match.get("Product", ""),       # H
            match.get("Stage", ""),         # I
            match.get("Price", ""),         # J
            new_due,                        # K - Due Date (override)
            "PRINT" if print_files else "NO",  # L - Print
            match.get("Material1", ""),     # M
            match.get("Material2", ""),     # N
            match.get("Material3", ""),     # O
            match.get("Material4", ""),     # P
            match.get("Material5", ""),     # Q
            match.get("Back Material", ""), # R
            match.get("Fur Color", ""),     # S
            match.get("EMB Backing", ""),   # T
            match.get("Top Stitch Color", ""),  # U
            match.get("Ship Date", ""),     # V
            match.get("Stitch Count", ""),  # W
            new_notes,                      # X - Notes (override)
            match.get("Image", ""),         # Y
            print_files,                    # Z - Print Files
            "",                             # AA - Empty/Unused
            new_type,                       # AB - Hard Date/Soft Date (override)
            match.get("Schedule String", ""),  # AC
            "",                             # AD - Start Date
            "",                             # AE - End Date
            match.get("Threads", ""),       # AF
            ""                              # AG - Tracking #
        ]


        while len(row) < 33:
            row.append("")

        print("📤 Writing new row to sheet:", row)

        next_row = len(colA) + 1
        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Production Orders!A{next_row}:AC{next_row}",
            valueInputOption="USER_ENTERED",
            body={"values": [row]}
        ).execute()

        print(f"✅ Reorder #{new_id} submitted successfully.")
        return jsonify({"status": "ok", "order": new_id}), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        print("❌ Exception during reorder:", str(e))
        return jsonify({"error": f"Server error: {str(e)}"}), 500


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
            "invoice": "https://example.com/invoice.pdf",
            "slips": ["https://example.com/slip1.pdf"]
        })

    except Exception as e:
        print("❌ Shipment error:", str(e))
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


# ─── Socket.IO connect/disconnect ─────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    logger.info(f"Client connected: {request.sid}")

@socketio.on("disconnect")
def on_disconnect():
    logger.info(f"Client disconnected: {request.sid}")

# ─── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting on port {port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=True, use_reloader=False)
