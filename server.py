import os
import logging
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Google Sheets setup ────────────────────────────────────────────────────
# Path to your JSON key file (make sure this matches what you've mounted in Render)
CREDS_FILE = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', 'credentials.json')
logger.info(f"Loading Google creds from file: {CREDS_FILE}")

credentials = service_account.Credentials.from_service_account_file(
    CREDS_FILE,
    scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
)
service = build('sheets', 'v4', credentials=credentials)
sheets = service.spreadsheets()

# ─── Constants ──────────────────────────────────────────────────────────────
SPREADSHEET_ID    = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE      = 'Production Orders!A1:AM1000'
EMBROIDERY_RANGE  = 'Embroidery List!A1:Z1000'

# ─── Flask app ──────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)  # <-- enables Access-Control-Allow-Origin: *

def fetch_sheet(spreadsheet_id, sheet_range):
    return sheets.values() \
                 .get(spreadsheetId=spreadsheet_id, range=sheet_range) \
                 .execute() \
                 .get('values', [])

def rows_to_dicts(rows):
    if not rows or len(rows) < 2:
        return []
    headers   = rows[0]
    data_rows = rows[1:]
    out = []
    for row in data_rows:
        item = {}
        for i, h in enumerate(headers):
            item[h] = row[i] if i < len(row) else ''
        out.append(item)
    return out

@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        rows = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE)
        return jsonify(rows_to_dicts(rows))
    except HttpError as e:
        logger.error("Google Sheets API error on /api/orders", exc_info=e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery_list():
    try:
        rows = fetch_sheet(SPREADSHEET_ID, EMBROIDERY_RANGE)
        return jsonify(rows_to_dicts(rows))
    except HttpError as e:
        logger.error("Google Sheets API error on /api/embroideryList", exc_info=e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/manualState', methods=['GET','POST'])
def manual_state():
    # stubbed so frontend's POST /api/manualState works without 404
    if request.method == 'GET':
        return jsonify({})
    else:
        return jsonify({}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(debug=True, host='0.0.0.0', port=port)
