from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

DEFAULT_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def configure_logging(
    log_dir: Path | None = None,
    log_file_name: str = "agent.log",
    level: int = logging.INFO,
    format_string: str = DEFAULT_LOG_FORMAT,
) -> Path:
    """Configure structured rotating file logging for PersonalJobAgent.

    Ensures logs are cleanly rotated and persisted in the local data directory
    without cluttering the console or stdout.
    """
    if log_dir is None:
        from job_agent.settings import get_settings
        log_dir = get_settings().project_root / "data" / "logs"

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_file_name

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Check if this log file is already configured to prevent duplicate logs
    for handler in root_logger.handlers:
        if (
            isinstance(handler, logging.handlers.RotatingFileHandler)
            and getattr(handler, "baseFilename", None) == str(log_path.resolve())
        ):
            return log_path

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(format_string))
    root_logger.addHandler(file_handler)

    return log_path
