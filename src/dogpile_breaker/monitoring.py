import contextlib
import time
import typing
from collections.abc import Iterator
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

PROMETHEUS_AVAILABLE = find_spec("prometheus_client")

if TYPE_CHECKING:
    from prometheus_client import Histogram

_T = TypeVar("_T")


class Metric(Protocol):
    def labels(self, *args: Any, **kwargs: Any) -> "Metric": ...
    def inc(self, amount: float = 1.0) -> None: ...
    def observe(self, value: float) -> None: ...
    def set(self, value: float) -> None: ...


class NoOpMetric:
    """A dummy prometheus metric which does nothing."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa:ARG002
        return None

    def labels(self, *args: Any, **kwargs: Any) -> "NoOpMetric":  # noqa:ARG002
        return self

    def inc(self, amount: float = 1.0) -> None:  # noqa:ARG002
        return None

    def observe(self, value: float) -> None:  # noqa:ARG002
        return None

    def set(self, value: float) -> None:  # noqa:ARG002
        return None


class Singleton(type, typing.Generic[_T]):
    _instances: dict["Singleton[_T]", _T] = {}  # noqa:RUF012

    def __call__(cls, *args: typing.Any, **kwargs: typing.Any) -> _T:
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class DogpileMetrics(metaclass=Singleton):
    """
    Holds all metrics for dogpile_breaker.

    If enabled=True, initializes real Prometheus metrics.
    Otherwise, assigns NoOpMetric instances.
    """

    def __init__(self, enabled: bool) -> None:  # noqa:FBT001
        if enabled and not PROMETHEUS_AVAILABLE:
            err_msg = "Prometheus client is not installed. Install it with `pip install prometheus_client`"
            raise RuntimeError(err_msg)
        if enabled:
            from prometheus_client import Counter, Gauge, Histogram  # noqa:PLC0415
        else:
            Counter: type[Metric] = NoOpMetric  # type:ignore[no-redef]  #noqa:N806
            Gauge: type[Metric] = NoOpMetric  # type:ignore[no-redef]  #noqa:N806
            Histogram: type[Metric] = NoOpMetric  # type:ignore[no-redef]  #noqa:N806

        # stats for whole cache region
        self.cache_hits = Counter(
            name="dogpile_cache_hits_total", documentation="Cache hits (non-expired)", labelnames=["region_name"]
        )
        self.cache_misses = Counter(
            name="dogpile_cache_misses_total",
            documentation="Cache misses (no entry / must generate)",
            labelnames=["region_name"],
        )
        self.cache_stale_served = Counter(
            name="dogpile_cache_stale_served_total",
            documentation="Stale/expired entries served",
            labelnames=["region_name"],
        )
        self.cache_herd_waited = Counter(
            name="dogpile_cache_herd_waited_total",
            documentation="Number of requests processed with herd protection",
            labelnames=["region_name"],
        )
        # stats for calling function to regenerate cache
        self.generation_calls = Counter(
            name="dogpile_generation_calls_total",
            documentation="Regeneration (generate_func) calls",
            labelnames=["region_name", "func_name"],
        )
        self.generation_errors = Counter(
            name="dogpile_generation_errors_total",
            documentation="Errors while generating",
            labelnames=["region_name", "func_name"],
        )
        self.generation_latency = Histogram(
            name="dogpile_generation_latency_seconds",
            documentation="Latency of generate_func",
            labelnames=["region_name", "func_name"],
        )
        # Backend storage metrics
        self.backend_latency = Histogram(
            name="dogpile_backend_op_latency_seconds",
            documentation="Backend operation latency",
            labelnames=["region_name", "storage_name", "operation"],
        )
        self.backend_errors = Counter(
            name="dogpile_backend_errors_total",
            documentation="Backend operation errors",
            labelnames=["region_name", "storage_name", "operation"],
        )

        # Circuit breaker metrics
        self.cb_state = Gauge(
            name="dogpile_cb_state",
            documentation="Circuit breaker state (0=closed,1=open,2=half_open)",
            labelnames=["region_name"],
        )
        self.cb_open = Counter(
            name="dogpile_cb_open_total", documentation="Circuit open events", labelnames=["region_name"]
        )
        self.cb_close = Counter(
            name="dogpile_cb_close_total", documentation="Circuit close events", labelnames=["region_name"]
        )
        self.cb_fallbacks = Counter(
            name="dogpile_cb_fallbacks_total", documentation="Fallback invocations", labelnames=["region_name"]
        )
        self.cb_fallback_errors = Counter(
            name="dogpile_cb_fallback_errors_total",
            documentation="Fallback storage errors",
            labelnames=["region_name", "error_type"],
        )


@contextlib.contextmanager
def timer_ctx(metric: "Histogram | NoOpMetric", labels: dict[str, str]) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        # Time can go backwards.
        duration = max(time.perf_counter() - start, 0)
        metric.labels(**labels).observe(duration)
