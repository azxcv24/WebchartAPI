"""실시간 추가 오라클 (계획 문서 4.3).

추가한 뒤의 그림 = (기존 + 추가) 원본 전체를 그린 그림. 피라미드는 다시 빌드하지 않고
merge만 했으므로, 이 테스트가 증분 merge와 격자 확장의 정확성을 검사한다.
"""

import numpy as np
import pandas as pd
import pytest

from webchart.application.append import append_columns
from webchart.application.interaction import range_summary
from webchart.application.pyramids import build_pyramid
from webchart.application.tiles import TileRequest, plan_frame
from webchart.application.windows import lattice_for
from webchart.config import settings
from webchart.domain.geometry import TileAddress, XRange
from webchart.models.chart import ChartSpec
from webchart.services.data.access import get_dataset_meta
from webchart.testing.synthetic import X_COLUMN, SyntheticSpec, generate

from .harness import assert_no_distortion, diff_tiles_against_reference, ingest, line_spec

BASE = SyntheticSpec(rows=100_000, series=2, spikes_per_series=10, gaps_per_series=2, seed=31)


def _tail(rows: int, start_x: float, seed: int, step: float = 1.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    x = start_x + np.arange(rows) * step
    s0 = np.sin(x / 3_000) * 2 + rng.normal(0, 0.3, rows)
    s1 = np.cos(x / 2_000) * 2 + rng.normal(0, 0.3, rows)
    s0[rng.choice(rows, 3, replace=False)] = 40.0  # 추가분에도 스파이크
    return pd.DataFrame({X_COLUMN: x, "s0": s0, "s1": s1})


def _append(dataset_id: str, frame: pd.DataFrame):
    return append_columns(dataset_id, {name: frame[name].tolist() for name in frame.columns})


async def test_append_beyond_end_grows_domain_and_stays_exact():
    frame = generate(BASE)
    ingest(frame, "live")
    build_pyramid("live", X_COLUMN, columns=BASE.y_columns)
    before = get_dataset_meta("live").pyramids[X_COLUMN]

    # 기존 끝(99,999) 뒤로 15만 행: 격자 폭을 넘으므로 2배씩 넓어진다
    tail = _tail(150_000, start_x=100_000, seed=1)
    result = _append("live", tail)
    meta = get_dataset_meta("live")
    after = meta.pyramids[X_COLUMN]

    assert result.grown_columns == [X_COLUMN] and result.rows == 150_000
    assert after.domain_start == before.domain_start
    assert after.domain_end - after.domain_start == 4 * (before.domain_end - before.domain_start)
    assert after.storage != before.storage
    combined = pd.concat([frame, tail], ignore_index=True)
    spec = line_spec("live", BASE.y_columns)
    for zoom in (0, 2):
        diff = await diff_tiles_against_reference(combined, meta, spec, zoom=zoom)
        print("zoom", zoom, diff.summary())
        assert_no_distortion(diff)


async def test_append_inside_range_merges_in_place():
    frame = generate(BASE)
    ingest(frame, "backfill")
    build_pyramid("backfill", X_COLUMN, columns=BASE.y_columns)
    storage = get_dataset_meta("backfill").pyramids[X_COLUMN].storage

    # 기존 구간 안에 반 칸씩 어긋난 x로 끼워 넣는다 (정렬 순서가 섞이는 추가)
    inside = _tail(5_000, start_x=20_000.5, seed=2, step=4.0)
    result = _append("backfill", inside)
    meta = get_dataset_meta("backfill")

    assert result.grown_columns == []
    assert meta.pyramids[X_COLUMN].storage == storage  # 같은 파일을 고쳤다
    combined = pd.concat([frame, inside], ignore_index=True).sort_values(X_COLUMN, kind="stable")
    spec = line_spec("backfill", BASE.y_columns)
    assert_no_distortion(await diff_tiles_against_reference(combined, meta, spec, zoom=1))

    summary = range_summary(meta, X_COLUMN, ["s0"], XRange(10_000.0, 60_000.0))
    window = combined[(combined[X_COLUMN] >= 10_000) & (combined[X_COLUMN] < 60_000)]["s0"].dropna()
    assert summary.series["s0"].count == len(window)
    assert summary.series["s0"].max == window.max()


def test_only_touched_tiles_change_cache_keys():
    frame = generate(BASE)
    ingest(frame, "keys")
    build_pyramid("keys", X_COLUMN, columns=BASE.y_columns)
    # y범위를 고정해 GLOBAL 범위 변화(새 최댓값 등)로 모든 키가 바뀌는 것을 막는다
    spec = ChartSpec(
        **line_spec("keys", BASE.y_columns).model_dump(exclude={"layers", "axes"}),
        axes={"y": {"range": (-60.0, 60.0)}},
    )

    def keys(meta) -> list[str]:
        lattice = lattice_for(meta, X_COLUMN, settings.tile_size)
        plan = plan_frame(meta, spec, lattice.domain, 3, prefetch_radius=0)
        return [
            TileRequest(meta, spec, lattice, TileAddress(3, i), 1, plan.y_ranges).cache_key()
            for i in range(8)
        ]

    # 첫 추가는 격자를 고정한다. 그 뒤의 추가부터 비교한다
    _append("keys", _tail(10, start_x=50_000.25, seed=3, step=0.5))
    before = keys(get_dataset_meta("keys"))
    _append("keys", _tail(10, start_x=30_000.25, seed=4, step=0.5))  # 타일 2 (25k~37.5k)
    after = keys(get_dataset_meta("keys"))

    changed = [i for i, (old, new) in enumerate(zip(before, after, strict=True)) if old != new]
    assert changed == [2]


def test_rejects_unknown_columns():
    from webchart.core.errors import DatasetProcessingError

    ingest(generate(SyntheticSpec(rows=100)), "strict")
    with pytest.raises(DatasetProcessingError):
        append_columns("strict", {"nope": [1.0]})
