"""Short-lived command broker for the warehouse Wilcom agent.

The Render service runs one worker, so an in-memory broker is sufficient for
the immediate-send workflow. Commands are accepted only while the warehouse
agent has a fresh heartbeat; they are not silently delivered later.
"""
from __future__ import annotations

import threading
import time
import uuid
from copy import deepcopy
from typing import Any, Dict, Iterable, Optional


MACHINE_IDS = ("machine1", "machine2", "machine3", "machine4")
TERMINAL_STATES = {"completed", "failed"}


class WilcomDispatch:
    def __init__(
        self,
        *,
        heartbeat_timeout: float = 15.0,
        lease_timeout: float = 90.0,
        retention_seconds: float = 3600.0,
        clock=time.monotonic,
    ) -> None:
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.lease_timeout = float(lease_timeout)
        self.retention_seconds = float(retention_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._commands: Dict[str, dict] = {}
        self._agent_seen_at = 0.0
        self._configured_machines = set()
        self._agent_version = ""

    def _now(self) -> float:
        return float(self._clock())

    def heartbeat(
        self,
        configured_machines: Optional[Iterable[str]] = None,
        agent_version: str = "",
    ) -> dict:
        with self._lock:
            self._agent_seen_at = self._now()
            self._configured_machines = {
                str(machine_id)
                for machine_id in (configured_machines or ())
                if str(machine_id) in MACHINE_IDS
            }
            self._agent_version = str(agent_version or "")
            self._cleanup_locked()
            return self._agent_status_locked()

    def agent_status(self) -> dict:
        with self._lock:
            return self._agent_status_locked()

    def _agent_status_locked(self) -> dict:
        age = self._now() - self._agent_seen_at if self._agent_seen_at else None
        online = age is not None and age <= self.heartbeat_timeout
        return {
            "online": online,
            "lastSeenSecondsAgo": round(age, 1) if age is not None else None,
            "configuredMachines": sorted(self._configured_machines),
            "agentVersion": self._agent_version,
        }

    def create(self, order_id: Any, machine_id: Any) -> dict:
        order = str(order_id or "").strip()
        machine = str(machine_id or "").strip()
        if not order:
            raise ValueError("orderId is required")
        if machine not in MACHINE_IDS:
            raise ValueError("machineId must be machine1, machine2, machine3, or machine4")

        with self._lock:
            status = self._agent_status_locked()
            if not status["online"]:
                raise RuntimeError("The warehouse computer is offline")
            if machine not in self._configured_machines:
                raise RuntimeError(f"{machine} needs a Wilcom device name")

            now = self._now()
            command_id = uuid.uuid4().hex
            command = {
                "id": command_id,
                "orderId": order,
                "machineId": machine,
                "status": "queued",
                "message": "Waiting for warehouse computer",
                "createdAt": now,
                "updatedAt": now,
                "leasedAt": None,
            }
            self._commands[command_id] = command
            self._cleanup_locked()
            return deepcopy(command)

    def lease_next(self) -> Optional[dict]:
        with self._lock:
            now = self._now()
            self._agent_seen_at = now
            self._cleanup_locked()
            candidates = sorted(
                self._commands.values(), key=lambda command: command["createdAt"]
            )
            for command in candidates:
                lease_expired = (
                    command["status"] == "processing"
                    and command.get("leasedAt") is not None
                    and now - float(command["leasedAt"]) > self.lease_timeout
                )
                if command["status"] != "queued" and not lease_expired:
                    continue
                command["status"] = "processing"
                command["message"] = "Opening file in Wilcom"
                command["leasedAt"] = now
                command["updatedAt"] = now
                return deepcopy(command)
            return None

    def finish(self, command_id: Any, *, ok: bool, message: str = "") -> Optional[dict]:
        key = str(command_id or "").strip()
        with self._lock:
            command = self._commands.get(key)
            if not command:
                return None
            now = self._now()
            command["status"] = "completed" if ok else "failed"
            command["message"] = str(message or (
                "File sent to machine" if ok else "Wilcom send failed"
            ))
            command["updatedAt"] = now
            return deepcopy(command)

    def get(self, command_id: Any) -> Optional[dict]:
        with self._lock:
            self._cleanup_locked()
            command = self._commands.get(str(command_id or "").strip())
            return deepcopy(command) if command else None

    def _cleanup_locked(self) -> None:
        cutoff = self._now() - self.retention_seconds
        stale = [
            command_id
            for command_id, command in self._commands.items()
            if command["status"] in TERMINAL_STATES
            and float(command.get("updatedAt") or 0) < cutoff
        ]
        for command_id in stale:
            self._commands.pop(command_id, None)


dispatch = WilcomDispatch()
