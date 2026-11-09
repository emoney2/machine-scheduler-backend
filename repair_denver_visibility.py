"""One-shot: make Denver Country Club order 1697-1700 Image + Print Files public."""
from __future__ import annotations

import json
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(BASE, "token.json")

FILE_IDS = [
    "1wxgAWvSLE39dfYp0yYv8T5K64aDijzgG",  # 1697
    "1hTwZHLkyzgYMWqgvWV74LnEiOfTQCCnF",  # 1698
    "1os2uiHXb7WxOQrc-6eciDHBPnyj4baVh",  # 1699
    "1m9cgR4CG3zQgfkJ0m8OkgliTGXaG7cgU",  # 1700
]
FOLDER_IDS = [
    "1y_rUe03Z1UstDxeQi5gh3_8Pnfif9s5A",  # 1697 print
    "1FqWk4nso0R7s48X_czouVB1i4j8PsDyk",  # 1698 print
    "1UANnWqtpizNYx_-huH8jFp8oP_lcusi-",  # 1699 print
    "1E6GaUUAYku7E-tfCmAYjOBbb8WYClAzn",  # 1700 print
]


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


def main() -> int:
    if not os.path.exists(TOKEN_PATH) and not os.environ.get("GOOGLE_TOKEN_JSON"):
        print("No token.json / GOOGLE_TOKEN_JSON")
        return 2
    drive = build("drive", "v3", credentials=load_creds(), cache_discovery=False)
    ids = list(FILE_IDS)
    for folder_id in FOLDER_IDS:
        print(f"folder {folder_id}")
        make_public(drive, folder_id)
        ids.extend(list_children(drive, folder_id))

    seen = set()
    uniq = []
    for fid in ids:
        if fid not in seen:
            seen.add(fid)
            uniq.append(fid)

    ok_n = 0
    for fid in uniq:
        ok = make_public(drive, fid)
        print(("OK" if ok else "FAIL"), fid)
        if ok:
            ok_n += 1
    print(f"Done: {ok_n}/{len(uniq)} public")
    print("Try opening Image links again, then refresh Sheets Preview.")
    return 0 if ok_n else 1


if __name__ == "__main__":
    raise SystemExit(main())
