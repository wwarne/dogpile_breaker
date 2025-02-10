from importlib.util import find_spec

if not find_spec("prometheus_client"):
    err_msg = "Prometheus client is not installed. Install it with `pip install prometheus_client`"
    raise ImportError(err_msg)

from prometheus_client import Counter, Histogram

from .base_middleware import StorageBackendMiddleware

CACHE_HIT = Counter("cache_hit_total", "Total number of cache hits", ["region_name"])
CACHE_MISS = Counter("cache_miss_total", "Total number of cache misses", ["region_name"])
CACHE_ERROR = Counter("cache_error_total", "Total number of cache errors", ["region_name"])
REQUEST_LATENCY = Histogram("cache_latency_seconds", "Cache storage latency in seconds", ["region_name", "operation"])


class PrometheusMiddleware(StorageBackendMiddleware):
    def __init__(self, region_name: str) -> None:
        self.region_name = region_name
        self.read_latency = REQUEST_LATENCY.labels(region_name=region_name, operation="read")
        self.write_latency = REQUEST_LATENCY.labels(region_name=region_name, operation="write")
        self.cache_hit = CACHE_HIT.labels(region_name=region_name)
        self.cache_miss = CACHE_MISS.labels(region_name=region_name)
        self.cache_error = CACHE_ERROR.labels(region_name=region_name)

    async def get_serialized(self, key: str) -> bytes | None:
        with self.read_latency.time():
            try:
                result = await self.proxied.get_serialized(key)
                if result is None:
                    self.cache_miss.inc()
                else:
                    self.cache_hit.inc()
            except Exception:
                self.cache_error.inc()
                raise
            else:
                return result

    async def set_serialized(self, key: str, value: bytes, ttl_sec: int) -> None:
        with self.write_latency.time():
            try:
                return await self.proxied.set_serialized(key, value, ttl_sec)
            except Exception:
                self.cache_error.inc()
                raise
