import asyncio
import socket
import sys
from typing import Any, TypeAlias, TypeVar

from redis.asyncio import Redis, Sentinel, SentinelConnectionPool
from redis.asyncio.connection import AbstractConnection
from redis.exceptions import RedisError
from typing_extensions import override

from dogpile_breaker.exceptions import CacheBackendInteractionError

# the functionality is available in 3.11.x but has a major issue before
# 3.11.3. See https://github.com/redis/redis-py/issues/2633
if sys.version_info >= (3, 11, 3):
    from asyncio import timeout as async_timeout  # type: ignore[attr-defined,import-not-found,no-redef,unused-ignore]
else:
    from async_timeout import timeout as async_timeout  # type: ignore[import-not-found,no-redef,unused-ignore]

_ConnectionT = TypeVar("_ConnectionT", bound=AbstractConnection)
# To describe a function parameter that is unused and will work with anything.
Unused: TypeAlias = object


class AsyncRedisClient(Redis):
    # Note - mypy throws error - Call to untyped function "execute_command" in typed context
    @override
    async def execute_command(self, *args: Any, **options: Any) -> Any:
        try:
            return await super().execute_command(*args, **options)  # type: ignore[no-untyped-call]
        except (
            RedisError,
            socket.gaierror,
            OSError,
            asyncio.TimeoutError,
        ) as e:
            raise CacheBackendInteractionError from e


class SentinelBlockingPool(SentinelConnectionPool):
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
        super().__init__(service_name, sentinel_manager, **kwargs)  # type:ignore[no-untyped-call]
        self._condition = asyncio.Condition()

    async def get_connection(self, command_name: Any = ..., *keys: Any, **options: Any) -> _ConnectionT:  # noqa: ARG002
        """Gets a connection from the pool, blocking until one is available"""
        try:
            async with self._condition:  # noqa: SIM117
                async with async_timeout(self.timeout):
                    await self._condition.wait_for(self.can_get_connection)
                    connection = super().get_available_connection()  # type:ignore[no-untyped-call]
        except asyncio.TimeoutError as err:
            raise ConnectionError("No connection available.") from err  # noqa: EM101, TRY003

        # We now perform the connection check outside of the lock.
        try:
            await self.ensure_connection(connection)
        except BaseException:
            await self.release(connection)
            raise
        else:
            return connection  # type: ignore[no-any-return]

    async def release(self, connection: AbstractConnection) -> None:
        """Releases the connection back to the pool."""
        async with self._condition:
            await super().release(connection)
            self._condition.notify()
