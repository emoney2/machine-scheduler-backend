import os
import json
import logging
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ——— Logging —————————————————————————————————————————————
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ——— Spreadsheet configuration ————————————————————————————
ORDERS_SPREADSHEET_ID   = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
# NOTE: Make sure this sheet/tab name exactly matches yours, and use a valid A1 range
ORDERS_RANGE            = 'Production Orders!A1:AM1000'

EMBROIDERY_SPREADSHEET_ID = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
EMBROIDERY_RANGE          = 'Embroidery!A1:AM1000'

# ——— Google Sheets client setup —————————————————————————————
creds = service_account.Credentials.from_service_account_file(
    'credentials.json',
    scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
)
sheets = build('sheets', 'v4', credentials=creds)

# ——— Flask app + CORS —————————————————————————————————————
app = Flask(__name__)
CORS(app)

# ——— manualState persistence —————————————————————————————
PERSISTED_FILE = 'persisted.json'
if not os.path.exists(PERSISTED_FILE):
    with open(PERSISTED_FILE, 'w') as f:
        json.dump({'machine1': [], 'machine2': []}, f)

def fetch_sheet(spreadsheet_id, sheet_range):
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=sheet_range
    ).execute()
    values = resp.get('values', [])
    if not values:
        return []
    header = values.pop(0)
    return [dict(zip(header, row)) for row in values]

# ——— Endpoints ————————————————————————————————————————————

@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        rows = fetch_sheet(ORDERS_SPREADSHEET_ID, ORDERS_RANGE)
        return jsonify(rows)
    except HttpError as e:
        logger.error('Google Sheets API error on /api/orders', exc_info=e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery_list():
    try:
        rows = fetch_sheet(EMBROIDERY_SPREADSHEET_ID, EMBROIDERY_RANGE)
        return jsonify(rows)
    except HttpError as e:
        logger.error('Google Sheets API error on /api/embroideryList', exc_info=e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/manualState', methods=['GET','POST'])
def manual_state():
    if request.method == 'POST':
        state = request.get_json()
        with open(PERSISTED_FILE, 'w') as f:
            json.dump(state, f)
        return jsonify(state), 200
    else:
        with open(PERSISTED_FILE) as f:
            return jsonify(json.load(f)), 200

# ——— Run the server ———————————————————————————————————————
if __name__ == '__main__':
    logger.info('Starting Flask on port %s', os.environ.get('PORT','10000'))
    app.run(
      host='0.0.0.0',
      port=int(os.environ.get('PORT', '10000')),
      debug=True
    )
