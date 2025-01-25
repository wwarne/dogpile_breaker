import asyncio
import sys
from collections.abc import Callable
from typing import Any, TypeAlias, TypeVar

from redis.asyncio import BlockingConnectionPool as AsyncBlockingConnectionPool
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio import Sentinel, SentinelConnectionPool
from redis.asyncio.connection import AbstractConnection
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

# the functionality is available in 3.11.x but has a major issue before
# 3.11.3. See https://github.com/redis/redis-py/issues/2633
if sys.version_info >= (3, 11, 3):
    from asyncio import timeout as async_timeout  # type: ignore[attr-defined]
else:
    from async_timeout import timeout as async_timeout

T = TypeVar("T")
_ConnectionT = TypeVar("_ConnectionT", bound=AbstractConnection)
# To describe a function parameter that is unused and will work with anything.
Unused: TypeAlias = object


def double_ttl(value: int) -> int:
    return 2 * value


class RedisStorageBackend:
    def __init__(
        self,
        redis_expiration_func: Callable[[int], int] = double_ttl,
        **redis_kwargs: Any,
    ) -> None:
        # Without the pool every call to try_lock creates a connection to redis
        # so if we have 100 coroutines calling cached_function which is trying to get a lock
        # then 100 connections to the Redis server are open (they will be closed after some time but that spike
        # in connections could lead to some exceptions because number of connections has limit)
        # To solve this problem, you need to use existing connection or limited connections to do the operation.
        # Main idea is to use a connection pool which will keep a connection for some time
        # and handle several operations on that connection.

        # By default, there is a 2**31 max connection pool size.
        # Such a large number seems unreasonable and unsafe.
        # Seems it's all to easy to DoS a server with the current defaults
        # By default Redis instance can handle 10000 connections at a time which is far less than our default pool size.
        # the Java client has 8 connections by default.
        # Unofficial go client implementation has 10 connections per CPU by default.

        # When max_connections is set so something realistic like 50 or 100 it starts
        # throwing redis.exceptions.ConnectionError: Too many connections
        # under load.
        # BlockingConnectionPool class that works as expected.
        # For some reason it isn't mentioned in documentation and honestly it should be the default pool.
        redis_kwargs["decode_responses"] = False
        retry = redis_kwargs.pop("retry", Retry(ExponentialBackoff(), 3))
        # note - during development if you set Redis's address as 'localhost' - it connects to it
        # via IPv4 & IPv6 then producing two connection errors merged together into OSError
        # and retry_on_error does not work.
        retry_on_error = redis_kwargs.pop(
            "retry_on_error", [RedisConnectionError, RedisTimeoutError, ConnectionRefusedError]
        )
        self.pool = AsyncBlockingConnectionPool(
            retry=retry,
            retry_on_error=retry_on_error,
            **redis_kwargs,
        )
        self.redis = AsyncRedis(
            connection_pool=self.pool,
        )
        self.redis_expiration_func = redis_expiration_func

    async def initialize(self) -> None:
        await self.redis.initialize()

    async def aclose(self) -> None:
        # https://github.com/redis/redis-py/issues/2995#issuecomment-1764876240
        # by default closing redis won't close the connection pool and it could lead
        # to problems. So we need to make sure we close the connections.
        await self.redis.aclose()  # type: ignore[attr-defined]
        await self.pool.aclose()  # type: ignore[attr-defined]

    async def get_serialized(self, key: str) -> bytes | None:
        return await self.redis.get(key)

    async def set_serialized(self, key: str, value: bytes, ttl_sec: int) -> None:
        # We store the validity time of the data itself inside the `value`,
        # while setting a larger TTL for Redis so that we can provide clients
        # slightly expired data while new data is being generated.
        # It's not ideal to leave TTL completely unset,
        # because then the cache will keep growing until the memory runs out,
        # and you'll need to implement your own garbage collection algorithms.
        expiration_time = self.redis_expiration_func(ttl_sec)
        await self.redis.set(name=key, value=value, ex=expiration_time)

    async def try_lock(self, key: str, lock_period_sec: int) -> bool:
        # This lock is not very precise
        # We only save a `1` as value.
        # For example a client may acquire the lock,
        # get blocked performing some operation for longer than the lock validity time
        # (the time at which the key will expire),
        # and later remove the lock, that was already acquired by some other client.
        # Using just DEL is not safe as a client may remove another client's lock.
        # But for our purpose I think it's ok.
        # You can read about it and solution to it here:
        # https://redis.io/docs/latest/develop/use/patterns/distributed-locks/#safety-and-liveness-guarantees
        r = await self.redis.set(
            name=f"{key}_lock",
            value=1,
            nx=True,  # set the value only if it does not exist.
            ex=lock_period_sec,
        )
        return r is True

    async def unlock(self, key: str) -> None:
        await self.redis.delete(f"{key}_lock")

    async def delete(self, key: str) -> None:
        await self.redis.delete(key)


class RedisSentinelBlockingPool(SentinelConnectionPool):
    """
    It performs the same function as the default
    `redis.asyncio.SentinelConnectionPool` implementation, in that,
    it maintains a pool of reusable connections that can be shared by
    multiple async redis clients.

    The difference is that, in the event that a client tries to get a
    connection from the pool when all of connections are in use, rather than
    raising a `redis.ConnectionError` (as the default implementation does), it
    blocks the current `Task` for a specified number of seconds until
    a connection becomes available.

    Use ``max_connections`` to increase / decrease the pool size::

    Use ``timeout`` to tell it either how many seconds to wait for a connection
    to become available, or to block forever
    """

    def __init__(self, service_name: str, sentinel_manager: Sentinel, **kwargs: Any) -> None:
        self.timeout = kwargs.pop("timeout", 20)
        super().__init__(service_name, sentinel_manager, **kwargs)
        self._condition = asyncio.Condition()

    async def get_connection(self, command_name: Unused, *keys: Unused, **options: Unused) -> _ConnectionT:  # noqa: ARG002
        """Gets a connection from the pool, blocking until one is available"""
        try:
            async with self._condition:  # noqa: SIM117
                async with async_timeout(self.timeout):
                    await self._condition.wait_for(self.can_get_connection)  # type: ignore[attr-defined]
                    connection = super().get_available_connection()  # type: ignore[attr-defined,misc]
        except asyncio.TimeoutError as err:
            raise ConnectionError("No connection available.") from err  # noqa: EM101, TRY003

        # We now perform the connection check outside of the lock.
        try:
            await self.ensure_connection(connection)  # type: ignore[attr-defined]
        except BaseException:
            await self.release(connection)
            raise
        else:
            return connection

    async def release(self, connection: AbstractConnection) -> None:
        """Releases the connection back to the pool."""
        async with self._condition:
            await super().release(connection)
            self._condition.notify()


class RedisSentinelBackend(RedisStorageBackend):
    def __init__(
        self,
        sentinels: list[tuple[str, int]],
        master_name: str,
        redis_expiration_func: Callable[[int], int] = double_ttl,
        **redis_kwargs: Any,
    ) -> None:
        redis_kwargs["decode_responses"] = False
        retry = redis_kwargs.pop("retry", Retry(ExponentialBackoff(), 3))
        retry_on_error = redis_kwargs.pop(
            "retry_on_error", [RedisConnectionError, RedisTimeoutError, ConnectionRefusedError]
        )
        self.master_name = master_name
        self.sentinel = Sentinel(
            sentinels=sentinels,
            retry=retry,
            retry_on_error=retry_on_error,
            **redis_kwargs,
        )
        self.redis: AsyncRedis
        self.master_name = master_name
        self.redis_expiration_func = redis_expiration_func

    async def initialize(self) -> None:
        self.redis = self.sentinel.master_for(
            self.master_name,
            connection_pool_class=RedisSentinelBlockingPool,
        )  # makes a connection
        await self.redis.initialize()

    async def aclose(self) -> None:
        # then using sentinel.master_for
        # The Redis object "owns" the pool so it closes after closing Redis instance
        await self.redis.aclose()  # type: ignore[attr-defined]
