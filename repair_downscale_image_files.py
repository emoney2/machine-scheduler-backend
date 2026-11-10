"""Downscale oversized Production Orders Image PNGs in place (same Drive file id).

JR presentation flats are often 50–100MB; Sheets IMAGE() and Drive preview fail.
Print Files copies are left untouched.
"""
from __future__ import annotations

import io
import json
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from PIL import Image

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(BASE, "token.json")
MAX_EDGE = 2400
MAX_BYTES = 6 * 1024 * 1024

# Order # → Image Drive file id (from sheet at time of repair)
DEFAULT_IDS = [
    ("1697", "1wxgAWvSLE39dfYp0yYv8T5K64aDijzgG"),
    ("1698", "1hTwZHLkyzgYMWqgvWV74LnEiOfTQCCnF"),
    ("1699", "1os2uiHXb7WxOQrc-6eciDHBPnyj4baVh"),
    ("1700", "1m9cgR4CG3zQgfkJ0m8OkgliTGXaG7cgU"),
    ("1702", "1ywQaLJCr91juEZY4ZG8hbKTzWjbZ5QPh"),
    ("1703", "1NKNuiIXJZ83WOLNSsSziJTL8ur-UxWbX"),
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


def download(drive, file_id: str) -> bytes:
    req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def downscale(data: bytes) -> tuple[bytes, bool]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, False
    im = Image.open(io.BytesIO(data))
    im.load()
    w, h = im.size
    long_edge = max(w, h) or 1
    if long_edge <= MAX_EDGE and len(data) <= MAX_BYTES:
        return data, False
    if long_edge > MAX_EDGE:
        scale = float(MAX_EDGE) / float(long_edge)
        im = im.resize(
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
            Image.Resampling.LANCZOS,
        )
    if im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGBA" if "A" in (im.mode or "") else "RGB")
    out = io.BytesIO()
    im.save(out, format="PNG", optimize=True)
    shrunk = out.getvalue()
    if shrunk and len(shrunk) < len(data):
        return shrunk, True
    return data, False


def ensure_public(drive, file_id: str) -> None:
    perms = (
        drive.permissions()
        .list(
            fileId=file_id,
            fields="permissions(type,role)",
            supportsAllDrives=True,
        )
        .execute()
    )
    if any(p.get("type") == "anyone" for p in (perms.get("permissions") or [])):
        return
    drive.permissions().create(
        fileId=file_id,
        body={"role": "reader", "type": "anyone"},
        supportsAllDrives=True,
        fields="id",
    ).execute()


def main() -> int:
    drive = build("drive", "v3", credentials=load_creds(), cache_discovery=False)
    for oid, fid in DEFAULT_IDS:
        meta = (
            drive.files()
            .get(
                fileId=fid,
                fields="name,size,mimeType",
                supportsAllDrives=True,
            )
            .execute()
        )
        print(
            f"Order {oid}: downloading {meta.get('name')} "
            f"({meta.get('size')} bytes)..."
        )
        raw = download(drive, fid)
        im = Image.open(io.BytesIO(raw))
        im.load()
        print(f"  dims {im.size[0]}x{im.size[1]}")
        new, changed = downscale(raw)
        if changed:
            print(f"  {len(raw)} -> {len(new)} bytes; uploading...")
            media = MediaIoBaseUpload(
                io.BytesIO(new), mimetype="image/png", resumable=False
            )
            drive.files().update(
                fileId=fid, media_body=media, supportsAllDrives=True
            ).execute()
            ensure_public(drive, fid)
            print(f"  UPDATED {oid}")
        else:
            ensure_public(drive, fid)
            print(f"  skip shrink {oid} (already small enough)")
    print("done — refresh the Google Sheet Preview column")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
