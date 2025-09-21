"""
Shared data structures and types used across the dogpile_breaker package.
This module contains classes that are imported by multiple modules to avoid circular imports.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ParamSpec, TypeAlias, TypeVar

from typing_extensions import Protocol, Self

from dogpile_breaker.exceptions import CantDeserializeError

if TYPE_CHECKING:
    from .monitoring import DogpileMetrics

# Type aliases for better code readability
ValuePayload: TypeAlias = Any
Serializer = Callable[[ValuePayload], bytes]
Deserializer = Callable[[bytes], ValuePayload]
JitterFunc: TypeAlias = Callable[[int], int]
AsyncFunc = TypeVar("AsyncFunc", bound=Callable[..., Awaitable[Any]])
P = ParamSpec("P")  # function parameters
R = TypeVar("R")  # function return value


class ShouldCacheFunc(Protocol):
    def __call__(self, source_args: tuple[Any], source_kwargs: dict[str, Any], result: Any) -> bool:
        """Receives source arguments and results and returns whether it should be cached."""


class KeyGeneratorFunc(Protocol):
    def __call__(self, fn: AsyncFunc, *args: Any, **kwargs: Any) -> str:
        """Receives function and its parameters and returns its key for caching."""


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
        # convert data to bytes so it can be stored in cache backend.
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
        except Exception as e:
            raise CantDeserializeError(e) from e
        else:
            return cls(payload=payload, **metadata)


class StorageBackend(Protocol):
    async def initialize(self, metrics: "DogpileMetrics", region_name: str) -> None:
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
