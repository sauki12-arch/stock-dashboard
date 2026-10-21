"""Reusable widgets: generic dict-row table model, filter proxy, cards, banners."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHeaderView, QLabel, QTableView, QVBoxLayout, QWidget)

from . import theme

NEG_INF = float("-inf")


@dataclass
class Col:
    key: str
    title: str
    fmt: Callable[[Any], str] = lambda v: "" if v is None else str(v)
    align: str = "c"                                   # c|l|r
    sort: Callable[[Any], Any] | None = None
    tip: Callable[[dict], str] | None = None
    getter: Callable[[dict], Any] | None = None


class DictTableModel(QAbstractTableModel):
    def __init__(self, cols: list[Col], row_color: Callable[[dict], str | None] | None = None, parent=None):
        super().__init__(parent)
        self.cols, self.rows, self.row_color = cols, [], row_color

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.cols)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.cols[section].title
        return None

    def _val(self, c: Col, row: dict):
        return c.getter(row) if c.getter else row.get(c.key)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, c = self.rows[index.row()], self.cols[index.column()]
        v = self._val(c, row)
        if role == Qt.DisplayRole:
            try:
                return c.fmt(v)
            except Exception:  # noqa: BLE001 - never let a bad cell crash the view
                return "n/a"
        if role == Qt.UserRole:
            if c.sort:
                return c.sort(v)
            return NEG_INF if v is None else v
        if role == Qt.TextAlignmentRole:
            return {"c": Qt.AlignCenter, "l": Qt.AlignLeft | Qt.AlignVCenter, "r": Qt.AlignRight | Qt.AlignVCenter}[c.align]
        if role == Qt.ForegroundRole and self.row_color:
            col = self.row_color(row)
            return QColor(col) if col else None
        if role == Qt.ToolTipRole and c.tip:
            return c.tip(row)
        return None


class FilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.predicate: Callable[[dict], bool] | None = None
        self.setSortRole(Qt.UserRole)

    def filterAcceptsRow(self, row, parent):
        if self.predicate is None:
            return True
        return bool(self.predicate(self.sourceModel().rows[row]))

    def refilter(self):
        self.invalidateFilter()


def make_table(cols: list[Col], row_color=None, selectable: bool = True) -> tuple[QTableView, DictTableModel, FilterProxy]:
    model = DictTableModel(cols, row_color)
    proxy = FilterProxy()
    proxy.setSourceModel(model)
    view = QTableView()
    view.setModel(proxy)
    view.setSortingEnabled(True)
    view.sortByColumn(-1, Qt.AscendingOrder)           # natural order until the user clicks a header
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setSelectionMode(QAbstractItemView.SingleSelection if selectable else QAbstractItemView.NoSelection)
    view.setEditTriggers(QAbstractItemView.NoEditTriggers)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(26)
    hh = view.horizontalHeader()
    hh.setSectionResizeMode(QHeaderView.Interactive)
    hh.setStretchLastSection(True)
    hh.setDefaultSectionSize(96)
    view.setLayoutDirection(Qt.RightToLeft)
    return view, model, proxy


def selected_row(view: QTableView, proxy: FilterProxy, model: DictTableModel) -> dict | None:
    idx = view.selectionModel().selectedRows()
    if not idx:
        return None
    return model.rows[proxy.mapToSource(idx[0]).row()]


class Card(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        self.t = QLabel(title)
        self.t.setObjectName("cardTitle")
        self.v = QLabel("—")
        self.v.setObjectName("cardValue")
        self.s = QLabel("")
        self.s.setObjectName("muted")
        self.s.setWordWrap(True)
        for w in (self.t, self.v, self.s):
            lay.addWidget(w)

    def set(self, value: str, sub: str = "", color: str | None = None) -> None:
        self.v.setText(value)
        self.v.setStyleSheet(f"color:{color};" if color else "")
        self.s.setText(sub)


class Banner(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setVisible(False)

    def show_msg(self, text: str, bad: bool = False) -> None:
        self.setObjectName("bannerBad" if bad else "banner")
        self.style().unpolish(self)
        self.style().polish(self)
        self.setText(text)
        self.setVisible(bool(text))


def title(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("title")
    return lb


def muted(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("muted")
    lb.setWordWrap(True)
    return lb


def fmt_money(v) -> str:
    return "" if v is None else f"{v:,.2f}"


def fmt_pct(v, digits: int = 1) -> str:
    return "" if v is None else f"{v:+.{digits}%}"


def fmt_num(v, d: int = 2) -> str:
    return "" if v is None else f"{v:,.{d}f}"
