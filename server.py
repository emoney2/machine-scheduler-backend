# File: server.py

import os
import json
from flask import Flask, jsonify
from flask_cors import CORS, cross_origin
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app)

SCOPES           = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID   = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE     = 'Production Orders!A:AC'
EMBROIDERY_RANGE = 'Embroidery List!A:ZZ'

def get_creds():
    # Load the JSON string from the env var and parse it
    key_json = os.environ['GOOGLE_SERVICE_ACCOUNT']
    info     = json.loads(key_json)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

def fetch_from_sheets():
    creds   = get_creds()
    service = build('sheets', 'v4', credentials=creds)
    result  = service.spreadsheets().values() \
        .get(spreadsheetId=SPREADSHEET_ID, range=ORDERS_RANGE) \
        .execute()
    rows = result.get('values', [])
    if not rows:
        return []
    headers = rows[0]
    def idx(name):
        return headers.index(name) if name in headers else -1
    i_id    = idx('Order #')
    i_sched = idx('Schedule String')
    # …find other indices similarly…
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
            'company':      row[idx('Company Name')] if idx('Company Name')<len(row) else '',
            'design':       row[idx('Design')]       if idx('Design')<len(row) else '',
            'quantity':     int(row[idx('Quantity')]) if row[idx('Quantity')].isdigit() else 1,
            'due_date':     row[idx('Due Date')]     if idx('Due Date')<len(row) else '',
            'due_type':     row[idx('Due Type')]     if idx('Due Type')<len(row) else '',
            'stitch_count': int(row[idx('Stitch Count')]) if row[idx('Stitch Count')].isdigit() else 30000,
            'machineId':    None,
            'start_date':   '',
            'end_date':     '',
            'delivery':     ''
        })
    return orders

def fetch_embroidery_list():
    creds   = get_creds()
    service = build('sheets', 'v4', credentials=creds)
    result  = service.spreadsheets().values() \
        .get(spreadsheetId=SPREADSHEET_ID, range=EMBROIDERY_RANGE) \
        .execute()
    rows = result.get('values', [])
    if not rows:
        return []
    headers, *data = rows
    return [
        { headers[i]: (row[i] if i < len(row) else '') for i in range(len(headers)) }
        for row in data
    ]

@app.route('/api/orders', methods=['GET'])
@cross_origin()
def get_orders():
    return jsonify(fetch_from_sheets())

@app.route('/api/embroideryList', methods=['GET'])
@cross_origin()
def get_embroidery_list():
    try:
        return jsonify(fetch_embroidery_list())
    except Exception:
        return jsonify({'error':'Unable to load embroidery list'}), 500

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
