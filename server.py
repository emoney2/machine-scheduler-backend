# ─── Imports & Logger Setup ─────────────────────────────────────────────────
import os
import json
import logging
import time
from functools import wraps
from dotenv import load_dotenv

from eventlet.semaphore import Semaphore
from flask import Flask, jsonify, request, session, redirect, url_for, render_template_string
from flask import make_response
from flask_cors import CORS
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
      r"/api/*":    {"origins": FRONTEND_URL},
      r"/submit":   {"origins": FRONTEND_URL}
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
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error":"authentication required"}), 401
            return redirect(url_for("login", next=request.path))
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
            session["user"] = u
            return redirect(FRONTEND_URL)
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
        ts = datetime.now().strftime("%-m/%-d/%Y %H:%M:%S")
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

        # upload print files (if present)
        print_links = ""
        if print_files:
            # first create the “Print Files” folder under the job folder
            pf = drive.files().create(
                body={
                  "name": "Print Files",
                  "mimeType": "application/vnd.google-apps.folder",
                  "parents": [folder]
                },
                fields="id"
            ).execute().get("id")
            make_public(pf)

            # upload each print file into that folder
            links = []
            for f in print_files:
                m = MediaIoBaseUpload(f.stream, mimetype=f.mimetype)
                up = drive.files().create(
                    body={"name": f.filename, "parents": [pf]},
                    media_body=m,
                    fields="id, webViewLink"
                ).execute()
                make_public(up["id"])
                links.append(up["webViewLink"])

            # **assign the comma-joined URLs** so they get written to the sheet
            print_links = ",".join(links)


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
          reenter,
          schedule_str
        ]

        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Production Orders!A{next_row}:AB{next_row}",
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
@app.route("/api/materials", methods=["POST"])
@login_required_session
def add_material():
    """
    Appends a new row to the Material Inventory sheet.
    Expects JSON with:
      materialName, unit, minInv, reorder, cost
    """
    data = request.get_json(silent=True) or {}
    name = data.get("materialName", "").strip()
    unit = data.get("unit", "").strip()
    min_inv = data.get("minInv", "").strip()
    reorder = data.get("reorder", "").strip()
    cost = data.get("cost", "").strip()
    try:
        # 1) Determine next row index
        existing = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Material Inventory!A2:A"
        ).execute().get("values", [])
        next_row = len(existing) + 2  # because A2 is row 2

        # 2) Grab row-2 formulas for B, C, H
        fmt_B2 = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Material Inventory!B2:B2",
            valueRenderOption="FORMULA"
        ).execute()["values"][0][0]
        fmt_C2 = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Material Inventory!C2:C2",
            valueRenderOption="FORMULA"
        ).execute()["values"][0][0]
        fmt_H2 = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Material Inventory!H2:H2",
            valueRenderOption="FORMULA"
        ).execute()["values"][0][0]

        # 3) Update formulas to reference new row
        row_ref = str(next_row)
        formulaB = fmt_B2.replace("2", row_ref)
        formulaC = fmt_C2.replace("2", row_ref)
        formulaH = fmt_H2.replace("2", row_ref)

        # 4) Build and append the row A→H
        row = [name, formulaB, formulaC, unit, min_inv, reorder, cost, formulaH]
        sheets.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Material Inventory!A2:H",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

        return jsonify({"status": "ok"}), 200

    except Exception:
        logger.exception("Error adding new material")
        return jsonify({"error": "Failed to add material"}), 500



@app.route("/api/fur-colors", methods=["GET"])
@login_required_session
def get_fur_colors():
    """
    Returns JSON array of fur color names from the
    'Material Inventory' sheet, Column H (cells H2:H).
    """
    try:
        # Adjust the range to grab column H
        rows = fetch_sheet(SPREADSHEET_ID, "Material Inventory!H2:H")
        fur_colors = [r[0] for r in rows if r and r[0].strip()]
        return jsonify(fur_colors), 200
    except Exception:
        logger.exception("Error fetching fur colors")
        return jsonify([]), 200


@app.route("/api/materials", methods=["GET"])
@login_required_session
def get_materials():
    """
    Returns JSON array of material names from the 'Material Inventory' sheet (column A).
    """
    try:
        rows = fetch_sheet(SPREADSHEET_ID, "Material Inventory!A2:A")
        materials = [r[0] for r in rows if r and r[0].strip()]
        return jsonify(materials), 200
    except Exception:
        logger.exception("Error fetching materials")
        return jsonify([]), 200

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
