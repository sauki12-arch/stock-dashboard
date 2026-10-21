"""Token-bucket rate limiter and retry-with-backoff helper (thread-safe)."""
from __future__ import annotations

import random
import threading
import time
from typing import Callable, TypeVar

T = TypeVar("T")


class RateLimiter:
    def __init__(self, per_second: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if per_second <= 0:
            raise ValueError("per_second must be > 0")
        self.interval = 1.0 / per_second
        self._clock, self._sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a slot is available; returns seconds waited."""
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + self.interval
            wait = start - now
        if wait > 0:
            self._sleep(wait)
        return max(wait, 0.0)


def retry_with_backoff(fn: Callable[[], T], *, retries: int, base: float,
                       retry_on: tuple[type[BaseException], ...] = (Exception,),
                       sleep: Callable[[float], None] = time.sleep,
                       jitter: Callable[[], float] = random.random,
                       on_retry: Callable[[int, BaseException, float], None] | None = None) -> T:
    """Call fn(); on `retry_on` exceptions wait base*2^n (+ up to 25 % jitter) and retry."""
    attempt = 0
    while True:
        try:
            return fn()
        except retry_on as e:  # noqa: PERF203
            if attempt >= retries:
                raise
            delay = base * (2 ** attempt) * (1 + 0.25 * jitter())
            if on_retry:
                on_retry(attempt + 1, e, delay)
            sleep(delay)
            attempt += 1
