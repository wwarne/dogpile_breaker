import math
from collections.abc import Iterable

import mmh3
from bitarray import bitarray


def hash_for_bloom(item: str, optimal_k: int, optimal_m: int) -> Iterable[int]:
    for i in range(optimal_k):
        bit_index = mmh3.hash64(key=item, seed=i, signed=False)[0] % optimal_m
        yield bit_index


class BloomFilter:
    def __init__(
        self,
        expected_items: int | None = None,
        false_positive_rate: float | None = None,
    ) -> None:
        self.expected_items = expected_items
        self.false_positive_rate = false_positive_rate
        # Compute size of bit array and number of hash functions
        self.bitarray_size = self._optimal_bit_array_size(self.expected_items, self.false_positive_rate)
        self.hash_count = self._optimal_hash_count(self.bitarray_size, self.expected_items)
        self.bits = bitarray(self.bitarray_size)
        self.bits.setall(False)  # noqa: FBT003
        self.probe_function = hash_for_bloom

    def _optimal_bit_array_size(self, expected_items: int, false_positive_rate: float) -> int:
        return math.ceil((-expected_items * math.log(false_positive_rate)) / (math.log(2) ** 2))

    def _optimal_hash_count(self, bit_array_size: int, expected_items: int) -> int:
        return round((bit_array_size / expected_items) * math.log(2))

    def __contains__(self, key: str) -> bool:
        return all(self.bits[i] for i in self.probe_function(key, self.bitarray_size, self.hash_count))

    def insert(self, item: str) -> None:
        for pos in self.probe_function(item, self.bitarray_size, self.hash_count):
            self.bits[pos] = True

    def clear(self) -> None:
        self.bits = bitarray(self.bitarray_size)
        self.bits.setall(False)  # noqa:FBT003


class DoorKeeper:
    """Wrapper around the bloom filter"""

    def __init__(self, expected_items: int = 100000, false_positive_rate: float = 0.01) -> None:
        self.filter = BloomFilter(expected_items, false_positive_rate)

    def allow(self, key: str) -> bool:
        return self._insert(key)

    def _insert(self, key: str) -> bool:
        """Add the key and return True if the key was already present."""
        already_present = key in self.filter
        self.filter.insert(key)
        return already_present

    def reset(self) -> None:
        self.filter.clear()
