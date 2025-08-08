__version__ = "0.14.0"

from importlib.util import find_spec

from .api import CacheRegion, ShouldCacheFunc, StorageBackend
from .backends.memory_backend import MemoryBackendLRU
from .backends.redis_backend import RedisSentinelBackend, RedisStorageBackend
from .middlewares.base_middleware import StorageBackendMiddleware
from .middlewares.circut_breaker_fallback_middleware import CircuitBreakerFallbackMiddleware

__all__ = [
    "CacheRegion",
    "CircuitBreakerFallbackMiddleware",
    "MemoryBackendLRU",
    "RedisSentinelBackend",
    "RedisStorageBackend",
    "ShouldCacheFunc",
    "StorageBackend",
    "StorageBackendMiddleware",
]

if find_spec("prometheus_client"):
    from .middlewares.prometheus_middleware import PrometheusMiddleware

    __all__ += ["PrometheusMiddleware"]
