from google_auth_oauthlib.flow import InstalledAppFlow
import pickle
import json

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    # Needed so needlepoint belt order drafts can include PDF attachments.
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]

def main():
    flow = InstalledAppFlow.from_client_secrets_file("oauth-credentials.json", SCOPES)
    creds = flow.run_local_server(port=8080)
    
    with open("token.json", "w") as token_file:
        token_file.write(creds.to_json())

    print("✅ token.json has been saved.")

if __name__ == "__main__":
    main()
