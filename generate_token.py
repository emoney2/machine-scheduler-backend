from __future__ import print_function
from google_auth_oauthlib.flow import InstalledAppFlow

# Drive scope is enough to fetch thumbnails/files
SCOPES = ["https://www.googleapis.com/auth/drive"]

def main():
    flow = InstalledAppFlow.from_client_secrets_file(
        "work_credentials.json",  # <-- use your new file name here
        SCOPES,
    )
    creds = flow.run_local_server(port=0)
    with open("token.json", "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print("✅ token.json created next to this script.")

if __name__ == "__main__":
    main()
