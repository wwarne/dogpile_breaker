__version__ = "0.2.0"

from .api import CacheRegion, ShouldCacheFunc, StorageBackend
from .redis_backend import RedisStorageBackend

__all__ = ["CacheRegion", "RedisStorageBackend", "ShouldCacheFunc", "StorageBackend"]
