"""Local protected storage for Telegram credentials.

* Windows: values are encrypted with DPAPI (CryptProtectData, current-user scope).
* Other OS (dev/test only): stored base64 with file mode 0600 and flagged `protected=False`.
* Environment variables AISP_TELEGRAM_TOKEN / AISP_TELEGRAM_CHAT_ID take precedence.
Credentials are never written to config.yaml, logs, or source code.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

from . import paths


def _dpapi(data: bytes, encrypt: bool) -> bytes:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _protect(plain: str) -> tuple[str, bool]:
    raw = plain.encode("utf-8")
    if os.name == "nt":  # pragma: no cover
        return base64.b64encode(_dpapi(raw, True)).decode(), True
    return base64.b64encode(raw).decode(), False


def _unprotect(blob: str, protected: bool) -> str:
    raw = base64.b64decode(blob)
    if protected:  # pragma: no cover
        raw = _dpapi(raw, False)
    return raw.decode("utf-8")


@dataclass
class TelegramCreds:
    token: str = ""
    chat_id: str = ""
    protected: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)


def save_telegram(token: str, chat_id: str) -> None:
    t, prot = _protect(token.strip())
    c, _ = _protect(chat_id.strip())
    p = paths.secrets_path()
    p.write_text(json.dumps({"token": t, "chat_id": c, "protected": prot}), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def load_telegram() -> TelegramCreds:
    env_t, env_c = os.environ.get("AISP_TELEGRAM_TOKEN"), os.environ.get("AISP_TELEGRAM_CHAT_ID")
    if env_t and env_c:
        return TelegramCreds(env_t, env_c, protected=False)
    p = paths.secrets_path()
    if not p.exists():
        return TelegramCreds()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        prot = bool(d.get("protected", False))
        return TelegramCreds(_unprotect(d["token"], prot), _unprotect(d["chat_id"], prot), prot)
    except Exception:  # noqa: BLE001 - corrupt/other-user file: behave as "not configured"
        return TelegramCreds()


def clear_telegram() -> None:
    p = paths.secrets_path()
    if p.exists():
        p.unlink()


def mask(token: str) -> str:
    return "" if not token else token[:4] + "…" + token[-3:] if len(token) > 8 else "***"
