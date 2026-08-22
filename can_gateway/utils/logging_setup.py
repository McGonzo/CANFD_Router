from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from can_gateway.utils.app_paths import app_path


def setup_logging(log_dir: str = "logs", level: int = logging.INFO) -> logging.Logger:
    log_path = Path(log_dir)
    if not log_path.is_absolute():
        log_path = app_path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("can_gateway")
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_path / "gateway.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console)
    return logger
