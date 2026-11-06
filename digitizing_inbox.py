"""
Import embroidery digitizing files emailed by Classic Digitizers into order Drive folders.

Runs on the Render backend (computer can be off):
  - optional Gmail users.watch + Pub/Sub push
  - always-on reconciliation poller (~60s) + daily watch renew

Mailbox: info@jrco.us (GOOGLE_TOKEN_JSON)
Vendor: classicdigitizers@gmail.com
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

VENDOR_EMAIL = (
    os.environ.get("DIGITIZING_VENDOR_EMAIL") or "classicdigitizers@gmail.com"
).strip().lower()
MAILBOX_EMAIL = (
    os.environ.get("DIGITIZING_INBOX_EMAIL")
    or os.environ.get("DESIGN_CONFIRMATION_FROM_EMAIL")
    or "info@jrco.us"
).strip().lower()

REQUIRED_EXTS = {".emb", ".dst"}
KEEP_EXTS = {".emb", ".dst", ".pdf", ".png", ".jpg", ".jpeg", ".zip", ".ofm", ".pxf"}
SIGNATURE_NAME_RE = re.compile(
    r"^(image|img|logo|signature|banner|spacer|pixel|track|icon)[\d_-]*\.",
    re.I,
)
PO_SUBJECT_RE = re.compile(
    r"(?:^|[\s\[\(\{])PO\s*[:#\-]?\s*(\d+)\b",
    re.I,
)
PO_FILENAME_RE = re.compile(r"\bPO\s*[:#\-]?\s*(\d+)\b", re.I)
REPLY_PREFIX_RE = re.compile(r"^((re|fw|fwd)\s*:\s*)+", re.I)

_BASE = Path(__file__).resolve().parent
DATA_DIR = _BASE / "data"
STATE_PATH = DATA_DIR / "digitizing_inbox.json"
SHEET_TAB = os.environ.get("DIGITIZING_INBOX_TAB", "Digitizing Inbox")
SHEET_HEADERS = [
    "Message Id",
    "Order #",
    "Status",
    "Received At",
    "Subject",
    "Filenames",
    "Drive File Ids",
    "Error",
    "Updated At",
]

_LOCK = threading.RLock()
_started = False
_start_lock = threading.Lock()

# Injected from server.py to avoid circular imports at import time.
_services: Dict[str, Any] = {}


def configure(**kwargs) -> None:
    """Wire live Google helpers from server.py."""
    _services.update(kwargs)


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_order_id(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    try:
        return str(int(float(s.replace(",", ""))))
    except ValueError:
        return s


def enabled() -> bool:
    flag = (os.environ.get("DIGITIZING_INBOX_ENABLED") or "0").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def poll_interval_seconds() -> int:
    try:
        seconds = int(os.environ.get("DIGITIZING_INBOX_POLL_SECONDS") or "60")
    except ValueError:
        seconds = 60
    return max(30, seconds)


def pubsub_topic() -> str:
    return (os.environ.get("DIGITIZING_GMAIL_PUBSUB_TOPIC") or "").strip()


def webhook_secret() -> str:
    return (os.environ.get("DIGITIZING_INBOX_WEBHOOK_SECRET") or "").strip()


def cron_secret() -> str:
    return (
        os.environ.get("DIGITIZING_INBOX_CRON_SECRET")
        or os.environ.get("SALES_CRON_SECRET")
        or ""
    ).strip()


# ---------- Pure matching helpers (unit-tested) ----------


def extract_sender_email(from_header: str) -> str:
    _name, addr = parseaddr(str(from_header or ""))
    return (addr or "").strip().lower()


def is_vendor_sender(from_header: str, vendor: str = VENDOR_EMAIL) -> bool:
    return extract_sender_email(from_header) == (vendor or "").strip().lower()


def parse_po_from_subject(subject: str) -> Dict[str, Any]:
    """
    Extract order number after PO in the subject.
    Returns {ok, orderNumber, reason, candidates}.
    """
    raw = str(subject or "").strip()
    cleaned = REPLY_PREFIX_RE.sub("", raw).strip()
    hits = [normalize_order_id(m.group(1)) for m in PO_SUBJECT_RE.finditer(f" {cleaned}")]
    hits = [h for h in hits if h]
    uniq = []
    for h in hits:
        if h not in uniq:
            uniq.append(h)
    if not uniq:
        return {
            "ok": False,
            "orderNumber": "",
            "reason": "missing_po",
            "candidates": [],
        }
    if len(uniq) > 1:
        return {
            "ok": False,
            "orderNumber": "",
            "reason": "ambiguous_po",
            "candidates": uniq,
        }
    return {
        "ok": True,
        "orderNumber": uniq[0],
        "reason": "",
        "candidates": uniq,
    }


def parse_po_from_filename(filename: str) -> Optional[str]:
    m = PO_FILENAME_RE.search(str(filename or ""))
    if not m:
        return None
    return normalize_order_id(m.group(1)) or None


def _ext(filename: str) -> str:
    name = str(filename or "").strip()
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def is_signature_or_tracking_filename(filename: str) -> bool:
    name = str(filename or "").strip()
    if not name:
        return True
    if SIGNATURE_NAME_RE.match(name):
        return True
    # Tiny tracking-style names without a design stem.
    stem = name.rsplit(".", 1)[0].strip().lower()
    if stem in {"logo", "signature", "banner", "spacer", "pixel", "track"}:
        return True
    return False


def should_keep_attachment(filename: str, mime: str = "") -> bool:
    name = str(filename or "").strip()
    if not name:
        return False
    if is_signature_or_tracking_filename(name):
        return False
    ext = _ext(name)
    mime_l = str(mime or "").lower()
    if ext in KEEP_EXTS:
        return True
    if ext in {".png", ".jpg", ".jpeg", ".gif"} and "image/" in mime_l:
        # Keep design-like image names; skip anonymous image parts.
        return bool(PO_FILENAME_RE.search(name) or len(name) > 12)
    if mime_l in {
        "application/octet-stream",
        "application/pdf",
        "application/zip",
    } and ext:
        return True
    return False


def message_has_required_design_file(filenames: Iterable[str]) -> bool:
    for name in filenames or []:
        if _ext(name) in REQUIRED_EXTS:
            return True
    return False


def attachment_po_conflict(filenames: Iterable[str], order_number: str) -> Optional[str]:
    oid = normalize_order_id(order_number)
    for name in filenames or []:
        other = parse_po_from_filename(name)
        if other and oid and other != oid:
            return f"Attachment {name!r} references PO {other}, expected {oid}"
    return None


def revision_filename(existing_names: Iterable[str], filename: str) -> str:
    """Pick a unique revision name when content changed but basename exists."""
    names = {str(n) for n in (existing_names or [])}
    if filename not in names:
        return filename
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    else:
        ext = "." + ext
    n = 2
    while True:
        candidate = f"{stem} (rev {n}){ext}"
        if candidate not in names:
            return candidate
        n += 1


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


# ---------- Durable state ----------


def _default_state() -> Dict[str, Any]:
    return {
        "enabled": False,
        "activatedAt": "",
        "historyId": "",
        "watchExpiration": "",
        "lastCheckAt": "",
        "lastSuccessAt": "",
        "lastError": "",
        "processedMessages": {},
        "orderFileHashes": {},  # orderNumber -> {sha256: filename}
        "orderFilenames": {},  # orderNumber -> [filenames in Drive we know]
    }


def load_state() -> Dict[str, Any]:
    with _LOCK:
        if not STATE_PATH.exists():
            return _default_state()
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return _default_state()
            base = _default_state()
            base.update(data)
            if not isinstance(base.get("processedMessages"), dict):
                base["processedMessages"] = {}
            if not isinstance(base.get("orderFileHashes"), dict):
                base["orderFileHashes"] = {}
            if not isinstance(base.get("orderFilenames"), dict):
                base["orderFilenames"] = {}
            return base
        except Exception:
            logger.exception("Failed to read digitizing inbox state")
            return _default_state()


def save_state(state: Dict[str, Any]) -> None:
    with _LOCK:
        _ensure_data_dir()
        tmp = STATE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        tmp.replace(STATE_PATH)


def activate(at_iso: Optional[str] = None) -> Dict[str, Any]:
    state = load_state()
    state["enabled"] = True
    if not state.get("activatedAt"):
        state["activatedAt"] = at_iso or _now_iso()
    save_state(state)
    return state


def deactivate() -> Dict[str, Any]:
    state = load_state()
    state["enabled"] = False
    save_state(state)
    return state


def status_snapshot() -> Dict[str, Any]:
    state = load_state()
    msgs = state.get("processedMessages") or {}
    imported = []
    needs_review = []
    errors = []
    for mid, row in msgs.items():
        item = {
            "messageId": mid,
            **(row if isinstance(row, dict) else {}),
        }
        st = str(item.get("status") or "")
        if st == "imported":
            imported.append(item)
        elif st == "needs_review":
            needs_review.append(item)
        elif st in {"error", "partial"}:
            errors.append(item)

    def _sort_key(row):
        return str(row.get("receivedAt") or row.get("updatedAt") or "")

    imported.sort(key=_sort_key, reverse=True)
    needs_review.sort(key=_sort_key, reverse=True)
    errors.sort(key=_sort_key, reverse=True)
    return {
        "ok": True,
        "featureEnabledEnv": enabled(),
        "activated": bool(state.get("enabled") and state.get("activatedAt")),
        "activatedAt": state.get("activatedAt") or "",
        "historyId": state.get("historyId") or "",
        "watchExpiration": state.get("watchExpiration") or "",
        "pubsubTopicConfigured": bool(pubsub_topic()),
        "lastCheckAt": state.get("lastCheckAt") or "",
        "lastSuccessAt": state.get("lastSuccessAt") or "",
        "lastError": state.get("lastError") or "",
        "mailbox": MAILBOX_EMAIL,
        "vendorEmail": VENDOR_EMAIL,
        "imported": imported[:50],
        "needsReview": needs_review[:50],
        "errors": errors[:50],
        "importedOrderNumbers": sorted(
            {
                normalize_order_id(r.get("orderNumber"))
                for r in imported
                if normalize_order_id(r.get("orderNumber"))
            }
        ),
    }


# ---------- Gmail / Drive helpers ----------


def _gmail():
    factory = _services.get("get_gmail")
    if not factory:
        raise RuntimeError("digitizing_inbox not configured (get_gmail)")
    return factory()


def _drive():
    factory = _services.get("get_drive")
    if not factory:
        raise RuntimeError("digitizing_inbox not configured (get_drive)")
    return factory()


def _resolve_folder(order_number: str) -> Dict[str, Any]:
    fn = _services.get("resolve_order_folder")
    if not fn:
        raise RuntimeError("digitizing_inbox not configured (resolve_order_folder)")
    return fn(normalize_order_id(order_number))


def _append_sheet_row(row: List[Any]) -> None:
    fn = _services.get("append_inbox_sheet_row")
    if fn:
        try:
            fn(row)
        except Exception:
            logger.exception("Failed to append Digitizing Inbox sheet row")


def _gmail_header(payload: dict, name: str) -> str:
    want = name.lower()
    for h in payload.get("headers") or []:
        if str(h.get("name") or "").lower() == want:
            return str(h.get("value") or "")
    return ""


def _walk_parts(payload: dict) -> List[dict]:
    out = []
    if not payload:
        return out
    stack = [payload]
    while stack:
        part = stack.pop()
        out.append(part)
        for child in part.get("parts") or []:
            stack.append(child)
    return out


def list_message_attachments(message: dict) -> List[Dict[str, Any]]:
    """Return candidate file attachments from a full Gmail message resource."""
    payload = message.get("payload") or {}
    found = []
    for part in _walk_parts(payload):
        filename = str(part.get("filename") or "").strip()
        body = part.get("body") or {}
        att_id = str(body.get("attachmentId") or "").strip()
        mime = str(part.get("mimeType") or "")
        headers = {
            str(h.get("name") or "").lower(): str(h.get("value") or "")
            for h in (part.get("headers") or [])
        }
        disposition = headers.get("content-disposition", "")
        if not filename and not att_id:
            continue
        if not filename:
            # Inline body blobs without names — skip.
            continue
        if "attachment" not in disposition.lower() and _ext(filename) not in KEEP_EXTS:
            if is_signature_or_tracking_filename(filename):
                continue
        if not should_keep_attachment(filename, mime):
            continue
        found.append(
            {
                "filename": filename,
                "mime": mime,
                "attachmentId": att_id,
                "size": int(body.get("size") or 0),
                "partId": str(part.get("partId") or ""),
            }
        )
    return found


def download_attachment(gmail, message_id: str, attachment_id: str) -> bytes:
    if not attachment_id:
        return b""
    resp = (
        gmail.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    data = resp.get("data") or ""
    # Gmail uses URL-safe base64.
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _mark_message(
    state: Dict[str, Any],
    message_id: str,
    *,
    status: str,
    order_number: str = "",
    subject: str = "",
    received_at: str = "",
    error: str = "",
    attachments: Optional[Dict[str, Any]] = None,
    filenames: Optional[List[str]] = None,
    drive_file_ids: Optional[List[str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    row = {
        "status": status,
        "orderNumber": normalize_order_id(order_number),
        "subject": subject,
        "receivedAt": received_at,
        "updatedAt": _now_iso(),
        "error": error,
        "attachments": attachments or {},
        "filenames": filenames or [],
        "driveFileIds": drive_file_ids or [],
        "dryRun": bool(dry_run),
        "digitizingFilesReceived": status == "imported",
    }
    if not dry_run:
        state.setdefault("processedMessages", {})[message_id] = row
        _append_sheet_row(
            [
                message_id,
                row["orderNumber"],
                status,
                received_at,
                subject,
                ", ".join(filenames or []),
                ", ".join(drive_file_ids or []),
                error,
                row["updatedAt"],
            ]
        )
    return row


def evaluate_message_metadata(
    *,
    from_header: str,
    subject: str,
    attachment_filenames: List[str],
) -> Dict[str, Any]:
    """Deterministic gate before Drive work. Pure — used by dry-run + tests."""
    if not is_vendor_sender(from_header):
        return {
            "qualify": False,
            "skip": True,
            "reason": "wrong_sender",
            "orderNumber": "",
        }
    po = parse_po_from_subject(subject)
    if not po.get("ok"):
        return {
            "qualify": False,
            "skip": False,
            "needsReview": True,
            "reason": po.get("reason") or "bad_po",
            "orderNumber": "",
            "candidates": po.get("candidates") or [],
        }
    order_number = po["orderNumber"]
    if not message_has_required_design_file(attachment_filenames):
        return {
            "qualify": False,
            "skip": True,
            "reason": "no_emb_or_dst",
            "orderNumber": order_number,
        }
    conflict = attachment_po_conflict(attachment_filenames, order_number)
    if conflict:
        return {
            "qualify": False,
            "skip": False,
            "needsReview": True,
            "reason": "filename_po_conflict",
            "error": conflict,
            "orderNumber": order_number,
        }
    return {
        "qualify": True,
        "skip": False,
        "orderNumber": order_number,
        "reason": "",
    }


def process_gmail_message(
    message: dict,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """Process one full Gmail message resource."""
    gmail = _gmail()
    message_id = str(message.get("id") or "").strip()
    if not message_id:
        return {"ok": False, "error": "missing message id"}

    state = load_state()
    existing = (state.get("processedMessages") or {}).get(message_id)
    if existing and not force and not dry_run:
        if existing.get("status") == "imported":
            return {"ok": True, "alreadyProcessed": True, "result": existing}
        if existing.get("status") == "needs_review" and not force:
            return {"ok": True, "alreadyProcessed": True, "result": existing}

    payload = message.get("payload") or {}
    from_header = _gmail_header(payload, "From")
    subject = _gmail_header(payload, "Subject")
    date_hdr = _gmail_header(payload, "Date")
    internal_ms = message.get("internalDate")
    received_at = ""
    if internal_ms:
        try:
            received_at = datetime.fromtimestamp(
                int(internal_ms) / 1000.0, tz=timezone.utc
            ).isoformat()
        except Exception:
            received_at = date_hdr
    else:
        received_at = date_hdr

    # Activation cutoff (default: only after activate).
    activated_at = str(state.get("activatedAt") or "").strip()
    if activated_at and received_at and not force and not dry_run:
        try:
            act_dt = datetime.fromisoformat(activated_at.replace("Z", "+00:00"))
            recv_dt = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
            if recv_dt.tzinfo is None:
                recv_dt = recv_dt.replace(tzinfo=timezone.utc)
            if act_dt.tzinfo is None:
                act_dt = act_dt.replace(tzinfo=timezone.utc)
            if recv_dt < act_dt:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "before_activation",
                    "messageId": message_id,
                }
        except Exception:
            pass

    attachments_meta = list_message_attachments(message)
    filenames = [a["filename"] for a in attachments_meta]
    gate = evaluate_message_metadata(
        from_header=from_header,
        subject=subject,
        attachment_filenames=filenames,
    )
    if gate.get("skip"):
        return {
            "ok": True,
            "skipped": True,
            "reason": gate.get("reason"),
            "messageId": message_id,
            "orderNumber": gate.get("orderNumber") or "",
        }
    if gate.get("needsReview"):
        row = _mark_message(
            state,
            message_id,
            status="needs_review",
            order_number=gate.get("orderNumber") or "",
            subject=subject,
            received_at=received_at,
            error=gate.get("error") or gate.get("reason") or "needs_review",
            filenames=filenames,
            dry_run=dry_run,
        )
        if not dry_run:
            save_state(state)
        return {"ok": True, "needsReview": True, "result": row, "gate": gate}

    order_number = normalize_order_id(gate.get("orderNumber"))
    folder = _resolve_folder(order_number)
    if not folder.get("ok"):
        row = _mark_message(
            state,
            message_id,
            status="needs_review",
            order_number=order_number,
            subject=subject,
            received_at=received_at,
            error=folder.get("error") or "folder_unresolved",
            filenames=filenames,
            dry_run=dry_run,
        )
        if not dry_run:
            save_state(state)
        return {
            "ok": True,
            "needsReview": True,
            "result": row,
            "folder": folder,
            "dryRun": dry_run,
            "proposed": {
                "orderNumber": order_number,
                "folderId": folder.get("folderId") or "",
                "filenames": filenames,
            },
        }

    folder_id = folder["folderId"]
    proposed = {
        "orderNumber": order_number,
        "folderId": folder_id,
        "folderLink": folder.get("folderLink") or "",
        "filenames": filenames,
    }
    if dry_run:
        return {
            "ok": True,
            "dryRun": True,
            "qualify": True,
            "proposed": proposed,
            "folder": folder,
            "messageId": message_id,
            "subject": subject,
            "from": extract_sender_email(from_header),
        }

    # Resume-aware upload per attachment.
    prior_atts = {}
    if existing and isinstance(existing.get("attachments"), dict):
        prior_atts = dict(existing["attachments"])

    drive = _drive()
    order_hashes = dict((state.get("orderFileHashes") or {}).get(order_number) or {})
    known_names = set((state.get("orderFilenames") or {}).get(order_number) or [])
    # Also list current Drive children for revision naming.
    try:
        children = (
            drive.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="files(id, name)",
                pageSize=200,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
            .get("files")
            or []
        )
        for ch in children:
            if ch.get("name"):
                known_names.add(ch["name"])
    except Exception:
        logger.exception("Could not list Drive children for order %s", order_number)

    att_results: Dict[str, Any] = dict(prior_atts)
    uploaded_ids: List[str] = []
    saved_names: List[str] = []
    failures: List[str] = []

    for att in attachments_meta:
        filename = att["filename"]
        att_key = att.get("attachmentId") or f"{att.get('partId')}:{filename}"
        prior = att_results.get(att_key) or {}
        if prior.get("status") in {"uploaded", "skipped_duplicate"} and prior.get(
            "driveFileId"
        ):
            uploaded_ids.append(prior["driveFileId"])
            saved_names.append(prior.get("savedAs") or filename)
            continue
        if prior.get("status") == "skipped_duplicate":
            saved_names.append(prior.get("savedAs") or filename)
            continue

        try:
            raw = download_attachment(gmail, message_id, att.get("attachmentId") or "")
            if not raw:
                raise RuntimeError("empty attachment body")
            digest = sha256_bytes(raw)
            if digest in order_hashes:
                att_results[att_key] = {
                    "status": "skipped_duplicate",
                    "filename": filename,
                    "savedAs": order_hashes[digest],
                    "sha256": digest,
                    "driveFileId": "",
                }
                saved_names.append(order_hashes[digest])
                continue

            save_as = filename
            # Same name, different content → revision.
            if filename in known_names:
                save_as = revision_filename(known_names, filename)

            from googleapiclient.http import MediaInMemoryUpload

            media = MediaInMemoryUpload(
                raw,
                mimetype=att.get("mime") or "application/octet-stream",
                resumable=False,
            )
            created = (
                drive.files()
                .create(
                    body={"name": save_as, "parents": [folder_id]},
                    media_body=media,
                    fields="id, name, webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
            file_id = created.get("id") or ""
            att_results[att_key] = {
                "status": "uploaded",
                "filename": filename,
                "savedAs": save_as,
                "sha256": digest,
                "driveFileId": file_id,
                "webViewLink": created.get("webViewLink") or "",
            }
            # Persist after each successful upload so a later crash can resume.
            order_hashes[digest] = save_as
            known_names.add(save_as)
            state.setdefault("orderFileHashes", {})[order_number] = order_hashes
            state.setdefault("orderFilenames", {})[order_number] = sorted(known_names)
            state.setdefault("processedMessages", {})[message_id] = {
                "status": "partial",
                "orderNumber": order_number,
                "subject": subject,
                "receivedAt": received_at,
                "updatedAt": _now_iso(),
                "error": "",
                "attachments": att_results,
                "filenames": [a["filename"] for a in attachments_meta],
                "driveFileIds": [
                    v.get("driveFileId")
                    for v in att_results.values()
                    if v.get("driveFileId")
                ],
                "digitizingFilesReceived": False,
            }
            save_state(state)

            uploaded_ids.append(file_id)
            saved_names.append(save_as)
        except Exception as exc:
            logger.exception(
                "Digitizing inbox upload failed msg=%s file=%s", message_id, filename
            )
            att_results[att_key] = {
                "status": "error",
                "filename": filename,
                "error": str(exc),
            }
            failures.append(f"{filename}: {exc}")

    if failures:
        row = _mark_message(
            state,
            message_id,
            status="partial" if any(
                v.get("status") == "uploaded" for v in att_results.values()
            ) else "error",
            order_number=order_number,
            subject=subject,
            received_at=received_at,
            error="; ".join(failures),
            attachments=att_results,
            filenames=saved_names or filenames,
            drive_file_ids=uploaded_ids,
        )
        state["lastError"] = row["error"]
        save_state(state)
        return {"ok": False, "result": row, "proposed": proposed}

    row = _mark_message(
        state,
        message_id,
        status="imported",
        order_number=order_number,
        subject=subject,
        received_at=received_at,
        error="",
        attachments=att_results,
        filenames=saved_names,
        drive_file_ids=uploaded_ids,
    )
    state["lastSuccessAt"] = _now_iso()
    state["lastError"] = ""
    save_state(state)
    return {"ok": True, "result": row, "proposed": proposed}


def _search_vendor_message_ids(gmail, *, after_epoch: Optional[int] = None, max_results: int = 50) -> List[str]:
    query = f"from:{VENDOR_EMAIL} has:attachment"
    if after_epoch:
        query += f" after:{int(after_epoch)}"
    ids = []
    resp = (
        gmail.users()
        .messages()
        .list(userId="me", q=query, maxResults=min(max_results, 100))
        .execute()
    )
    for m in resp.get("messages") or []:
        if m.get("id"):
            ids.append(m["id"])
    return ids


def run_pass(*, dry_run: bool = False, max_messages: int = 40, force_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Poll recent vendor emails and process qualifying ones."""
    state = load_state()
    if not dry_run:
        state["lastCheckAt"] = _now_iso()
        save_state(state)

    if not enabled() and not dry_run and not force_ids:
        return {"ok": True, "skipped": True, "reason": "env_disabled"}
    if not state.get("enabled") and not dry_run and not force_ids:
        return {"ok": True, "skipped": True, "reason": "not_activated"}

    gmail = _gmail()
    processed = []
    after_epoch = None
    activated_at = str(state.get("activatedAt") or "").strip()
    if activated_at and not force_ids:
        try:
            after_epoch = int(
                datetime.fromisoformat(activated_at.replace("Z", "+00:00")).timestamp()
            ) - 86400
        except Exception:
            after_epoch = None

    ids = force_ids or _search_vendor_message_ids(
        gmail, after_epoch=after_epoch, max_results=max_messages
    )
    for mid in ids:
        try:
            msg = (
                gmail.users()
                .messages()
                .get(userId="me", id=mid, format="full")
                .execute()
            )
            result = process_gmail_message(msg, dry_run=dry_run)
            processed.append({"messageId": mid, **result})
        except Exception as exc:
            logger.exception("Digitizing inbox failed on message %s", mid)
            processed.append({"messageId": mid, "ok": False, "error": str(exc)})

    # Advance historyId from profile for watch sync.
    try:
        profile = gmail.users().getProfile(userId="me").execute() or {}
        hid = str(profile.get("historyId") or "")
        if hid and not dry_run:
            state = load_state()
            state["historyId"] = hid
            state["lastCheckAt"] = _now_iso()
            save_state(state)
    except Exception:
        logger.warning("Could not refresh Gmail historyId")

    return {
        "ok": True,
        "dryRun": dry_run,
        "checked": len(ids),
        "results": processed,
    }


def renew_watch() -> Dict[str, Any]:
    topic = pubsub_topic()
    if not topic:
        return {"ok": False, "error": "DIGITIZING_GMAIL_PUBSUB_TOPIC not set"}
    gmail = _gmail()
    body = {
        "topicName": topic,
        "labelIds": ["INBOX"],
        "labelFilterBehavior": "INCLUDE",
    }
    resp = gmail.users().watch(userId="me", body=body).execute() or {}
    state = load_state()
    state["historyId"] = str(resp.get("historyId") or state.get("historyId") or "")
    state["watchExpiration"] = str(resp.get("expiration") or "")
    state["lastCheckAt"] = _now_iso()
    save_state(state)
    return {"ok": True, "watch": resp, "topic": topic}


def handle_pubsub_push(envelope: dict) -> Dict[str, Any]:
    """
    Handle a Pub/Sub push body. Returns quickly after queueing a reconcile pass.
    Notification is only 'done' after run_pass completes (caller may ack after).
    """
    message = (envelope or {}).get("message") or {}
    data_b64 = message.get("data") or ""
    history_id = ""
    email_address = ""
    if data_b64:
        try:
            pad = "=" * (-len(data_b64) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(data_b64 + pad).decode("utf-8"))
            history_id = str(decoded.get("historyId") or "")
            email_address = str(decoded.get("emailAddress") or "").lower()
        except Exception:
            logger.warning("Could not decode Pub/Sub Gmail notification data")
    if email_address and MAILBOX_EMAIL and email_address != MAILBOX_EMAIL:
        return {
            "ok": False,
            "error": f"notification mailbox {email_address} != {MAILBOX_EMAIL}",
        }
    if history_id:
        state = load_state()
        # Keep the newest history tip; processing uses search/reconcile.
        try:
            if int(history_id) >= int(state.get("historyId") or 0):
                state["historyId"] = history_id
                save_state(state)
        except Exception:
            state["historyId"] = history_id
            save_state(state)
    return run_pass(dry_run=False)


def _loop():
    try:
        initial = int(os.environ.get("DIGITIZING_INBOX_POLL_INITIAL_SECONDS") or "40")
    except ValueError:
        initial = 40
    time.sleep(max(0, initial))
    last_watch_renew = 0.0
    while True:
        try:
            if enabled():
                state = load_state()
                if state.get("enabled"):
                    run_pass(dry_run=False)
                    topic = pubsub_topic()
                    if topic and (time.time() - last_watch_renew) > 20 * 3600:
                        try:
                            renew_watch()
                            last_watch_renew = time.time()
                        except Exception:
                            logger.exception("Digitizing inbox watch renew failed")
        except Exception:
            logger.exception("Digitizing inbox poll pass failed")
        time.sleep(poll_interval_seconds())


def start_background_poller():
    """Daemon reconcile/poller (+ daily watch renew). Safe to call more than once."""
    global _started
    if not enabled():
        logger.info("[DigitizingInbox] disabled (DIGITIZING_INBOX_ENABLED)")
        return
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="digitizing-inbox", daemon=True).start()
    logger.info(
        "[DigitizingInbox] background poller started; vendor=%s mailbox=%s interval=%ss",
        VENDOR_EMAIL,
        MAILBOX_EMAIL,
        poll_interval_seconds(),
    )
