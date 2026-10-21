import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Fixed "now": Tuesday 2026-03-10 21:30 UTC = 17:30 EDT (after the close, DST already in force)
NOW = datetime(2026, 3, 10, 21, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AISP_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AISP_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("AISP_TELEGRAM_CHAT_ID", raising=False)
    return tmp_path / "home"


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def clock():
    return lambda: NOW
