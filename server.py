# backend/server.py
import os, json
from flask import Flask, request, jsonify
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app)  # <-- enable CORS on all routes

# --- CONFIG -------------------------------------------------
# Path to the JSON file you will upload as a “Secret File” on Render:
SERVICE_ACCOUNT_FILE = 'credentials.json'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']

# Your spreadsheet ID needs to be set as an env var on Render:
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
if not SPREADSHEET_ID:
    raise RuntimeError("You must set SPREADSHEET_ID in your Render environment.")

# Manual state persistence (in-memory for now; optional: file-backed)
_manual_state = []

# --- HELPERS ------------------------------------------------
def _get_sheets_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build('sheets', 'v4', credentials=creds)

def fetch_from_sheets():
    svc = _get_sheets_service().spreadsheets().values()
    # adjust your range names as needed
    orders = svc.get(spreadsheetId=SPREADSHEET_ID, range='orders!A2:G').execute().get('values', [])
    embroidery = svc.get(spreadsheetId=SPREADSHEET_ID, range='embroideryList!A2:B').execute().get('values', [])
    # turn rows into objects:
    orders_objs = []
    for row in orders:
        # expect [ id, company, design, quantity, stitch_count, due_date, due_type ]
        try:
            orders_objs.append({
                'id': int(row[0]),
                'company': row[1],
                'design': row[2],
                'quantity': int(row[3]),
                'stitch_count': int(row[4]),
                'due_date': row[5],
                'due_type': row[6]
            })
        except:
            continue
    embroidery_objs = []
    for row in embroidery:
        # expect [ Order #, Embroidery Start Time ]
        embroidery_objs.append({
            'Order #': row[0],
            'Embroidery Start Time': row[1]
        })
    return orders_objs, embroidery_objs

# --- ROUTES -------------------------------------------------
@app.route('/api/manualState', methods=['GET'])
def get_manual_state():
    return jsonify(_manual_state)

@app.route('/api/manualState', methods=['POST'])
def post_manual_state():
    data = request.get_json(force=True)
    if isinstance(data, list):
        global _manual_state
        _manual_state = data
        return '', 204
    return 'Bad payload', 400

@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        orders, _ = fetch_from_sheets()
        return jsonify(orders)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery_list():
    try:
        _, embroidery = fetch_from_sheets()
        return jsonify(embroidery)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# catch-all to remind you
@app.route('/')
def idx():
    return "Use /api/orders or /api/embroideryList", 404

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
