"""AWG CLI wrapper — runs awg commands as local subprocesses."""

import asyncio
import tempfile
import os
from dataclasses import dataclass

from . import config


@dataclass
class PeerInfo:
    public_key: str
    preshared_key: str
    endpoint: str
    allowed_ips: str
    latest_handshake: int   # unix timestamp (0 = never)
    transfer_rx: int        # bytes
    transfer_tx: int        # bytes


async def show_peers(interface: str) -> list[PeerInfo]:
    """Parse `awg show <iface> dump` into PeerInfo objects."""
    proc = await asyncio.create_subprocess_exec(
        config.AWG_BIN, "show", interface, "dump",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"awg show dump failed: {stderr.decode().strip()}")

    peers: list[PeerInfo] = []
    lines = stdout.decode().strip().split("\n")
    for line in lines[1:]:          # skip interface line
        parts = line.split("\t")
        if len(parts) < 8:
            continue
        peers.append(PeerInfo(
            public_key=parts[0],
            preshared_key=parts[1],
            endpoint=parts[2],
            allowed_ips=parts[3],
            latest_handshake=int(parts[4]) if parts[4] != "0" else 0,
            transfer_rx=int(parts[5]),
            transfer_tx=int(parts[6]),
        ))
    return peers


async def add_peer(
    interface: str,
    public_key: str,
    preshared_key: str,
    allowed_ips: str,
) -> None:
    """Add a peer to the running AWG interface."""
    cmd = [config.AWG_BIN, "set", interface, "peer", public_key]

    if preshared_key and preshared_key != "(none)":
        cmd.extend(["preshared-key", "/dev/stdin", "allowed-ips", allowed_ips])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(input=(preshared_key + "\n").encode())
    else:
        cmd.extend(["allowed-ips", allowed_ips])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"awg set peer failed: {stderr.decode().strip()}")


async def remove_peer(interface: str, public_key: str) -> None:
    """Remove a peer from the running AWG interface."""
    proc = await asyncio.create_subprocess_exec(
        config.AWG_BIN, "set", interface, "peer", public_key, "remove",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"awg set peer remove failed: {stderr.decode().strip()}")


async def remove_peers_batch(interface: str, public_keys: list[str]) -> None:
    """Remove multiple peers in a single awg set command."""
    if not public_keys:
        return
    args = [config.AWG_BIN, "set", interface]
    for key in public_keys:
        args.extend(["peer", key, "remove"])
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"awg set batch remove failed: {stderr.decode().strip()}")


async def add_peers_batch(interface: str, peers: list[dict]) -> int:
    """Add multiple peers in a single `awg addconf` call.

    Generates a temporary config with [Peer] sections and feeds it to
    `awg addconf <iface> <file>` — one subprocess instead of N.

    Each dict: {public_key, preshared_key, allowed_ips}.
    Returns count of peers added.
    """
    if not peers:
        return 0

    # Build config content with [Peer] sections
    config_lines: list[str] = []
    for p in peers:
        config_lines.append("[Peer]")
        config_lines.append(f"PublicKey = {p['public_key']}")
        psk = p.get("preshared_key", "(none)")
        if psk and psk != "(none)":
            config_lines.append(f"PresharedKey = {psk}")
        config_lines.append(f"AllowedIPs = {p['allowed_ips']}")
        config_lines.append("")  # blank line between peers

    # Write to temp file, run addconf, clean up
    fd, tmp_path = tempfile.mkstemp(prefix="awg_peers_", suffix=".conf")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(config_lines))

        proc = await asyncio.create_subprocess_exec(
            config.AWG_BIN, "addconf", interface, tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"awg addconf failed: {stderr.decode().strip()}")
    finally:
        os.unlink(tmp_path)

    return len(peers)
