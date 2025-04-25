import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from dogpile_breaker.api import CachedEntry
from dogpile_breaker.exceptions import CantDeserializeError


# Example serializer and deserializer for testing
def example_serializer(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


def example_deserializer(data: bytes) -> Any:
    return json.loads(data.decode("utf-8"))


@pytest.fixture
def valid_payload() -> dict[str, str]:
    return {"key": "value", "timestamp": datetime.now(tz=timezone.utc).isoformat()}


@pytest.fixture
def expired_timestamp() -> float:
    return (datetime.now(tz=timezone.utc) - timedelta(hours=1)).timestamp()


@pytest.fixture
def future_timestamp() -> float:
    return (datetime.now(tz=timezone.utc) + timedelta(hours=1)).timestamp()


@pytest.fixture
def valid_cached_entry(valid_payload: dict[str, str], future_timestamp: float) -> CachedEntry:
    return CachedEntry(payload=valid_payload, expiration_timestamp=future_timestamp)


def test_to_bytes(valid_cached_entry: CachedEntry) -> None:
    serialized_data = valid_cached_entry.to_bytes(serializer=example_serializer)
    assert isinstance(serialized_data, bytes)
    assert b"key" in serialized_data
    assert b"expiration_timestamp" in serialized_data


def test_from_bytes(valid_cached_entry: CachedEntry, valid_payload: dict[str, str], future_timestamp: float) -> None:
    serialized_data = valid_cached_entry.to_bytes(serializer=example_serializer)
    deserialized_entry = CachedEntry.from_bytes(serialized_data, deserializer=example_deserializer)
    assert deserialized_entry is not None
    assert deserialized_entry.payload == valid_payload
    assert deserialized_entry.expiration_timestamp == future_timestamp


def test_from_bytes_invalid_payload() -> None:
    invalid_data = b'invalid|{"expiration_timestamp": 1738012598.794059}'
    with pytest.raises(CantDeserializeError):
        CachedEntry.from_bytes(invalid_data, deserializer=example_deserializer)


def test_to_bytes_empty_payload() -> None:
    empty_entry = CachedEntry(payload=None, expiration_timestamp=1234567890)
    serialized_data = empty_entry.to_bytes(serializer=example_serializer)
    assert isinstance(serialized_data, bytes)
    assert b"expiration_timestamp" in serialized_data
