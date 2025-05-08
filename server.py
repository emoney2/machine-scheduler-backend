# File: server.py

import os
import json
import base64
from flask import Flask, jsonify, request
from flask_cors import CORS, cross_origin
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app)  # allow CORS on all routes

# —————————————————————————————
# Configuration
# —————————————————————————————
SCOPES           = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID   = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE     = 'Production Orders!A:AC'
EMBROIDERY_RANGE = 'Embroidery List!A:ZZ'
PERSISTED_FILE   = 'persisted.json'


# —————————————————————————————
# Internal: build credentials from SERVICE_ACCOUNT_B64
# —————————————————————————————
def get_sheet_creds():
    b64 = os.getenv('SERVICE_ACCOUNT_B64', '')
    if not b64:
        raise RuntimeError('SERVICE_ACCOUNT_B64 not set')
    info = json.loads(base64.b64decode(b64))
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


# —————————————————————————————
# Helper: Fetch Production Orders
# —————————————————————————————
def fetch_from_sheets():
    creds = get_sheet_creds()
    service = build('sheets', 'v4', credentials=creds)
    result = service.spreadsheets().values() \
        .get(spreadsheetId=SPREADSHEET_ID, range=ORDERS_RANGE) \
        .execute()
    rows = result.get('values', [])
    if not rows:
        return []

    headers = rows[0]
    def idx_any(candidates):
        for name in candidates:
            if name in headers:
                return headers.index(name)
        return -1

    i_id       = idx_any(['Order #'])
    i_sched    = idx_any(['Schedule String'])
    i_company  = idx_any(['Company Name'])
    i_design   = idx_any(['Design'])
    i_qty      = idx_any(['Quantity'])
    i_due      = idx_any(['Due Date'])
    i_due_type = idx_any(['Hard Date/Soft Date', 'Due Type', 'Hard Date', 'Soft Date'])
    i_sc       = idx_any(['Stitch Count'])

    orders = []
    for row in rows[1:]:
        if i_sched < 0 or i_sched >= len(row) or not row[i_sched].strip():
            continue
        try:
            oid = int(row[i_id])
        except:
            continue

        due_type = ''
        if 0 <= i_due_type < len(row):
            due_type = row[i_due_type].strip()

        orders.append({
            'id':           oid,
            'title':        row[i_sched],
            'company':      row[i_company]   if 0 <= i_company   < len(row) else '',
            'design':       row[i_design]    if 0 <= i_design    < len(row) else '',
            'quantity':     int(row[i_qty])  if 0 <= i_qty       < len(row) and row[i_qty].isdigit() else 1,
            'due_date':     row[i_due]       if 0 <= i_due       < len(row) else '',
            'due_type':     due_type,
            'stitch_count': int(row[i_sc])   if 0 <= i_sc        < len(row) and row[i_sc].isdigit() else 30000,
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
    creds = get_sheet_creds()
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
@cross_origin()
def get_orders():
    live = fetch_from_sheets()
    persisted = load_persisted()
    by_id = {o['id']: o for o in live}
    for p in persisted:
        oid = p.get('id')
        if oid in by_id:
            by_id[oid].update(p)
    return jsonify(list(by_id.values()))


@app.route('/api/embroideryList', methods=['GET'])
@cross_origin()
def get_embroidery_list():
    try:
        rows = fetch_embroidery_list()
        return jsonify(rows)
    except Exception as e:
        app.logger.error('Error fetching Embroidery List', exc_info=e)
        return jsonify({'error': 'Unable to load embroidery list'}), 500


@app.route('/api/manualState', methods=['GET'])
@cross_origin()
def get_manual_state():
    return jsonify(load_persisted())


@app.route('/api/manualState', methods=['POST'])
@cross_origin()
def post_manual_state():
    data = request.get_json(force=True)
    save_persisted(data)
    return jsonify(success=True)


# —————————————————————————————
# Always add CORS headers
# —————————————————————————————
@app.after_request
def apply_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    return response


# —————————————————————————————
# Global error handler to preserve CORS on exceptions
# —————————————————————————————
@app.errorhandler(Exception)
def handle_all_errors(e):
    code = getattr(e, "code", 500)
    payload = {"error": str(e)}
    resp = jsonify(payload)
    resp.status_code = code
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    return resp


# —————————————————————————————
# Main
# —————————————————————————————
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
