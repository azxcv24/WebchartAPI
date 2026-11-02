"""문자열·날짜로 들어온 시간 컬럼을 감지해 timestamp로 바꾼다.

CSV·TSV·JSON은 시간 값을 문자열로 담는다. 문자열로 저장하면 x축 범위를 계산할 수
없으므로(D3) 인제스트 단계에서 timestamp로 바꾼다. 날짜 전용 값(date32, datetime.date)도
자정 시각의 timestamp로 바꿔, 이후 단계가 시간 타입 하나만 다루게 한다.

감지 규칙: 표본의 비결측 값이 **모두** ISO 8601로 파싱되는 문자열 컬럼만 시간 컬럼으로
본다. 일부만 파싱되는 컬럼은 범주형 문자열일 가능성이 높아 건드리지 않는다.

시간대 처리
- 오프셋이 없는 값(naive)은 그대로 naive timestamp로 저장한다 (벽시계 값 유지).
- 오프셋이 있는 값은 UTC timestamp로 저장한다. 오프셋이 섞여 있어도 UTC로 통일된다.
"""

import datetime as dt
import logging
import warnings
from dataclasses import dataclass

import dask.dataframe as dd
import pandas as pd

logger = logging.getLogger("webchart.ingest.time_columns")

# 감지에 쓰는 표본 행 수 (첫 파티션 앞부분)
SAMPLE_ROWS = 1000


@dataclass(frozen=True, slots=True)
class TimeColumn:
    name: str
    is_tz_aware: bool
    is_text: bool = True  # False면 날짜 값(date)이라 ISO 8601 파싱 형식을 쓰지 않는다


def detect_time_columns(sample: pd.DataFrame) -> list[TimeColumn]:
    """표본에서 ISO 8601 시간 문자열 컬럼과 날짜 전용 컬럼을 찾는다."""
    detected: list[TimeColumn] = []
    for name in sample.columns:
        values = sample[name].dropna()
        if values.empty:
            continue
        if _is_date_column(values):
            detected.append(TimeColumn(str(name), is_tz_aware=False, is_text=False))
            continue
        if not _is_string_column(values):
            continue
        parsed = _parse_iso8601(values)
        if parsed is not None:
            detected.append(TimeColumn(str(name), is_tz_aware=parsed.dt.tz is not None))
    return detected


def parse_time_columns(ddf: dd.DataFrame, columns: list[TimeColumn]) -> dd.DataFrame:
    """감지된 컬럼을 timestamp로 바꾼다.

    표본 밖에서 파싱되지 않는 값은 NaT(결측)가 된다. 대용량 인제스트 전체를 값 하나
    때문에 실패시키지 않기 위해서이고, 결측 개수는 스키마 통계(null_count)에 드러난다.
    """
    for column in columns:
        ddf[column.name] = dd.to_datetime(
            ddf[column.name],
            format="ISO8601" if column.is_text else None,
            utc=column.is_tz_aware,
            errors="coerce",
        )
        logger.info(f"시간 컬럼으로 변환: {column.name} (tz-aware={column.is_tz_aware})")
    return ddf


def _is_date_column(values: pd.Series) -> bool:
    """날짜 전용 값(시각 없음)인지: Arrow date 타입이거나 datetime.date 객체."""
    dtype = values.dtype
    if isinstance(dtype, pd.ArrowDtype):
        import pyarrow as pa

        return bool(pa.types.is_date(dtype.pyarrow_dtype))
    if not pd.api.types.is_object_dtype(dtype):
        return False
    return bool(
        values.map(
            lambda value: isinstance(value, dt.date) and not isinstance(value, dt.datetime)
        ).all()
    )


def _is_string_column(values: pd.Series) -> bool:
    if pd.api.types.is_string_dtype(values) and not pd.api.types.is_object_dtype(values):
        return True
    return bool(values.map(lambda value: isinstance(value, str)).all())


def _parse_iso8601(values: pd.Series) -> pd.Series | None:
    """모든 값이 ISO 8601이면 파싱 결과를, 아니면 None을 반환한다.

    naive로 먼저 시도하고, 오프셋이 섞여 실패하면 UTC로 다시 시도한다.
    """
    for to_utc in (False, True):
        try:
            with warnings.catch_warnings():
                # 오프셋이 섞인 값을 utc=False로 파싱하면 pandas가 FutureWarning과 함께
                # object 결과를 돌려준다. 이 경우는 아래 dtype 검사로 걸러 UTC로 재시도한다.
                warnings.simplefilter("ignore", FutureWarning)
                parsed = pd.to_datetime(values, format="ISO8601", utc=to_utc)
        except (ValueError, TypeError, OverflowError):
            continue
        if pd.api.types.is_datetime64_any_dtype(parsed):
            return parsed
    return None
