"""Ingest backend protocol definition."""

from pathlib import Path
from typing import Protocol, runtime_checkable

from webchart.models.dataset import DatasetMeta, RemoteSourceSpec


@runtime_checkable
class IngestBackend(Protocol):
    """Protocol for data ingestion backends."""

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        """Ingest data from source and write as partitioned Parquet."""
        ...

    @property
    def supported_protocols(self) -> list[str]:
        """List of URI schemes this backend handles."""
        ...
