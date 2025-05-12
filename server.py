# File: server.py
import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app)

# ---- Configuration ----
SCOPES           = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID   = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE     = 'Production Orders!A:AC'
EMBROIDERY_RANGE = 'Embroidery List!A:ZZ'
MANUAL_STATE_FILE = 'manual_state.json'


def load_credentials():
    """Load service account from credentials.json in the same folder."""
    creds_path = os.path.join(os.path.dirname(__file__), 'credentials.json')
    return service_account.Credentials.from_service_account_file(
        creds_path, scopes=SCOPES
    )


def fetch_from_sheets():
    creds = load_credentials()
    service = build('sheets', 'v4', credentials=creds)
    data = service.spreadsheets().values() \
        .get(spreadsheetId=SPREADSHEET_ID, range=ORDERS_RANGE) \
        .execute().get('values', [])
    if not data or len(data) < 2:
        return []
    headers, rows = data[0], data[1:]
    return [
        { headers[i]: row[i] if i < len(row) else '' for i in range(len(headers)) }
        for row in rows
    ]


def fetch_embroidery_list():
    creds = load_credentials()
    service = build('sheets', 'v4', credentials=creds)
    data = service.spreadsheets().values() \
        .get(spreadsheetId=SPREADSHEET_ID, range=EMBROIDERY_RANGE) \
        .execute().get('values', [])
    if not data or len(data) < 2:
        return []
    headers, rows = data[0], data[1:]
    return [
        { headers[i]: row[i] if i < len(row) else '' for i in range(len(headers)) }
        for row in rows
    ]


@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        return jsonify(fetch_from_sheets())
    except Exception as e:
        return jsonify({ 'error': str(e) }), 500


@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery_list():
    try:
        return jsonify(fetch_embroidery_list())
    except Exception as e:
        return jsonify({ 'error': str(e) }), 500


@app.route('/api/manualState', methods=['GET', 'POST'])
def manual_state():
    if request.method == 'GET':
        if os.path.exists(MANUAL_STATE_FILE):
            with open(MANUAL_STATE_FILE) as f:
                data = json.load(f)
        else:
            data = []
        return jsonify(data)

    # POST
    try:
        payload = request.get_json(silent=True) or []
        with open(MANUAL_STATE_FILE, 'w') as f:
            json.dump(payload, f)
        return jsonify({ 'status': 'ok' })
    except Exception as e:
        return jsonify({ 'error': str(e) }), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=True)
