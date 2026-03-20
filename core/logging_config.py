"""
core/logging_config.py

Structured logging for DevAgent.
Call setup_logging() once at startup (in webhook.py or cli/main.py).

All modules use the standard pattern:
    import logging
    logger = logging.getLogger(__name__)
"""

from __future__ import annotations

import logging
import os
import sys


def setup_logging() -> None:
    """
    Configure logging for the entire application.
    Log level is read from DEVAGENT_LOG_LEVEL (default: INFO).
    Call this once at application startup before importing other modules.
    """
    level_name = os.environ.get("DEVAGENT_LOG_LEVEL", "INFO").upper()
    level      = getattr(logging, level_name, logging.INFO)

    fmt     = "%(asctime)s · %(levelname)-8s · %(name)s · %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%SZ"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # Quiet noisy third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3", "github",
                  "openai", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
