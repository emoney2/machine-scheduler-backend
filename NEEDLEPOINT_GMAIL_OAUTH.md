# Needlepoint belt emails — Gmail OAuth (no password)

Vendor emails need a **Gmail draft with PDF attachments**.  
Gmail’s compose URL cannot attach files. Storing a Gmail password is not required.

## One-time setup

1. On a machine with the backend OAuth client file (`new.json` / `credentials.json`):

   ```bash
   cd backend
   python generate_token.py
   ```

2. Sign in as `justin.eckard@jrcogolf.com` and allow **Drive, Sheets, and Gmail**.

3. Copy the new `token.json` into production:
   - Set env `GOOGLE_TOKEN_JSON` to the file contents, **or**
   - Replace the server’s `token.json`

4. Restart the backend.

5. Remove `NEEDLEPOINT_GMAIL_APP_PASSWORD` / `NEEDLEPOINT_IMAP_PASSWORD` if present.

## After that

**Create email** opens a real Gmail **draft** with `1621.pdf` (etc.) already attached. Review and Send.
