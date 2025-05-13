# File: server.py

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
    scopes=["https://www.googleapis.com/auth/spreadsheets"],
)
sheets = build("sheets", "v4", credentials=creds).spreadsheets()

# Initialize Flask
app = Flask(__name__)
CORS(app)  # Allow all origins and methods (GET/POST/PUT/etc)

def fetch_sheet(spreadsheet_id, sheet_range):
    result = sheets.values().get(
        spreadsheetId=spreadsheet_id,
        range=sheet_range
    ).execute()
    return result.get("values", [])

def write_sheet(spreadsheet_id, write_range, values):
    body = {"values": values}
    return sheets.values().update(
        spreadsheetId=spreadsheet_id,
        range=write_range,
        valueInputOption="RAW",
        body=body
    ).execute()

@app.route("/api/orders", methods=["GET"])
def get_orders():
    try:
        rows = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE)
        if not rows:
            return jsonify([])
        headers = rows[0]
        data = [dict(zip(headers, row)) for row in rows[1:]]
        data = [r for r in data if r.get("Order #")]
        return jsonify(data)
    except Exception:
        logger.exception("Error in GET /api/orders")
        return jsonify({"error": "Failed to fetch orders"}), 500

@app.route("/api/embroideryList", methods=["GET"])
def get_embroidery_list():
    try:
        rows = fetch_sheet(SPREADSHEET_ID, EMBROIDERY_RANGE)
        if not rows:
            return jsonify([])
        headers = rows[0]
        data = [dict(zip(headers, row)) for row in rows[1:]]
        data = [r for r in data if r.get("Company Name")]
        return jsonify(data)
    except Exception:
        logger.exception("Error in GET /api/embroideryList")
        return jsonify({"error": "Failed to fetch embroidery list"}), 500

@app.route("/api/orders/<order_id>", methods=["PUT"])
def update_order(order_id):
    """
    Expects JSON body with {"Stage": "...", "Start Date": "...", "End Date": "...", "Delivery": "..."}
    Finds the row matching Order # == order_id and writes those columns back.
    """
    try:
        payload = request.get_json()
        # First fetch all rows
        rows = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE)
        headers = rows[0]
        # find the row index
        for idx, row in enumerate(rows[1:], start=2):  # sheet rows are 1-based, +1 for header
            if len(row) > headers.index("Order #") and row[headers.index("Order #")] == order_id:
                # build the single-row update
                update_vals = []
                for col in ["Stage", "Embroidery Start Time", "Embroidery End Time", "In Hand Date"]:
                    if col in payload:
                        val = payload[col]
                        # find col index
                        cidx = headers.index(col)
                        update_vals.append((cidx, val))
                # prepare A:Z row array
                full_row = [""] * len(headers)
                for cidx, val in update_vals:
                    full_row[cidx] = val
                # write back that one row
                write_range = f"Production Orders!A{idx}:{chr(ord('A')+len(headers)-1)}{idx}"
                write_sheet(SPREADSHEET_ID, write_range, [full_row])
                return jsonify({"status": "ok"})
        return jsonify({"error": "order not found"}), 404
    except Exception:
        logger.exception(f"Error in PUT /api/orders/{order_id}")
        return jsonify({"error": "Failed to update order"}), 500

@app.route("/api/manualState", methods=["POST"])
def manual_state():
    """
    Expects JSON body [{ id: "order# or ph-xxx", stage: "..." , etc }, ...]
    For each entry, updates its Stage (or placeholder markers) in the sheet.
    """
    try:
        changes = request.get_json()
        # fetch current sheet to locate rows
        rows = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE)
        headers = rows[0]
        header_to_index = {h: i for i, h in enumerate(headers)}
        # build a map from Order # to row index
        id_to_row = {
            row[header_to_index["Order #"]]: idx+2
            for idx, row in enumerate(rows[1:])
            if len(row) > header_to_index["Order #"]
        }
        # apply each change
        for item in changes:
            oid = item["id"]
            if oid.startswith("ph-"):
                continue  # skip placeholders here
            row_num = id_to_row.get(oid)
            if not row_num:
                continue
            # only updating Stage here
            new_stage = item.get("stage")
            if new_stage:
                col_idx = header_to_index["Stage"]
                write_range = f"Production Orders!{chr(ord('A')+col_idx)}{row_num}"
                write_sheet(SPREADSHEET_ID, write_range, [[new_stage]])
        return jsonify({"status": "ok"})
    except Exception:
        logger.exception("Error in POST /api/manualState")
        return jsonify({"error": "Failed to apply manual state"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting Flask server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
