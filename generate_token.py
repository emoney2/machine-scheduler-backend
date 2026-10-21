"""Re-authorize Google OAuth for Drive, Sheets, and Gmail drafts (no password stored).

Run locally once:
  python generate_token.py

Sign in as justin.eckard@jrcogolf.com, allow Drive + Sheets + Gmail.
Then copy the new token.json into production as GOOGLE_TOKEN_JSON (or the token file).

Remove NEEDLEPOINT_GMAIL_APP_PASSWORD if you set it — that path is no longer used.
"""
from google_auth_oauthlib.flow import InstalledAppFlow
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]

CLIENT_CANDIDATES = (
    "new.json",
    "credentials.json",
    "oauth-credentials.json",
    "work_credentials.json",
)


def main() -> None:
    client = next((p for p in CLIENT_CANDIDATES if Path(p).exists()), None)
    if not client:
        raise SystemExit(
            "No OAuth client secrets file found. Place new.json or credentials.json in backend/."
        )
    print(f"Using client secrets: {client}")
    print("Scopes:", ", ".join(SCOPES))
    flow = InstalledAppFlow.from_client_secrets_file(client, SCOPES)
    # Force consent so Gmail scopes are actually granted on an existing Drive/Sheets app.
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        access_type="offline",
        include_granted_scopes="true",
    )
    Path("token.json").write_text(creds.to_json(), encoding="utf-8")
    scopes = list(getattr(creds, "scopes", None) or [])
    print("Wrote token.json")
    print("Granted scopes:", scopes)
    if not any("gmail" in s for s in scopes):
        print("WARNING: Gmail scope missing — deny/allow again and re-run.")
    else:
        print("Gmail draft permission OK. Update production GOOGLE_TOKEN_JSON with this file.")


if __name__ == "__main__":
    main()
