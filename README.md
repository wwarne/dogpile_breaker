# Cache for Asyncio Applications with Anti-Dogpile Effect

---
**⚠️ Warning**

This project is experimental and should be used with caution.
I'm planning to use it in few of my projects to get feedback and improve.
So expect potential API changes as the project evolves.
You can read the code (I put comments with explanation there).
I will try to improve docs and add tests in the next releases.
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
- **Thundering herd protection:** Simultaneously requests to same key are grouped and only load from source once.

Redis is used as the main cache storage due to its reliability and widespread use.

## Exclusive Distributed Lock

Before regenerating cached data, the process tries to acquire a Redis lock. 
This ensures that only one request regenerates data (even if we have our app on different hosts but using the same cache), 
while others either wait (if no cached data exists) or use expired data. 
This approach reduces backend load and speeds up cache regeneration.

## Extend Cache Time

Imagine caching the result of `expensive_func()` for 1 minute. 

Internally, a `CachedEntry` structure is created with:

- **`payload:`** The cached data.
- **`expiration_timestamp:`** Current time + TTL (1 minute) + random jitter.

Redis' TTL is set higher than the actual expiration. 
This means even after data expires, it remains temporarily available in Redis, 
allowing only one request to regenerate data while others use the outdated cache.

You can customize this behavior using `redis_expiration_func`. 
By default, Redis' TTL is set to twice the data TTL.

## Using Jitter to Spread Expiration Times

Without jitter, caching 100 different requests with a 1-minute TTL results in all entries expiring simultaneously. 
This triggers 100 backend requests at once.

To prevent this, we use random jitter, which levels expiration times, reducing backend load spikes.

By default, we use the `full_jitter` algorithm described in [this AWS article](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/).
You can provide your own `jitter_func` for customization.

## Thundering herd protection

It's a duplicate function call suppression mechanism.

Then we have a lot of calls  we group concurrent requests to same resource(cache key) into a singleflight,
which will load from remote cache OR data source only once.

Exclusive Distributed Lock helps then you have the same requests hitting different instances of your application
(for example your app is running on 4 hosts and each of them got  request for the same data)
Thundering herd protection helps then you have a lot of requests hitting the same instance. For example you
have 100 requests for the same data hitting 1 instance of your app. Only 1 request is going to calculate required data
while 99 are going to wait for it and use the result of the first one.

This is why you can find that number of total redis GET commands count is reduced under high concurrency


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

`should_cache_fn` determines whether a function's result should be cached. 
It receives the function's arguments and result as inputs.

Be cautious when using this feature. 
Consider a scenario with 100 identical requests. 
Only one request runs `await generate_func(...)`, while others wait. 
If `should_cache_fn` returns `False`, the result won't be cached, causing another waiting request to run the function.

In such cases, all 100 requests will be processed sequentially, 
resulting in poor response times but preventing backend overload.

Use `should_cache_fn` only for rare and specific scenarios.

## Quick Start

The configuration process consists of two stages:

Initialization:
When a CacheRegion object is created using CacheRegion(), 
it becomes available for use, typically during module import. 
This allows functions to be decorated early in the application's lifecycle.
The only thing we need to provide are serializer and deserializer for data.
Deserializer should raise CantDeserializeError if error occurs.

Runtime Configuration:
Additional settings passed to CacheRegion.configure() are usually sourced from a configuration file. 
These settings often available only at runtime, resulting in a two-step configuration process.
(also we need to be able to execute async def functions inside .configure())

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
        backend=RedisStorageBackend,
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

You can use a decorator 
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
# the expensive_func_v2 will run only once, another 9 coroutines will get result from cache

# you can call the func directly (in case you don't want to use cache)
bypass_cache_result = await expensive_func_v2.call_without_cache(sleep_for=1)
# you can also save new result in cache
# providing new result and arguments with which function was called
await expensive_func_v2.save_to_cache('HAHA', sleep_for=10)
result = await expensive_func_v2(sleep_for=10)  # will return HAHA from cache
print(result)

```

### Middlewares

The StorageBackendMiddleware is a class provided to easily augment existing 
backend behavior without having to extend the original class.

Middlewares are added to the CacheRegion object using the CacheRegion.configure() method. 
Only the overridden methods need to be specified and the real backend can be accessed 
with the `self.proxied` object from inside the `StorageBackendMiddleware`.

Classes that extend `StorageBackendMiddleware` can be stacked together.
The `.proxied` property will always point to either the concrete backend instance or the next middleware 
in the chain that a method can be delegated towards.

```python

from dogpile_breaker import StorageBackendMiddleware

import logging
log = logging.getLogger(__name__)

class LoggingMiddleware(StorageBackendMiddleware):
    async def set_serialized(self, key: str, value: bytes, ttl_sec: int) -> None:
        log.debug('Setting Cache Key: %s' % key)
        await self.proxied.set_serialized(key, value)
```

`StorageBackendMiddleware` can be be configured to optionally take arguments
In the example below, the `RetryDeleteMiddleware` class accepts a retry_count parameter on initialization. 
In the event of an exception on delete(), it will retry this many times before returning:

```python
from dogpile_breaker import StorageBackendMiddleware

class RetryDeleteMiddleware(StorageBackendMiddleware):
    def __init__(self, retry_count: int = 5) -> None:
        self.retry_count = retry_count

    async def delete(self, key: str) -> None:
        retries = self.retry_count
        while retries > 0:
            retries -= 1
            try:
                return await self.proxied.delete(key)
            except asyncio.CancelledError:
                # never consume CancelledError
                # https://superfastpython.com/asyncio-task-cancellation-best-practices/
                raise 
            except Exception:
                pass
```

The wrap parameter of the CacheRegion.configure() accepts a list which can contain any combination of instantiated proxy objects as well as uninstantiated proxy classes. Putting the two examples above together would look like this:

```python

from dogpile_breaker import CacheRegion, RedisStorageBackend

retry_middleware = RetryDeleteMiddleware(5)

region = CacheRegion(
        serializer=serialize_func,
        deserializer=deserialize_func,
    )
await region.configure(
        backend=RedisStorageBackend,
        backend_arguments={},
        middlewares=[LoggingMiddleware, retry_middleware]
    )
```

In the above example, the `LoggingMiddleware` object would be instantated by the `CacheRegion` 
and applied to wrap requests on behalf of the `retry_middleware` instance; 
that proxy in turn wraps requests on behalf of the original `RedisStorageBackend` backend.

So Chain of responsibility is going to be LoggingMiddleware -> retry_middleware -> RedisStorageBackend

## License

This project is licensed under the [MIT License](https://opensource.org/licenses/MIT).

## Acknowledgments

Thanks to the [Redis-py](https://github.com/redis/redis-py) 
and [Dogpile.cache](https://github.com/sqlalchemy/dogpile.cache) communities for their excellent libraries.

