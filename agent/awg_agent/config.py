"""Configuration — loaded from environment variables (set via /etc/awg-agent/config)."""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PORT = int(os.environ.get("AWG_AGENT_PORT", "9101"))
TOKEN = os.environ.get("AWG_AGENT_TOKEN", "")
ENROLLMENT_SECRET = os.environ.get("AWG_ENROLLMENT_SECRET", "")
MASTER_ENROLL_URL = os.environ.get("MASTER_ENROLL_URL", "")
SERVER_ID = int(os.environ.get("SERVER_ID", "0") or "0")
AGENT_VERSION = os.environ.get("AWG_AGENT_VERSION", "dev")
INTERFACE = os.environ.get("AWG_INTERFACE", "awg0")
LOG_LEVEL = os.environ.get("AWG_AGENT_LOG_LEVEL", "INFO")
CONFIG_PATH = os.environ.get("AWG_AGENT_CONFIG_PATH", "/etc/awg-agent/config")

# Webhook: agent pushes connect/disconnect events to master
MASTER_WEBHOOK_URL = os.environ.get("MASTER_WEBHOOK_URL", "")
WATCHER_INTERVAL = int(os.environ.get("AWG_WATCHER_INTERVAL", "5"))


def _resolve_binary(env_name: str, default: str) -> str:
    raw_value = os.environ.get(env_name, "")
    candidates = [candidate for candidate in raw_value.split(os.pathsep) if candidate] or [default]
    for candidate in candidates:
        if "/" not in candidate:
            return candidate
        if Path(candidate).exists():
            return candidate
    return candidates[-1]


AWG_BIN = _resolve_binary("AWG_BIN", "awg")
AWG_QUICK_BIN = _resolve_binary("AWG_QUICK_BIN", "awg-quick")


def set_runtime_token(token: str) -> None:
    global TOKEN
    TOKEN = token


def set_master_webhook_url(url: str) -> None:
    global MASTER_WEBHOOK_URL
    MASTER_WEBHOOK_URL = url


def _persist_updates(updates: dict[str, str]) -> None:
    """Update /etc/awg-agent/config in-place, preserving unknown lines."""
    path = Path(CONFIG_PATH)
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    rendered: list[str] = []
    seen: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if "=" not in line or stripped.startswith("#"):
            rendered.append(line)
            continue

        key, _value = line.split("=", 1)
        key = key.strip()
        if key in updates:
            rendered.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            rendered.append(line)

    for key, value in updates.items():
        if key not in seen:
            rendered.append(f"{key}={value}")

    text = "\n".join(rendered).rstrip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.debug("Could not chmod %s to 600", path)


def persist_runtime_state(token: str, webhook_url: str = "") -> None:
    """Persist runtime token and optional webhook URL to the agent config file."""
    set_runtime_token(token)
    updates = {"AWG_AGENT_TOKEN": token}
    if webhook_url:
        set_master_webhook_url(webhook_url)
        updates["MASTER_WEBHOOK_URL"] = webhook_url
    _persist_updates(updates)
