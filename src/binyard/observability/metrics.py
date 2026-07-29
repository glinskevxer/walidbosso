"""Counters, gauges and timers, kept in process and rendered on demand.

This is not a metrics system. It is the smallest thing that answers "how many
loads went over the scale today and how long did the blender take", exposed on
an endpoint a scrape can read. A house running one server does not need a push
gateway, and if it grows into one, this registry is the shim that feeds it.

The renderer emits the Prometheus text exposition format because that is what
every scraper reads, not because anything here depends on Prometheus.

Thread safety is a single lock around the whole registry. Contention would
matter if this were on a hot path; it is on the path of a truck crossing a
scale, which happens a few hundred times on a busy day.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from binyard.errors import ValidationError

__all__ = ["REGISTRY", "Registry", "Sample", "counter", "gauge", "observe", "timed"]

_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_")


@dataclass(frozen=True, slots=True)
class Sample:
    """One named number, with its labels."""

    name: str
    value: float
    labels: tuple[tuple[str, str], ...] = ()
    kind: str = "counter"

    def rendered_labels(self) -> str:
        if not self.labels:
            return ""
        inner = ",".join(f'{key}="{_escape(value)}"' for key, value in self.labels)
        return "{" + inner + "}"


@dataclass
class _Histogram:
    buckets: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    seen: int = 0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * (len(self.buckets) + 1)

    def observe(self, value: float) -> None:
        self.total += value
        self.seen += 1
        for index, edge in enumerate(self.buckets):
            if value <= edge:
                self.counts[index] += 1
                return
        self.counts[-1] += 1


class Registry:
    """Every metric this process holds."""

    #: Seconds. Sized for a blend solve and a settlement run, not a web request.
    DEFAULT_BUCKETS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], _Histogram] = {}

    def counter(self, name: str, amount: float = 1.0, **labels: str) -> None:
        key = (_checked(name), _labels(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + amount

    def gauge(self, name: str, value: float, **labels: str) -> None:
        key = (_checked(name), _labels(labels))
        with self._lock:
            self._gauges[key] = value

    def observe(self, name: str, seconds: float, **labels: str) -> None:
        key = (_checked(name), _labels(labels))
        with self._lock:
            histogram = self._histograms.get(key)
            if histogram is None:
                histogram = _Histogram(buckets=self.DEFAULT_BUCKETS)
                self._histograms[key] = histogram
            histogram.observe(seconds)

    @contextmanager
    def timed(self, name: str, **labels: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, time.perf_counter() - started, **labels)

    def value_of(self, name: str, **labels: str) -> float:
        key = (name, _labels(labels))
        with self._lock:
            if key in self._counters:
                return self._counters[key]
            if key in self._gauges:
                return self._gauges[key]
            if key in self._histograms:
                return float(self._histograms[key].seen)
        raise ValidationError("no such metric", name=name)

    def samples(self) -> tuple[Sample, ...]:
        with self._lock:
            found = [
                Sample(name=name, value=value, labels=labels, kind="counter")
                for (name, labels), value in sorted(self._counters.items())
            ]
            found += [
                Sample(name=name, value=value, labels=labels, kind="gauge")
                for (name, labels), value in sorted(self._gauges.items())
            ]
            for (name, labels), histogram in sorted(self._histograms.items()):
                found.append(
                    Sample(
                        name=f"{name}_count",
                        value=float(histogram.seen),
                        labels=labels,
                        kind="counter",
                    )
                )
                found.append(
                    Sample(
                        name=f"{name}_sum",
                        value=histogram.total,
                        labels=labels,
                        kind="counter",
                    )
                )
        return tuple(found)

    def render(self) -> str:
        """The Prometheus text exposition format, which every scraper reads."""
        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"{name}{Sample(name, value, labels).rendered_labels()} {value:g}")
            for (name, labels), value in sorted(self._gauges.items()):
                lines.append(f"{name}{Sample(name, value, labels).rendered_labels()} {value:g}")
            for (name, labels), histogram in sorted(self._histograms.items()):
                rendered = Sample(name, 0.0, labels).rendered_labels()
                running = 0
                for index, edge in enumerate(histogram.buckets):
                    running += histogram.counts[index]
                    bucket = _with_label(labels, "le", f"{edge:g}")
                    lines.append(f"{name}_bucket{bucket} {running}")
                running += histogram.counts[-1]
                lines.append(f"{name}_bucket{_with_label(labels, 'le', '+Inf')} {running}")
                lines.append(f"{name}_sum{rendered} {histogram.total:g}")
                lines.append(f"{name}_count{rendered} {histogram.seen}")
        return "\n".join(lines) + ("\n" if lines else "")

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


REGISTRY = Registry()


def counter(name: str, amount: float = 1.0, **labels: str) -> None:
    """Add to a counter on the process registry."""
    REGISTRY.counter(name, amount, **labels)


def gauge(name: str, value: float, **labels: str) -> None:
    """Set a gauge on the process registry."""
    REGISTRY.gauge(name, value, **labels)


def observe(name: str, seconds: float, **labels: str) -> None:
    """Record a timing on the process registry."""
    REGISTRY.observe(name, seconds, **labels)


def timed(name: str, **labels: str):
    """Time the block and record it on the process registry."""
    return REGISTRY.timed(name, **labels)


def _checked(name: str) -> str:
    if not name or set(name) - _NAME_CHARS:
        raise ValidationError(
            "a metric name is lowercase letters, digits and underscores", name=name
        )
    return name


def _labels(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    for key in labels:
        _checked(key)
    return tuple(sorted((key, str(value)) for key, value in labels.items()))


def _with_label(labels: tuple[tuple[str, str], ...], key: str, value: str) -> str:
    combined = (*labels, (key, value))
    inner = ",".join(f'{name}="{_escape(item)}"' for name, item in combined)
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
