"""
core/logger.py — Structured logging for the application
"""
import logging
import logging.handlers
import os
from datetime import datetime


def make_log_entry(level: str, msg: str) -> dict:
    return {
        "time":  datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "msg":   msg,
    }


def setup_logging(log_dir: str = "./logs", level: str = "INFO"):
    """Configure root logger with file rotation + console output."""
    os.makedirs(log_dir, exist_ok=True)
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Rotating file — info
    info_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(fmt)
    root.addHandler(info_handler)

    # Rotating file — errors only
    err_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "error.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    err_handler.setLevel(logging.ERROR)
    err_handler.setFormatter(fmt)
    root.addHandler(err_handler)

    logging.getLogger("werkzeug").setLevel(logging.WARNING)   # quieten Flask dev server
    logging.info("Logging initialised. Level=%s  Dir=%s", level, log_dir)
