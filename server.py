# File: server.py

import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app)

# —————————————————————————————
# Configuration
# —————————————————————————————
SERVICE_ACCOUNT_FILE = 'credentials.json'
SCOPES               = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID       = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'

# Range for your Production Orders tab
ORDERS_RANGE         = 'Production Orders!A:AC'
# Range for your Embroidery List tab (extend through column ZZ)
EMBROIDERY_RANGE     = 'Embroidery List!A:ZZ'

PERSISTED_FILE       = 'persisted.json'


# —————————————————————————————
# Helper: Fetch Production Orders
# —————————————————————————————
def fetch_from_sheets():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    service = build('sheets', 'v4', credentials=creds)

    result = service.spreadsheets().values() \
        .get(spreadsheetId=SPREADSHEET_ID, range=ORDERS_RANGE) \
        .execute()
    rows = result.get('values', [])
    if not rows:
        return []

    headers = rows[0]
    def idx_any(cands):
        for c in cands:
            if c in headers:
                return headers.index(c)
        return -1

    # find column indices
    i_id       = idx_any(['Order #'])
    i_sched    = idx_any(['Schedule String'])
    i_company  = idx_any(['Company Name'])
    i_design   = idx_any(['Design'])
    i_qty      = idx_any(['Quantity'])
    i_due      = idx_any(['Due Date'])
    i_due_type = idx_any(['Hard Date/Soft Date','Due Type','Hard/Soft Date'])
    i_sc       = idx_any(['Stitch Count'])

    if i_id < 0 or i_sched < 0:
        raise RuntimeError(f"Missing Order # or Schedule String in headers: {headers!r}")

    orders = []
    for row in rows[1:]:
        if len(row) <= i_sched or not row[i_sched].strip():
            continue
        try:
            oid = int(row[i_id])
        except:
            continue

        orders.append({
            'id':           oid,
            'title':        row[i_sched],
            'company':      row[i_company]   if 0 <= i_company  < len(row) else '',
            'design':       row[i_design]    if 0 <= i_design   < len(row) else '',
            'quantity':     int(row[i_qty])  if 0 <= i_qty      < len(row) and row[i_qty].isdigit() else 1,
            'due_date':     row[i_due]       if 0 <= i_due      < len(row) else '',
            'due_type':     row[i_due_type]  if 0 <= i_due_type < len(row) else '',
            'stitch_count': int(row[i_sc])   if 0 <= i_sc       < len(row) and row[i_sc].isdigit() else 30000,
            'machineId':    None,
            'start_date':   '',
            'end_date':     '',
            'delivery':     ''
        })

    return orders


# —————————————————————————————
# Helper: Fetch Embroidery List tab
# —————————————————————————————
def fetch_embroidery_list():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
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
        entry = {}
        for idx, col in enumerate(headers):
            entry[col] = row[idx] if idx < len(row) else ''
        json_rows.append(entry)

    return json_rows


# —————————————————————————————
# Persisted state helpers
# —————————————————————————————
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


# —————————————————————————————
# Routes
# —————————————————————————————

@app.route('/api/orders', methods=['GET'])
def get_orders():
    # 1) live sheet orders
    live = fetch_from_sheets()
    # 2) overlay persisted changes
    persisted = load_persisted()
    by_id = {o['id']: o for o in live}
    for p in persisted:
        oid = p.get('id')
        if oid in by_id:
            by_id[oid].update(p)
    return jsonify(live)


@app.route('/api/orders/<int:order_id>', methods=['PUT'])
def update_order(order_id):
    data = request.get_json(force=True)
    # remove old entry
    persisted = [p for p in load_persisted() if p.get('id') != order_id]
    # add updated values
    entry = {
        'id':         order_id,
        'machineId':  data.get('machineId'),
        'start_date': data.get('start_date',''),
        'end_date':   data.get('end_date',''),
        'delivery':   data.get('delivery',''),
        'position':   data.get('position')
    }
    persisted.append(entry)
    save_persisted(persisted)
    return jsonify(success=True)


@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery_list():
    """
    Returns the raw rows of your 'Embroidery List' tab,
    as JSON objects keyed by the header row.
    """
    try:
        rows = fetch_embroidery_list()
        return jsonify(rows)
    except Exception as e:
        app.logger.error('Error fetching Embroidery List tab', exc_info=e)
        return jsonify({ 'error': 'Unable to load embroidery list' }), 500


# —————————————————————————————
# Main
# —————————————————————————————
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
