"""Warehouse-side agent that opens an EMB and sends it to a named Wilcom device.

This program must run in the interactive Windows session where EmbroideryStudio
and EmbroideryHub are available. It intentionally does not run as a Windows
service because desktop UI automation cannot interact with a locked service
session.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict

import requests

AGENT_VERSION = "1.0"
MACHINE_IDS = ("machine1", "machine2", "machine3", "machine4")
PLACEHOLDER_PREFIX = "REPLACE_WITH_"
LOG = logging.getLogger("wilcom-agent")


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    required = ("serverUrl", "agentToken", "ordersRoot", "machineDevices")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError(f"Missing config value(s): {', '.join(missing)}")
    if str(config["agentToken"]).startswith(PLACEHOLDER_PREFIX):
        raise ValueError("Replace agentToken in wilcom_agent_config.json")
    return config


def configured_machines(config: dict) -> list:
    mappings = config.get("machineDevices") or {}
    return [
        machine_id
        for machine_id in MACHINE_IDS
        if str(mappings.get(machine_id) or "").strip()
        and not str(mappings[machine_id]).strip().startswith(PLACEHOLDER_PREFIX)
    ]


def exact_emb_path(config: dict, order_id: Any) -> Path:
    order = str(order_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", order):
        raise ValueError("Invalid order number")
    root = Path(str(config["ordersRoot"]))
    return root / order / f"{order}.EMB"


def _window_has_design(window, design_name: str) -> bool:
    wanted = design_name.lower()
    stem = Path(design_name).stem.lower()
    try:
        title = str(window.window_text() or "").lower()
        if wanted in title or stem in title:
            return True
    except Exception:
        pass
    try:
        for control in window.descendants(control_type="TabItem"):
            text = str(control.window_text() or "").lower()
            if wanted in text or text == stem:
                return True
    except Exception:
        pass
    return False


def _find_wilcom_window(desktop, timeout: float, design_name: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for window in desktop.windows():
            try:
                title = str(window.window_text() or "")
                if (
                    re.search(r"EmbroideryStudio|Wilcom", title, re.IGNORECASE)
                    and window.is_visible()
                    and _window_has_design(window, design_name)
                ):
                    return window
            except Exception:
                continue
        time.sleep(0.5)
    raise RuntimeError(
        f"EmbroideryStudio did not confirm that {design_name} finished opening"
    )


def _click_send_to_machine(main_window):
    patterns = (
        r"(?i).*Send to Device/Machine.*",
        r"(?i).*Send to EmbroideryConnect.*",
    )
    for control_type in ("Button", "MenuItem"):
        for pattern in patterns:
            try:
                control = main_window.child_window(
                    title_re=pattern, control_type=control_type
                )
                if control.exists(timeout=1):
                    control.wrapper_object().click_input()
                    return
            except Exception:
                continue

    # Wilcom documents this command under File. UI Automation usually exposes
    # the menu even when the toolbar button has no accessible name.
    try:
        main_window.menu_select("File->Send to Device/Machine")
        return
    except Exception as exc:
        raise RuntimeError(
            "Could not find Wilcom's 'Send to Device/Machine' control"
        ) from exc


def _select_device_and_send(desktop, device_name: str, timeout: float):
    deadline = time.time() + timeout
    dialog = None
    while time.time() < deadline and dialog is None:
        for pattern in (
            r"(?i).*Send.*Device.*",
            r"(?i).*Send.*Machine.*",
            r"(?i).*EmbroideryConnect.*",
        ):
            try:
                candidate = desktop.window(title_re=pattern)
                if candidate.exists(timeout=0.5) and candidate.is_visible():
                    dialog = candidate
                    break
            except Exception:
                continue
        if dialog is None:
            time.sleep(0.4)
    if dialog is None:
        raise RuntimeError("Wilcom machine-selection dialog did not appear")

    selected = False
    for control_type in ("ListItem", "DataItem", "RadioButton", "Text"):
        try:
            item = dialog.child_window(title=device_name, control_type=control_type)
            if item.exists(timeout=0.7):
                item.wrapper_object().click_input()
                selected = True
                break
        except Exception:
            continue

    if not selected:
        try:
            combo = dialog.child_window(control_type="ComboBox").wrapper_object()
            combo.select(device_name)
            selected = True
        except Exception:
            pass

    if not selected:
        raise RuntimeError(
            f"Wilcom device '{device_name}' was not found. Check machineDevices."
        )

    try:
        send_button = dialog.child_window(
            title_re=r"(?i)^(Send|OK)$", control_type="Button"
        )
        send_button.wait("visible enabled", timeout=5)
        send_button.wrapper_object().click_input()
    except Exception as exc:
        raise RuntimeError("Could not click Send in the Wilcom dialog") from exc

    # A closed dialog is the most dependable confirmation exposed by Wilcom's UI.
    try:
        dialog.wait_not("visible", timeout=timeout)
    except Exception as exc:
        raise RuntimeError(
            "Wilcom did not close the send dialog; check for an on-screen error"
        ) from exc


def send_with_wilcom(config: dict, order_id: str, machine_id: str) -> str:
    if machine_id not in MACHINE_IDS:
        raise ValueError(f"Unknown machine: {machine_id}")
    device_name = str(
        (config.get("machineDevices") or {}).get(machine_id) or ""
    ).strip()
    if not device_name or device_name.startswith(PLACEHOLDER_PREFIX):
        raise ValueError(f"{machine_id} needs a Wilcom device name")

    emb_path = exact_emb_path(config, order_id)
    if not emb_path.is_file():
        raise FileNotFoundError(f"File not found: {emb_path}")

    from pywinauto import Desktop

    LOG.info("Opening %s for %s (%s)", emb_path, machine_id, device_name)
    os.startfile(str(emb_path))
    timeout = float(config.get("openTimeoutSeconds") or 30)
    desktop = Desktop(backend="uia")
    main_window = _find_wilcom_window(desktop, timeout, emb_path.name)
    main_window.set_focus()
    _click_send_to_machine(main_window)
    _select_device_and_send(desktop, device_name, timeout)
    return f"Order {order_id} sent to {device_name}"


def load_history(path: Path) -> Dict[str, dict]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_history(path: Path, history: Dict[str, dict]) -> None:
    trimmed = dict(list(history.items())[-200:])
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(trimmed, handle, indent=2)
    temp.replace(path)


def post_result(
    session: requests.Session,
    base_url: str,
    command_id: str,
    result: dict,
) -> bool:
    response = session.post(
        f"{base_url}/wilcom-agent/result/{command_id}",
        json=result,
        timeout=15,
    )
    if response.status_code == 404:
        return True
    response.raise_for_status()
    return True


def run(config_path: Path) -> None:
    config = load_config(config_path)
    base_url = str(config["serverUrl"]).rstrip("/")
    history_path = config_path.with_name("wilcom_agent_history.json")
    history = load_history(history_path)
    session = requests.Session()
    session.headers.update(
        {
            "X-Wilcom-Agent-Token": str(config["agentToken"]),
            "Content-Type": "application/json",
        }
    )
    poll_seconds = max(1.0, float(config.get("pollSeconds") or 2))

    LOG.info("Wilcom agent %s started; server=%s", AGENT_VERSION, base_url)
    while True:
        try:
            # Retry final acknowledgements before accepting more work. This
            # prevents a successful design from being sent twice after a brief
            # network failure.
            for command_id, result in list(history.items()):
                if not result.get("pending"):
                    continue
                if post_result(session, base_url, command_id, result):
                    result["pending"] = False
                    save_history(history_path, history)

            response = session.post(
                f"{base_url}/wilcom-agent/next",
                json={
                    "agentVersion": AGENT_VERSION,
                    "configuredMachines": configured_machines(config),
                },
                timeout=15,
            )
            response.raise_for_status()
            command = (response.json() or {}).get("command")
            if not command:
                time.sleep(poll_seconds)
                continue

            command_id = str(command["id"])
            previous = history.get(command_id)
            if previous:
                result = previous
            else:
                try:
                    message = send_with_wilcom(
                        config,
                        str(command["orderId"]),
                        str(command["machineId"]),
                    )
                    result = {"ok": True, "message": message, "pending": True}
                except Exception as exc:
                    LOG.exception("Command %s failed", command_id)
                    result = {"ok": False, "message": str(exc), "pending": True}
                history[command_id] = result
                save_history(history_path, history)

            if post_result(session, base_url, command_id, result):
                result["pending"] = False
                save_history(history_path, history)
        except requests.RequestException as exc:
            LOG.warning("Backend unavailable: %s", exc)
            time.sleep(max(5.0, poll_seconds))
        except Exception:
            LOG.exception("Unexpected agent error")
            time.sleep(max(5.0, poll_seconds))


def main() -> int:
    parser = argparse.ArgumentParser(description="Warehouse Wilcom command agent")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).with_name("wilcom_agent_config.json")),
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(
                Path(args.config).with_name("wilcom_agent.log"), encoding="utf-8"
            ),
            logging.StreamHandler(),
        ],
    )
    try:
        run(Path(args.config).resolve())
        return 0
    except Exception as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
