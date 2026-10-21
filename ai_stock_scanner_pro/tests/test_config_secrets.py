import pytest

from aiscanner import secrets
from aiscanner.config import ConfigError, Settings, load_settings, save_settings


def test_defaults_valid_and_roundtrip(tmp_path):
    s = load_settings(tmp_path / "none.yaml")
    assert s.risk.risk_per_trade == 0.01 and s.risk.max_positions == 10
    assert s.data.provider == "yahoo"        # real data by default; demo data is an explicit opt-in
    p = save_settings(s, tmp_path / "c.yaml")
    assert load_settings(p) == s


def test_override_and_unknown_key(tmp_path):
    (tmp_path / "c.yaml").write_text("risk:\n  max_positions: 7\nscan:\n  min_price: 8\n")
    s = load_settings(tmp_path / "c.yaml")
    assert s.risk.max_positions == 7 and s.scan.min_price == 8.0
    (tmp_path / "d.yaml").write_text("risk:\n  bogus: 1\n")
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "d.yaml")


@pytest.mark.parametrize("mut", [
    lambda s: setattr(s.risk, "risk_per_trade", 0.5),
    lambda s: setattr(s.risk, "max_positions", 0),
    lambda s: s.scan.swing_weights.update(trend=50),
    lambda s: setattr(s.scan, "band_high", 50),
    lambda s: setattr(s.alerts, "quiet_start", "25:00"),
    lambda s: setattr(s.data, "provider", "bloomberg"),
])
def test_invalid_settings_rejected(mut):
    s = Settings()
    mut(s)
    with pytest.raises(ConfigError):
        s.validate()


def test_secrets_roundtrip_not_plaintext_on_disk_and_env_override(monkeypatch):
    assert not secrets.load_telegram().configured
    secrets.save_telegram("123456:ABC-secret-token", "-100777")
    c = secrets.load_telegram()
    assert c.configured and c.token == "123456:ABC-secret-token" and c.chat_id == "-100777"
    raw = secrets.paths.secrets_path().read_text()
    assert "ABC-secret-token" not in raw          # at least base64-obscured; DPAPI-encrypted on Windows
    monkeypatch.setenv("AISP_TELEGRAM_TOKEN", "envtok")
    monkeypatch.setenv("AISP_TELEGRAM_CHAT_ID", "42")
    assert secrets.load_telegram().token == "envtok"
    assert secrets.mask("123456:ABC-secret-token").startswith("1234")
    secrets.clear_telegram()
    monkeypatch.delenv("AISP_TELEGRAM_TOKEN")
    assert not secrets.load_telegram().configured


@pytest.mark.skipif(__import__("os").name != "nt", reason="DPAPI exists only on Windows – run this test on the target PC")
def test_dpapi_roundtrip_and_ciphertext_differs_windows_only():
    blob, protected = secrets._protect("123456:SECRET")
    assert protected and "SECRET" not in __import__("base64").b64decode(blob).decode("latin1")
    assert secrets._unprotect(blob, True) == "123456:SECRET"
