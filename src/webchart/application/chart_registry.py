"""차트 등록: spec을 저장하고 내용 해시를 ID로 쓴다 (계획 문서 4.2).

같은 spec은 항상 같은 chart_id다. 그래서 임베드 URL·XYZ 타일 URL이 안정적이고,
CDN·브라우저 캐시를 그대로 쓸 수 있다.
"""

import hashlib
import hmac
import json
from pathlib import Path

from webchart.config import settings
from webchart.core.errors import WebchartError
from webchart.models.chart import ChartSpec

CHART_ID_LENGTH = 20


class ChartNotFoundError(WebchartError):
    def __init__(self, chart_id: str):
        super().__init__(f"Chart not found: {chart_id}", "CHART_NOT_FOUND")


def chart_id_of(spec: ChartSpec) -> str:
    canonical = json.dumps(spec.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:CHART_ID_LENGTH]


def register_chart(spec: ChartSpec) -> str:
    chart_id = chart_id_of(spec)
    path = _chart_path(chart_id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_suffix(".tmp")
        staging.write_text(spec.model_dump_json(indent=2))
        staging.replace(path)
    return chart_id


def load_chart(chart_id: str) -> ChartSpec:
    if not chart_id.isalnum() or len(chart_id) != CHART_ID_LENGTH:
        raise ChartNotFoundError(chart_id)  # 경로 조작 방지: 16진 ID만 받는다
    path = _chart_path(chart_id)
    if not path.exists():
        raise ChartNotFoundError(chart_id)
    return ChartSpec.model_validate_json(path.read_text())


def embed_token(chart_id: str) -> str | None:
    """임베드 URL용 토큰 (HMAC). 인증이 꺼져 있으면 None (토큰 없이 열린다).

    `<img src>`나 CDN은 헤더를 붙일 수 없으므로, API 키 대신 차트 하나에만 유효한
    토큰을 URL에 싣는다. 키 자체는 URL에 노출되지 않는다.
    """
    secret = _embed_secret()
    if secret is None:
        return None
    return hmac.new(secret, chart_id.encode(), hashlib.sha256).hexdigest()[:32]


def is_valid_token(chart_id: str, token: str | None) -> bool:
    expected = embed_token(chart_id)
    if expected is None:
        return True
    return token is not None and hmac.compare_digest(expected, token)


def _embed_secret() -> bytes | None:
    if settings.embed_secret:
        return settings.embed_secret.encode()
    if settings.api_keys:
        return hashlib.sha256("\n".join(sorted(settings.api_keys)).encode()).digest()
    return None


def _chart_path(chart_id: str) -> Path:
    return settings.data_dir / "charts" / f"{chart_id}.json"
