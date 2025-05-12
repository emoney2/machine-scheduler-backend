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
# Make sure these sheet names exactly match your tabs!
ORDERS_SPREADSHEET_ID     = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE              = 'Production Orders!A:AM'

EMBROIDERY_SPREADSHEET_ID = ORDERS_SPREADSHEET_ID
EMBROIDERY_RANGE          = 'Embroidery List!A:AM'

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
    header = values[0]
    rows   = values[1:]
    out = []
    for row in rows:
        # pad missing cells
        row += [''] * (len(header) - len(row))
        out.append(dict(zip(header, row)))
    return out

# ——— Endpoints ————————————————————————————————————————————

@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        data = fetch_sheet(ORDERS_SPREADSHEET_ID, ORDERS_RANGE)
        return jsonify(data)
    except HttpError as e:
        logger.error('Google Sheets API error on /api/orders', exc_info=e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery_list():
    try:
        data = fetch_sheet(EMBROIDERY_SPREADSHEET_ID, EMBROIDERY_RANGE)
        return jsonify(data)
    except HttpError as e:
        logger.error('Google Sheets API error on /api/embroideryList', exc_info=e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/manualState', methods=['GET','POST'])
def manual_state():
    if request.method == 'POST':
        state = request.get_json(force=True)
        with open(PERSISTED_FILE, 'w') as f:
            json.dump(state, f)
        return jsonify(state), 200
    else:
        with open(PERSISTED_FILE) as f:
            return jsonify(json.load(f)), 200

# ——— Run the server ———————————————————————————————————————
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    logger.info(f'Starting Flask on port {port}')
    app.run(host='0.0.0.0', port=port, debug=True)
