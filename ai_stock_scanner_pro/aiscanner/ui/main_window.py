"""Main window: RTL navigation, global actions, background workers, scheduler tick."""
from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QListWidget, QMainWindow, QMessageBox, QProgressBar,
                               QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from .. import __version__
from ..core import AppCore
from ..logging_setup import setup_logging
from ..models import LONGTERM, SWING
from . import theme
from .candidates import CandidateBrowser
from .screens import AlertsScreen, OverviewScreen, PortfolioScreen, RegimeScreen, RiskScreen
from .settings_screen import SettingsScreen
from .widgets import Banner
from .workers import TaskWorker

log = logging.getLogger(__name__)
NAV = ["סקירה כללית", "משטר שוק", "סורק גילוי", "הזדמנויות סווינג", "הזדמנויות טווח ארוך", "מעקב תיק", "מרכז סיכונים", "היסטוריית התראות", "הגדרות ואבחון"]


class MainWindow(QMainWindow):
    def __init__(self, core: AppCore):
        super().__init__()
        self.core = core
        self.worker: TaskWorker | None = None
        self.setWindowTitle(f"AI Stock Scanner Pro – Quant Edition {__version__}")
        self.setLayoutDirection(Qt.RightToLeft)
        self.resize(1480, 920)
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        for n in NAV:
            self.nav.addItem(n)
        self.stack = QStackedWidget()
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(10, 8, 10, 4)
        bl.addLayout(self._toolbar())
        self.mode_banner = Banner()
        bl.addWidget(self.mode_banner)
        bl.addWidget(self.stack, 1)
        outer.addWidget(self.nav)
        outer.addWidget(body, 1)

        self.screens = [OverviewScreen(self), RegimeScreen(self), CandidateBrowser(core, None), CandidateBrowser(core, SWING),
                        CandidateBrowser(core, LONGTERM), PortfolioScreen(self), RiskScreen(self), AlertsScreen(self), SettingsScreen(self)]
        for s in self.screens:
            self.stack.addWidget(s)
        for b in self.screens[2:5]:
            b.open_sizing.connect(self.go_sizing)
        self.dirty = set(range(len(self.screens)))
        self.nav.currentRowChanged.connect(self._navigate)
        self.nav.setCurrentRow(0)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(60_000)
        self.on_settings_changed()
        QTimer.singleShot(500, self._startup)

    def _startup(self) -> None:
        r = self.core.sync_portfolio(force=True)
        if r.status != "disabled":
            self.notify(f"סנכרון BARCHART: {r.status} {r.message}")
            self.dirty.add(5)
            self._navigate(self.nav.currentRow())
        if not self.core.store.bar_counts():
            self.status.setText("אין נתוני שוק – לחץ 'עדכן + סרוק' כדי להוריד נתוני אמת")

    # ---------------------------------------------------------------- chrome
    def _toolbar(self) -> QHBoxLayout:
        h = QHBoxLayout()
        self.b_update = QPushButton("⟳ עדכן נתונים")
        self.b_scan = QPushButton("🔍 סרוק עכשיו")
        self.b_scan.setObjectName("primary")
        self.b_both = QPushButton("עדכן + סרוק")
        self.b_cancel = QPushButton("בטל")
        self.b_cancel.setEnabled(False)
        self.b_update.clicked.connect(lambda: self.run_update())
        self.b_scan.clicked.connect(lambda: self.run_scan())
        self.b_both.clicked.connect(lambda: self.run_update(then_scan=True))
        self.b_cancel.clicked.connect(self.cancel)
        self.prog = QProgressBar()
        self.prog.setMaximumWidth(320)
        self.prog.setVisible(False)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        for w in (self.b_update, self.b_scan, self.b_both, self.b_cancel, self.prog):
            h.addWidget(w)
        h.addWidget(self.status, 1)
        return h

    def notify(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 12_000)

    def on_settings_changed(self) -> None:
        if self.core.provider.is_synthetic:
            self.mode_banner.show_msg("⚠ מצב הדגמה – נתונים סינתטיים (מדומים). אין להשתמש בהם להחלטות. בחר Yahoo Finance בהגדרות לעבודה עם נתוני שוק.")
        else:
            self.mode_banner.show_msg("")
        self.statusBar().showMessage(f"ספק: {self.core.provider.name} | מצב: ניתוח ומסחר נייר בלבד – אין חיבור לברוקר")
        self.dirty = set(range(len(self.screens)))
        self._navigate(self.nav.currentRow())

    def _navigate(self, i: int) -> None:
        if i < 0:
            return
        self.stack.setCurrentIndex(i)
        if i in self.dirty:
            self.dirty.discard(i)
            try:
                self.screens[i].refresh()
            except Exception:  # noqa: BLE001 - a rendering bug in one screen must not take the app down
                log.exception("screen refresh failed")
                self.notify("שגיאה ברענון המסך – ראה יומן")

    def refresh_all(self) -> None:
        self.dirty = set(range(len(self.screens)))
        self._navigate(self.nav.currentRow())

    def go_sizing(self, symbol: str, strategy: str) -> None:
        self.nav.setCurrentRow(6)
        self.screens[6].prefill(symbol, strategy)

    # ---------------------------------------------------------------- tasks
    @property
    def busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _start(self, label: str, fn, on_done) -> None:
        if self.busy:
            self.notify("פעולה אחרת רצה כעת")
            return
        self.worker = TaskWorker(fn, label, self)
        self.worker.progress.connect(self._progress)
        self.worker.succeeded.connect(lambda r: self._done(label, r, on_done))
        self.worker.failed.connect(lambda m: self._failed(label, m))
        for b in (self.b_update, self.b_scan, self.b_both):
            b.setEnabled(False)
        self.b_cancel.setEnabled(True)
        self.prog.setVisible(True)
        self.prog.setRange(0, 0)
        self.status.setText(label + "…")
        self.worker.start()

    def _progress(self, d: int, t: int, m: str) -> None:
        if t:
            self.prog.setRange(0, t)
            self.prog.setValue(d)
        self.status.setText(f"{self.worker.label}: {d}/{t} {m}")

    def _finish_ui(self) -> None:
        for b in (self.b_update, self.b_scan, self.b_both):
            b.setEnabled(True)
        self.b_cancel.setEnabled(False)
        self.prog.setVisible(False)

    def _done(self, label: str, res, on_done) -> None:
        self._finish_ui()
        self.status.setText(f"{label} – הסתיים")
        on_done(res)

    def _failed(self, label: str, msg: str) -> None:
        self._finish_ui()
        self.status.setText(f"{label} – נכשל")
        log.error("%s failed: %s", label, msg)
        QMessageBox.critical(self, label, msg)

    def cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.status.setText("מבטל…")

    def run_update(self, then_scan: bool = False) -> None:
        def done(rep):
            msg = rep.summary()
            self.notify(msg)
            if rep.provider_down:
                sample = next(iter(rep.messages.values()), "")
                QMessageBox.warning(self, "ספק נתונים", "ספק הנתונים אינו זמין כרגע (נכשלו כל הבקשות). נשארים עם הנתונים השמורים, ייתכן שהם ישנים.\n"
                                    f"סיבה: {sample}\nבדוק חיבור אינטרנט/חומת אש, או נסה שוב בעוד כמה דקות (Yahoo מגביל קצב).")
            elif rep.n_failed:
                bad = [(s, rep.messages.get(s, rep.status[s])) for s in rep.status if rep.status[s] in ("failed", "empty")]
                self.notify(msg + " – ראה 'הגדרות ואבחון' לפירוט שגיאות")
                if len(bad) >= max(5, len(rep.status) // 20):
                    QMessageBox.warning(self, "עדכון חלקי", f"{len(bad)} מתוך {len(rep.status)} סימולים לא נטענו (ייתכן שהוחלפו/נמחקו, או הגבלת קצב).\n"
                                        + "\n".join(f"{s}: {m}" for s, m in bad[:6]) + "\n\nהסימולים האלה מוחרגים מהדירוג. הרצה חוזרת של 'עדכן נתונים' מנסה אותם שוב.")
            self.refresh_all()
            if then_scan and not rep.cancelled:
                self.run_scan()
        self._start("עדכון נתונים", lambda p, c: self.core.update_data(p, c), done)

    def run_scan(self) -> None:
        if not self.core.store.bar_counts():          # nothing downloaded yet: fetch first, then scan
            self.notify("אין נתוני שוק – מורידים נתונים ואז סורקים")
            return self.run_update(then_scan=True)

        def done(res):
            self.notify(f"הסריקה הסתיימה: {res.n_eligible}/{res.n_universe} זכאיות, משטר {res.regime['regime']} ({res.seconds:.0f} שניות)")
            from ..alerts.manager import detect_new_setups
            prev = self.core.latest_scan(offset=1)
            self.core.alerts.process(detect_new_setups(prev, res, self.core.settings.alerts))
            self.refresh_all()
        self._start("סריקה", lambda p, c: self.core.scan(p, c), done)

    def run_job(self, job: str) -> None:
        def done(jr):
            self.notify(f"משימה {job}: " + " | ".join(jr.messages[:2]))
            self.refresh_all()
        self._start(f"משימה מתוזמנת: {job}", lambda p, c: self.core.run_job(job, p, c), done)

    def _tick(self) -> None:
        r = self.core.sync_portfolio(min_interval=True)
        if r.status == "ok" and r.changed:
            self.notify(f"BARCHART סונכרן: +{len(r.added)} ~{len(r.updated)} -{len(r.removed)}")
            self.dirty.update({0, 5, 6})
            self._navigate(self.nav.currentRow())
        if self.busy or not self.core.settings.schedule.enabled:
            return
        due = self.core.due_jobs()
        if due:
            self.run_job(due[0])

    def closeEvent(self, e) -> None:
        if self.busy:
            self.worker.cancel()
            self.worker.wait(5000)
        super().closeEvent(e)


def run_gui(provider: str | None = None) -> int:
    setup_logging()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setLayoutDirection(Qt.RightToLeft)
    app.setStyleSheet(theme.QSS)
    app.setFont(QFont(theme.FONT_FAMILY, 10))
    core = AppCore()
    if provider:
        core.set_provider(provider)
    w = MainWindow(core)
    w.show()
    return app.exec()
