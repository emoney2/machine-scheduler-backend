# File: server.py

import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS, cross_origin
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
# Enable CORS on all routes
CORS(app)

# —————————————————————————————
# Configuration
# —————————————————————————————
SERVICE_ACCOUNT_FILE = 'credentials.json'
SCOPES               = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID       = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'

# Ranges in your sheet
ORDERS_RANGE         = 'Production Orders!A:AC'
EMBROIDERY_RANGE     = 'Embroidery List!A:ZZ'

# Optional persisted file (if you need to overlay local updates)
PERSISTED_FILE       = 'persisted.json'


# —————————————————————————————
# Helpers
# —————————————————————————————
def fetch_from_sheets():
    """Fetch and parse the 'Production Orders' tab."""
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
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


def fetch_embroidery_list():
    """Fetch and return the 'Embroidery List' tab as an array of objects."""
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
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


# —————————————————————————————
# (Optional) Persisted state helpers
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
@cross_origin()
def get_orders():
    """Return the parsed orders list."""
    orders = fetch_from_sheets()

    # Example: overlay persisted updates if you need it
    persisted = load_persisted()
    by_id = {o['id']: o for o in orders}
    for p in persisted:
        oid = p.get('id')
        if oid in by_id:
            by_id[oid].update(p)

    return jsonify(list(by_id.values()))


@app.route('/api/embroideryList', methods=['GET'])
@cross_origin()
def get_embroidery_list():
    """Return the raw embroidery list rows as JSON."""
    try:
        rows = fetch_embroidery_list()
        return jsonify(rows)
    except Exception as e:
        app.logger.error('Error fetching Embroidery List', exc_info=e)
        return jsonify({'error': 'Unable to load embroidery list'}), 500


# —————————————————————————————
# Main
# —————————————————————————————
if __name__ == '__main__':
    # In production, Render will set the port via the PORT env var if needed
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
