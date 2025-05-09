# File: server.py
import os
import json
import base64
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
# allow CORS from anywhere on all /api/* routes
CORS(app, resources={r"/api/*": {"origins": "*"}})

# —————————————————————————————
# Configuration
# —————————————————————————————
# If you prefer file-based:
SERVICE_ACCOUNT_FILE = 'credentials.json'
# or set your encoded service account in env var:
# os.environ['GOOGLE_SERVICE_ACCOUNT_B64'] = '<base64-json>'

SCOPES             = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID     = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE       = 'Production Orders!A:AC'
EMBROIDERY_RANGE   = 'Embroidery List!A:ZZ'
PERSISTED_FILE     = 'persisted.json'


# —————————————————————————————
# Auth helper (either from JSON file or B64 env var)
# —————————————————————————————
def _get_credentials():
    b64 = os.getenv('GOOGLE_SERVICE_ACCOUNT_B64')
    if b64:
        info = json.loads(base64.b64decode(b64).decode())
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)


# —————————————————————————————
# Sheet-fetch helpers
# —————————————————————————————
def fetch_from_sheets():
    creds   = _get_credentials()
    service = build('sheets', 'v4', credentials=creds)
    resp    = service.spreadsheets().values() \
                  .get(spreadsheetId=SPREADSHEET_ID, range=ORDERS_RANGE) \
                  .execute()
    rows    = resp.get('values', [])
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
    for r in rows[1:]:
        if i_sched<0 or i_sched>=len(r) or not r[i_sched].strip(): 
            continue
        try:
            oid = int(r[i_id])
        except:
            continue
        orders.append({
            'id':         oid,
            'title':      r[i_sched],
            'company':    r[i_company]   if 0<=i_company<len(r) else '',
            'design':     r[i_design]    if 0<=i_design<len(r)  else '',
            'quantity':   int(r[i_qty])  if 0<=i_qty<len(r) and r[i_qty].isdigit() else 1,
            'due_date':   r[i_due]       if 0<=i_due<len(r)     else '',
            'due_type':   r[i_due_type]  if 0<=i_due_type<len(r) else '',
            'stitch_count': int(r[i_sc]) if 0<=i_sc<len(r) and r[i_sc].isdigit() else 30000,
            'machineId':  None,
            'start_date': '',
            'end_date':   '',
            'delivery':   ''
        })
    return orders


def fetch_embroidery_list():
    creds   = _get_credentials()
    service = build('sheets', 'v4', credentials=creds)
    resp    = service.spreadsheets().values() \
                  .get(spreadsheetId=SPREADSHEET_ID, range=EMBROIDERY_RANGE) \
                  .execute()
    rows    = resp.get('values', [])
    if not rows:
        return []
    headers, *data = rows
    out = []
    for r in data:
        entry = { headers[i]: (r[i] if i<len(r) else '') for i in range(len(headers)) }
        out.append(entry)
    return out


# —————————————————————————————
# Persisted-state helpers
# —————————————————————————————
def load_persisted():
    if not os.path.exists(PERSISTED_FILE):
        return []
    try:
        return json.load(open(PERSISTED_FILE))
    except:
        return []


def save_persisted(data):
    with open(PERSISTED_FILE, 'w') as f:
        json.dump(data, f, indent=2)


# —————————————————————————————
# API: GET + PUT for orders
# —————————————————————————————

@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        live = fetch_from_sheets()
    except Exception as e:
        app.logger.error('Error in /api/orders', exc_info=e)
        return jsonify(error=str(e)), 500

    persisted = load_persisted()
    by_id     = {o['id']: o for o in live}

    for p in persisted:
        if isinstance(p, dict):
            oid = p.get('id')
            if oid in by_id:
                by_id[oid].update(p)
    return jsonify(list(by_id.values()))


@app.route('/api/orders/<int:order_id>', methods=['PUT'])
def update_order(order_id):
    data      = request.get_json(force=True) or {}
    persisted = [p for p in load_persisted() if not (isinstance(p, dict) and p.get('id')==order_id)]

    # only carry through the fields you need
    entry = {'id': order_id}
    for f in ('machineId','start_date','end_date','delivery','position','linkedTo'):
        if f in data:
            entry[f] = data[f]
    persisted.append(entry)
    save_persisted(persisted)
    return jsonify(success=True)


# —————————————————————————————
# API: manualState GET & POST
# —————————————————————————————
@app.route('/api/manualState', methods=['GET','POST'])
def manual_state():
    if request.method=='GET':
        return jsonify(load_persisted())
    payload = request.get_json(force=True) or {}
    save_persisted(payload)
    return jsonify(success=True)


# —————————————————————————————
# API: Embroidery List
# —————————————————————————————
@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery():
    try:
        return jsonify(fetch_embroidery_list())
    except Exception as e:
        app.logger.error('Error in /api/embroideryList', exc_info=e)
        return jsonify(error=str(e)), 500


# —————————————————————————————
# Main
# —————————————————————————————
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
