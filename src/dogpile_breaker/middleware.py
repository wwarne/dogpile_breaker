import typing

from typing_extensions import Self

if typing.TYPE_CHECKING:
    from .api import StorageBackend


class StorageBackendMiddleware:
    def wrap(self, backend_storage: "StorageBackend") -> Self:
        """Take a backend as an argument and setup the self.proxied property.
        Return an object that be used as a backend by a `CacheRegion` object.
        """
        self.proxied = backend_storage
        return self

    async def initialize(self) -> None:
        return await self.proxied.initialize()

    async def aclose(self) -> None:
        return await self.proxied.aclose()

    async def get_serialized(self, key: str) -> bytes | None:
        return await self.proxied.get_serialized(key)

    async def set_serialized(self, key: str, value: bytes, ttl_sec: int) -> None:
        return await self.proxied.set_serialized(key, value, ttl_sec)

    async def delete(self, key: str) -> None:
        return await self.proxied.delete(key)

    async def try_lock(self, key: str, lock_period_sec: int) -> bool:
        return await self.proxied.try_lock(key, lock_period_sec)

    async def unlock(self, key: str) -> None:
        return await self.proxied.unlock(key)
