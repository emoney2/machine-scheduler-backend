# Gmail OAuth for attached emails (digitizing + needlepoint)

Gmail’s browser compose URL **cannot auto-attach files**.
Attached sends use the Gmail API as `justin.eckard@jrcogolf.com`.

Drive/Sheets can stay on `info@jrco.us` via `GOOGLE_TOKEN_JSON`.
Digitizing uses a **separate** Justin Gmail token: `GOOGLE_GMAIL_TOKEN_JSON`.

## One-time setup (Justin Gmail token)

1. On a machine with the backend OAuth client file (`new.json` / `credentials.json`):

   ```bash
   cd backend
   python generate_token.py --gmail
   ```

2. Sign in as **`justin.eckard@jrcogolf.com`** (not info@) and allow Gmail.

3. Deploy:

   ```powershell
   powershell ./deploy_gmail_token.ps1
   ```

   This sets Render `GOOGLE_GMAIL_TOKEN_JSON` + `NEEDLEPOINT_GMAIL_ACCOUNT`.

4. Confirm:

   `GET /api/drive/token-status` → `gmailEmail` should be `justin.eckard@jrcogolf.com`.

## Notes

- Overview material orders still open browser compose as Justin (no attachments).
- Digitizing **Send Email** API-sends with designs auto-attached from Justin’s mailbox.
