"""Local filesystem ingest backend."""

import shutil
from pathlib import Path

from webchart.config import settings
from webchart.models.dataset import DatasetMeta, RemoteSourceSpec
from webchart.services.ingest.upload import convert_to_parquet


class LocalIngestBackend:
    """Ingest data from local filesystem paths."""

    @property
    def supported_protocols(self) -> list[str]:
        return ["file"]

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        source_path = Path(spec.uri.replace("file://", ""))

        # The dataset_id is encoded in `dest` (data_dir/datasets/<dataset_id>);
        # honor it instead of minting a new one so the caller's response id
        # matches the on-disk dataset.
        dataset_id = dest.name
        temp_path = settings.data_dir / "uploads" / f"{dataset_id}{source_path.suffix}"
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, temp_path)

        return convert_to_parquet(temp_path, dataset_id)
