"""Readable, rotating local logs with credential redaction."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .security import SecretRedactionFilter


def configure_application_logging(
    log_directory: Path,
    *,
    known_secrets: Iterable[str] = (),
) -> logging.Logger:
    """Return the application logger configured with bounded, safe file output."""
    log_directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("workcall_transcriber")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(
        log_directory / "workcall-transcriber.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.addFilter(SecretRedactionFilter(known_secrets))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    return logger
