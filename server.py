# File: server.py

import os
import json
import base64
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app)

# —————————————————————————————
# Configuration & Credentials
# —————————————————————————————

SPREADSHEET_ID = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE   = 'Production Orders!A:AC'
EMBROIDERY_RANGE = 'Embroidery List!A:ZZ'
PERSISTED_FILE = 'persisted.json'

# Load service account creds from B64 env var if set, otherwise from file
if os.getenv('SERVICE_ACCOUNT_B64'):
    creds_json = base64.b64decode(os.getenv('SERVICE_ACCOUNT_B64'))
    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
    )
else:
    creds = service_account.Credentials.from_service_account_file(
        'credentials.json',
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
    )

# —————————————————————————————
# Helpers
# —————————————————————————————

def fetch_from_sheets():
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

    # find the indices we need
    i_id       = idx('Order #')
    i_sched    = idx('Schedule String')
    i_company  = idx('Company Name')
    i_design   = idx('Design')
    i_qty      = idx('Quantity')
    i_due      = idx('Due Date')
    i_due_type = idx('Hard Date/Soft Date') or idx('Due Type')
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

def fetch_embroidery_list():
    service = build('sheets', 'v4', credentials=creds)
    result = service.spreadsheets().values() \
        .get(spreadsheetId=SPREADSHEET_ID, range=EMBROIDERY_RANGE) \
        .execute()
    rows = result.get('values', [])
    if not rows:
        return []
    headers, *data_rows = rows
    json_rows = []
    for row in data_rows:
        entry = { headers[i]: (row[i] if i < len(row) else '') for i in range(len(headers)) }
        json_rows.append(entry)
    return json_rows

def load_persisted():
    if not os.path.exists(PERSISTED_FILE):
        return []
    try:
        data = json.load(open(PERSISTED_FILE))
        # only keep dicts with an 'id'
        return [item for item in data if isinstance(item, dict) and 'id' in item]
    except:
        return []

def save_persisted(data):
    with open(PERSISTED_FILE, 'w') as f:
        json.dump(data, f, indent=2)

# —————————————————————————————
# Routes
# —————————————————————————————

@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        live = fetch_from_sheets()
        persisted = load_persisted()
        by_id = { o['id']: o for o in live }
        for p in persisted:
            oid = p.get('id')
            if oid in by_id:
                by_id[oid].update(p)
        return jsonify(list(by_id.values()))
    except Exception as e:
        app.logger.exception(e)
        return jsonify({ 'error': 'Unable to load orders' }), 500

@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery_list():
    try:
        rows = fetch_embroidery_list()
        return jsonify(rows)
    except Exception as e:
        app.logger.exception(e)
        return jsonify({ 'error': 'Unable to load embroidery list' }), 500

@app.route('/api/manualState', methods=['GET'])
def get_manual_state():
    return jsonify(load_persisted())

@app.route('/api/manualState', methods=['POST'])
def post_manual_state():
    data = request.get_json(force=True)
    if isinstance(data, list):
        filtered = [item for item in data if isinstance(item, dict) and 'id' in item]
        save_persisted(filtered)
        return jsonify({ 'success': True })
    return jsonify({ 'error': 'Invalid payload' }), 400

@app.route('/api/orders/<int:order_id>', methods=['PUT'])
def update_order(order_id):
    data = request.get_json(force=True) or {}
    persisted = load_persisted()
    # remove any existing entry for this order_id
    persisted = [p for p in persisted if p.get('id') != order_id]
    # add our new data
    entry = { 'id': order_id }
    entry.update(data)
    persisted.append(entry)
    save_persisted(persisted)
    return jsonify({ 'success': True })

# —————————————————————————————
# Main
# —————————————————————————————
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
