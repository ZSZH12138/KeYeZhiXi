"""Private rotating diagnostics for the desktop parent process."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


_LOGGER_NAME = "course_insight.desktop"


def configure_launcher_logging(logs_dir: Path) -> logging.Logger:
    """Configure one UTF-8 rotating file handler without duplicate writes."""

    directory = Path(logs_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "launcher.log"
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in tuple(logger.handlers):
        owned_path = getattr(handler, "_course_insight_desktop_path", None)
        if owned_path == log_path:
            return logger
        if owned_path is not None:
            handler.close()
            logger.removeHandler(handler)

    handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler._course_insight_desktop_path = log_path  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        )
    )
    logger.addHandler(handler)
    return logger

