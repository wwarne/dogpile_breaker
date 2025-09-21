import asyncio
import functools
import random
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from typing_extensions import Self

from .models import (
    CachedEntry,
    CachedFuncWithMethods,
    CachingDecorator,
    Deserializer,
    JitterFunc,
    KeyGeneratorFunc,
    P,
    R,
    Serializer,
    ShouldCacheFunc,
    StorageBackend,
    ValuePayload,
)
from .monitoring import DogpileMetrics, timer_ctx

if sys.version_info >= (3, 11, 3):
    from asyncio import timeout  # type: ignore[attr-defined,import-not-found,no-redef,unused-ignore]
else:
    from async_timeout import timeout  # type: ignore[import-not-found,no-redef,unused-ignore]


def full_jitter(value: int) -> int:
    """Jitter the value across the full range (0 to value).

    This corresponds to the "Full Jitter" algorithm specified in the
    AWS blog's post on the performance of various jitter algorithms.
    (https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)
    """
    return random.randint(0, value)  # noqa: S311


class CacheRegion:
    def __init__(
        self,
        serializer: Serializer,
        deserializer: Deserializer,
        region_name: str,
        stats_enabled: bool,  # noqa:FBT001
    ) -> None:
        self.serializer = serializer
        self.deserializer = deserializer
        self.region_name = region_name
        self.backend_storage: StorageBackend
        self.awaits: dict[str, asyncio.Future[Any]] = {}
        self.default_jitter_fn = full_jitter
        self.stats_enabled = stats_enabled
        self.metrics = DogpileMetrics(enabled=stats_enabled)

    async def configure(
        self,
        backend_class: type[StorageBackend],
        backend_arguments: dict[str, Any],
    ) -> Self:
        self.backend_storage = backend_class(**backend_arguments)
        await self.backend_storage.initialize(metrics=self.metrics, region_name=self.region_name)
        return self

    async def aclose(self) -> None:
        await self.backend_storage.aclose()

    async def get_or_create(
        self,
        key: str,
        ttl_sec: int,
        lock_period_sec: int,
        generate_func: Callable[P, Awaitable[R]],
        generate_func_args: tuple[Any, ...],
        generate_func_kwargs: dict[str, Any],  # P.kwargs,
        should_cache_fn: ShouldCacheFunc | None = None,
        jitter_func: JitterFunc | None = full_jitter,
    ) -> R:
        """This function will retrieve a value by key from the cache and return it.

        If the value is missing or its validity has expired,
        the value will be obtained as the result of awaiting the function `generate_func(*args, **kwargs)`.
        Only the coroutine that successfully acquires the lock in backend storage will recalculate the result.
        The others will simply wait for the result.
        If the result exists but is just outdated, while one coroutine is executing `generate_func`,
        the others will return the outdated result.

        :param key: the key under which the function's value is stored in the cache
        :param ttl_sec: the number of seconds for which the value is considered valid
        :param lock_period_sec: the duration for which to acquire the lock during value regeneration
        :param generate_func: the function that calculates the data
        :param should_cache_fn: a func that takes the original arguments and result, decides whether to cache the result
        :param generate_func_args: parameters to invoke the recalculation function
        :param generate_func_kwargs: parameters to invoke the recalculation function
        :param jitter_func: a function that randomly changes the ttl of a record to achieve more even distribution
        :return:
        """
        # use tmp cached awaitables to avoid thundering herd
        herd_leader = self.awaits.get(key, None)
        if herd_leader is None:
            # we use Future because you can `await` it multiple times
            # All calls to `get_or_create` with the same `key` would be groupped into one `singleflight`
            # only one request is actually going to be executed while others is going to wait for this Future() object
            herd_leader = asyncio.Future()
            self.awaits[key] = herd_leader

            try:
                value = await self._get_from_backend(key=key)
                cache_handler = self._non_existed_cache_handler if value is None else self._existed_cache_handler
                result = await cache_handler(
                    key=key,
                    ttl_sec=ttl_sec,
                    lock_period_sec=lock_period_sec,
                    data_from_cache=value,
                    generate_func=generate_func,
                    should_cache_fn=should_cache_fn,
                    generate_func_args=generate_func_args,
                    generate_func_kwargs=generate_func_kwargs,
                    jitter_func=jitter_func,
                )
                herd_leader.set_result(result)
            except Exception as e:
                herd_leader.set_exception(e)
                raise
            finally:
                self.awaits.pop(key, None)
        else:
            result = await herd_leader
        return result

    async def _existed_cache_handler(
        self,
        key: str,
        ttl_sec: int,
        lock_period_sec: int,
        data_from_cache: CachedEntry | None,  # None to make mypy happy because we always call this func with Data
        generate_func: Callable[P, Awaitable[R]],
        should_cache_fn: ShouldCacheFunc | None,
        generate_func_args: tuple[Any, ...],  # P.args,
        generate_func_kwargs: dict[str, Any],  # P.kwargs,
        jitter_func: JitterFunc | None,
    ) -> R:
        if data_from_cache is None:
            # This part won't execute because we only call this function after retrieving a value from the cache.
            # However, the cache_handler must have the same type signature to satisfy mypy.
            # Therefore, we handle the case when data_from_cache is None.
            # In this case, we simply call the function to handle the scenario when the cache is empty.
            return await self._non_existed_cache_handler(
                key=key,
                ttl_sec=ttl_sec,
                lock_period_sec=lock_period_sec,
                data_from_cache=data_from_cache,
                generate_func=generate_func,
                should_cache_fn=should_cache_fn,
                generate_func_args=generate_func_args,
                generate_func_kwargs=generate_func_kwargs,
                jitter_func=jitter_func,
            )

        # We reach this point when a CacheEntry is found in the cache.
        # We store the entry itself longer than its TTL so that during a high influx of requests,
        # we can serve slightly outdated data while one process updates the data,
        # rather than forcing everyone to wait.
        self.metrics.cache_hits.labels(region_name=self.region_name).inc()
        is_outdated = time.time() > data_from_cache.expiration_timestamp
        if not is_outdated:
            # Everything is great, the data is up-to-date, return it.
            return cast("R", data_from_cache.payload)
        # The data is outdated, it needs to be updated.
        # To ensure that only one process performs the update and hits the database,
        # we acquire a lock for data update
        # (since we are using Redis, this lock will be distributed across all requests
        # from all app instances/k8s pods/processes that fetch information about the specific entity).
        grabbed_lock = await self.backend_storage.try_lock(key, lock_period_sec)
        if grabbed_lock:
            # This process successfully acquired the lock, meaning it is responsible for updating the data.
            # If an error occurs here, the other processes will serve outdated data.
            # Then, after lock_period_sec expires, Redis will remove the lock,
            # and another request will attempt to update the data.
            # This will continue until the data is updated or until Redis remove the record,
            # and subsequent requests will follow the 'non_existed_cache_handler()' path.
            self.metrics.generation_calls.labels(region_name=self.region_name, func_name=generate_func.__name__).inc()
            with timer_ctx(
                self.metrics.generation_latency, {"region_name": self.region_name, "func_name": generate_func.__name__}
            ):
                try:
                    result = await generate_func(*generate_func_args, **generate_func_kwargs)
                except Exception:
                    self.metrics.generation_errors.labels(
                        region_name=self.region_name, func_name=generate_func.__name__
                    ).inc()
                    await self.backend_storage.unlock(key)
                    raise
            # If we have a should_cache_fn function,
            # we use it to check whether the result should be saved in the cache
            # (for example, we might not want to do this under certain conditions
            # or when specific parameters are present).

            if not should_cache_fn or should_cache_fn(
                source_args=generate_func_args, source_kwargs=generate_func_kwargs, result=result
            ):
                await self._set_cached_value_to_backend(
                    key=key,
                    value=result,
                    ttl_sec=ttl_sec,
                    jitter_func=jitter_func,
                )
            await self.backend_storage.unlock(key)
            return result
        # We couldn't acquire the lock, meaning another process is updating the data.
        # In the meantime, we return outdated data to avoid making the clients wait.
        self.metrics.cache_stale_served.labels(region_name=self.region_name).inc()
        return cast("R", data_from_cache.payload)

    async def _check_if_data_apper_in_cache(self, key: str, lock_period_sec: int) -> CachedEntry | None:
        """
        This is a coroutine which checks if the data is apper in the cache in case the process refreshing the data
        is not the current one (it could be on another machine for example).
        :param key: Key to check in cache
        :param lock_period_sec: Lock period for refreshing data. If data won't appear after this time
        :return:
        """
        try:
            async with timeout(lock_period_sec):
                while True:
                    data_from_cache = await self._get_from_backend(key=key)
                    if data_from_cache:
                        return data_from_cache
                    await asyncio.sleep(lock_period_sec / 4)
        except asyncio.TimeoutError:
            return None

    async def _non_existed_cache_handler(
        self,
        key: str,
        ttl_sec: int,
        lock_period_sec: int,
        data_from_cache: CachedEntry | None,
        generate_func: Callable[P, Awaitable[R]],
        should_cache_fn: ShouldCacheFunc | None,
        generate_func_args: tuple[Any, ...],  # P.args,
        generate_func_kwargs: dict[str, Any],  # P.kwargs,
        jitter_func: JitterFunc | None,
    ) -> R:
        self.metrics.cache_misses.labels(region_name=self.region_name).inc()
        # This is the case when there is nothing in the cache.
        # We need to update the data and store it there.
        # Only the process that can acquire the lock will do this,
        # while the others will wait to avoid overloading the system.
        while data_from_cache is None:
            grabbed_lock = await self.backend_storage.try_lock(key, lock_period_sec)
            if grabbed_lock:
                # The lock was successfully acquired, and this process is responsible for updating the data.
                with timer_ctx(
                    self.metrics.generation_latency,
                    {"region_name": self.region_name, "func_name": generate_func.__name__},
                ):
                    self.metrics.generation_calls.labels(
                        region_name=self.region_name, func_name=generate_func.__name__
                    ).inc()
                    try:
                        result = await generate_func(*generate_func_args, **generate_func_kwargs)
                    except Exception:
                        self.metrics.generation_errors.labels(
                            region_name=self.region_name, func_name=generate_func.__name__
                        ).inc()
                        await self.backend_storage.unlock(key)
                        raise
                # If we have a should_cache_fn function,
                # we use it to check whether the result should be saved in the cache
                # (for example, we might not want to do this under certain conditions
                # or when specific parameters are present).
                if not should_cache_fn or should_cache_fn(
                    source_args=generate_func_args, source_kwargs=generate_func_kwargs, result=result
                ):
                    await self._set_cached_value_to_backend(
                        key=key,
                        value=result,
                        ttl_sec=ttl_sec,
                        jitter_func=jitter_func,
                    )
                await self.backend_storage.unlock(key)
                return result
            # We wait timeout(lock_period_sec)
            # because if we just check whether anything has appeared in the cache,
            # we could end up in a situation where the process updating the cache has crashed.
            # In that case, we would simply end up in an infinite loop,
            # as nothing would appear in the cache.
            # By "waking up" after the lock expired we will
            # read the data from the cache (which will be None), exit this loop,
            # and enter a new iteration of the outer while loop,
            # where we will try to acquire the lock again, calculate, and write the data to the cache.
            self.metrics.cache_herd_waited.labels(region_name=self.region_name).inc()
            data_from_cache = await self._check_if_data_apper_in_cache(key, lock_period_sec)

        # Finally, some coroutine has updated the data (it could be this one, or a parallel one).
        return cast("R", data_from_cache.payload)

    async def _get_from_backend(
        self,
        key: str,
    ) -> CachedEntry | None:
        data = await self.backend_storage.get_serialized(key)
        return CachedEntry.from_bytes(
            data=data,
            deserializer=self.deserializer,
        )

    async def _set_cached_value_to_backend(
        self,
        key: str,
        value: ValuePayload,
        ttl_sec: int,
        jitter_func: JitterFunc | None,
    ) -> None:
        final_ttl = ttl_sec + jitter_func(ttl_sec) if jitter_func else ttl_sec
        new_cache_entry = CachedEntry(
            payload=value,
            expiration_timestamp=time.time() + final_ttl,
        )
        await self.backend_storage.set_serialized(
            key=key,
            value=new_cache_entry.to_bytes(serializer=self.serializer),
            ttl_sec=final_ttl,
        )

    def cache_on_arguments(
        self,
        ttl_sec: int,
        lock_period_sec: int,
        function_key_generator: KeyGeneratorFunc,
        should_cache_fn: ShouldCacheFunc | None = None,
        jitter_func: JitterFunc | None = full_jitter,
    ) -> CachingDecorator:
        """
        A function decorator that will cache the return
        value of the function using a key derived from the
        function itself and its arguments.

        The decorator internally makes use of the
        :meth:`.CacheRegion.get_or_create` method to access the
        cache and conditionally call the function.  See that
        method for additional behavioral details.

        The function is also given an attribute `call_without_cache` containing non-cached version of a function.
        So in case you want to call function directly

          await generate_something.call_without_cache(3,4)

         equivalent to calling ``generate_something(3, 4)`` without using cache at all.


        Another attribute ``save_to_cache()`` is added to provide extra caching
        possibilities relative to the function.   This is a convenience
        method which will store a given
        value directly without calling the decorated function.
        The value to be cached is passed as the first argument, and the
        arguments which would normally be passed to the function
        should follow::

            await generate_something.save_to_cache(3, 5, 6)

        The above example is equivalent to calling
        ``generate_something(5, 6)``, if the function were to produce
        the value ``3`` as the value to be cached.

        :param ttl_sec: the number of seconds for which the value is considered valid
        :param lock_period_sec: the duration for which to acquire the lock during value regeneration
        :param function_key_generator: function which receives the function itself and its parameters and should
        return string which is going to be used as caching key.
        :param should_cache_fn: function which receives the function itself, arguments this function was called with,
        and result. Should return boolean indicating whether this result should be cached.
        :param jitter_func: function to modify TTL of cached result for better dispersion of invalidation times.
        :return: CachingDecoratorWrapper: function that will cache the return value and has two additional
        methods attached to it:
         - save_to_cache(result_, *args, **kwargs) - accepts the same arguments,
         and could be used to save `result_` to the cache manually
         - call_without_cache(*args, **kwargs) - matches the type of original function, accepts the same argument,
        and could be used to call the function bypassing the cache completely.
        """

        def decorator(func: Callable[P, Awaitable[R]]) -> CachedFuncWithMethods[P, R]:
            async def save_to_cache(result_: R, *args: P.args, **kwargs: P.kwargs) -> None:
                key = function_key_generator(func, *args, **kwargs)
                await self._set_cached_value_to_backend(
                    key=key,
                    value=result_,
                    ttl_sec=ttl_sec,
                    jitter_func=jitter_func,
                )

            @functools.wraps(func)
            async def caching_dec_impl(*args: P.args, **kwargs: P.kwargs) -> R:
                key = function_key_generator(func, *args, **kwargs)
                return await self.get_or_create(
                    key=key,
                    ttl_sec=ttl_sec,
                    lock_period_sec=lock_period_sec,
                    generate_func=func,
                    generate_func_args=args,
                    generate_func_kwargs=kwargs,
                    should_cache_fn=should_cache_fn,
                )

            caching_dec_impl.call_without_cache = func  # type: ignore[attr-defined]
            caching_dec_impl.save_to_cache = save_to_cache  # type: ignore[attr-defined]
            return cast("CachedFuncWithMethods[P, R]", caching_dec_impl)

        return decorator

    async def direct_save_to_cache(self, key: str, value: ValuePayload, ttl_sec: int, jitter_func: JitterFunc) -> None:
        """
        Sometimes you want to override some cached value.
        While using @cache_on_arguments decorator provides a quick way to do it,
        sometimes you can't use decorator but still want to save something to cache.

        :param key: the key of the cached value
        :param value: the value to be cached
        :param ttl_sec: the number of seconds for which the value is considered valid
        :param jitter_func: a function that randomly changes the ttl of a record to achieve more even distribution.
        Note - you can use cache_region.default_jitter_fn attribute.
        """
        await self._set_cached_value_to_backend(
            key=key,
            value=value,
            ttl_sec=ttl_sec,
            jitter_func=jitter_func,
        )
