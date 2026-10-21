"""Filesystem locations. `AISP_HOME` overrides everything (used by tests)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Directory holding bundled read-only resources (config templates, universe)."""
    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "config"
    return PACKAGE_DIR.parent / "config"


def app_home() -> Path:
    env = os.environ.get("AISP_HOME")
    if env:
        p = Path(env)
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA", Path.home())) / "AIStockScannerPro"
    else:
        p = Path.home() / ".config" / "AIStockScannerPro"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    return app_home() / "config.yaml"


def secrets_path() -> Path:
    return app_home() / "secrets.json"


def db_path() -> Path:
    return app_home() / "scanner.db"


def log_dir() -> Path:
    p = app_home() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_universe_path() -> Path:
    return resource_dir() / "universe_default.csv"
