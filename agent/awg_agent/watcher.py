"""Handshake watcher — monitors peer state changes and sends webhooks to master.

Runs as a background asyncio task. Every WATCHER_INTERVAL seconds it calls
`awg show <iface> dump` (local, ~1ms) and compares handshake timestamps
to detect connect/disconnect transitions. Events are POSTed to MASTER_WEBHOOK_URL.
"""

import asyncio
import logging
import time

import aiohttp

from . import awg, config

logger = logging.getLogger(__name__)

OFFLINE_THRESHOLD = 180  # seconds — local edge detector; master alerts use a higher threshold

# Peer states tracked by watcher
_prev_handshakes: dict[str, int] = {}  # public_key → last known handshake timestamp
_peer_online: dict[str, bool] = {}     # public_key → currently online?
_session: aiohttp.ClientSession | None = None
_task: asyncio.Task | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5),
            headers={
                "Authorization": f"Bearer {config.TOKEN}",
                "Content-Type": "application/json",
            },
        )
    return _session


async def _send_webhook(event: str, public_key: str, *, last_handshake: int | None = None) -> None:
    """POST event to master. Failures are logged but never crash the watcher."""
    if not config.MASTER_WEBHOOK_URL:
        return
    payload = {
        "event": event,
        "public_key": public_key,
        "timestamp": int(time.time()),
    }
    if last_handshake is not None:
        payload["last_handshake"] = int(last_handshake)
    try:
        session = await _get_session()
        async with session.post(config.MASTER_WEBHOOK_URL, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning("Webhook %s failed (%d): %s", event, resp.status, body[:200])
    except Exception as e:
        logger.debug("Webhook send error: %s", e)


async def _poll_loop() -> None:
    """Main watcher loop — detect connect/disconnect transitions."""
    logger.info("Watcher started, interval=%ds, url=%s",
                config.WATCHER_INTERVAL, config.MASTER_WEBHOOK_URL[:60] if config.MASTER_WEBHOOK_URL else "(none)")

    while True:
        try:
            now = int(time.time())
            peers = await awg.show_peers(config.INTERFACE)

            current_keys: set[str] = set()
            for peer in peers:
                pk = peer.public_key
                current_keys.add(pk)
                lh = peer.latest_handshake

                was_online = _peer_online.get(pk, False)

                # Determine current online state
                is_online = lh > 0 and (now - lh) < OFFLINE_THRESHOLD

                if is_online and not was_online:
                    # Transition: offline/new → online
                    logger.info("Peer %s..%s CONNECT (handshake=%d)", pk[:8], pk[-4:], lh)
                    await _send_webhook("connect", pk, last_handshake=lh)
                elif not is_online and was_online:
                    # Transition: online → offline
                    logger.info("Peer %s..%s DISCONNECT (last_hs=%d, age=%ds)",
                                pk[:8], pk[-4:], lh, now - lh if lh > 0 else -1)
                    await _send_webhook("disconnect", pk, last_handshake=lh)

                _prev_handshakes[pk] = lh
                _peer_online[pk] = is_online

            # Peers removed from AWG (e.g. peer deleted) → disconnect
            removed = set(_peer_online.keys()) - current_keys
            for pk in removed:
                if _peer_online.get(pk):
                    logger.info("Peer %s..%s REMOVED (was online)", pk[:8], pk[-4:])
                    await _send_webhook("disconnect", pk)
                del _peer_online[pk]
                _prev_handshakes.pop(pk, None)

        except Exception as e:
            logger.error("Watcher poll error: %s", e)

        await asyncio.sleep(config.WATCHER_INTERVAL)


def start() -> None:
    """Create and store the watcher background task."""
    global _task
    if not config.MASTER_WEBHOOK_URL:
        logger.info("MASTER_WEBHOOK_URL not set — watcher disabled")
        return
    if not config.INTERFACE:
        logger.info("AWG_INTERFACE not set — watcher disabled")
        return
    _task = asyncio.ensure_future(_poll_loop())


async def stop() -> None:
    """Cancel watcher and close HTTP session."""
    global _task, _session
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    if _session and not _session.closed:
        await _session.close()
        _session = None
