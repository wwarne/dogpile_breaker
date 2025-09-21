import asyncio
import time
from collections import OrderedDict

from dogpile_breaker.models import CachedEntry, Deserializer, Serializer
from dogpile_breaker.monitoring import DogpileMetrics


class MemoryBackendLRU:
    """
    In-memory LRU caching backend with TTL.
    """

    def __init__(
        self,
        max_size: int = 1000,
        check_interval: float | None = 5.0,
    ) -> None:
        """
        :param max_size: maximum size of the LRU caching backend.
        :param check_interval: interval between checks (if None then delete expired items only during _get calls).
        """
        self._max_size = max_size
        self._cache: OrderedDict[str, tuple[bytes, float, float | int]] = OrderedDict()
        self._check_interval = check_interval
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self.region_name: str

    async def initialize(self, metrics: DogpileMetrics, region_name: str) -> None:  # noqa:ARG002
        if self._check_interval:
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        self.region_name = region_name

    async def aclose(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
        self._cache.clear()

    async def _periodic_cleanup(self) -> None:
        """Background task that periodically cleans up expired entries."""
        if not self._check_interval:
            return
        while True:
            await asyncio.sleep(self._check_interval)
            async with self._lock:
                current_time = time.monotonic()
                expired = [
                    key
                    for key, (data, created_at, ttl) in self._cache.items()
                    if current_time >= created_at + ttl  # ttl is always set in our version
                ]
                for key in expired:
                    del self._cache[key]

    async def get_serialized(self, key: str) -> bytes | None:
        async with self._lock:
            if key not in self._cache:
                return None
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            data, created_at, ttl = self._cache[key]
            if time.monotonic() > created_at + ttl:
                self._cache.pop(key, None)
                return None
            return data

    async def set_serialized(self, key: str, value: bytes, ttl_sec: int) -> None:
        async with self._lock:
            # If we already have this key, remove it first
            if key in self._cache:
                self._cache.pop(key)
            # If we're at capacity, remove the least recently used item
            if len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)
            self._cache[key] = (value, time.monotonic(), ttl_sec)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._cache.pop(key, None)

    async def try_lock(self, key: str, lock_period_sec: int) -> bool:  # noqa: ARG002
        """
        I decide to do this function as a no-op.
        We have signleflight in CacheRegion (see part where herd_leader = ...)
        So I didn't find any difference under load.
        Probably will retest it in the future.
        """
        return True

    async def unlock(self, key: str) -> None:  # noqa: ARG002
        return

    async def get_cached_entry(self, key: str, deserializer: Deserializer) -> CachedEntry | None:
        data = await self.get_serialized(key)
        return CachedEntry.from_bytes(
            data=data,
            deserializer=deserializer,
        )

    async def set_cached_entry(self, key: str, value: CachedEntry, serializer: Serializer, ttl_sec: int) -> None:
        await self.set_serialized(
            key=key,
            value=value.to_bytes(serializer=serializer),
            ttl_sec=ttl_sec,
        )
