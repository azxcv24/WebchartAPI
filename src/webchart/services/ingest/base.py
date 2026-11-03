"""인제스트 백엔드 프로토콜."""

from pathlib import Path
from typing import Protocol, runtime_checkable

from webchart.models.dataset import DatasetMeta, RemoteSourceSpec


@runtime_checkable
class IngestBackend(Protocol):
    """원격·로컬 소스에서 데이터를 가져와 데이터셋으로 저장하는 커넥터.

    구현체는 registry.register_backend로 URI 스킴별로 등록된다.
    """

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        """spec이 가리키는 소스를 가져와 정렬된 Parquet 데이터셋으로 저장한다.

        데이터셋 ID는 dest의 마지막 경로 요소(data_dir/datasets/<dataset_id>)다.
        """
        ...

    @property
    def supported_protocols(self) -> list[str]:
        """이 백엔드가 처리하는 URI 스킴 목록 (예: ["s3", "minio"])."""
        ...
