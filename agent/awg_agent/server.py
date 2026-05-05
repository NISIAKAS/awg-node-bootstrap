"""HTTP server — mounts API routes and Prometheus metrics endpoint."""

import logging

from aiohttp import web

from . import api, metrics

logger = logging.getLogger(__name__)


async def metrics_handler(request: web.Request) -> web.Response:
    """GET /metrics — Prometheus scrape endpoint (no auth)."""
    text = await metrics.generate_metrics()
    return web.Response(
        body=text.encode(),
        content_type="text/plain",
        charset="utf-8",
    )


def create_app() -> web.Application:
    """Build and return the aiohttp Application with all routes."""
    app = web.Application()

    # Prometheus metrics (no auth — port is firewalled to management server)
    app.router.add_get("/metrics", metrics_handler)

    # Health check (no auth)
    app.router.add_get("/health", api.health)

    # Peer management API (Bearer token required)
    app.router.add_get("/api/v1/peers", api.list_peers)
    app.router.add_post("/api/v1/peers", api.add_peer)
    app.router.add_delete("/api/v1/peers/{public_key}", api.remove_peer)
    app.router.add_post("/api/v1/peers/batch", api.batch_add_peers)
    app.router.add_post("/api/v1/peers/batch-remove", api.batch_remove_peers)
    app.router.add_post("/api/v1/node/apply-awg-config", api.apply_awg_config)
    app.router.add_post("/api/v1/node/relay-binding/reconcile", api.reconcile_relay_binding)
    app.router.add_post("/api/v1/node/relay-binding/remove", api.remove_relay_binding)
    app.router.add_post("/api/v1/node/firewall/allow-udp-source", api.allow_udp_source)
    app.router.add_post("/api/v1/node/firewall/remove-udp-source", api.remove_udp_source)

    return app
