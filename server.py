# ─── Imports & Logger Setup ─────────────────────────────────────────────────
import os
import json
import logging
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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

        # 3) Token‐match check: 
        #    “token_at_login” must equal the current ADMIN_TOKEN, 
        #    otherwise we immediately log out.
        ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
        token_at_login = session.get("token_at_login", "")
        if token_at_login != ADMIN_TOKEN:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "session invalidated"}), 401
            return redirect(url_for("login", next=request.path))

        # 4) Idle timeout: 3 hours of inactivity
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
            # if no "last_activity" for whatever reason, force login
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("login", next=request.path))

        # 5) All good—update last_activity and proceed
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
    print("🔧 Received /updateStartTime payload:", data)

    row_id = data.get("id")
    start_time = data.get("startTime")

    if not row_id or not start_time:
        return jsonify({"error": "Missing ID or start time"}), 400

    try:
        success = update_embroidery_start_time_in_sheet(row_id, start_time)
        if success:
            return jsonify({"status": "ok"}), 200
        else:
            return jsonify({"error": "Update failed"}), 500
    except Exception as e:
        print("❌ Server error:", e)
        return jsonify({"error": str(e)}), 500


# ✅ You must define or update this function to match your actual Google Sheet logic
import traceback

def update_embroidery_start_time_in_sheet(job_id, start_time):
    """
    Finds the row in 'Embroidery List' where column A matches job_id,
    then writes start_time to column AA (27th column).
    """
    service = get_sheets_service()
    sheet = service.spreadsheets()

    # Fetch column A (Job IDs) from Embroidery List
    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Embroidery List!A2:A"
    ).execute()

    values = result.get("values", [])

    # Find the matching row
    for i, row in enumerate(values, start=2):  # A2 = row 2
        if str(row[0]) == str(job_id):
            target_range = f"Embroidery List!AA{i}"
            sheet.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=target_range,
                valueInputOption="RAW",
                body={"values": [[start_time]]}
            ).execute()
            print(f"✅ Updated embroidery_start for job {job_id} at {target_range}")
            return True

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
<!doctype html>
<title>Login</title>
<h2>Please log in</h2>
<form method=post>
  <input name=username placeholder="Username" required>
  <input name=password type=password placeholder="Password" required>
  <button type=submit>Log In</button>
</form>
{% if error %}<p style="color:red">{{ error }}</p>{% endif %}
"""

@app.route("/login", methods=["GET","POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form["username"]
        p = request.form["password"]
        ADMIN_PW    = os.environ.get("ADMIN_PASSWORD", "")
        ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

        if u == "admin" and p == ADMIN_PW:
            session.clear()
            session["user"]           = u
            # Instead of pwd_at_login, store token_at_login:
            session["token_at_login"] = ADMIN_TOKEN
            session["last_activity"]  = datetime.utcnow().isoformat()
            return redirect(FRONTEND_URL)
        else:
            error = "Invalid credentials"
    return render_template_string(_login_page, error=error)



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
    data = request.get_json()
    product = data.get("product")
    length = data.get("length")
    width = data.get("width")
    height = data.get("height")

    if not all([product, length, width, height]):
        return jsonify({"error": "Missing fields"}), 400

    try:
        volume = int(length) * int(width) * int(height)
    except ValueError:
        return jsonify({"error": "Invalid dimensions"}), 400

    sheets = get_sheets_service().spreadsheets()
    table_range = "Table!A2:A"
    result = sheets.values().get(spreadsheetId=SHEET_ID, range=table_range).execute()
    rows = result.get("values", [])
    products = [row[0] for row in rows if row]

    if product in products:
        row_index = products.index(product) + 2
    else:
        row_index = len(products) + 2
        sheets.values().append(
            spreadsheetId=SHEET_ID,
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
        def tpl_formula(cell):
            resp = sheets.values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"Production Orders!{cell}2",
                valueRenderOption="FORMULA"
            ).execute()
            raw = resp.get("values", [[""]])[0][0] or ""
            return raw.replace("2", str(next_row))

        # timestamp + template cells from row 2
        ts = datetime.now(ZoneInfo("America/New_York")).strftime("%-m/%-d/%Y %H:%M:%S")
        def tpl(cell):
            return sheets.values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"Production Orders!{cell}2"
            ).execute().get("values",[[""]])[0][0]

        preview      = tpl_formula("C")
        stage        = tpl_formula("I")
        ship_date    = tpl_formula("V")
        stitch_count = tpl_formula("W")
        reenter      = "FALSE"
        schedule_str = tpl_formula("AC")

        # create Drive folder for this order
        drive = build("drive","v3",credentials=creds)
        # helper: grant “anyone with link” reader access
        def make_public(file_id):
            drive.permissions().create(
                fileId=file_id,
                body={"type":"anyone","role":"reader"}
            ).execute()
        folder_meta = {
            "name": str(new_order),
            "mimeType": "application/vnd.google-apps.folder"
        }
        folder = drive.files().create(
            body=folder_meta,
            fields="id"
        ).execute().get("id")
        make_public(folder)


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
                body={"name": f.filename, "parents": [folder]},
                media_body=m,
                fields="id, webViewLink"
            ).execute()
            make_public(up["id"])
            prod_links.append(up["webViewLink"])

        # upload print files (if present) — link to the “Print Files” folder
        print_links = ""
        if print_files:
            # 1) create the “Print Files” folder under the job folder, and get its link
            pf_res = drive.files().create(
                body={
                  "name": "Print Files",
                  "mimeType": "application/vnd.google-apps.folder",
                  "parents": [folder]
                },
                fields="id, webViewLink"
            ).execute()
            pf_id = pf_res["id"]
            pf_link = pf_res["webViewLink"]
            make_public(pf_id)

            # 2) upload each print file into that folder (we don't need their individual links)
            for f in print_files:
                m = MediaIoBaseUpload(f.stream, mimetype=f.mimetype)
                drive.files().create(
                    body={"name": f.filename, "parents": [pf_id]},
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

        # now turn that one cell into a checkbox
        meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        orders_sheet = next(
            s for s in meta["sheets"]
            if s["properties"]["title"] == "Production Orders"
        )
        sheet_id = orders_sheet["properties"]["sheetId"]
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={
                "requests": [{
                    "setDataValidation": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": next_row - 1,
                            "endRowIndex": next_row,
                            "startColumnIndex": 28,
                            "endColumnIndex": 29
                        },
                        "rule": {
                            "condition": {"type": "BOOLEAN"},
                            "showCustomUi": True,
                            "strict": False
                        }
                    }
                }]
            }
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
    data = request.get_json(silent=True) or {}
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
    """
    Adds new materials to Material Inventory, then logs to Material Log.
    Expects JSON array of objects with:
      materialName, unit, minInv, reorder, cost, action, quantity, notes?
    """
    raw   = request.get_json(silent=True) or []
    items = raw if isinstance(raw, list) else [raw]

    # 1) Fetch the formulas from row 2, columns B, C, H
    def get_formula(col):
        resp = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Material Inventory!{col}2",
            valueRenderOption="FORMULA"
        ).execute()
        return resp.get("values", [[""]])[0][0] or ""

    fmtB = get_formula("B")
    fmtC = get_formula("C")
    fmtH = get_formula("H")

    now      = datetime.now(ZoneInfo("America/New_York"))\
                    .strftime("%-m/%-d/%Y %H:%M:%S")
    inv_rows = []
    log_rows = []

    for it in items:
        name    = it.get("materialName","").strip()
        unit    = it.get("unit","").strip()
        mininv  = it.get("minInv","").strip()
        reorder = it.get("reorder","").strip()
        cost    = it.get("cost","").strip()
        action  = it.get("action","").strip()
        qty     = it.get("quantity","").strip()
        notes   = it.get("notes","").strip()

        # skip incomplete entries
        if not (name and action and qty):
            continue

        # 2) Build the inventory row A–H
        inv_rows.append([
            name,   # A
            fmtB,   # B (formula from B2)
            fmtC,   # C (formula from C2)
            unit,   # D
            mininv, # E
            reorder,# F
            cost,   # G
            fmtH    # H (formula from H2)
        ])

        # 3) Build the log row A–I
        log_rows.append([
            now,    # A: timestamp
            "",     # B
            "",     # C
            "",     # D
            "",     # E
            name,   # F: material
            qty,    # G: quantity
            "IN",   # H
            action  # I: O/R
        ])

    # 4) Append to Material Inventory
    if inv_rows:
        sheets.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Material Inventory!A2:H",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": inv_rows}
        ).execute()

    # 5) Append to Material Log
    if log_rows:
        sheets.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Material Log!A2:I",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": log_rows}
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
    order_ids = data.get("order_ids", [])
    boxes = data.get("boxes", [])
    shipped_quantities = data.get("shipped_quantities", {})  # New

    if not order_ids:
        return jsonify({"error": "Missing order_ids"}), 400

    sheet_id = os.environ["SPREADSHEET_ID"]
    sheet_name = "Production Orders"
    try:
        # Fetch current sheet data
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{sheet_name}!A1:Z",
        ).execute()
        rows = result.get("values", [])

        headers = rows[0]
        id_col = headers.index("Order ID")
        shipped_col = headers.index("Shipped")
        stage_col = headers.index("Stage")

        # Prepare updates
        updates = []
        for i, row in enumerate(rows[1:], start=2):  # start=2 for 1-based + header row
            order_id = row[id_col] if id_col < len(row) else ""
            if order_id in order_ids:
                shipped_qty = shipped_quantities.get(order_id)
                if shipped_qty is not None:
                    updates.append({
                        "range": f"{sheet_name}!{chr(71)}{i}",  # Column G = 71 = 'G'
                        "values": [[str(shipped_qty)]]
                    })
                updates.append({
                    "range": f"{sheet_name}!{chr(stage_col + 65)}{i}",
                    "values": [["Complete"]]
                })

        # Apply updates
        if updates:
            sheets_service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": updates}
            ).execute()

        # Simulate label, invoice, and slip creation
        return jsonify({
            "labels": ["https://example.com/label1.pdf"],
            "invoice": "https://example.com/invoice.pdf",
            "slips": ["https://example.com/slip1.pdf"]
        })

    except Exception as e:
        print("Shipment error:", str(e))
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
