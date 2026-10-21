from datetime import datetime, timedelta, timezone

import pytest

from aiscanner.alerts.manager import (AlertManager, DEF_Q, DEF_R, FAILED, NO_TG, REC, SENT, detect_new_setups,
                                      in_quiet_hours)
from aiscanner.alerts.telegram import TelegramClient
from aiscanner.config import AlertCfg
from aiscanner.data.store import Store
from aiscanner.models import CRITICAL, INFO, WARNING, Alert

DAY = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)        # 14:00 Jerusalem (IST, UTC+2) – outside quiet hours


class Sender:
    def __init__(self, ok=True):
        self.msgs, self.ok = [], ok

    def __call__(self, text):
        from aiscanner.alerts.telegram import SendResult
        self.msgs.append(text)
        return SendResult(self.ok, 200 if self.ok else 500, "" if self.ok else "boom")


def mgr(sender=None, now=DAY, **cfg):
    clock = {"t": now}
    m = AlertManager(Store(":memory:"), AlertCfg(**cfg), sender, lambda: clock["t"])
    m._clock = clock
    return m


def crit(key="k1", title="T", body="B"):
    return Alert(key, CRITICAL, "stop_breach", title, body, "AAA")


# ----------------------------------------------------------------- quiet hours
@pytest.mark.parametrize("hour,expected", [(20, False), (21, True), (0, True), (4, True), (5, False), (12, False)])
def test_quiet_hours_wrap_midnight(hour, expected):
    # quiet 23:00-07:00 Jerusalem; Jerusalem = UTC+2 in March (before DST start on the last Friday of March)
    now = datetime(2026, 3, 10, hour, 0, tzinfo=timezone.utc)
    assert in_quiet_hours(now, "23:00", "07:00", "Asia/Jerusalem") is expected


def test_quiet_hours_same_day_window_and_disabled():
    now = datetime(2026, 3, 10, 10, 0, tzinfo=timezone.utc)      # 12:00 local
    assert in_quiet_hours(now, "11:00", "13:00", "Asia/Jerusalem")
    assert not in_quiet_hours(now, "13:00", "13:00", "Asia/Jerusalem")


def test_quiet_hours_follow_dst():
    # 2026-07-01 Jerusalem is UTC+3: 20:30 UTC = 23:30 local => quiet
    assert in_quiet_hours(datetime(2026, 7, 1, 20, 30, tzinfo=timezone.utc), "23:00", "07:00", "Asia/Jerusalem")
    assert not in_quiet_hours(datetime(2026, 3, 10, 20, 30, tzinfo=timezone.utc), "23:00", "07:00", "Asia/Jerusalem")


# ----------------------------------------------------------------- policy
def test_only_critical_is_sent():
    s = Sender()
    m = mgr(s)
    out = m.process([crit("a"), Alert("b", WARNING, "near_stop", "w", "w", "AAA"), Alert("c", INFO, "x", "i", "i")])
    assert [o.status for o in out] == [SENT, REC, REC]
    assert len(s.msgs) == 1 and "T" in s.msgs[0] and "תמיכת החלטה בלבד" in s.msgs[0]
    assert len(m.history()) == 3


def test_duplicates_suppressed_within_window_then_allowed():
    s = Sender()
    m = mgr(s, dedupe_hours=24)
    assert m.process([crit("same")])[0].status == SENT
    assert m.process([crit("same")])[0].status == "duplicate_suppressed"
    assert len(s.msgs) == 1 and len(m.history()) == 1
    m._clock["t"] = DAY + timedelta(hours=25)
    assert m.process([crit("same")])[0].status == SENT
    # duplicates inside a single batch too
    m2 = mgr(Sender())
    assert [o.status for o in m2.process([crit("z"), crit("z")])] == [SENT, "duplicate_suppressed"]


def test_no_credentials_records_but_does_not_send():
    m = mgr(None)
    assert m.process([crit()])[0].status == NO_TG
    assert m.history()[0]["status"] == NO_TG


def test_disabled_flag():
    s = Sender()
    m = mgr(s, enabled=False)
    assert m.process([crit()])[0].status == NO_TG and not s.msgs


def test_quiet_hours_defer_then_flush_after_wake():
    s = Sender()
    night = datetime(2026, 3, 10, 22, 0, tzinfo=timezone.utc)     # 00:00 Jerusalem
    m = mgr(s, now=night)
    assert m.process([crit("n1")])[0].status == DEF_Q and not s.msgs
    assert m.flush_deferred()[0].status == DEF_Q                   # still quiet
    m._clock["t"] = datetime(2026, 3, 11, 6, 0, tzinfo=timezone.utc)     # 08:00 local
    out = m.flush_deferred()
    assert out[0].status == SENT and len(s.msgs) == 1
    assert m.history()[0]["status"] == SENT and m.history()[0]["sent_ts"]


def test_rate_limit_defers_and_recovers():
    s = Sender()
    m = mgr(s, max_per_hour=2)
    out = m.process([crit(f"r{i}") for i in range(4)])
    assert [o.status for o in out] == [SENT, SENT, DEF_R, DEF_R]
    m._clock["t"] = DAY + timedelta(minutes=61)
    out = m.flush_deferred()
    assert [o.status for o in out][:2] == [SENT, SENT] and len(s.msgs) == 4


def test_failed_send_is_retried_and_old_alerts_expire():
    s = Sender(ok=False)
    m = mgr(s)
    assert m.process([crit("f")])[0].status == FAILED
    s.ok = True
    assert m.flush_deferred()[0].status == SENT
    m2 = mgr(Sender(ok=False))
    m2.process([crit("old")])
    m2._clock["t"] = DAY + timedelta(hours=30)
    assert m2.flush_deferred()[0].status == "expired"


# ----------------------------------------------------------------- Telegram client
class FakeResp:
    def __init__(self, code, data):
        self.status_code, self._d = code, data

    def json(self):
        return self._d


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_telegram_success_payload():
    fs = FakeSession([FakeResp(200, {"ok": True})])
    c = TelegramClient("123:SECRET", "-100", session=fs)
    assert c.send("שלום").ok
    url, payload, timeout = fs.calls[0]
    assert url == "https://api.telegram.org/bot123:SECRET/sendMessage"
    assert payload["chat_id"] == "-100" and payload["text"] == "שלום" and timeout == 10.0


def test_telegram_error_redacts_token():
    fs = FakeSession([RuntimeError("failed to reach https://api.telegram.org/bot123:SECRET/sendMessage")])
    r = TelegramClient("123:SECRET", "-100", session=fs).send("x")
    assert not r.ok and "SECRET" not in r.error


def test_telegram_http_error_and_429_retry():
    fs = FakeSession([FakeResp(400, {"ok": False, "description": "chat not found"})])
    r = TelegramClient("123:ABCdef", "42", session=fs).send("x")
    assert not r.ok and r.status == 400 and "chat not found" in r.error
    sleeps = []
    fs = FakeSession([FakeResp(429, {"ok": False, "parameters": {"retry_after": 3}}), FakeResp(200, {"ok": True})])
    r = TelegramClient("123:ABCdef", "42", session=fs, sleep=sleeps.append).send("x")
    assert r.ok and sleeps == [3] and len(fs.calls) == 2


def test_telegram_truncates_and_validates():
    fs = FakeSession([FakeResp(200, {"ok": True})])
    TelegramClient("123:ABCdef", "42", session=fs).send("x" * 10_000)
    assert len(fs.calls[0][1]["text"]) == 4096
    with pytest.raises(ValueError):
        TelegramClient("", "c")


# ----------------------------------------------------------------- new setup detection
class FakeCand:
    def __init__(self, sym, score):
        self.symbol, self._s = sym, score
        self.scores = {"swing": {"levels": {"entry": 10, "stop": 9, "target": 12, "rr": 2.0}},
                       "longterm": {"levels": {"entry": 10, "stop": 9, "target": 12, "rr": 2.0}}}

    def score(self, st):
        return self._s


class FakeScan:
    def __init__(self, cands, regime="RISK_ON", synthetic=False, as_of="2026-03-10"):
        self.cands, self.regime, self.synthetic, self.as_of = cands, {"regime": regime}, synthetic, as_of

    def ranked(self, st, n=None):
        return self.cands


def test_new_setup_only_when_newly_qualifying():
    cfg = AlertCfg(new_setup_min_score=85)
    prev = FakeScan([FakeCand("OLD", 90)])
    cur = FakeScan([FakeCand("OLD", 91), FakeCand("NEW", 88), FakeCand("LOW", 70)])
    al = detect_new_setups(prev, cur, cfg)
    assert {a.symbol for a in al} == {"NEW"} and all(a.severity == CRITICAL for a in al)
    assert len(al) == 2                                   # one per strategy
    assert {a.symbol for a in detect_new_setups(None, cur, cfg)} == {"OLD", "NEW"}


def test_new_setup_suppressed_in_risk_off_and_downgraded_when_synthetic():
    cfg = AlertCfg()
    assert detect_new_setups(None, FakeScan([FakeCand("A", 95)], regime="RISK_OFF"), cfg) == []
    al = detect_new_setups(None, FakeScan([FakeCand("A", 95)], synthetic=True), cfg)
    assert al and all(a.severity == INFO and a.title.startswith("[סינתטי]") for a in al)


def test_new_setup_key_is_weekly_so_it_does_not_repeat_daily():
    cfg = AlertCfg()
    a1 = detect_new_setups(None, FakeScan([FakeCand("A", 95)], as_of="2026-03-09"), cfg)[0].key
    a2 = detect_new_setups(None, FakeScan([FakeCand("A", 95)], as_of="2026-03-11"), cfg)[0].key
    a3 = detect_new_setups(None, FakeScan([FakeCand("A", 95)], as_of="2026-03-17"), cfg)[0].key
    assert a1 == a2 != a3
