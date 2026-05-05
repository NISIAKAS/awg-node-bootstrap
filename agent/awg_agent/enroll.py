"""Startup enrollment with the master server."""

import asyncio
import logging
import socket

import aiohttp

from . import config

logger = logging.getLogger(__name__)


async def ensure_runtime_token(max_attempts: int = 5) -> bool:
    """Enroll the node with master and fetch runtime token if needed."""
    if config.TOKEN:
        return True
    if not (config.ENROLLMENT_SECRET and config.MASTER_ENROLL_URL and config.SERVER_ID):
        return False

    payload = {
        "server_id": config.SERVER_ID,
        "enrollment_secret": config.ENROLLMENT_SECRET,
        "hostname": socket.gethostname(),
        "node_port": config.PORT,
        "interface": config.INTERFACE,
        "agent_version": config.AGENT_VERSION,
    }

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(1, max_attempts + 1):
            try:
                async with session.post(config.MASTER_ENROLL_URL, json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(
                            "Node enrollment failed (%d/%d) status=%d: %s",
                            attempt,
                            max_attempts,
                            resp.status,
                            body[:200],
                        )
                    else:
                        data = await resp.json()
                        token = data.get("runtime_token", "")
                        if not token:
                            logger.warning("Node enrollment response missing runtime_token")
                            return False
                        config.persist_runtime_state(
                            token,
                            data.get("master_webhook_url", ""),
                        )
                        logger.info(
                            "Node enrolled with master: server_id=%s node_port=%s",
                            config.SERVER_ID,
                            data.get("node_port"),
                        )
                        return True
            except Exception as e:
                logger.warning(
                    "Node enrollment error (%d/%d): %s",
                    attempt,
                    max_attempts,
                    e,
                )

            if attempt < max_attempts:
                await asyncio.sleep(min(5 * attempt, 20))

    return False
