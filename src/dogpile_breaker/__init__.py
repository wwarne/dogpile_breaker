from importlib.metadata import version

from .api import CacheRegion
from .backends.circuit_breaker_fallback_backend import CircuitBreakerFallbackBackend
from .backends.memory_backend import MemoryBackendLRU
from .backends.redis_backend import RedisSentinelBackend, RedisStorageBackend
from .models import ShouldCacheFunc, StorageBackend

__all__ = [
    "CacheRegion",
    "CircuitBreakerFallbackBackend",
    "MemoryBackendLRU",
    "RedisSentinelBackend",
    "RedisStorageBackend",
    "ShouldCacheFunc",
    "StorageBackend",
]
__version__ = version("dogpile_breaker")
