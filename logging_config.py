"""structlog setup with JSON output and call_id correlation.

Bind a logger with `logger = log.bind(call_id=call_id)` per call so every
event in that call's context carries the call_id automatically.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    # The `websockets` library logs the full HTTP/WS handshake at DEBUG level,
    # including the `Authorization: Bearer xai-...` request header. With
    # LOG_LEVEL=DEBUG in .env this leaked the live xAI API key to stdout (and
    # any captured log file) on every connect. Pin the websockets loggers to
    # WARNING so app-level DEBUG never bleeds into protocol traces, regardless
    # of how the root logger is configured. Also do the same for `httpx` and
    # `httpcore`, which can leak HubSpot / Make.com / Supabase auth headers
    # the same way.
    for noisy in ("websockets", "websockets.client", "websockets.server",
                  "websockets.protocol", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger()
