"""Re-authorize Google OAuth (no password stored).

Digitizing auto-attach must send from justin.eckard@jrcogolf.com.
Drive/Sheets can stay on info@ via the main token.json.

Gmail-only token (recommended for digitizing):
  python generate_token.py --gmail
  -> writes gmail_token.json for Justin
  -> deploy with: powershell ./deploy_gmail_token.ps1

Full token (Drive + Sheets + Gmail as Justin):
  python generate_token.py
  -> writes token.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

FULL_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]

GMAIL_SCOPES = [
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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--gmail",
        action="store_true",
        help="Create gmail_token.json for Justin only (keeps existing Drive token.json).",
    )
    args = parser.parse_args()

    client = next((p for p in CLIENT_CANDIDATES if Path(p).exists()), None)
    if not client:
        raise SystemExit(
            "No OAuth client secrets file found. Place new.json or credentials.json in backend/."
        )

    scopes = GMAIL_SCOPES if args.gmail else FULL_SCOPES
    out_path = Path("gmail_token.json" if args.gmail else "token.json")

    print(f"Using client secrets: {client}")
    print("Scopes:", ", ".join(scopes))
    print(f"IMPORTANT: Sign in as {REQUIRED_GMAIL_ACCOUNT} (not info@jrco.us).")
    print(f"Output file: {out_path}")

    flow = InstalledAppFlow.from_client_secrets_file(client, scopes)
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
            f"Re-run and choose {REQUIRED_GMAIL_ACCOUNT}."
        )

    payload = json.loads(creds.to_json())
    payload["account"] = email
    out_path.write_text(json.dumps(payload), encoding="utf-8")
    granted = list(getattr(creds, "scopes", None) or payload.get("scopes") or [])
    print(f"Wrote {out_path} for {email}")
    print("Granted scopes:", granted)
    if not any("gmail" in s for s in granted):
        raise SystemExit("Gmail scope missing — re-run and accept Gmail permission.")
    print("Next: powershell ./deploy_gmail_token.ps1")


if __name__ == "__main__":
    main()
