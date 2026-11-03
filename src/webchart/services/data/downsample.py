"""이전 버전의 근사 다운샘플링 (LTTB, min-max, average).

무왜곡 기본 경로는 M4(domain/projection.py)다. 이 모듈은 클라이언트가 `downsample_algorithm`에
"m4"가 아닌 값을 명시했을 때만, M4로 만든 꺾은선을 한 번 더 줄이는 데 쓴다
(application/static_chart.py). 결과는 원본과 픽셀 단위로 같지 않으므로 응답은 `exact=false`다.

- LTTB (Largest Triangle Three Buckets): 모양을 비교적 잘 살리는 점 선택
- min-max: 구간마다 최솟값·최댓값 행만 남긴다
- average: 구간마다 평균 행 하나로 줄인다
"""

import logging

import numpy as np
import pandas as pd

from webchart.services.data.x_domain import to_canonical

logger = logging.getLogger("webchart.data.downsample")


def downsample(
    df: pd.DataFrame,
    x_column: str,
    y_column: str,
    target_points: int,
    algorithm: str = "lttb",
) -> pd.DataFrame:
    """`df`를 대략 `target_points`행으로 줄인다. 이미 그 이하면 그대로 돌려준다.

    `df`는 `x_column` 오름차순이어야 한다. `algorithm`은 "lttb", "minmax", "average" 중
    하나이고, 모르는 값이면 경고를 남기고 LTTB를 쓴다.
    """
    n = len(df)
    if n <= target_points:
        return df

    if algorithm == "lttb":
        return _lttb(df, x_column, y_column, target_points)
    elif algorithm == "minmax":
        return _minmax(df, x_column, y_column, target_points)
    elif algorithm == "average":
        return _average(df, x_column, y_column, target_points)
    else:
        logger.warning(f"Unknown algorithm '{algorithm}', falling back to LTTB")
        return _lttb(df, x_column, y_column, target_points)


def _lttb(
    df: pd.DataFrame,
    x_column: str,
    y_column: str,
    target_points: int,
) -> pd.DataFrame:
    """LTTB (Sveinn Steinarsson, 2013). 첫 행과 마지막 행은 항상 남긴다.

    버킷마다 직전 선택점·다음 버킷 평균점과 만드는 삼각형 면적이 가장 큰 행을 고른다.
    직전 선택점이 다음 버킷 계산에 들어가므로 바깥 루프는 순차(Python)이고,
    버킷 안의 면적 탐색만 NumPy로 벡터화한다.
    """
    n = len(df)
    if target_points < 3:
        # 삼각형을 만들 수 없다: 양 끝점만 남긴다 (1개면 첫 점)
        if n <= target_points:
            return df.copy()
        idx = [0] if target_points == 1 else [0, n - 1]
        return df.iloc[idx].reset_index(drop=True)

    x = to_canonical(df[x_column])  # 시간 x축도 삼각형 면적을 계산할 수 있게 수치로 바꾼다
    y = df[y_column].to_numpy(dtype=np.float64, copy=False)

    bucket_size = (n - 2) / (target_points - 2)

    # 버킷 k = [edges[k], edges[k+1]). 첫 행은 이미 남겼으므로 edges[0] = 1, 끝은 n으로 막는다.
    edges = (np.arange(target_points) * bucket_size).astype(np.int64) + 1
    edges = np.minimum(edges, n)
    edges[0] = 1
    edges[-1] = n

    selected = np.empty(target_points, dtype=np.int64)
    selected[0] = 0
    selected[-1] = n - 1

    prev_idx = 0
    for k in range(target_points - 2):
        bs, be = int(edges[k]), int(edges[k + 1])
        nbs, nbe = int(edges[k + 1]), int(edges[k + 2])

        if be <= bs:
            # 빈 버킷: 직전 선택 행을 되풀이한다
            selected[k + 1] = prev_idx
            continue

        if nbe > nbs:
            avg_x = x[nbs:nbe].mean()
            avg_y = y[nbs:nbe].mean()
        else:
            avg_x = x[-1]
            avg_y = y[-1]

        # 현재 버킷 모든 행의 삼각형 면적 (상수배는 비교에 영향이 없어 생략)
        cx = x[bs:be]
        cy = y[bs:be]
        area = np.abs(
            (x[prev_idx] - avg_x) * (cy - y[prev_idx]) - (x[prev_idx] - cx) * (avg_y - y[prev_idx])
        )
        max_offset = int(np.argmax(area))
        chosen = bs + max_offset
        selected[k + 1] = chosen
        prev_idx = chosen

    return df.iloc[selected].reset_index(drop=True)


def _minmax(
    df: pd.DataFrame,
    x_column: str,
    y_column: str,
    target_points: int,
) -> pd.DataFrame:
    """행 수로 균등 분할한 구간마다 y 최솟값·최댓값 행을 x 순서대로 남긴다.

    구간 하나가 두 행을 내므로 구간 수는 `target_points // 2`다. 같은 행이 겹치면 한 번만 남긴다.
    """
    n = len(df)
    num_buckets = target_points // 2
    bucket_size = n / num_buckets

    selected_indices = []

    for i in range(num_buckets):
        start = int(i * bucket_size)
        end = int((i + 1) * bucket_size)
        end = min(end, n)

        if start >= end:
            continue

        bucket = df.iloc[start:end]
        y_vals = bucket[y_column]

        min_idx = y_vals.idxmin()
        max_idx = y_vals.idxmax()

        # x 순서(행 순서)를 지킨다
        if min_idx <= max_idx:
            selected_indices.extend([min_idx, max_idx])
        else:
            selected_indices.extend([max_idx, min_idx])

    seen = set()
    unique_indices = []
    for idx in selected_indices:
        if idx not in seen:
            seen.add(idx)
            unique_indices.append(idx)

    return df.loc[unique_indices].reset_index(drop=True)


def _average(
    df: pd.DataFrame,
    x_column: str,
    y_column: str,
    target_points: int,
) -> pd.DataFrame:
    """행 수로 균등 분할한 구간마다 평균 행 하나를 만든다.

    수치 컬럼은 평균, 그 밖의 컬럼은 구간 가운데 행의 값을 쓴다.
    """
    n = len(df)
    bucket_indices = np.linspace(0, n, target_points + 1, dtype=int)

    rows = []
    for i in range(target_points):
        start = bucket_indices[i]
        end = bucket_indices[i + 1]
        if start >= end:
            continue

        bucket = df.iloc[start:end]
        row = {x_column: bucket[x_column].mean()}

        for col in df.columns:
            if col != x_column:
                if pd.api.types.is_numeric_dtype(df[col]):
                    row[col] = bucket[col].mean()
                else:
                    row[col] = bucket[col].iloc[len(bucket) // 2]

        rows.append(row)

    return pd.DataFrame(rows)
