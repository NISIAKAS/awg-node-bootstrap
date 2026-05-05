"""REST API handlers for AWG peer, relay, and node management."""

import asyncio
import hmac
import logging
import os
import re
from functools import wraps
from pathlib import Path
from urllib.parse import unquote

from aiohttp import web

from . import awg, config

logger = logging.getLogger(__name__)
AWG_CONFIG_DIR = Path("/etc/amnezia/amneziawg")
AWG_CONFIG_PATH = AWG_CONFIG_DIR / "awg0.conf"


async def _run_command(*args: str, input_text: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if input_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=input_text.encode() if input_text is not None else None)
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


async def _run_shell(command: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


def _extract_listen_port(config_text: str) -> int | None:
    match = re.search(r"^\s*ListenPort\s*=\s*(\d+)\s*$", config_text, re.MULTILINE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


async def _ensure_ip_forward() -> None:
    rc, _, err = await _run_command("sysctl", "-w", "net.ipv4.ip_forward=1")
    if rc != 0:
        raise RuntimeError(f"sysctl ip_forward failed: {err}")
    sysctl_conf = Path("/etc/sysctl.d/99-awg-node.conf")
    existing = sysctl_conf.read_text(encoding="utf-8") if sysctl_conf.exists() else ""
    line = "net.ipv4.ip_forward = 1"
    if line not in existing.splitlines():
        text = existing.rstrip()
        if text:
            text += "\n"
        text += line + "\n"
        sysctl_conf.write_text(text, encoding="utf-8")


async def _sync_udp_port(old_port: int | None, new_port: int | None) -> None:
    if not new_port:
        return
    if not shutil_which("ufw"):
        return
    rc, _, err = await _run_command("ufw", "allow", f"{new_port}/udp")
    if rc != 0 and "Skipping adding existing rule" not in err:
        logger.warning("ufw allow %s/udp failed: %s", new_port, err)
    if old_port and old_port != new_port:
        await _run_command("ufw", "--force", "delete", "allow", f"{old_port}/udp")


async def _ufw_allow_udp_source(source_host: str, port: int) -> None:
    if not shutil_which("ufw"):
        return
    rc, _, err = await _run_command(
        "ufw",
        "allow",
        "from",
        source_host,
        "to",
        "any",
        "port",
        str(port),
        "proto",
        "udp",
    )
    if rc != 0 and "Skipping adding existing rule" not in err:
        raise RuntimeError(f"ufw allow from {source_host} to {port}/udp failed: {err}")


async def _ufw_remove_udp_source(source_host: str, port: int) -> None:
    if not shutil_which("ufw"):
        return
    await _run_command(
        "ufw",
        "--force",
        "delete",
        "allow",
        "from",
        source_host,
        "to",
        "any",
        "port",
        str(port),
        "proto",
        "udp",
    )


async def _apply_relay_rule(relay_port: int, exit_host: str, exit_port: int) -> None:
    rule_tag = f"awg-relay-{relay_port}"
    await _ensure_ip_forward()
    rc, _, err = await _run_shell(
        " ".join(
            [
                "set -e;",
                f"iptables -t nat -C PREROUTING -p udp --dport {relay_port}",
                f"-m comment --comment '{rule_tag}' -j DNAT --to-destination {exit_host}:{exit_port} 2>/dev/null ||",
                f"iptables -t nat -A PREROUTING -p udp --dport {relay_port}",
                f"-m comment --comment '{rule_tag}' -j DNAT --to-destination {exit_host}:{exit_port};",
                f"iptables -C FORWARD -p udp -d {exit_host} --dport {exit_port}",
                f"-m comment --comment '{rule_tag}' -j ACCEPT 2>/dev/null ||",
                f"iptables -I FORWARD 1 -p udp -d {exit_host} --dport {exit_port}",
                f"-m comment --comment '{rule_tag}' -j ACCEPT;",
                f"iptables -t nat -C POSTROUTING -p udp -d {exit_host} --dport {exit_port}",
                f"-m comment --comment '{rule_tag}' -j MASQUERADE 2>/dev/null ||",
                f"iptables -t nat -A POSTROUTING -p udp -d {exit_host} --dport {exit_port}",
                f"-m comment --comment '{rule_tag}' -j MASQUERADE;",
                "iptables-save > /etc/iptables/rules.v4 2>/dev/null || true",
            ]
        )
    )
    if rc != 0:
        raise RuntimeError(f"relay rule apply failed: {err}")
    await _sync_udp_port(None, relay_port)


async def _remove_relay_rule(relay_port: int) -> None:
    rule_tag = f"awg-relay-{relay_port}"
    rc, _, err = await _run_shell(
        " ".join(
            [
                "set -e;",
                f'(iptables -t nat -S PREROUTING | grep "{rule_tag}" |',
                "sed 's/^-A/-D/' | while read rule; do iptables -t nat $rule; done) || true;",
                f'(iptables -t nat -S POSTROUTING | grep "{rule_tag}" |',
                "sed 's/^-A/-D/' | while read rule; do iptables -t nat $rule; done) || true;",
                f'(iptables -S FORWARD | grep "{rule_tag}" |',
                "sed 's/^-A/-D/' | while read rule; do iptables $rule; done) || true;",
                "iptables-save > /etc/iptables/rules.v4 2>/dev/null || true",
            ]
        )
    )
    if rc != 0:
        raise RuntimeError(f"relay rule remove failed: {err}")
    if shutil_which("ufw"):
        await _run_command("ufw", "--force", "delete", "allow", f"{relay_port}/udp")


async def _is_interface_active(interface: str) -> bool:
    rc, _, _ = await _run_command(config.AWG_BIN, "show", interface)
    return rc == 0


def shutil_which(binary: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / binary
        if candidate.exists():
            return str(candidate)
    return None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def require_auth(handler):
    """Decorator: require valid Bearer token on /api/* endpoints."""
    @wraps(handler)
    async def wrapper(request: web.Request) -> web.Response:
        if not config.TOKEN:
            # No token configured — refuse requests (agent misconfigured)
            logger.error("AWG_AGENT_TOKEN not set — refusing request")
            return web.json_response({"error": "agent not configured"}, status=503)
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {config.TOKEN}"
        if not hmac.compare_digest(auth.encode(), expected.encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)
    return wrapper


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@require_auth
async def list_peers(request: web.Request) -> web.Response:
    """GET /api/v1/peers"""
    try:
        peers = await awg.show_peers(config.INTERFACE)
        return web.json_response({
            "peers": [
                {
                    "public_key": p.public_key,
                    "endpoint": p.endpoint,
                    "allowed_ips": p.allowed_ips,
                    "latest_handshake": p.latest_handshake,
                    "transfer_rx": p.transfer_rx,
                    "transfer_tx": p.transfer_tx,
                }
                for p in peers
            ]
        })
    except Exception as e:
        logger.error("list_peers error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


@require_auth
async def add_peer(request: web.Request) -> web.Response:
    """POST /api/v1/peers — body: {public_key, preshared_key, allowed_ips}"""
    try:
        data = await request.json()
        await awg.add_peer(
            config.INTERFACE,
            data["public_key"],
            data.get("preshared_key", "(none)"),
            data["allowed_ips"],
        )
        logger.info("Added peer %s allowed_ips=%s", data["public_key"][:16], data["allowed_ips"])
        return web.json_response({"status": "ok"})
    except KeyError as e:
        return web.json_response({"error": f"missing field: {e}"}, status=400)
    except Exception as e:
        logger.error("add_peer error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


@require_auth
async def remove_peer(request: web.Request) -> web.Response:
    """DELETE /api/v1/peers/{public_key}"""
    try:
        public_key = unquote(request.match_info["public_key"])
        await awg.remove_peer(config.INTERFACE, public_key)
        logger.info("Removed peer %s", public_key[:16])
        return web.json_response({"status": "ok"})
    except Exception as e:
        logger.error("remove_peer error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


@require_auth
async def batch_add_peers(request: web.Request) -> web.Response:
    """POST /api/v1/peers/batch — body: {peers: [{public_key, preshared_key, allowed_ips}, ...]}"""
    try:
        data = await request.json()
        peers_data = data["peers"]
        added = await awg.add_peers_batch(config.INTERFACE, peers_data)
        logger.info("Batch add: %d peers via addconf", added)
        return web.json_response({"status": "ok", "added": added, "total": len(peers_data)})
    except KeyError as e:
        return web.json_response({"error": f"missing field: {e}"}, status=400)
    except Exception as e:
        logger.error("batch_add error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


@require_auth
async def batch_remove_peers(request: web.Request) -> web.Response:
    """POST /api/v1/peers/batch-remove — body: {public_keys: [...]}"""
    try:
        data = await request.json()
        public_keys = data["public_keys"]
        await awg.remove_peers_batch(config.INTERFACE, public_keys)
        logger.info("Batch removed %d peers", len(public_keys))
        return web.json_response({"status": "ok", "removed": len(public_keys)})
    except KeyError as e:
        return web.json_response({"error": f"missing field: {e}"}, status=400)
    except Exception as e:
        logger.error("batch_remove error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


@require_auth
async def apply_awg_config(request: web.Request) -> web.Response:
    """POST /api/v1/node/apply-awg-config — write awg0.conf and reconcile awg0."""
    try:
        if not config.INTERFACE:
            return web.json_response({"error": "awg interface is not configured on this node"}, status=400)

        data = await request.json()
        config_text = data["config"]
        new_port = data.get("awg_port") or _extract_listen_port(config_text)

        old_port: int | None = None
        if AWG_CONFIG_PATH.exists():
            old_port = _extract_listen_port(AWG_CONFIG_PATH.read_text(encoding="utf-8"))

        AWG_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        AWG_CONFIG_PATH.write_text(config_text, encoding="utf-8")
        os.chmod(AWG_CONFIG_PATH, 0o600)

        await _ensure_ip_forward()
        await _sync_udp_port(old_port, new_port)

        is_active = await _is_interface_active(config.INTERFACE)

        if is_active:
            sync_cmd = (
                f"bash -lc \"{config.AWG_BIN} syncconf {config.INTERFACE} "
                f"<({config.AWG_QUICK_BIN} strip {config.INTERFACE} 2>/dev/null || cat {AWG_CONFIG_PATH})\""
            )
            rc, _, err = await _run_shell(sync_cmd)
            if rc != 0:
                raise RuntimeError(f"awg syncconf failed: {err}")
            action = "synced"
        else:
            rc, _, err = await _run_command(config.AWG_QUICK_BIN, "up", config.INTERFACE)
            if rc != 0:
                raise RuntimeError(f"awg-quick up failed: {err}")
            action = "started"

        return web.json_response({
            "status": "ok",
            "action": action,
            "awg_port": new_port,
            "service": f"{config.AWG_QUICK_BIN} {config.INTERFACE}",
        })
    except KeyError as e:
        return web.json_response({"error": f"missing field: {e}"}, status=400)
    except Exception as e:
        logger.error("apply_awg_config error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


@require_auth
async def reconcile_relay_binding(request: web.Request) -> web.Response:
    """POST /api/v1/node/relay-binding/reconcile"""
    try:
        data = await request.json()
        relay_port = int(data["relay_port"])
        exit_host = data["exit_host"]
        exit_port = int(data["exit_port"])
        await _remove_relay_rule(relay_port)
        await _apply_relay_rule(relay_port, exit_host, exit_port)
        return web.json_response(
            {"status": "ok", "relay_port": relay_port, "exit_host": exit_host, "exit_port": exit_port}
        )
    except KeyError as e:
        return web.json_response({"error": f"missing field: {e}"}, status=400)
    except Exception as e:
        logger.error("reconcile_relay_binding error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


@require_auth
async def remove_relay_binding(request: web.Request) -> web.Response:
    """POST /api/v1/node/relay-binding/remove"""
    try:
        data = await request.json()
        relay_port = int(data["relay_port"])
        await _remove_relay_rule(relay_port)
        return web.json_response({"status": "ok", "relay_port": relay_port})
    except KeyError as e:
        return web.json_response({"error": f"missing field: {e}"}, status=400)
    except Exception as e:
        logger.error("remove_relay_binding error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


@require_auth
async def allow_udp_source(request: web.Request) -> web.Response:
    """POST /api/v1/node/firewall/allow-udp-source"""
    try:
        data = await request.json()
        source_host = data["source_host"]
        port = int(data["port"])
        await _ufw_allow_udp_source(source_host, port)
        return web.json_response({"status": "ok", "source_host": source_host, "port": port})
    except KeyError as e:
        return web.json_response({"error": f"missing field: {e}"}, status=400)
    except Exception as e:
        logger.error("allow_udp_source error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


@require_auth
async def remove_udp_source(request: web.Request) -> web.Response:
    """POST /api/v1/node/firewall/remove-udp-source"""
    try:
        data = await request.json()
        source_host = data["source_host"]
        port = int(data["port"])
        await _ufw_remove_udp_source(source_host, port)
        return web.json_response({"status": "ok", "source_host": source_host, "port": port})
    except KeyError as e:
        return web.json_response({"error": f"missing field: {e}"}, status=400)
    except Exception as e:
        logger.error("remove_udp_source error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def health(request: web.Request) -> web.Response:
    """GET /health"""
    return web.json_response({
        "status": "ok",
        "interface": config.INTERFACE,
        "registered": bool(config.TOKEN),
        "node_port": config.PORT,
        "agent_version": config.AGENT_VERSION,
        "server_id": config.SERVER_ID or None,
    })
