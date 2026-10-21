"""Telegram Bot API client (sendMessage). Credentials come from `secrets`; the token is redacted from any error text."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

MAX_LEN = 4096
API = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class SendResult:
    ok: bool
    status: int = 0
    error: str = ""
    retry_after: int = 0


class TelegramClient:
    def __init__(self, token: str, chat_id: str, session=None, timeout: float = 10.0,
                 sleep: Callable[[float], None] = time.sleep, max_retry_wait: int = 30):
        if not token or not chat_id:
            raise ValueError("token and chat_id are required")
        self.token, self.chat_id, self.timeout, self.sleep = token, str(chat_id), timeout, sleep
        self.max_retry_wait = max_retry_wait
        if session is None:
            import requests
            session = requests.Session()
        self.session = session

    def _redact(self, text: str) -> str:
        return text.replace(self.token, "***")

    def _post(self, method: str, payload: dict) -> SendResult:
        try:
            r = self.session.post(API.format(token=self.token, method=method), json=payload, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001 - network failures must never crash the app
            return SendResult(False, 0, self._redact(f"{type(e).__name__}: {e}"))
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {}
        if r.status_code == 200 and data.get("ok"):
            return SendResult(True, 200)
        ra = int((data.get("parameters") or {}).get("retry_after", 0) or 0)
        return SendResult(False, r.status_code, self._redact(str(data.get("description") or f"HTTP {r.status_code}")), ra)

    def send(self, text: str) -> SendResult:
        text = text if len(text) <= MAX_LEN else text[: MAX_LEN - 1] + "…"
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        res = self._post("sendMessage", payload)
        if not res.ok and res.status == 429 and 0 < res.retry_after <= self.max_retry_wait:
            self.sleep(res.retry_after)
            res = self._post("sendMessage", payload)
        return res

    def test(self) -> SendResult:
        return self.send("✅ AI Stock Scanner Pro – הודעת בדיקה. החיבור ל-Telegram תקין.")
