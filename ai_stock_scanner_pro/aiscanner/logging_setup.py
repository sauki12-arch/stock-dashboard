"""Rotating file log under %APPDATA%/AIStockScannerPro/logs (never logs credentials)."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from . import paths


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    h = RotatingFileHandler(paths.log_dir() / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)
    root.setLevel(level)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
