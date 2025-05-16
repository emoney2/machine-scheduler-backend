from google.oauth2 import service_account
import google.auth.transport.requests

# Path to your credentials.json
KEYFILE = "credentials.json"

# Scopes required for Sheets
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

creds = service_account.Credentials.from_service_account_file(KEYFILE, scopes=SCOPES)
request = google.auth.transport.requests.Request()

# Attempt a token refresh
creds.refresh(request)
print("✅ Token refreshed successfully!")
print("Access token (first 40 chars):", creds.token[:40], "…")
