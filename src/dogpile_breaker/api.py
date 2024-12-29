import asyncio
import functools
import json
import random
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ParamSpec, Protocol, TypeAlias, TypeVar, cast

from typing_extensions import Self

from .exceptions import CantDeserializeError

if sys.version_info >= (3, 11):
    from asyncio import timeout
else:
    from async_timeout import timeout

ValuePayload: TypeAlias = Any
Serializer = Callable[[ValuePayload], bytes]
Deserializer = Callable[[bytes], ValuePayload]
JitterFunc: TypeAlias = Callable[[int], int]
AsyncFunc = TypeVar("AsyncFunc", bound=Callable[..., Awaitable[Any]])
P = ParamSpec("P")  # function parameters
R = TypeVar("R")  # function return value


class ShouldCacheFunc(Protocol):
    def __call__(self, source_args: tuple[Any], source_kwargs: dict[str, Any], result: Any) -> bool:
        """Receives source arguments and results and returns whether it should cached."""


class KeyGeneratorFunc(Protocol):
    def __call__(self, fn: AsyncFunc, *args: Any, **kwargs: Any) -> str:
        """Receives function and its parameters and returns its key for caching."""


class StorageBackend(Protocol):
    async def initialize(self) -> None:
        """Some operation after creating the instance."""

    async def aclose(self) -> None:
        """Close the resources (connections, clients, etc.)"""

    async def get_serialized(self, key: str) -> bytes | None:
        """Reads cached data from storage."""

    async def set_serialized(self, key: str, value: bytes, ttl_sec: int) -> None:
        """Saves bytes into storage backend."""

    async def delete(self, key: str) -> None:
        """Deletes cached data from storage."""

    async def try_lock(self, key: str, lock_period_sec: int) -> bool:
        """Returns True if successfully acquired lock, False otherwise. Should not wait for lock."""

    async def unlock(self, key: str) -> None:
        """Releases lock."""


class CachedFuncWithMethods(Protocol[P, R]):
    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R: ...
    async def call_without_cache(self, *args: P.args, **kwargs: P.kwargs) -> R: ...
    async def save_to_cache(self, _result: R, *args: P.args, **kwargs: P.kwargs) -> None: ...


class CachingDecorator(Protocol):
    def __call__(self, func: Callable[P, Awaitable[R]]) -> CachedFuncWithMethods[P, R]: ...


@dataclass
class CachedEntry:
    # What we actually store in cache is this class
    payload: ValuePayload
    expiration_timestamp: int | float

    def to_bytes(self, serializer: Serializer) -> bytes:
        # convert data to bytes so it can be stored in redis.
        # metadata is serialized with standard `json` module
        # so the user only should write serializer and deserializer for its own data stored in `payload`
        main_data_bytes = serializer(self.payload)
        metadata_bytes = json.dumps({"expiration_timestamp": self.expiration_timestamp}, ensure_ascii=False).encode()
        return b"%b|%b" % (main_data_bytes, metadata_bytes)

    @classmethod
    def from_bytes(cls, data: bytes | None, deserializer: Deserializer) -> Self | None:
        if not data:
            return None
        bytes_payload, _, bytes_metadata = data.partition(b"|")
        metadata = json.loads(bytes_metadata)
        try:
            payload = deserializer(bytes_payload)
        except CantDeserializeError:
            return None
        else:
            return cls(payload=payload, **metadata)


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
        serializer: Callable[[ValuePayload], bytes],
        deserializer: Callable[[bytes], ValuePayload],
    ) -> None:
        self.serializer = serializer
        self.deserializer = deserializer
        self.backend_storage: StorageBackend
        self.events: dict[str, asyncio.Event] = {}

        self.check_storage_locks: dict[str, asyncio.Lock] = {}
        self.check_storage_tasks: dict[str, asyncio.Task] = {}
        # This text describes tricks I use to help the case then the cache is empty.
        # Imagine we have 1 process with 200 coroutines asking for get_or_create() wit the same parameters
        # As we don't have data in cache - only 1 coroutine is going to run regenerate_func and save data to the cache
        # while other 199 sleep a little and then check if data is available.
        # Therefore, they are spamming cache with requests.
        # I decided to use asyncio.Event to help reduce those requests.
        # Idea here is - I will create asyncio.Event and every coroutine waiting for the same data
        # is going to wait for it.
        # The one coroutine calculating the data are going to do `event.set()` then it's done.
        # After event.set all the waiting coroutines are woke up by eventloop so they can grab data
        # and return it to the user. So we will reduce number of requests hitting Redis server.

        # But another interesting case:
        # Imagine we have 2 processes, each of them have 200 coroutines, all requesting the same cache key.
        # While in process #1 everything is going to work as described before
        # inside process #2 all the 200 coroutine couldn't get the lock and run regenerate_func()
        # So they need a way of knowing then the cache is updated
        # To solve this I will run a `_check_if_data_apper_in_cache` coroutine. It will periodically check the cache
        # and if new data  available it will set `event.set()` allowing other coroutines to grab this data and return.

        # Using the _check_if_data_apper_in_cache helps reduce the number of requests to Redis server.
        # For example instead of 200 request every N seconds then it's only 3-4 requests from one coroutine
        # while other 200 just waiting.

        # Of course `_check_if_data_apper_in_cache` and waiting for an event should have timeout.
        # Process regenerating data in cache could be killed or crashed and we don't want end up in an infinite loop
        # That's why I use asyncio.timeout to limit the waiting time.

        # Another interesting case is then we have 200 coroutines waiting the same cache key.
        # One coroutine grabs the lock and calculated the data.
        # But after running function `should_cache_fn` we decided that we don't want to store result in cache.
        # Now we still have an empty cache and 199 requests.
        # In that case I use asyncio.Lock so

    async def configure(
        self,
        backend: type[StorageBackend],
        backend_arguments: dict[str, Any],
    ) -> Self:
        self.backend_storage = backend(**backend_arguments)
        await self.backend_storage.initialize()
        return self

    async def aclose(self) -> None:
        await self.backend_storage.aclose()

    async def get_or_create(
        self,
        key: str,
        ttl_sec: int,
        lock_period_sec: int,
        generate_func: Callable[P, Awaitable[R]],
        generate_func_args: P.args,
        generate_func_kwargs: P.kwargs,
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
        value = await self._get_from_backend(key=key)
        if value is None:
            if key not in self.events:
                self.events[key] = asyncio.Event()
                self.check_storage_locks[key] = asyncio.Lock()
            cache_handler = self._non_existed_cache_handler
        else:
            cache_handler = self._existed_cache_handler
        return await cache_handler(
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

    async def _existed_cache_handler(
        self,
        key: str,
        ttl_sec: int,
        lock_period_sec: int,
        data_from_cache: CachedEntry | None,  # None to make mypy happy because we always call this func with Data
        generate_func: Callable[P, Awaitable[R]],
        should_cache_fn: ShouldCacheFunc | None,
        generate_func_args: P.args,
        generate_func_kwargs: P.kwargs,
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
        is_outdated = time.time() > data_from_cache.expiration_timestamp
        if not is_outdated:
            # Everything is great, the data is up-to-date, return it.
            return data_from_cache.payload
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
            result = await generate_func(*generate_func_args, **generate_func_kwargs)
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
        return data_from_cache.payload

    async def _check_if_data_apper_in_cache(self, key: str, lock_period_sec: int) -> None:
        """
        This is a coroutine which checks if the data is apper in the cache in case the process refreshing the data
        is not the current one (it could be on another machine for example).
        Checking cache with only one coroutine helps reduce the load on redis server.
        (we are doing only one request each lock_period_sec / 4 instead of N requests from
        every requests waiting for a data.)
        :param key: Key to check in cache
        :param lock_period_sec: Lock period for refreshing data. If data won't appear after this time
        :return:
        """
        try:
            async with timeout(lock_period_sec):
                while True:
                    if key not in self.check_storage_locks:
                        return
                    async with self.check_storage_locks[key]:
                        data_from_cache = await self._get_from_backend(key=key)
                        if data_from_cache:
                            self.events[key].set()
                            del self.check_storage_tasks[key]
                            return
                        await asyncio.sleep(lock_period_sec / 4)
        except asyncio.TimeoutError:
            del self.check_storage_tasks[key]
            return

    async def _non_existed_cache_handler(
        self,
        key: str,
        ttl_sec: int,
        lock_period_sec: int,
        data_from_cache: CachedEntry | None,
        generate_func: Callable[P, Awaitable[R]],
        should_cache_fn: ShouldCacheFunc | None,
        generate_func_args: P.args,
        generate_func_kwargs: P.kwargs,
        jitter_func: JitterFunc | None,
    ) -> R:
        # This is the case when there is nothing in the cache.
        # We need to update the data and store it there.
        # Only the process that can acquire the lock will do this,
        # while the others will wait to avoid overloading the system.
        while data_from_cache is None:
            grabbed_lock = await self.backend_storage.try_lock(key, lock_period_sec)
            if grabbed_lock:
                # The lock was successfully acquired, and this process is responsible for updating the data.
                result = await generate_func(*generate_func_args, **generate_func_kwargs)
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

                # we calculated the new data so we need to wait coroutines waiting for that event
                if key in self.events:
                    self.events[key].set()

                # if we saved data in cache - we don't need this event and coroutine to check the cache
                # if we didn't save data in cache - all waiting coroutines
                # are going to run another cycle of while data_from_cache is None:
                # so they are going to call regenerate_func one after another, only 1 at the time
                # so they won't overload the backend
                self.events.pop(key, None)
                self.check_storage_locks.pop(key, None)
                check_task = self.check_storage_tasks.pop(key, None)
                if check_task:
                    check_task.cancel()
                return result

            # Waiting for another process to update the data in the cache.
            # We are periodically polling the cache in a loop.
            if key not in self.check_storage_tasks:
                # run a coroutine which is going to check the cache periodically
                self.check_storage_tasks[key] = asyncio.create_task(
                    self._check_if_data_apper_in_cache(key, lock_period_sec)
                )
            # We wait timeout(lock_period_sec)
            # because if we just check whether anything has appeared in the cache,
            # we could end up in a situation where the process updating the cache has crashed.
            # In that case, we would simply end up in an infinite loop,
            # as nothing would appear in the cache.
            # By "waking up" after the lock expired we will
            # read the data from the cache (which will be None), exit this loop,
            # and enter a new iteration of the outer while loop,
            # where we will try to acquire the lock again, calculate, and write the data to the cache.
            if key in self.events:
                # We will run  this codepath in case that `if grabbed_lock:` still running
                # so we have an event to wait and we just wait.
                await self._wait_for_data_saved_in_cache_event(key, lock_period_sec)
                data_from_cache = await self._get_from_backend(key=key)
            else:
                # We will run this code if our `if grabbed_lock` branch is done.
                # So it deleted Event and _check_if_data_apper_in_cache task.
                # And if the data is still not in cache it means that `should_cache_fn` function
                # decided that result is not suited to be in cache.
                await asyncio.sleep(lock_period_sec / 2)
                data_from_cache = None

        # Finally, some coroutine has updated the data (it could be this one, or a parallel one).
        return data_from_cache.payload

    async def _wait_for_data_saved_in_cache_event(self, key: str, lock_period_sec: int) -> None:
        try:
            async with timeout(lock_period_sec):
                if key in self.events:
                    await self.events[key].wait()
        except asyncio.TimeoutError:
            return

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

        The function is also given an attribute `call_without_cached` containing non-cached version of a function.
        So in case you want to call function directly

          await generate_something.call_without_cached(3,4)

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

            caching_dec_impl.call_without_cached = func  # type: ignore[attr-defined]
            caching_dec_impl.save_to_cache = save_to_cache  # type: ignore[attr-defined]
            return cast(CachedFuncWithMethods[P, R], caching_dec_impl)

        return cast(CachingDecorator, decorator)
