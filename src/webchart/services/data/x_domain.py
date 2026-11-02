"""x축 값 정규화: 컬럼의 원래 값 ↔ 정규 좌표(float64).

격자·타일·호버 계산은 모두 정규 좌표 하나로 한다. 그래야 수치 축과 시간 축이
같은 코드 경로를 탄다 (D3 해소).

정규 좌표 규칙
- NUMERIC: 값 그대로 float64
- TIME: epoch 나노초
  - tz-aware 시간은 UTC 기준 나노초다.
  - naive 시간은 벽시계 값을 그대로 UTC로 간주한다. 시간대 변환이 일어나지 않으므로
    표시할 때도 원래 벽시계 값이 그대로 나온다.
  - float64라서 2026년 기준 해상도는 약 256ns다. 픽셀 폭보다 훨씬 작아 렌더 좌표로는
    충분하다. 정확한 원래 값이 필요한 곳(호버 등)은 원본 행의 값을 다시 읽는다.

날짜 전용 컬럼(date32 등)도 자정 시각의 timestamp로 보고 같은 규칙을 적용한다.
"""

import datetime as dt
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
import pyarrow as pa

from webchart.models.dataset import ColumnInfo, ColumnKind


def column_kind_of(arrow_type: pa.DataType) -> ColumnKind:
    """Arrow 타입 → 컬럼 분류."""
    if pa.types.is_integer(arrow_type) or pa.types.is_floating(arrow_type):
        return ColumnKind.NUMERIC
    if pa.types.is_decimal(arrow_type):
        return ColumnKind.NUMERIC
    if pa.types.is_timestamp(arrow_type) or pa.types.is_date(arrow_type):
        return ColumnKind.TIME
    return ColumnKind.OTHER


def canonical_scalar(value: Any, kind: ColumnKind) -> float | None:
    """통계값 하나(예: Parquet 로우그룹 min)를 정규 좌표로 바꾼다."""
    if value is None:
        return None
    if kind is ColumnKind.NUMERIC:
        return float(value)
    if kind is ColumnKind.TIME:
        return float(pd.Timestamp(value).as_unit("ns").value)
    return None


def to_canonical(values: pd.Series) -> np.ndarray:
    """컬럼 값 전체 → 정규 좌표 float64 배열. 결측(NaN/NaT)은 NaN이 된다."""
    if pd.api.types.is_datetime64_any_dtype(values):
        nanoseconds = values.dt.as_unit("ns").astype("int64").to_numpy(dtype=np.float64)
        return np.where(values.isna().to_numpy(), np.nan, nanoseconds)
    if pd.api.types.is_bool_dtype(values):
        raise TypeError(f"불리언 컬럼 '{values.name}'은 x축으로 쓸 수 없다")
    if pd.api.types.is_numeric_dtype(values):
        return values.to_numpy(dtype=np.float64, na_value=np.nan)
    if _holds_dates(values):
        return to_canonical(pd.to_datetime(values))
    raise TypeError(f"컬럼 '{values.name}'({values.dtype})은 x축으로 쓸 수 없다")


def canonical_from_arrow(values: pa.Array | pa.ChunkedArray) -> npt.NDArray[np.float64]:
    """Arrow 컬럼 → 정규 좌표 float64 배열. 결측(null, NaN)은 NaN이 된다.

    원본 저장소(RawStore)가 pandas를 거치지 않고 바로 숫자 배열을 얻기 위해 쓴다.
    """
    arrow_type = values.type
    if pa.types.is_timestamp(arrow_type) or pa.types.is_date(arrow_type):
        values = values.cast(pa.timestamp("ns")) if pa.types.is_date(arrow_type) else values
        nanoseconds = values.cast(pa.timestamp("ns", tz=getattr(values.type, "tz", None)))
        return _to_float_numpy(nanoseconds.cast(pa.int64()))
    if pa.types.is_integer(arrow_type) or pa.types.is_floating(arrow_type):
        return _to_float_numpy(values)
    if pa.types.is_decimal(arrow_type):
        return _to_float_numpy(values)
    raise TypeError(f"Arrow 타입 {arrow_type}은 수치 좌표로 바꿀 수 없다")


def _to_float_numpy(values: pa.Array | pa.ChunkedArray) -> npt.NDArray[np.float64]:
    # epoch 나노초는 2^53을 넘으므로 정밀도 손실을 허용한다 (해상도 약 256ns, 모듈 설명 참고)
    floats = values.cast(pa.float64(), safe=False)
    if isinstance(floats, pa.ChunkedArray):
        floats = floats.combine_chunks() if floats.num_chunks else pa.array([], pa.float64())
    result: npt.NDArray[np.float64] = floats.to_numpy(zero_copy_only=False)
    return result


def to_native_bound(x: float, column: ColumnInfo | None) -> Any:
    """정규 좌표 하나 → 그 컬럼과 비교할 수 있는 원래 타입의 값 (범위 필터용)."""
    if column is None or column.kind is not ColumnKind.TIME:
        return x
    timestamp = pd.Timestamp(round(x), unit="ns")
    return timestamp.tz_localize("UTC") if column.timezone else timestamp


def format_x(x: float, column: ColumnInfo | None) -> str:
    """정규 좌표를 사람이 읽는 문자열로 바꾼다 (호버 라벨 등).

    tz-aware 시간은 원본 시간대로 바꿔 보여준다.
    """
    if column is None or column.kind is not ColumnKind.TIME:
        return f"{x:.6g}"
    timestamp = pd.Timestamp(round(x), unit="ns")
    if column.timezone:
        timestamp = timestamp.tz_localize("UTC").tz_convert(column.timezone)
    return timestamp.isoformat()


def _holds_dates(values: pd.Series) -> bool:
    """object 컬럼이 datetime.date / datetime 값을 담고 있는지 (첫 비결측 값으로 판단)."""
    if not pd.api.types.is_object_dtype(values):
        return False
    non_null = values.dropna()
    return not non_null.empty and isinstance(non_null.iloc[0], dt.date)
