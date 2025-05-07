# File: server.py

import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS, cross_origin
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime

app = Flask(__name__)
# Enable CORS for all routes
CORS(app)

# —————————————————————————————————————————
# Configuration
# —————————————————————————————————————————
SCOPES           = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID   = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE     = 'Production Orders!A:AC'
EMBROIDERY_RANGE = 'Embroidery List!A:ZZ'

# Files for persisted state
PERSISTED_FILE      = 'persisted.json'
MANUAL_STATE_FILE   = 'manualState.json'

# —————————————————————————————————————————
# Google Sheets Authentication Helper
# —————————————————————————————————————————
def get_creds():
    key_json = os.environ['GOOGLE_SERVICE_ACCOUNT']
    info = json.loads(key_json)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

# —————————————————————————————————————————
# Helper: Fetch Production Orders
# —————————————————————————————————————————
def fetch_from_sheets():
    creds = get_creds()
    service = build('sheets', 'v4', credentials=creds)
    result = service.spreadsheets().values() \
        .get(spreadsheetId=SPREADSHEET_ID, range=ORDERS_RANGE) \
        .execute()
    rows = result.get('values', [])
    if not rows:
        return []

    headers = rows[0]
    def idx(name):
        return headers.index(name) if name in headers else -1

    # Identify relevant column indices
    i_id       = idx('Order #')
    i_sched    = idx('Schedule String')
    i_company  = idx('Company Name')
    i_design   = idx('Design')
    i_qty      = idx('Quantity')
    i_due      = idx('Due Date')
    i_due_type = idx('Due Type')
    i_sc       = idx('Stitch Count')

    orders = []
    for row in rows[1:]:
        if i_sched < 0 or i_sched >= len(row) or not row[i_sched].strip():
            continue
        try:
            oid = int(row[i_id])
        except:
            continue
        orders.append({
            'id':           oid,
            'title':        row[i_sched],
            'company':      row[i_company]  if 0 <= i_company  < len(row) else '',
            'design':       row[i_design]   if 0 <= i_design   < len(row) else '',
            'quantity':     int(row[i_qty]) if 0 <= i_qty      < len(row) and row[i_qty].isdigit() else 1,
            'due_date':     row[i_due]      if 0 <= i_due      < len(row) else '',
            'due_type':     row[i_due_type] if 0 <= i_due_type < len(row) else '',
            'stitch_count': int(row[i_sc])  if 0 <= i_sc       < len(row) and row[i_sc].isdigit() else 30000,
            'machineId':    None,
            'start_date':   '',
            'end_date':     '',
            'delivery':     ''
        })
    return orders

# —————————————————————————————————————————
# Helper: Fetch Embroidery List
# —————————————————————————————————————————
def fetch_embroidery_list():
    creds = get_creds()
    service = build('sheets', 'v4', credentials=creds)
    result = service.spreadsheets().values() \
        .get(spreadsheetId=SPREADSHEET_ID, range=EMBROIDERY_RANGE) \
        .execute()
    rows = result.get('values', [])
    if not rows:
        return []

    headers, *data_rows = rows
    return [
        { headers[i]: (row[i] if i < len(row) else '') for i in range(len(headers)) }
        for row in data_rows
    ]

# —————————————————————————————————————————
# Persisted Orders Helpers
# —————————————————————————————————————————
def load_persisted():
    if not os.path.exists(PERSISTED_FILE):
        return []
    try:
        with open(PERSISTED_FILE) as f:
            return json.load(f)
    except json.JSONDecodeError:
        return []

def save_persisted(data):
    with open(PERSISTED_FILE, 'w') as f:
        json.dump(data, f, indent=2)

# —————————————————————————————————————————
# Manual State Helpers
# —————————————————————————————————————————
def load_manual():
    if not os.path.exists(MANUAL_STATE_FILE):
        return {'state': {'machine1': [], 'machine2': []}, 'lastModified': None}
    try:
        with open(MANUAL_STATE_FILE) as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {'state': {'machine1': [], 'machine2': []}, 'lastModified': None}

def save_manual(state):
    payload = {
        'state': state,
        'lastModified': datetime.utcnow().isoformat() + 'Z'
    }
    with open(MANUAL_STATE_FILE, 'w') as f:
        json.dump(payload, f, indent=2)
    return payload

# —————————————————————————————————————————
# Routes: Orders & Embroidery
# —————————————————————————————————————————
@app.route('/api/orders', methods=['GET'])
@cross_origin()
def get_orders():
    orders = fetch_from_sheets()
    persisted = load_persisted()
    by_id = {o['id']: o for o in orders}
    for p in persisted:
        if p.get('id') in by_id:
            by_id[p['id']].update(p)
    return jsonify(list(by_id.values()))

@app.route('/api/embroideryList', methods=['GET'])
@cross_origin()
def get_embroidery_list():
    try:
        return jsonify(fetch_embroidery_list())
    except Exception:
        return jsonify({'error': 'Unable to load embroidery list'}), 500

# —————————————————————————————————————————
# Routes: Manual State
# —————————————————————————————————————————
@app.route('/api/manualState', methods=['GET'])
@cross_origin()
def get_manual_state():
    return jsonify(load_manual())

@app.route('/api/manualState', methods=['POST'])
@cross_origin()
def set_manual_state():
    state = request.get_json(force=True)
    result = save_manual(state)
    return jsonify(result)

# —————————————————————————————————————————
# Main
# —————————————————————————————————————————
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
