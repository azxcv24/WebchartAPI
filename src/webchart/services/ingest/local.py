"""로컬 파일시스템 인제스트 백엔드 (file:// URI)."""

import shutil
from pathlib import Path

from webchart.config import settings
from webchart.models.dataset import DatasetMeta, RemoteSourceSpec
from webchart.services.ingest.upload import convert_to_parquet


class LocalIngestBackend:
    """서버 로컬 경로의 파일을 uploads/로 복사한 뒤 Parquet으로 변환한다."""

    @property
    def supported_protocols(self) -> list[str]:
        return ["file"]

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        source_path = Path(spec.uri.replace("file://", ""))

        # 데이터셋 ID는 dest(data_dir/datasets/<dataset_id>)에 이미 들어 있다. 새로 만들지
        # 않고 그대로 써야 호출자가 응답한 ID와 디스크의 데이터셋이 일치한다.
        # 복사본을 쓰는 이유: convert_to_parquet은 끝나면 입력 파일을 지운다.
        dataset_id = dest.name
        temp_path = settings.data_dir / "uploads" / f"{dataset_id}{source_path.suffix}"
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, temp_path)

        return convert_to_parquet(temp_path, dataset_id)
