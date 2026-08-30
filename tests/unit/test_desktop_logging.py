from __future__ import annotations

import logging
from pathlib import Path

from course_insight.desktop.logging import configure_launcher_logging


def test_launcher_logging_writes_once_to_the_persistent_log_directory(
    tmp_path: Path,
) -> None:
    logger = configure_launcher_logging(tmp_path)
    repeated = configure_launcher_logging(tmp_path)

    logger.error("desktop lifecycle diagnostic marker")
    for handler in logger.handlers:
        handler.flush()

    assert repeated is logger
    assert len(logger.handlers) == 1
    content = (tmp_path / "launcher.log").read_text(encoding="utf-8")
    assert content.count("desktop lifecycle diagnostic marker") == 1

    for handler in tuple(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
