# ─── Imports & Logger Setup ─────────────────────────────────────────────────
import os
import json
import logging
import time
from functools import wraps
from dotenv import load_dotenv

from eventlet.semaphore import Semaphore
from flask import Flask, jsonify, request, session, redirect, url_for, render_template_string
from flask_cors import CORS
from flask_socketio import SocketIO

from google.oauth2 import service_account
from googleapiclient.discovery import build
from httplib2 import Http
from google_auth_httplib2 import AuthorizedHttp

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
# allow cross-site cookies
app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
)

# only allow our Netlify front-end on /api/* and support cookies
CORS(
    app,
    resources={ r"/api/*": {"origins": FRONTEND_URL} },
    supports_credentials=True
)

# Socket.IO (same origin)
socketio = SocketIO(
    app,
    cors_allowed_origins=FRONTEND_URL,
    async_mode="eventlet"
)

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
        "credentials.json", scopes=["https://www.googleapis.com/auth/spreadsheets"]
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
    """
    Loads whatever is in cell J2 of Manual State and returns it as a string.
    Falls back to empty string if anything goes wrong.
    """
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
    # grab the sheet-stored password
    sheet_pw = get_sheet_password()

    if request.method == "POST":
        form_pw = request.form["password"]
        if form_pw == sheet_pw:
            session["user"] = "admin"
            # always send back to your React root
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
