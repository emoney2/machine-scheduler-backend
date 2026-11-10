"""Make Image + Print Files publicly viewable for given Production Order #s.

Usage:
  python repair_order_visibility.py 1702 1703 1697 1698 1699 1700

Fixes blank Sheets IMAGE() previews and empty Drive links when anyone-reader
was missing after a JR presentation / PDF order submit.
"""
from __future__ import annotations

import json
import os
import re
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(BASE, "token.json")
def _load_dotenv() -> None:
    for path in (os.path.join(BASE, ".env"), os.path.join(BASE, "..", ".env")):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass


_load_dotenv()
SPREADSHEET_ID = (os.environ.get("SPREADSHEET_ID") or "").strip()


def load_creds() -> Credentials:
    env = (os.environ.get("GOOGLE_TOKEN_JSON") or "").strip()
    info = json.loads(env) if env else None
    if not info:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            info = json.load(f)
    creds = Credentials.from_authorized_user_info(info)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def make_public(drive, file_id: str) -> bool:
    try:
        perms = (
            drive.permissions()
            .list(
                fileId=file_id,
                fields="permissions(id,type,role)",
                supportsAllDrives=True,
            )
            .execute()
        )
        if any(
            p.get("type") == "anyone"
            and p.get("role") in ("reader", "commenter", "writer")
            for p in (perms.get("permissions") or [])
        ):
            return True
        drive.permissions().create(
            fileId=file_id,
            body={"role": "reader", "type": "anyone"},
            supportsAllDrives=True,
            fields="id",
        ).execute()
        return True
    except Exception as e:
        print(f"FAIL {file_id}: {e}")
        return False


def extract_file_id(text: str) -> str | None:
    s = str(text or "")
    m = re.search(r"/file/d/([A-Za-z0-9_-]{10,})", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", s)
    if m:
        return m.group(1)
    return None


def extract_folder_id(text: str) -> str | None:
    m = re.search(r"/folders/([A-Za-z0-9_-]{10,})", str(text or ""))
    return m.group(1) if m else None


def list_children(drive, folder_id: str) -> list[str]:
    out: list[str] = []
    page_token = None
    while True:
        resp = (
            drive.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, mimeType, name)",
                pageSize=100,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        for f in resp.get("files") or []:
            fid = f.get("id")
            if not fid:
                continue
            out.append(fid)
            print(f"  child {f.get('name')} ({fid})")
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def main(argv: list[str]) -> int:
    order_ids = [a.strip().replace(",", "") for a in argv if a.strip()]
    if not order_ids:
        print(__doc__)
        return 2
    if not SPREADSHEET_ID:
        print("SPREADSHEET_ID missing (set in env or backend/.env)")
        return 2
    if not os.path.exists(TOKEN_PATH) and not os.environ.get("GOOGLE_TOKEN_JSON"):
        print("No token.json / GOOGLE_TOKEN_JSON")
        return 2

    creds = load_creds()
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

    print(f"Spreadsheet: {SPREADSHEET_ID}")
    result = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range="Production Orders!A1:AZ")
        .execute()
    )
    rows = result.get("values") or []
    if not rows:
        print("Production Orders empty")
        return 1
    headers = rows[0]
    wanted = set(order_ids)
    file_ids: list[str] = []
    folder_ids: list[str] = []

    for r in rows[1:]:
        padded = list(r) + [""] * max(0, len(headers) - len(r))
        row = dict(zip(headers, padded))
        oid = str(row.get("Order #") or "").strip().replace(",", "")
        if oid not in wanted:
            continue
        company = row.get("Company Name") or ""
        print(f"Order {oid} ({company})")
        img = str(row.get("Image") or "").strip()
        for part in re.split(r"[\s,]+", img):
            fid = extract_file_id(part) or extract_file_id(img)
            if fid and fid not in file_ids:
                file_ids.append(fid)
                print(f"  Image file {fid}")
        pf = str(row.get("Print Files") or "").strip()
        folder = extract_folder_id(pf)
        if folder and folder not in folder_ids:
            folder_ids.append(folder)
            print(f"  Print folder {folder}")

    found: set[str] = set()
    for r in rows[1:]:
        padded = list(r) + [""] * max(0, len(headers) - len(r))
        row = dict(zip(headers, padded))
        oid = str(row.get("Order #") or "").strip().replace(",", "")
        if oid in wanted:
            found.add(oid)
    missing = wanted - found
    if missing:
        print(f"WARNING: orders not found in sheet: {sorted(missing)}")

    for folder_id in folder_ids:
        print(f"folder {folder_id}")
        make_public(drive, folder_id)
        file_ids.extend(list_children(drive, folder_id))

    seen: set[str] = set()
    uniq: list[str] = []
    for fid in file_ids:
        if fid not in seen:
            seen.add(fid)
            uniq.append(fid)

    if not uniq:
        print("No Drive file ids resolved — nothing to repair")
        return 1

    ok_n = 0
    for fid in uniq:
        ok = make_public(drive, fid)
        print(("OK" if ok else "FAIL"), fid)
        if ok:
            ok_n += 1
    print(f"Done: {ok_n}/{len(uniq)} public")
    print("Refresh the Google Sheet — Preview should show again.")
    return 0 if ok_n == len(uniq) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
