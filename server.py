import eventlet
eventlet.monkey_patch()
from eventlet import semaphore

# serialize all Sheets API calls so Eventlet never does simultaneous reads
sheet_lock = semaphore.Semaphore(1)

import os
import logging
import time
import json
from dotenv import load_dotenv

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Load environment variables from .env (if present)
load_dotenv()

# ─── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Debug: make sure we're in the right directory and see what files are present
logger.info(f"▶︎ CWD = {os.getcwd()}")
logger.info(f"▶︎ Files here = {os.listdir('.')}")

# ─── Google Sheets configuration ───────────────────────────────────────────────
SPREADSHEET_ID   = "11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4"
ORDERS_RANGE     = "'Production Orders'!A:AM"
EMBROIDERY_RANGE = "'Embroidery List'!A:AM"
MANUAL_RANGE     = "'Manual State'!A2:B2"
CREDENTIALS_FILE = "credentials.json"

if not os.path.exists(CREDENTIALS_FILE):
    logger.error(f"⚠️  {CREDENTIALS_FILE} not found!")
else:
    size = os.path.getsize(CREDENTIALS_FILE)
    logger.info(f"✔️  {CREDENTIALS_FILE} exists, size {size} bytes")

logger.info(f"Loading Google credentials from {CREDENTIALS_FILE}")
creds = service_account.Credentials.from_service_account_file(
    CREDENTIALS_FILE,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
logger.info("🔑 Service account email: %s", creds.service_account_email)

sheets = build("sheets", "v4", credentials=creds).spreadsheets()


# ─── Flask + CORS + SocketIO ────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ─── In-memory caches & settings ────────────────────────────────────────────────
CACHE_TTL           = 300   # seconds
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
    # serialize .execute() calls so Eventlet never does two reads on the same socket
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

# ─── MANUAL STATE ENDPOINTS ───────────────────────────────────────────────────
@app.route("/api/manualState", methods=["GET"])
def get_manual_state():
    global _manual_state_cache, _manual_state_ts
    now = time.time()
    if _manual_state_cache is not None and (now - _manual_state_ts) < CACHE_TTL:
        return jsonify(_manual_state_cache), 200

    try:
        with sheet_lock:
            vals = sheets.values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=MANUAL_RANGE
            ).execute().get("values", [])
        row = vals[0] if vals else ["", ""]
        if len(row) < 2:
            row += [""] * (2 - len(row))
        ms1 = [s for s in row[0].split(",") if s]
        ms2 = [s for s in row[1].split(",") if s]
        result = {"machine1": ms1, "machine2": ms2}
        _manual_state_cache = result
        _manual_state_ts    = now
        return jsonify(result), 200

    except Exception:
        logger.exception("Error reading manual state")
        if _manual_state_cache is not None:
            return jsonify(_manual_state_cache), 200
        return jsonify({"machine1": [], "machine2": []}), 200


@app.route("/api/manualState", methods=["POST"])
def save_manual_state():
    data = request.get_json(silent=True) or {"machine1": [], "machine2": []}
    row = [
        ",".join(data.get("machine1", [])),
        ",".join(data.get("machine2", []))
    ]
    try:
        with sheet_lock:
            sheets.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=MANUAL_RANGE,
                valueInputOption="RAW",
                body={"values": [row]}
            ).execute()
        logger.info(f"Manual state written: {row}")
        global _manual_state_cache, _manual_state_ts
        _manual_state_cache = None
        _manual_state_ts    = 0
        socketio.emit("manualStateUpdated", data, broadcast=True)
        return jsonify({"status": "ok"}), 200

    except Exception:
        logger.exception("Error writing manual state")
        return jsonify({"error": "Server error"}), 500


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
