# File: server.py

import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS, cross_origin
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
# Allow CORS from anywhere on all /api/* routes
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ————————————————————————————
# Configuration
# ————————————————————————————
SERVICE_ACCOUNT_FILE = os.getenv('SERVICE_ACCOUNT_FILE', 'credentials.json')
SCOPES               = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID       = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE         = 'Production Orders!A:AC'
EMBROIDERY_RANGE     = 'Embroidery List!A:ZZ'

# state files
PERSISTED_FILE       = 'persisted.json'       # overlays order fields on individual jobs
MANUAL_STATE_FILE    = 'manual_state.json'    # {"machine1":[id,...], "machine2":[id,...]}
LINK_STATE_FILE      = 'link_state.json'      # { jobId: linkedJobId, ... }


# ————————————————————————————
# Helpers: load & save JSON safely
# ————————————————————————————
def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError:
        return default

def _save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


# ————————————————————————————
# Helpers: Google Sheets fetching
# ————————————————————————————
def fetch_from_sheets():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    service = build('sheets', 'v4', credentials=creds)
    resp = service.spreadsheets().values() \
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
    i_due_type = idx_any(['Hard Date/Soft Date','Due Type','Hard/Soft Date'])
    i_sc       = idx_any(['Stitch Count'])

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
            'company':      row[i_company]   if 0 <= i_company  < len(row) else '',
            'design':       row[i_design]    if 0 <= i_design   < len(row) else '',
            'quantity':     int(row[i_qty])  if 0 <= i_qty      < len(row) and row[i_qty].isdigit() else 1,
            'due_date':     row[i_due]       if 0 <= i_due      < len(row) else '',
            'due_type':     row[i_due_type]  if 0 <= i_due_type < len(row) else '',
            'stitch_count': int(row[i_sc])   if 0 <= i_sc       < len(row) and row[i_sc].isdigit() else 30000,
            # these three fields can be overlaid/persisted or computed
            'machineId':    None,
            'start_date':   '',
            'end_date':     '',
            'delivery':     ''
        })
    return orders

def fetch_embroidery_list():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    service = build('sheets', 'v4', credentials=creds)
    resp = service.spreadsheets().values() \
        .get(spreadsheetId=SPREADSHEET_ID, range=EMBROIDERY_RANGE) \
        .execute()
    rows = resp.get('values', [])
    if not rows:
        return []

    headers, *data_rows = rows
    json_rows = []
    for row in data_rows:
        entry = { headers[i]: (row[i] if i < len(row) else '') for i in range(len(headers)) }
        json_rows.append(entry)
    return json_rows


# ————————————————————————————
# Persisted-overlay helpers
# ————————————————————————————
def load_persisted():
    return _load_json(PERSISTED_FILE, [])

def save_persisted(data):
    _save_json(PERSISTED_FILE, data)


# ————————————————————————————
# Manual-state helpers
# ————————————————————————————
def load_manual_state():
    return _load_json(MANUAL_STATE_FILE, {"machine1": [], "machine2": []})

def save_manual_state(state):
    _save_json(MANUAL_STATE_FILE, state)


# ————————————————————————————
# Link-state helpers
# ————————————————————————————
def load_link_map():
    return _load_json(LINK_STATE_FILE, {})

def save_link_map(m):
    _save_json(LINK_STATE_FILE, m)


# ————————————————————————————
# Routes
# ————————————————————————————

@app.route('/api/orders', methods=['GET'])
@cross_origin()
def get_orders():
    try:
        # 1) fetch live sheet data
        live = fetch_from_sheets()

        # 2) overlay any persisted cell-level edits
        persisted = load_persisted()
        by_id     = {o['id']: o for o in live}
        for p in persisted:
            oid = p.get('id')
            if oid in by_id:
                by_id[oid].update(p)
        orders = list(by_id.values())

        # 3) apply manual ordering
        manual = load_manual_state()
        # build map: jobId -> jobObject
        job_map = {o['id']: o for o in orders}
        # for each machine, reorder
        def apply_manual(col_id):
            out = []
            for oid in manual.get(col_id, []):
                if oid in job_map:
                    job_map[oid]['machineId'] = col_id
                    out.append(job_map[oid])
            # any that live in that col but not in manual list, append at end
            for o in orders:
                if o['machineId']==col_id and o not in out:
                    out.append(o)
            return out

        # reset all machine assignments for those not in M1/M2
        for o in orders:
            if o['machineId'] not in ('machine1','machine2'):
                o['machineId'] = 'queue'

        # rebuild a single flat list in the three-column order
        final = apply_manual('machine1') + apply_manual('machine2')
        # queue is any remaining
        queue_jobs = [o for o in orders if o['machineId']=='queue']
        final += queue_jobs

        # 4) attach linkedTo
        links = load_link_map()
        for o in final:
            o['linkedTo'] = links.get(str(o['id'])) or None

        return jsonify(final)
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


@app.route('/api/manualState', methods=['GET'])
@cross_origin()
def get_manual_state():
    return jsonify(load_manual_state())


@app.route('/api/manualState', methods=['POST'])
@cross_origin()
def post_manual_state():
    data = request.get_json(force=True)
    save_manual_state(data)
    return jsonify({'success': True})


@app.route('/api/links', methods=['GET'])
@cross_origin()
def get_links():
    return jsonify(load_link_map())


@app.route('/api/links', methods=['POST'])
@cross_origin()
def post_links():
    data = request.get_json(force=True)
    # expect { jobId: linkedJobId, ... }
    save_link_map({ str(k): str(v) for k,v in data.items() })
    return jsonify({'success': True})


# ————————————————————————————
# Main
# ————————————————————————————
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
