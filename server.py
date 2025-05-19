# ─── Imports & Logger Setup ─────────────────────────────────────────────────
import os
import json
import logging
import time
from dotenv import load_dotenv

# configure root logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# add this import before you use Semaphore()
from eventlet.semaphore import Semaphore

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO
from google.oauth2 import service_account
from googleapiclient.discovery import build
from httplib2 import Http
from google_auth_httplib2 import AuthorizedHttp

# ─── Load .env ────────────────────────────────────────────────────────────
load_dotenv()

# ─── Semaphore for serializing sheet calls ────────────────────────────────
sheet_lock = Semaphore(1)


# ─── Google Sheets credentials ─────────────────────────────────────────────
SPREADSHEET_ID   = os.environ.get("SPREADSHEET_ID")
ORDERS_RANGE     = os.environ.get("ORDERS_RANGE",     "Production Orders!A1:AM")
EMBROIDERY_RANGE = os.environ.get("EMBROIDERY_RANGE", "Embroidery List!A1:AM")
MANUAL_RANGE     = os.environ.get("MANUAL_RANGE",     "Manual State!A2:Z2")


CREDENTIALS_FILE = "credentials.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

if os.environ.get("GOOGLE_CREDENTIALS"):
    # in production: credentials JSON is stored in env var
    creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=SCOPES
    )
else:
    # local dev: read from credentials.json on disk
    if not os.path.exists(CREDENTIALS_FILE):
        logging.error(f"⚠️  {CREDENTIALS_FILE} not found!")
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=SCOPES
    )

logging.info("🔑 Service account email: %s", creds.service_account_email)

# wrap httplib2.Http to enforce a timeout
_http = Http(timeout=10)
authed_http = AuthorizedHttp(creds, http=_http)

# build the Sheets client (disable discovery cache so updates are always fresh)
service = build(
    "sheets",
    "v4",
    credentials=creds,
    cache_discovery=False
)
sheets = service.spreadsheets()
# ─── Flask + CORS + SocketIO ────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ─── In-memory caches & settings ────────────────────────────────────────────────
# with CACHE_TTL = 0, every GET will hit Sheets directly
CACHE_TTL           = 0   
_orders_cache       = None
_orders_ts          = 0
_emb_cache          = None
_emb_ts             = 0
_manual_state_cache = None
_manual_state_ts    = 0

# ─── In-memory links store ──────────────────────────────────────────────────────
_links_store = {}

# ─── Helpers ───────────────────────────────────────────────────────────────────
def apply_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,OPTIONS"
    return response

app.after_request(apply_cors)

def fetch_sheet(spreadsheet_id, sheet_range):
    with sheet_lock:
        res = sheets.values().get(
            spreadsheetId=spreadsheet_id,
            range=sheet_range
        ).execute()
    return res.get("values", [])



# ─── ORDERS ENDPOINT ───────────────────────────────────────────────────────────
@app.route("/api/orders", methods=["GET"])
def get_orders():
    global _orders_cache, _orders_ts
    now = time.time()
    if _orders_cache is not None and (now - _orders_ts) < CACHE_TTL:
        return jsonify(_orders_cache), 200

    try:
        rows = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE)
        _orders_ts = now
        if not rows:
            _orders_cache = []
        else:
            headers = rows[0]
            _orders_cache = [dict(zip(headers, r)) for r in rows[1:]]
    except Exception:
        logger.exception("Error fetching orders sheet")
        if _orders_cache is not None:
            return jsonify(_orders_cache), 200
        _orders_cache = []
    return jsonify(_orders_cache), 200

# ─── EMBROIDERY LIST ENDPOINT ──────────────────────────────────────────────────
@app.route("/api/embroideryList", methods=["GET"])
def get_embroidery_list():
    global _emb_cache, _emb_ts
    now = time.time()
    if _emb_cache is not None and (now - _emb_ts) < CACHE_TTL:
        return jsonify(_emb_cache), 200

    try:
        rows = fetch_sheet(SPREADSHEET_ID, EMBROIDERY_RANGE)
        _emb_ts = now
        if not rows:
            _emb_cache = []
        else:
            headers = rows[0]
            _emb_cache = [dict(zip(headers, r)) for r in rows[1:]]
    except Exception:
        logger.exception("Error fetching embroidery sheet")
        if _emb_cache is not None:
            return jsonify(_emb_cache), 200
        _emb_cache = []
    return jsonify(_emb_cache), 200

# ─── UPDATE SINGLE ORDER ───────────────────────────────────────────────────────
@app.route("/api/orders/<order_id>", methods=["PUT"])
def update_order(order_id):
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    logger.info(f"Received update for order {order_id}: {data!r}")
    try:
        # serialize this write too
        with sheet_lock:
            sheets.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"Production Orders!H{order_id}",
                valueInputOption="RAW",
                body={"values": [[ data.get("embroidery_start", "") ]]}
            ).execute()
        socketio.emit("orderUpdated", {"orderId": order_id})
        return jsonify({"status": "ok"}), 200

    except Exception:
        logger.exception(f"Failed to update order {order_id}")
        return jsonify({"error": "Server error"}), 500


# ─── LINKS ENDPOINTS ───────────────────────────────────────────────────────────
@app.route("/api/links", methods=["GET"])
def get_links():
    return jsonify(_links_store), 200

@app.route("/api/links", methods=["POST"])
def save_links():
    global _links_store
    _links_store = request.get_json() or {}
    logger.info(f"Links updated: {_links_store}")
    socketio.emit("linksUpdated", _links_store)
    return jsonify({"status": "ok"}), 200

# ─── PLACEHOLDERS SOCKET.IO RELAY ────────────────────────────────────────────
@socketio.on("placeholdersUpdated")
def handle_placeholders_updated(data):
    """
    When any client emits 'placeholdersUpdated', broadcast it back to everyone
    so they can re-fetch and see the new placeholder immediately.
    """
    logger.info(f"Received placeholdersUpdated: {data}")
    socketio.emit("placeholdersUpdated", data, broadcast=True)


# ─── MANUAL STATE ENDPOINTS ───────────────────────────────────────────────────
# at the very top, after your other imports:
from eventlet.semaphore import Semaphore

# just below your creds setup:
sheet_lock = Semaphore(1)


# ─── MANUAL STATE ENDPOINTS ───────────────────────────────────────────────────
# ─── Use C–H to hold one placeholder record ──────────────────────────────────
MANUAL_RANGE     = os.environ.get("MANUAL_RANGE", "Manual State!A2:H2")

# ─── MANUAL STATE ENDPOINTS ───────────────────────────────────────────────────
# ─── MANUAL STATE ENDPOINTS ───────────────────────────────────────────────────
@app.route("/api/manualState", methods=["GET"])
def get_manual_state():
    """
    Returns JSON:
      {
        "machine1": [...],
        "machine2": [...],
        "placeholders": [ { id, company, quantity, stitchCount, inHand, dueType }, … ]
      }
    """
    global _manual_state_cache, _manual_state_ts
    now = time.time()

    # serve from cache while fresh
    if _manual_state_cache is not None and (now - _manual_state_ts) < CACHE_TTL:
        return jsonify(_manual_state_cache), 200

    try:
        vals = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=MANUAL_RANGE
        ).execute().get("values", [])

        # A2,B2 = machine lists; C–H = first placeholder (if any)
        row = vals[0] if vals else ["", ""]
        while len(row) < 8:
            row.append("")

        ms1 = [s for s in row[0].split(",") if s]
        ms2 = [s for s in row[1].split(",") if s]

        # reconstruct the single placeholder (you can extend this to multiple)
        ph = {
          "id":          row[2],
          "company":     row[3],
          "quantity":    row[4],
          "stitchCount": row[5],
          "inHand":      row[6],
          "dueType":     row[7]
        }
        phs = row[2] and [ph] or []

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
        if _manual_state_cache is not None:
            return jsonify(_manual_state_cache), 200
        return jsonify({"machine1": [], "machine2": [], "placeholders": []}), 200


@app.route("/api/manualState", methods=["POST"])
def save_manual_state():
    """
    Expects JSON:
      {
        machine1: [...],
        machine2: [...],
        placeholders: [ { id, company, quantity, stitchCount, inHand, dueType }, … ]
      }
    """
    global _manual_state_cache, _manual_state_ts

    data = request.get_json(silent=True) or {}
    m1  = data.get("machine1", [])
    m2  = data.get("machine2", [])
    phs = data.get("placeholders", [])

    # build an 8-cell row:
    #  • A,B = comma-lists
    #  • C–H = the first placeholder’s six fields, or blanks
    if phs and isinstance(phs[0], dict):
        first = phs[0]
        fields = [
            first.get("id", ""),
            first.get("company", ""),
            first.get("quantity", ""),
            first.get("stitchCount", ""),
            first.get("inHand", ""),
            first.get("dueType", "")
        ]
    else:
        # either no placeholders or they were strings
        fields = [""] * 6

    row = [
        ",".join(m1),
        ",".join(m2),
        *fields
    ]

    try:
        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=MANUAL_RANGE,
            valueInputOption="RAW",
            body={"values": [ row ]}
        ).execute()

        # cache & timestamp
        _manual_state_cache = {
            "machine1":     m1,
            "machine2":     m2,
            "placeholders": phs if isinstance(phs, list) else []
        }
        _manual_state_ts    = time.time()

        # broadcast
        socketio.emit("manualStateUpdated", _manual_state_cache)
        return jsonify({"status": "ok"}), 200

    except Exception:
        logger.exception("Error writing manual state")
        return jsonify({"status": "error"}), 500

# ─── SOCKET.IO CONNECTION LOGS ────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    logger.info(f"Client connected: {request.sid}")

@socketio.on("disconnect")
def on_disconnect():
    logger.info(f"Client disconnected: {request.sid}")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting Flask-SocketIO on port {port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=True, use_reloader=False)
