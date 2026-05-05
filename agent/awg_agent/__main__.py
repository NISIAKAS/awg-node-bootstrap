"""Entry point: python -m awg_agent"""

import asyncio
import logging

from aiohttp import web

from .config import PORT, LOG_LEVEL
from .server import create_app
from . import watcher
from . import sync_module
from . import enroll
from . import config


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    if not config.TOKEN and not (config.ENROLLMENT_SECRET and config.MASTER_ENROLL_URL and config.SERVER_ID):
        logging.getLogger(__name__).warning(
            "AWG_AGENT_TOKEN not set and enrollment is not configured — API endpoints will refuse all requests"
        )
    app = create_app()

    async def on_startup(app: web.Application) -> None:
        enrolled = await enroll.ensure_runtime_token()
        if not config.TOKEN:
            logging.getLogger(__name__).info(
                "Runtime token not available after startup enrollment=%s — watcher and peer sync disabled",
                enrolled,
            )
            return

        watcher.start()
        sync_instance = sync_module.get_sync_instance()
        if sync_instance.api_base_url:
            sync_instance.start()
        else:
            logging.getLogger(__name__).info(
                "MASTER_WEBHOOK_URL not set or invalid — peer sync disabled"
            )

    async def on_cleanup(app: web.Application) -> None:
        await watcher.stop()
        # Stop peer sync module
        sync_instance = sync_module.get_sync_instance()
        if sync_instance.running:
            await sync_instance.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()

