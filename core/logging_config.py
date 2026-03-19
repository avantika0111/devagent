"""
core/logging_config.py

Structured logging for DevAgent.
Call setup_logging() once at startup (in webhook.py or cli/main.py).

All modules use:
    import logging
    logger = logging.getLogger(__name__)

This gives clean, consistent output with the right level and format.
"""

from __future__ import annotations

import logging
import os
import sys


def setup_logging() -> None:
    """
    Configure logging for the entire application.

    Log level is read from DEVAGENT_LOG_LEVEL (default: INFO).
    Format is plain text for development, structured for production.

    Call this once at application startup.
    """
    level_name = os.environ.get("DEVAGENT_LOG_LEVEL", "INFO").upper()
    level      = getattr(logging, level_name, logging.INFO)

    # Format: timestamp · level · module · message
    fmt = "%(asctime)s · %(levelname)-8s · %(name)s · %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%SZ"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # Quiet noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("github").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
