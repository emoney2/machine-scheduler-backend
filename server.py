# File: server.py
import os
import json
import base64
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
# allow CORS on all /api/* endpoints
CORS(app, resources={r"/api/*": {"origins": "*"}})

# —————————————————————————————
# Configuration
# —————————————————————————————
SCOPES           = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID   = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE     = 'Production Orders!A:AC'
EMBROIDERY_RANGE = 'Embroidery List!A:ZZ'

PERSISTED_FILE     = 'persisted.json'
MANUAL_STATE_FILE  = 'manual_state.json'

# —————————————————————————————
# Auth helper
# —————————————————————————————
def get_credentials():
    b64 = os.getenv('SERVICE_ACCOUNT_B64')
    if b64:
        data = base64.b64decode(b64)
        info = json.loads(data)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    # fallback
    return service_account.Credentials.from_service_account_file('credentials.json', scopes=SCOPES)

# —————————————————————————————
# Sheet fetchers
# —————————————————————————————
def fetch_from_sheets():
    creds   = get_credentials()
    service = build('sheets', 'v4', credentials=creds)
    result  = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=ORDERS_RANGE
    ).execute()
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
    i_due_type = idx('Hard Date/Soft Date') or idx('Due Type') or idx('Hard/Soft Date')
    i_sc       = idx('Stitch Count')

    orders = []
    for row in rows[1:]:
        if i_sched<0 or i_sched>=len(row) or not row[i_sched].strip():
            continue
        try:
            oid = int(row[i_id])
        except:
            continue
        orders.append({
            'id':           oid,
            'title':        row[i_sched],
            'company':      row[i_company]   if 0<=i_company< len(row) else '',
            'design':       row[i_design]    if 0<=i_design< len(row) else '',
            'quantity':     int(row[i_qty])  if 0<=i_qty< len(row) and row[i_qty].isdigit() else 1,
            'due_date':     row[i_due]       if 0<=i_due< len(row) else '',
            'due_type':     row[i_due_type]  if 0<=i_due_type< len(row) else '',
            'stitch_count': int(row[i_sc])   if 0<=i_sc< len(row) and row[i_sc].isdigit() else 30000,
            'machineId':    None,
            'start_date':   '',
            'end_date':     '',
            'delivery':     ''
        })
    return orders

def fetch_embroidery_list():
    creds   = get_credentials()
    service = build('sheets', 'v4', credentials=creds)
    result  = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=EMBROIDERY_RANGE
    ).execute()
    rows = result.get('values', [])
    if not rows:
        return []

    headers, *data_rows = rows
    json_rows = []
    for row in data_rows:
        entry = { headers[i]: row[i] if i < len(row) else '' for i in range(len(headers)) }
        json_rows.append(entry)
    return json_rows

# —————————————————————————————
# Persistence
# —————————————————————————————
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return default

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def load_persisted():
    return load_json(PERSISTED_FILE, [])

def save_persisted(d):
    save_json(PERSISTED_FILE, d)

def load_manual():
    return load_json(MANUAL_STATE_FILE, {"machine1": [], "machine2": []})

def save_manual(d):
    save_json(MANUAL_STATE_FILE, d)

# —————————————————————————————
# API Endpoints
# —————————————————————————————

@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        # live from Sheets
        orders = fetch_from_sheets()
        # overlay persisted field-edits
        persisted = load_persisted()
        by_id = {o['id']: o for o in orders}
        for p in persisted:
            oid = p.get('id')
            if oid in by_id:
                by_id[oid].update(p)

        # overlay manual placement
        manual = load_manual()
        for col in ('machine1','machine2'):
            for oid in manual.get(col, []):
                if oid in by_id:
                    by_id[oid]['machineId'] = col

        return jsonify(list(by_id.values()))
    except Exception as e:
        app.logger.error("Error in /api/orders", exc_info=e)
        return jsonify({"error": str(e)}), 500

@app.route('/api/manualState', methods=['GET','POST'])
def manual_state():
    if request.method == 'GET':
        return jsonify(load_manual())
    # POST
    body = request.get_json(force=True)
    save_manual(body)
    return jsonify({"success": True})

@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery_list():
    try:
        return jsonify(fetch_embroidery_list())
    except Exception as e:
        app.logger.error("Error in /api/embroideryList", exc_info=e)
        return jsonify({"error": str(e)}), 500

# —————————————————————————————
# Main
# —————————————————————————————
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
