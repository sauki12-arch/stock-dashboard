"""Overview, Market Regime, Portfolio, Risk Center, Alerts History."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox,
                               QPushButton, QSpinBox, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from .. import i18n
from ..data.universe import SECTOR_ETFS
from ..discovery.indicators import build_frame
from ..models import CRITICAL, WARNING, Reason
from ..portfolio.broker import OrderRejected
from ..portfolio.importer import load_positions_csv
from ..portfolio.store import Position
from . import theme
from .charts import HBars, PriceChart
from .widgets import (Banner, Card, Col, fmt_money, fmt_num, fmt_pct, make_table, muted, selected_row, title)

STATUS_MARK = {"ok": "● תקין", "warning": "■ אזהרה", "critical": "▲ קריטי", "no_data": "? אין נתונים"}
STATUS_COLOR = {"ok": None, "warning": theme.WARN, "critical": theme.BAD, "no_data": theme.MUTED}


def reason_lines(reasons: list[Reason]) -> str:
    return "\n".join(i18n.mark(r) + i18n.render(r) for r in reasons)


def local_ts(iso: str, tz: str = "Asia/Jerusalem") -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).strftime("%d/%m/%Y %H:%M")
    except Exception:  # noqa: BLE001
        return iso or ""


# ====================================================================== Overview
class OverviewScreen(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win, self.core = win, win.core
        lay = QVBoxLayout(self)
        lay.addWidget(title("סקירה כללית"))
        self.banner = Banner()
        lay.addWidget(self.banner)
        row = QHBoxLayout()
        self.c_regime, self.c_scan, self.c_port, self.c_alerts, self.c_data = (Card(t) for t in (
            "משטר שוק", "סריקה אחרונה", "תיק (נייר)", "התראות קריטיות (24 שעות)", "איכות נתונים"))
        for c in (self.c_regime, self.c_scan, self.c_port, self.c_alerts, self.c_data):
            row.addWidget(c)
        lay.addLayout(row)
        cols = lambda st: [Col("rank", "#"), Col("symbol", "סימול", align="l"), Col("score", "ציון", fmt=lambda v: f"{v:.1f}"),  # noqa: E731
                           Col("klass", "מחלקה"), Col("rr", "R/R", fmt=fmt_num), Col("setups", "תבניות", align="l")]
        self.t_sw = make_table(cols(0))
        self.t_lt = make_table(cols(0))
        g = QHBoxLayout()
        for name, t in (("5 המובילות – סווינג", self.t_sw), ("5 המובילות – טווח ארוך", self.t_lt)):
            box = QGroupBox(name)
            v = QVBoxLayout(box)
            v.addWidget(t[0])
            g.addWidget(box)
        lay.addLayout(g, 1)
        self.warn = QTextEdit()
        self.warn.setReadOnly(True)
        self.warn.setMaximumHeight(110)
        self.warn.setLayoutDirection(Qt.RightToLeft)
        lay.addWidget(QLabel("אזהרות נתונים ותפעול:"))
        lay.addWidget(self.warn)
        self.stamp = muted("")
        lay.addWidget(self.stamp)

    def refresh(self):
        core = self.core
        sc = core.latest_scan()
        warn = []
        self.banner.show_msg("")             # the main window carries the permanent synthetic-data banner
        if sc is None:
            self.c_regime.set("—", "טרם בוצעה סריקה")
            self.c_scan.set("—", "")
        else:
            rg = sc.regime
            col = {"RISK_ON": theme.GOOD, "NEUTRAL": theme.WARN, "RISK_OFF": theme.BAD}[rg["regime"]]
            self.c_regime.set(i18n.REGIME[rg["regime"]], f"ציון {rg['score']:.0f}/100 | ביטחון {rg['confidence']:.0%} (לא מכויל)", col)
            self.c_scan.set(f"{sc.n_eligible}/{sc.n_universe}", f"זכאיות | נכון ל-{sc.as_of}\n{local_ts(sc.ts)} | {sc.seconds:.0f} שניות")
            for st, tbl in ((("swing"), self.t_sw), ("longterm", self.t_lt)):
                tbl[1].set_rows([{"rank": i + 1, "symbol": c.symbol, "score": c.score(st), "klass": i18n.KLASS_MARK[c.scores[st]["klass"]] + i18n.KLASS[c.scores[st]["klass"]],
                                  "rr": (c.scores[st].get("levels") or {}).get("rr"),
                                  "setups": ", ".join(i18n.PATTERNS.get(s, s) for s in c.scores[st]["setups"])} for i, c in enumerate(sc.ranked(st, 5))])
            if sc.failed:
                warn.append(f"✖ {len(sc.failed)} מניות נכשלו בטעינה: " + ", ".join(list(sc.failed)[:12]) + (" …" if len(sc.failed) > 12 else ""))
            if sc.stale_symbols:
                warn.append(f"⚠ {len(sc.stale_symbols)} מניות עם נתונים ישנים (מוחרגות מהדירוג).")
            if sc.synthetic:
                warn.append("⚠ הסריקה האחרונה בוצעה על נתונים סינתטיים.")
            if sc.regime.get("missing"):
                warn.append("⚠ רכיבי משטר חסרים: " + ", ".join(sc.regime["missing"]))
        st = core.portfolio_state()
        m = core.risk.metrics(st, core.pstore.peak_equity())
        pl = st.equity - core.pstore.starting_equity
        self.c_port.set(f"{st.equity:,.0f}$", f"{m['n_positions']}/{core.settings.risk.max_positions} פוזיציות | P/L כולל {pl:+,.0f}$ | DD {m['drawdown']:.1%}",
                        theme.BAD if m["drawdown"] <= -core.settings.risk.drawdown_alert_pct else None)
        hist = core.alerts.history(500)
        now = core.clock()
        n = sum(1 for a in hist if a["severity"] == CRITICAL and (now - datetime.fromisoformat(a["ts"])).total_seconds() < 86400)
        self.c_alerts.set(str(n), "בהיסטוריה: " + str(len(hist)), theme.BAD if n else None)
        spy_last = core.store.last_bar_date("SPY")
        errs = core.store.recent_fetch_errors(1000)
        if spy_last:
            from ..data import market_calendar as cal
            import pandas as pd
            behind = cal.sessions_behind(pd.Timestamp(spy_last).date(), now, core.settings.data.grace_minutes_after_close)
            self.c_data.set("עדכני" if behind == 0 else f"{behind} ימים מאחור", f"נר אחרון SPY: {spy_last} | שגיאות משיכה: {len(errs)}", None if behind == 0 else theme.WARN)
            if behind:
                warn.append(f"⚠ הנתונים ישנים ב-{behind} ימי מסחר – הרץ עדכון נתונים.")
        else:
            self.c_data.set("אין נתונים", "הרץ 'עדכן נתונים'", theme.WARN)
        if not core.alerts.send:
            warn.append("ℹ Telegram לא מוגדר – התראות נשמרות בהיסטוריה בלבד.")
        self.warn.setPlainText("\n".join(warn) or "אין אזהרות.")
        self.stamp.setText(f"עודכן: {local_ts(now.isoformat())} (שעון ישראל) | ספק נתונים: {core.provider.name} | מצב: ניתוח ומסחר נייר בלבד")


# ====================================================================== Market regime
class RegimeScreen(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win, self.core = win, win.core
        lay = QVBoxLayout(self)
        lay.addWidget(title("משטר שוק"))
        self.banner = Banner()
        lay.addWidget(self.banner)
        top = QHBoxLayout()
        self.c_reg, self.c_conf, self.c_post = Card("משטר"), Card("ביטחון / כיול"), Card("עמדת חשיפה (המלצה בלבד)")
        for c in (self.c_reg, self.c_conf, self.c_post):
            top.addWidget(c)
        lay.addLayout(top)
        sp = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.lists = {}
        for key, name in (("evidence", "ראיות תומכות"), ("counter", "טיעוני נגד"), ("invalid", "תנאי ביטול")):
            box = QGroupBox(name)
            v = QVBoxLayout(box)
            lw = QListWidget()
            lw.setLayoutDirection(Qt.RightToLeft)
            lw.setWordWrap(True)
            v.addWidget(lw)
            ll.addWidget(box)
            self.lists[key] = lw
        self.comp = make_table([Col("name", "רכיב", align="l"), Col("v", "ערך (0=Risk-Off, 1=Risk-On)", fmt=lambda v: f"{v:.2f}  " + "█" * int(round(v * 10)) + "░" * (10 - int(round(v * 10))))])
        ll.addWidget(self.comp[0])
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.chart = PriceChart()
        self.sec_chart = HBars()
        self.sec_note = muted("")
        rl.addWidget(self.chart, 2)
        rl.addWidget(QLabel("חוזק יחסי של סקטורים מול SPY (63/20/126 ימים משוקלל)"))
        rl.addWidget(self.sec_chart, 2)
        rl.addWidget(self.sec_note)
        sp.addWidget(left)
        sp.addWidget(right)
        sp.setSizes([520, 700])
        lay.addWidget(sp, 1)

    def refresh(self):
        sc = self.core.latest_scan()
        if sc is None:
            self.banner.show_msg("אין סריקה – משטר השוק מחושב בסריקה.")
            return
        self.banner.show_msg("נתונים סינתטיים – המשטר אינו משקף שוק אמיתי." if sc.synthetic else "")
        rg = sc.regime
        col = {"RISK_ON": theme.GOOD, "NEUTRAL": theme.WARN, "RISK_OFF": theme.BAD}[rg["regime"]]
        mark = {"RISK_ON": "▲ ", "NEUTRAL": "■ ", "RISK_OFF": "▼ "}[rg["regime"]]
        self.c_reg.set(mark + i18n.REGIME[rg["regime"]], f"ציון {rg['score']:.0f}/100" + (f" | עקיפה: {rg['override']}" if rg["override"] else ""), col)
        self.c_conf.set(f"{rg['confidence']:.0%}", "סטטוס כיול: " + ("לא מכויל (heuristic, לא הסתברות)" if rg["calibration"] == "uncalibrated" else rg["calibration"]))
        self.c_post.set(i18n.POSTURE[rg["posture"]].split(" – ")[0], f"מומלץ עד {rg['suggested_max_positions']} פוזיציות. מגבלות הסיכון הקשיחות ללא שינוי.")
        for key, items in (("evidence", rg["evidence"]), ("counter", rg["counterarguments"]), ("invalid", rg["invalidations"])):
            self.lists[key].clear()
            for it in items:
                r = Reason(it["code"], it["params"], it["positive"])
                self.lists[key].addItem(i18n.mark(r) + i18n.render(r))
        names = {"trend_spy": "מגמת SPY", "trend_qqq": "מגמת QQQ", "breadth": "רוחב שוק", "volatility": "תנודתיות", "drawdown": "ירידה משיא", "correlation": "קורלציה"}
        self.comp[1].set_rows([{"name": names.get(k, k), "v": v} for k, v in rg["components"].items()])
        secs = rg["strongest_sectors"] + rg["weakest_sectors"]
        seen, items = set(), []
        for s, v in sorted(secs, key=lambda t: -t[1]):
            if s not in seen:
                seen.add(s)
                items.append((f"{SECTOR_ETFS.get(s, s)} ({s})", v))
        self.sec_chart.show_bars(list(reversed(items)))
        self.sec_note.setText("חזקים: " + ", ".join(s for s, _ in rg["strongest_sectors"]) + " | חלשים: " + ", ".join(s for s, _ in rg["weakest_sectors"])
                              + (" | חסר: " + ", ".join(rg["missing"]) if rg["missing"] else ""))
        df, _ = self.core.engine.load("SPY")
        if len(df):
            self.chart.show_frame(build_frame(df.tail(520)), title="SPY")


# ====================================================================== Portfolio
class PositionDialog(QDialog):
    def __init__(self, parent, sectors):
        super().__init__(parent)
        self.setWindowTitle("הוספת פוזיציה קיימת (ידני)")
        self.setLayoutDirection(Qt.RightToLeft)
        f = QFormLayout(self)
        self.sym = QLineEdit()
        self.qty = QDoubleSpinBox(); self.qty.setRange(0, 1e9); self.qty.setDecimals(2)
        self.cost = QDoubleSpinBox(); self.cost.setRange(0, 1e7); self.cost.setDecimals(4)
        self.stop = QDoubleSpinBox(); self.stop.setRange(0, 1e7); self.stop.setDecimals(4); self.stop.setSpecialValueText("ללא")
        self.target = QDoubleSpinBox(); self.target.setRange(0, 1e7); self.target.setDecimals(4); self.target.setSpecialValueText("ללא")
        self.sector = QComboBox(); self.sector.addItem("—", "")
        for etf, nm in SECTOR_ETFS.items():
            self.sector.addItem(f"{nm} ({etf})", etf)
        self.strategy = QComboBox()
        for lbl, v in (("לא מוגדר", ""), ("סווינג", "swing"), ("טווח ארוך", "longterm")):
            self.strategy.addItem(lbl, v)
        self.sym.setText("")
        for lbl, w in (("סימול", self.sym), ("כמות", self.qty), ("מחיר ממוצע", self.cost), ("סטופ", self.stop), ("יעד", self.target), ("סקטור", self.sector), ("אסטרטגיה", self.strategy)):
            f.addRow(lbl, w)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def position(self) -> Position:
        return Position(self.sym.text().strip().upper(), self.qty.value(), self.cost.value(), self.stop.value() or None,
                        self.target.value() or None, self.sector.currentData(), self.strategy.currentData())


class PortfolioScreen(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win, self.core = win, win.core
        self.report = None
        lay = QVBoxLayout(self)
        lay.addWidget(title("מעקב תיק (נייר / החזקות ידניות)"))
        self.banner = Banner()
        lay.addWidget(self.banner)
        cards = QHBoxLayout()
        self.c_eq, self.c_cash, self.c_pl, self.c_n, self.c_dd = (Card(t) for t in ("הון כולל", "מזומן", "רווח/הפסד", "פוזיציות", "ירידה מהשיא"))
        for c in (self.c_eq, self.c_cash, self.c_pl, self.c_n, self.c_dd):
            cards.addWidget(c)
        lay.addLayout(cards)
        btns = QHBoxLayout()
        for text, fn, name in (("➕ הוסף פוזיציה קיימת", self.add_manual, ""), ("📥 ייבוא CSV", self.import_csv, ""),
                               ("🔄 חשב מחדש", self.recompute, "primary"), ("⬆ החל סטופ נגרר מוצע", self.apply_trailing, ""),
                               ("✏ ערוך סטופ", self.edit_stop, ""), ("💵 סגור פוזיציה (נייר)", self.close_pos, ""),
                               ("🗑 הסר רשומה", self.remove, "danger"), ("⚙ אפס חשבון נייר", self.reset_account, "danger")):
            b = QPushButton(text)
            if name:
                b.setObjectName(name)
            b.clicked.connect(fn)
            btns.addWidget(b)
        lay.addLayout(btns)
        tabs = QTabWidget()
        fm = lambda v: "" if v is None else f"{v:,.2f}"                                                      # noqa: E731
        self.pos = make_table([
            Col("symbol", "סימול", align="l"), Col("status", "סטטוס", fmt=lambda v: STATUS_MARK[v]), Col("qty", "כמות", fmt=lambda v: f"{v:,.0f}"),
            Col("avg_cost", "מחיר ממוצע", fmt=fm), Col("price", "מחיר", fmt=fm), Col("market_value", "שווי", fmt=lambda v: f"{v:,.0f}"),
            Col("pnl", "רווח/הפסד $", fmt=lambda v: f"{v:+,.0f}"), Col("pnl_pct", "רווח/הפסד %", fmt=lambda v: f"{v:+.1%}"),
            Col("stop", "סטופ", fmt=fm), Col("target", "יעד", fmt=fm), Col("r_multiple", "R", fmt=lambda v: "" if v is None else f"{v:+.1f}R"),
            Col("dist_to_stop_atr", "מרחק לסטופ (ATR)", fmt=lambda v: "" if v is None else f"{v:.1f}"),
            Col("open_risk", "סיכון פתוח $", fmt=lambda v: f"{v:,.0f}"), Col("trailing_stop", "סטופ נגרר מוצע", fmt=fm),
            Col("sector", "סקטור", fmt=lambda v: v or "—"), Col("strategy", "אסטרטגיה", fmt=lambda v: i18n.STRATEGY.get(v, "—")),
            Col("flags", "הערות", align="l", fmt=lambda v: v)], row_color=lambda r: STATUS_COLOR[r["status"]])
        tabs.addTab(self.pos[0], "פוזיציות")
        self.trades = make_table([Col("ts", "זמן", fmt=local_ts), Col("symbol", "סימול"), Col("side", "צד"), Col("qty", "כמות", fmt=lambda v: f"{v:,.0f}"),
                                  Col("price", "מחיר", fmt=fm), Col("fees", "עמלות", fmt=fm), Col("realized_pl", "רווח ממומש", fmt=lambda v: f"{v:+,.2f}"), Col("note", "הערה", align="l")])
        tabs.addTab(self.trades[0], "היסטוריית עסקאות")
        alloc = QWidget()
        al = QHBoxLayout(alloc)
        self.bar_stock, self.bar_sector = HBars(), HBars()
        for t, w in (("הקצאה לפי מניה", self.bar_stock), ("הקצאה לפי סקטור", self.bar_sector)):
            box = QGroupBox(t)
            QVBoxLayout(box).addWidget(w)
            al.addWidget(box)
        tabs.addTab(alloc, "הקצאה וריכוז")
        self.warn = QTextEdit(); self.warn.setReadOnly(True); self.warn.setLayoutDirection(Qt.RightToLeft)
        tabs.addTab(self.warn, "התראות ואזהרות")
        lay.addWidget(tabs, 1)
        self.note = muted("סטופ-לוס אינו מבטיח ביצוע במחיר הסטופ (גאפים, החלקה, עמלות). הצעות סטופ נגרר הן המלצה בלבד ולא מוחלות אוטומטית.")
        lay.addWidget(self.note)

    # ---------------------------------------------------------------
    def refresh(self):
        self.recompute(record=False)

    def recompute(self, *_, record: bool = True):
        core = self.core
        rep = core.monitor(record_equity=record)
        self.report = rep
        m = rep.metrics
        st = core.portfolio_state()
        pl = m["equity"] - core.pstore.starting_equity
        self.c_eq.set(f"{m['equity']:,.0f}$", f"שווי פוזיציות {m['market_value']:,.0f}$ ({m['exposure_pct']:.0%})")
        self.c_cash.set(f"{m['cash']:,.0f}$", "")
        self.c_pl.set(f"{pl:+,.0f}$", f"ממומש {core.pstore.realized_pl():+,.0f}$ | לא ממומש {sum(p.pnl for p in rep.positions):+,.0f}$", theme.GOOD if pl >= 0 else theme.BAD)
        self.c_n.set(f"{m['n_positions']}/{core.settings.risk.max_positions}", f"סיכון פתוח {m['open_risk']:,.0f}$ ({m['open_risk_pct']:.1%})" + (" *כולל הנחה לפוזיציות ללא סטופ" if m["assumed_stop_positions"] else ""))
        self.c_dd.set(f"{m['drawdown']:.1%}", f"שיא הון: {m['peak_equity']:,.0f}$", theme.BAD if m["drawdown"] <= -core.settings.risk.drawdown_alert_pct else None)
        rows = [dict(vars(h), flags="; ".join(i18n.render(r) for r in h.flags)) for h in rep.positions]
        self.pos[1].set_rows(rows)
        self.pos[0].resizeColumnsToContents()
        self.trades[1].set_rows([vars(t) for t in core.pstore.trades(300)])
        self.bar_stock.show_bars(sorted([(k, v) for k, v in m["weights"].items()], key=lambda t: t[1]))
        self.bar_sector.show_bars(sorted([(k, v) for k, v in m["sector_weights"].items()], key=lambda t: t[1]))
        lines = [f"{i18n.SEVERITY[a.severity]} | {a.title} | {a.body}" for a in rep.alerts] + [f"נתונים: {w}" for w in rep.data_warnings]
        self.warn.setPlainText("\n".join(lines) or "אין התראות או אזהרות.")
        self.banner.show_msg("; ".join(rep.data_warnings[:3]), bad=True) if rep.data_warnings else self.banner.show_msg("")
        if record and rep.alerts:
            outs = core.alerts.process(rep.alerts)
            sent = sum(1 for o in outs if o.status == "sent")
            self.win.notify(f"התראות חדשות: {sum(1 for o in outs if o.status != 'duplicate_suppressed')}, נשלחו ל-Telegram: {sent}")

    def _sel(self):
        r = selected_row(self.pos[0], self.pos[2], self.pos[1])
        if not r:
            QMessageBox.information(self, "בחירה", "בחר פוזיציה מהטבלה.")
        return r

    def add_manual(self):
        d = PositionDialog(self, SECTOR_ETFS)
        if d.exec():
            p = d.position()
            try:
                self.core.broker.add_manual_position(p)
            except ValueError as e:
                QMessageBox.warning(self, "שגיאה", str(e))
                return
            self.recompute()

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "בחר קובץ CSV", "", "CSV (*.csv)")
        if not path:
            return
        res = load_positions_csv(path, self.core.universe.sector)
        msg = f"נקראו {len(res.positions)} פוזיציות תקינות, {len(res.errors)} שגיאות."
        if res.errors:
            msg += "\n\n" + "\n".join(res.errors[:15])
        if res.positions:
            replace = QMessageBox.question(self, "ייבוא", msg + "\n\nלהחליף את כל הפוזיציות הקיימות? (לא = מיזוג)", QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if replace == QMessageBox.Cancel:
                return
            if replace == QMessageBox.Yes:
                self.core.pstore.clear_positions()
            for p in res.positions:
                self.core.broker.add_manual_position(p)
            self.recompute()
        else:
            QMessageBox.warning(self, "ייבוא", msg)

    def apply_trailing(self):
        r = self._sel()
        if not r:
            return
        if not r["trailing_stop"]:
            QMessageBox.information(self, "סטופ נגרר", "אין הצעה לסטופ נגרר גבוה מהנוכחי.")
            return
        try:
            self.core.broker.update_stop(r["symbol"], r["trailing_stop"], price=r["price"])
        except ValueError as e:
            QMessageBox.warning(self, "שגיאה", str(e))
        self.recompute()

    def edit_stop(self):
        r = self._sel()
        if not r:
            return
        v, ok = QInputDialog.getDouble(self, "ערוך סטופ", f"סטופ חדש עבור {r['symbol']} (מחיר נוכחי {r['price']:.2f}):", r["stop"] or 0.0, 0.0, 1e7, 4)
        if ok:
            try:
                self.core.broker.update_stop(r["symbol"], v, price=r["price"], allow_lower=True)
            except ValueError as e:
                QMessageBox.warning(self, "שגיאה", str(e))
            self.recompute()

    def close_pos(self):
        r = self._sel()
        if not r:
            return
        if QMessageBox.question(self, "סגירה", f"לסגור את {r['symbol']} ({r['qty']:.0f} מניות) במחיר {r['price']:.2f} (נייר, כולל החלקה ועמלה)?") == QMessageBox.Yes:
            pl = self.core.broker.close_position(r["symbol"], r["price"])
            self.win.notify(f"נסגרה פוזיציה {r['symbol']}: רווח/הפסד {pl:+,.2f}$")
            self.recompute()

    def remove(self):
        r = self._sel()
        if r and QMessageBox.question(self, "הסרה", f"להסיר את הרשומה {r['symbol']} ללא השפעה על המזומן?") == QMessageBox.Yes:
            self.core.pstore.delete_position(r["symbol"])
            self.recompute()

    def reset_account(self):
        v, ok = QInputDialog.getDouble(self, "איפוס חשבון נייר", "הון התחלתי חדש ($). פעולה זו תמחק פוזיציות, עסקאות והיסטוריית הון:", self.core.settings.risk.starting_cash, 0, 1e9, 2)
        if ok and QMessageBox.warning(self, "אישור", "למחוק את כל נתוני התיק?", QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.core.pstore.reset(v)
            self.recompute()


# ====================================================================== Risk center
class RiskScreen(QWidget):
    paper_opened = Signal()

    def __init__(self, win):
        super().__init__()
        self.win, self.core = win, win.core
        self.decision = None
        self.proposal = None
        lay = QVBoxLayout(self)
        lay.addWidget(title("מרכז סיכונים"))
        self.banner = Banner()
        lay.addWidget(self.banner)
        sp = QSplitter(Qt.Horizontal)
        # --- calculator
        box = QGroupBox("מחשבון גודל פוזיציה ובדיקת כללי סיכון (דטרמיניסטי – ללא AI)")
        f = QFormLayout(box)
        self.sym = QLineEdit()
        self.entry = QDoubleSpinBox(); self.entry.setRange(0, 1e7); self.entry.setDecimals(4)
        self.stop = QDoubleSpinBox(); self.stop.setRange(0, 1e7); self.stop.setDecimals(4)
        self.target = QDoubleSpinBox(); self.target.setRange(0, 1e7); self.target.setDecimals(4)
        self.strategy = QComboBox()
        for lbl, v in (("סווינג", "swing"), ("טווח ארוך", "longterm"), ("לא מוגדר", "")):
            self.strategy.addItem(lbl, v)
        self.override = QSpinBox(); self.override.setRange(0, 10_000_000); self.override.setSpecialValueText("לפי הנוסחה")
        for lbl, w in (("סימול", self.sym), ("מחיר כניסה", self.entry), ("סטופ", self.stop), ("יעד (אופציונלי)", self.target), ("אסטרטגיה", self.strategy), ("כמות ידנית (≤ נוסחה)", self.override)):
            f.addRow(lbl, w)
        b = QHBoxLayout()
        self.b_eval = QPushButton("בדוק והצע גודל"); self.b_eval.setObjectName("primary"); self.b_eval.clicked.connect(self.evaluate)
        self.b_fill = QPushButton("בצע פוזיציית נייר"); self.b_fill.setEnabled(False); self.b_fill.clicked.connect(self.open_paper)
        b.addWidget(self.b_eval); b.addWidget(self.b_fill)
        f.addRow(b)
        self.result = QTextEdit(); self.result.setReadOnly(True); self.result.setLayoutDirection(Qt.RightToLeft)
        f.addRow(self.result)
        f.addRow(muted("נוסחה: כמות = floor(הון × 1% ÷ |כניסה − סטופ|). פוזיציות ארוכות בלבד. הדחייה היא סופית – אפשר רק להקטין כמות, לעולם לא להגדיל מעבר לנוסחה."))
        # --- limits and breaches
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.limits = make_table([Col("name", "מדד", align="l"), Col("cur", "נוכחי"), Col("limit", "מגבלה"), Col("state", "מצב")], row_color=lambda r: theme.BAD if r["state"].startswith("▲") else None)
        rl.addWidget(QLabel("ניצול מגבלות הסיכון"))
        rl.addWidget(self.limits[0])
        rl.addWidget(QLabel("חריגות והתראות סיכון פעילות"))
        self.breaches = QTextEdit(); self.breaches.setReadOnly(True); self.breaches.setLayoutDirection(Qt.RightToLeft)
        rl.addWidget(self.breaches)
        sp.addWidget(box); sp.addWidget(right); sp.setSizes([560, 700])
        lay.addWidget(sp, 1)

    def prefill(self, symbol: str, strategy: str):
        scan = self.core.latest_scan()
        c = scan.by_symbol(symbol) if scan else None
        self.sym.setText(symbol)
        i = self.strategy.findData(strategy)
        self.strategy.setCurrentIndex(max(i, 0))
        lv = ((c.scores.get(strategy) or {}).get("levels")) if c else None
        if lv:
            self.entry.setValue(lv["entry"]); self.stop.setValue(lv["stop"]); self.target.setValue(lv["target"])
        self.override.setValue(0)
        self.result.clear()
        self.b_fill.setEnabled(False)

    def evaluate(self):
        sym = self.sym.text().strip().upper()
        if not sym:
            return
        ov = self.override.value() or None
        d = self.core.evaluate_proposal(sym, self.entry.value(), self.stop.value(), self.target.value() or None,
                                        strategy=self.strategy.currentData(), shares_override=ov)
        self.decision = d
        self.proposal = (sym, self.entry.value(), self.stop.value(), self.target.value() or None, self.strategy.currentData())
        html = [f"<h3 style='color:{theme.GOOD if d.approved else theme.BAD}'>{'✔ מאושר' if d.approved else '✖ נדחה'}</h3>",
                f"תקציב סיכון (1% מההון): <b>{d.risk_budget:,.2f}$</b><br>כמות לפי הנוסחה: <b>{d.computed_shares}</b>"]
        if d.shares:
            html.append(f"<br>כמות מוערכת: <b>{d.shares}</b> | שווי {d.position_value:,.0f}$ ({d.position_pct:.1%} מההון) | סיכון עד הסטופ {d.risk_amount:,.2f}$"
                        f"<br>הפסד במצב קיצון (גאפ + החלקה + עמלות): <b>{d.worst_case_loss:,.2f}$</b>")
        if d.reasons:
            html.append("<br><b>סיבות דחייה:</b><br>" + "<br>".join(f"<span style='color:{theme.BAD}'>✖</span> {i18n.render(r)}" for r in d.reasons))
            if d.suggested_max_shares >= 1 and d.suggested_max_shares != d.shares:
                html.append(f"<br>💡 הכמות המרבית שעומדת בכל מגבלות הגודל/מזומן/חשיפה: <b>{d.suggested_max_shares}</b> (הזן אותה בשדה 'כמות ידנית' ובדוק שוב)")
        if d.notes:
            html.append(f"<br><span style='color:{theme.MUTED}'>הערות: {', '.join(d.notes)}</span>")
        self.result.setHtml("<div dir='rtl'>" + "".join(html) + "</div>")
        self.b_fill.setEnabled(d.approved)

    def open_paper(self):
        d = self.decision
        if not d or not d.approved:
            return
        sym = self.proposal[0]
        df, q = self.core.engine.load(sym)
        last = float(df["close"].iloc[-1]) if len(df) else None
        prices = self.core.last_prices([p.symbol for p in self.core.pstore.positions()])
        if QMessageBox.question(self, "אישור", f"לפתוח פוזיציית נייר: {d.shares} × {sym} במחיר ~{d.order.entry:.2f}, סטופ {d.order.stop:.2f}?\n(הדמיה בלבד – לא נשלחת הוראה לברוקר)") != QMessageBox.Yes:
            return
        try:
            self.core.broker.open_position(d.order, prices, now=self.core.clock(), last_price=last, data_ok=q.ok, data_detail=",".join(q.codes()))
        except OrderRejected as e:
            QMessageBox.warning(self, "נדחה", "\n".join(i18n.render(r) for r in e.reasons))
            return
        self.win.notify(f"נפתחה פוזיציית נייר {sym}")
        self.b_fill.setEnabled(False)
        self.refresh()
        self.paper_opened.emit()

    def refresh(self):
        core, c = self.core, self.core.settings.risk
        self.banner.show_msg("")
        st = core.portfolio_state()
        peak = core.pstore.peak_equity()
        m = core.risk.metrics(st, peak)

        def row(name, cur, lim, bad, fmt):
            return {"name": name, "cur": fmt(cur), "limit": fmt(lim), "state": "▲ חריגה" if bad else "● תקין"}
        pct = lambda v: f"{v:.1%}"                                                                 # noqa: E731
        rows = [row("פוזיציות פתוחות", m["n_positions"], c.max_positions, m["n_positions"] > c.max_positions, lambda v: f"{v:.0f}"),
                row("סיכון פתוח מצטבר", m["open_risk_pct"], c.max_total_risk_pct, m["open_risk_pct"] > c.max_total_risk_pct, pct),
                row("פוזיציה בודדת הגדולה ביותר", m["largest_weight"], c.max_position_pct, m["largest_weight"] > c.max_position_pct, pct),
                row("ירידה מהשיא", -m["drawdown"], c.drawdown_alert_pct, -m["drawdown"] >= c.drawdown_alert_pct, pct)]
        for sec, w in sorted(m["sector_weights"].items(), key=lambda t: -t[1]):
            rows.append(row(f"סקטור {sec}", w, c.max_sector_pct, sec != "?" and w > c.max_sector_pct, pct))
        rows.append({"name": "סיכון לעסקה", "cur": pct(c.risk_per_trade), "limit": pct(c.risk_per_trade), "state": "● קבוע"})
        self.limits[1].set_rows(rows)
        rep = core.monitor(record_equity=False)
        crit = [a for a in rep.alerts if a.severity in (CRITICAL, WARNING)]
        self.breaches.setPlainText("\n".join(f"{i18n.SEVERITY[a.severity]} | {a.body}" for a in crit) or "אין חריגות.")


# ====================================================================== Alerts
class AlertsScreen(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win, self.core = win, win.core
        lay = QVBoxLayout(self)
        lay.addWidget(title("היסטוריית התראות"))
        row = QHBoxLayout()
        self.sev = QComboBox()
        for lbl, v in (("כל החומרות", ""), ("קריטי", CRITICAL), ("אזהרה", WARNING), ("מידע", "info")):
            self.sev.addItem(lbl, v)
        self.sev.currentIndexChanged.connect(lambda *_: self.proxy.refilter())
        b1 = QPushButton("רענן"); b1.clicked.connect(self.refresh)
        b2 = QPushButton("נסה לשלוח התראות שנדחו"); b2.clicked.connect(self.flush)
        b3 = QPushButton("שלח הודעת בדיקה ל-Telegram"); b3.clicked.connect(self.test)
        for w in (self.sev, b1, b2, b3):
            row.addWidget(w)
        row.addStretch(1)
        lay.addLayout(row)
        status_he = {"sent": "✔ נשלח", "recorded": "● נרשם", "not_sent_no_telegram": "✖ לא נשלח (אין Telegram)", "deferred_quiet": "⏸ נדחה (שעות שקט)",
                     "deferred_rate": "⏸ נדחה (מגבלת קצב)", "failed": "✖ נכשל", "expired": "⌛ פג תוקף"}
        self.t, self.m, self.proxy = make_table([
            Col("ts", "זמן (ישראל)", fmt=local_ts), Col("severity", "חומרה", fmt=lambda v: i18n.SEVERITY.get(v, v)), Col("kind", "סוג"),
            Col("symbol", "סימול"), Col("title", "כותרת", align="l"), Col("body", "פירוט", align="l"),
            Col("status", "סטטוס", fmt=lambda v: status_he.get(v, v)), Col("reason", "פרטים", align="l")],
            row_color=lambda r: theme.BAD if r["severity"] == CRITICAL else theme.WARN if r["severity"] == WARNING else None)
        self.proxy.predicate = lambda r: not self.sev.currentData() or r["severity"] == self.sev.currentData()
        lay.addWidget(self.t, 1)
        self.note = muted("")
        lay.addWidget(self.note)

    def refresh(self):
        self.m.set_rows(self.core.alerts.history(1000))
        self.t.resizeColumnsToContents()
        self.note.setText("Telegram: " + ("מוגדר – רק התראות קריטיות נשלחות" if self.core.alerts.send else "לא מוגדר – ההתראות נשמרות כאן בלבד")
                          + f" | שעות שקט {self.core.settings.alerts.quiet_start}-{self.core.settings.alerts.quiet_end} ({self.core.settings.alerts.quiet_tz}) | עד {self.core.settings.alerts.max_per_hour} הודעות בשעה")

    def flush(self):
        outs = self.core.alerts.flush_deferred()
        self.win.notify(f"נבדקו {len(outs)} התראות ממתינות")
        self.refresh()

    def test(self):
        r = self.core.telegram_test()
        QMessageBox.information(self, "Telegram", "ההודעה נשלחה בהצלחה." if r.ok else f"השליחה נכשלה: {r.error}")
