"""Background execution so the UI stays responsive during data updates and scans."""
from __future__ import annotations

import traceback
from typing import Callable

from PySide6.QtCore import QThread, Signal


class TaskWorker(QThread):
    progress = Signal(int, int, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable, label: str = "", parent=None):
        super().__init__(parent)
        self.fn, self.label = fn, label
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def is_cancelled(self) -> bool:
        return self._cancel

    def run(self) -> None:
        try:
            res = self.fn(lambda d, t, m: self.progress.emit(int(d), int(t), str(m)), self.is_cancelled)
            self.succeeded.emit(res)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
