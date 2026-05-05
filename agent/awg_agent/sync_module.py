"""Peer sync module for automatic peer recovery.

Monitors and restores missing peers by comparing DB state with local WireGuard config.
Implements partial recovery strategy and adaptive sync intervals.
"""

import asyncio
import logging
import random
import time
from urllib.parse import urlsplit, urlunsplit
from typing import Optional

import aiohttp

from . import awg, config

logger = logging.getLogger(__name__)


def _derive_api_base_url(master_webhook_url: str) -> str:
    """Convert webhook URL into API base URL.

    Example:
      https://host/api/v1/webhooks/agent -> https://host/api/v1
    """
    if not master_webhook_url:
        return ""

    parsed = urlsplit(master_webhook_url)
    path = parsed.path.rstrip("/")
    suffix = "/webhooks/agent"
    if path.endswith(suffix):
        path = path[: -len(suffix)]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class PeerSync:
    """
    Periodic health check & peer recovery.

    - Sync intervals: aggressive (30s) after start → relaxed (5min)
    - API calls: GET expected peers from master → compare with local → push missing
    - Partial recovery: batch errors don't block individual peer pushes
    """

    def __init__(self, master_url: Optional[str] = None):
        self.master_url = master_url or config.MASTER_WEBHOOK_URL
        self.api_base_url = _derive_api_base_url(self.master_url)
        self.session: Optional[aiohttp.ClientSession] = None
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.successful_syncs = 0  # Track consecutive successes
        self.last_sync_time = 0.0
        self.sync_interval = 30  # Start with aggressive interval

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={
                    "Authorization": f"Bearer {config.TOKEN}",
                    "Content-Type": "application/json",
                },
            )
        return self.session

    async def _get_expected_peers(self) -> Optional[list]:
        """
        Fetch expected peers from master API.

        Returns list of peer dicts or None on error.
        """
        if not self.api_base_url:
            logger.warning("No usable master API base URL configured, skipping sync")
            return None

        url = f"{self.api_base_url}/servers/peers/expected"
        session = await self._get_session()

        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("peers", [])
                elif resp.status == 404:
                    logger.warning("No unit mapped to this exit server on master")
                    return []
                else:
                    logger.error("Master API error %d: %s", resp.status, await resp.text())
                    return None
        except aiohttp.ClientError as e:
            logger.error("Master API connection error: %s", e)
            return None
        except Exception as e:
            logger.error("Unexpected error fetching expected peers: %s", e)
            return None

    async def _get_local_peers(self) -> list:
        """Get current peers from local WireGuard interface."""
        try:
            return await awg.show_peers(config.INTERFACE)
        except Exception as e:
            logger.error("Error reading local WireGuard peers: %s", e)
            return []

    def _get_missing_peers(self, expected: list, local: list) -> list:
        """Compare expected vs local and return missing peers."""
        if not expected:
            return []

        local_pubkeys = {p.public_key for p in local}
        missing = [p for p in expected if p["public_key"] not in local_pubkeys]
        logger.debug("Peers check: expected=%d, local=%d, missing=%d",
                    len(expected), len(local), len(missing))
        return missing

    async def _restore_peers(self, missing_peers: list) -> dict:
        """
        Restore missing peers using partial recovery strategy.

        Returns: {"batch_failed": bool, "individual_recovered": int, "total_restored": int}
        """
        if not missing_peers:
            return {"batch_failed": False, "individual_recovered": 0, "total_restored": 0}

        logger.info("Starting peer restore: %d peers", len(missing_peers))

        result = {
            "batch_failed": False,
            "individual_recovered": 0,
            "total_restored": 0,
        }

        # Try batch restore first
        try:
            peer_data = [
                {
                    "public_key": p["public_key"],
                    "preshared_key": p["preshared_key"],
                    "allowed_ips": p["allowed_ips"],
                }
                for p in missing_peers
            ]
            await awg.add_peers_batch(config.INTERFACE, peer_data)
            result["total_restored"] = len(missing_peers)
            logger.info("Batch restore OK: %d peers", len(missing_peers))
            return result
        except Exception as batch_error:
            logger.warning("Batch restore failed, trying individual: %s", batch_error)
            result["batch_failed"] = True

            # Fallback to individual peer restores
            for peer in missing_peers:
                try:
                    await awg.add_peer(
                        config.INTERFACE,
                        peer["public_key"],
                        peer["preshared_key"],
                        peer["allowed_ips"],
                    )
                    result["individual_recovered"] += 1
                    result["total_restored"] += 1
                    logger.info("Individual restore OK: %s", peer["public_key"][:16])
                except Exception as individual_error:
                    logger.error("Individual restore failed: %s - %s",
                               peer["public_key"][:16], individual_error)

            return result

    async def _sync_cycle(self) -> None:
        """Single sync cycle: fetch → compare → restore."""
        start_time = time.time()

        try:
            # Step 1: Fetch expected peers from master
            expected = await self._get_expected_peers()
            if expected is None:
                logger.warning("Failed to fetch expected peers, skipping sync")
                return

            # Step 2: Get local peers from WireGuard
            local = await self._get_local_peers()

            # Step 3: Find missing peers
            missing = self._get_missing_peers(expected, local)

            # Step 4: Restore missing peers (if any)
            if missing:
                restore_result = await self._restore_peers(missing)
                logger.info("Sync complete: %d missing, %d restored",
                           len(missing), restore_result["total_restored"])
            else:
                logger.debug("Sync complete: no missing peers")

            # Update state
            self.successful_syncs += 1
            self.last_sync_time = time.time()

            # Adapt interval: after 3 successful syncs → 5 min
            if self.successful_syncs >= 3 and self.sync_interval < 300:
                self.sync_interval = 300
                logger.info("Adaptive interval: %ds (relaxed mode)", self.sync_interval)

            sync_duration = time.time() - start_time
            logger.info("Sync cycle completed in %.2fs", sync_duration)

        except Exception as e:
            logger.error("Sync cycle error: %s", e)
            self.successful_syncs = 0  # Reset on error

    async def _sync_loop(self) -> None:
        """Main sync loop with adaptive intervals."""
        logger.info(
            "Peer sync started, interval=%ds, api_base=%s",
            self.sync_interval,
            self.api_base_url[:60] if self.api_base_url else "(none)",
        )

        while self.running:
            try:
                await self._sync_cycle()
            except Exception as e:
                logger.error("Sync loop error: %s", e)

            # Wait for next cycle with jitter to avoid thundering herd
            jitter = random.uniform(0.8, 1.2)
            await asyncio.sleep(self.sync_interval * jitter)

    def start(self) -> None:
        """Start peer sync background task."""
        if self.running:
            logger.warning("Peer sync already running")
            return

        self.running = True
        self.successful_syncs = 0
        self.sync_interval = 30  # Reset to aggressive interval

        self.task = asyncio.ensure_future(self._sync_loop())
        logger.info("Peer sync task started")

    async def stop(self) -> None:
        """Stop peer sync background task."""
        if not self.running:
            return

        logger.info("Stopping peer sync...")
        self.running = False

        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None

        logger.info("Peer sync stopped")


# Global sync instance (will be initialized with unit_id from config/environment)
_sync_instance: Optional[PeerSync] = None


def get_sync_instance(master_url: Optional[str] = None) -> PeerSync:
    """Get or create global sync instance."""
    global _sync_instance
    if _sync_instance is None:
        _sync_instance = PeerSync(master_url=master_url)
    return _sync_instance
