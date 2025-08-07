# http://dimacs.rutgers.edu/~graham/pubs/papers/cmsoft.pdf
# Count-Min Data Structure for approximation
# CountMin Sketch uses a matrix of counters and multiple hash functions.
# The addition of an entry increments a counter in each row and
# the frequency is estimated by taking the minimum value observed.
# This approach lets us tradeoff between space, efficiency,
# and the error rate due to collisions by adjusting the matrix's width and depth.
# (from https://highscalability.com/design-of-a-modern-cache/)
# https://dev.to/epam_india_python/efficient-frequency-estimation-with-count-min-sketch-a-memory-saving-approach-in-python-ih5#problems-in-cm-sketch

import array
from collections.abc import Iterator

import mmh3


def hash_mmh3(item: str, depth: int, width: int) -> Iterator[int]:
    """Simple MurMurHash hashing function that maps the result
    to the given depth and width.

    We need to cound hash multiple times (for each row in our 2D array)
    And then map hash to an index inside row.

    I could make it as few functions like this:
    def hash(item, depth) -> list[int]:
       ...
    and later use something like buckets = [i % width for i in hash(item, depth)]
    but why create unnecessary function calls
    (I doubt I would change this to another hash).
    """
    for i in range(depth):
        index = mmh3.hash64(key=item, signed=False, seed=i)[0] % width
        yield index


class CountMinSketch:
    def __init__(
        self,
        width: int,
        depth: int = 4,
    ) -> None:
        self.width = width
        self.depth = depth
        # Count-Min Sketch values are always non-negative integers
        # Overflow is unlikely - unsigned 32-bit int max (4.2 billion)
        # is sufficient for most use cases
        # especially that we are doing reset() after `sample` number of requests to cache
        # width = 10000, depth = 4 - size of list of lists of 0 - 320344
        # size of list of array.array is 160408
        self.table = [array.array("I", [0] * width) for _ in range(depth)]

    def update(self, item: str, count: int = 1) -> None:
        """Update the frequency of the item based on the given count."""
        for column_index, bucket_index in enumerate(hash_mmh3(item, depth=self.depth, width=self.width)):
            self.table[column_index][bucket_index] += count

    def estimate(self, item: str) -> int:
        """Estimate the frequency of the given item."""
        items = (
            self.table[column_index][bucket_index]
            for column_index, bucket_index in enumerate(hash_mmh3(item, depth=self.depth, width=self.width))
        )
        return min(items)

    def reset(self) -> None:
        """Reset the count to the starting state."""
        self.table = [array.array("I", [0] * self.width) for _ in range(self.depth)]
