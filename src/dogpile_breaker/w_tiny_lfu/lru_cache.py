from collections import OrderedDict
from typing import Any


class LRUCache:
    """
    A dictionary-like container that stores a given maximum items.
    If an additional item is added when the LRUCache is full, the least
    recently used key is discarded to make room for the new item.
    """

    def __init__(self, capacity: int = 128) -> None:
        super().__init__()
        self.capacity = capacity
        self.cache: OrderedDict[str, Any] = OrderedDict()

    def __contains__(self, item: str) -> bool:
        return item in self.cache

    def __len__(self) -> int:
        return len(self.cache)

    def get(self, key: str) -> Any | None:
        if key not in self.cache:
            return None
        # Move to end (most recently used)
        self.cache.move_to_end(key)
        return self.cache[key]

    def set(self, key: str, value: Any) -> tuple[Any, Any]:
        """Store a new views, potentially discarding an old value.
        Return evicted key and value pair.
        """

        evicted_key, evicted_value = None, None
        # If we already have this key, remove it first
        # after addition it will be moved to the end
        if key in self.cache:
            self.cache.pop(key)
        self.cache[key] = value
        # If we're at capacity, remove the least recently used item
        if len(self.cache) >= self.capacity:
            evicted_key, evicted_value = self.cache.popitem(last=False)
        return evicted_key, evicted_value

    def remove(self, key: str) -> None:
        self.cache.pop(key, None)

    def get_victim(self) -> str | None:
        """Get the last key in the cache. Cache is ordered following the LRU scheme."""
        if self.capacity <= len(self.cache):
            return next(iter(self.cache))
        return None
