"""Dark theme (QSS) + colour tokens. Colour is never the only carrier of meaning: statuses also use glyphs/text."""
from __future__ import annotations

import os

BG, PANEL, PANEL2, BORDER = "#12161c", "#1a2029", "#212a36", "#2e3948"
TEXT, MUTED = "#e6e9ee", "#93a0b2"
ACCENT, GOOD, WARN, BAD = "#4da3ff", "#3ecf8e", "#f5b942", "#ff6b6b"
SEL = "#25456b"
FONT_FAMILY = "Segoe UI" if os.name == "nt" else "DejaVu Sans"

QSS = f"""
* {{ font-family: "{FONT_FAMILY}", "Arial", "Tahoma"; font-size: 13px; color: {TEXT}; }}
QMainWindow, QWidget {{ background: {BG}; }}
QLabel {{ background: transparent; }}
QFrame#card, QGroupBox {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; }}
QGroupBox {{ margin-top: 14px; padding: 10px; font-weight: bold; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top right; padding: 0 8px; color: {ACCENT}; }}
QLabel#title {{ font-size: 20px; font-weight: bold; }}
QLabel#cardTitle {{ color: {MUTED}; font-size: 12px; }}
QLabel#cardValue {{ font-size: 22px; font-weight: bold; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#banner {{ background: #4a3a10; color: #ffd98a; border: 1px solid #8a6d1d; border-radius: 6px; padding: 6px 10px; font-weight: bold; }}
QLabel#bannerBad {{ background: #4a1c1c; color: #ffb3b3; border: 1px solid #a33; border-radius: 6px; padding: 6px 10px; font-weight: bold; }}
QListWidget#nav {{ background: {PANEL}; border: none; min-width: 190px; max-width: 190px; outline: 0; }}
QListWidget#nav::item {{ padding: 11px 14px; border-radius: 6px; margin: 2px 6px; }}
QListWidget#nav::item:selected {{ background: {SEL}; color: white; }}
QListWidget#nav::item:hover {{ background: {PANEL2}; }}
QTableView {{ background: {PANEL}; alternate-background-color: {PANEL2}; gridline-color: {BORDER}; border: 1px solid {BORDER};
  selection-background-color: {SEL}; selection-color: white; }}
QHeaderView::section {{ background: {PANEL2}; color: {MUTED}; padding: 5px; border: none; border-bottom: 1px solid {BORDER}; font-weight: bold; }}
QPushButton {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 14px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #5b6675; }}
QPushButton#primary {{ background: #1f5fa8; border-color: #2d78cc; font-weight: bold; }}
QPushButton#danger {{ background: #7a2323; border-color: #a33; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit, QTimeEdit {{ background: {PANEL2}; border: 1px solid {BORDER};
  border-radius: 5px; padding: 4px 6px; selection-background-color: {SEL}; }}
QComboBox QAbstractItemView {{ background: {PANEL2}; selection-background-color: {SEL}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; top: -1px; }}
QTabBar::tab {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 7px 14px; margin: 0 2px; border-top-left-radius: 6px; border-top-right-radius: 6px; }}
QTabBar::tab:selected {{ background: {SEL}; }}
QProgressBar {{ border: 1px solid {BORDER}; border-radius: 5px; text-align: center; background: {PANEL2}; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QStatusBar {{ background: {PANEL}; color: {MUTED}; }}
QScrollBar:vertical {{ background: {PANEL}; width: 12px; }} QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 24px; }}
QScrollBar:horizontal {{ background: {PANEL}; height: 12px; }} QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 5px; min-width: 24px; }}
QToolTip {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER}; }}
"""
