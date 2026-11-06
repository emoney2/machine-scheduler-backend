# Digitizing inbox (Classic Digitizers → Drive)

Cloud automation on the **Render backend** watches **info@jrco.us** for mail from
**classicdigitizers@gmail.com**, extracts the PO / order number from the subject
(e.g. `PO 1670 Jigglypuff Fairway`), and saves attachments into that order’s
existing Google Drive folder.

This runs while your computer is off and the browser is closed. It does **not**
approve digitizing or move jobs to embroidery.

## What was implemented in code

| Piece | Location |
|-------|----------|
| Matching, import, dedupe, dry-run, poller | `digitizing_inbox.py` |
| Folder resolve + HTTP APIs + Pub/Sub + cron | `server.py` |
| Gunicorn background poller | `gunicorn.conf.py` |
| Digitizing tab status / activate / dry-run / retry | `frontend/src/DigitizingList.jsx` |
| Unit tests (PO 1670 fixture) | `test_digitizing_inbox.py` |

**Not live until you configure env + Activate** (see below). Deployed code alone
does not import mail.

## Environment variables (no secrets in git)

```bash
# Master switch (default ON). Set to 0 only to disable.
DIGITIZING_INBOX_ENABLED=1

# Mailbox + vendor (defaults shown)
DIGITIZING_INBOX_EMAIL=info@jrco.us
DIGITIZING_VENDOR_EMAIL=classicdigitizers@gmail.com

# Same info@ OAuth token already used for Drive/Sheets (must include Gmail)
GOOGLE_TOKEN_JSON=...   # existing

# Optional Pub/Sub push (preferred when configured)
DIGITIZING_GMAIL_PUBSUB_TOPIC=projects/YOUR_GCP_PROJECT/topics/digitizing-gmail
DIGITIZING_INBOX_WEBHOOK_SECRET=long-random-string

# Optional Render Cron secret (falls back to SALES_CRON_SECRET)
DIGITIZING_INBOX_CRON_SECRET=long-random-string

# Polling reconcile — default every 15 minutes (min 5 minutes)
DIGITIZING_INBOX_POLL_SECONDS=900
DIGITIZING_INBOX_POLL_INITIAL_SECONDS=90

# Existing orders parent (folder named by Order #)
ORDERS_PARENT_FOLDER_ID=1n6RX0SumEipD5Nb3pUIgO5OtQFfyQXYz
```

**First email only per order:** after a successful import for order `1670`, any later
vendor message that also says PO 1670 is ignored so a wrong re-send cannot overwrite
files you already fixed.

## Google configuration you must complete

### 1. Confirm info@ Gmail API access

The importer uses **`get_google_credentials()` / `GOOGLE_TOKEN_JSON`** (info@), not
Justin’s `GOOGLE_GMAIL_TOKEN_JSON`.

Scopes already requested by the app include `gmail.modify` (read + attachments
without needing send/delete for this feature). Confirm:

1. Open `GET /api/drive/token-status` while logged into the app.
2. If Gmail calls fail with insufficient scopes, re-authorize **info@jrco.us**
   with the existing Drive + Sheets + Gmail scopes (`generate_token.py` without
   `--gmail`, signed in as **info@jrco.us**), then update Render `GOOGLE_TOKEN_JSON`.

Minimum for this feature: Gmail read + attachment download, Drive file create
in order folders, Sheets append to the log tab.

### 2. Enable APIs in the Google Cloud project

In the same GCP project as the OAuth client:

- Gmail API
- Google Drive API
- Google Sheets API
- Cloud Pub/Sub API (only if using push)

### 3. (Preferred) Gmail push via Pub/Sub

1. Create a Pub/Sub topic, e.g. `digitizing-gmail`.
2. Grant publish to `gmail-api-push@system.gserviceaccount.com` on that topic.
3. Create a **push** subscription to:

   `https://machine-scheduler-backend.onrender.com/api/digitizing/inbox/pubsub?token=YOUR_WEBHOOK_SECRET`

4. Set on Render:

   - `DIGITIZING_GMAIL_PUBSUB_TOPIC=projects/.../topics/digitizing-gmail`
   - `DIGITIZING_INBOX_WEBHOOK_SECRET=YOUR_WEBHOOK_SECRET`

5. After deploy, open Digitizing → **Activate** (or call watch renew). The
   backend calls `users.watch` and renews about daily from the poller / cron.

### 4. Always-on reconcile (required safety net)

Even with Pub/Sub, the gunicorn poller (when `DIGITIZING_INBOX_ENABLED=1`) checks
about every 60s and recovers missed notifications.

Optional Render Cron (every 5–15 minutes):

`GET/POST /api/digitizing/inbox/cron`  
Header: `X-Digitizing-Inbox-Cron-Secret: ...`  
Add `?renewWatch=1` once per day if you want an explicit watch renew.

### 5. Keep it running

1. Deploy backend + frontend.
2. Leave `DIGITIZING_INBOX_ENABLED` unset or `1` (default on).
3. The Digitizing tab shows **Last polled** only — no activate/pause controls.

The poller auto-starts with the Render web service and records `activatedAt` once.
Only messages after that first start are considered (no automatic historical backfill).

API (session cookie required except pubsub/cron):

- `GET /api/digitizing/inbox/status` — includes `lastPolledAt`
- `POST /api/digitizing/inbox/run` `{ "dryRun": true }` — optional ops use

## Matching rules (deterministic)

- Exact From address `classicdigitizers@gmail.com`
- Subject PO number (strips Re:/Fwd:, spacing, `PO#` / `PO-`)
- At least one `.EMB` or `.DST` attachment (any case; octet-stream OK)
- Skips signature/tracking image names
- Flags for review: missing/ambiguous PO, filename PO conflict, missing or
  conflicting Drive folders (does **not** create a guessed folder)
- Order folder: Drive child named `Order #` under `ORDERS_PARENT_FOLDER_ID`,
  cross-checked with Production Orders `Order Folder Link` / `Folder Link`

## Deduping

State file `backend/data/digitizing_inbox.json` (+ Sheet tab **Digitizing Inbox**):

- Per Gmail message id + attachment
- Content hash per order skips identical re-imports
- Same filename, different bytes → `name (rev 2).ext`
- Partial failures resume without re-uploading completed files

## Verified locally

```text
python -m unittest test_digitizing_inbox.py -v
# 16 tests OK — PO 1670 subject/files, vendor filter, uppercase exts,
# separate-thread subject, ambiguous PO, revision names, dry-run fixture shape
```

Live Gmail/Drive import is **not** running until Render env + **Activate** are done.

## Hosting note

Pub/Sub push is supported when the topic + webhook secret are set. Render’s web
service also runs a **~60s background poller** (same pattern as wholesale signup)
so the feature works even before Pub/Sub is fully wired. Do not rely on a browser
timer.
