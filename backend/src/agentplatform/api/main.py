"""``agentplatform-api`` entrypoint: run the FastAPI app with uvicorn.

* ``access_log=False``: uvicorn's access log includes query strings, and WebSocket
  URLs carry single-use tickets. ``RequestContextMiddleware`` logs paths only.
* ``ws_ping_interval``/``ws_ping_timeout`` = 20 s: protocol-level ping/pong for every
  WebSocket (SPEC T09 item 6); dead peers are dropped after one missed pong.
* Binds to ``APP_API_HOST`` (default ``127.0.0.1``: single local user, SPEC §16 auth v1).
"""

from __future__ import annotations

import uvicorn

from agentplatform.api.app import create_app
from agentplatform.config import load_settings
from agentplatform.logs import configure_logging

WS_PING_S = 20.0


def main() -> None:
    configure_logging()
    settings = load_settings()
    uvicorn.run(
        create_app(settings=settings),
        host=settings.app_api_host,
        port=settings.app_api_port,
        access_log=False,
        log_config=None,
        ws_ping_interval=WS_PING_S,
        ws_ping_timeout=WS_PING_S,
        proxy_headers=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
