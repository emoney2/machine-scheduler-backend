import os
import json
import base64
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
SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE = 'Production Orders!A:AC'
EMBROIDERY_RANGE = 'Embroidery List!A:ZZ'
PERSISTED_FILE = 'persisted.json'
MANUAL_STATE_FILE = 'manual_state.json'

# —————————————————————————————
# Helper: Load service account creds from Base64 env var
# —————————————————————————————
def load_creds_dict():
    b64 = os.getenv('SERVICE_ACCOUNT_B64')
    if not b64:
        raise RuntimeError("Missing SERVICE_ACCOUNT_B64 environment variable")
    creds_json = base64.b64decode(b64).decode('utf-8')
    return json.loads(creds_json)

# —————————————————————————————
# Helper: Fetch Production Orders from Sheets
# —————————————————————————————
def fetch_from_sheets():
    creds_info = load_creds_dict()
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
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
    i_due_type = idx('Hard Date/Soft Date') if 'Hard Date/Soft Date' in headers else idx('Due Type')
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

# —————————————————————————————
# Helper: Fetch Embroidery List tab
# —————————————————————————————
def fetch_embroidery_list():
    creds_info = load_creds_dict()
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
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
        entry = {headers[i]: (row[i] if i < len(row) else '') for i in range(len(headers))}
        json_rows.append(entry)
    return json_rows

# —————————————————————————————
# Helpers: Persisted and Manual State
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

def load_manual_state():
    if not os.path.exists(MANUAL_STATE_FILE):
        return {}
    try:
        with open(MANUAL_STATE_FILE) as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}

def save_manual_state(data):
    with open(MANUAL_STATE_FILE, 'w') as f:
        json.dump(data, f, indent=2)

# —————————————————————————————
# Routes
# —————————————————————————————
@app.route('/api/orders', methods=['GET'])
@cross_origin()
def get_orders():
    try:
        live = fetch_from_sheets()
        # overlay persisted edits
        persisted = load_persisted()
        by_id = {o['id']: o for o in live}
        for p in persisted:
            oid = p.get('id')
            if oid in by_id:
                by_id[oid].update(p)
        return jsonify(list(by_id.values()))
    except Exception as e:
        app.logger.error('Error in /api/orders', exc_info=e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/embroideryList', methods=['GET'])
@cross_origin()
def get_embroidery_list():
    try:
        return jsonify(fetch_embroidery_list())
    except Exception as e:
        app.logger.error('Error in /api/embroideryList', exc_info=e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/manualState', methods=['GET', 'OPTIONS'])
@cross_origin()
def manual_state_get():
    return jsonify(load_manual_state())

@app.route('/api/manualState', methods=['POST'])
@cross_origin()
def manual_state_post():
    data = request.get_json(force=True)
    save_manual_state(data)
    return jsonify(success=True)

# —————————————————————————————
# Main
# —————————————————————————————
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
