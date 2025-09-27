# Cache for Asyncio Applications with Anti-Dogpile Effect

---
**⚠️ Warning**

This project is experimental and should be used with caution.
I'm planning to use it in a few of my projects to gather feedback and improve it.
Expect potential API changes as the project evolves.
You can read the code (I included explanatory comments there).
I will try to improve the documentation and add tests in upcoming releases.
---

The dogpile effect occurs when a cache expires under heavy traffic. 
When this happens, all incoming requests hit the backend simultaneously, 
potentially overwhelming it.

For example, if 100 requests access a cached entry simultaneously, 
and the entry expires, all 100 requests will bypass the cache and hit the backend, 
causing a significant load spike.


This package addresses the dogpile effect using several strategies:

- **Exclusive Distributed Lock:** Ensures only one process regenerates data.
- **Extended Cache Time:** Keeps data longer than its expiration to serve stale data temporarily.
- **Jitter Application:** Distributes expiration times more evenly to prevent synchronized cache invalidation.
- **Thundering Herd Protection:** Groups simultaneous requests for the same key, loading data from the source only once.

Redis is used as the main cache storage due to its reliability and widespread use.

## Exclusive Distributed Lock

Before regenerating cached data, the process tries to acquire a Redis lock. 
This ensures that only one request regenerates data (even if the app runs on different hosts but uses the same cache), 
while others either wait (if no cached data exists) or use expired data. 
This approach reduces backend load and speeds up cache regeneration.

## Extend Cache Time

Imagine caching the result of `expensive_func()` for 1 minute. 

Internally, a `CachedEntry` structure is created with:

- **`payload:`** The cached data.
- **`expiration_timestamp:`** Current time + TTL (1 minute) + random jitter.

Redis' TTL is set higher than the actual expiration. 
This means that even after data expires, it remains temporarily available in Redis, 
allowing only one request to regenerate data while others use the outdated cache.

You can customize this behavior using `redis_expiration_func`. 
By default, Redis' TTL is set to twice the data TTL.

## Using Jitter to Spread Expiration Times

Without jitter, caching 100 different requests with a 1-minute TTL results in all entries expiring simultaneously. 
This triggers 100 backend requests at once.

To prevent this, random jitter is applied, which distributes expiration times more evenly, reducing backend load spikes.

By default, the `full_jitter` algorithm described in [this AWS article](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/) is used.  
You can provide your own `jitter_func` for customization.

## Thundering herd protection

This is a duplicate function call suppression mechanism.

When many calls occur simultaneously, concurrent requests for the same resource (cache key) are grouped into a single flight,
which loads data from the remote cache **or** the source only once.

- **Exclusive Distributed Lock** helps when identical requests hit different instances of your application  
  (e.g., your app is running on 4 hosts, and each of them receives the same request).  
- **Thundering Herd Protection** helps when many identical requests hit the same instance.  
  For example, if 100 requests for the same data hit one instance of your app, only 1 request calculates the data, 
  while the other 99 wait and reuse the first result.

This is why the total number of Redis `GET` commands decreases significantly under high concurrency.


## Solution Concept

The following logic defines how the cache operates:

1. **Request Data from Cache:**
   - **Data Exists:**
     - **Not Expired:** Return cached data.
     - **Expired:** Try to acquire a lock:
       - **Lock Acquired:** Regenerate data, update cache, release lock.
       - **Lock Not Acquired:** Return expired data.
   - **Data Missing:** Try to acquire a lock:
     - **Lock Acquired:** Regenerate data, update cache, release lock.
     - **Lock Not Acquired:** Wait.

## Handling Edge Cases

### What if the Process Fails Before Saving Data?

We use `lock_period_sec` to define the lock duration. 
If the generating process is killed, Redis automatically releases the lock after this period, 
allowing another process to regenerate the data.

### What is `should_cache_fn`?

`should_cache_fn` determines whether a function’s result should be cached. 
It receives the function’s arguments and result as inputs.

Be cautious when using this feature.  
Consider a scenario with 100 identical requests.  
Only one request runs `await generate_func(...)`, while others wait.  
If `should_cache_fn` returns `False`, the result won’t be cached, causing another waiting request to run the function.

In such cases, all 100 requests will be processed sequentially, 
resulting in poor response times but preventing backend overload.

Use `should_cache_fn` only for rare and specific scenarios.

## Quick Start

The configuration process consists of two stages:

**Initialization:**  
When a `CacheRegion` object is created using `CacheRegion()`,  
it becomes available for use, typically during module import.  
This allows functions to be decorated early in the application’s lifecycle.  
The only required components are a serializer and a deserializer for data.  
The deserializer should raise `CantDeserializeError` if an error occurs.

**Runtime Configuration:**  
Additional settings passed to `CacheRegion.configure()` are usually sourced from a configuration file.  
These settings are often available only at runtime, resulting in a two-step configuration process.  
(Additionally, it must be possible to execute `async def` functions inside `.configure()`.)

Here’s a quick example to get started:

```python
import asyncio
from datetime import datetime, timezone

from dogpile_breaker import CacheRegion, RedisStorageBackend


def my_serializer(data: str) -> bytes:
    return data.encode("utf-8")


def my_deserializer(data: bytes) -> str:
    return data.decode("utf-8")


cache_instance = CacheRegion(serializer=my_serializer, deserializer=my_deserializer)


async def expensive_func(sleep_for: int) -> str:
    print(f'{datetime.now(tz=timezone.utc)} | Running expensive_func')
    await asyncio.sleep(sleep_for)
    return f'This is result for parameter {sleep_for=}'


async def main() -> None:
    await cache_instance.configure(
        backend_class=RedisStorageBackend,
        backend_arguments={
            "host": 'localhost',
            "port": 6379,
            "db": 0,
        }
    )
    tasks = [
        cache_instance.get_or_create(
            key='expensive_func_1',
            ttl_sec=5,
            lock_period_sec=2,
            generate_func=expensive_func,
            generate_func_args=(),
            generate_func_kwargs={'sleep_for': 1},
        ) for _ in range(10)
    ]
    print(f'{datetime.now(tz=timezone.utc)} | Running 10 requests')
    many_requests = await asyncio.gather(*tasks)
    print(f'{datetime.now(tz=timezone.utc)} | {len(many_requests)}')


""" The output be like
2024-12-22 09:06:29.466529+00:00 | Running 10 requests
2024-12-22 09:06:29.479806+00:00 | Running expensive_func  (expensive func called only once while other requests are waiting)
2024-12-22 09:06:30.690893+00:00 | 10 (every request got cached result)
"""
```

You can also use a decorator 
```python

def cache_key_generator(fn: Callable[[int], Awaitable[str]], sleep_for: int) -> str:
    """
    This function are going to be called with the source function and all the args & kwargs of a call.
    It should return string used as a key in cache to store result.
    """
    return f"{fn.__name__}:{sleep_for}"

@cache_instance.cache_on_arguments(
    ttl_sec=5,
    lock_period_sec=2,
    function_key_generator=cache_key_generator,
)
async def expensive_func_v2(sleep_for: int) -> str:
    print(f'{datetime.now(tz=timezone.utc)} | Running expensive_func')
    await asyncio.sleep(sleep_for)
    return f'This is result for parameter {sleep_for=}'
```

After that you can call the decorated function

```python
tasks = [expensive_func_v2(sleep_for=1) for _ in range(10)]
results = await asyncio.gather(*tasks)
# expensive_func_v2 will run only once; the other 9 coroutines will get the result from the cache

# You can call the function directly (in case you don’t want to use the cache):
bypass_cache_result = await expensive_func_v2.call_without_cache(sleep_for=1)

# you can also save new result in cache
# by providing the new result and the arguments with which the function was called
await expensive_func_v2.save_to_cache('HAHA', sleep_for=10)
result = await expensive_func_v2(sleep_for=10)  # will return HAHA from cache
print(result)

```

## Backends

### Redis storage backends

There are two Redis backends `RedisStorageBackend` and `RedisSentinelBackend`  

#### RedisStorageBackend

Used to connect to a single Redis instance.

```python
from dogpile_breaker import RedisStorageBackend
from dogpile_breaker.backends.redis_backend import double_ttl

await cache_instance.configure(
    backend_class=RedisStorageBackend,
    backend_arguments={
        "host": 'localhost',
        "port": 6379,
        "db": 0,
        "username": "redis_user",
        "password": "secret",
        "max_connections": 200,
        "timeout": 10,
        "socket_timeout": 0.5,
        "redis_expiration_func": double_ttl,
    }
)

```

#### RedisSentinelBackend

Used to connect to a cluster of Redis instances using Sentinel


### In-memory backend - An in-memory Least Recently Used (LRU) cache backend with TTL support.

`MemoryBackendLRU` is a simple in-memory cache backend that maintains a bounded cache size
using an LRU (Least Recently Used) eviction policy and enforces per-item TTL.

It supports optional periodic cleanup of expired entries.

- **Parameters**  
  - `max_size: int` – Maximum number of items in the cache.  
  - `check_interval: float | None` – Interval in seconds between background cleanup runs; if None, expired entries are pruned only on access.

### Internal Behavior

- **LRU Eviction**: Uses an `OrderedDict` to track insertion/access order. When `max_size` is reached, the least recently used item is evicted.
- **TTL Enforcement**: Stores `(value, created_at_timestamp, ttl)` tuples. On `get` operation, expired entries are removed.
- **Periodic Cleanup**: If `check_interval` is set, an `asyncio.Task` runs `_periodic_cleanup`, removing expired entries at the interval.
- **Safety**: All cache operations are guarded by a single `asyncio.Lock`.

### CircuitBreakerFallbackBackend

`CircuitBreakerFallbackBackend` uses primary and fallback  storage backends 
to provide fault tolerance using the Circuit Breaker pattern.

On backend errors, it switches to a fallback storage, usually in-memory (by default `MemoryBackendLRU`).

It transitions between **CLOSED**, **OPEN**, and **HALF_OPEN** states to avoid overwhelming a failing backend.

- **Parameters**  
  - `primary_storage: StorageBackend` - Backend used when the circuit is closed (normal state). 
  - `fallback_storage: StorageBackend` – Backend used when the circuit is open (defaults to `MemoryBackendLRU()`).  
  - `failure_threshold: int` – Consecutive failure count to transition from CLOSED → OPEN.  
  - `cooldown_seconds: float` – Time to wait in OPEN before trying backend again (transition to HALF_OPEN).  
  - `success_threshold: int` – Consecutive success count in HALF_OPEN to transition back to CLOSED.

All storage methods (`get_serialized`, `set_serialized`, `delete`, `try_lock`, `unlock`) are overridden.
Calls are routed through the private `_call_with_circuit` helper

#### Usage example

```python
import asyncio
from dogpile_breaker import CacheRegion, RedisStorageBackend
from dogpile_breaker.backends.memory_backend import MemoryBackendLRU
from dogpile_breaker.backends.circut_breaker_fallback_backend import CircuitBreakerFallbackBackend

def my_serializer(data: str) -> bytes:
    return data.encode()

def my_deserializer(data: bytes) -> str:
    return data.decode()

async def main():
    region = CacheRegion(serializer=my_serializer, deserializer=my_deserializer, region_name='main_region', stats_enabled=False)
    primary = RedisStorageBackend(host='localhost', port=6379)
    fallback = MemoryBackendLRU(max_size=100)
    # Configure CacheRegion with Redis and the circuit breaker middleware
    await region.configure(
        backend_class=CircuitBreakerFallbackBackend,
        backend_arguments={
            'primary_storage': primary,
            'fallback_storage': fallback,
            'failure_threshold': 2,
            'cooldown_seconds': 30.0,
            'success_threshold': 1,
        },
    )

    # Use region.get_or_create or decorated functions as usual.
    value = await region.get_or_create(
        key='mykey',
        ttl_sec=10,
        lock_period_sec=5,
        generate_func=get_f,
        generate_func_args=(),
        generate_func_kwargs={},
    )

async def get_f():
    return 'data'

if __name__ == '__main__':

    asyncio.run(main())
```



### Prometheus metrics

While creating CacheRegion you can choose attribute `stats_enabled: bool`.

If enabled=True, initializes real Prometheus metrics.  Otherwise, assigns NoOpMetric instances which .



#### Metrics Exposed


**Cache effectiveness**

dogpile_cache_hits_total{region_name} – Successful cache hits.

dogpile_cache_misses_total{region_name} – Cache misses (regeneration needed).

dogpile_cache_stale_served_total{region_name} – Expired entries served.

dogpile_cache_herd_waited_total{region_name} – Requests delayed by herd protection.

**Regeneration**

dogpile_generation_calls_total{region_name, func_name} – Calls to regenerate data.

dogpile_generation_errors_total{region_name, func_name} – Failures during regeneration.

dogpile_generation_latency_seconds{region_name, func_name} – Histogram of regeneration latency.

**Backend storage**

dogpile_backend_op_latency_seconds{region_name, storage_name, operation} – Histogram of backend operation latency.

dogpile_backend_errors_total{region_name, storage_name, operation} – Backend operation failures.

**Circuit breaker**

dogpile_cb_state{region_name} – Current state (0=closed, 1=open, 2=half-open).

dogpile_cb_open_total{region_name} – Times circuit was opened.

dogpile_cb_close_total{region_name} – Times circuit was closed.

dogpile_cb_fallbacks_total{region_name} – Fallback invocations.

dogpile_cb_fallback_errors_total{region_name, error_type} – Errors during fallback.

Example dashboard - [grafana_dashboard_example.json](grafana_dashboard_example.json)

Go to Dashboards → Import. Paste JSON.  Select your Prometheus datasource. Modify as you wish.

## License

This project is licensed under the [MIT License](https://opensource.org/licenses/MIT).

## Acknowledgments

Thanks to the [Redis-py](https://github.com/redis/redis-py) 
and [Dogpile.cache](https://github.com/sqlalchemy/dogpile.cache) communities for their excellent libraries.

