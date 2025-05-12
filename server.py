import os
import json
import base64
import logging
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ——— Setup logging —————————————————————————————————————————
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# ——— Load credentials —————————————————————————————————————————
def load_service_account_info():
    creds_path = os.path.join(os.getcwd(), 'credentials.json')
    if os.path.exists(creds_path):
        logger.info(f'Loading Google creds from file: {creds_path}')
        with open(creds_path, 'r') as f:
            return json.load(f)
    logger.info('credentials.json not found, falling back to SERVICE_ACCOUNT_B64 env var')
    b64 = os.environ.get('SERVICE_ACCOUNT_B64')
    if not b64:
        raise RuntimeError('No credentials.json file and SERVICE_ACCOUNT_B64 is not set')
    decoded = base64.b64decode(b64).decode('utf-8')
    return json.loads(decoded)

sa_info = load_service_account_info()
SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
credentials = Credentials.from_service_account_info(sa_info, scopes=SCOPES)

# ——— Build Sheets client —————————————————————————————————————————
sheets = build('sheets', 'v4', credentials=credentials)

# ——— Your sheet IDs & ranges — adjust these to your spreadsheet —————————
ORDERS_SPREADSHEET_ID      = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
ORDERS_RANGE              = 'Production Orders!A:AZ'
EMBROIDERY_SPREADSHEET_ID = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
EMBROIDERY_RANGE          = 'Embroidery List!A:ASZ'

# ——— Flask app setup —————————————————————————————————————————————
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})


def fetch_sheet(spreadsheet_id, range_name):
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=range_name
    ).execute()
    return resp.get('values', [])


@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        rows = fetch_sheet(ORDERS_SPREADSHEET_ID, ORDERS_RANGE)
        # assume header row in rows[0]
        header = rows[0]
        data = [
            dict(zip(header, row))
            for row in rows[1:]
        ]
        return jsonify(data)
    except Exception as e:
        logger.exception('Error in /api/orders')
        return jsonify({'error': str(e)}), 500


@app.route('/api/embroideryList', methods=['GET'])
def get_embroidery_list():
    try:
        rows = fetch_sheet(EMBROIDERY_SPREADSHEET_ID, EMBROIDERY_RANGE)
        header = rows[0]
        data = [
            dict(zip(header, row))
            for row in rows[1:]
        ]
        return jsonify(data)
    except Exception as e:
        logger.exception('Error in /api/embroideryList')
        return jsonify({'error': str(e)}), 500


# simple in-memory manual state for demo; feel free to back this with a file/db
manual_state = {'machine1': [], 'machine2': []}

@app.route('/api/manualState', methods=['GET', 'POST'])
def manual_state_endpoint():
    global manual_state
    if request.method == 'POST':
        try:
            manual_state = request.get_json() or manual_state
            return jsonify(manual_state)
        except Exception as e:
            logger.exception('Error POST /api/manualState')
            return jsonify({'error': str(e)}), 500
    else:
        return jsonify(manual_state)


if __name__ == '__main__':
    logger.info('Starting Flask server on port %s', os.environ.get('PORT', '10000'))
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')), debug=True)
