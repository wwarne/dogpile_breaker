import math
import time
from datetime import timedelta
from typing import Any

from dogpile_breaker.w_tiny_lfu.count_min_sketch import CountMinSketch
from dogpile_breaker.w_tiny_lfu.door_keeper import DoorKeeper
from dogpile_breaker.w_tiny_lfu.lru_cache import LRUCache
from dogpile_breaker.w_tiny_lfu.segmented_lru import SLRUCache


class WindowedTinyLFU:
    def __init__(
        self,
        size: int = 1_000_000,
        sample_size: int = 100_000,
        false_positive_rate: float = 0.01,
        window_size_percent: int = 1,
        protected_ratio: float = 0.8,
    ) -> None:
        self.size = size

        # TinyLFU periodically clear the counters
        # in the CMS to age out old access patterns,
        # ensuring the cache adapts to changing workloads.
        self.sample = sample_size
        self.age = 0

        # To track access frequencies efficiently,
        # TinyLFU uses a Count-Min Sketch (CMS), a probabilistic data structure
        # In Caffeine (Java), the default depth = 4 (~98% confidence).
        # In ScyllaDB, depth = 5 or 6 depending on workload sensitivity.
        # More depth - more hashes we need to count
        self.bouncer = CountMinSketch(width=size, depth=4)
        # A Bloom Filter variant used to filter one-time accesses.
        # Instead of counting every new access, TinyLFU uses the Doorkeeper
        # to decide if the item should be counted at all.
        # If it's seen more than once, it enters CMS.
        self.doorkeeper = DoorKeeper(sample_size, false_positive_rate)

        # Cache Structure
        # Window Cache (~1%): LRU cache for recent accesses; all new items go here.
        # Main Cache (~99%): SLRU with TinyLFU-based admission
        self.lru_percent_size = window_size_percent
        self.lru_size = math.ceil((self.lru_percent_size * size) / 100)
        self.lru_size = max(self.lru_size, 1)
        self.window_cache = LRUCache(self.lru_size)

        self.slru_size = math.ceil(size * ((100 - self.lru_percent_size) / 100))
        self.slru_size = max(self.slru_size, 1)
        self.main_cache = SLRUCache(total_capacity=self.slru_size, protected_ratio=protected_ratio)

    def get(self, key: str, default: Any = None) -> Any:
        value = self[key]
        return value if value is not None else default

    def set(self, key: str, value: Any) -> None:
        self[key] = value

    def __getitem__(self, key: str) -> Any:
        self.bouncer.update(key)
        value = self.window_cache.get(key)
        if value:
            return value
        value = self.main_cache.get(key)
        if value:
            return value
        return None

    def __setitem__(self, key: str, value: Any) -> None:
        # after a fixed number of insertions (sample size),
        # halve all counters to decay history over time,
        # as described in the TinyLFU paper
        self.age += 1
        if self.age >= self.sample:
            self.bouncer.halve_counters()
            self.doorkeeper.reset()
            self.age = 0
        # Step 1: Update frequency
        if self.doorkeeper.allow(key):
            self.bouncer.update(key)
        # Step 2: If key is already in main cache, refresh it
        if key in self.main_cache:
            self.main_cache.set(key, value)
            return

        # Step 3: Admit to window cache
        evicted_key, evicted_value = self.window_cache.set(key, value)
        if not evicted_key:
            return
        # Step 5: Evicted candidate from window — consider for main cache
        victim_key = self.main_cache.get_victim()
        if not victim_key:
            # SLRU has space — admit unconditionally
            self.main_cache.set(evicted_key, evicted_value)
            return
        # Step 6: Compare frequencies (TinyLFU admission)
        victim_count = self.bouncer.estimate(victim_key)
        evicted_item_count = self.bouncer.estimate(evicted_key)
        if victim_count < evicted_item_count:
            self.main_cache.set(evicted_key, evicted_value)
        else:
            return

    def remove(self, key: str) -> None:
        self.window_cache.remove(key)
        self.main_cache.remove(key)

    def __contains__(self, key: str) -> bool:
        return key in self.window_cache or key in self.main_cache

    def __len__(self) -> int:
        return len(self.window_cache) + len(self.main_cache)


class WindowedTinyLFUTTL:
    def __init__(
        self,
        size: int = 1_000_000,
        sample_size: int = 100_000,
        false_positive_rate: float = 0.01,
        window_size_percent: int = 1,
        protected_ratio: float = 0.8,
    ) -> None:
        self.size = size
        # TinyLFU periodically clear the counters
        # in the CMS to age out old access patterns,
        # ensuring the cache adapts to changing workloads.
        self.sample = sample_size
        self.age = 0

        # To track access frequencies efficiently,
        # TinyLFU uses a Count-Min Sketch (CMS), a probabilistic data structure
        # In Caffeine (Java), the default depth = 4 (~98% confidence).
        # In ScyllaDB, depth = 5 or 6 depending on workload sensitivity.
        # More depth - more hashes we need to count
        self.bouncer = CountMinSketch(width=size, depth=4)
        # A Bloom Filter variant used to filter one-time accesses.
        # Instead of counting every new access, TinyLFU uses the Doorkeeper
        # to decide if the item should be counted at all.
        # If it's seen more than once, it enters CMS.
        self.doorkeeper = DoorKeeper(sample_size, false_positive_rate)

        # Cache Structure
        # Window Cache (~1%): LRU cache for recent accesses; all new items go here.
        # Main Cache (~99%): SLRU with TinyLFU-based admission
        self.lru_percent_size = window_size_percent
        self.lru_size = math.ceil((self.lru_percent_size * size) / 100)
        self.lru_size = max(self.lru_size, 1)
        self.window_cache = LRUCache(self.lru_size)

        self.slru_size = math.ceil(size * ((100 - self.lru_percent_size) / 100))
        self.slru_size = max(self.slru_size, 1)
        self.main_cache = SLRUCache(total_capacity=self.slru_size, protected_ratio=protected_ratio)
        # store metadata: key = (created_at, ttl)
        self.store_meta: dict[str, float | None] = {}

    def get(self, key: str, default: Any = None) -> Any:
        value = self[key]
        return value if value is not None else default

    def __getitem__(self, key: str) -> Any:
        self.bouncer.update(key)
        expired_at = self.store_meta.get(key, None)
        if expired_at is not None and expired_at < time.monotonic():
            # expired - remove
            self.remove(key)
            return None

        value = self.window_cache.get(key)
        if value:
            return value
        value = self.main_cache.get(key)
        if value:
            return value
        return None

    def set(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        # after a fixed number of insertions (sample size),
        # halve all counters to decay history over time,
        # as described in the TinyLFU paper
        self.age += 1
        if self.age >= self.sample:
            self.bouncer.halve_counters()
            self.doorkeeper.reset()
            self.age = 0

        #  From the paper about TinyLFU
        #  Upon
        # item arrival, we first check if the item is contained in the Doorkeeper. If it is not contained in the
        # Doorkeeper (as is expected with first timers and tail items), the item is inserted to the Doorkeeper and
        # otherwise, it is inserted to the main structure. When querying items, we use both the Doorkeeper and the
        # main structures. That is, if the item is included in the Doorkeeper, TinyLFU estimates the frequency of
        # this item as its estimation in the main structure plus 1. Otherwise, TinyLFU returns just the estimation
        # from the main structure.
        # As I understood it - we need to update CountMinSketch probabilities only if item added for a second time
        # but we also need to cache this first-time items into our window_cache
        if self.doorkeeper.allow(key):
            # Step 1: Update frequency
            self.bouncer.update(key)
        # Step 2. Calculate and store TTL
        expire_at = time.monotonic() + ttl.total_seconds() if ttl is not None else None
        self.store_meta[key] = expire_at

        # Step 3: If key is already in main cache, refresh it
        if key in self.main_cache:
            self.main_cache.set(key, value)
            return

        # Step 4: Admit to window cache
        evicted_key, evicted_value = self.window_cache.set(key, value)
        if not evicted_key:
            return
        # Step 5: Check eviction
        expire_at = self.store_meta.get(evicted_key)
        if expire_at is not None and expire_at < time.monotonic():
            # evicted key is expired so no need to promote it back to main_cache
            return

        # Step 6: Evicted candidate from window — consider for main cache
        victim_key = self.main_cache.get_victim()
        if not victim_key:
            # SLRU has space — admit unconditionally
            self.main_cache.set(evicted_key, evicted_value)
            return

        # Step 7: Compare frequencies (TinyLFU admission)
        victim_count = self.bouncer.estimate(victim_key)
        evicted_item_count = self.bouncer.estimate(evicted_key)
        if victim_count < evicted_item_count:
            self.main_cache.set(evicted_key, evicted_value)
        else:
            return

    def remove(self, key: str) -> None:
        self.window_cache.remove(key)
        self.main_cache.remove(key)
        self.store_meta.pop(key, None)

    def __contains__(self, key: str) -> bool:
        return key in self.window_cache or key in self.main_cache

    def __len__(self) -> int:
        return len(self.window_cache) + len(self.main_cache)
