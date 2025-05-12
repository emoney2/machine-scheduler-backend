# File: backend/server.py

import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app)

# ——— Config ———
SCOPES           = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID   = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE     = 'Production Orders!A:AC'
EMBROIDERY_RANGE = 'Embroidery List!A:ZZ'
MANUAL_FILE      = 'manual_state.json'

def get_credentials():
    # 1) First, look for raw JSON in env var
    svc_json = os.getenv('GOOGLE_SERVICE_ACCOUNT')
    if svc_json:
        info = json.loads(svc_json)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    # 2) Fallback to credentials.json on disk (if you ever use that)
    path = os.path.join(os.path.dirname(__file__), 'credentials.json')
    if os.path.exists(path):
        return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    raise RuntimeError("No credentials: set GOOGLE_SERVICE_ACCOUNT or drop credentials.json")

def fetch_sheet(range_name):
    creds   = get_credentials()
    service = build('sheets', 'v4', credentials=creds)
    return service.spreadsheets() \
                  .values() \
                  .get(spreadsheetId=SPREADSHEET_ID, range=range_name) \
                  .execute() \
                  .get('values', [])

@app.route('/api/orders')
def api_orders():
    try:
        rows = fetch_sheet(ORDERS_RANGE)
        if not rows:
            return jsonify([])
        headers = rows[0]
        data    = [dict(zip(headers, row)) for row in rows[1:]]
        return jsonify(data)
    except Exception as e:
        return jsonify({ "error": str(e) }), 500

@app.route('/api/embroideryList')
def api_embroidery():
    try:
        rows = fetch_sheet(EMBROIDERY_RANGE)
        if not rows:
            return jsonify([])
        headers = rows[0]
        data    = [dict(zip(headers, row)) for row in rows[1:]]
        return jsonify(data)
    except Exception as e:
        return jsonify({ "error": str(e) }), 500

@app.route('/api/manualState', methods=['GET','POST'])
def api_manual_state():
    if request.method == 'POST':
        payload = request.get_json() or []
        with open(MANUAL_FILE, 'w') as f:
            json.dump(payload, f)
        return '', 200

    if os.path.exists(MANUAL_FILE):
        with open(MANUAL_FILE) as f:
            payload = json.load(f)
    else:
        payload = []
    return jsonify(payload)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=True)
