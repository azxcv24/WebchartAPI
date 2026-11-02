"""프로세스 내 지표 (계획 문서 4.1): 캐시 적중률, 렌더 지연, 경로별 exact 비율.

외부 의존성 없이 Prometheus 텍스트 형식으로 내보낸다 (`GET /metrics`).
여러 워커를 띄우면 지표는 워커별이다 (Prometheus가 워커마다 긁어 합친다).
"""

import bisect
import threading
from collections import defaultdict
from dataclasses import dataclass, field

# 렌더 지연 히스토그램 경계 (초)
LATENCY_BUCKETS_S = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

Labels = tuple[tuple[str, str], ...]


@dataclass
class _Histogram:
    counts: list[int] = field(default_factory=lambda: [0] * (len(LATENCY_BUCKETS_S) + 1))
    total: float = 0.0
    observations: int = 0

    def observe(self, seconds: float) -> None:
        self.counts[bisect.bisect_left(LATENCY_BUCKETS_S, seconds)] += 1
        self.total += seconds
        self.observations += 1


class Metrics:
    """카운터와 지연 히스토그램. 렌더 스레드에서도 부르므로 잠금으로 보호한다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, Labels], float] = defaultdict(float)
        self._histograms: dict[tuple[str, Labels], _Histogram] = defaultdict(_Histogram)
        self._help: dict[str, str] = {}

    def increment(self, name: str, help_text: str, amount: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._help.setdefault(name, help_text)
            self._counters[(name, _labels(labels))] += amount

    def observe(self, name: str, help_text: str, seconds: float, **labels: str) -> None:
        with self._lock:
            self._help.setdefault(name, help_text)
            self._histograms[(name, _labels(labels))].observe(seconds)

    def counter(self, name: str, **labels: str) -> float:
        with self._lock:
            return self._counters.get((name, _labels(labels)), 0.0)

    def render_prometheus(self) -> str:
        with self._lock:
            lines: list[str] = []
            for name in sorted({key[0] for key in self._counters}):
                lines += [f"# HELP {name} {self._help[name]}", f"# TYPE {name} counter"]
                for (metric, labels), value in sorted(self._counters.items()):
                    if metric == name:
                        lines.append(f"{name}{_format(labels)} {value:g}")
            for name in sorted({key[0] for key in self._histograms}):
                lines += [f"# HELP {name} {self._help[name]}", f"# TYPE {name} histogram"]
                for (metric, labels), histogram in sorted(self._histograms.items()):
                    if metric != name:
                        continue
                    cumulative = 0
                    for bound, count in zip(
                        (*LATENCY_BUCKETS_S, float("inf")), histogram.counts, strict=True
                    ):
                        cumulative += count
                        le = "+Inf" if bound == float("inf") else f"{bound:g}"
                        lines.append(f"{name}_bucket{_format((*labels, ('le', le)))} {cumulative}")
                    lines.append(f"{name}_sum{_format(labels)} {histogram.total:g}")
                    lines.append(f"{name}_count{_format(labels)} {histogram.observations}")
            return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


def _labels(labels: dict[str, str]) -> Labels:
    return tuple(sorted(labels.items()))


def _format(labels: Labels) -> str:
    if not labels:
        return ""
    escaped = (f'{key}="{value}"'.replace("\n", " ") for key, value in labels)
    return "{" + ",".join(escaped) + "}"


metrics = Metrics()


def source_kind(source: str) -> str:
    """출처에서 레벨 번호를 뺀다: pyramid:L12+raw → pyramid+raw (라벨 종류를 늘리지 않게)."""
    parts = [part.split(":")[0] for part in source.split("+") if part]
    return "+".join(dict.fromkeys(parts)) or "none"
