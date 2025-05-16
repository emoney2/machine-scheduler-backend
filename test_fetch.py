from google.oauth2 import service_account
from googleapiclient.discovery import build

# Copy these from your server.py
SPREADSHEET_ID   = "11s5QahOgGsDRFWFX6diXvonG5pESRE1ak79V-8uEbb4"
ORDERS_RANGE     = "'Production Orders'!A:AM"
CREDENTIALS_FILE = "credentials.json"

# Build Sheets client
creds = service_account.Credentials.from_service_account_file(
    CREDENTIALS_FILE,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
service = build("sheets", "v4", credentials=creds)
sheets = service.spreadsheets()

# Fetch the sheet
result = sheets.values().get(
    spreadsheetId=SPREADSHEET_ID,
    range=ORDERS_RANGE
).execute()
rows = result.get("values", [])

print(f"Fetched {len(rows)} rows.")
if rows:
    print("Header row:", rows[0])
    print("First data row:", rows[1] if len(rows) > 1 else "(no data rows)")
