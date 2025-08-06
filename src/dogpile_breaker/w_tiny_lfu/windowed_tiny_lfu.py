import math
from typing import Any

from dogpile_breaker.w_tiny_lfu.count_min_sketch import CountMinSketch
from dogpile_breaker.w_tiny_lfu.door_keeper import DoorKeeper
from dogpile_breaker.w_tiny_lfu.lru_cache import LRUCache
from dogpile_breaker.w_tiny_lfu.segmented_lru import SLRUCache


class TinyLFU:
    def __init__(
        self,
        size: int = 1_000_000,
        sample: int = 100_000,
        false_positive: float = 0.01,
    ) -> None:
        self.size = size

        # TinyLFU periodically clear the counters
        # in the CMS to age out old access patterns,
        # ensuring the cache adapts to changing workloads.
        self.sample = sample
        self.age = 0

        # To track access frequencies efficiently,
        # TinyLFU uses a Count-Min Sketch (CMS), a probabilistic data structure
        self.bouncer = CountMinSketch(size)
        # A Bloom Filter variant used to filter one-time accesses.
        # Instead of counting every new access, TinyLFU uses the Doorkeeper
        # to decide if the item should be counted at all.
        # If it's seen more than once, it enters CMS.
        self.doorkeeper = DoorKeeper(sample, false_positive)

        # Cache Structure
        # Window Cache (~1%): LRU cache for recent accesses; all new items go here.
        # Main Cache (~99%): SLRU with TinyLFU-based admission
        self.lru_percent_size = 1
        self.lru_size = math.ceil((self.lru_percent_size * size) / 100)
        self.lru_size = max(self.lru_size, 1)
        self.window_cache = LRUCache(self.lru_size)

        self.slru_size = math.ceil(size * ((100 - self.lru_percent_size) / 100))
        self.slru_size = max(self.slru_size, 1)
        self.main_cache = SLRUCache(total_capacity=self.slru_size)

    def __getitem__(self, key: str) -> Any | None:
        self.age += 1
        if self.age >= self.sample:
            self.bouncer.reset()
            self.doorkeeper.reset()
            self.age = 0
        self.bouncer.update(key)
        value = self.window_cache.get(key)
        if value:
            return value
        value = self.main_cache.get(key)
        if value:
            return value
        return None

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self.main_cache:
            self.main_cache.remove(key)
        # promote to window cache and
        # grab the value which was removed from window_cache to put our new key/value pair
        evicted_key, evicted_value = self.window_cache.set(key, value)
        if not evicted_key:
            return

        # If we return evicted key to main_cache - we can evict something from there
        # So we need to compare probabilities for both keys and save the better one
        # victim - is a last key in main_cache if it's full. It will be overriden
        # if we save our evicted_key.

        victim_key = self.main_cache.get_victim()
        if not victim_key:
            self.main_cache.set(evicted_key, evicted_value)
            return

        if not self.doorkeeper.allow(evicted_key):
            return

        victim_count = self.bouncer.estimate(victim_key)
        item_count = self.bouncer.estimate(evicted_key)
        if victim_count < item_count:
            self.main_cache.set(evicted_key, evicted_value)
        else:
            return

    def remove(self, key: str) -> None:
        self.window_cache.remove(key)
        self.main_cache.remove(key)

    def __contains__(self, key: str) -> bool:
        return key in self.window_cache or key in self.main_cache
