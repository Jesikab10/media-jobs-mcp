"""Token bucket used on both sides of the wire.

- Server side (jobs API): one bucket per client id; an empty bucket becomes a 429 with
  ``Retry-After`` so well-behaved clients know exactly how long to wait.
- Client side (MCP -> API): the same bucket throttles outgoing calls *before* they fail.
  Retrying reacts to a 429; throttling avoids it.

The clock is injectable so tests never sleep.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    rate_per_s: float
    capacity: int
    clock: Callable[[], float] = time.monotonic
    _tokens: float = field(init=False)
    _updated: float = field(init=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._tokens = float(self.capacity)
        self._updated = self.clock()

    def _refill(self) -> None:
        now = self.clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate_per_s)
        self._updated = now

    def try_acquire(self) -> float:
        """Take one token. Returns 0.0 on success, otherwise the seconds until one is available."""
        with self._lock:
            self._refill()
            if self._tokens >= 1:
                self._tokens -= 1
                return 0.0
            return (1 - self._tokens) / self.rate_per_s


def retry_after_header(wait_s: float) -> str:
    """`Retry-After` is whole seconds; round up so a client never retries too early."""
    return str(max(1, math.ceil(wait_s)))


class BucketRegistry:
    """One bucket per key (client id), created lazily, bounded in memory (LRU eviction).

    Keys come from authenticated callers, but a caller can still vary them; the bound keeps
    memory flat. An evicted key restarts with a full bucket, so the bound must stay well above
    the number of legitimate clients.
    """

    def __init__(
        self,
        rate_per_s: float,
        capacity: int,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ):
        self._rate = rate_per_s
        self._capacity = capacity
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(self._rate, self._capacity, self._clock)
                self._buckets[key] = bucket
                if len(self._buckets) > self._max_keys:
                    self._buckets.popitem(last=False)
            else:
                self._buckets.move_to_end(key)
            return bucket

    def __len__(self) -> int:
        return len(self._buckets)
