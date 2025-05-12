import os
import json
import logging
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Google Sheets Setup ─────────────────────────────────────────────────────
# Path to your service account JSON key (either env var or file in project root)
CREDENTIALS_FILE = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', 'credentials.json')
logger.info(f"Loading Google creds from file: {CREDENTIALS_FILE}")
creds = Credentials.from_service_account_file(
    CREDENTIALS_FILE,
    scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
)
sheets = build('sheets', 'v4', credentials=creds)

# ─── Spreadsheet IDs & Ranges ────────────────────────────────────────────────
ORDERS_SPREADSHEET_ID      = os.environ.get('ORDERS_SPREADSHEET_ID', 'YOUR_ORDERS_SPREADSHEET_ID')
ORDERS_RANGE              = os.environ.get('ORDERS_RANGE', 'Orders!A:ZZ')
EMBROIDERY_SPREADSHEET_ID = os.environ.get('EMBROIDERY_SPREADSHEET_ID', 'YOUR_EMBROIDERY_SPREADSHEET_ID')
EMBROIDERY_RANGE          = os.environ.get('EMBROIDERY_RANGE', 'Embroidery!A:ZZ')

# ─── Manual‐state persistence ────────────────────────────────────────────────
MANUAL_STATE_FILE = 'manual_state.json'

# ─── Flask App & CORS ────────────────────────────────────────────────────────
app = Flask(__name__)
# Allow any origin to hit any /api/* route
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ─── Helpers ─────────────────────────────────────────────────────────────────
def fetch_sheet(spreadsheet_id, range_name):
    return sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=range_name
    ).execute()

def parse_rows(rows):
    """Turn a list-of-lists into list-of-dicts using row0 as header."""
    header = rows[0]
    data = []
    for row in rows[1:]:
        entry = {}
        for i, key in enumerate(header):
            entry[key] = row[i] if i < len(row) else ''
        data.append(entry)
    return data

# ─── Endpoints ───────────────────────────────────────────────────────────────
@app.route('/api/orders')
def get_orders():
    try:
        result = fetch_sheet(ORDERS_SPREADSHEET_ID, ORDERS_RANGE)
        rows = result.get('values', [])
        return jsonify(parse_rows(rows)) if rows else jsonify([])
    except HttpError as e:
        logger.error("Google Sheets API error on /api/orders", exc_info=e)
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error("Error in /api/orders", exc_info=e)
        return jsonify({"error": str(e)}), 500

@app.route('/api/embroideryList')
def get_embroideries():
    try:
        result = fetch_sheet(EMBROIDERY_SPREADSHEET_ID, EMBROIDERY_RANGE)
        rows = result.get('values', [])
        return jsonify(parse_rows(rows)) if rows else jsonify([])
    except HttpError as e:
        logger.error("Google Sheets API error on /api/embroideryList", exc_info=e)
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error("Error in /api/embroideryList", exc_info=e)
        return jsonify({"error": str(e)}), 500

@app.route('/api/manualState', methods=['GET','POST'])
def manual_state():
    if request.method == 'POST':
        try:
            state = request.get_json(force=True) or {}
            with open(MANUAL_STATE_FILE, 'w') as f:
                json.dump(state, f)
            return jsonify(state)
        except Exception as e:
            logger.error("Error writing manual state", exc_info=e)
            return jsonify({"error": str(e)}), 500
    else:
        if os.path.exists(MANUAL_STATE_FILE):
            with open(MANUAL_STATE_FILE) as f:
                return jsonify(json.load(f))
        else:
            return jsonify({})

# ─── Run ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"Starting Flask on port {port}")
    app.run(host='0.0.0.0', port=port, debug=True)
