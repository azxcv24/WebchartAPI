"""Local filesystem ingest backend."""

import shutil
import uuid
from pathlib import Path

from webchart.config import settings
from webchart.models.dataset import DatasetMeta, RemoteSourceSpec
from webchart.services.ingest.upload import convert_to_parquet


class LocalIngestBackend:
    """Ingest data from local filesystem paths."""

    @property
    def supported_protocols(self) -> list[str]:
        return ["file"]

    async def validate_source(self, spec: RemoteSourceSpec) -> bool:
        path = Path(spec.uri.replace("file://", ""))
        return path.exists() and path.is_file()

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        source_path = Path(spec.uri.replace("file://", ""))

        # Copy to temp location for processing
        dataset_id = str(uuid.uuid4())
        temp_path = settings.data_dir / "uploads" / f"{dataset_id}{source_path.suffix}"
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, temp_path)

        return convert_to_parquet(temp_path, dataset_id)
