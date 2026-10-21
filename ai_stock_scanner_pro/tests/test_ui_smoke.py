"""GUI smoke tests on Qt's offscreen platform (no display needed)."""
import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QEventLoop, QItemSelectionModel as S, Qt, QTimer                      # noqa: E402
from PySide6.QtWidgets import QApplication                                                         # noqa: E402

from aiscanner.config import Settings                                                              # noqa: E402
from aiscanner.core import AppCore                                                                 # noqa: E402
from aiscanner.data.universe import Universe, load_universe                                        # noqa: E402
from aiscanner.portfolio.store import Position                                                     # noqa: E402

NOW = datetime(2026, 3, 10, 21, 30, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def qapp():
    from aiscanner.ui import theme
    app = QApplication.instance() or QApplication([])
    app.setLayoutDirection(Qt.RightToLeft)
    app.setStyleSheet(theme.QSS)
    return app


@pytest.fixture
def win(qapp, tmp_path):
    from aiscanner.ui.main_window import MainWindow
    full = load_universe()
    syms = ["AAPL", "MSFT"] + [x for x in full.symbols if x not in ("AAPL", "MSFT")][:23]
    u = Universe(symbols=syms, sector={x: full.sector[x] for x in syms})
    from aiscanner.data.mock import MockProvider
    st_ = Settings()
    st_.barchart.enabled = False
    core = AppCore(st_, clock=lambda: NOW, db_path=tmp_path / "ui.db", universe=u, sleep=lambda s: None,
                   provider=MockProvider(now=lambda: NOW, sector_map=u.sector))
    core.update_data()
    core.scan()
    w = MainWindow(core)
    w.show()
    qapp.processEvents()
    yield w
    w.close()


def wait_for(qapp, cond, timeout_ms=120_000):
    loop = QEventLoop()
    t = QTimer()
    t.setInterval(50)
    elapsed = {"ms": 0}

    def tick():
        elapsed["ms"] += 50
        if cond() or elapsed["ms"] > timeout_ms:
            loop.quit()
    t.timeout.connect(tick)
    t.start()
    loop.exec()
    t.stop()
    return cond()


def test_rtl_and_nine_screens(win, qapp):
    assert win.layoutDirection() == Qt.RightToLeft and qapp.layoutDirection() == Qt.RightToLeft
    assert win.nav.count() == 9 and win.stack.count() == 9
    texts = [win.nav.item(i).text() for i in range(9)]
    assert "סורק גילוי" in texts and "מרכז סיכונים" in texts and "הגדרות ואבחון" in texts
    for i in range(9):
        win.nav.setCurrentRow(i)
        win.refresh_all()
        qapp.processEvents()
        assert win.stack.currentIndex() == i
    assert win.mode_banner.isVisible() or "סינתטי" in win.mode_banner.text()


def test_discovery_and_strategy_screens_populate_with_filters(win, qapp):
    disc, swing, lt = win.screens[2], win.screens[3], win.screens[4]
    for scr in (disc, swing, lt):
        scr.refresh()
    assert len(disc.model.rows) == 25 and disc.proxy.rowCount() > 0
    assert swing.proxy.rowCount() <= len(swing.model.rows)
    # every row shown on the swing screen is validated and ranked
    for r in range(swing.proxy.rowCount()):
        row = swing.model.rows[swing.proxy.mapToSource(swing.proxy.index(r, 0)).row()]
        assert row["rank"] > 0 and row["scores"]["swing"]["validated"]
    disc.search.setText("AAPL")
    assert disc.proxy.rowCount() <= 1
    disc.search.setText("")
    disc.min_score.setValue(101)
    assert disc.proxy.rowCount() == 0
    disc.min_score.setValue(0)
    # Top-N limits
    disc.valid_only.setChecked(True)
    disc.topn.setCurrentIndex(0)
    assert disc.proxy.rowCount() <= 10


def test_detail_pane_explains_score(win, qapp):
    lt = win.screens[4]
    lt.refresh()
    lt.view.selectionModel().select(lt.proxy.index(0, 0), S.ClearAndSelect | S.Rows)
    qapp.processEvents()
    html = lt.detail.text.toPlainText()
    assert "פירוט ציון" in html and "מגמה" in html and "סיכוי/סיכון" in html
    assert lt.detail.btn.isEnabled()


def test_risk_center_flow_approves_and_opens_paper_position(win, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    risk = win.screens[6]
    last = float(win.core.store.load_bars("MSFT").close.iloc[-1])
    risk.sym.setText("MSFT")
    risk.entry.setValue(last)
    risk.stop.setValue(last * 0.92)
    risk.override.setValue(5)                                # formula allows 6 at this stop; 5 is a deliberate down-size
    risk.evaluate()
    assert "מאושר" in risk.result.toPlainText() and risk.b_fill.isEnabled()
    risk.open_paper()
    assert win.core.pstore.get("MSFT").qty == 5
    risk.override.setValue(0)
    risk.evaluate()                                         # duplicate position now rejected
    assert "נדחה" in risk.result.toPlainText() and not risk.b_fill.isEnabled()


def test_portfolio_screen_shows_breach_and_trailing(win, qapp):
    last = float(win.core.store.load_bars("AAPL").close.iloc[-1])
    win.core.broker.add_manual_position(Position("AAPL", 10, last * 0.9, stop=last * 1.2, sector="XLK", strategy="longterm"))
    port = win.screens[5]
    port.recompute()
    rows = {r["symbol"]: r for r in port.pos[1].rows}
    assert rows["AAPL"]["status"] == "critical"
    assert win.core.alerts.history()[0]["kind"] in ("stop_breach", "new_setup", "trend_break")
    assert "קריטי" in port.warn.toPlainText()


def test_settings_save_roundtrip_and_validation(win, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    st = win.screens[8]
    st.r["max_positions"].setValue(7)
    st.save()
    from aiscanner.config import load_settings
    assert load_settings().risk.max_positions == 7 and win.core.settings.risk.max_positions == 7
    st.q_start.setText("99:99")
    st.save()                                               # invalid -> rejected, previous file intact
    assert load_settings().alerts.quiet_start == "23:00"


def test_diagnostics_table_populated(win, qapp):
    st = win.screens[8]
    st.run_diag(False)
    names = [r["name"] for r in st.diag[1].rows]
    assert "execution_mode" in names and "provider" in names and "telegram" in names
    assert any(r["status"] == "warn" and r["name"] == "provider" for r in st.diag[1].rows)     # synthetic flagged


def test_background_scan_keeps_ui_responsive_and_persists(win, qapp):
    before = win.core.store.table_counts()["scan_runs"]
    ticks = {"n": 0}
    t = QTimer()
    t.timeout.connect(lambda: ticks.__setitem__("n", ticks["n"] + 1))
    t.start(20)
    win.run_scan()
    assert win.busy and not win.b_scan.isEnabled() and win.b_cancel.isEnabled()
    assert wait_for(qapp, lambda: not win.busy)
    t.stop()
    assert win.core.store.table_counts()["scan_runs"] == before + 1
    assert ticks["n"] > 3                                   # event loop kept running while the worker computed
    assert win.b_scan.isEnabled() and not win.b_cancel.isEnabled()


def test_background_update_then_cancel(win, qapp):
    win.core.store.delete_bars("AAPL")
    win.run_update()
    win.cancel()
    assert wait_for(qapp, lambda: not win.busy)
    assert win.b_update.isEnabled()


def test_alerts_screen_filters(win, qapp):
    from aiscanner.models import Alert, CRITICAL, WARNING
    win.core.alerts.process([Alert("a1", CRITICAL, "stop_breach", "t", "b", "AAA"), Alert("a2", WARNING, "near_stop", "t2", "b2", "BBB")])
    al = win.screens[7]
    al.refresh()
    n_all = al.proxy.rowCount()
    al.sev.setCurrentIndex(1)
    assert 0 < al.proxy.rowCount() < n_all or n_all == 1
    for i in range(al.proxy.rowCount()):
        assert al.m.rows[al.proxy.mapToSource(al.proxy.index(i, 0)).row()]["severity"] == "critical"
