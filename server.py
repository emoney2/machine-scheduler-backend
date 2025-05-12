# File: backend/server.py
import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
# Allow any origin to hit any /api/* route
CORS(app, resources={r"/api/*": {"origins": "*"}})

# These env-vars must be set in Render (or your host):
#   SPREADSHEET_ID:   your Google Sheets ID
#   ORDERS_RANGE:     e.g. "Orders!A:F"
#   EMB_RANGE:        e.g. "Embroidery!A:B"
SPREADSHEET_ID = os.environ['SPREADSHEET_ID']
ORDERS_RANGE   = os.environ.get('ORDERS_RANGE',   'Orders!A:F')
EMB_RANGE      = os.environ.get('EMB_RANGE',      'Embroidery!A:B')

# Where we store the manualState payload
PERSISTED_PATH = os.path.join(os.path.dirname(__file__), 'persisted.json')

def load_credentials():
    # 1) First try reading a file mounted at backend/credentials.json
    cred_path = os.path.join(os.path.dirname(__file__), 'credentials.json')
    if os.path.exists(cred_path):
        with open(cred_path, 'r') as f:
            info = json.load(f)
    else:
        # 2) Fallback: look for a base64 string in SERVICE_ACCOUNT_B64
        b64 = os.environ.get('SERVICE_ACCOUNT_B64')
        if not b64:
            raise RuntimeError("Missing credentials.json and SERVICE_ACCOUNT_B64")
        info = json.loads(__import__('base64').b64decode(b64))
    return service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )

def get_sheets_service():
    creds = load_credentials()
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)

def fetch_from_sheets():
    svc = get_sheets_service()
    data = svc.spreadsheets().values() \
        .get(spreadsheetId=SPREADSHEET_ID, range=ORDERS_RANGE) \
        .execute()
    rows = data.get('values', [])
    if not rows:
        return []
    headers = rows[0]
    result = []
    for row in rows[1:]:
        item = {headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))}
        result.append(item)
    return result

def fetch_embroidery_list():
    svc = get_sheets_service()
    data = svc.spreadsheets().values() \
        .get(spreadsheetId=SPREADSHEET_ID, range=EMB_RANGE) \
        .execute()
    rows = data.get('values', [])
    if not rows:
        return []
    headers = rows[0]
    result = []
    for row in rows[1:]:
        item = {headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))}
        result.append(item)
    return result

@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        return jsonify(fetch_from_sheets())
    except Exception as e:
        return jsonify({ "error": str(e) }), 500

@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery_list():
    try:
        return jsonify(fetch_embroidery_list())
    except Exception as e:
        return jsonify({ "error": str(e) }), 500

@app.route('/api/manualState', methods=['GET','POST'])
def manual_state():
    if request.method == 'POST':
        payload = request.get_json(force=True)
        with open(PERSISTED_PATH, 'w') as f:
            json.dump(payload, f)
        return '', 200
    else:
        if os.path.exists(PERSISTED_PATH):
            with open(PERSISTED_PATH) as f:
                return jsonify(json.load(f))
        else:
            return jsonify([])

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=True)
