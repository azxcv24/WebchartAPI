"""로컬 파일시스템 인제스트 백엔드 (file:// URI).

서버 디스크를 읽는 기능이라 기본으로 꺼져 있다. `WEBCHART_LOCAL_INGEST_ROOT`로 지정한
디렉터리 안의 파일만 읽는다 (경로를 정규화한 뒤 검사하므로 `..`나 심볼릭 링크로 빠져나갈
수 없다). 지정하지 않으면 file:// 요청은 거부된다.
"""

import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse

from webchart.config import settings
from webchart.core.errors import ConnectorError
from webchart.models.dataset import DatasetMeta, RemoteSourceSpec
from webchart.services.ingest.common import enforce_size, validate_remote_format
from webchart.services.ingest.upload import convert_to_parquet


class LocalIngestBackend:
    """서버 로컬 경로의 파일을 uploads/로 복사한 뒤 Parquet으로 변환한다."""

    @property
    def supported_protocols(self) -> list[str]:
        return ["file"]

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        source_path = _allowed_path(spec.uri)
        suffix = validate_remote_format(spec.uri)
        enforce_size(source_path.stat().st_size, suffix)

        # 데이터셋 ID는 dest(data_dir/datasets/<dataset_id>)에 이미 들어 있다. 새로 만들지
        # 않고 그대로 써야 호출자가 응답한 ID와 디스크의 데이터셋이 일치한다.
        # 복사본을 쓰는 이유: convert_to_parquet은 끝나면 입력 파일을 지운다.
        dataset_id = dest.name
        temp_path = settings.data_dir / "uploads" / f"{dataset_id}{source_path.suffix}"
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, temp_path)

        return convert_to_parquet(temp_path, dataset_id)


def _allowed_path(uri: str) -> Path:
    """file:// URI → 허용 디렉터리 안의 실제 파일 경로. 밖이면 ConnectorError."""
    root = settings.local_ingest_root
    if root is None:
        raise ConnectorError("file:// 인제스트가 꺼져 있다 (WEBCHART_LOCAL_INGEST_ROOT 미설정)")
    path = Path(unquote(urlparse(uri).path))
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as error:
        raise ConnectorError(f"파일이 없다: {path}") from error
    if not resolved.is_file() or not resolved.is_relative_to(root.resolve()):
        raise ConnectorError(f"허용 디렉터리 밖의 경로다: {path}")
    return resolved
