"""DataEngine: orchestrates provider → validation → SQLite with failure isolation."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Callable

import pandas as pd

from ..config import DataCfg
from . import market_calendar as cal
from .provider import INTRADAY_MINUTES, DataProvider, ProviderError, ProviderUnavailable
from .ratelimit import RateLimiter, retry_with_backoff
from .store import Store
from .validation import DataQuality, drop_incomplete_bars, validate_daily

log = logging.getLogger(__name__)

UP_TO_DATE, UPDATED, FAILED, EMPTY, REFRESHED = "up_to_date", "updated", "failed", "empty", "full_refresh"


@dataclass
class UpdateReport:
    status: dict[str, str] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    rows: dict[str, int] = field(default_factory=dict)
    provider_down: bool = False
    cancelled: bool = False
    started: float = field(default_factory=time.time)
    finished: float = 0.0

    def count(self, st: str) -> int:
        return sum(1 for v in self.status.values() if v == st)

    @property
    def n_failed(self) -> int:
        return self.count(FAILED) + self.count(EMPTY)

    def summary(self) -> str:
        return (f"{len(self.status)} symbols: {self.count(UPDATED) + self.count(REFRESHED)} updated, "
                f"{self.count(UP_TO_DATE)} current, {self.n_failed} failed"
                + (", PROVIDER DOWN" if self.provider_down else "") + (", cancelled" if self.cancelled else ""))


class DataEngine:
    def __init__(self, provider: DataProvider, store: Store, cfg: DataCfg | None = None, *,
                 clock: Callable[[], datetime] = cal.now_utc, sleep: Callable[[float], None] = time.sleep):
        self.provider, self.store, self.cfg = provider, store, cfg or DataCfg()
        self.clock, self.sleep = clock, sleep
        self.limiter = RateLimiter(self.cfg.requests_per_second, sleep=sleep)
        self._lock = Lock()

    # ------------------------------------------------------------------
    def target_session(self):
        return cal.last_completed_session(self.clock(), self.cfg.grace_minutes_after_close)

    def _plan(self, symbols: list[str], force_full: bool):
        target = self.target_session()
        today = cal.to_ny(self.clock()).date()
        full_start = today - timedelta(days=self.cfg.history_days)
        plan: dict[str, tuple] = {}
        for s in symbols:
            last = self.store.last_bar_date(s)
            if force_full or last is None:
                plan[s] = (full_start, True)
            else:
                last_d = pd.Timestamp(last).date()
                if last_d >= target:
                    continue
                plan[s] = (max(full_start, last_d - timedelta(days=self.cfg.overlap_days)), False)
        return plan, target

    def _fetch_batch(self, batch: list[str], start, end):
        def call():
            self.limiter.acquire()
            return self.provider.fetch_daily(batch, start, end)
        return retry_with_backoff(call, retries=self.cfg.max_retries, base=self.cfg.backoff_base_s,
                                  retry_on=(ProviderUnavailable,), sleep=self.sleep,
                                  on_retry=lambda n, e, d: log.warning("retry %d in %.1fs: %s", n, d, e))

    # ------------------------------------------------------------------
    def update_daily(self, symbols: list[str], *, force_full: bool = False,
                     progress: Callable[[int, int, str], None] | None = None,
                     cancel: Callable[[], bool] | None = None) -> UpdateReport:
        rep = UpdateReport()
        symbols = list(dict.fromkeys(symbols))
        plan, target = self._plan(symbols, force_full)
        for s in symbols:
            if s not in plan:
                rep.status[s] = UP_TO_DATE
        total, done = len(symbols), len(symbols) - len(plan)
        if progress:
            progress(done, total, "plan")
        end = cal.to_ny(self.clock()).date()

        # group by start so one batch shares (roughly) the same window
        order = sorted(plan, key=lambda s: (plan[s][0], s))
        batches = [order[i:i + self.cfg.batch_size] for i in range(0, len(order), self.cfg.batch_size)]
        need_full: list[str] = []
        retry_single: list[str] = []
        consecutive_dead = 0

        def process(batch: list[str]):
            if cancel and cancel():
                return batch, None, "cancelled"
            start = min(plan[s][0] for s in batch)
            try:
                return batch, self._fetch_batch(batch, start, end), None
            except ProviderUnavailable as e:
                return batch, None, str(e)

        def ingest(sym: str, res, is_full: bool, start_hint) -> None:
            nonlocal done
            if isinstance(res, ProviderError):
                rep.status[sym], rep.messages[sym] = FAILED, res.message
                self.store.log_fetch(sym, "1d", "error", 0, res.message)
                if res.retryable:
                    retry_single.append(sym)
                return
            df = drop_incomplete_bars(res, self.clock(), self.cfg.grace_minutes_after_close)
            before = len(df)
            df = df.dropna(subset=["open", "high", "low", "close"])
            dropped = before - len(df)
            if df.empty:
                rep.status[sym], rep.messages[sym] = EMPTY, "no complete bars returned"
                self.store.log_fetch(sym, "1d", "empty", 0, "no complete bars returned")
                return
            if not is_full and self._revision_detected(sym, df):
                need_full.append(sym)
                return
            if is_full:
                self.store.delete_bars(sym)
            n = self.store.upsert_bars(sym, df)
            rep.status[sym] = REFRESHED if is_full and force_full else UPDATED
            rep.rows[sym] = n
            self.store.set_meta(sym, last_bar=df.index[-1].strftime("%Y-%m-%d"),
                                **({"last_full_refresh": self.clock().isoformat(timespec="seconds")} if is_full else {}))
            self.store.log_fetch(sym, "1d", "ok", n, f"dropped {dropped} incomplete rows" if dropped else "")

        def run_batches(blist: list[list[str]], full_flags: dict[str, bool]):
            nonlocal done, consecutive_dead
            with ThreadPoolExecutor(max_workers=self.cfg.max_workers) as ex:
                for batch, res, err in ex.map(process, blist):
                    if err == "cancelled":
                        rep.cancelled = True
                        continue
                    if res is None:
                        for s in batch:
                            rep.status[s], rep.messages[s] = FAILED, err or "provider error"
                            self.store.log_fetch(s, "1d", "error", 0, err or "")
                        consecutive_dead += 1
                    else:
                        consecutive_dead = 0
                        for s in batch:
                            r = res.get(s, ProviderError(s, "symbol missing from provider response"))
                            ingest(s, r, full_flags[s], None)
                    done += len(batch)
                    if progress:
                        progress(min(done, total), total, f"batch of {len(batch)}")

        run_batches(batches, {s: plan[s][1] for s in plan})
        if consecutive_dead and consecutive_dead >= len(batches):
            rep.provider_down = True

        # second pass 1: corporate-action revisions -> full history refresh
        if need_full and not rep.cancelled:
            full_start = end - timedelta(days=self.cfg.history_days)
            for s in need_full:
                plan[s] = (full_start, True)
            fb = [need_full[i:i + self.cfg.batch_size] for i in range(0, len(need_full), self.cfg.batch_size)]
            done -= len(need_full)
            run_batches(fb, {s: True for s in need_full})
            for s in need_full:
                if rep.status.get(s) == UPDATED:
                    rep.status[s] = REFRESHED
                    rep.messages[s] = "adjusted prices revised (split/dividend) – full history refreshed"

        # second pass 2: retry retryable per-symbol errors individually (bounded)
        singles = [s for s in dict.fromkeys(retry_single) if rep.status.get(s) == FAILED][:100]
        if singles and not rep.cancelled and not rep.provider_down:
            for s in singles:
                if cancel and cancel():
                    rep.cancelled = True
                    break
                try:
                    res = self._fetch_batch([s], plan[s][0], end).get(s)
                except ProviderUnavailable as e:
                    rep.messages[s] = str(e)
                    continue
                if res is not None and not isinstance(res, ProviderError):
                    rep.messages.pop(s, None)
                    ingest(s, res, plan[s][1], None)

        rep.finished = time.time()
        if progress:
            progress(total, total, "done")
        log.info("data update: %s", rep.summary())
        return rep

    def _revision_detected(self, sym: str, new: pd.DataFrame) -> bool:
        old = self.store.load_bars(sym, since=new.index[0].strftime("%Y-%m-%d"))
        common = old.index.intersection(new.index)
        if len(common) == 0:
            return False
        a, b = old.loc[common, "close"], new.loc[common, "close"]
        return bool(((a - b).abs() / a.abs()).max() > self.cfg.adjust_tolerance)

    # ------------------------------------------------------------------
    def load(self, symbol: str, min_bars: int = 1) -> tuple[pd.DataFrame, DataQuality]:
        df = self.store.load_bars(symbol)
        df = drop_incomplete_bars(df, self.clock(), self.cfg.grace_minutes_after_close)
        q = validate_daily(symbol, df, self.clock(), min_bars=min_bars,
                           max_stale_sessions=self.cfg.max_stale_sessions,
                           grace_minutes=self.cfg.grace_minutes_after_close)
        return df, q

    def update_intraday(self, symbol: str, interval: str = "5m", days: int = 5) -> pd.DataFrame | ProviderError:
        """Fetch + persist completed intraday bars (UTC timestamps). Not used for ranking."""
        if not self.provider.supports_intraday:
            return ProviderError(symbol, f"{self.provider.name} has no intraday support", retryable=False)

        def call():
            self.limiter.acquire()
            return self.provider.fetch_intraday(symbol, interval, days)
        try:
            res = retry_with_backoff(call, retries=self.cfg.max_retries, base=self.cfg.backoff_base_s,
                                     retry_on=(ProviderUnavailable,), sleep=self.sleep)
        except ProviderUnavailable as e:
            self.store.log_fetch(symbol, interval, "error", 0, str(e))
            return ProviderError(symbol, str(e))
        if isinstance(res, ProviderError):
            self.store.log_fetch(symbol, interval, "error", 0, res.message)
            return res
        now_naive = self.clock().astimezone(cal.UTC).replace(tzinfo=None)
        res = res[res.index + pd.Timedelta(minutes=INTRADAY_MINUTES[interval]) <= now_naive]  # completed bars only
        self.store.upsert_bars(symbol, res, interval)
        self.store.log_fetch(symbol, interval, "ok", len(res))
        return res
