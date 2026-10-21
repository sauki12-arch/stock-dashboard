"""Settings and Diagnostics."""
from __future__ import annotations

import dataclasses as dc

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from .. import secrets
from ..config import ConfigError, load_settings, save_settings
from ..diagnostics import run_diagnostics
from ..scheduler import next_runs
from . import theme
from .widgets import Col, make_table, muted, title

WEEKDAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]


def dspin(lo, hi, val, dec=2, step=None):
    w = QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(dec)
    w.setValue(val)
    if step:
        w.setSingleStep(step)
    return w


def ispin(lo, hi, val):
    w = QSpinBox()
    w.setRange(lo, hi)
    w.setValue(val)
    return w


class SettingsScreen(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win, self.core = win, win.core
        lay = QVBoxLayout(self)
        lay.addWidget(title("הגדרות ואבחון"))
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self._general()
        self._risk()
        self._scan()
        self._alerts()
        self._schedule()
        self._diag()
        bar = QHBoxLayout()
        save = QPushButton("💾 שמור הגדרות")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        bar.addWidget(save)
        bar.addStretch(1)
        lay.addLayout(bar)

    # ---------------------------------------------------------------- tabs
    def _general(self):
        w = QWidget(); f = QFormLayout(w)
        self.provider = QComboBox()
        self.provider.addItem("Mock – נתונים סינתטיים (הדגמה בלבד, לא נתוני שוק!)", "mock")
        self.provider.addItem("Yahoo Finance (yfinance) – לא רשמי, מושהה, לשימוש אישי", "yahoo")
        self.history = ispin(300, 3000, 800)
        self.universe = QLineEdit()
        br = QPushButton("בחר קובץ…"); br.clicked.connect(self._browse_universe)
        row = QHBoxLayout(); row.addWidget(self.universe, 1); row.addWidget(br)
        self.workers = ispin(1, 8, 3)
        self.rps = dspin(0.2, 20, 2.0, 1)
        self.batch = ispin(1, 200, 50)
        f.addRow("ספק נתונים", self.provider)
        f.addRow("היסטוריית ימים (קלנדרי)", self.history)
        f.addRow("קובץ יקום מניות (CSV; ריק = ברירת מחדל 500)", row)
        f.addRow("מקביליות מרבית", self.workers)
        f.addRow("בקשות לשנייה (הגבלת קצב)", self.rps)
        f.addRow("גודל אצווה", self.batch)
        f.addRow(muted("Yahoo Finance אינו מקור רשמי ואינו מבטיח זמינות, שלמות או נתונים בזמן אמת. אין להפיץ מחדש את הנתונים. "
                       "לשימוש ייצורי מומלץ ספק בתשלום (ממשק DataProvider ניתן להחלפה)."))
        self.tabs.addTab(w, "כללי ונתונים")

    def _risk(self):
        r = self.core.settings.risk
        w = QWidget(); f = QFormLayout(w)
        self.r = {
            "risk_per_trade": dspin(0.1, 5, r.risk_per_trade * 100, 2, 0.1),
            "max_positions": ispin(1, 100, r.max_positions),
            "max_position_pct": dspin(1, 100, r.max_position_pct * 100, 1),
            "max_sector_pct": dspin(1, 100, r.max_sector_pct * 100, 1),
            "max_total_risk_pct": dspin(0.5, 50, r.max_total_risk_pct * 100, 1),
            "drawdown_alert_pct": dspin(1, 80, r.drawdown_alert_pct * 100, 1),
            "slippage_bps": dspin(0, 200, r.slippage_bps, 1),
            "fee_per_trade": dspin(0, 100, r.fee_per_trade, 2),
            "gap_through_pct": dspin(0, 20, r.gap_through_pct * 100, 1),
            "gap_stress_multiple": dspin(1, 10, r.gap_stress_multiple, 1),
            "starting_cash": dspin(0, 1e9, r.starting_cash, 0),
        }
        labels = {"risk_per_trade": "סיכון לעסקה (% מההון)", "max_positions": "מקסימום פוזיציות", "max_position_pct": "מקסימום משקל פוזיציה (%)",
                  "max_sector_pct": "מקסימום סקטור (%)", "max_total_risk_pct": "מקסימום סיכון פתוח מצטבר (%)", "drawdown_alert_pct": "סף התראת ירידה מהשיא (%)",
                  "slippage_bps": "החלקה (נקודות בסיס)", "fee_per_trade": "עמלה לעסקה ($)", "gap_through_pct": "תרחיש גאפ דרך הסטופ (%)",
                  "gap_stress_multiple": "מכפיל מרבי של הפסד קיצון מתקציב הסיכון", "starting_cash": "הון התחלתי לחשבון נייר (לאיפוס)"}
        for k, wd in self.r.items():
            f.addRow(labels[k], wd)
        f.addRow(muted("כללי הסיכון מיושמים בקוד דטרמיניסטי; אף מודל AI אינו יכול לעקוף אותם. ערכי ברירת המחדל לפי המפרט: 1% סיכון, 10 פוזיציות."))
        self.tabs.addTab(w, "סיכון")

    def _scan(self):
        s = self.core.settings.scan
        w = QWidget(); f = QFormLayout(w)
        self.s = {"min_price": dspin(0.5, 1000, s.min_price), "min_dollar_volume": dspin(0, 1e11, s.min_dollar_volume, 0),
                  "min_bars": ispin(60, 1000, s.min_bars), "max_spread_proxy": dspin(0.05, 10, s.max_spread_proxy * 100, 2),
                  "swing_min_rr": dspin(0.5, 10, s.swing_min_rr, 2), "longterm_min_rr": dspin(0.5, 10, s.longterm_min_rr, 2)}
        for k, lbl in (("min_price", "מחיר מינימלי ($)"), ("min_dollar_volume", "מחזור יומי ממוצע מינימלי ($)"), ("min_bars", "מינימום ימי היסטוריה"),
                       ("max_spread_proxy", "מקסימום אומדן ספרד (%) – Corwin-Schultz"), ("swing_min_rr", "R/R מינימלי – סווינג"), ("longterm_min_rr", "R/R מינימלי – טווח ארוך")):
            f.addRow(lbl, self.s[k])
        self.exp = QCheckBox("זהה תבניות ניסיוניות (משולש/נס/כוס וידית) – לא משפיעות על הציון")
        self.exp.setChecked(s.enable_experimental_patterns)
        f.addRow(self.exp)
        wt = "\n".join(f"{k}: {v}" for k, v in s.swing_weights.items())
        box = QTextEdit(); box.setReadOnly(True); box.setMaximumHeight(130)
        box.setPlainText(f"משקלי ציון (סווינג = טווח ארוך): {wt}\n\nשינוי משקלים אפשרי רק דרך גרסאות מנוהלות: הצעה → בקטסט → אישור ידני (ראו README). "
                         "ספי הציון 85/70/55 זמניים ולא מכוילים.")
        f.addRow(box)
        self.tabs.addTab(w, "סורק וציונים")

    def _alerts(self):
        a = self.core.settings.alerts
        w = QWidget(); f = QFormLayout(w)
        creds = secrets.load_telegram()
        self.tg_token = QLineEdit(); self.tg_token.setEchoMode(QLineEdit.Password)
        self.tg_token.setPlaceholderText("הוזן מחדש כדי לעדכן" if creds.configured else "BotFather token")
        self.tg_chat = QLineEdit(creds.chat_id)
        self.a_enabled = QCheckBox("שליחת התראות קריטיות ל-Telegram"); self.a_enabled.setChecked(a.enabled)
        self.q_start, self.q_end = QLineEdit(a.quiet_start), QLineEdit(a.quiet_end)
        self.q_tz = QLineEdit(a.quiet_tz)
        self.a_rate, self.a_dedupe = ispin(1, 60, a.max_per_hour), ispin(1, 168, a.dedupe_hours)
        self.a_score = ispin(60, 100, a.new_setup_min_score)
        for lbl, wd in (("Token של הבוט", self.tg_token), ("Chat ID", self.tg_chat), ("", self.a_enabled), ("שעות שקט – התחלה (HH:MM)", self.q_start),
                        ("שעות שקט – סיום (HH:MM)", self.q_end), ("אזור זמן לשעות שקט", self.q_tz), ("מקסימום הודעות בשעה", self.a_rate),
                        ("חלון מניעת כפילויות (שעות)", self.a_dedupe), ("ציון מינימלי להתראת מועמד חדש", self.a_score)):
            f.addRow(lbl, wd)
        b = QHBoxLayout()
        sv = QPushButton("שמור פרטי Telegram"); sv.clicked.connect(self.save_tg)
        ts = QPushButton("שלח הודעת בדיקה"); ts.setObjectName("primary"); ts.clicked.connect(self.test_tg)
        cl = QPushButton("מחק פרטי Telegram"); cl.setObjectName("danger"); cl.clicked.connect(self.clear_tg)
        for x in (sv, ts, cl):
            b.addWidget(x)
        f.addRow(b)
        self.tg_state = muted("")
        f.addRow(self.tg_state)
        f.addRow(muted("ה-Token נשמר מקומית בלבד (מוצפן ב-DPAPI של Windows), אינו נכתב ל-config.yaml או לקוד המקור. נשלחות רק התראות קריטיות."))
        self.tabs.addTab(w, "התראות ו-Telegram")
        self._tg_status()

    def _schedule(self):
        c = self.core.settings.schedule
        w = QWidget(); f = QFormLayout(w)
        self.sch_on = QCheckBox("הפעל סריקות מתוזמנות (האפליקציה חייבת להיות פתוחה)"); self.sch_on.setChecked(c.enabled)
        self.pre = ispin(5, 600, c.pre_market_minutes_before_open)
        self.post = ispin(0, 600, c.post_close_minutes_after_close)
        self.intra_on = QCheckBox("רענון תוך-יומי לניטור תיק"); self.intra_on.setChecked(c.intraday_enabled)
        self.intra = ispin(5, 240, c.intraday_minutes)
        self.wday = QComboBox()
        for i, n in enumerate(WEEKDAYS):
            self.wday.addItem(n, i)
        self.wday.setCurrentIndex(c.weekly_review_weekday)
        self.wtime = QLineEdit(c.weekly_review_time)
        for lbl, wd in (("", self.sch_on), ("סריקת טרום-פתיחה: דקות לפני פתיחת NYSE", self.pre), ("סריקת אחרי סגירה: דקות אחרי הסגירה", self.post),
                        ("", self.intra_on), ("מרווח רענון תוך-יומי (דקות)", self.intra), ("סקירה שבועית – יום", self.wday), ("סקירה שבועית – שעה (שעון ניו-יורק)", self.wtime)):
            f.addRow(lbl, wd)
        self.next_lbl = muted("")
        f.addRow(self.next_lbl)
        f.addRow(muted("כל השעות לפי America/New_York, כולל מעבר שעון קיץ וימי חג/קצרים של NYSE. אין שירות רקע: משימות שהוחמצו מבוצעות בהשלמה כשהאפליקציה נפתחת."))
        self.tabs.addTab(w, "תזמון")

    def _diag(self):
        w = QWidget(); v = QVBoxLayout(w)
        b = QHBoxLayout()
        r1 = QPushButton("הרץ אבחון"); r1.setObjectName("primary"); r1.clicked.connect(lambda: self.run_diag(False))
        r2 = QPushButton("בדוק גם חיבור לספק הנתונים"); r2.clicked.connect(lambda: self.run_diag(True))
        r3 = QPushButton("הצג שגיאות משיכה אחרונות"); r3.clicked.connect(self.show_errors)
        for x in (r1, r2, r3):
            b.addWidget(x)
        b.addStretch(1)
        v.addLayout(b)
        icon = {"ok": "● תקין", "warn": "■ אזהרה", "fail": "▲ כשל"}
        self.diag = make_table([Col("status", "מצב", fmt=lambda s: icon.get(s, s)), Col("name", "בדיקה", align="l"), Col("detail", "פרטים", align="l")],
                               row_color=lambda r: theme.BAD if r["status"] == "fail" else theme.WARN if r["status"] == "warn" else None)
        v.addWidget(self.diag[0], 1)
        self.errbox = QTextEdit(); self.errbox.setReadOnly(True); self.errbox.setMaximumHeight(150)
        v.addWidget(self.errbox)
        self.tabs.addTab(w, "אבחון")

    # ---------------------------------------------------------------- actions
    def refresh(self):
        s = self.core.settings
        self.provider.setCurrentIndex(self.provider.findData(s.data.provider))
        self.history.setValue(s.data.history_days)
        self.universe.setText(s.universe_path)
        self.workers.setValue(s.data.max_workers)
        self.rps.setValue(s.data.requests_per_second)
        self.batch.setValue(s.data.batch_size)
        self._next()
        self.run_diag(False)

    def _next(self):
        nr = next_runs(self.core.clock(), self.core.settings.schedule)
        names = {"pre_market": "טרום-פתיחה", "post_close": "אחרי סגירה", "weekly_review": "סקירה שבועית", "intraday": "תוך-יומי"}
        self.next_lbl.setText("הריצות הבאות (שעון ניו-יורק): " + " | ".join(f"{names[k]}: {v:%a %d/%m %H:%M}" for k, v in nr.items() if v))

    def _browse_universe(self):
        p, _ = QFileDialog.getOpenFileName(self, "בחר CSV של היקום", "", "CSV (*.csv)")
        if p:
            self.universe.setText(p)

    def save(self):
        s = load_settings()
        s.data.provider = self.provider.currentData()
        s.data.history_days, s.data.max_workers = self.history.value(), self.workers.value()
        s.data.requests_per_second, s.data.batch_size = self.rps.value(), self.batch.value()
        s.universe_path = self.universe.text().strip()
        r = self.r
        s.risk.risk_per_trade = r["risk_per_trade"].value() / 100
        s.risk.max_positions = r["max_positions"].value()
        s.risk.max_position_pct = r["max_position_pct"].value() / 100
        s.risk.max_sector_pct = r["max_sector_pct"].value() / 100
        s.risk.max_total_risk_pct = r["max_total_risk_pct"].value() / 100
        s.risk.drawdown_alert_pct = r["drawdown_alert_pct"].value() / 100
        s.risk.slippage_bps, s.risk.fee_per_trade = r["slippage_bps"].value(), r["fee_per_trade"].value()
        s.risk.gap_through_pct = r["gap_through_pct"].value() / 100
        s.risk.gap_stress_multiple, s.risk.starting_cash = r["gap_stress_multiple"].value(), r["starting_cash"].value()
        s.scan.min_price, s.scan.min_dollar_volume, s.scan.min_bars = self.s["min_price"].value(), self.s["min_dollar_volume"].value(), self.s["min_bars"].value()
        s.scan.max_spread_proxy = self.s["max_spread_proxy"].value() / 100
        s.scan.swing_min_rr, s.scan.longterm_min_rr = self.s["swing_min_rr"].value(), self.s["longterm_min_rr"].value()
        s.scan.enable_experimental_patterns = self.exp.isChecked()
        a = s.alerts
        a.enabled, a.quiet_start, a.quiet_end, a.quiet_tz = self.a_enabled.isChecked(), self.q_start.text().strip(), self.q_end.text().strip(), self.q_tz.text().strip()
        a.max_per_hour, a.dedupe_hours, a.new_setup_min_score = self.a_rate.value(), self.a_dedupe.value(), self.a_score.value()
        c = s.schedule
        c.enabled, c.pre_market_minutes_before_open, c.post_close_minutes_after_close = self.sch_on.isChecked(), self.pre.value(), self.post.value()
        c.intraday_enabled, c.intraday_minutes = self.intra_on.isChecked(), self.intra.value()
        c.weekly_review_weekday, c.weekly_review_time = self.wday.currentData(), self.wtime.text().strip()
        try:
            save_settings(s)
        except (ConfigError, ValueError) as e:
            QMessageBox.warning(self, "הגדרות לא תקינות", str(e))
            return
        old_provider = self.core.settings.data.provider
        self.core.reload_settings(s)
        if s.data.provider != old_provider:
            self.core.set_provider(s.data.provider)
            self.core.alerts.send = self.core._sender()
        if s.data.provider == "yahoo":
            QMessageBox.information(self, "Yahoo Finance", "נבחר Yahoo Finance. הנתונים אינם רשמיים, עשויים להיות מושהים או חלקיים, ומיועדים לשימוש אישי בלבד. "
                                    "הרצת 500 מניות כרוכה בהגבלות קצב – העדכון הראשון עשוי לקחת כמה דקות.")
        self.win.notify("ההגדרות נשמרו")
        self.win.on_settings_changed()
        self._next()

    def _tg_status(self):
        c = secrets.load_telegram()
        self.tg_state.setText(("מוגדר" + (" – מוצפן (Windows DPAPI)" if c.protected else " – לא מוצפן (סביבת פיתוח/לא-Windows)")) if c.configured else "לא מוגדר")

    def save_tg(self):
        tok, chat = self.tg_token.text().strip(), self.tg_chat.text().strip()
        if not tok:
            cur = secrets.load_telegram()
            tok = cur.token
        if not tok or not chat:
            QMessageBox.warning(self, "Telegram", "יש להזין Token ו-Chat ID.")
            return
        secrets.save_telegram(tok, chat)
        self.tg_token.clear()
        self.core.refresh_telegram()
        self._tg_status()
        self.win.notify("פרטי Telegram נשמרו")

    def clear_tg(self):
        secrets.clear_telegram()
        self.core.refresh_telegram()
        self._tg_status()

    def test_tg(self):
        self.save_tg() if self.tg_token.text().strip() else None
        r = self.core.telegram_test()
        QMessageBox.information(self, "Telegram", "הודעת הבדיקה נשלחה בהצלחה." if r.ok else f"השליחה נכשלה: {r.error}")

    def run_diag(self, check_provider: bool):
        self.diag[1].set_rows([dc.asdict(d) for d in run_diagnostics(self.core, check_provider)])
        self.diag[0].resizeColumnsToContents()

    def show_errors(self):
        errs = self.core.store.recent_fetch_errors(200)
        self.errbox.setPlainText("\n".join(f"{e['ts'][:19]} {e['symbol']} [{e['interval']}] {e['message']}" for e in errs) or "אין שגיאות משיכה רשומות.")
