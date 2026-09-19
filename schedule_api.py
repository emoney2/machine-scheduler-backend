"""Flask routes for production schedules, versions, approvals, and settings."""
from __future__ import annotations

import logging
from threading import Lock
from datetime import datetime
from uuid import uuid4

from flask import Blueprint, jsonify, request

from schedule_store import friendly_sheets_error

logger = logging.getLogger(__name__)


def create_schedule_blueprint(
    service,
    *,
    login_required,
    values_service,
    spreadsheet_id: str,
    socketio=None,
):
    bp = Blueprint("production_schedule", __name__, url_prefix="/api/schedule")
    schema_lock = Lock()
    schema_ready = False

    @bp.before_request
    def ensure_schedule_schema():
        nonlocal schema_ready
        if schema_ready:
            return None
        with schema_lock:
            if schema_ready:
                return None
            try:
                service.store.ensure_schema()
                schema_ready = True
            except Exception as exc:
                logger.exception("Scheduling Sheets schema initialization failed")
                return jsonify({"error": friendly_sheets_error(exc)}), 503
        return None

    def actor() -> str:
        return "admin"

    def emit(event: str, payload: dict):
        try:
            if socketio:
                socketio.emit(event, payload)
        except Exception:
            logger.exception("Schedule socket emit failed")

    @bp.get("/published")
    @login_required
    def published():
        try:
            version = service.store.published_version()
            if not version:
                return jsonify({"version": None, "schedule": None}), 200
            loaded = service.store.load_schedule(str(version.get("Version ID") or ""))
            return jsonify({"version": version, "schedule": loaded}), 200
        except Exception as exc:
            logger.exception("Could not load published schedule")
            return jsonify({"error": friendly_sheets_error(exc)}), 503

    @bp.get("/proposal")
    @login_required
    def proposal():
        try:
            version = service.store.active_proposal()
            if not version:
                return jsonify({"version": None, "schedule": None}), 200
            loaded = service.store.load_schedule(str(version.get("Version ID") or ""))
            published_version = service.store.published_version()
            comparison = service._comparison(
                published_version,
                {
                    "orders": [
                        {"order_number": row.get("order_number")}
                        for row in (loaded.get("orders") or [])
                    ],
                    "sewing": loaded.get("sewing") or [],
                    "embroidery": loaded.get("embroidery") or [],
                },
            )
            return jsonify({"version": version, "schedule": loaded, "comparison": comparison}), 200
        except Exception as exc:
            logger.exception("Could not load schedule proposal")
            return jsonify({"error": friendly_sheets_error(exc)}), 503

    @bp.get("/versions")
    @login_required
    def versions():
        rows = service.store.versions()
        rows.reverse()
        return jsonify({"versions": rows[:100]}), 200

    @bp.get("/versions/<version_id>")
    @login_required
    def version_detail(version_id):
        try:
            return jsonify(service.store.load_schedule(version_id)), 200
        except KeyError:
            return jsonify({"error": "Schedule version not found"}), 404

    @bp.post("/rebuild")
    @login_required
    def rebuild():
        data = request.get_json(silent=True) or {}
        reason = str(data.get("reason") or "manual").strip()[:200]
        result = service.rebuild(reason=reason, notify=True)
        status = 200 if result.get("ok") else 500
        if result.get("ok"):
            emit("scheduleProposalUpdated", {
                "versionId": (result.get("version") or {}).get("Version ID"),
                "initialBaseline": bool(result.get("initialBaseline")),
            })
        return jsonify(result), status

    @bp.post("/check")
    @login_required
    def check():
        # Idempotent fingerprinting prevents duplicate versions and emails.
        result = service.rebuild(reason="data change check", notify=True)
        status = 200 if result.get("ok") else 500
        return jsonify({
            "ok": result.get("ok"),
            "deduplicated": result.get("deduplicated", False),
            "initialBaseline": result.get("initialBaseline", False),
            "version": result.get("version"),
            "error": result.get("error"),
        }), status

    @bp.post("/versions/<version_id>/approve")
    @login_required
    def approve(version_id):
        data = request.get_json(silent=True) or {}
        try:
            result = service.approve(version_id, actor(), str(data.get("notes") or ""))
        except KeyError:
            return jsonify({"error": "Schedule version not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        emit("schedulePublished", {"versionId": version_id})
        return jsonify({"ok": True, "schedule": result}), 200

    @bp.post("/versions/<version_id>/reject")
    @login_required
    def reject(version_id):
        data = request.get_json(silent=True) or {}
        try:
            result = service.reject(version_id, actor(), str(data.get("notes") or ""))
        except KeyError:
            return jsonify({"error": "Schedule version not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        emit("scheduleProposalUpdated", {"versionId": version_id, "status": "Rejected"})
        return jsonify({"ok": True, "schedule": result}), 200

    @bp.get("/settings")
    @login_required
    def get_settings():
        try:
            _orders, _inventory, metrics = service.load_inputs()
        except Exception:
            logger.exception("Could not load sewing output metrics")
            metrics = {}
        return jsonify({"settings": service._settings(), "sewingOutput": metrics}), 200

    @bp.put("/settings")
    @login_required
    def save_settings():
        data = request.get_json(silent=True) or {}
        settings = data.get("settings")
        if not isinstance(settings, dict):
            return jsonify({"error": "settings object is required"}), 400
        allowed = {
            "regularSewingCapacity", "emergencySewingCapacity",
            "approvedEmergencyDates", "holidays", "productFactors",
            "frenchSeamFactor", "unusualShapeFactor", "sewingChangeoverMinutes",
        }
        clean = {key: value for key, value in settings.items() if key in allowed}
        service.store.save_settings(clean, actor())
        emit("scheduleSettingsUpdated", {"keys": sorted(clean)})
        return jsonify({"ok": True, "settings": service._settings()}), 200

    @bp.get("/locks")
    @login_required
    def locks():
        return jsonify({"locks": service.store.locks()}), 200

    @bp.put("/locks/<lock_id>")
    @login_required
    def save_lock(lock_id):
        data = request.get_json(silent=True) or {}
        lock = {
            **data,
            "lockId": lock_id,
            "active": bool(data.get("active", True)),
        }
        if not str(lock.get("orderNumber") or "").strip() or not str(lock.get("date") or "").strip():
            return jsonify({"error": "orderNumber and date are required"}), 400
        try:
            units = float(lock.get("capacityUnits") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "capacityUnits must be numeric"}), 400
        if units <= 0:
            return jsonify({"error": "capacityUnits must be greater than zero"}), 400
        lock["capacityUnits"] = units
        service.store.upsert_lock(lock, actor())
        result = service.rebuild(reason=f"sewing lock {lock_id} changed", notify=True)
        emit("scheduleProposalUpdated", {"lockId": lock_id})
        return jsonify({"ok": True, "lock": lock, "rebuild": result}), 200

    @bp.delete("/locks/<lock_id>")
    @login_required
    def delete_lock(lock_id):
        found = next((row for row in service.store.locks() if row.get("lockId") == lock_id), None)
        if not found:
            return jsonify({"error": "Lock not found"}), 404
        service.store.upsert_lock({**found, "lockId": lock_id, "active": False}, actor())
        result = service.rebuild(reason=f"sewing lock {lock_id} removed", notify=True)
        emit("scheduleProposalUpdated", {"lockId": lock_id, "active": False})
        return jsonify({"ok": True, "rebuild": result}), 200

    @bp.post("/sewing-completion")
    @login_required
    def sewing_completion():
        data = request.get_json(silent=True) or {}
        oid = str(data.get("orderNumber") or "").strip()
        name = str(data.get("name") or actor()).strip()
        try:
            finished = int(data.get("finishedPieces"))
        except (TypeError, ValueError):
            return jsonify({"error": "finishedPieces must be a whole number"}), 400
        if not oid or finished <= 0:
            return jsonify({"error": "orderNumber and positive finishedPieces are required"}), 400
        # Existing Sewing Log contract: intermediate steps stay zero; finished
        # pieces are recorded only in Top so Sewing Summary does not double count.
        values_service.append(
            spreadsheetId=spreadsheet_id,
            range="'Sewing Log'!A:H",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [[
                datetime.now().isoformat(), oid, name, 0, 0, 0, 0, finished
            ]]},
        ).execute()
        result = service.rebuild(reason=f"end-of-day sewing completion for {oid}", notify=True)
        emit("scheduleProposalUpdated", {"orderNumber": oid, "finishedPieces": finished})
        return jsonify({"ok": True, "rebuild": result}), 200

    @bp.post("/explain")
    @login_required
    def explain():
        data = request.get_json(silent=True) or {}
        question = str(data.get("question") or "").strip()
        version_id = str(data.get("versionId") or "").strip()
        if not question:
            return jsonify({"error": "question is required"}), 400
        if not version_id:
            version = service.store.active_proposal() or service.store.published_version()
            version_id = str((version or {}).get("Version ID") or "")
        if not version_id:
            return jsonify({"error": "No schedule is available"}), 404
        try:
            schedule = service.store.load_schedule(version_id)
            return jsonify(service.explain(schedule, question)), 200
        except KeyError:
            return jsonify({"error": "Schedule version not found"}), 404
        except Exception as exc:
            logger.exception("Schedule explanation failed")
            return jsonify({"error": str(exc)}), 502

    return bp
