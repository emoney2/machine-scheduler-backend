import os
import logging
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ——— CONFIGURATION —————————————————————————————————————————————————————
SPREADSHEET_ID    = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'  
ORDERS_RANGE      = 'Production Orders!A1:AM1000'
EMBROIDERY_RANGE  = 'Embroidery List!A1:AM1000'
CREDENTIALS_FILE  = 'credentials.json'  # make sure this file is in your project root

# ——— BOILERPLATE ———————————————————————————————————————————————————————
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
CORS(app)

# load service account creds once
creds = Credentials.from_service_account_file(
    CREDENTIALS_FILE,
    scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
)
sheets = build('sheets', 'v4', credentials=creds).spreadsheets()

def fetch_sheet(range_name):
    """Fetch rows from the given range, return list of dicts."""
    result = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=range_name
    ).execute()
    values = result.get('values', [])
    if not values:
        return []
    headers = values[0]
    return [dict(zip(headers, row)) for row in values[1:]]

# ——— ENDPOINTS —————————————————————————————————————————————————————————
@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        data = fetch_sheet(ORDERS_RANGE)
        return jsonify(data)
    except HttpError as e:
        logging.exception("Error fetching orders")
        return jsonify({"error": str(e)}), 500

@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery_list():
    try:
        data = fetch_sheet(EMBROIDERY_RANGE)
        return jsonify(data)
    except HttpError as e:
        logging.exception("Error fetching embroidery list")
        return jsonify({"error": str(e)}), 500

@app.route('/api/manualState', methods=['GET', 'POST'])
def manual_state():
    if request.method == 'GET':
        return jsonify({"machine1": [], "machine2": []})
    else:
        # echo back whatever the client posted
        return jsonify(request.get_json() or {})

# ——— LAUNCH —————————————————————————————————————————————————————————————
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, debug=True)
