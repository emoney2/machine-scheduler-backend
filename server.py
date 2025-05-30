# ─── Imports & Logger Setup ─────────────────────────────────────────────────
import os
import json
import logging
import time
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


# ─── Load .env & Logger ─────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Front-end URL & Flask Setup ─────────────────────────────────────────────
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://machineschedule.netlify.app")

# ─── Flask + CORS + SocketIO ────────────────────────────────────────────────────
app = Flask(__name__)
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
        r"/api/*":        {"origins": FRONTEND_URL},
        r"/api/threads":  {"origins": FRONTEND_URL},
        r"/submit":       {"origins": FRONTEND_URL}
    },
    supports_credentials=True
)

# Socket.IO (same origin)
socketio = SocketIO(
    app,
    cors_allowed_origins=FRONTEND_URL,
    async_mode="eventlet"
)

from flask import session  # (if not already imported)

@app.before_request
def _debug_session():
    logger.info("🔑 Session data for %s → %s", request.path, dict(session))


# After-request, echo back the real Origin so withCredentials can work
@app.after_request
def apply_cors(response):
    origin = request.headers.get("Origin")
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

        if request.method == "OPTIONS":
            return make_response("", 204)

        # 1) Must be logged in
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error":"authentication required"}), 401
            return redirect(url_for("login", next=request.path))

        # 2) Immediate logout on password change
        current_pw = get_sheet_password()
        if session.get("pwd_at_login") != current_pw:
            session.clear()
            return redirect(url_for("login", next=request.path))

        # 3) Idle‐timeout: 3 hours of inactivity
        last = session.get("last_activity")
        if last:
            last_dt = datetime.fromisoformat(last)
            if datetime.utcnow() - last_dt > timedelta(hours=3):
                session.clear()
                return redirect(url_for("login", next=request.path))
        else:
            # if somehow never set, force a fresh login
            session.clear()
            return redirect(url_for("login", next=request.path))

        # 4) All good — update last_activity and proceed
        session["last_activity"] = datetime.utcnow().isoformat()
        return f(*args, **kwargs)
    return decorated


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

# ─── In-memory caches & settings ────────────────────────────────────────────
# with CACHE_TTL = 0, every GET will hit Sheets directly
CACHE_TTL           = 0

# orders cache + timestamp
_orders_cache       = None
_orders_ts          = 0

# embroidery list cache + timestamp
_emb_cache          = None
_emb_ts             = 0

# manualState cache + timestamp (for placeholders & machine assignments)
_manual_state_cache = None
_manual_state_ts    = 0


def fetch_sheet(spreadsheet_id, sheet_range):
    with sheet_lock:
        res = sheets.values().get(
            spreadsheetId=spreadsheet_id,
            range=sheet_range
        ).execute()
    return res.get("values", [])

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
        # pull the live password from J2
        sheet_pw = get_sheet_password()
        # only "admin" + exact sheet password unlocks
        if u == "admin" and p == sheet_pw:
            # fresh login: clear any old session data
            session.clear()
            session["user"]         = u
            session["pwd_at_login"] = sheet_pw
            # track last activity for idle timeout
            session["last_activity"] = datetime.utcnow().isoformat()
            return redirect(FRONTEND_URL)
        error = "Invalid credentials"
    # For GET requests, or POST with bad creds, show the login form
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

@app.route("/api/embroideryList", methods=["GET"])
@login_required_session
def get_embroidery_list():
    try:
        rows = fetch_sheet(SPREADSHEET_ID, EMBROIDERY_RANGE)
        headers = rows[0] if rows else []
        data    = [dict(zip(headers,r)) for r in rows[1:]] if rows else []
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
MANUAL_RANGE       = os.environ.get("MANUAL_RANGE", "Manual State!A2:H")
MANUAL_CLEAR_RANGE = os.environ.get("MANUAL_RANGE", "Manual State!A2:H")

# ─── MANUAL STATE ENDPOINT (GET) ───────────────────────────────────────────────
@app.route("/api/manualState", methods=["GET"])
@login_required_session
def get_manual_state():
    """
    Returns JSON:
      {
        machine1: [...],
        machine2: [...],
        placeholders: [
          { id, company, quantity, stitchCount, inHand, dueType },
          … up to however many rows you have …
        ]
      }
    """
    global _manual_state_cache, _manual_state_ts
    now = time.time()

    if _manual_state_cache is not None and (now - _manual_state_ts) < CACHE_TTL:
        return jsonify(_manual_state_cache), 200

    try:
        # read columns A–H from row 2 down
        resp = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=MANUAL_RANGE  # e.g. "Manual State!A2:H50"
        ).execute()
        rows = resp.get("values", [])

        # first row contains A2/B2 lists + first placeholder C–H
        first = rows[0] if rows else []
        while len(first) < 8:
            first.append("")

        ms1 = [s for s in first[0].split(",") if s]
        ms2 = [s for s in first[1].split(",") if s]

        phs = []
        for r in rows:
            # placeholder columns are indices 2–7 (C–H)
            if len(r) >= 3 and r[2].strip():
                ph = {
                    "id":          r[2],
                    "company":     r[3] if len(r) > 3 else "",
                    "quantity":    r[4] if len(r) > 4 else "",
                    "stitchCount": r[5] if len(r) > 5 else "",
                    "inHand":      r[6] if len(r) > 6 else "",
                    "dueType":     r[7] if len(r) > 7 else ""
                }
                phs.append(ph)

        result = {
            "machine1":     ms1,
            "machine2":     ms2,
            "placeholders": phs
        }

        _manual_state_cache = result
        _manual_state_ts    = now
        return jsonify(result), 200

    except Exception:
        logger.exception("Error reading manual state")
        if _manual_state_cache:
            return jsonify(_manual_state_cache), 200
        return jsonify({"machine1": [], "machine2": [], "placeholders": []}), 200


# ─── MANUAL STATE ENDPOINT (POST) ──────────────────────────────────────────────
@app.route("/api/manualState", methods=["POST"])
@login_required_session
def save_manual_state():
    """
    Expects JSON:
      {
        machine1: [...],
        machine2: [...],
        placeholders: [
          { id, company, quantity, stitchCount, inHand, dueType },
          …
        ]
      }
    Writes back:
      - A2 = comma-joined machine1
      - B2 = comma-joined machine2
      - C2:H2 = placeholders[0]
      - C3:H3 = placeholders[1], etc.
    """
    global _manual_state_cache, _manual_state_ts

    data = request.get_json(silent=True) or {}
    m1  = data.get("machine1", [])
    m2  = data.get("machine2", [])
    phs = data.get("placeholders", [])

    # build the 2D array of rows to write
    rows = []
    # first row: machines + first placeholder (if any)
    first_fields = [""] * 6
    if phs:
        first = phs[0]
        first_fields = [
            first.get("id", ""),
            first.get("company", ""),
            first.get("quantity", ""),
            first.get("stitchCount", ""),
            first.get("inHand", ""),
            first.get("dueType", "")
        ]
    rows.append([
        ",".join(m1),
        ",".join(m2),
        *first_fields
    ])

    # subsequent placeholder rows
    for ph in phs[1:]:
        rows.append([
            "", "",
            ph.get("id",""),
            ph.get("company",""),
            ph.get("quantity",""),
            ph.get("stitchCount",""),
            ph.get("inHand",""),
            ph.get("dueType","")
        ])

    # determine how many rows to write (at least 1)
    num_rows = max(1, len(rows))
    end_row  = 2 + num_rows - 1  # row 2 through row end_row

    write_range = f"{MANUAL_RANGE.split('!')[0]}!A2:H{end_row}"

    try:
        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=write_range,
            valueInputOption="RAW",
            body={"values": rows}
        ).execute()

        _manual_state_cache = {
            "machine1":     m1,
            "machine2":     m2,
            "placeholders": phs
        }
        _manual_state_ts = time.time()

        socketio.emit("manualStateUpdated", _manual_state_cache)
        return jsonify({"status": "ok"}), 200

    except Exception:
        logger.exception("Error writing manual state")
        return jsonify({"status": "error"}), 500

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
@cross_origin(origins=FRONTEND_URL, supports_credentials=True)
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
@cross_origin(origins=FRONTEND_URL, supports_credentials=True)
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
@cross_origin(origins=FRONTEND_URL, supports_credentials=True)
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
@cross_origin(origins=FRONTEND_URL, supports_credentials=True)
def material_inventory_preflight():
    return make_response("", 204)

# ─── MATERIAL-LOG POST ────────────────────────────────────────────────────
@app.route("/api/materialInventory", methods=["POST"])
@cross_origin(origins=FRONTEND_URL, supports_credentials=True)
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
        qty_idx = i_or - 1              # Quantity is one column left of O/R
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
@cross_origin(origins=FRONTEND_URL, supports_credentials=True)
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
