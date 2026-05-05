"""Prometheus metrics generation — AWG-specific metrics in text exposition format."""

import time
import logging

from . import awg, config

logger = logging.getLogger(__name__)

HANDSHAKE_ONLINE_THRESHOLD = 180  # seconds — peer considered online if handshake < 3 min


def _read_proc_value(path: str, default: str = "0") -> str:
    """Read a single value from /proc or /sys."""
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, IOError):
        return default


def _read_tcp_retransmits() -> int:
    """Read TCPRetransSegs from /proc/net/netstat."""
    try:
        with open("/proc/net/netstat") as f:
            lines = f.readlines()
        # /proc/net/netstat has paired header/value lines
        for i in range(0, len(lines) - 1, 2):
            if lines[i].startswith("TcpExt:"):
                keys = lines[i].strip().split()
                values = lines[i + 1].strip().split()
                kv = dict(zip(keys, values))
                return int(kv.get("TCPRetransSegs", 0))
    except (OSError, IOError, ValueError):
        pass
    return 0


async def generate_metrics() -> str:
    """Generate Prometheus text exposition format metrics."""
    lines: list[str] = []
    now = time.time()

    # --- AWG peer metrics (skipped if interface not configured or not running) ---
    try:
        peers = await awg.show_peers(config.INTERFACE) if config.INTERFACE else []
    except Exception:
        # AWG interface not available (relay node or service down) — skip peer metrics
        peers = []

    online_peers = 0

    lines.append("# HELP awg_peers_total Total number of configured AWG peers")
    lines.append("# TYPE awg_peers_total gauge")
    lines.append(f"awg_peers_total {len(peers)}")

    if peers:
        lines.append("# HELP awg_peer_rx_bytes Received bytes per peer")
        lines.append("# TYPE awg_peer_rx_bytes gauge")
        for p in peers:
            lbl = f'public_key="{p.public_key}",allowed_ips="{p.allowed_ips}"'
            lines.append(f"awg_peer_rx_bytes{{{lbl}}} {p.transfer_rx}")

        lines.append("# HELP awg_peer_tx_bytes Transmitted bytes per peer")
        lines.append("# TYPE awg_peer_tx_bytes gauge")
        for p in peers:
            lbl = f'public_key="{p.public_key}",allowed_ips="{p.allowed_ips}"'
            lines.append(f"awg_peer_tx_bytes{{{lbl}}} {p.transfer_tx}")

        lines.append("# HELP awg_peer_last_handshake_seconds Seconds since last handshake (-1 = never)")
        lines.append("# TYPE awg_peer_last_handshake_seconds gauge")
        for p in peers:
            lbl = f'public_key="{p.public_key}",allowed_ips="{p.allowed_ips}"'
            if p.latest_handshake > 0:
                age = now - p.latest_handshake
                lines.append(f"awg_peer_last_handshake_seconds{{{lbl}}} {age:.0f}")
                if age < HANDSHAKE_ONLINE_THRESHOLD:
                    online_peers += 1
            else:
                lines.append(f"awg_peer_last_handshake_seconds{{{lbl}}} -1")

    lines.append("# HELP awg_peers_online Peers with recent handshake (< 3 min)")
    lines.append("# TYPE awg_peers_online gauge")
    lines.append(f"awg_peers_online {online_peers}")

    # --- Conntrack ---
    conntrack_count = _read_proc_value("/proc/sys/net/netfilter/nf_conntrack_count")
    conntrack_max = _read_proc_value("/proc/sys/net/netfilter/nf_conntrack_max")

    lines.append("# HELP awg_conntrack_entries Current conntrack entries")
    lines.append("# TYPE awg_conntrack_entries gauge")
    lines.append(f"awg_conntrack_entries {conntrack_count}")

    lines.append("# HELP awg_conntrack_max Maximum conntrack entries")
    lines.append("# TYPE awg_conntrack_max gauge")
    lines.append(f"awg_conntrack_max {conntrack_max}")

    # --- TCP retransmits ---
    retrans = _read_tcp_retransmits()
    lines.append("# HELP awg_tcp_retransmits_total Total TCP retransmitted segments")
    lines.append("# TYPE awg_tcp_retransmits_total counter")
    lines.append(f"awg_tcp_retransmits_total {retrans}")

    lines.append("")  # trailing newline required by spec
    return "\n".join(lines)
