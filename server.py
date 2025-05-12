import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ─── Configuration ──────────────────────────────────────────────────────────────
SCOPES           = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID   = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE     = 'Production Orders!A:AC'
EMBROIDERY_RANGE = 'Embroidery List!A:ZZ'
PERSISTED_FILE   = 'persisted.json'   # where manualState is saved

app = Flask(__name__)
CORS(app)  # allow * from any origin on all routes

# ─── Helpers ────────────────────────────────────────────────────────────────────
def load_credentials():
    """Load credentials.json from the same directory as this file."""
    dirpath = os.path.dirname(os.path.abspath(__file__))
    cred_path = os.path.join(dirpath, 'credentials.json')
    info = json.load(open(cred_path, 'r'))
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

def fetch_from_sheets(range_name):
    """Fetch a range from the spreadsheet and return list-of-dicts."""
    creds   = load_credentials()
    service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
    sheet   = service.spreadsheets()
    resp    = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=range_name).execute()
    rows    = resp.get('values', [])
    if not rows:
        return []
    headers = rows[0]
    data    = []
    for row in rows[1:]:
        entry = {}
        for idx, h in enumerate(headers):
            entry[h] = row[idx] if idx < len(row) else ""
        data.append(entry)
    return data

def load_persisted():
    """Load manualState array from disk (or return empty list)."""
    if os.path.exists(PERSISTED_FILE):
        try:
            return json.load(open(PERSISTED_FILE, 'r'))
        except:
            pass
    return []

def save_persisted(state):
    """Save manualState array to disk."""
    with open(PERSISTED_FILE, 'w') as f:
        json.dump(state, f)

# ─── Routes ─────────────────────────────────────────────────────────────────────
@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        data = fetch_from_sheets(ORDERS_RANGE)
        return jsonify(data)
    except Exception as e:
        return jsonify({ 'error': str(e) }), 500

@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery_list():
    try:
        data = fetch_from_sheets(EMBROIDERY_RANGE)
        return jsonify(data)
    except Exception as e:
        return jsonify({ 'error': str(e) }), 500

@app.route('/api/manualState', methods=['GET', 'POST'])
def manual_state():
    if request.method == 'GET':
        return jsonify(load_persisted())
    else:
        state = request.get_json(force=True)
        save_persisted(state)
        return jsonify(state)

# ─── Run ────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=True)
