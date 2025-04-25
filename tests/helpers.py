import random
from datetime import datetime, timedelta, timezone
from typing import Any

from dogpile_breaker.api import CachedEntry


def default_str_serializer(data: str) -> bytes:
    return data.encode("utf-8")


def default_str_deserializer(data: bytes) -> str:
    return data.decode("utf-8")


def gen_some_key() -> str:
    return f"some_key_{random.randint(1, 100000)}"


class CachedEntryFactory(CachedEntry):
    @classmethod
    def with_expired_timestamp(cls, data: Any) -> CachedEntry:
        return CachedEntry(
            payload=data, expiration_timestamp=(datetime.now(tz=timezone.utc) - timedelta(hours=1)).timestamp()
        )

    @classmethod
    def with_future_timestamp(cls, data: Any) -> CachedEntry:
        return CachedEntry(
            payload=data, expiration_timestamp=(datetime.now(tz=timezone.utc) + timedelta(hours=1)).timestamp()
        )
