import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ── CONFIG ────────────────────────────────────────────────────────────────────
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "YOUR_SPREADSHEET_ID_HERE")
ORDERS_RANGE   = os.environ.get("ORDERS_RANGE",   "orders!A:F")
EMB_RANGE      = os.environ.get("EMB_RANGE",      "embroidery!A:B")
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")
MANUAL_STATE_FILE = os.path.join(os.path.dirname(__file__), "manual_state.json")

# ── GOOGLE CLIENT ─────────────────────────────────────────────────────────────
creds = service_account.Credentials.from_service_account_file(
    CREDENTIALS_FILE,
    scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
)
sheets = build('sheets', 'v4', credentials=creds).spreadsheets()

# ── HELPERS ───────────────────────────────────────────────────────────────────
def fetch_from_sheets():
    return sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=ORDERS_RANGE
    ).execute().get('values', [])

def fetch_embroidery_list():
    return sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=EMB_RANGE
    ).execute().get('values', [])

def load_manual_state():
    if not os.path.exists(MANUAL_STATE_FILE):
        return []
    with open(MANUAL_STATE_FILE, 'r') as f:
        return json.load(f)

def save_manual_state(data):
    with open(MANUAL_STATE_FILE, 'w') as f:
        json.dump(data, f)

# ── ROUTES ─────────────────────────────────────────────────────────────────────
@app.route("/api/orders", methods=["GET"])
def get_orders():
    try:
        rows = fetch_from_sheets()
        # First row is header
        header, *data = rows
        orders = [dict(zip(header, row)) for row in data]
        return jsonify(orders)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/embroideryList", methods=["GET"])
def get_embroidery():
    try:
        rows = fetch_embroidery_list()
        header, *data = rows
        emb = [dict(zip(header, row)) for row in data]
        return jsonify(emb)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/manualState", methods=["GET", "POST"])
def manual_state():
    if request.method == "POST":
        save_manual_state(request.json or [])
        return jsonify({"status":"ok"})
    else:
        return jsonify(load_manual_state())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
