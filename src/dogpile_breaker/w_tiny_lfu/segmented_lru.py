from typing import Any

from dogpile_breaker.w_tiny_lfu.lru_cache import LRUCache


class SLRUCache:
    """
    An SLRU item has the following lifecycle:

    New item is inserted to probational segment.
    This item becomes the most recently used item in the probational segment.

    If the probational segment is full, the least recently used item is evicted from SLRU.
    If an item in the probational segment is accessed (with get or set),
    the item is migrated to the protected segment.
    This item becomes the most recently used item of the protected segment.

    If the protected segment is full,
    the least recently used item from the segment is moved to probational segment.
    This item becomes the most recently used item in the probational segment.
    If an item in the protected segment is accessed,
    it becomes the most recently used item of the protected segment.

    Segmented LRU (SLRU) is an advanced caching algorithm that improves upon
    classic Least Recently Used (LRU) by dividing the cache into segments:
    SLRU Design:

    Two segments:
        probation: New or demoted entries.
        protected: Frequently accessed (promoted) entries.
    Eviction occurs from probation only.
    Promoting items from probation → protected avoids thrashing.
    """

    def __init__(
        self,
        total_capacity: int = 256,
        protected_ratio: float = 0.8,
    ) -> None:
        self.total_capacity = total_capacity
        self.protected_cap = int(total_capacity * protected_ratio)
        self.probation_cap = total_capacity - self.protected_cap
        self.protected_cache = LRUCache(self.protected_cap)
        self.probation_cache = LRUCache(self.probation_cap)

    def __contains__(self, item: str) -> bool:
        return item in self.probation_cache or item in self.protected_cache

    def __len__(self) -> int:
        return len(self.probation_cache) + len(self.protected_cache)

    def set(self, key: str, value: Any) -> None:
        if key in self.protected_cache:
            self.protected_cache.set(key, value)
        elif key in self.probation_cache:
            # If a key in the probationary segment is accessed again,
            # it's considered valuable → it is promoted to the protected segment.
            self.probation_cache.remove(key)
            # If a key in the protected segment is evicted (due to reaching its capacity),
            # it is demoted back to the probationary segment,
            # rather than being dropped immediately.
            evicted_key, evicted_value = self.protected_cache.set(key, value)
            if evicted_key:
                self.probation_cache.set(evicted_key, evicted_value)
        else:
            # New keys go into the probationary segment.
            self.probation_cache.set(key, value)

    def get(self, key: str) -> Any:
        if key in self.protected_cache:
            return self.protected_cache.get(key)
        if key in self.probation_cache:
            # Any successful access (get or set)
            # to a key in the probationary segment implies
            # "this key is worth keeping longer" →
            # so you promote it into the protected segment.

            value = self.probation_cache.get(key)
            self.probation_cache.remove(key)
            # If a key in the protected segment is evicted (due to reaching its capacity),
            # it is demoted back to the probationary segment,
            # rather than being dropped immediately.
            evicted_key, evicted_value = self.protected_cache.set(key, value)
            if evicted_key:
                self.probation_cache.set(evicted_key, evicted_value)
            return value
        return None

    def remove(self, key: str) -> None:
        if key in self.protected_cache:
            self.protected_cache.remove(key)
        if key in self.probation_cache:
            self.probation_cache.remove(key)

    def get_victim(self) -> str | None:
        """Get the last key in the cache. Cache is ordered following the SLRU scheme."""
        if len(self) >= self.total_capacity:
            return self.probation_cache.get_victim()
        return None
