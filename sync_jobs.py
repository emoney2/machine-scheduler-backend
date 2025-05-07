# File: backend/sync_jobs.py

import json
import string
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Path to your service account key
SERVICE_ACCOUNT_FILE = 'credentials.json'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']

# Your Spreadsheet ID and ranges
SPREADSHEET_ID = '11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4'
HEADER_RANGE   = 'Production Orders!1:1'     # header row
DETAILS_RANGE  = 'Production Orders!AC2:AC'  # full job text column

# Authenticate and build the Sheets API client
creds = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)
service = build('sheets', 'v4', credentials=creds)

# 1) Fetch headers
header_res = service.spreadsheets().values().get(
    spreadsheetId=SPREADSHEET_ID,
    range=HEADER_RANGE
).execute()
headers = header_res.get('values', [[]])[0]

# Helper: map a header name to its column letter (A, B, ..., Z, AA, AB, ...)
def header_to_col(name):
    try:
        idx = headers.index(name)
    except ValueError:
        return None
    letters = string.ascii_uppercase
    col = ''
    while idx >= 0:
        col = letters[idx % 26] + col
        idx = idx // 26 - 1
    return col

# 2) Determine columns by header name
stitch_col = header_to_col('Stitch Count')
qty_col    = header_to_col('Quantity')
due_col    = header_to_col('Due Date')

# 3) Build ranges for batchGet
ranges = [DETAILS_RANGE]
if stitch_col:
    ranges.append(f'Production Orders!{stitch_col}2:{stitch_col}')
if qty_col:
    ranges.append(f'Production Orders!{qty_col}2:{qty_col}')
if due_col:
    ranges.append(f'Production Orders!{due_col}2:{due_col}')

# 4) Fetch the columns in one call
result = service.spreadsheets().values().batchGet(
    spreadsheetId=SPREADSHEET_ID,
    ranges=ranges
).execute()
value_ranges = result.get('valueRanges', [])

details_vals = value_ranges[0].get('values', [])
stitch_vals  = value_ranges[1].get('values', []) if len(value_ranges) > 1 else []
qty_vals     = value_ranges[2].get('values', []) if len(value_ranges) > 2 else []
due_vals     = value_ranges[3].get('values', []) if len(value_ranges) > 3 else []

orders = []
for i, det in enumerate(details_vals):
    cell = det[0].strip()
    if not cell:
        continue

    # Stitch Count (default 30000)
    if i < len(stitch_vals) and stitch_vals[i] and stitch_vals[i][0].strip():
        try:
            sc = float(stitch_vals[i][0])
        except ValueError:
            sc = 30000.0
    else:
        sc = 30000.0

    # Quantity (default 1)
    if i < len(qty_vals) and qty_vals[i] and qty_vals[i][0].strip():
        try:
            q = float(qty_vals[i][0])
        except ValueError:
            q = 1.0
    else:
        q = 1.0

    # Due Date (default empty string)
    if i < len(due_vals) and due_vals[i] and due_vals[i][0].strip():
        due_date = due_vals[i][0].strip()
    else:
        due_date = ''

    # Split ID and title on first " - "
    if ' - ' in cell:
        order_id, title = cell.split(' - ', 1)
    else:
        order_id, title = cell, cell

    orders.append({
        'id':           order_id.strip(),
        'title':        title.strip(),
        'stitch_count': sc,
        'quantity':     q,
        'due_date':     due_date
    })

# 5) Save to JSON
with open('orders.json', 'w') as f:
    json.dump(orders, f, indent=2)

print(f"Synced {len(orders)} orders to orders.json")
