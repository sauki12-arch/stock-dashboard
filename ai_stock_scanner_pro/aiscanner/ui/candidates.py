"""Discovery / Swing / Long-Term screens share one browser widget (table + filters + explainable detail pane)."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QSplitter, QTextEdit, QVBoxLayout, QWidget)

from .. import i18n
from ..data.universe import SECTOR_ETFS
from ..discovery.indicators import build_frame
from ..models import LONGTERM, STRATEGIES, SWING, Reason
from . import theme
from .charts import PriceChart
from .widgets import Banner, Col, fmt_num, fmt_pct, make_table, muted, selected_row, title

KLASS_COLOR = {"high": theme.GOOD, "strong": theme.ACCENT, "monitor": theme.WARN, "low": theme.MUTED}


def klass_text(sc: dict | None) -> str:
    if not sc:
        return "—"
    return i18n.KLASS_MARK[sc["klass"]] + i18n.KLASS[sc["klass"]]


def reasons_html(items: list[dict]) -> str:
    out = []
    for it in items:
        r = Reason(it["code"], it["params"], it["positive"])
        color = {True: theme.GOOD, False: theme.BAD, None: theme.MUTED}[r.positive]
        out.append(f'<span style="color:{color}">{i18n.mark(r)}</span>{i18n.render(r)}')
    return "<br>".join(out)


class CandidateDetail(QWidget):
    open_sizing = Signal(str, str)           # symbol, strategy

    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.core = core
        self.cur: tuple | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.head = QLabel("בחר מניה מהרשימה")
        self.head.setObjectName("title")
        self.sub = muted("")
        self.chart = PriceChart()
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setLayoutDirection(Qt.RightToLeft)
        self.btn = QPushButton("חשב גודל פוזיציה וסיכון…")
        self.btn.setObjectName("primary")
        self.btn.setEnabled(False)
        self.btn.clicked.connect(lambda: self.cur and self.open_sizing.emit(self.cur[0], self.cur[1]))
        top = QHBoxLayout()
        top.addWidget(self.head, 1)
        top.addWidget(self.btn)
        lay.addLayout(top)
        lay.addWidget(self.sub)
        sp = QSplitter(Qt.Vertical)
        sp.addWidget(self.chart)
        sp.addWidget(self.text)
        sp.setSizes([280, 260])
        lay.addWidget(sp, 1)

    def show_candidate(self, cand, strategy: str) -> None:
        if cand is None:
            return
        self.cur = (cand.symbol, strategy)
        sc = cand.scores.get(strategy)
        self.head.setText(f"{cand.symbol}  —  {i18n.STRATEGY[strategy]}")
        etf = cand.sector
        self.sub.setText(f"סקטור: {SECTOR_ETFS.get(etf, '—')} ({etf or '—'}) | מחיר אחרון {cand.price:,.2f} | נר אחרון {cand.last_bar}")
        self.btn.setEnabled(bool(sc and sc.get("levels")))
        df, _ = self.core.engine.load(cand.symbol)
        spy = self.core.engine.load("SPY")[0]
        f = build_frame(df.tail(520), spy if len(spy) else None) if len(df) else None
        self.chart.show_frame(f, (sc or {}).get("levels"), title=cand.symbol)
        html = []
        if not cand.eligible:
            html.append(f"<h3 style='color:{theme.BAD}'>לא עבר שערי זכאות — לא מדורג</h3>" + reasons_html(cand.gate_failures))
        elif sc:
            ok = sc["validated"]
            html.append(f"<h3>ציון {sc['total']:.1f}/100 — {klass_text(sc)} "
                        f"<span style='color:{theme.GOOD if ok else theme.WARN}'>{'✔ עבר ולידציה' if ok else '✖ לא עבר ולידציה'}</span></h3>")
            if not self.core.settings.scan.bands_calibrated:
                html.append(f"<p style='color:{theme.MUTED}'>⚠ ספי הציון (85/70/55) זמניים ואינם מכוילים בבקטסט.</p>")
            if sc["failures"]:
                html.append("<b>כללי ולידציה שנכשלו:</b><br>" + reasons_html(sc["failures"]))
            lv = sc.get("levels")
            if lv:
                html.append(f"<p><b>רמות מוצעות (אינדיקציה, לא הוראה):</b> כניסה ~{lv['entry']:.2f} | סטופ {lv['stop']:.2f} "
                            f"({i18n.BASIS.get(lv['stop_basis'], lv['stop_basis'])}) | יעד {lv['target']:.2f} "
                            f"({i18n.BASIS.get(lv['target_basis'], lv['target_basis'])}) | סיכוי/סיכון {lv['rr']:.2f}</p>")
            if sc["setups"]:
                html.append("<b>תבניות:</b> " + ", ".join(i18n.PATTERNS.get(s, s) for s in sc["setups"]))
            if sc["experimental"]:
                html.append("<br><b>תבניות ניסיוניות (לא משפיעות על הציון):</b> " + ", ".join(i18n.PATTERNS.get(s, s) for s in sc["experimental"]))
            html.append("<hr><b>פירוט ציון:</b>")
            for k, comp in sc["components"].items():
                html.append(f"<p><b>{i18n.COMPONENTS[k]}: {comp['points']:.1f}/{comp['max']:.0f}</b><br>{reasons_html(comp['reasons'])}</p>")
        if cand.quality:
            html.append(f"<p style='color:{theme.WARN}'>⚠ אזהרות נתונים: {', '.join(cand.quality)}</p>")
        self.text.setHtml("<div dir='rtl'>" + "".join(html) + "</div>")


class CandidateBrowser(QWidget):
    open_sizing = Signal(str, str)

    def __init__(self, core, strategy: str | None, parent=None):
        super().__init__(parent)
        self.core, self.strategy = core, strategy
        self.scan = None
        lay = QVBoxLayout(self)
        names = {None: "סורק גילוי – כל המניות", SWING: "הזדמנויות סווינג", LONGTERM: "הזדמנויות טווח ארוך"}
        lay.addWidget(title(names[strategy]))
        self.banner = Banner()
        lay.addWidget(self.banner)

        f = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("חיפוש סימול…")
        self.topn = QComboBox()
        for label, n in (("Top 10", 10), ("Top 25", 25), ("Top 50", 50), ("הכל", 0)):
            self.topn.addItem(label, n)
        self.topn.setCurrentIndex(1 if strategy else 3)
        self.min_score = QDoubleSpinBox()
        self.min_score.setRange(0, 100)
        self.min_score.setValue(0)
        self.klass = QComboBox()
        self.klass.addItem("כל המחלקות", "")
        for k in ("high", "strong", "monitor", "low"):
            self.klass.addItem(i18n.KLASS_MARK[k] + i18n.KLASS[k], k)
        self.sector = QComboBox()
        self.sector.addItem("כל הסקטורים", "")
        for etf, nm in SECTOR_ETFS.items():
            self.sector.addItem(f"{nm} ({etf})", etf)
        self.valid_only = QCheckBox("מאומתים בלבד")
        self.valid_only.setChecked(strategy is not None)
        self.elig_only = QCheckBox("זכאים בלבד")
        self.elig_only.setChecked(True)
        if strategy is None:
            self.rank_by = QComboBox()
            self.rank_by.addItem("מיון לפי סווינג", SWING)
            self.rank_by.addItem("מיון לפי טווח ארוך", LONGTERM)
            f.addWidget(self.rank_by)
            self.rank_by.currentIndexChanged.connect(self.refresh)
        for w in (self.search, self.topn, QLabel("ציון מינימלי:"), self.min_score, self.klass, self.sector, self.valid_only, self.elig_only):
            f.addWidget(w)
        f.addStretch(1)
        lay.addLayout(f)

        cols = self._columns()
        self.view, self.model, self.proxy = make_table(cols, row_color=lambda r: None)
        self.detail = CandidateDetail(core)
        self.detail.open_sizing.connect(self.open_sizing)
        sp = QSplitter(Qt.Horizontal)
        sp.addWidget(self.detail)
        sp.addWidget(self.view)
        sp.setSizes([430, 900])
        lay.addWidget(sp, 1)
        self.count = muted("")
        lay.addWidget(self.count)

        self.proxy.predicate = self._accept
        for w in (self.search,):
            w.textChanged.connect(self._filters_changed)
        for w in (self.topn, self.klass, self.sector):
            w.currentIndexChanged.connect(self._filters_changed)
        for w in (self.valid_only, self.elig_only):
            w.toggled.connect(self._filters_changed)
        self.min_score.valueChanged.connect(self._filters_changed)
        self.view.selectionModel().selectionChanged.connect(self._on_select)

    # ------------------------------------------------------------------
    def _strat(self) -> str:
        return self.strategy or self.rank_by.currentData()

    def _columns(self) -> list[Col]:
        def sc(st):
            return lambda r: r["scores"].get(st)

        def lvl(st, key):
            return lambda r: ((r["scores"].get(st) or {}).get("levels") or {}).get(key)
        cols = [Col("rank", "#", fmt=lambda v: str(v) if v else "", sort=lambda v: v or 0),
                Col("symbol", "סימול", align="l"),
                Col("sector", "סקטור", fmt=lambda v: v or "—")]
        if self.strategy is None:
            for st in (SWING, LONGTERM):
                cols.append(Col(f"{st}_score", f"ציון {i18n.STRATEGY[st]}", fmt=lambda v: "—" if v is None else f"{v:.1f}",
                                getter=lambda r, st=st: (r["scores"].get(st) or {}).get("total")))
                cols.append(Col(f"{st}_klass", f"מחלקה {i18n.STRATEGY[st]}", fmt=lambda v: klass_text(v),
                                getter=lambda r, st=st: r["scores"].get(st), sort=lambda v: -1 if not v else v["total"]))
            cols += [Col("val", "אימות", fmt=lambda v: v, getter=self._val_text),
                     Col("price", "מחיר", fmt=fmt_num)]
        else:
            st = self.strategy
            cols += [Col("score", "ציון", fmt=lambda v: "—" if v is None else f"{v:.1f}", getter=lambda r: (r["scores"].get(st) or {}).get("total")),
                     Col("klass", "מחלקה", fmt=klass_text, getter=sc(st), sort=lambda v: -1 if not v else v["total"]),
                     Col("price", "מחיר", fmt=fmt_num),
                     Col("entry", "כניסה", fmt=fmt_num, getter=lvl(st, "entry")),
                     Col("stop", "סטופ", fmt=fmt_num, getter=lvl(st, "stop")),
                     Col("target", "יעד", fmt=fmt_num, getter=lvl(st, "target")),
                     Col("rr", "R/R", fmt=fmt_num, getter=lvl(st, "rr")),
                     Col("setups", "תבניות", align="l", fmt=lambda v: v, getter=lambda r: ", ".join(i18n.PATTERNS.get(s, s) for s in (r["scores"].get(st) or {}).get("setups", [])))]
        cols += [Col("rsi", "RSI", fmt=lambda v: "" if v is None else f"{v:.0f}", getter=lambda r: r["metrics"].get("rsi14")),
                 Col("relvol", "נפח יח'", fmt=lambda v: "" if v is None else f"{v:.2f}x", getter=lambda r: r["metrics"].get("relvol")),
                 Col("atrp", "ATR%", fmt=lambda v: "" if v is None else f"{v:.1%}", getter=lambda r: r["metrics"].get("atr_pct")),
                 Col("rs63", "RS63 מול SPY", fmt=lambda v: "" if v is None else f"{v:+.1%}", getter=lambda r: r["metrics"].get("rs63_spy")),
                 Col("gate", "סיבת אי-זכאות", align="l", fmt=lambda v: v, getter=lambda r: "; ".join(i18n.render(Reason(g["code"], g["params"], False)) for g in r["gate_failures"]))]
        return cols

    def _val_text(self, r) -> str:
        if not r["eligible"]:
            return "✖ לא זכאי"
        parts = []
        for st in STRATEGIES:
            s = r["scores"].get(st)
            parts.append(("✔" if s and s["validated"] else "✖") + (" ס" if st == SWING else " ט"))
        return "  ".join(parts)

    def _accept(self, r: dict) -> bool:
        st = self._strat()
        if self.elig_only.isChecked() and not r["eligible"]:
            return False
        s = r["scores"].get(st)
        if self.valid_only.isChecked() and not (s and s["validated"]):
            return False
        if s is not None and s["total"] < self.min_score.value():
            return False
        if self.min_score.value() > 0 and s is None:
            return False
        k = self.klass.currentData()
        if k and not (s and s["klass"] == k):
            return False
        if self.sector.currentData() and r["sector"] != self.sector.currentData():
            return False
        q = self.search.text().strip().upper()
        if q and q not in r["symbol"]:
            return False
        n = self.topn.currentData()
        if n:                                   # Top N applies to the validated ranking
            return 0 < r["rank"] <= n
        return True

    def _filters_changed(self, *_):
        self.proxy.refilter()
        self._update_count()

    def _update_count(self):
        self.count.setText(f"מוצגות {self.proxy.rowCount()} מתוך {len(self.model.rows)} מניות")

    # ------------------------------------------------------------------
    def refresh(self, *_):
        self.scan = self.core.latest_scan()
        if self.scan is None:
            self.banner.show_msg("עדיין לא בוצעה סריקה. לחץ על 'סרוק עכשיו' בסרגל העליון.", bad=False)
            self.model.set_rows([])
            return
        msgs = []
        if self.scan.synthetic:
            msgs.append("נתונים סינתטיים (מדומים) – לא לשימוש בהחלטות מסחר.")
        if self.scan.failed:
            msgs.append(f"{len(self.scan.failed)} מניות נכשלו בטעינה/ניתוח.")
        if self.scan.stale_symbols:
            msgs.append(f"{len(self.scan.stale_symbols)} מניות עם נתונים ישנים (לא מדורגות).")
        if not self.scan.bands_calibrated:
            msgs.append("ספי הציון זמניים (לא מכוילים).")
        self.banner.show_msg(" | ".join(msgs) + f"  • סריקה: {self.scan.ts[:16].replace('T', ' ')} UTC, נכון ל-{self.scan.as_of}", bad=bool(self.scan.stale_symbols or self.scan.failed))
        st = self._strat()
        ranked = {c.symbol: i + 1 for i, c in enumerate(self.scan.ranked(st))}
        rows = []
        for c in self.scan.candidates:
            rows.append({"rank": ranked.get(c.symbol, 0), "symbol": c.symbol, "sector": c.sector, "price": c.price,
                         "eligible": c.eligible, "scores": c.scores, "metrics": c.metrics, "gate_failures": c.gate_failures,
                         "_c": c, "last_bar": c.last_bar})
        rows.sort(key=lambda r: (r["rank"] == 0, r["rank"], -(((r["scores"].get(st) or {}).get("total")) or 0)))
        self.model.set_rows(rows)
        self.view.sortByColumn(0 if self.strategy else self._score_col(), Qt.AscendingOrder if self.strategy else Qt.DescendingOrder)
        self.view.resizeColumnsToContents()
        self._update_count()

    def _score_col(self) -> int:
        st = self._strat()
        for i, c in enumerate(self.model.cols):
            if c.key == f"{st}_score":
                return i
        return 0

    def _on_select(self, *_):
        r = selected_row(self.view, self.proxy, self.model)
        if r:
            self.detail.show_candidate(r["_c"], self._strat())
