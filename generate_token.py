# generate_token.py
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import json

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

def main():
    flow = InstalledAppFlow.from_client_secrets_file(
        "new.json", SCOPES
    )
    # force an offline refresh token
    creds = flow.run_local_server(
        port=0, prompt="consent", access_type="offline", include_granted_scopes="true"
    )
    with open("token.json", "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print("Wrote token.json with refresh_token.")

if __name__ == "__main__":
    main()
