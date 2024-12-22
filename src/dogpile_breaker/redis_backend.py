from collections.abc import Callable

from redis.asyncio import BlockingConnectionPool as AsyncBlockingConnectionPool
from redis.asyncio import Redis as AsyncRedis


def double_ttl(value: int) -> int:
    return 2 * value


class RedisStorageBackend:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        username: str | None = None,
        password: str | None = None,
        db: int = 0,
        max_connections: int = 10,
        socket_timeout: float = 0.5,
        redis_expiration_func: Callable[[int], int] = double_ttl,
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

        self.pool = AsyncBlockingConnectionPool(
            max_connections=max_connections,
            host=host,
            port=port,
            db=db,
            username=username,
            password=password,
            socket_timeout=socket_timeout,
        )
        self.redis = AsyncRedis(
            connection_pool=self.pool,
            decode_responses=False,
        )
        self.redis_expiration_func = redis_expiration_func

    async def initialize(self) -> None:
        await self.redis.initialize()

    async def aclose(self) -> None:
        # https://github.com/redis/redis-py/issues/2995#issuecomment-1764876240
        # by default closing redis won't close the connection pool and it could lead
        # to problems. So we need to make sure we close the connections.
        await self.redis.aclose()
        await self.pool.aclose()

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
