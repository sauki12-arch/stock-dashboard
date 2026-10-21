"""pyqtgraph charts: candlesticks (up = hollow, down = filled so direction does not depend on colour), SMAs, volume,
trade levels and simple horizontal bars."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPicture

from . import theme

pg.setConfigOptions(antialias=True, background=theme.PANEL, foreground=theme.TEXT)


class CandleItem(pg.GraphicsObject):
    def __init__(self, df: pd.DataFrame):
        super().__init__()
        self.pic = QPicture()
        p = QPainter(self.pic)
        up, dn = QColor(theme.GOOD), QColor(theme.BAD)
        for i, (o, h, l, c) in enumerate(zip(df["open"], df["high"], df["low"], df["close"])):
            col = up if c >= o else dn
            p.setPen(QPen(col, 1))
            p.drawLine(QPointF(i, l), QPointF(i, h))
            p.setBrush(QBrush(Qt.NoBrush) if c >= o else QBrush(col))        # hollow = up, filled = down
            top, bot = max(o, c), min(o, c)
            p.drawRect(QRectF(i - 0.32, bot, 0.64, max(top - bot, (h - l) * 0.01)))
        p.end()
        self._df = df

    def paint(self, p, *args):
        p.drawPicture(0, 0, self.pic)

    def boundingRect(self):
        return QRectF(self.pic.boundingRect())


class DateAxis(pg.AxisItem):
    def __init__(self, dates, **kw):
        super().__init__(orientation="bottom", **kw)
        self.dates = list(dates)

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            i = int(round(v))
            out.append(self.dates[i].strftime("%d/%m/%y") if 0 <= i < len(self.dates) and abs(v - i) < 1e-6 else "")
        return out


class PriceChart(pg.GraphicsLayoutWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(260)
        self.setLayoutDirection(Qt.LeftToRight)         # charts stay LTR (time flows left→right)

    def show_frame(self, f: pd.DataFrame, levels: dict | None = None, bars: int = 160, title: str = "") -> None:
        self.clear()
        if f is None or len(f) < 5:
            self.addLabel("אין נתונים", color=theme.MUTED)
            return
        d = f.tail(bars)
        n = len(d)
        axis = DateAxis(d.index)
        p1 = self.addPlot(row=0, col=0, axisItems={"bottom": axis}, title=title or None)
        p1.showGrid(x=True, y=True, alpha=0.15)
        p1.addItem(CandleItem(d))
        x = np.arange(n)
        for col, name, color in (("sma20", "SMA20", "#f5b942"), ("sma50", "SMA50", "#4da3ff"), ("sma200", "SMA200", "#c792ea")):
            if col in d and d[col].notna().any():
                p1.plot(x, d[col].to_numpy(), pen=pg.mkPen(color, width=1.4), name=name)
        if levels:
            for k, color, style in (("entry", "#e6e9ee", Qt.DashLine), ("stop", theme.BAD, Qt.DotLine), ("target", theme.GOOD, Qt.DotLine)):
                if levels.get(k):
                    ln = pg.InfiniteLine(pos=levels[k], angle=0, pen=pg.mkPen(color, width=1.2, style=style),
                                         label={"entry": "כניסה", "stop": "סטופ", "target": "יעד"}[k] + f" {levels[k]:.2f}",
                                         labelOpts={"position": 0.97, "color": color})
                    p1.addItem(ln)
        p1.addLegend(offset=(10, 10))
        p1.setXRange(-1, n + 1)
        p2 = self.addPlot(row=1, col=0, axisItems={"bottom": DateAxis(d.index)})
        p2.setMaximumHeight(90)
        p2.setXLink(p1)
        p2.addItem(pg.BarGraphItem(x=x, height=d["volume"].to_numpy(), width=0.7, brush=pg.mkBrush(theme.ACCENT + "99")))
        p2.showGrid(y=True, alpha=0.15)


class HBars(pg.PlotWidget):
    """Horizontal bars: values positive -> accent, negative -> warning; labels on the left axis."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(160)
        self.setLayoutDirection(Qt.LeftToRight)
        self.showGrid(x=True, alpha=0.15)
        self.getAxis("left").setWidth(170)

    def show_bars(self, items: list[tuple[str, float]], unit_pct: bool = True) -> None:
        self.clear()
        if not items:
            return
        ys = np.arange(len(items))
        vals = np.array([v for _, v in items], float)
        brushes = [pg.mkBrush(theme.ACCENT if v >= 0 else theme.WARN) for v in vals]
        self.addItem(pg.BarGraphItem(x0=np.zeros(len(vals)), y=ys, height=0.6, width=vals, brushes=brushes))
        ax = self.getAxis("left")
        ax.setTicks([[(i, lbl) for i, (lbl, _) in enumerate(items)]])
        self.setYRange(-0.6, len(items) - 0.4)
        lo, hi = min(0.0, float(vals.min())), max(0.0, float(vals.max()))
        pad = (hi - lo) * 0.2 or 0.05
        self.setXRange(lo - pad, hi + pad)
        for y, v in zip(ys, vals):
            t = pg.TextItem(f"{v:+.1%}" if unit_pct else f"{v:.1f}", color=theme.TEXT, anchor=(0 if v >= 0 else 1, 0.5))
            t.setPos(v, y)
            self.addItem(t)
