"""재현 가능한 합성 데이터 생성기.

왜곡을 잘 드러내는 데이터를 만든다.
- 단일 샘플 스파이크: 다운샘플링이 피크를 버리면 바로 보인다.
- 시리즈마다 다른 위치의 스파이크: 첫 시리즈만 보고 점을 고르는 버그(D2)를 드러낸다.
- 불규칙한 x 간격: 행 개수 기준 버킷과 픽셀 경계가 어긋나는 문제(D4)를 드러낸다.

같은 명세(seed 포함)는 항상 같은 데이터를 만든다. 대용량 벤치마크를 위해
청크 단위로도 생성할 수 있다 (`iter_chunks`).
"""

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import pandas as pd

X_COLUMN = "x"
TIME_ORIGIN = pd.Timestamp("2026-01-01")

# 기본 신호의 진폭. 노이즈를 더해도 대략 ±(BASE_AMPLITUDE + 1) 안에 머문다.
BASE_AMPLITUDE = 2.0
NOISE_STD = 0.3


def series_column(index: int) -> str:
    """i번째 y 시리즈의 컬럼 이름 (`s0`, `s1`, ...)."""
    return f"s{index}"


@dataclass(frozen=True, slots=True)
class SyntheticSpec:
    """합성 데이터 명세.

    Attributes:
        rows: 전체 행 수
        series: y 시리즈 수. 컬럼 이름은 `series_column(i)`
        spikes_per_series: 시리즈마다 넣을 단일 샘플 스파이크 수
        spike_height: 스파이크 높이. 짝수 시리즈는 +, 홀수 시리즈는 - 방향
        time_axis: True면 x가 datetime64[ns] (TIME_ORIGIN부터 평균 1초 간격)
        irregular_x: True면 x 간격이 평균 1인 지수분포 (불규칙 샘플링)
        seed: 난수 시드
    """

    rows: int
    series: int = 1
    spikes_per_series: int = 0
    spike_height: float = 50.0
    time_axis: bool = False
    irregular_x: bool = False
    seed: int = 0

    @property
    def y_columns(self) -> list[str]:
        return [series_column(i) for i in range(self.series)]


def spike_positions(spec: SyntheticSpec) -> dict[str, np.ndarray]:
    """시리즈별 스파이크 행 위치 (오름차순). 시리즈끼리 위치가 겹치지 않는다."""
    total = spec.spikes_per_series * spec.series
    if total == 0:
        return {column: np.empty(0, dtype=np.int64) for column in spec.y_columns}
    if total > spec.rows:
        raise ValueError(f"스파이크 수({total})가 행 수({spec.rows})보다 많다")
    rng = np.random.default_rng([spec.seed, 1])
    chosen = rng.choice(spec.rows, size=total, replace=False)
    return {
        column: np.sort(chosen[i * spec.spikes_per_series : (i + 1) * spec.spikes_per_series])
        for i, column in enumerate(spec.y_columns)
    }


def iter_chunks(spec: SyntheticSpec, chunk_rows: int = 1_000_000) -> Iterator[pd.DataFrame]:
    """`chunk_rows`행씩 DataFrame을 만든다. 이어 붙이면 `generate(spec)`과 같다."""
    if chunk_rows <= 0:
        raise ValueError(f"chunk_rows는 양수여야 한다: {chunk_rows}")
    spikes = spike_positions(spec)
    x_offset = 0.0  # 불규칙 간격에서 다음 청크가 이어받을 x 시작값

    for chunk_index, row_start in enumerate(range(0, spec.rows, chunk_rows)):
        row_end = min(row_start + chunk_rows, spec.rows)
        rng = np.random.default_rng([spec.seed, 2, chunk_index])
        row_numbers = np.arange(row_start, row_end, dtype=np.int64)

        if spec.irregular_x:
            steps = rng.exponential(1.0, size=row_end - row_start)
            x_values = x_offset + np.cumsum(steps)
            x_offset = float(x_values[-1])
        else:
            x_values = row_numbers.astype(np.float64)

        frame: dict[str, object] = {X_COLUMN: _as_x_column(x_values, spec.time_axis)}
        phase = row_numbers / max(spec.rows, 1)
        for i, column in enumerate(spec.y_columns):
            # 시리즈마다 주파수를 다르게 해 서로 겹쳐 보이지 않게 한다
            signal = BASE_AMPLITUDE * np.sin(2 * np.pi * phase * (i + 3))
            values = signal + rng.normal(0.0, NOISE_STD, size=row_end - row_start)
            _apply_spikes(values, spikes[column], row_start, row_end, spec.spike_height, i)
            frame[column] = values
        yield pd.DataFrame(frame)


def generate(spec: SyntheticSpec) -> pd.DataFrame:
    """명세 전체를 하나의 DataFrame으로 만든다 (작은 데이터용)."""
    return pd.concat(list(iter_chunks(spec, chunk_rows=max(spec.rows, 1))), ignore_index=True)


def _as_x_column(x_values: np.ndarray, time_axis: bool) -> object:
    if not time_axis:
        return x_values
    # 1 단위 = 1초. 나노초 정수로 바꿔 datetime64[ns]를 만든다.
    nanoseconds = np.round(x_values * 1e9).astype(np.int64)
    return pd.to_datetime(TIME_ORIGIN.value + nanoseconds, unit="ns")


def _apply_spikes(
    values: np.ndarray,
    positions: np.ndarray,
    row_start: int,
    row_end: int,
    height: float,
    series_index: int,
) -> None:
    """이 청크 범위 [row_start, row_end)에 속한 스파이크만 값에 덮어쓴다."""
    in_chunk = positions[(positions >= row_start) & (positions < row_end)]
    direction = 1.0 if series_index % 2 == 0 else -1.0
    values[in_chunk - row_start] = direction * height
