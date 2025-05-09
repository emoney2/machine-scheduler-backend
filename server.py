# File: server.py

import os, json, base64
from flask import Flask, jsonify, request
from flask_cors import CORS, cross_origin
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# — config —
SERVICE_ACCOUNT_FILE = 'credentials.json'
SCOPES               = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID       = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE         = 'Production Orders!A:AC'
EMBROIDERY_RANGE     = 'Embroidery List!A:ZZ'

PERSISTED_FILE       = 'persisted.json'
MANUAL_STATE_FILE    = 'manual_state.json'
LINK_STATE_FILE      = 'link_state.json'

# — helpers for creds —
def get_google_creds():
    b64 = os.getenv('SERVICE_ACCOUNT_B64')
    if b64:
        info = json.loads(base64.b64decode(b64))
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )

# — JSON load/save —
def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError:
        return default

def _save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

# — state helpers —
def load_persisted():     return _load_json(PERSISTED_FILE, [])
def save_persisted(d):    _save_json(PERSISTED_FILE, d)
def load_manual():        return _load_json(MANUAL_STATE_FILE, {"machine1": [], "machine2": []})
def save_manual(d):       _save_json(MANUAL_STATE_FILE, d)
def load_links():         return _load_json(LINK_STATE_FILE, {})
def save_links(d):        _save_json(LINK_STATE_FILE, d)

# — fetch from Sheets —
def fetch_from_sheets():
    creds = get_google_creds()
    service = build('sheets', 'v4', credentials=creds)
    resp = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=ORDERS_RANGE
    ).execute()
    rows = resp.get('values', [])
    if not rows: return []

    headers = rows[0]
    def idx_any(c): 
        for x in c:
            if x in headers:
                return headers.index(x)
        return -1

    i_id       = idx_any(['Order #'])
    i_sched    = idx_any(['Schedule String'])
    i_company  = idx_any(['Company Name'])
    i_design   = idx_any(['Design'])
    i_qty      = idx_any(['Quantity'])
    i_due      = idx_any(['Due Date'])
    i_due_type = idx_any(['Hard Date/Soft Date','Due Type','Hard/Soft Date'])
    i_sc       = idx_any(['Stitch Count'])

    out = []
    for row in rows[1:]:
        if i_sched<0 or i_sched>=len(row) or not row[i_sched].strip():
            continue
        try: oid = int(row[i_id])
        except: continue
        out.append({
          'id':           oid,
          'title':        row[i_sched],
          'company':      row[i_company] if 0<=i_company<len(row) else '',
          'design':       row[i_design]  if 0<=i_design <len(row) else '',
          'quantity':     int(row[i_qty]) if 0<=i_qty<len(row) and row[i_qty].isdigit() else 1,
          'due_date':     row[i_due]     if 0<=i_due <len(row) else '',
          'due_type':     row[i_due_type] if 0<=i_due_type<len(row) else '',
          'stitch_count': int(row[i_sc]) if 0<=i_sc<len(row) and row[i_sc].isdigit() else 30000,
          'machineId':    None,
          'start_date':   '',
          'end_date':     '',
          'delivery':     ''
        })
    return out

def fetch_embroidery_list():
    creds = get_google_creds()
    service = build('sheets','v4',credentials=creds)
    resp = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=EMBROIDERY_RANGE
    ).execute()
    rows = resp.get('values',[])
    if not rows: return []
    headers,*data = rows
    return [
      { headers[i]: (row[i] if i<len(row) else '') 
        for i in range(len(headers)) }
      for row in data
    ]

# ————————————————————————————
# Routes
# ————————————————————————————

@app.route('/api/orders', methods=['GET'])
@cross_origin()
def get_orders():
    try:
        live      = fetch_from_sheets()
        persisted = load_persisted()
        by_id     = {o['id']: o for o in live}
        for p in persisted:
            if p.get('id') in by_id:
                by_id[p['id']].update(p)
        orders = list(by_id.values())

        # assign everyone to queue by default
        for o in orders:
            if o.get('machineId') not in ('machine1','machine2'):
                o['machineId'] = 'queue'

        # apply manual ordering
        manual = load_manual()
        # helper to rebuild a column
        def apply_col(col):
            arr = []
            for oid in manual.get(col, []):
                if oid in by_id:
                    by_id[oid]['machineId'] = col
                    arr.append(by_id[oid])
            # any that were on that machine but not in manual list
            for o in orders:
                if o['machineId']==col and o not in arr:
                    arr.append(o)
            return arr

        final = apply_col('machine1') + apply_col('machine2')
        # then everything left is queue
        final += [o for o in orders if o['machineId']=='queue']

        # attach links
        links = load_links()
        for o in final:
            o['linkedTo'] = links.get(str(o['id'])) or None

        return jsonify(final)
    except Exception as e:
        app.logger.error('Error in /api/orders', exc_info=e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/embroideryList', methods=['GET'])
@cross_origin()
def get_embroidery():
    try:
        return jsonify(fetch_embroidery_list())
    except Exception as e:
        app.logger.error('Error in /api/embroideryList', exc_info=e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/manualState', methods=['GET','POST'])
@cross_origin()
def manual_state():
    if request.method=='GET':
        return jsonify(load_manual())
    data = request.get_json(force=True)
    save_manual(data)
    return jsonify({'success': True})

@app.route('/api/links', methods=['GET','POST'])
@cross_origin()
def links():
    if request.method=='GET':
        return jsonify(load_links())
    data = request.get_json(force=True)
    save_links({ str(k): str(v) for k,v in data.items() })
    return jsonify({'success': True})

if __name__=='__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
