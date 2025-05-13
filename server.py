import os
import logging
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === CONFIGURATION ===
SPREADSHEET_ID     = "11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4"
ORDERS_RANGE       = "Production Orders!A:AM"
EMBROIDERY_RANGE   = "Embroidery List!A:AM"
CREDENTIALS_FILE   = "credentials.json"   # must be present alongside this file

# Load service account credentials and build Sheets API client
logger.info(f"Loading Google credentials from {CREDENTIALS_FILE}")
creds = service_account.Credentials.from_service_account_file(
    CREDENTIALS_FILE,
    scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
)
sheets = build("sheets", "v4", credentials=creds).spreadsheets()

# Initialize Flask
app = Flask(__name__)
CORS(app)  # Allow all origins; you can lock this down if desired

def fetch_sheet(spreadsheet_id, sheet_range):
    """Fetch a sheet range and return a list of rows (each row is a list of cells)."""
    result = sheets.values().get(
        spreadsheetId=spreadsheet_id,
        range=sheet_range
    ).execute()
    return result.get("values", [])

# === ORDERS ENDPOINT ===
@app.route("/api/orders")
def get_orders():
    try:
        rows = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE)
        if not rows:
            return jsonify([])

        # First row is header
        headers = rows[0]
        data = [dict(zip(headers, row)) for row in rows[1:]]
        # Filter out any blank entries (no Order #)
        data = [r for r in data if r.get("Order #")]
        return jsonify(data)
    except Exception as e:
        logger.error("Error in /api/orders", exc_info=True)
        return jsonify({"error": str(e)}), 500

# === EMBROIDERY LIST ENDPOINT ===
@app.route("/api/embroideryList")
def get_embroidery_list():
    try:
        rows = fetch_sheet(SPREADSHEET_ID, EMBROIDERY_RANGE)
        if not rows:
            return jsonify([])

        headers = rows[0]
        data = [dict(zip(headers, row)) for row in rows[1:]]
        # Filter out any blank entries (no Company Name)
        data = [r for r in data if r.get("Company Name")]
        return jsonify(data)
    except Exception as e:
        logger.error("Error in /api/embroideryList", exc_info=True)
        return jsonify({"error": str(e)}), 500

# === UPDATE ORDER (Embroidery Start) ===
@app.route("/api/orders/<order_id>", methods=["PUT"])
def update_order(order_id):
    try:
        data = request.get_json() or {}
        # In a full implementation, update the sheet here.
        logger.info(f"Received update for order {order_id}: {data}")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error("Error in PUT /api/orders/<id>", exc_info=True)
        return jsonify({"error": str(e)}), 500

# === SAVE MANUAL STATE ===
@app.route("/api/manualState", methods=["POST"])
def save_manual_state():
    try:
        state = request.get_json() or {}
        logger.info(f"Received manualState: {state}")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error("Error in POST /api/manualState", exc_info=True)
        return jsonify({"error": str(e)}), 500

# === SAVE LINKS ===
@app.route("/api/links", methods=["POST"])
def save_links():
    try:
        links = request.get_json() or {}
        logger.info(f"Received links: {links}")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error("Error in POST /api/links", exc_info=True)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting Flask server on port {port}")
    # Debug=True only in development
    app.run(host="0.0.0.0", port=port, debug=True)
