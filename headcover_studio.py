"""
Headcover Studio backend (/headcover-studio page).

The studio renders everything in the browser. The backend only:
  GET  /api/headcover-studio/materials   live Material Inventory names (+ vendor/color); furs = rows named "<Color> Fur"
  POST /api/headcover-studio/sheets      save a finished proof sheet into the club's customer folder
  GET  /api/headcover-studio/sheets      list sheets already saved for a club

Customer folders are the club folders in My Drive / Design (e.g. "Atlanta Athletic Club",
"ColoradoGolfClub"). Names are matched ignoring spaces, punctuation, case and the usual
abbreviations (CC = Country Club, GC = Golf Club). If there is no exact match the page is
given the closest folders to choose from, or it can ask to create Design/<Club>.

Routes are registered from server.py with register_routes(app, ...), so this module has no
import-time dependency on server.py.
"""
import datetime
import difflib
import io
import os
import re

from flask import jsonify, request

# My Drive / Design (holds one folder per customer)
CUSTOMER_PARENT_ID = (
    os.environ.get("HEADCOVER_CUSTOMER_PARENT_ID") or "1ByURKxvYfdOvdlpCZRZVtB-ps15KYADR"
).strip()
# folders in Design that are not customers
NOT_CUSTOMERS = {"designtemplates", "presentationexports"}
FOLDER_MIME = "application/vnd.google-apps.folder"
MAX_SHEET_BYTES = 25 * 1024 * 1024

_ABBREV = [
    (r"\bg\s*&\s*cc\b", "golf and country club"),
    (r"\bt\s*&\s*cc\b", "tennis and country club"),
    (r"\bcc\b", "country club"),
    (r"\bgc\b", "golf club"),
    (r"\bg\s*&\s*c\b", "golf and country"),
]


def norm_name(name):
    """'Greenville CC' and 'Greenville Country Club' -> 'greenvillecountryclub'."""
    # split CamelCase folder names first: 'ColoradoGolfClub' -> 'colorado golf club'
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(name or "")).lower().strip()
    for rx, full in _ABBREV:
        s = re.sub(rx, full, s)
    s = s.replace("&", " and ")
    s = re.sub(r"^the\s+", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def match_folder(club, folders):
    """folders: [{id, name}]. Returns (exact_folder_or_None, [closest candidates])."""
    want = norm_name(club)
    if not want:
        return None, []
    pool = [f for f in folders if norm_name(f["name"]) not in NOT_CUSTOMERS]
    for f in pool:
        if norm_name(f["name"]) == want:
            return f, []
    scored = []
    for f in pool:
        n = norm_name(f["name"])
        r = difflib.SequenceMatcher(None, want, n).ratio()
        if n and (n in want or want in n):
            r = max(r, 0.8)
        if r >= 0.55:
            scored.append((r, f))
    scored.sort(key=lambda t: -t[0])
    return None, [{"id": f["id"], "name": f["name"]} for _, f in scored[:5]]


def sheet_filename(club, kind, existing_names, today=None):
    today = today or datetime.date.today()
    label = "Picks" if kind == "picks" else "Concepts"
    safe = re.sub(r'[\\/:*?"<>|]+', " ", club).strip() or "Club"
    base = f"{safe} – Headcover {label} – {today.isoformat()}"
    name, n = f"{base}.png", 2
    taken = set(existing_names or [])
    while name in taken:
        name = f"{base} ({n}).png"
        n += 1
    return name


def register_routes(app, *, login_required, get_drive, send_cached_json, material_rows,
                    list_children, create_folder, upload_bytes, logger):
    """Wire the Headcover Studio routes into the Flask app (called once from server.py)."""

    def _customer_folders(drive):
        return [{"id": f["id"], "name": f["name"]}
                for f in list_children(drive, CUSTOMER_PARENT_ID) if f.get("mimeType") == FOLDER_MIME]

    @app.route("/api/headcover-studio/materials", methods=["GET"])
    @login_required
    def headcover_studio_materials():
        def build():
            mats = []
            for r in material_rows() or []:
                mats.append({"name": r.get("name", ""), "vendor": r.get("vendor", ""), "color": r.get("color", "")})
            # Furs are Material Inventory rows named "<Color> Fur" (e.g. "Navy Blue Fur").
            furs = [m["name"] for m in mats if re.search(r"\bfur\b", m["name"], re.I)]
            return {"materials": mats, "furs": furs}

        return send_cached_json("headcover:materials", 300, build)

    @app.route("/api/headcover-studio/sheets", methods=["GET"])
    @login_required
    def headcover_studio_list_sheets():
        club = (request.args.get("club") or "").strip()
        if not club:
            return jsonify({"error": "club required"}), 400
        try:
            drive = get_drive()
            folder, candidates = match_folder(club, _customer_folders(drive))
            if not folder:
                return jsonify({"folder": None, "candidates": candidates, "sheets": []})
            files = [f for f in list_children(drive, folder["id"])
                     if f.get("mimeType") != FOLDER_MIME and "Headcover" in f.get("name", "")]
            return jsonify({"folder": folder, "sheets": files})
        except Exception as e:   # pragma: no cover - Drive errors
            logger.exception("[headcover-studio] list sheets failed")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/headcover-studio/sheets", methods=["POST"])
    @login_required
    def headcover_studio_save_sheet():
        club = (request.form.get("club") or "").strip()
        up = request.files.get("file")
        if not club:
            return jsonify({"error": "Type the club name first."}), 400
        if not up:
            return jsonify({"error": "No sheet image was sent."}), 400
        data = up.read()
        if not data or len(data) > MAX_SHEET_BYTES or not data.startswith(b"\x89PNG"):
            return jsonify({"error": "The sheet must be a PNG under 25 MB."}), 400
        kind = (request.form.get("kind") or "concepts").strip().lower()
        try:
            drive = get_drive()
            folders = _customer_folders(drive)
            created = False
            folder_id = (request.form.get("folderId") or "").strip()
            if folder_id:
                folder = next((f for f in folders if f["id"] == folder_id), None)
                if not folder:
                    return jsonify({"error": "That folder isn't in Design any more."}), 400
            else:
                folder, candidates = match_folder(club, folders)
                if not folder:
                    if request.form.get("create") != "1" and candidates:
                        # close but not exact: let the person pick, so we don't make a near-duplicate folder
                        return jsonify({"needsChoice": True, "candidates": candidates})
                    folder = {"id": create_folder(drive, club, CUSTOMER_PARENT_ID), "name": club}
                    created = True
                    logger.info("[headcover-studio] created customer folder Design/%s", club)
            existing = [f.get("name", "") for f in list_children(drive, folder["id"])]
            fname = sheet_filename(club, kind, existing)
            res = upload_bytes(io.BytesIO(data), fname, "image/png", folder["id"])
            desc = "; ".join(x for x in [
                "JR & Co. Headcover Studio proof sheet",
                f"Materials: {request.form.get('materials')}" if request.form.get("materials") else "",
                f"Fur: {request.form.get('fur')}" if request.form.get("fur") else "",
            ] if x)
            try:
                drive.files().update(fileId=res["id"], body={"description": desc}, supportsAllDrives=True).execute()
            except Exception:
                logger.warning("[headcover-studio] could not set description on %s", res.get("id"))
            return jsonify({
                "ok": True, "fileId": res.get("id"), "fileName": fname, "webViewLink": res.get("webViewLink"),
                "folderId": folder["id"], "folderName": folder["name"], "createdFolder": created,
            })
        except Exception as e:   # pragma: no cover - Drive errors
            logger.exception("[headcover-studio] save sheet failed")
            return jsonify({"error": f"Drive error: {e}"}), 500
