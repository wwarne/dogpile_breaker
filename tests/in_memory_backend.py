import asyncio
import time
from typing import Any


class InMemoryStorage:
    """
    Simple dictionary-based backend.
    Used for testing.

    Using await asyncio.sleep(0) to allow a coroutine switch
    sleep() always suspends the current task, allowing other tasks to run
    (in real-life cache backends we are waiting for server response and switch to another coroutine)
    """

    def __init__(self, **kwargs: Any) -> None:
        self._cache = kwargs.pop("cache_dict", {})
        self._get_serialized_called_num = 0
        self._set_serialized_called_num = 0
        self._try_lock_called_num = 0

    async def initialize(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def get_serialized(self, key: str) -> bytes | None:
        self._get_serialized_called_num += 1
        await asyncio.sleep(0)
        return self._cache.get(key, None)  # type:ignore[no-any-return]

    async def set_serialized(self, key: str, value: bytes, ttl_sec: int) -> None:  # noqa: ARG002
        self._set_serialized_called_num += 1
        await asyncio.sleep(0)
        self._cache[key] = value

    async def delete(self, key: str) -> None:
        self._cache.pop(key, None)

    async def try_lock(self, key: str, lock_period_sec: int) -> bool:
        self._try_lock_called_num += 1
        await asyncio.sleep(0)
        if f"{key}_lock" in self._cache and self._cache.get(f"{key}_lock_expire", 0) > time.time():
            return False
        self._cache[f"{key}_lock"] = b"1"
        self._cache[f"{key}_lock_expire"] = time.time() + lock_period_sec
        return True

    async def unlock(self, key: str) -> None:
        self._cache.pop(f"{key}_lock", None)
