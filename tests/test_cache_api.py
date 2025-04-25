import asyncio
import time
from typing import Any, Generic, Protocol, TypeVar

import pytest
import pytest_asyncio

from dogpile_breaker import CacheRegion
from dogpile_breaker.api import CachedEntry
from tests.helpers import CachedEntryFactory, default_str_deserializer, default_str_serializer, gen_some_key
from tests.in_memory_backend import InMemoryStorage

T = TypeVar("T")
CACHE_TTL = 10
LOCK_PERIOD = 1


class CacheRegionFixture(Protocol):
    async def __call__(self, cache_dict: dict[str, str | int | float | bytes] | None = None) -> CacheRegion: ...


@pytest_asyncio.fixture
async def preconfigured_cache() -> CacheRegionFixture:
    async def _configure(cache_dict: dict[str, str | int | float | bytes] | None = None) -> CacheRegion:
        cache = CacheRegion(
            serializer=default_str_serializer,
            deserializer=default_str_deserializer,
        )
        return await cache.configure(backend_class=InMemoryStorage, backend_arguments={"cache_dict": cache_dict or {}})

    return _configure


class ExpensiveFunction(Generic[T]):
    def __init__(self, response: T) -> None:
        self.call_count = 0
        self.response = response
        self.called_with_args: Any = None
        self.called_with_kwargs: Any = None

    async def __call__(self, *args: Any, **kwargs: Any) -> T:
        self.call_count += 1
        self.called_with_args = args
        self.called_with_kwargs = kwargs
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeShouldCacheFunc:
    def __init__(self, response: bool):  # noqa: FBT001
        self.call_count = 0
        self.response = response
        self.called_with_args: Any = None
        self.called_with_kwargs: Any = None

    def __call__(self, *args: Any, **kwargs: Any) -> bool:
        self.call_count += 1
        self.called_with_args = args
        self.called_with_kwargs = kwargs
        return self.response


@pytest.mark.asyncio
async def test_configure_call_creates_backend_instance() -> None:
    class FakeBackend:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs
            self.init_called = False

        async def initialize(self) -> None:
            self.init_called = True

    cache = CacheRegion(
        serializer=default_str_serializer,
        deserializer=default_str_deserializer,
    )
    backend_arguments = {
        "foo": "bar",
        "port": 999,
        "host": "localhost",
    }

    await cache.configure(
        backend_class=FakeBackend,  # type:ignore[arg-type]
        backend_arguments=backend_arguments,
    )

    assert isinstance(cache.backend_storage, FakeBackend)
    assert cache.backend_storage.kwargs == backend_arguments
    assert cache.backend_storage.init_called is True


@pytest.mark.parametrize(
    ("cache_data", "expected"),
    [
        (CachedEntryFactory.with_future_timestamp("cached value"), "cached value"),
        (CachedEntryFactory.with_expired_timestamp("old value"), "new shiny value"),
    ],
    ids=["non_expired", "expired"],
)
@pytest.mark.asyncio
async def test_cache_behavior_if_data_present_in_cache(
    preconfigured_cache: CacheRegionFixture,
    cache_data: CachedEntry,
    expected: str,
) -> None:
    cache = await preconfigured_cache({"test_key": cache_data.to_bytes(serializer=default_str_serializer)})
    gen_func = ExpensiveFunction("new shiny value")
    result = await cache.get_or_create(
        key="test_key",
        ttl_sec=CACHE_TTL,
        lock_period_sec=LOCK_PERIOD,
        generate_func=gen_func,
        generate_func_args=(),
        generate_func_kwargs={},
    )
    assert result == expected


@pytest.mark.asyncio
async def test_calls_gen_func_if_no_data_in_cache(preconfigured_cache: CacheRegionFixture) -> None:
    cache = await preconfigured_cache()
    gen_func = ExpensiveFunction(response="func_result")
    result = await cache.get_or_create(
        key="test_key",
        ttl_sec=CACHE_TTL,
        lock_period_sec=LOCK_PERIOD,
        generate_func=gen_func,
        generate_func_args=(1, 2, 3),
        generate_func_kwargs={"param": "value"},
    )

    assert result == "func_result"
    assert gen_func.call_count == 1
    assert gen_func.called_with_args == (1, 2, 3)
    assert gen_func.called_with_kwargs == {"param": "value"}


@pytest.mark.asyncio
async def test_saves_data_in_cache(preconfigured_cache: CacheRegionFixture) -> None:
    cache = await preconfigured_cache()
    cache_key = gen_some_key()
    before = await cache.backend_storage.get_serialized(cache_key)
    assert before is None

    await cache.get_or_create(
        key=cache_key,
        ttl_sec=CACHE_TTL,
        lock_period_sec=LOCK_PERIOD,
        generate_func=ExpensiveFunction(response="func_result"),
        generate_func_args=(),
        generate_func_kwargs={},
    )

    after = await cache.backend_storage.get_serialized(cache_key)
    assert after is not None
    assert b"func_result" in after


@pytest.mark.asyncio
async def test_update_cache_if_expired(preconfigured_cache: CacheRegionFixture) -> None:
    cache_key = gen_some_key()
    cached_entry = CachedEntryFactory.with_expired_timestamp(data="old expired value")
    cache = await preconfigured_cache(
        cache_dict={
            cache_key: cached_entry.to_bytes(serializer=default_str_serializer),
        }
    )
    gen_func = ExpensiveFunction(response="new shiny value")
    result = await cache.get_or_create(
        key=cache_key,
        ttl_sec=CACHE_TTL,
        lock_period_sec=LOCK_PERIOD,
        generate_func=gen_func,
        generate_func_args=(1, 2, 3),
        generate_func_kwargs={"param": "value"},
    )
    assert result == "new shiny value"
    assert gen_func.call_count == 1


@pytest.mark.asyncio
async def test_should_save_cache_fn_called_with_parameters_if_no_data_in_cache(
    preconfigured_cache: CacheRegionFixture,
) -> None:
    cache = await preconfigured_cache(None)
    should_cache_func = FakeShouldCacheFunc(response=False)
    cache_key = gen_some_key()
    assert (await cache.backend_storage.get_serialized(cache_key)) is None
    result = await cache.get_or_create(
        key=cache_key,
        ttl_sec=10,
        lock_period_sec=1,
        generate_func=ExpensiveFunction(response="new shiny value"),
        generate_func_args=(1, 2, 3),
        generate_func_kwargs={"param": "value"},
        should_cache_fn=should_cache_func,
    )
    assert result == "new shiny value"
    assert should_cache_func.call_count == 1
    assert should_cache_func.called_with_kwargs == {
        "source_args": (1, 2, 3),
        "source_kwargs": {"param": "value"},
        "result": "new shiny value",
    }


@pytest.mark.asyncio
async def test_should_save_cache_fn_called_with_parameters_if_data_in_cache_updated(
    preconfigured_cache: CacheRegionFixture,
) -> None:
    cache_key = gen_some_key()
    cached_entry = CachedEntryFactory.with_expired_timestamp(data="old expired value")
    cache = await preconfigured_cache(
        {
            cache_key: cached_entry.to_bytes(serializer=default_str_serializer),
        }
    )

    should_cache_func = FakeShouldCacheFunc(response=False)
    result = await cache.get_or_create(
        key=cache_key,
        ttl_sec=CACHE_TTL,
        lock_period_sec=LOCK_PERIOD,
        generate_func=ExpensiveFunction(response="new shiny value"),
        generate_func_args=(1, 2, 3),
        generate_func_kwargs={"param": "value"},
        should_cache_fn=should_cache_func,
    )
    assert result == "new shiny value"
    assert should_cache_func.call_count == 1
    assert should_cache_func.called_with_kwargs == {
        "source_args": (1, 2, 3),
        "source_kwargs": {"param": "value"},
        "result": "new shiny value",
    }


@pytest.mark.asyncio
async def test_do_not_save_in_cache_if_should_cache_fn_returns_false(preconfigured_cache: CacheRegionFixture) -> None:
    cache_key = gen_some_key()
    cache = await preconfigured_cache(None)
    assert (await cache.backend_storage.get_serialized(cache_key)) is None
    result = await cache.get_or_create(
        key=cache_key,
        ttl_sec=10,
        lock_period_sec=1,
        generate_func=ExpensiveFunction(response="new shiny value"),
        generate_func_args=(),
        generate_func_kwargs={},
        should_cache_fn=FakeShouldCacheFunc(response=False),
    )
    assert result == "new shiny value"
    assert (await cache.backend_storage.get_serialized(cache_key)) is None


@pytest.mark.asyncio
async def test_do_not_save_updated_value_in_cache_if_should_cache_fn_returns_false(
    preconfigured_cache: CacheRegionFixture,
) -> None:
    cache_key = gen_some_key()
    cached_entry = CachedEntryFactory.with_expired_timestamp(data="old expired value")
    cache = await preconfigured_cache(
        {
            cache_key: cached_entry.to_bytes(serializer=default_str_serializer),
        }
    )
    result = await cache.get_or_create(
        key=cache_key,
        ttl_sec=CACHE_TTL,
        lock_period_sec=LOCK_PERIOD,
        generate_func=ExpensiveFunction(response="new shiny value"),
        generate_func_args=(),
        generate_func_kwargs={},
        should_cache_fn=FakeShouldCacheFunc(response=False),
    )
    assert result == "new shiny value"
    in_cache = await cache._get_from_backend(cache_key)
    assert in_cache.payload == "old expired value"  # type:ignore[union-attr]  # previous value in cache


@pytest.mark.asyncio
async def test_serialize_and_deserialize_working_together(preconfigured_cache: CacheRegionFixture) -> None:
    cache = await preconfigured_cache(None)
    cache_key = gen_some_key()
    await cache._set_cached_value_to_backend(cache_key, "data str", CACHE_TTL, None)
    result = await cache._get_from_backend(cache_key)
    assert result.payload == "data str"  # type:ignore[union-attr]


@pytest.mark.asyncio
async def test_concurrent_requests_no_data_in_cache_gen_func_called_once(
    preconfigured_cache: CacheRegionFixture,
) -> None:
    cache = await preconfigured_cache(None)
    cache_key = gen_some_key()
    gen_func = ExpensiveFunction(response="str value")
    tasks = [
        cache.get_or_create(
            cache_key,
            ttl_sec=CACHE_TTL,
            lock_period_sec=LOCK_PERIOD,
            generate_func=gen_func,
            generate_func_args=(),
            generate_func_kwargs={},
        )
        for _ in range(2)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert results == ["str value", "str value"]
    assert gen_func.call_count == 1


@pytest.mark.asyncio
async def test_concurrent_requests_non_expired_data_in_cache(preconfigured_cache: CacheRegionFixture) -> None:
    cache_key = gen_some_key()
    cached_entry = CachedEntryFactory.with_future_timestamp(data="existing value")
    cache = await preconfigured_cache(
        {
            cache_key: cached_entry.to_bytes(serializer=default_str_serializer),
        }
    )
    gen_func = ExpensiveFunction(response="str value")
    tasks = [
        cache.get_or_create(
            cache_key,
            ttl_sec=CACHE_TTL,
            lock_period_sec=LOCK_PERIOD,
            generate_func=gen_func,
            generate_func_args=(),
            generate_func_kwargs={},
        )
        for _ in range(2)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert results == ["existing value", "existing value"]
    assert gen_func.call_count == 0


@pytest.mark.asyncio
async def test_concurrent_requests_expired_data_in_cache_gen_func_called_once(
    preconfigured_cache: CacheRegionFixture,
) -> None:
    cache_key = gen_some_key()
    cached_entry = CachedEntryFactory.with_expired_timestamp(data="existing value")
    cache = await preconfigured_cache({cache_key: cached_entry.to_bytes(serializer=default_str_serializer)})
    gen_func = ExpensiveFunction(response="new value")
    tasks = [
        cache.get_or_create(
            cache_key,
            ttl_sec=CACHE_TTL,
            lock_period_sec=LOCK_PERIOD,
            generate_func=gen_func,
            generate_func_args=(),
            generate_func_kwargs={},
        )
        for _ in range(2)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert results == ["new value", "new value"]
    assert gen_func.call_count == 1


@pytest.mark.asyncio
async def test_singleflight(preconfigured_cache: CacheRegionFixture) -> None:
    cache = await preconfigured_cache(None)
    cache_key = gen_some_key()
    gen_func = ExpensiveFunction(response="str value")
    tasks = [
        cache.get_or_create(
            cache_key,
            ttl_sec=CACHE_TTL,
            lock_period_sec=LOCK_PERIOD,
            generate_func=gen_func,
            generate_func_args=(),
            generate_func_kwargs={},
        )
        for _ in range(2)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert results == ["str value", "str value"]
    # because requests with same cache key are groupped together
    # only one of them is going to work with actual cache backend
    # and because of that we would have only 1 calls in this test
    assert cache.backend_storage._get_serialized_called_num == 1  # type:ignore[attr-defined]
    assert cache.backend_storage._set_serialized_called_num == 1  # type:ignore[attr-defined]
    assert cache.backend_storage._try_lock_called_num == 1  # type:ignore[attr-defined]


@pytest.mark.asyncio
async def test_return_slightly_expired_data_while_data_is_updating_by_another_process(
    preconfigured_cache: CacheRegionFixture,
) -> None:
    cache_key = gen_some_key()
    cached_entry = CachedEntryFactory.with_expired_timestamp(data="expired value")
    cache = await preconfigured_cache(
        {
            cache_key: cached_entry.to_bytes(serializer=default_str_serializer),
            f"{cache_key}_lock": 1,  # simulate that another process grabbed lock for this key and updating data
            f"{cache_key}_lock_expire": time.time()
            + 10,  # simulate that another process grabbed lock for this key and updating data
        }
    )
    gen_func = ExpensiveFunction(response="new value")
    result = await cache.get_or_create(
        key=cache_key,
        ttl_sec=CACHE_TTL,
        lock_period_sec=LOCK_PERIOD,
        generate_func=gen_func,
        generate_func_args=(),
        generate_func_kwargs={},
        should_cache_fn=FakeShouldCacheFunc(response=False),
    )
    assert result == "expired value"


@pytest.mark.asyncio
async def test_wait_for_lock_timeout_if_cache_has_no_data(
    preconfigured_cache: CacheRegionFixture,
) -> None:
    cache_key = gen_some_key()
    cache = await preconfigured_cache(
        {
            f"{cache_key}_lock": 1,  # simulate that another process grabbed lock for this key and updating data
            f"{cache_key}_lock_expire": time.time() + 1,
        }
    )
    gen_func = ExpensiveFunction(response="new value")
    await cache.get_or_create(
        key=cache_key,
        ttl_sec=CACHE_TTL,
        lock_period_sec=LOCK_PERIOD,
        generate_func=gen_func,
        generate_func_args=(),
        generate_func_kwargs={},
        should_cache_fn=FakeShouldCacheFunc(response=False),
    )
    # call _get_serialized - to check if data in cache (1 call)
    # call to try_lock (False, simulate that other process is updating data)
    # Then call every lock_period_sec / 4 sec to check if data has appeared (4 calls)
    # Total is going to be 5
    # After that the timeout event happens and lock's TTL is expired
    # new call to try_lock returns True
    assert cache.backend_storage._get_serialized_called_num == 5  # type:ignore[attr-defined]
    assert cache.backend_storage._try_lock_called_num == 2  # type:ignore[attr-defined]
