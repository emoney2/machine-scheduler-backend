# File: server.py
import os
import json
import base64
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
# Enable CORS for every route, every method, every origin
CORS(app)

# —————————————————————————————
# Configuration
# —————————————————————————————
SERVICE_ACCOUNT_ENV = 'SERVICE_ACCOUNT_B64'
SCOPES              = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID      = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE        = 'Production Orders!A:AC'
EMBROIDERY_RANGE    = 'Embroidery List!A:ZZ'
MANUAL_STATE_FILE   = 'manual_state.json'
PERSISTED_FILE      = 'persisted.json'  # for any future overlays


def get_credentials():
    b64 = os.getenv(SERVICE_ACCOUNT_ENV)
    if not b64:
        raise RuntimeError(f"{SERVICE_ACCOUNT_ENV} is not set")
    raw = base64.b64decode(b64)
    info = json.loads(raw)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def fetch_from_sheets():
    creds   = get_credentials()
    service = build('sheets', 'v4', credentials=creds)
    resp    = service.spreadsheets().values() \
        .get(spreadsheetId=SPREADSHEET_ID, range=ORDERS_RANGE) \
        .execute()
    rows = resp.get('values', [])
    if not rows:
        return []

    headers = rows[0]
    def idx_any(cands):
        for c in cands:
            if c in headers:
                return headers.index(c)
        return -1

    i_id       = idx_any(['Order #'])
    i_sched    = idx_any(['Schedule String'])
    i_company  = idx_any(['Company Name'])
    i_design   = idx_any(['Design'])
    i_qty      = idx_any(['Quantity'])
    i_due      = idx_any(['Due Date'])
    i_due_type = idx_any(['Hard Date/Soft Date', 'Due Type', 'Hard/Soft Date'])
    i_sc       = idx_any(['Stitch Count'])

    if i_id < 0 or i_sched < 0:
        raise RuntimeError("Missing required headers")

    orders = []
    for row in rows[1:]:
        if i_sched >= len(row) or not row[i_sched].strip():
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
            'embroidery_start': '',
        })
    return orders


def fetch_embroidery_list():
    creds   = get_credentials()
    service = build('sheets', 'v4', credentials=creds)
    resp    = service.spreadsheets().values() \
        .get(spreadsheetId=SPREADSHEET_ID, range=EMBROIDERY_RANGE) \
        .execute()
    rows = resp.get('values', [])
    if not rows:
        return []
    headers, *data = rows
    result = []
    for row in data:
        entry = { headers[i]: (row[i] if i < len(row) else '') for i in range(len(headers)) }
        result.append(entry)
    return result


# —————————————————————————————
# Manual State (persist across refreshes)
# —————————————————————————————
@app.route('/api/manualState', methods=['GET'])
def get_manual_state():
    if os.path.exists(MANUAL_STATE_FILE):
        try:
            with open(MANUAL_STATE_FILE) as f:
                return jsonify(json.load(f))
        except:
            pass
    # default
    return jsonify({'machine1': [], 'machine2': []})


@app.route('/api/manualState', methods=['POST'])
def post_manual_state():
    data = request.get_json(force=True)
    with open(MANUAL_STATE_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    return jsonify(success=True)


# —————————————————————————————
# Orders Endpoints
# —————————————————————————————
@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        live      = fetch_from_sheets()
        # overlay persisted local overrides if you need
        try:
            persisted = json.load(open(PERSISTED_FILE))
        except:
            persisted = []
        by_id = {o['id']: o for o in live}
        for p in persisted:
            oid = p.get('id')
            if oid in by_id:
                by_id[oid].update(p)
        return jsonify(list(by_id.values()))
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route('/api/orders/<int:order_id>', methods=['PUT'])
def update_order(order_id):
    """
    For saving e.g. embroidery_start from the UI back to the sheet (persist locally).
    """
    body = request.get_json(force=True)
    # merge into persisted.json
    try:
        allp = json.load(open(PERSISTED_FILE))
    except:
        allp = []
    # remove old entry
    allp = [p for p in allp if p.get('id') != order_id]
    entry = {'id': order_id, **body}
    allp.append(entry)
    with open(PERSISTED_FILE, 'w') as f:
        json.dump(allp, f, indent=2)
    return jsonify(success=True)


# —————————————————————————————
# Embroidery List Endpoint
# —————————————————————————————
@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery_list():
    try:
        rows = fetch_embroidery_list()
        return jsonify(rows)
    except Exception as e:
        return jsonify(error=str(e)), 500


# —————————————————————————————
# Main
# —————————————————————————————
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
