import os
import logging
import time
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === CONFIGURATION ===
SPREADSHEET_ID   = "11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4"
ORDERS_RANGE     = "Production Orders!A:AM"
EMBROIDERY_RANGE = "Embroidery List!A:AM"
MANUAL_RANGE     = "Manual State!A2:B2"
CREDENTIALS_FILE = "credentials.json"

# === CACHING SETTINGS ===
CACHE_TTL     = 300  # seconds
_orders_cache = None
_orders_ts    = 0
_emb_cache    = None
_emb_ts       = 0

# === LINKS STORE (in-memory) ===
_links_store = {}

# Load Google credentials with full Sheets scope
logger.info(f"Loading Google credentials from {CREDENTIALS_FILE}")
creds = service_account.Credentials.from_service_account_file(
    CREDENTIALS_FILE,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
sheets = build("sheets", "v4", credentials=creds).spreadsheets()

# Initialize Flask
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

@app.after_request
def apply_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,OPTIONS"
    return response

def fetch_sheet(spreadsheet_id, sheet_range):
    """Fetch a sheet range and return a list of rows."""
    result = sheets.values().get(
        spreadsheetId=spreadsheet_id,
        range=sheet_range
    ).execute()
    return result.get("values", [])

# === ORDERS ENDPOINT WITH CACHING (no filter) ===
@app.route("/api/orders", methods=["GET"])
def get_orders():
    global _orders_cache, _orders_ts
    now = time.time()
    if _orders_cache is not None and (now - _orders_ts) < CACHE_TTL:
        return jsonify(_orders_cache)
    try:
        rows = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE)
        _orders_ts = now
        if not rows:
            _orders_cache = []
        else:
            headers = rows[0]
            data = [dict(zip(headers, row)) for row in rows[1:]]
            _orders_cache = data
    except Exception:
        logger.exception("Error fetching ORDERS_RANGE")
        if _orders_cache is not None:
            return jsonify(_orders_cache)
        _orders_cache = []
    return jsonify(_orders_cache)

# === EMBROIDERY LIST ENDPOINT WITH CACHING & FALLBACK ===
@app.route("/api/embroideryList", methods=["GET"])
def get_embroidery_list():
    global _emb_cache, _emb_ts
    now = time.time()
    if _emb_cache is not None and (now - _emb_ts) < CACHE_TTL:
        return jsonify(_emb_cache)
    try:
        rows = fetch_sheet(SPREADSHEET_ID, EMBROIDERY_RANGE)
        _emb_ts = now
        if not rows:
            _emb_cache = []
        else:
            headers = rows[0]
            data = [dict(zip(headers, row)) for row in rows[1:]]
            _emb_cache = data
    except Exception:
        logger.exception("Error fetching EMBROIDERY_RANGE")
        if _emb_cache is not None:
            return jsonify(_emb_cache)
        _emb_cache = []
    return jsonify(_emb_cache)

# === UPDATE ORDER (Embroidery Start) ===
@app.route("/api/orders/<order_id>", methods=["PUT"])
def update_order(order_id):
    try:
        data = request.get_json() or {}
        logger.info(f"Received update for order {order_id}: {data}")
        return jsonify({"status": "ok"}), 200
    except Exception:
        logger.exception("Error in PUT /api/orders/<order_id>")
        return jsonify({"error": "Server error"}), 500

# === LINKS ENDPOINTS ===
@app.route("/api/links", methods=["GET"])
def get_links():
    return jsonify(_links_store), 200

@app.route("/api/links", methods=["POST"])
def save_links():
    global _links_store
    _links_store = request.get_json() or {}
    logger.info(f"Links updated: {_links_store}")
    return jsonify({"status": "ok"}), 200

# === MANUAL STATE ENDPOINTS (sheet-backed) ===
@app.route("/api/manualState", methods=["GET"])
def get_manual_state():
    try:
        result = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=MANUAL_RANGE
        ).execute().get("values", [])
        row = result[0] if result else ["", ""]
        ms1 = [s for s in row[0].split(",") if s]
        ms2 = [s for s in row[1].split(",") if s]
        return jsonify({"machine1": ms1, "machine2": ms2}), 200
    except Exception:
        logger.exception("Error reading manual state from sheet")
        return jsonify({"machine1": [], "machine2": []}), 200

@app.route("/api/manualState", methods=["POST"])
def save_manual_state():
    try:
        data = request.get_json() or {"machine1": [], "machine2": []}
        row = [
            ",".join(data.get("machine1", [])),
            ",".join(data.get("machine2", []))
        ]
        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=MANUAL_RANGE,
            valueInputOption="RAW",
            body={"values": [row]}
        ).execute()
        logger.info(f"Manual state written to sheet: {row}")
        return jsonify({"status": "ok"}), 200
    except Exception:
        logger.exception("Error writing manual state to sheet")
        return jsonify({"error": "Server error"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting Flask server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
