# File: server.py

import os
import json
import base64
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app)  # Allow CORS on all routes

# —————————————————————————————
# Configuration
# —————————————————————————————
SERVICE_ACCOUNT_FILE = 'credentials.json'
# If you prefer to embed your JSON key in an env var instead:
# export SERVICE_ACCOUNT_B64="$(base64 credentials.json)"
SCOPES         = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'

ORDERS_RANGE     = 'Production Orders!A:AC'
EMBROIDERY_RANGE = 'Embroidery List!A:ZZ'

MANUAL_STATE_FILE = 'manual_state.json'


# —————————————————————————————
# Auth helper
# —————————————————————————————
def get_credentials():
    b64 = os.getenv('SERVICE_ACCOUNT_B64')
    if b64:
        info = json.loads(base64.b64decode(b64))
        return service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )
    return service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )


# —————————————————————————————
# Sheet fetchers
# —————————————————————————————
def fetch_from_sheets():
    creds   = get_credentials()
    service = build('sheets','v4',credentials=creds)
    result  = service.spreadsheets().values()\
                  .get(spreadsheetId=SPREADSHEET_ID, range=ORDERS_RANGE)\
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

    i_id       = idx_any(['Order #'])
    i_sched    = idx_any(['Schedule String'])
    i_company  = idx_any(['Company Name'])
    i_design   = idx_any(['Design'])
    i_qty      = idx_any(['Quantity'])
    i_due      = idx_any(['Due Date'])
    i_due_type = idx_any(['Hard Date/Soft Date','Due Type','Hard/Soft Date'])
    i_sc       = idx_any(['Stitch Count'])

    orders = []
    for row in rows[1:]:
        # skip blank schedule entries
        if i_sched<0 or i_sched>=len(row) or not row[i_sched].strip():
            continue

        try:
            oid = int(row[i_id])
        except:
            continue

        orders.append({
            'id':           oid,
            'title':        row[i_sched],
            'company':      row[i_company]  if 0<=i_company<len(row) else '',
            'design':       row[i_design]   if 0<=i_design<len(row) else '',
            'quantity':     int(row[i_qty]) if 0<=i_qty<len(row)  and row[i_qty].isdigit() else 1,
            'due_date':     row[i_due]      if 0<=i_due<len(row)  else '',
            'due_type':     row[i_due_type] if 0<=i_due_type<len(row) else '',
            'stitch_count': int(row[i_sc])  if 0<=i_sc<len(row)   and row[i_sc].isdigit() else 30000,
            'machineId':    None,
            'start_date':   '',
            'end_date':     '',
            'delivery':     ''
        })

    return orders


def fetch_embroidery_list():
    creds   = get_credentials()
    service = build('sheets','v4',credentials=creds)
    result  = service.spreadsheets().values()\
                  .get(spreadsheetId=SPREADSHEET_ID, range=EMBROIDERY_RANGE)\
                  .execute()
    rows = result.get('values', [])
    if not rows:
        return []

    headers, *data_rows = rows
    json_rows = []
    for row in data_rows:
        entry = { headers[i]: (row[i] if i<len(row) else '') 
                  for i in range(len(headers)) }
        json_rows.append(entry)

    return json_rows


# —————————————————————————————
# Manual‐state persistence
# —————————————————————————————
def load_manual_state():
    if not os.path.exists(MANUAL_STATE_FILE):
        return {'machine1': [], 'machine2': []}
    try:
        with open(MANUAL_STATE_FILE) as f:
            data = json.load(f)
            return {
                'machine1': data.get('machine1', []),
                'machine2': data.get('machine2', [])
            }
    except:
        return {'machine1': [], 'machine2': []}


def save_manual_state(state):
    with open(MANUAL_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


# —————————————————————————————
# Routes
# —————————————————————————————

@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        return jsonify(fetch_from_sheets())
    except Exception as e:
        app.logger.error('Error in /api/orders', exc_info=e)
        return jsonify({ 'error': str(e) }), 500


@app.route('/api/embroideryList', methods=['GET'])
def get_embro():
    try:
        return jsonify(fetch_embroidery_list())
    except Exception as e:
        app.logger.error('Error in /api/embroideryList', exc_info=e)
        return jsonify({ 'error': str(e) }), 500


@app.route('/api/manualState', methods=['GET'])
def get_manual():
    return jsonify(load_manual_state())


@app.route('/api/manualState', methods=['POST'])
def post_manual():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({ 'error': 'Expected an object with machine1/machine2 arrays' }), 400
    ms = {
      'machine1': data.get('machine1', []),
      'machine2': data.get('machine2', [])
    }
    save_manual_state(ms)
    return jsonify({ 'success': True })


# —————————————————————————————
# Main
# —————————————————————————————
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
