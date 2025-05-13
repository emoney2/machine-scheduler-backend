#!/usr/bin/env python3
import os, json, logging
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ——— Configuration —————————————————————————————————————
SPREADSHEET_ID      = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE        = 'Production Orders!A1:AM1000'
EMBROIDERY_RANGE    = 'Embroidery List!A1:AM1000'
PERSIST_FILE        = 'persisted.json'
CREDS_FILE          = 'credentials.json'
PORT                = int(os.environ.get('PORT', 10000))

# ——— Logging —————————————————————————————————————————————
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ——— Flask + CORS ———————————————————————————————————————
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

@app.after_request
def _apply_cors(response):
    response.headers.setdefault('Access-Control-Allow-Origin', '*')
    response.headers.setdefault('Access-Control-Allow-Methods', 'GET,POST,OPTIONS,PUT,DELETE')
    response.headers.setdefault('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    return response

# ——— Google Sheets client —————————————————————————————
logger.info(f'Loading Google creds from file: {CREDS_FILE}')
creds = service_account.Credentials.from_service_account_file(
    CREDS_FILE,
    scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
)
sheets = build('sheets', 'v4', credentials=creds).spreadsheets()

def fetch_sheet(spreadsheet_id, sheet_range):
    return sheets.values().get(spreadsheetId=spreadsheet_id, range=sheet_range).execute().get('values', [])

# ——— /api/orders ———————————————————————————————————————
@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        rows = fetch_sheet(SPREADSHEET_ID, ORDERS_RANGE)
        if not rows:
            return jsonify([])
        headers = rows[0]
        data = []
        for row in rows[1:]:
            obj = { headers[i]: row[i] if i < len(row) else '' for i in range(len(headers)) }
            data.append(obj)
        return jsonify(data)
    except Exception as e:
        logger.exception('Error in /api/orders')
        return jsonify({'error': str(e)}), 500

# ——— /api/embroideryList —————————————————————————————————
@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery_list():
    try:
        rows = fetch_sheet(SPREADSHEET_ID, EMBROIDERY_RANGE)
        if not rows:
            return jsonify([])
        headers = rows[0]
        data = []
        for row in rows[1:]:
            obj = { headers[i]: row[i] if i < len(row) else '' for i in range(len(headers)) }
            data.append(obj)
        return jsonify(data)
    except Exception as e:
        logger.exception('Error in /api/embroideryList')
        return jsonify({'error': str(e)}), 500

# ——— Manual-state persistence ————————————————————————————
def load_manual_state():
    try:
        return json.load(open(PERSIST_FILE))
    except:
        return {'machine1': [], 'machine2': []}

def save_manual_state(state):
    json.dump(state, open(PERSIST_FILE, 'w'))

@app.route('/api/manualState', methods=['GET'])
def get_manual_state():
    return jsonify(load_manual_state())

@app.route('/api/manualState', methods=['POST'])
def post_manual_state():
    try:
        state = request.get_json()
        save_manual_state(state)
        return jsonify(state)
    except Exception as e:
        logger.exception('Error in /api/manualState')
        return jsonify({'error': str(e)}), 500

# ——— Run server ————————————————————————————————————————
if __name__ == '__main__':
    logger.info(f'Starting Flask on port {PORT}')
    app.run(host='0.0.0.0', port=PORT, debug=True)
