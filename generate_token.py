from __future__ import print_function
from google_auth_oauthlib.flow import InstalledAppFlow

# Drive scope
SCOPES = ["https://www.googleapis.com/auth/drive"]

def main():
    flow = InstalledAppFlow.from_client_secrets_file(
        "work_credentials.json",  # your desktop OAuth client from WORK project
        SCOPES,
    )
    # Force a refresh_token to be issued
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    with open("token.json", "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print("✅ token.json created (with offline access).")

if __name__ == "__main__":
    main()
