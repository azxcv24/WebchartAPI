"""피라미드 빌드 유스케이스: 원본 → 격자 정렬 BucketStats 레벨 (계획 문서 1.3)."""

import logging

from webchart.application.windows import lattice_for, pyramid_dir
from webchart.config import settings
from webchart.core.errors import DatasetProcessingError
from webchart.infrastructure.pyramid_store import build_pyramid as build_levels
from webchart.infrastructure.pyramid_store import choose_max_level
from webchart.models.dataset import PYRAMID_FORMAT_VERSION, ColumnKind, PyramidInfo
from webchart.services.data.access import get_dataset_meta, raw_store, save_dataset_meta

logger = logging.getLogger("webchart.application.pyramids")


def build_pyramid(dataset_id: str, x_column: str, columns: list[str] | None = None) -> PyramidInfo:
    """x 컬럼 하나에 대한 피라미드를 만들고 meta.json에 기록한다.

    Args:
        dataset_id: 데이터셋 ID
        x_column: 수치 또는 시간 컬럼
        columns: 집계할 y 컬럼. None이면 x를 뺀 모든 수치 컬럼
    """
    meta = get_dataset_meta(dataset_id)
    kinds = {column.name: column.kind for column in meta.columns}
    if kinds.get(x_column) not in (ColumnKind.NUMERIC, ColumnKind.TIME):
        raise DatasetProcessingError(f"x_column '{x_column}'은 수치나 시간 컬럼이어야 한다")

    numeric = [name for name, kind in kinds.items() if kind is ColumnKind.NUMERIC]
    y_columns = [c for c in (columns or numeric) if c != x_column]
    not_numeric = [c for c in y_columns if kinds.get(c) is not ColumnKind.NUMERIC]
    if not_numeric:
        raise DatasetProcessingError(f"y 컬럼은 수치여야 한다: {not_numeric}")
    if not y_columns:
        raise DatasetProcessingError("피라미드에는 수치 y 컬럼이 하나 이상 필요하다")

    domain = lattice_for(meta, x_column, tile_size_px=1).domain
    max_level = choose_max_level(
        meta.row_count, len(y_columns), settings.pyramid_max_level, settings.pyramid_max_bytes
    )
    size_bytes = build_levels(
        raw_store(meta), pyramid_dir(dataset_id, x_column), x_column, y_columns, domain, max_level
    )
    info = PyramidInfo(
        version=PYRAMID_FORMAT_VERSION,
        x_column=x_column,
        columns=y_columns,
        max_level=max_level,
        data_version=meta.data_version,
        domain_start=domain.start,
        domain_end=domain.end,
        size_bytes=size_bytes,
    )

    # 빌드 도중 다른 갱신이 있었을 수 있으므로 meta를 다시 읽어 피라미드 항목만 바꾼다
    fresh = get_dataset_meta(dataset_id)
    fresh.pyramids[x_column] = info
    save_dataset_meta(fresh)
    logger.info(
        f"피라미드 빌드: {dataset_id}/{x_column} L0..L{max_level}, {size_bytes / 1e6:.1f} MB"
    )
    return info
