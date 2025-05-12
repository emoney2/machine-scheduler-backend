# File: server.py

import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS, cross_origin
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
# enable CORS on all routes
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

# Where we persist the manualState overrides
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
        # skip rows without a Schedule String
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
            # placeholders for manual state overlay
            'machineId':    None,
            'start_date':   '',
            'end_date':     '',
            'delivery':     ''
        })

    return orders


def fetch_embroidery_list():
    """Fetch the 'Embroidery List' tab as JSON rows."""
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
        entry = {
            headers[i]: (row[i] if i < len(row) else '')
            for i in range(len(headers))
        }
        json_rows.append(entry)
    return json_rows


def load_persisted():
    """Load manualState overrides from disk."""
    if not os.path.exists(PERSISTED_FILE):
        return []
    try:
        with open(PERSISTED_FILE) as f:
            return json.load(f)
    except json.JSONDecodeError:
        return []


def save_persisted(data):
    """Save manualState overrides to disk."""
    with open(PERSISTED_FILE, 'w') as f:
        json.dump(data, f, indent=2)


# —————————————————————————————
# Routes
# —————————————————————————————

@app.route('/api/manualState', methods=['GET'])
@cross_origin()
def get_manual_state():
    try:
        return jsonify(load_persisted())
    except Exception as e:
        app.logger.error('Error loading manual state', exc_info=e)
        return jsonify({'error': 'Unable to load manual state'}), 500


@app.route('/api/manualState', methods=['POST'])
@cross_origin()
def post_manual_state():
    try:
        payload = request.get_json(force=True)
        save_persisted(payload)
        return jsonify(payload)
    except Exception as e:
        app.logger.error('Error saving manual state', exc_info=e)
        return jsonify({'error': 'Unable to save manual state'}), 500


@app.route('/api/orders', methods=['GET'])
@cross_origin()
def get_orders():
    """Return the live orders, with any manualState overlaid."""
    try:
        live = fetch_from_sheets()
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
def get_embroidery_list_route():
    """Return the raw Embroidery List rows as JSON."""
    try:
        return jsonify(fetch_embroidery_list())
    except Exception as e:
        app.logger.error('Error in /api/embroideryList', exc_info=e)
        return jsonify({'error': str(e)}), 500


# —————————————————————————————
# Main
# —————————————————————————————
if __name__ == '__main__':
    # in production your host/port may be overridden by Render
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
