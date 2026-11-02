"""Re-authorize Google OAuth for Drive, Sheets, and Gmail (no password stored).

Run locally once:
  python generate_token.py

Sign in as justin.eckard@jrcogolf.com (required), allow Drive + Sheets + Gmail.
Then deploy with:
  powershell ./deploy_gmail_token.ps1

Or copy token.json into production as GOOGLE_TOKEN_JSON.
"""
from __future__ import annotations

import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]

REQUIRED_GMAIL_ACCOUNT = "justin.eckard@jrcogolf.com"

CLIENT_CANDIDATES = (
    "new.json",
    "credentials.json",
    "oauth-credentials.json",
    "work_credentials.json",
)


def _profile_email(creds: Credentials) -> str:
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = gmail.users().getProfile(userId="me").execute() or {}
    return str(profile.get("emailAddress") or "").strip().lower()


def main() -> None:
    client = next((p for p in CLIENT_CANDIDATES if Path(p).exists()), None)
    if not client:
        raise SystemExit(
            "No OAuth client secrets file found. Place new.json or credentials.json in backend/."
        )
    print(f"Using client secrets: {client}")
    print("Scopes:", ", ".join(SCOPES))
    print(f"IMPORTANT: Sign in as {REQUIRED_GMAIL_ACCOUNT} (not info@jrco.us).")
    flow = InstalledAppFlow.from_client_secrets_file(client, SCOPES)
    # Force consent so Gmail scopes are actually granted on an existing Drive/Sheets app.
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        access_type="offline",
        include_granted_scopes="true",
        login_hint=REQUIRED_GMAIL_ACCOUNT,
    )
    email = _profile_email(creds)
    print("Authenticated Gmail mailbox:", email or "(unknown)")
    if email != REQUIRED_GMAIL_ACCOUNT:
        raise SystemExit(
            f"Wrong account ({email or 'unknown'}). "
            f"Re-run and choose {REQUIRED_GMAIL_ACCOUNT} so digitizing emails "
            "appear in Justin's Sent folder."
        )

    payload = json.loads(creds.to_json())
    payload["account"] = email
    Path("token.json").write_text(json.dumps(payload), encoding="utf-8")
    scopes = list(getattr(creds, "scopes", None) or payload.get("scopes") or [])
    print("Wrote token.json for", email)
    print("Granted scopes:", scopes)
    if not any("gmail" in s for s in scopes):
        print("WARNING: Gmail scope missing — deny/allow again and re-run.")
    else:
        print("Gmail permission OK. Deploy with: powershell ./deploy_gmail_token.ps1")


if __name__ == "__main__":
    main()
