# File: server.py

import os
import json
import base64
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app)   # ← full CORS

# —————————————————————————————
# Configuration
# —————————————————————————————
SCOPES         = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'

ORDERS_RANGE     = 'Production Orders!A:AC'
EMBROIDERY_RANGE = 'Embroidery List!A:ZZ'

PERSISTED_FILE   = 'persisted.json'


# —————————————————————————————
# Auth helper: either from SERVICE_ACCOUNT_B64 or fallback to credentials.json
# —————————————————————————————
def get_credentials():
    b64 = os.getenv('SERVICE_ACCOUNT_B64')
    if b64:
        # decode the JSON you pasted into SERVICE_ACCOUNT_B64
        info = json.loads(base64.b64decode(b64).decode())
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    # fallback if you happen to have a credentials.json file
    return service_account.Credentials.from_service_account_file(
        'credentials.json', scopes=SCOPES
    )


def sheets_values():
    creds = get_credentials()
    return build('sheets', 'v4', credentials=creds).spreadsheets().values()


# —————————————————————————————
# Fetch orders from your sheet
# —————————————————————————————
def fetch_from_sheets():
    res = sheets_values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=ORDERS_RANGE
    ).execute()
    rows = res.get('values', [])
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
    i_due_type = idx_any(['Hard Date/Soft Date','Due Type','Hard/Soft Date'])
    i_sc       = idx_any(['Stitch Count'])

    if i_id < 0 or i_sched < 0:
        raise RuntimeError(f"Sheet missing Order# or ScheduleString header: {headers!r}")

    out = []
    for row in rows[1:]:
        if i_sched >= len(row) or not row[i_sched].strip():
            continue
        try:
            oid = int(row[i_id])
        except:
            continue
        out.append({
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
    return out


# —————————————————————————————
# Fetch embroidery list
# —————————————————————————————
def fetch_embroidery_list():
    res = sheets_values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=EMBROIDERY_RANGE
    ).execute()
    rows = res.get('values', [])
    if not rows:
        return []
    headers, *data_rows = rows
    out = []
    for row in data_rows:
        obj = {}
        for i, h in enumerate(headers):
            obj[h] = row[i] if i < len(row) else ''
        out.append(obj)
    return out


# —————————————————————————————
# Persisted manual-state helpers
# —————————————————————————————
def load_persisted():
    if not os.path.exists(PERSISTED_FILE):
        return []
    try:
        data = json.load(open(PERSISTED_FILE))
        return data if isinstance(data, list) else []
    except:
        return []


def save_persisted(arr):
    with open(PERSISTED_FILE, 'w') as f:
        json.dump(arr, f, indent=2)


# —————————————————————————————
# Routes
# —————————————————————————————

@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        live = fetch_from_sheets()
    except Exception as e:
        return jsonify({ 'error': str(e) }), 500

    persisted = load_persisted()
    by_id = { o['id']: o for o in live }
    for p in persisted:
        if isinstance(p, dict):
            oid = p.get('id')
            if oid in by_id:
                by_id[oid].update(p)

    return jsonify(list(by_id.values()))


@app.route('/api/manualState', methods=['GET','POST'])
def manual_state():
    if request.method == 'GET':
        return jsonify(load_persisted())
    data = request.get_json(force=True)
    if not isinstance(data, list):
        return jsonify({ 'error': 'expected JSON array' }), 400
    save_persisted(data)
    return jsonify({ 'success': True })


@app.route('/api/embroideryList', methods=['GET'])
def embroidery_list():
    try:
        return jsonify(fetch_embroidery_list())
    except Exception as e:
        return jsonify({ 'error': str(e) }), 500


# —————————————————————————————
# Run
# —————————————————————————————
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
