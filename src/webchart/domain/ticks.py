"""축 눈금 계산 (계획 문서 2.5, 4.3).

눈금은 서버가 계산한다. 정적 렌더와 WS 프레임이 같은 함수를 써서, 클라이언트 구현과
상관없이 같은 눈금이 나오고 시간축 정렬·시간대 처리도 이 한 곳에 모인다.

- 수치축: 1·2·5 × 10^k 간격 (nice number)
- 시간축: 사람이 읽기 좋은 단위(초·분·시·일·월·년)에 맞춘 눈금. 표시 시간대의 벽시계
  기준으로 정렬한다 (예: Asia/Seoul이면 한국 자정에 일 눈금).

시간 규칙(정규 좌표 = epoch 나노초)은 services/data/x_domain.py와 같다. 시간대가 없는
(naive) 시간은 벽시계 값을 UTC로 저장했으므로 UTC로 계산하면 원래 벽시계 값이 나온다.
"""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

# 눈금 사이 목표 간격 (px). 축 길이 / 이 값 ≈ 눈금 수
DEFAULT_TICK_SPACING_PX = 100

_NS_PER_US = 1_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Tick:
    """눈금 하나. value는 정규 좌표, pixel은 축 시작에서의 거리(px)."""

    value: float
    label: str
    pixel: float = 0.0


def target_tick_count(length_px: float, spacing_px: float = DEFAULT_TICK_SPACING_PX) -> int:
    return max(2, int(length_px // spacing_px))


def place(ticks: list[Tick], low: float, high: float, length_px: float) -> list[Tick]:
    """눈금에 픽셀 위치를 붙인다 (low → 0px, high → length_px)."""
    scale = length_px / (high - low)
    return [Tick(t.value, t.label, (t.value - low) * scale) for t in ticks]


# --- 수치축 -----------------------------------------------------------------------


def numeric_ticks(low: float, high: float, target_count: int) -> list[Tick]:
    """[low, high] 안의 1·2·5 × 10^k 간격 눈금."""
    if not high > low:
        return [Tick(low, _format_number(low, 1.0))]
    step = _closest_nice_step(high - low, max(1, target_count))
    first = math.ceil(low / step - 1e-9)
    last = math.floor(high / step + 1e-9)
    return [Tick(i * step, _format_number(i * step, step)) for i in range(first, last + 1)]


def _closest_nice_step(span: float, target_count: int) -> float:
    """구간 수(span / step)가 target_count에 가장 가까운 1·2·5 × 10^k 간격."""
    exponent = math.floor(math.log10(span / target_count))
    candidates = [
        multiplier * 10.0**power
        for power in (exponent - 1, exponent, exponent + 1)
        for multiplier in (1.0, 2.0, 5.0)
    ]
    return min(candidates, key=lambda step: abs(span / step - target_count))


def _format_number(value: float, step: float) -> str:
    magnitude = max(abs(value), step)
    if magnitude >= 1e7 or step < 1e-4:
        text = f"{value:.3g}"
    else:
        decimals = max(0, -math.floor(math.log10(step) + 1e-9))
        text = f"{value:.{decimals}f}"
    if text.startswith("-") and float(text) == 0:
        return text[1:]  # "-0.0" → "0.0"
    return text


# --- 시간축 -----------------------------------------------------------------------

# (단위, 배수 후보). 간격이 짧은 것부터 고른다
_TIME_LADDER: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("ms", (1, 2, 5, 10, 20, 50, 100, 200, 500)),
    ("s", (1, 2, 5, 10, 15, 30)),
    ("min", (1, 2, 5, 10, 15, 30)),
    ("h", (1, 2, 3, 6, 12)),
    ("D", (1, 2, 5, 10)),
    ("MS", (1, 2, 3, 6)),
    ("YS", (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000)),
)

# 단위 길이 (나노초). 월·년은 평균 길이로 간격을 고를 때만 쓴다
_UNIT_NS = {
    "ms": 10**6,
    "s": 10**9,
    "min": 60 * 10**9,
    "h": 3_600 * 10**9,
    "D": 86_400 * 10**9,
    "MS": int(30.44 * 86_400 * 10**9),
    "YS": int(365.25 * 86_400 * 10**9),
}

_MAX_TICKS = 1_000  # 안전장치


def time_ticks(
    start_ns: float, end_ns: float, target_count: int, timezone: str | None = None
) -> list[Tick]:
    """[start_ns, end_ns] 안의 시간 눈금. 라벨은 표시 시간대의 벽시계 값이다."""
    zone: tzinfo = ZoneInfo(timezone) if timezone else UTC
    if not end_ns > start_ns:
        return [Tick(start_ns, _to_local(start_ns, zone).isoformat())]
    unit, step = _choose_interval((end_ns - start_ns) / max(1, target_count))

    current = _floor(_to_local(start_ns, zone), unit, step, zone)
    ticks: list[Tick] = []
    previous_day: tuple[int, int, int] | None = None
    for _ in range(_MAX_TICKS):
        value = _to_ns(current)
        if value > end_ns:
            break
        if value >= start_ns:
            day = (current.year, current.month, current.day)
            ticks.append(Tick(float(value), _label(current, unit, show_date=day != previous_day)))
            previous_day = day
        current = _advance(current, unit, step, zone)
    return ticks


def _choose_interval(raw_ns: float) -> tuple[str, int]:
    for unit, steps in _TIME_LADDER:
        for step in steps:
            if _UNIT_NS[unit] * step >= raw_ns:
                return unit, step
    return "YS", _TIME_LADDER[-1][1][-1]


def _to_local(ns: float, zone: tzinfo) -> datetime:
    return (_EPOCH + timedelta(microseconds=round(ns) // _NS_PER_US)).astimezone(zone)


def _to_ns(moment: datetime) -> int:
    return ((moment - _EPOCH) // timedelta(microseconds=1)) * _NS_PER_US


def _floor(moment: datetime, unit: str, step: int, zone: tzinfo) -> datetime:
    """벽시계 기준으로 unit·step 경계에 내림."""
    if unit == "ms":
        micro = moment.microsecond - moment.microsecond % (step * 1_000)
        return moment.replace(microsecond=micro)
    if unit == "s":
        return moment.replace(second=moment.second - moment.second % step, microsecond=0)
    if unit == "min":
        return moment.replace(minute=moment.minute - moment.minute % step, second=0, microsecond=0)
    if unit == "h":
        return moment.replace(hour=moment.hour - moment.hour % step, minute=0, second=0,
                              microsecond=0)  # fmt: skip
    if unit == "D":
        # 여러 날 간격은 날짜 순번(ordinal)의 배수로 맞춘다. 월마다 1일로 다시 맞추면
        # 월말·월초 눈금이 붙어 버린다.
        ordinal = moment.toordinal() - moment.toordinal() % step
        day = date.fromordinal(ordinal)
        return datetime(day.year, day.month, day.day, tzinfo=zone)
    if unit == "MS":
        month = moment.month - (moment.month - 1) % step
        return datetime(moment.year, month, 1, tzinfo=zone)
    return datetime(max(1, moment.year - moment.year % step), 1, 1, tzinfo=zone)


def _advance(moment: datetime, unit: str, step: int, zone: tzinfo) -> datetime:
    """다음 눈금. 일 이상은 벽시계 날짜로 더해 서머타임에도 자정에 맞춘다."""
    fixed = {"ms": timedelta(milliseconds=step), "s": timedelta(seconds=step),
             "min": timedelta(minutes=step), "h": timedelta(hours=step)}  # fmt: skip
    if unit in fixed:
        return (moment.astimezone(UTC) + fixed[unit]).astimezone(zone)
    if unit == "D":
        next_day = moment.date() + timedelta(days=step)
        return datetime(next_day.year, next_day.month, next_day.day, tzinfo=zone)
    if unit == "MS":
        months = moment.year * 12 + moment.month - 1 + step
        return datetime(months // 12, months % 12 + 1, 1, tzinfo=zone)
    return datetime(moment.year + step, 1, 1, tzinfo=zone)


def _label(moment: datetime, unit: str, show_date: bool) -> str:
    if unit == "YS":
        return f"{moment:%Y}"
    if unit == "MS":
        return f"{moment:%Y-%m}"
    if unit == "D":
        return f"{moment:%Y-%m-%d}"
    clock = {
        "h": "%H:%M",
        "min": "%H:%M",
        "s": "%H:%M:%S",
        "ms": "%H:%M:%S.%f",
    }[unit]
    text = moment.strftime(clock)
    if unit == "ms":
        text = text[:-3]  # 마이크로초 → 밀리초
    return f"{moment:%m-%d} {text}" if show_date else text
