# File: backend/server.py

import os
import json
import base64
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app)  # enable CORS on all routes

# ——— Configuration ———
SCOPES           = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID   = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE     = 'Production Orders!A:AC'
EMBROIDERY_RANGE = 'Embroidery List!A:ZZ'
PERSISTED_FILE   = 'manual_state.json'


def get_credentials():
    """Load creds from credentials.json on disk, else from SERVICE_ACCOUNT_B64."""
    json_path = os.path.join(os.path.dirname(__file__), 'credentials.json')
    if os.path.exists(json_path):
        return service_account.Credentials.from_service_account_file(
          json_path, scopes=SCOPES
        )
    b64 = os.getenv('SERVICE_ACCOUNT_B64')
    if not b64:
        raise RuntimeError("No credentials.json and no SERVICE_ACCOUNT_B64 set")
    info = json.loads(base64.b64decode(b64))
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def fetch_sheet(range_name):
    creds   = get_credentials()
    service = build('sheets', 'v4', credentials=creds)
    sheet   = service.spreadsheets().values() \
                       .get(spreadsheetId=SPREADSHEET_ID, range=range_name) \
                       .execute()
    return sheet.get('values', [])


@app.route('/api/orders')
def get_orders():
    try:
        rows = fetch_sheet(ORDERS_RANGE)
        if not rows: 
            return jsonify([])

        headers = rows[0]
        payload = [dict(zip(headers, row)) for row in rows[1:]]
        return jsonify(payload)
    except Exception as e:
        return jsonify({ 'error': str(e) }), 500


@app.route('/api/embroideryList')
def get_embroidery_list():
    try:
        rows = fetch_sheet(EMBROIDERY_RANGE)
        if not rows:
            return jsonify([])

        headers = rows[0]
        payload = [dict(zip(headers, row)) for row in rows[1:]]
        return jsonify(payload)
    except Exception as e:
        return jsonify({ 'error': str(e) }), 500


@app.route('/api/manualState', methods=['GET', 'POST'])
def manual_state():
    # POST → overwrite the persisted JSON
    if request.method == 'POST':
        data = request.get_json() or []
        with open(PERSISTED_FILE, 'w') as f:
            json.dump(data, f)
        return '', 200

    # GET → read it (or return empty list)
    if os.path.exists(PERSISTED_FILE):
        with open(PERSISTED_FILE) as f:
            data = json.load(f)
    else:
        data = []
    return jsonify(data)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=True)
