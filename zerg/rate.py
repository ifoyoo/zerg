"""Shared asynchronous request rate limiting."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class RateLimiter:
    """Token-bucket limiter shared by all workers of one spider."""

    def __init__(
        self,
        rate: float | None,
        burst: int = 1,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.rate = float(rate) if rate is not None else 0.0
        self.burst = max(1, int(burst))
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(self.burst)
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.rate <= 0:
            return
        while True:
            async with self._lock:
                now = self._clock()
                elapsed = max(0.0, now - self._updated)
                self._tokens = min(
                    float(self.burst), self._tokens + elapsed * self.rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            await self._sleep(wait)
