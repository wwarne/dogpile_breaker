import asyncio
import time
import typing
from enum import Enum

from typing_extensions import assert_never

from dogpile_breaker.backends.memory_backend import MemoryBackendLRU
from dogpile_breaker.exceptions import CacheBackendInteractionError

if typing.TYPE_CHECKING:
    from dogpile_breaker.models import StorageBackend
    from dogpile_breaker.monitoring import DogpileMetrics

P = typing.ParamSpec("P")  # function parameters
R = typing.TypeVar("R")  # function return value


class CircuitState(Enum):
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failures threshold reached, using fallback only
    HALF_OPEN = "half_open"  # Testing if Redis is available again


class CircuitBreakerFallbackBackend:
    """
    Circuit Breaker + fallback backend (in-memory by default).

    - CLOSED: all ops go to default storage (usually Redis); Failures count up
    - OPEN: all ops go straight to fallback_storage until timeout
    - HALF OPEN: allow a few trial calls to default storage; If they succeed - CLOSE, on any fail - OPEN
    """

    def __init__(
        self,
        primary_storage: "StorageBackend",
        fallback_storage: typing.Optional["StorageBackend"] = None,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        success_threshold: int = 3,
    ) -> None:
        self.primary_storage = primary_storage
        self.fallback = fallback_storage or MemoryBackendLRU()
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.success_threshold = success_threshold
        # Circut state
        self.failure_count = 0
        self.success_count = 0
        self.state = CircuitState.CLOSED
        self.last_failure_time = 0.0
        # Lock to protect all state transitions
        self.lock = asyncio.Lock()
        self.metrics: DogpileMetrics
        self.region_name: str

    async def record_failure(self) -> None:
        async with self.lock:
            self.success_count = 0
            if self.state == CircuitState.CLOSED:
                self.failure_count += 1
                self.last_failure_time = time.time()
                if self.failure_count >= self.failure_threshold:
                    self.state = CircuitState.OPEN
                    self.metrics.cb_state.labels(region_name=self.region_name).set(1.0)
                    self.metrics.cb_open.labels(region_name=self.region_name).inc()
            elif self.state == CircuitState.HALF_OPEN:
                # In half-open state any failure sends us back to open
                self.state = CircuitState.OPEN
                self.metrics.cb_state.labels(region_name=self.region_name).set(1.0)
                self.metrics.cb_open.labels(region_name=self.region_name).inc()

                self.last_failure_time = time.time()

    async def record_success(self) -> None:
        async with self.lock:
            if self.state == CircuitState.CLOSED:
                self.failure_count = 0
            elif self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.success_threshold:
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    self.metrics.cb_state.labels(region_name=self.region_name).set(0)
                    self.metrics.cb_close.labels(region_name=self.region_name).inc()

    async def should_use_main(self) -> bool:
        async with self.lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                # Check if cooldown period has elapsed
                cooldown_elapsed = time.time() - self.last_failure_time > self.cooldown_seconds
                if cooldown_elapsed:
                    # Transition to half-open state to test backend
                    self.state = CircuitState.HALF_OPEN
                    self.metrics.cb_state.labels(region_name=self.region_name).set(2)
                    self.success_count = 0
                    return True
                return False
            if self.state == CircuitState.HALF_OPEN:
                return True
            assert_never(self.state)

    async def _call_with_circuit(
        self,
        func_original: typing.Callable[P, typing.Awaitable[R]],
        func_fallback: typing.Callable[P, typing.Awaitable[R]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        should_use_main = await self.should_use_main()
        if should_use_main:
            # CircutBreaker in CLOSED state - we should use main backend storage
            try:
                result = await func_original(*args, **kwargs)
                await self.record_success()
                return result  # noqa: TRY300
            except CacheBackendInteractionError:
                # If there is an error - record it and use fallback
                await self.record_failure()
                return await func_fallback(*args, **kwargs)
        else:
            # when Circut Breaker is in OPEN state - we should use fallback storage
            self.metrics.cb_fallbacks.labels(region_name=self.region_name).inc()
            return await func_fallback(*args, **kwargs)

    async def initialize(self, metrics: "DogpileMetrics", region_name: str) -> None:
        self.metrics = metrics
        self.region_name = region_name
        await self.primary_storage.initialize(metrics=self.metrics, region_name=region_name)
        await self.fallback.initialize(metrics=self.metrics, region_name=region_name)
        self.metrics.cb_state.labels(region_name=self.region_name).set(0)

    async def aclose(self) -> None:
        await self.primary_storage.aclose()
        await self.fallback.aclose()

    async def get_serialized(self, key: str) -> bytes | None:
        return await self._call_with_circuit(
            func_original=self.primary_storage.get_serialized,
            func_fallback=self.fallback.get_serialized,
            key=key,
        )

    async def set_serialized(self, key: str, value: bytes, ttl_sec: int) -> None:
        return await self._call_with_circuit(
            func_original=self.primary_storage.set_serialized,
            func_fallback=self.fallback.set_serialized,
            key=key,
            value=value,
            ttl_sec=ttl_sec,
        )

    async def delete(self, key: str) -> None:
        return await self._call_with_circuit(
            func_original=self.primary_storage.delete,
            func_fallback=self.fallback.delete,
            key=key,
        )

    async def try_lock(self, key: str, lock_period_sec: int) -> bool:
        return await self._call_with_circuit(
            func_original=self.primary_storage.try_lock,
            func_fallback=self.fallback.try_lock,
            key=key,
            lock_period_sec=lock_period_sec,
        )

    async def unlock(self, key: str) -> None:
        return await self._call_with_circuit(
            func_original=self.primary_storage.unlock,
            func_fallback=self.fallback.unlock,
            key=key,
        )
