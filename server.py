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
from werkzeug.security import check_password_hash, generate_password_hash

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

app = Flask(__name__)
# allow cross-site cookies
app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
)

# only allow our Netlify front-end on /api/*
CORS(
    app,
    resources={r"/api/*": {"origins": FRONTEND_URL}},
    supports_credentials=True
)

# Socket.IO (same origin)
socketio = SocketIO(
    app,
    cors_allowed_origins=FRONTEND_URL,
    async_mode="eventlet"
)

# echo back the real Origin so withCredentials can work
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

# in-memory user store (just one admin)
users = {
    "admin": generate_password_hash(os.environ.get("ADMIN_PW", "changeme"))
}

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

def fetch_sheet(spreadsheet_id, sheet_range):
    with sheet_lock:
        res = sheets.values().get(
            spreadsheetId=spreadsheet_id,
            range=sheet_range
        ).execute()
    return res.get("values", [])

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
        if u in users and check_password_hash(users[u], p):
            session["user"] = u
            nxt = request.args.get("next") or ""
            return redirect(f"{FRONTEND_URL}{nxt}")
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

@app.route("/api/manualState", methods=["GET"])
@login_required_session
def get_manual_state():
    """
    Returns JSON:
      {
        "machine1": [...],           # from A2 (comma-list)
        "machine2": [...],           # from B2 (comma-list)
        "placeholders": [            # every row where col C (id) is non-empty
          { id, company, quantity, stitchCount, inHand, dueType }, …
        ]
      }
    """
    try:
        rows = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=MANUAL_RANGE
        ).execute().get("values", [])
    except Exception:
        logger.exception("Error fetching manualState")
        return jsonify({"machine1": [], "machine2": [], "placeholders": []}), 200

    if not rows:
        return jsonify({"machine1": [], "machine2": [], "placeholders": []}), 200

    # first row drives machine lists
    first = rows[0] + ["", ""]  # pad to at least 2 cols
    ms1 = [s for s in first[0].split(",") if s]
    ms2 = [s for s in first[1].split(",") if s]

    # every row (including first) with a non-empty ID in col C
    phs = []
    for row in rows:
        # pad so we can index 0..7 safely
        r = (row + [""] * 8)[:8]
        if r[2].strip():
            phs.append({
                "id": r[2],
                "company":     r[3],
                "quantity":    r[4],
                "stitchCount": r[5],
                "inHand":      r[6],
                "dueType":     r[7]
            })

    result = {"machine1": ms1, "machine2": ms2, "placeholders": phs}
    return jsonify(result), 200


@app.route("/api/manualState", methods=["POST"])
@login_required_session
def save_manual_state():
    """
    Expects JSON:
      {
        machine1: [...],
        machine2: [...],
        placeholders: [ { id, company, quantity, stitchCount, inHand, dueType }, … ]
      }
    Overwrites Manual State!A2:H with rows:
      • row2: [A2]=csv(machine1), [B2]=csv(machine2), [C2-H2]=first ph
      • row3+:      [C-H]=subsequent phs
    """
    data = request.get_json(silent=True) or {}
    m1  = data.get("machine1", [])
    m2  = data.get("machine2", [])
    phs = data.get("placeholders", [])

    # Build the 2D array of values to write
    values = []
    if phs:
        # first row includes A/B
        first = phs[0]
        values.append([
            ",".join(m1),
            ",".join(m2),
            first.get("id",""),
            first.get("company",""),
            first.get("quantity",""),
            first.get("stitchCount",""),
            first.get("inHand",""),
            first.get("dueType",""),
        ])
        # any additional placeholders go on their own rows, with blank A/B
        for ph in phs[1:]:
            values.append([
                "", "",           # fill A/B empty
                ph.get("id",""),
                ph.get("company",""),
                ph.get("quantity",""),
                ph.get("stitchCount",""),
                ph.get("inHand",""),
                ph.get("dueType",""),
            ])
    else:
        # no placeholders: still need to write machine lists row2, then nothing else
        values.append([ ",".join(m1), ",".join(m2), "", "", "", "", "", "" ])

    try:
        # 1) clear out the old area
        sheets.values().clear(
            spreadsheetId=SPREADSHEET_ID,
            range=MANUAL_CLEAR_RANGE
        ).execute()

        # 2) write the new block
        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=MANUAL_RANGE,
            valueInputOption="RAW",
            body={"values": values}
        ).execute()

        # 3) broadcast so clients re-fetch
        new_state = {"machine1": m1, "machine2": m2, "placeholders": phs}
        socketio.emit("manualStateUpdated", new_state, broadcast=True)
        return jsonify({"status":"ok"}), 200

    except Exception:
        logger.exception("Error writing manualState")
        return jsonify({"status":"error"}), 500

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
